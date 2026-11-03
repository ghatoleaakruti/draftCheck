"""Tests for the extract/check pipeline. A fake client replies, so no network."""

import json
import logging
from pathlib import Path

import pytest

from draftcheck import pipeline
from draftcheck.llm_client import JSONChatResult, Usage
from draftcheck.pipeline import check, extract, extract_page
from draftcheck.prompts import EXTRACTION_USER
from draftcheck.render import RenderError, load_pages
from draftcheck.schema import Units

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
SAMPLE_PDF = REPO_ROOT / "samples" / "generated" / "plate_001.pdf"


class FakeClient:
    """Returns one canned extraction dict and records what it was asked."""

    def __init__(self, data: dict, text: str = "") -> None:
        self._data = data
        self._text = text or json.dumps(data)
        self.calls: list[dict] = []

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        self.calls.append({"system": system, "user": user, "images": images})
        return JSONChatResult(
            data=self._data,
            text=self._text,
            latency_s=1.25,
            usage=Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
            model="fake-model",
            attempts=1,
        )


def model_output() -> dict:
    return json.loads((FIXTURES / "plate_001.model.json").read_text())


def test_extract_sends_one_page_image_and_parses_the_reply():
    client = FakeClient(model_output())

    result = extract(SAMPLE_PDF, client)

    assert len(client.calls) == 1
    images = client.calls[0]["images"]
    assert len(images) == 1, "only page 1 should be sent"
    assert images[0].startswith(b"\x89PNG")
    assert result.drawing.title_block.drawing_number == "DC-0001"
    assert result.drawing.units is Units.MM, "'MILLIMETERS' should normalize to the enum"


def test_extract_keeps_the_raw_text_and_metrics():
    client = FakeClient(model_output(), text="  {...raw reply...}  ")

    result = extract(SAMPLE_PDF, client)

    assert result.raw_text == "  {...raw reply...}  "
    assert result.latency_s == 1.25
    assert result.model == "fake-model"
    assert result.attempts == 1
    assert result.usage.total_tokens == 3


def test_extract_then_check_finds_the_seeded_undefined_datum():
    client = FakeClient(model_output())

    findings = check(extract(SAMPLE_PDF, client).drawing)

    rule_ids = [finding.rule_id for finding in findings]
    assert "GDT-001" in rule_ids, f"expected the seeded error, got {rule_ids}"


def test_extract_passes_unreadable_items_through_as_parse_issues():
    data = model_output()
    data["fcfs"].append({"characteristic": "wobbliness", "tolerance": 0.1})

    result = extract(SAMPLE_PDF, FakeClient(data))

    assert result.drawing.parse_issues, "the bad frame should be recorded, not crash the run"


def test_extract_raises_on_a_file_that_cannot_be_rendered(tmp_path):
    missing = tmp_path / "nope.pdf"

    with pytest.raises(RenderError):
        extract(missing, FakeClient(model_output()))


def test_check_is_deterministic():
    drawing = extract(SAMPLE_PDF, FakeClient(model_output())).drawing

    assert check(drawing) == check(drawing)


def test_extract_page_sends_the_given_bytes_and_logs_each_stage(caplog):
    client = FakeClient(model_output())
    png = load_pages(SAMPLE_PDF, dpi=72)[0].png_bytes

    with caplog.at_level(logging.INFO, logger="draftcheck"):
        result = extract_page(png, client)

    assert client.calls[0]["images"] == [png]
    assert result.drawing.units == Units.MM
    messages = [record.getMessage() for record in caplog.records]
    assert [m.split(" (")[0] for m in messages] == [
        "model call started",
        "model call done",
        "parse done",
    ]
    assert all("memory" in m and " MB" in m for m in messages)
    # Sizes only: nothing from the prompt or the reply reaches the log.
    assert not any(EXTRACTION_USER[:40] in m or "fcf_1" in m for m in messages)


def test_extract_renders_only_page_one(monkeypatch):
    seen: dict = {}

    def fake_load_pages(path, **kwargs):
        seen.update(kwargs)
        return load_pages(path, **kwargs)

    monkeypatch.setattr(pipeline, "load_pages", fake_load_pages)
    extract(SAMPLE_PDF, FakeClient(model_output()))
    assert seen["max_pages"] == 1
