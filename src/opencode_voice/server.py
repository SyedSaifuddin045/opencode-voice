"""Localhost HTTP API + orchestration for the speech daemon.

Endpoints:
    POST /speak    {"text": ...}                  -> one-shot speech
    POST /stream   {"sessionID","partID","text"}  -> streaming snapshot diff
    POST /flush    {"sessionID"}                  -> speak remaining partials
    POST /stop     {}                             -> interrupt + clear queue
    POST /enabled  {"enabled": bool}              -> runtime mute/unmute
    GET  /health   -> {"ok", "model_loaded"}
    GET  /status   -> {running, model_loaded, speaking, queue_length, ...}
    POST /shutdown -> graceful daemon exit

Text path: adapter (per part) -> sentence -> synth worker -> playback queue.
HTTP threads never touch the model; a single synth worker does.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .playback import scale_pcm_f32
from .queue import SpeechQueue, SpeechTask
from .speech_adapter import SpeechAdapter

log = logging.getLogger("opencode_voice.server")

_MAX_BODY = 4 * 1024 * 1024


class SpeechService:
    def __init__(self, config: Config, tts, player):
        self.config = config
        self.tts = tts
        self.playback = SpeechQueue(player, max_queue=config.max_queue)
        self._synth_q: deque[str] = deque()
        self._cv = threading.Condition()
        self._adapters: dict[tuple[str, str], SpeechAdapter] = {}
        self._part_session: dict[str, str] = {}
        self._parts: dict[str, str] = {}
        self._shutdown = False
        self._gen = 0  # bumped by stop(); synth results from older gens drop
        self._last_stop_msg: str | None = None  # messageID idempotency
        self.enabled = self._load_enabled()
        self.last_activity = time.time()
        self._threads: list[threading.Thread] = []
        self.started_at = time.time()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- enabled

    def _load_enabled(self) -> bool:
        try:
            raw = Path(self.config.enabled_file).read_text().strip()
        except OSError:
            return True
        return raw != "0"

    def set_enabled(self, value: bool) -> None:
        """Runtime toggle: persist, and mute (stop speech) when disabling."""
        self.enabled = bool(value)
        try:
            Path(self.config.enabled_file).parent.mkdir(parents=True, exist_ok=True)
            Path(self.config.enabled_file).write_text("1" if value else "0")
        except OSError:
            log.exception("failed to persist enabled state")
        if not value:
            self.stop()
        log.info("speech %s", "enabled" if value else "disabled")

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.playback.start()
        worker = threading.Thread(
            target=self._synth_loop, name="speech-synth", daemon=True
        )
        worker.start()
        self._threads.append(worker)

    def shutdown(self) -> None:
        with self._cv:
            self._shutdown = True
            self._synth_q.clear()
            self._cv.notify_all()
        self.playback.stop_speech()
        self.playback.stop_worker()
        for t in self._threads:
            t.join(timeout=2)

    # -------------------------------------------------------------- inputs

    def _new_adapter(self) -> SpeechAdapter:
        cfg = self.config
        return SpeechAdapter(
            speak_code=cfg.speak_code,
            speak_urls=cfg.speak_urls,
            pronunciations=cfg.pronunciations,
            max_sentence_chars=cfg.max_sentence_chars,
        )

    def speak(self, text: str) -> int:
        """One-shot: adapt full text and enqueue every sentence."""
        if not self.enabled:
            return 0
        self.last_activity = time.time()
        if not text or not text.strip():
            return 0
        adapter = self._new_adapter()
        sentences = adapter.feed(text) + adapter.flush()
        for s in sentences:
            self._queue_sentence(s)
        return len(sentences)

    def stream(self, session_id: str, part_id: str, text: str) -> int:
        """Streaming snapshot for one text part (diffed against last)."""
        if not self.enabled:
            return 0
        self.last_activity = time.time()
        prev = self._parts.get(part_id, "")
        key = (session_id, part_id)
        if text.startswith(prev):
            delta = text[len(prev):]
        else:
            adapter = self._adapters.get(key)
            if adapter is not None:
                adapter.reset()
            delta = text
        self._parts[part_id] = text
        self._part_session[part_id] = session_id
        if not delta:
            return 0
        if not self.config.stream_sentences:
            return 0  # deferred until /flush
        adapter = self._adapters.get(key)
        if adapter is None:
            adapter = self._new_adapter()
            self._adapters[key] = adapter
        sentences = adapter.feed(delta)
        for s in sentences:
            self._queue_sentence(s)
        return len(sentences)

    def flush(self, session_id: str | None = None) -> int:
        """Speak everything still buffered (stream or deferred mode)."""
        if not self.enabled:
            return 0
        self.last_activity = time.time()
        count = 0
        if not self.config.stream_sentences:
            # deferred mode: adapt full snapshots now
            for part_id, text in list(self._parts.items()):
                if session_id and self._part_session.get(part_id) != session_id:
                    continue
                count += self.speak(text)
            self._clear_session(session_id)
            return count
        for key in list(self._adapters):
            sid, _ = key
            if session_id and sid != session_id:
                continue
            adapter = self._adapters.pop(key)
            for s in adapter.flush():
                self._queue_sentence(s)
                count += 1
        if session_id:
            self._clear_session(session_id)
        return count

    def stop(self, message_id: str | None = None) -> None:
        """Interrupt: drop pending synthesis, pending playback, cut audio.

        message_id: plugin sends the user-message id; repeated stops for
        the same message (opencode re-emits message.updated at turn end)
        are ignored so they cannot kill the assistant's own reply.
        """
        self.last_activity = time.time()
        if message_id is not None:
            if message_id == self._last_stop_msg:
                log.info("ignoring duplicate stop for %s", message_id)
                return
            self._last_stop_msg = message_id
        with self._cv:
            self._gen += 1
            self._synth_q.clear()
            self._cv.notify_all()
        for adapter in self._adapters.values():
            adapter.reset()
        self._adapters.clear()
        self._parts.clear()
        self._part_session.clear()
        self.playback.stop_speech()

    # -------------------------------------------------------------- status

    def status(self) -> dict:
        with self._cv:
            synth_pending = len(self._synth_q)
        return {
            "running": True,
            "enabled": self.enabled,
            "model_loaded": bool(getattr(self.tts, "loaded", False)),
            "speaking": self.playback.is_speaking(),
            "queue_length": self.playback.length(),
            "synth_pending": synth_pending,
            "voice": getattr(self.tts, "voice", None),
            "uptime_s": round(time.time() - self.started_at, 1),
        }

    # ------------------------------------------------------------- internal

    def _queue_sentence(self, sentence: str) -> None:
        if not sentence:
            return
        with self._cv:
            self._synth_q.append(sentence)
            self._cv.notify()

    def _clear_session(self, session_id: str | None) -> None:
        if session_id is None:
            return
        for part_id in [
            p for p, s in self._part_session.items() if s == session_id
        ]:
            self._parts.pop(part_id, None)
            self._part_session.pop(part_id, None)

    def _synth_loop(self) -> None:
        while True:
            with self._cv:
                while not self._synth_q and not self._shutdown:
                    self._cv.wait()
                if self._shutdown:
                    return
                text = self._synth_q.popleft()
                gen = self._gen
            try:
                pcm, sr = self.tts.synth(text)
                with self._cv:
                    stale = gen != self._gen
                if stale:
                    log.info("dropped stale sentence (interrupted): %.40r", text)
                    continue
                pcm = scale_pcm_f32(pcm, self.config.volume)
                log.info("enqueue playback: %.60r (%.2fs)", text, len(pcm) / 4 / sr)
                self.playback.enqueue(SpeechTask(text=text, pcm=pcm, samplerate=sr))
            except Exception:
                log.exception("synthesis failed for %.80r", text)


class _Handler(BaseHTTPRequestHandler):
    service: SpeechService = None  # injected via class factory
    config: Config = None

    def log_message(self, fmt, *args):
        logging.getLogger("opencode_voice.http").debug(fmt, *args)

    # ------------------------------------------------------------- helpers

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return {}
        if length > _MAX_BODY:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # -------------------------------------------------------------- routes

    def do_GET(self):
        if self.path == "/health":
            self._send(
                200,
                {
                    "ok": True,
                    "model_loaded": bool(
                        getattr(self.service.tts, "loaded", False)
                    ),
                },
            )
        elif self.path == "/status":
            self._send(200, self.service.status())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        if body is None:
            self._send(400, {"error": "invalid json body"})
            return
        log.info("POST %s %s", self.path, json.dumps(body)[:120])
        try:
            if self.path == "/speak":
                text = str(body.get("text") or "")
                n = self.service.speak(text)
                if not self.service.enabled:
                    self._send(200, {"status": "disabled", "sentences": 0,
                                     "enabled": False})
                    return
                self._send(202, {"status": "queued", "sentences": n})
            elif self.path == "/stream":
                session_id = str(body.get("sessionID") or "")
                part_id = str(body.get("partID") or "")
                text = str(body.get("text") or "")
                if not session_id or not part_id:
                    self._send(400, {"error": "sessionID and partID required"})
                    return
                n = self.service.stream(session_id, part_id, text)
                if not self.service.enabled:
                    self._send(200, {"sentences": 0, "enabled": False})
                    return
                self._send(202, {"sentences": n})
            elif self.path == "/flush":
                session_id = body.get("sessionID")
                n = self.service.flush(str(session_id) if session_id else None)
                if not self.service.enabled:
                    self._send(200, {"sentences": 0, "enabled": False})
                    return
                self._send(202, {"sentences": n})
            elif self.path == "/stop":
                mid = body.get("messageID")
                self.service.stop(str(mid) if mid else None)
                self._send(200, {"status": "stopped"})
            elif self.path == "/enabled":
                if "enabled" not in body:
                    self._send(400, {"error": "enabled required"})
                    return
                self.service.set_enabled(bool(body["enabled"]))
                self._send(200, {"enabled": self.service.enabled})
            elif self.path == "/shutdown":
                self._send(200, {"status": "shutting down"})
                threading.Thread(
                    target=self._shutdown_graceful, daemon=True
                ).start()
            else:
                self._send(404, {"error": "not found"})
        except Exception:
            log.exception("request failed: %s", self.path)
            self._send(500, {"error": "internal error"})

    def _shutdown_graceful(self):
        time.sleep(0.1)
        self.server.should_shutdown = True
        self.server.shutdown()


def build_server(config: Config, service: SpeechService):
    """Create threaded HTTP server bound to localhost; start serving."""
    handler = type(
        "BoundHandler",
        (_Handler,),
        {"service": service, "config": config},
    )
    httpd = ThreadingHTTPServer((config.host, config.port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    thread.start()
    return httpd, thread
