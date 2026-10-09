import struct
import threading
import time

import pytest

from opencode_voice.playback import AfplayPlayer, pick_player, scale_pcm_f32


def pcm(*values: float) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def vals(b: bytes):
    return struct.unpack(f"<{len(b) // 4}f", b)


def test_scale_identity():
    b = pcm(0.5, -0.25, 1.0)
    assert scale_pcm_f32(b, 1.0) == b


def test_scale_half():
    out = vals(scale_pcm_f32(pcm(1.0, -1.0), 0.5))
    assert out == pytest.approx((0.5, -0.5))


def test_scale_zero():
    assert vals(scale_pcm_f32(pcm(1.0, 0.3), 0.0)) == pytest.approx((0.0, 0.0))


def test_scale_clips_at_one():
    out = vals(scale_pcm_f32(pcm(0.9, -0.9), 2.0))
    assert out == pytest.approx((1.0, -1.0))


def test_scale_odd_length_raises():
    with pytest.raises(ValueError):
        scale_pcm_f32(b"\x00\x00\x00", 1.0)


def test_pick_player_explicit_afplay():
    p = pick_player("afplay", volume=1.0)
    assert isinstance(p, AfplayPlayer)


def test_pick_player_auto_falls_back(monkeypatch):
    import opencode_voice.playback as pb

    def boom(volume=1.0):
        raise RuntimeError("no audio device")

    monkeypatch.setattr(pb, "SoundDevicePlayer", boom)
    p = pick_player("auto", volume=1.0)
    assert isinstance(p, AfplayPlayer)


def test_pick_player_unknown_mode_raises():
    with pytest.raises(ValueError):
        pick_player("cassette", volume=1.0)


def test_afplay_play_and_interrupt(monkeypatch):
    import tempfile
    import threading
    from pathlib import Path

    calls = []

    class FakeProc:
        def __init__(self):
            self.killed = False
            self.evt = threading.Event()

        def wait(self, timeout=None):
            calls.append("wait")
            self.evt.wait(3)

        def terminate(self):
            self.killed = True
            self.evt.set()

    def fake_run(cmd, **kw):
        calls.append(cmd[0])
        return FakeProc()

    tmp = Path(tempfile.mkdtemp())
    p = AfplayPlayer(volume=1.0, runner=fake_run, tmpdir=tmp, player_bin="afplay")
    t = threading.Thread(target=p.play, args=(pcm(0.0, 0.0), 24000))
    t.start()
    deadline = 20
    for _ in range(deadline * 50):
        if p.current is not None:
            break
        time.sleep(0.02)
    assert "afplay" in calls
    assert p.current is not None
    p.stop()
    t.join(3)
    assert not t.is_alive()
    assert p.current is None or p.current.killed is True
    # no leftover wavs
    assert list(tmp.glob("*.wav")) == []
