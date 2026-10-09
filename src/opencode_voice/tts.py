"""Kokoro-82M TTS via MLX on Apple Silicon.

Model stays resident: load() once (daemon startup), synth() per sentence.
Single-threaded by design — the daemon calls synth() only from its
synthesis worker thread.
"""

from __future__ import annotations

import logging
import time

import numpy as np

log = logging.getLogger("opencode_voice.tts")

# Canonical voices shipped with Kokoro-82M (54 presets across 13 locales).
VOICES: list[str] = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "em_santa",
    "ff_siwis",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
    "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
]


class KokoroTTS:
    """Resident Kokoro-82M wrapper (lazy model load)."""

    def __init__(
        self,
        model_id: str = "mlx-community/Kokoro-82M-bf16",
        voice: str = "af_heart",
        speed: float = 1.0,
        lang_code: str = "a",
    ):
        self.model_id = model_id
        self.voice = voice
        self.speed = speed
        self.lang_code = lang_code
        self.sample_rate = 24000
        self._model = None
        self._pipeline = None
        self.load_seconds: float | None = None
        self.warmup_seconds: float | None = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> float:
        """Load model + G2P pipeline from HuggingFace (cached after first)."""
        if self.loaded:
            return 0.0
        t0 = time.perf_counter()
        from mlx_audio.tts import load_model
        from mlx_audio.tts.models.kokoro.pipeline import KokoroPipeline

        log.info("loading Kokoro model %s", self.model_id)
        self._model = load_model(self.model_id)
        repo_id = getattr(self._model, "repo_id", None) or self._model.REPO_ID
        self._pipeline = KokoroPipeline(
            lang_code=self.lang_code,
            model=self._model,
            repo_id=repo_id,
        )
        self.sample_rate = int(self._model.sample_rate)
        self.load_seconds = time.perf_counter() - t0
        log.info("model loaded in %.2fs", self.load_seconds)
        return self.load_seconds

    def warmup(self) -> float:
        """Synthesize a short phrase so kernels/voices are hot."""
        t0 = time.perf_counter()
        self.synth("Voice system ready.")
        self.warmup_seconds = time.perf_counter() - t0
        log.info("warm-up complete in %.2fs", self.warmup_seconds)
        return self.warmup_seconds

    def synth(
        self, text: str, voice: str | None = None, speed: float | None = None
    ) -> tuple[bytes, int]:
        """Synthesize speech to raw float32 mono PCM bytes."""
        if not text or not text.strip():
            raise ValueError("empty text")
        if not self.loaded:
            self.load()
        voice = voice or self.voice
        speed = self.speed if speed is None else speed

        chunks: list[np.ndarray] = []
        for result in self._pipeline(
            text, voice=voice, speed=speed, split_pattern=None
        ):
            audio = result.audio
            if audio is None:
                continue
            chunks.append(np.asarray(audio, dtype=np.float32).reshape(-1))
        if not chunks:
            raise RuntimeError("TTS produced no audio")
        pcm = np.concatenate(chunks)
        # guard against degenerate output
        if not np.isfinite(pcm).all():
            pcm = np.nan_to_num(pcm, nan=0.0, posinf=0.0, neginf=0.0)
        np.clip(pcm, -1.0, 1.0, out=pcm)
        return pcm.astype("<f4").tobytes(), self.sample_rate
