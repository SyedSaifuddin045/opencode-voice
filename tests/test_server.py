import json
import struct
import threading
import time
from http.client import HTTPConnection

import pytest

from opencode_voice.config import Config
from opencode_voice.server import SpeechService, build_server


class FakeTTS:
    loaded = True
    sample_rate = 24000

    def __init__(self):
        self.calls: list[str] = []
        self.delay = 0.0

    def synth(self, text):
        self.calls.append(text)
        if self.delay:
            time.sleep(self.delay)
        return struct.pack("<4f", 0.0, 0.0, 0.0, 0.0), 24000


class FakePlayer:
    def __init__(self):
        self.played = []
        self.stopped = False
        self.lock = threading.Lock()

    def play(self, pcm, samplerate):
        with self.lock:
            self.played.append(pcm)

    def stop(self):
        self.stopped = True


class Env:
    def __init__(self, config, tts, player, httpd, thread):
        self.config = config
        self.tts = tts
        self.player = player
        self.httpd = httpd
        self.thread = thread
        self.port = httpd.server_address[1]

    def request(self, method, path, body=None, timeout=3):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else None)

    def wait_played(self, n, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.player.played) >= n:
                return True
            time.sleep(0.01)
        return False

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def env(tmp_path):
    cfg = Config(port=0, enabled_file=str(tmp_path / "enabled"))
    tts = FakeTTS()
    player = FakePlayer()
    service = SpeechService(cfg, tts, player)
    httpd, thread = build_server(cfg, service)
    service.start()
    e = Env(cfg, tts, player, httpd, thread)
    yield e
    e.close()
    service.shutdown()


def test_health(env):
    status, body = env.request("GET", "/health")
    assert status == 200
    assert body["ok"] is True
    assert body["model_loaded"] is True


def test_status_shape(env):
    status, body = env.request("GET", "/status")
    assert status == 200
    for key in ("running", "model_loaded", "speaking", "queue_length", "voice"):
        assert key in body
    assert body["running"] is True
    assert body["queue_length"] == 0
    assert body["speaking"] is False


def test_speak_converts_markdown_and_plays(env):
    status, body = env.request(
        "POST", "/speak", {"text": "## Hello\n\nThis is **important**."}
    )
    assert status == 202
    assert body["sentences"] >= 1
    assert env.wait_played(body["sentences"])
    joined = " ".join(env.tts.calls)
    assert "#" not in joined
    assert "**" not in joined
    assert "important" in joined


def test_speak_empty_noop(env):
    status, body = env.request("POST", "/speak", {"text": ""})
    assert status == 202
    assert body["sentences"] == 0
    status, body = env.request("POST", "/speak", {"text": "   "})
    assert body["sentences"] == 0
    time.sleep(0.1)
    assert env.tts.calls == []


def test_stream_sentences_emitted_incrementally(env):
    env.request(
        "POST",
        "/stream",
        {"sessionID": "s1", "partID": "p1", "text": "I found the problem. The Docker cont"},
    )
    assert env.wait_played(1)
    assert env.tts.calls[0] == "I found the problem."
    env.request(
        "POST",
        "/stream",
        {
            "sessionID": "s1",
            "partID": "p1",
            "text": "I found the problem. The Docker container is crashing. Done.",
        },
    )
    assert env.wait_played(3)
    assert "The Docker container is crashing." in env.tts.calls
    assert "Done." in env.tts.calls


def test_stream_skips_code_blocks(env):
    env.request(
        "POST",
        "/stream",
        {"sessionID": "s1", "partID": "p1", "text": "Intro. ```\nx = 1\n``` Tail."},
    )
    assert env.wait_played(2)
    joined = " ".join(env.tts.calls)
    assert "x = 1" not in joined
    assert "Intro." in joined
    assert "Tail." in joined


def test_stream_duplicate_snapshot_not_spoken_twice(env):
    text = "Only once."
    for _ in range(3):
        env.request(
            "POST",
            "/stream",
            {"sessionID": "s1", "partID": "p1", "text": text},
        )
    assert env.wait_played(1)
    time.sleep(0.15)
    assert env.tts.calls.count("Only once.") == 1


def test_flush_speaks_remainder(env):
    env.request(
        "POST",
        "/stream",
        {"sessionID": "s1", "partID": "p1", "text": "Partial sentence no end"},
    )
    env.request("POST", "/flush", {"sessionID": "s1"})
    assert env.wait_played(1)
    assert "Partial sentence no end" in env.tts.calls


def test_stop_clears_queue_and_interrupts(env):
    env.tts.delay = 0.2
    env.request("POST", "/speak", {"text": "One. Two. Three. Four. Five."})
    status, body = env.request("POST", "/stop", {})
    assert status == 200
    time.sleep(0.05)
    assert env.player.stopped is True
    # queue fully drained/cleared: nothing more gets played after settle
    time.sleep(0.4)
    played_after_stop = len(env.player.played)
    time.sleep(0.3)
    assert len(env.player.played) == played_after_stop


def test_shutdown_endpoint(env):
    status, body = env.request("POST", "/shutdown", {})
    assert status == 200


def test_new_prompt_flow_stop_then_stream(env):
    # old response speaking
    env.tts.delay = 0.1
    env.request("POST", "/speak", {"text": "Old one. Old two. Old three."})
    # user interrupts with new prompt
    env.request("POST", "/stop", {})
    env.tts.delay = 0.0
    env.request(
        "POST",
        "/stream",
        {"sessionID": "s2", "partID": "p9", "text": "Fresh start."},
    )
    assert env.wait_played(1)
    assert "Fresh start." in env.tts.calls


def test_stop_during_synth_drops_stale_sentence(env):
    """stop() mid-synthesis must discard the in-flight PCM (gen race)."""
    env.tts.delay = 0.4
    status, body = env.request(
        "POST", "/speak", {"text": "One sentence here."}
    )
    assert status == 202 and body["sentences"] == 1
    time.sleep(0.1)  # synth worker now inside slow synth
    status, _ = env.request("POST", "/stop", {})
    assert status == 200
    time.sleep(0.6)  # slow synth completes; gen check must drop it
    assert len(env.player.played) == 0
    assert env.request("GET", "/status")[1]["queue_length"] == 0


def test_duplicate_stop_same_message_id_is_ignored(env):
    """Second stop for the same user message must not kill the reply."""
    status, _ = env.request(
        "POST", "/speak", {"text": "A sentence to protect."}
    )
    assert status == 202
    assert env.wait_played(1)
    # duplicate stop (turn-end re-fire): payload carries messageID
    status, _ = env.request(
        "POST", "/stop", {"sessionID": "s", "messageID": "msg_1"}
    )
    assert status == 200
    status, _ = env.request(
        "POST", "/stop", {"sessionID": "s", "messageID": "msg_1"}
    )
    assert status == 200
    # playback survived the ignored duplicate
    assert len(env.player.played) >= 1
    # a genuinely new prompt (new messageID) does interrupt
    status, _ = env.request(
        "POST", "/stop", {"sessionID": "s", "messageID": "msg_2"}
    )
    assert status == 200
    assert env.player.stopped is True


def test_enabled_toggle_gates_speech(env):
    """off: speech endpoints ignore input; on: speech resumes."""
    status, body = env.request("POST", "/enabled", {"enabled": False})
    assert status == 200 and body["enabled"] is False
    status, body = env.request("POST", "/speak", {"text": "Should be ignored."})
    assert status == 200
    assert body["sentences"] == 0
    assert body["enabled"] is False
    status, body = env.request(
        "POST", "/stream", {"sessionID": "s", "partID": "p", "text": "Nope."}
    )
    assert body["sentences"] == 0
    status, st = env.request("GET", "/status")
    assert st["enabled"] is False
    # nothing plays
    time.sleep(0.1)
    assert len(env.player.played) == 0
    # back on
    status, body = env.request("POST", "/enabled", {"enabled": True})
    assert body["enabled"] is True
    status, body = env.request("POST", "/speak", {"text": "Now speak again."})
    assert body["sentences"] >= 1
    assert env.wait_played(1)


def test_disable_interrupts_current_speech(env):
    status, _ = env.request("POST", "/speak", {"text": "Talking right now."})
    assert status == 202
    assert env.wait_played(1)
    status, body = env.request("POST", "/enabled", {"enabled": False})
    assert body["enabled"] is False
    # pending speech cleared, player stopped
    time.sleep(0.1)
    st = env.request("GET", "/status")[1]
    assert st["speaking"] is False
    assert st["queue_length"] == 0
    assert env.player.stopped is True


def test_status_includes_enabled(env):
    st = env.request("GET", "/status")[1]
    assert st["enabled"] is True
