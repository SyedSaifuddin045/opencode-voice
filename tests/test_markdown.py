from opencode_voice.markdown import to_speech


def test_heading_becomes_sentence():
    out = to_speech("## Deployment\n\nSome text.")
    assert "#" not in out
    assert "Deployment" in out
    assert out.strip().startswith("Deployment")


def test_bold_and_italic_stripped():
    out = to_speech("This is **important** and _also_.")
    assert "**" not in out
    assert "_" not in out
    assert "important" in out


def test_inline_code_kept_as_words():
    out = to_speech("Run `docker compose up` now.")
    assert "`" not in out
    assert "docker compose up" in out


def test_link_uses_anchor_text_only():
    out = to_speech("See [the documentation](https://example.com/docs) for details.")
    assert "https" not in out
    assert "example.com" not in out
    assert "documentation" in out


def test_bare_url_removed_or_shortened():
    out = to_speech("Read https://example.com/very/long/path?q=1 now.")
    assert "example.com" not in out
    assert "now" in out


def test_code_block_removed():
    text = 'Here is the fix:\n\n```python\ndef create_upload():\n    pass\n```\n\nThe server works now.'
    out = to_speech(text)
    assert "def create_upload" not in out
    assert "Here is the fix" in out
    assert "server works now" in out


def test_fenced_block_empty_after_removal_leaves_no_blank_chaos():
    out = to_speech("Intro:\n```\n$ ls -la\ndrwxr-xr-x\n```\nDone.")
    assert "drwxr-xr-x" not in out
    assert "Done." in out


def test_table_converted_to_statements():
    text = "| Service | Status |\n|---------|--------|\n| API     | OK     |\n| Worker  | Failed |"
    out = to_speech(text)
    assert "|" not in out
    assert "API" in out
    assert "Failed" in out


def test_bullet_list_naturalized():
    text = "- Fixed Docker configuration\n- Added persistent storage\n- Restarted the container"
    out = to_speech(text)
    assert out.count("-") == 0 or "Fixed" in out
    assert "Fixed Docker configuration" in out
    assert "persistent storage" in out
    assert "Restarted the container" in out
    assert not out.strip().startswith("-")


def test_numbered_list_naturalized():
    out = to_speech("1. First step\n2. Second step")
    assert "1." not in out
    assert "First step" in out


def test_blockquote_stripped():
    out = to_speech("> Warning: cache is stale.")
    assert ">" not in out
    assert "Warning" in out


def test_strikethrough_stripped():
    out = to_speech("Use ~~this~~ that.")
    assert "~~" not in out
    assert "that" in out


def test_image_markdown_dropped():
    out = to_speech("Look ![diagram](img.png) here.")
    assert "diagram" not in out or "img.png" not in out
    assert "img.png" not in out


def test_horizontal_rule_removed():
    out = to_speech("Before.\n\n---\n\nAfter.")
    assert "---" not in out
    assert "Before." in out
    assert "After." in out


def test_empty_input():
    assert to_speech("") == ""
    assert to_speech("   \n\n  ") == ""
