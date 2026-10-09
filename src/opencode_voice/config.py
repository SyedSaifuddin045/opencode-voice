"""Configuration for opencode-voice.

Loads TOML config (default: ~/.config/opencode-voice/config.toml,
override with $OC_VOICE_CONFIG) over sensible defaults. Unknown keys
are ignored; invalid TOML raises.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PRONUNCIATIONS: dict[str, str] = {
    "nginx": "engine-ex",
    "Kubernetes": "koo-ber-net-eez",
    "PostgreSQL": "post-gres-cue-ell",
    "SQLite": "sequel-light",
    "kubernetes": "koo-ber-net-eez",
    "Postgres": "post-gres",
    "zsh": "zee-shell",
}


def default_config_path() -> Path:
    return Path.home() / ".config" / "opencode-voice" / "config.toml"


@dataclass
class Config:
    # [general]
    enabled: bool = True
    log_level: str = "info"
    log_text: bool = False

    # [tts]
    provider: str = "kokoro"
    model: str = "mlx-community/Kokoro-82M-bf16"
    voice: str = "af_heart"
    speed: float = 1.0
    volume: float = 1.0

    # [server]
    host: str = "127.0.0.1"
    port: int = 8765

    # [speech]
    mode: str = "deterministic"
    speak_code: bool = False
    speak_tool_output: bool = False
    speak_urls: bool = False
    speak_markdown: bool = False
    speak_tool_status: bool = False
    max_sentence_chars: int = 400
    pronunciations: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_PRONUNCIATIONS)
    )

    # [behavior]
    interrupt_on_new_prompt: bool = True
    stream_sentences: bool = True
    playback: str = "auto"  # auto | sounddevice | afplay
    max_queue: int = 32
    idle_shutdown_s: int = 300  # exit after N seconds without requests (0 = never)

    # runtime paths (not usually set from TOML)
    pid_file: str = ""
    log_file: str = ""
    enabled_file: str = ""

    def __post_init__(self):
        state = Path.home() / ".local" / "state" / "opencode-voice"
        if not self.pid_file:
            self.pid_file = str(state / "daemon.pid")
        if not self.log_file:
            self.log_file = str(state / "daemon.log")
        if not self.enabled_file:
            self.enabled_file = str(state / "enabled")
        env_file = os.environ.get("OC_VOICE_ENABLED_FILE")
        if env_file:
            self.enabled_file = env_file


# section -> config attribute names
_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "general": ("enabled", "log_level", "log_text"),
    "tts": ("provider", "model", "voice", "speed", "volume"),
    "server": ("host", "port"),
    "speech": (
        "mode",
        "speak_code",
        "speak_tool_output",
        "speak_urls",
        "speak_markdown",
        "speak_tool_status",
        "max_sentence_chars",
        "pronunciations",
    ),
    "behavior": (
        "interrupt_on_new_prompt",
        "stream_sentences",
        "playback",
        "max_queue",
        "idle_shutdown_s",
    ),
}

_TRUTHY = {True, "true", "yes", "on", 1, "1"}


def _coerce(value, current):
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in _TRUTHY
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, dict):
        if not isinstance(value, dict):
            raise ValueError(f"expected table, got {type(value).__name__}")
        return {str(k): str(v) for k, v in value.items()}
    return str(value)


def load_config(path: str | Path | None = None) -> Config:
    """Load config from TOML file over defaults.

    Resolution order: explicit path arg > $OC_VOICE_CONFIG > default path.
    Missing file = defaults. Invalid TOML raises.
    """
    cfg = Config()

    env_path = os.environ.get("OC_VOICE_CONFIG")
    target = Path(path) if path is not None else Path(env_path) if env_path else default_config_path()

    if not target.exists():
        return cfg

    with open(target, "rb") as f:
        data = tomllib.load(f)

    for section, keys in _SECTION_KEYS.items():
        table = data.get(section)
        if not isinstance(table, dict):
            continue
        for key in keys:
            if key not in table:
                continue
            current = getattr(cfg, key)
            setattr(cfg, key, _coerce(table[key], current))

    # top-level "pronunciations" convenience also accepted
    if isinstance(data.get("pronunciations"), dict):
        cfg.pronunciations = {
            str(k): str(v) for k, v in data["pronunciations"].items()
        }

    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"refusing to bind non-localhost host {cfg.host!r}: "
            "opencode-voice API must stay local"
        )
    return cfg
