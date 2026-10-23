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


# --- sounddevice must never touch the audio device until speech plays ------

class FakeOutputStream:
    instances = []

    def __init__(self, samplerate, channels, dtype):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.frames = 0
        self.started = False
        self.closed = False
        FakeOutputStream.instances.append(self)

    @property
    def active(self):
        return self.started

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def abort(self):
        self.started = False

    def close(self):
        self.closed = True

    def write(self, block):
        self.frames += len(block)


class FakeSd:
    """Fake PortAudio. `Stream` is duplex (mic + speakers) and must never be used."""

    def __init__(self):
        self.duplex_calls = 0

    def Stream(self, *args, **kwargs):
        self.duplex_calls += 1
        raise AssertionError(
            "duplex sd.Stream opens the microphone and forces Bluetooth "
            "headphones into HFP (16 kHz mono)"
        )

    def OutputStream(self, samplerate, channels, dtype):
        return FakeOutputStream(samplerate, channels, dtype)


def _player_with_fake_sd(monkeypatch):
    import sys
    import opencode_voice.playback as pb

    fake = FakeSd()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    FakeOutputStream.instances = []
    return pb.SoundDevicePlayer(volume=1.0), fake


def test_sounddevice_init_does_not_open_audio_stream(monkeypatch):
    player, fake = _player_with_fake_sd(monkeypatch)
    assert FakeOutputStream.instances == []
    assert fake.duplex_calls == 0
    player.close()


def test_sounddevice_play_opens_output_only_stream(monkeypatch):
    player, fake = _player_with_fake_sd(monkeypatch)
    seconds = 0.5
    pcm_in = pcm(*([0.0] * int(24000 * seconds)))
    player.play(pcm_in, 24000)
    assert fake.duplex_calls == 0  # never opens the microphone
    assert len(FakeOutputStream.instances) == 1
    stream = FakeOutputStream.instances[0]
    assert stream.samplerate == 24000
    assert stream.channels == 1
    assert stream.frames == pytest.approx(int(24000 * seconds), abs=2)
    player.close()


def test_sounddevice_reuses_stream_at_same_rate(monkeypatch):
    player, fake = _player_with_fake_sd(monkeypatch)
    player.play(pcm(0.0, 0.1), 24000)
    player.play(pcm(0.0, 0.1), 24000)
    assert len(FakeOutputStream.instances) == 1
    player.close()


def test_sounddevice_reopens_stream_when_rate_changes(monkeypatch):
    player, fake = _player_with_fake_sd(monkeypatch)
    player.play(pcm(0.0, 0.1), 24000)
    player.play(pcm(0.0, 0.1), 22050)
    assert [s.samplerate for s in FakeOutputStream.instances] == [24000, 22050]
    player.close()
