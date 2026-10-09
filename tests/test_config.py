from pathlib import Path

from opencode_voice.config import Config, load_config


def test_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OC_VOICE_CONFIG", str(tmp_path / "missing.toml"))
    cfg = load_config()
    assert cfg.enabled is True
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8765
    assert cfg.voice == "af_heart"
    assert cfg.speed == 1.0
    assert cfg.volume == 1.0
    assert cfg.mode == "deterministic"
    assert cfg.speak_code is False
    assert cfg.speak_urls is False
    assert cfg.interrupt_on_new_prompt is True
    assert cfg.stream_sentences is True


def test_toml_overrides_defaults(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        """
[general]
enabled = false

[tts]
voice = "af_bella"
speed = 1.25

[server]
port = 9999

[speech]
speak_urls = true

[behavior]
stream_sentences = false
"""
    )
    monkeypatch.setenv("OC_VOICE_CONFIG", str(p))
    cfg = load_config()
    assert cfg.enabled is False
    assert cfg.voice == "af_bella"
    assert cfg.speed == 1.25
    assert cfg.port == 9999
    assert cfg.speak_urls is True
    assert cfg.stream_sentences is False


def test_invalid_toml_raises(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text("not [ valid")
    monkeypatch.setenv("OC_VOICE_CONFIG", str(p))
    try:
        load_config()
    except Exception:
        return
    raise AssertionError("invalid toml must raise")


def test_pronunciations_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OC_VOICE_CONFIG", str(tmp_path / "missing.toml"))
    cfg = load_config()
    assert isinstance(cfg.pronunciations, dict)
    assert len(cfg.pronunciations) > 0
