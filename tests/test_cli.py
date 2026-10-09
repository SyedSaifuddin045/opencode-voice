from opencode_voice.cli import main


def test_voices_lists_kokoro_voices(capsys):
    rc = main(["voices"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "af_heart" in out
    assert "am_michael" in out
    assert len(out.split()) >= 50


def test_status_when_daemon_down(capsys, monkeypatch):
    monkeypatch.setenv("OC_VOICE_PORT", "1")  # nothing listening
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "running" in out
    assert "false" in out.lower()


def test_health_when_daemon_down(capsys, monkeypatch):
    monkeypatch.setenv("OC_VOICE_PORT", "1")
    rc = main(["health"])
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert rc != 0
    assert "not running" in out.lower() or "unreachable" in out.lower()


def test_unknown_command(capsys):
    rc = main(["dance"])
    assert rc != 0


def test_off_on_toggle_roundtrip(tmp_path, capsys, monkeypatch):
    state = tmp_path / "enabled"
    monkeypatch.setenv("OC_VOICE_ENABLED_FILE", str(state))
    monkeypatch.setenv("OC_VOICE_PORT", "1")  # daemon down: file-only path
    rc = main(["off"])
    assert rc == 0
    assert state.read_text().strip() == "0"
    assert "disabled" in capsys.readouterr().out.lower()
    rc = main(["on"])
    assert rc == 0
    assert state.read_text().strip() == "1"
    rc = main(["toggle"])
    assert rc == 0
    assert state.read_text().strip() == "0"
    out = capsys.readouterr().out.lower()
    assert "off" in out or "disabled" in out


def test_status_shows_enabled_state(tmp_path, capsys, monkeypatch):
    state = tmp_path / "enabled"
    state.write_text("0")
    monkeypatch.setenv("OC_VOICE_ENABLED_FILE", str(state))
    monkeypatch.setenv("OC_VOICE_PORT", "1")
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"enabled": false' in out
