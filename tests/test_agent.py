"""Tests for the correction loop.

Every test drives the loop with a scripted client, so the model's replies are chosen by
the test and no network call is ever made. That is the point: the loop's job is to decide
what a reply is worth, and these tests hand it replies worth very different amounts —
a real fix, a fix that breaks something else, a deletion, nonsense, and an endless stall.
"""

import json
from pathlib import Path

from draftcheck.agent import correct
from draftcheck.llm_client import JSONChatResult, Usage
from draftcheck.rules import run_rules
from draftcheck.schema import parse_drawing

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_JSON = REPO_ROOT / "tests" / "fixtures" / "plate_001.model.json"

PAGE = b"\x89PNG fake page bytes"


class ScriptedClient:
    """Replies with the next scripted reply, and records what it was asked.

    Once the script runs out it proposes nothing, so a loop that calls more often than a
    test intended keeps running instead of raising — the test asserts on ``calls`` and
    says so plainly.
    """

    def __init__(self, *replies: dict | Exception) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        self.calls.append({"system": system, "user": user, "images": images})
        reply = self._replies.pop(0) if self._replies else {"edits": []}
        if isinstance(reply, Exception):
            raise reply
        return JSONChatResult(
            data=reply,
            text=json.dumps(reply),
            latency_s=0.5,
            usage=Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
            model="scripted",
            attempts=1,
        )


def drawing_with_undefined_datum():
    """A frame citing datum B, which no symbol on the drawing defines. One GDT-001 error."""
    return parse_drawing(
        {
            "units": "mm",
            "general_tolerance": {"linear_plus_minus": 0.1},
            "datums": [{"id": "DT1", "label": "A", "bbox": [0.1, 0.1, 0.2, 0.2]}],
            "dimensions": [{"id": "D1", "value": 50.0, "is_basic": True}],
            "fcfs": [
                {
                    "id": "F1",
                    "characteristic": "position",
                    "tolerance": 0.25,
                    "datum_refs": [{"label": "A"}, {"label": "B"}],
                }
            ],
        }
    )


def define_b() -> dict:
    return {
        "op": "add_datum",
        "label": "B",
        "location": "the bottom edge of the plate",
        "rationale": "The position tolerance is measured from B, so B has to be a real feature.",
        "finding_ids": ["GDT-001@F1"],
    }


def statuses(report) -> dict[str, str]:
    return {status.key: status.status for status in report.statuses}


# --- the happy path -------------------------------------------------------------------


def test_a_good_fix_resolves_the_finding_in_round_one():
    drawing = drawing_with_undefined_datum()
    client = ScriptedClient({"edits": [define_b()]})

    report = correct(drawing, PAGE, client)

    assert len(client.calls) == 1, "the loop should stop as soon as no errors remain"
    assert len(report.rounds) == 1
    assert report.rounds[0].resolved == ["GDT-001@F1"]
    assert not report.rounds[0].introduced
    assert statuses(report) == {"GDT-001@F1": "resolved"}
    assert report.final_findings == []
    assert [datum.label for datum in report.final_drawing.datums] == ["A", "B"]


def test_the_drawing_passed_in_is_left_alone():
    drawing = drawing_with_undefined_datum()

    correct(drawing, PAGE, ScriptedClient({"edits": [define_b()]}))

    assert len(drawing.datums) == 1
    assert run_rules(drawing)[0].rule_id == "GDT-001", "the original must still be checkable"


def test_a_clean_drawing_never_calls_the_model():
    drawing = parse_drawing(
        {
            "units": "mm",
            "general_tolerance": {"linear_plus_minus": 0.1},
            "dimensions": [{"id": "D1", "value": 50.0}],
        }
    )
    client = ScriptedClient()

    report = correct(drawing, PAGE, client)

    assert client.calls == []
    assert report.rounds == []
    assert report.statuses == []


# --- a fix that breaks something else -------------------------------------------------


def test_a_round_that_introduces_an_error_is_recorded_and_fixed_in_the_next_round():
    drawing = drawing_with_undefined_datum()
    # Round 1 defines B, which clears GDT-001, but also turns the frame into a flatness
    # control — and a form control with datum references is GDT-003.
    breaking = {
        "op": "set_field",
        "target": "fcf",
        "id": "F1",
        "field": "characteristic",
        "value": "flatness",
        "rationale": "Reading the symbol again, it looks like flatness.",
        "finding_ids": ["GDT-001@F1"],
    }
    repair = {
        "op": "set_field",
        "target": "fcf",
        "id": "F1",
        "field": "characteristic",
        "value": "position",
        "rationale": "The frame locates a hole, so position is the right characteristic.",
        "finding_ids": ["GDT-003@F1"],
    }
    client = ScriptedClient({"edits": [define_b(), breaking]}, {"edits": [repair]})

    report = correct(drawing, PAGE, client)

    assert len(report.rounds) == 2
    first, second = report.rounds
    assert first.resolved == ["GDT-001@F1"]
    assert first.introduced == ["GDT-003@F1"], "the loop must notice what it broke"
    assert second.resolved == ["GDT-003@F1"]
    assert report.final_findings == []
    # Only GDT-001 was on the drawing to begin with, and it is the only thing reported on.
    assert statuses(report) == {"GDT-001@F1": "resolved"}


def test_the_next_round_is_told_what_the_last_one_introduced():
    client = ScriptedClient(
        {
            "edits": [
                {
                    "op": "set_field",
                    "target": "fcf",
                    "id": "F1",
                    "field": "characteristic",
                    "value": "flatness",
                }
            ]
        }
    )

    correct(drawing_with_undefined_datum(), PAGE, client)

    second_prompt = client.calls[1]["user"]
    assert "introduced_by_this_round" in second_prompt
    assert "GDT-003@F1" in second_prompt


# --- refusals -------------------------------------------------------------------------


def test_deleting_a_frame_is_refused_and_the_finding_stays_unresolved():
    drawing = drawing_with_undefined_datum()
    deletion = {
        "op": "remove_fcf",
        "id": "F1",
        "rationale": "Removing the frame makes the finding go away.",
        "finding_ids": ["GDT-001@F1"],
    }
    client = ScriptedClient({"edits": [deletion]}, {"edits": [deletion]}, {"edits": [deletion]})

    report = correct(drawing, PAGE, client)

    assert len(report.rounds) == 3, "a refused edit still costs a round"
    assert all(not record.applied for record in report.rounds)
    assert all(len(record.rejected) == 1 for record in report.rounds)
    assert "never deleted" in report.rounds[0].rejected[0].reason
    assert len(report.final_drawing.fcfs) == 1, "the frame must still be on the drawing"
    assert statuses(report) == {"GDT-001@F1": "unresolved"}


def test_the_next_round_is_told_why_an_edit_was_refused():
    client = ScriptedClient({"edits": [{"op": "remove_fcf", "id": "F1"}]})

    correct(drawing_with_undefined_datum(), PAGE, client)

    second_prompt = client.calls[1]["user"]
    assert "refused_because" in second_prompt
    assert "not an allowed operation" in second_prompt


def test_malformed_edits_are_rejected_without_crashing():
    drawing = drawing_with_undefined_datum()
    client = ScriptedClient(
        {"edits": ["just define datum B", 42, {"op": 7}, {"no_op_key": True}]},
        {"edits": "define datum B please"},
        {"not_an_edits_key": []},
    )

    report = correct(drawing, PAGE, client)

    assert len(report.rounds) == 3
    assert [len(record.rejected) for record in report.rounds] == [4, 1, 1]
    assert all(not record.applied for record in report.rounds)
    assert report.final_drawing == drawing
    assert statuses(report) == {"GDT-001@F1": "unresolved"}


def test_a_failing_model_call_stops_the_loop_and_is_recorded():
    client = ScriptedClient(RuntimeError("the provider timed out"))

    report = correct(drawing_with_undefined_datum(), PAGE, client)

    assert len(report.rounds) == 1
    assert "timed out" in report.rounds[0].error
    assert statuses(report) == {"GDT-001@F1": "unresolved"}


# --- giving up ------------------------------------------------------------------------


def test_a_model_that_never_fixes_anything_stops_after_three_rounds():
    drawing = drawing_with_undefined_datum()
    # A real, applicable edit that has nothing to do with the finding: the loop keeps
    # going because the error is still there, not because nothing was applied.
    useless = {
        "op": "set_field",
        "target": "dimension",
        "id": "D1",
        "field": "text_raw",
        "value": "50",
        "rationale": "Tidying the callout text.",
        "finding_ids": ["GDT-001@F1"],
    }
    client = ScriptedClient({"edits": [useless]}, {"edits": [useless]}, {"edits": [useless]})

    report = correct(drawing, PAGE, client)

    assert len(client.calls) == 3, "three rounds, and no fourth"
    assert len(report.rounds) == 3
    assert all(len(record.applied) == 1 for record in report.rounds)
    assert all(not record.resolved for record in report.rounds)
    assert statuses(report) == {"GDT-001@F1": "unresolved"}
    assert report.unresolved_count == 1


def test_max_rounds_is_adjustable():
    client = ScriptedClient()

    report = correct(drawing_with_undefined_datum(), PAGE, client, max_rounds=1)

    assert len(client.calls) == 1
    assert len(report.rounds) == 1


def test_the_model_does_not_get_to_decide_what_was_resolved():
    drawing = drawing_with_undefined_datum()
    # The edit applies cleanly and claims the finding; the rules disagree.
    client = ScriptedClient(
        {
            "edits": [
                {
                    "op": "set_field",
                    "target": "dimension",
                    "id": "D1",
                    "field": "text_raw",
                    "value": "50",
                    "rationale": "This resolves the undefined datum.",
                    "finding_ids": ["GDT-001@F1"],
                }
            ]
        }
    )

    report = correct(drawing, PAGE, client, max_rounds=1)

    assert report.rounds[0].applied[0].finding_ids == ["GDT-001@F1"]
    assert not report.rounds[0].resolved
    assert statuses(report) == {"GDT-001@F1": "unresolved"}


# --- the real sample ------------------------------------------------------------------


def test_plate_001_undefined_datum_is_resolved_by_removing_the_c_reference():
    drawing = parse_drawing(json.loads(FIXTURE_JSON.read_text()))
    assert [finding.rule_id for finding in run_rules(drawing)] == ["GDT-001"]

    client = ScriptedClient(
        {
            "edits": [
                {
                    "op": "remove_datum_ref",
                    "fcf_id": "fcf_1",
                    "label": "C",
                    "rationale": (
                        "The plate is located by two datums, so the third reference is "
                        "surplus and nothing on the sheet defines it."
                    ),
                    "finding_ids": ["GDT-001@fcf_1"],
                }
            ]
        }
    )

    report = correct(drawing, PAGE, client)

    assert len(report.rounds) == 1
    assert report.rounds[0].resolved == ["GDT-001@fcf_1"]
    assert report.final_findings == []
    assert statuses(report) == {"GDT-001@fcf_1": "resolved"}
    frame = report.final_drawing.fcfs[0]
    assert [ref.label for ref in frame.datum_refs] == ["A", "B"]
    assert frame.characteristic.value == "position", "the frame itself is untouched"


def test_the_report_serializes_for_corrections_json():
    drawing = drawing_with_undefined_datum()
    client = ScriptedClient({"edits": [define_b(), {"op": "remove_fcf", "id": "F1"}]})

    payload = correct(drawing, PAGE, client).to_dict()
    round_one = json.loads(json.dumps(payload))["rounds"][0]

    assert round_one["applied"][0]["description"].startswith("add_datum")
    assert round_one["applied"][0]["rationale"]
    assert round_one["rejected"][0]["reason"]
    assert round_one["resolved"] == ["GDT-001@F1"]
