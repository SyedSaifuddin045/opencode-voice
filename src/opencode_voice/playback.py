"""Local audio playback: float32 PCM buffers -> macOS speakers.

Primary: sounddevice (PortAudio, bundled in wheel) with chunked writes
for ~100ms interrupt latency. Fallback: afplay with temp WAV files.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import uuid
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("opencode_voice.playback")

_CHUNK_SECONDS = 0.1


def scale_pcm_f32(pcm: bytes, volume: float) -> bytes:
    """Scale float32 PCM bytes by volume, clipping to [-1, 1]."""
    if len(pcm) % 4 != 0:
        raise ValueError("pcm length must be a multiple of 4 (float32)")
    if volume == 1.0:
        return pcm
    arr = np.frombuffer(pcm, dtype="<f4") * np.float32(volume)
    np.clip(arr, -1.0, 1.0, out=arr)
    return arr.astype("<f4").tobytes()


def _f32_to_wav_bytes(pcm: bytes, samplerate: int) -> bytes:
    """Encode float32 mono PCM as 16-bit WAV bytes."""
    arr = np.frombuffer(pcm, dtype="<f4").copy()
    np.clip(arr, -1.0, 1.0, out=arr)
    ints = (arr * 32767.0).astype("<i2")
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(ints.tobytes())
    return buf.getvalue()


class SoundDevicePlayer:
    """Chunked blocking writes to a PortAudio stream (interruptible)."""

    def __init__(self, volume: float = 1.0):
        import sounddevice as sd

        self._sd = sd
        self.volume = volume
        self._stream = None
        self._stream_rate = 0
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        # probe: can we open an output stream at all?
        probe = sd.Stream(samplerate=24000, channels=1, dtype="float32")
        try:
            probe.start()
            probe.stop()
        finally:
            probe.close()

    def play(self, pcm: bytes, samplerate: int) -> None:
        if self.volume != 1.0:
            pcm = scale_pcm_f32(pcm, self.volume)
        data = np.frombuffer(pcm, dtype="<f4")
        if data.size == 0:
            return
        self._stop_evt.clear()
        with self._lock:
            if self._stream is None or self._stream_rate != samplerate:
                self._close_stream_locked()
                self._stream = self._sd.Stream(
                    samplerate=samplerate, channels=1, dtype="float32"
                )
                self._stream.start()
                self._stream_rate = samplerate
            elif not self._stream.active:
                # previous stop() aborted the stream: restart it
                self._stream.start()
            stream = self._stream
        chunk = max(1, int(samplerate * _CHUNK_SECONDS))
        for i in range(0, data.size, chunk):
            if self._stop_evt.is_set():
                break
            block = data[i : i + chunk]
            try:
                stream.write(block)
            except Exception:
                if self._stop_evt.is_set():
                    log.debug("write aborted by stop()")
                else:
                    log.exception("stream write failed")
                break
        # keep stream open for the next sentence (cheap)

    def stop(self) -> None:
        self._stop_evt.set()
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.abort()
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            self._close_stream_locked()

    def _close_stream_locked(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._stream_rate = 0


class AfplayPlayer:
    """Temp-WAV + afplay fallback (no extra dependencies)."""

    def __init__(
        self,
        volume: float = 1.0,
        runner=subprocess.Popen,
        tmpdir: str | os.PathLike | None = None,
        player_bin: str = "afplay",
    ):
        self.volume = volume
        self.runner = runner
        self.tmpdir = Path(tmpdir) if tmpdir else Path(tempfile.mkdtemp(prefix="ocv-"))
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        self.player_bin = player_bin
        self.current = None
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()

    def play(self, pcm: bytes, samplerate: int) -> None:
        if self.volume != 1.0:
            pcm = scale_pcm_f32(pcm, self.volume)
        if len(pcm) == 0:
            return
        self._stop_evt.clear()
        path = self.tmpdir / f"{uuid.uuid4().hex}.wav"
        path.write_bytes(_f32_to_wav_bytes(pcm, samplerate))
        proc = None
        try:
            proc = self.runner([self.player_bin, str(path)])
            with self._lock:
                self.current = proc
            proc.wait()
        finally:
            with self._lock:
                if self.current is proc:
                    self.current = None
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def stop(self) -> None:
        self._stop_evt.set()
        with self._lock:
            proc = self.current
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def close(self) -> None:
        pass


def pick_player(mode: str = "auto", volume: float = 1.0):
    """Select playback backend. Raises ValueError on unknown mode."""
    if mode == "afplay":
        return AfplayPlayer(volume=volume)
    if mode == "sounddevice":
        return SoundDevicePlayer(volume=volume)
    if mode != "auto":
        raise ValueError(f"unknown playback mode: {mode!r}")
    try:
        return SoundDevicePlayer(volume=volume)
    except Exception as exc:
        log.warning("sounddevice unavailable (%s), falling back to afplay", exc)
        return AfplayPlayer(volume=volume)
