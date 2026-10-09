import threading
import time

from opencode_voice.queue import SpeechQueue, SpeechTask


class FakePlayer:
    def __init__(self):
        self.played = []
        self.stopped = False

    def play(self, pcm, samplerate):
        self.played.append(pcm)

    def stop(self):
        self.stopped = True


def make_queue(player=None):
    return SpeechQueue(player or FakePlayer())


def test_enqueue_plays_in_order():
    q = make_queue()
    q.start()
    q.enqueue(SpeechTask(text="one", pcm=b"1", samplerate=24000))
    q.enqueue(SpeechTask(text="two", pcm=b"2", samplerate=24000))
    deadline = time.time() + 2
    while len(q.player.played) < 2 and time.time() < deadline:
        time.sleep(0.01)
    q.stop_worker()
    assert q.player.played == [b"1", b"2"]


def test_clear_empties_pending():
    q = make_queue()
    q.start()
    for i in range(5):
        q.enqueue(SpeechTask(text=str(i), pcm=b"x", samplerate=24000))
    q.clear()
    assert q.length() == 0


def test_stop_interrupts_current_and_clears():
    q = make_queue()
    q.start()
    for i in range(3):
        q.enqueue(SpeechTask(text=str(i), pcm=b"x", samplerate=24000))
    q.stop_speech()
    time.sleep(0.1)
    assert q.length() == 0
    assert q.player.stopped is True
    q.stop_worker()


def test_enqueue_after_clear_resumes():
    q = make_queue()
    q.start()
    q.clear()
    q.enqueue(SpeechTask(text="fresh", pcm=b"f", samplerate=24000))
    deadline = time.time() + 2
    while not q.player.played and time.time() < deadline:
        time.sleep(0.01)
    q.stop_worker()
    assert q.player.played == [b"f"]


def test_status_counts():
    q = make_queue()
    assert q.length() == 0
    q.enqueue(SpeechTask(text="a", pcm=b"a", samplerate=24000))
    # not started: stays queued
    assert q.length() == 1
    q.clear()
    assert q.length() == 0


def test_worker_error_does_not_kill_worker():
    q = make_queue()

    class BoomPlayer(FakePlayer):
        def play(self, pcm, samplerate):
            if pcm == b"bad":
                raise RuntimeError("boom")
            super().play(pcm, samplerate)

    q.player = BoomPlayer()
    q.start()
    q.enqueue(SpeechTask(text="bad", pcm=b"bad", samplerate=24000))
    q.enqueue(SpeechTask(text="good", pcm=b"good", samplerate=24000))
    deadline = time.time() + 2
    while q.player.played != [b"good"] and time.time() < deadline:
        time.sleep(0.01)
    q.stop_worker()
    assert q.player.played == [b"good"]


def test_speaking_flag():
    q = make_queue()
    gate = threading.Event()

    class SlowPlayer(FakePlayer):
        def play(self, pcm, samplerate):
            gate.wait(2)
            super().play(pcm, samplerate)

    q.player = SlowPlayer()
    q.start()
    q.enqueue(SpeechTask(text="s", pcm=b"s", samplerate=24000))
    deadline = time.time() + 2
    while not q.is_speaking() and time.time() < deadline:
        time.sleep(0.01)
    assert q.is_speaking() is True
    gate.set()
    deadline = time.time() + 2
    while q.is_speaking() and time.time() < deadline:
        time.sleep(0.01)
    assert q.is_speaking() is False
    q.stop_worker()
