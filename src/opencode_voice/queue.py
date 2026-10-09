"""Audio speech queue: ordered playback, interrupt, clear.

Single worker thread plays PCM buffers through a player object
(simpleaudio/sounddevice/afplay wrapper). Errors in one item never
kill the worker. stop_speech() clears pending work and stops current
audio immediately.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger("opencode_voice.queue")


@dataclass
class SpeechTask:
    text: str
    pcm: bytes | None = None
    samplerate: int = 24000
    meta: dict = field(default_factory=dict)


class SpeechQueue:
    def __init__(self, player, max_queue: int = 32):
        self.player = player
        self.max_queue = max_queue
        self._q: deque[SpeechTask] = deque()
        self._cv = threading.Condition()
        self._worker: threading.Thread | None = None
        self._shutdown = False
        self._speaking = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self._cv:
            if self._worker and self._worker.is_alive():
                return
            self._shutdown = False
            self._worker = threading.Thread(
                target=self._run, name="speech-queue", daemon=True
            )
            self._worker.start()

    def stop_worker(self) -> None:
        with self._cv:
            self._shutdown = True
            self._cv.notify_all()
            worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=2)

    # ------------------------------------------------------------ commands

    def enqueue(self, task: SpeechTask) -> None:
        with self._cv:
            if len(self._q) >= self.max_queue:
                dropped = self._q.popleft()
                log.warning("queue full, dropped oldest: %.60s", dropped.text)
            self._q.append(task)
            self._cv.notify()

    def clear(self) -> None:
        with self._cv:
            self._q.clear()

    def stop_speech(self) -> None:
        """Stop current audio immediately and drop everything pending."""
        with self._cv:
            self._q.clear()
            self._cv.notify_all()
        try:
            self.player.stop()
        except Exception:
            log.exception("player.stop failed")

    def length(self) -> int:
        with self._cv:
            return len(self._q)

    def is_speaking(self) -> bool:
        with self._cv:
            return self._speaking

    # ------------------------------------------------------------- worker

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._q and not self._shutdown:
                    self._cv.wait()
                if self._shutdown:
                    return
                task = self._q.popleft()
                self._speaking = True
            try:
                if task.pcm is not None:
                    self.player.play(task.pcm, task.samplerate)
            except Exception:
                # never let a playback error kill the worker
                log.exception("playback failed for %.60s", task.text)
            finally:
                with self._cv:
                    self._speaking = False
