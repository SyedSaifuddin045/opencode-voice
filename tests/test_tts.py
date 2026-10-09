import pytest

from opencode_voice.tts import VOICES, KokoroTTS


def test_voices_contains_defaults():
    assert "af_heart" in VOICES
    assert "am_michael" in VOICES
    assert len(VOICES) >= 50


def test_voices_unique():
    assert len(VOICES) == len(set(VOICES))


def test_tts_lazy_no_model_load():
    tts = KokoroTTS(model_id="does/not-exist", voice="af_heart")
    assert tts.loaded is False


def test_synth_empty_text_raises_before_load():
    tts = KokoroTTS(model_id="does/not-exist", voice="af_heart")
    with pytest.raises(ValueError):
        tts.synth("")
    with pytest.raises(ValueError):
        tts.synth("   \n ")
    assert tts.loaded is False


def test_default_sample_rate():
    tts = KokoroTTS(model_id="does/not-exist", voice="af_heart")
    assert tts.sample_rate == 24000
