"""The development fake model: what it answers, and that Cancel stops its pause."""

import json
import threading

import pytest

from draftcheck.edits import parse_edit
from draftcheck.fake_model import (
    FAKE_EXTRACTION,
    FAKE_MODEL_NAME,
    FakeModelClient,
    fake_client_factory,
)
from draftcheck.llm_client import ModelCancelled
from draftcheck.prompts import EXTRACTION_SYSTEM


def test_extraction_returns_the_saved_reading() -> None:
    result = FakeModelClient(delay_s=0).chat_json(EXTRACTION_SYSTEM, "read it", [b"png"])
    assert result.data == json.loads(FAKE_EXTRACTION.read_text(encoding="utf-8"))
    assert result.model == FAKE_MODEL_NAME
    assert result.attempts == 1


def test_every_correction_round_proposes_the_same_valid_edit() -> None:
    client = FakeModelClient(delay_s=0)
    first = client.chat_json("correct this", "findings", [])
    second = client.chat_json("correct this", "findings", [])
    assert first.data == second.data
    [edit] = first.data["edits"]
    assert edit["op"] == "remove_datum_ref"
    assert (edit["fcf_id"], edit["label"]) == ("fcf_1", "C")
    # The edit is one the real edit parser understands, so the loop treats it like any other.
    parsed, problem = parse_edit(edit)
    assert parsed is not None, problem


def test_the_edit_list_is_a_fresh_copy_each_time() -> None:
    """The loop may mutate what it is given; that must not change the next round's edit."""
    client = FakeModelClient(delay_s=0)
    first = client.chat_json("correct", "", [])
    first.data["edits"][0]["label"] = "Z"
    assert client.chat_json("correct", "", []).data["edits"][0]["label"] == "C"


def test_abort_ends_the_pause_with_a_cancel() -> None:
    client = FakeModelClient(delay_s=30)
    threading.Timer(0.05, client.abort).start()
    with pytest.raises(ModelCancelled):
        client.chat_json(EXTRACTION_SYSTEM, "", [])


def test_factory_reads_the_delay_when_called(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("draftcheck.fake_model.FAKE_DELAY_S", 0)
    client = fake_client_factory("baseten")
    assert client.fake is True
    assert client.chat_json(EXTRACTION_SYSTEM, "", []).latency_s < 1
