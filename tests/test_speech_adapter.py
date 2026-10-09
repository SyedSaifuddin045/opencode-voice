from opencode_voice.speech_adapter import SpeechAdapter


def test_symbol_gt_eq():
    a = SpeechAdapter()
    assert a.normalize("if x >= 10") == "if x greater than or equal to 10"


def test_symbol_lt_eq():
    a = SpeechAdapter()
    assert "less than or equal to" in a.normalize("y <= 3")


def test_symbol_equals_double():
    a = SpeechAdapter()
    assert "equals" in a.normalize("if a == b")


def test_symbol_not_equals():
    a = SpeechAdapter()
    assert "not equal" in a.normalize("if a != b")


def test_arrow_becomes_to():
    a = SpeechAdapter()
    assert a.normalize("A -> B") == "A to B"
    assert "to" in a.normalize("A → B")


def test_ampersand_and_pipe():
    a = SpeechAdapter()
    assert " and " in a.normalize("enable x && y")
    assert " or " in a.normalize("enable x || y")


def test_abbreviations_not_spelled_out():
    a = SpeechAdapter()
    out = a.normalize("The API uses HTTP over TCP with JSON and YAML in a REST CLI SDK SQL AWS SSH GPU RAM CPU UDP.")
    assert "A.P.I." not in out
    assert "API" in out or "api" in out.lower()
    assert "HTTP" in out


def test_abbreviation_acronyms_readable():
    a = SpeechAdapter()
    # single letters / version-like tokens must not explode into words
    out = a.normalize("Python 3.12 and C++ are fine")
    assert "3.12" in out


def test_pronunciation_dictionary_applied():
    a = SpeechAdapter(pronunciations={"Kubernetes": "koo-ber-net-eez"})
    assert "koo-ber-net-eez" in a.normalize("We deploy to Kubernetes daily")


def test_double_space_collapsed():
    a = SpeechAdapter()
    assert "  " not in a.normalize("hello    world")


# --- streaming sentence buffer ---


def test_streaming_emits_complete_sentences():
    a = SpeechAdapter()
    out = a.feed("I found the problem. The Docker cont")
    assert out == ["I found the problem."]
    out2 = a.feed("ainer is crashing. More text")
    assert out2 == ["The Docker container is crashing."]
    assert a.flush() == ["More text"]


def test_no_emit_until_sentence_end():
    a = SpeechAdapter()
    assert a.feed("partial sentence without end") == []
    assert a.flush() == ["partial sentence without end"]


def test_streaming_across_chunk_boundaries_inside_word():
    a = SpeechAdapter()
    assert a.feed("The upload director") == []
    assert a.feed("y is deleted. Done.") == ["The upload directory is deleted.", "Done."]


def test_flush_empty_returns_empty():
    a = SpeechAdapter()
    assert a.feed("nothing yet") == []
    assert a.flush() == ["nothing yet"]
    assert a.flush() == []


def test_abbreviations_do_not_split_sentence():
    a = SpeechAdapter()
    out = a.feed("We use the API. Next.")
    assert out == ["We use the API. Next."] or out == ["We use the API.", "Next."]
    # period after known abbreviation should not force premature split mid-sentence
    b = SpeechAdapter()
    assert b.feed("The U.S. economy grew.") == ["The U.S. economy grew."]


def test_url_not_spoken():
    a = SpeechAdapter()
    out = a.normalize("Visit https://example.com/page?a=1 for info")
    assert "example.com" not in out


def test_reset_clears_buffer():
    a = SpeechAdapter()
    a.feed("half a senten")
    a.reset()
    assert a.flush() == []


def test_empty_feed():
    a = SpeechAdapter()
    assert a.feed("") == []


def test_long_response_chunks_safely():
    a = SpeechAdapter()
    text = "This is sentence number %d. " % 1 + ""
    buf = "".join("Sentence %d ends here. " % i for i in range(200))
    got = []
    step = 37
    for i in range(0, len(buf), step):
        got.extend(a.feed(buf[i : i + step]))
    got.extend(a.flush())
    assert len(got) >= 195
    assert all(len(s) < 500 for s in got)


def test_speak_off_content_skipped_by_adapter_toggle():
    a = SpeechAdapter(speak_code=False)
    # code already stripped by markdown stage; adapter guards fences arriving raw
    out = a.feed("Intro. ```\ncode here\n``` Tail.")
    joined = " ".join(out)
    assert "code here" not in joined


def test_status_is_speech_ready():
    a = SpeechAdapter()
    text = "## Summary\n\n- fixed bug\n- tests pass\n\n```py\nx=1\n```"
    a.feed(text)
    out = a.flush()
    assert out, "must produce speech from mixed markdown"
    joined = " ".join(out)
    assert "```" not in joined and "##" not in joined and "-" not in joined.split()[0]
