"""The development fake model: what it answers, and that Cancel stops its pause."""

import json
import threading

import pytest

from draftcheck.agent import correct
from draftcheck.edits import parse_edit
from draftcheck.fake_model import (
    FAKE_EXTRACTION,
    FAKE_MODEL_NAME,
    FAKE_SCENARIO_ENV_VAR,
    REJECT_THEN_FIX,
    FakeModelClient,
    fake_client_factory,
    fake_scenario,
)
from draftcheck.llm_client import ModelCancelled
from draftcheck.prompts import EXTRACTION_SYSTEM
from draftcheck.schema import parse_drawing


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


def test_reject_then_fix_scripts_three_different_rounds() -> None:
    client = FakeModelClient(delay_s=0, scenario=REJECT_THEN_FIX)
    ops = [client.chat_json("correct", "", []).data["edits"][0]["op"] for _ in range(3)]
    assert ops == ["delete_fcf", "set_field", "remove_datum_ref"]
    # Past the script it keeps proposing the fix rather than running out.
    assert client.chat_json("correct", "", []).data["edits"][0]["op"] == "remove_datum_ref"


def test_reject_then_fix_through_the_real_loop_rejects_fails_then_verifies() -> None:
    """The guards and the rules decide each round's fate; the script only proposes."""
    drawing = parse_drawing(json.loads(FAKE_EXTRACTION.read_text(encoding="utf-8")))
    report = correct(drawing, b"png", FakeModelClient(delay_s=0, scenario=REJECT_THEN_FIX))
    assert len(report.rounds) == 3
    first, second, third = report.rounds
    assert (len(first.applied), len(first.rejected)) == (0, 1)
    assert (len(second.applied), len(second.rolled_back), second.resolved) == (0, 1, [])
    assert third.resolved == ["GDT-001@fcf_1"]
    assert report.unresolved_count == 0
    # Round 2's tightened tolerance was rolled back: the frame keeps Ø0.25 and loses C.
    [frame] = report.final_drawing.fcfs
    assert frame.tolerance == 0.25
    assert [ref.label for ref in frame.datum_refs] == ["A", "B"]


def test_the_scenario_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("draftcheck.fake_model.FAKE_DELAY_S", 0)
    monkeypatch.delenv(FAKE_SCENARIO_ENV_VAR, raising=False)
    assert fake_scenario() is None
    assert fake_client_factory().chat_json("correct", "", []).data["edits"][0]["op"] == (
        "remove_datum_ref"
    )
    monkeypatch.setenv(FAKE_SCENARIO_ENV_VAR, "reject_then_fix")
    assert fake_scenario() == REJECT_THEN_FIX
    assert fake_client_factory().chat_json("correct", "", []).data["edits"][0]["op"] == (
        "delete_fcf"
    )
    monkeypatch.setenv(FAKE_SCENARIO_ENV_VAR, "something_else")
    assert fake_scenario() is None
