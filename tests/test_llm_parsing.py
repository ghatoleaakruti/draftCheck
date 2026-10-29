import pytest

from draftcheck.llm_parsing import JSONParseError, extract_json


def test_plain_json():
    assert extract_json('{"rule": "D1", "passed": true}') == {"rule": "D1", "passed": True}


def test_json_in_code_fence():
    text = '```json\n{"rule": "D1", "count": 2}\n```'
    assert extract_json(text) == {"rule": "D1", "count": 2}


def test_json_after_think_block():
    text = '<think>The drawing has two datums, so I should report both.</think>\n{"datums": 2}'
    assert extract_json(text) == {"datums": 2}


def test_text_before_and_after_json():
    text = 'Here is my answer:\n{"ok": true}\nLet me know if you need more.'
    assert extract_json(text) == {"ok": True}


def test_no_json_raises():
    with pytest.raises(JSONParseError):
        extract_json("I could not read the drawing.")


def test_invalid_json_raises():
    with pytest.raises(JSONParseError):
        extract_json('{"rule": "D1", }')


def test_top_level_list_raises():
    with pytest.raises(JSONParseError):
        extract_json('[{"rule": "D1"}, {"rule": "D2"}]')


def test_raw_text_kept_on_error():
    raw = "no json here at all"
    with pytest.raises(JSONParseError) as excinfo:
        extract_json(raw)
    assert excinfo.value.raw_text == raw
