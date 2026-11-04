"""Tests for the correction loop.

Every test drives the loop with a scripted client, so the model's replies are chosen by
the test and no network call is ever made. That is the point: the loop's job is to decide
what a reply is worth, and these tests hand it replies worth very different amounts —
a real fix, a fix that breaks something else, a deletion, nonsense, and an endless stall.
"""

import json
from pathlib import Path

import pytest

from draftcheck.agent import correct
from draftcheck.llm_client import (
    MODEL_TIMEOUT_MESSAGE,
    JSONChatResult,
    ModelCancelled,
    ModelTimeout,
    Usage,
)
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
    # Both edits cite GDT-001 and it clears, so both are kept — and together they turn
    # the frame into a flatness frame that still references datum A: a new GDT-003.
    client = ScriptedClient(
        {
            "edits": [
                {
                    "op": "remove_datum_ref",
                    "fcf_id": "F1",
                    "label": "B",
                    "finding_ids": ["GDT-001@F1"],
                },
                {
                    "op": "set_field",
                    "target": "fcf",
                    "id": "F1",
                    "field": "characteristic",
                    "value": "flatness",
                    "finding_ids": ["GDT-001@F1"],
                },
            ]
        }
    )

    report = correct(drawing_with_undefined_datum(), PAGE, client)

    assert report.rounds[0].introduced == ["GDT-003@F1"]
    assert report.rounds[0].rolled_back == []
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


def test_a_timed_out_round_stops_the_loop_and_keeps_the_rounds_before_it():
    """Round one proposes nothing useful, round two runs out of time: the loop ends there."""
    client = ScriptedClient({"edits": []}, ModelTimeout(), define_b())

    report = correct(drawing_with_undefined_datum(), PAGE, client)

    assert len(client.calls) == 2  # the third reply is never asked for
    assert [record.number for record in report.rounds] == [1, 2]
    assert report.rounds[0].error is None
    assert not report.rounds[0].timed_out
    assert report.rounds[1].timed_out
    assert report.rounds[1].error == MODEL_TIMEOUT_MESSAGE
    assert statuses(report) == {"GDT-001@F1": "unresolved"}
    assert report.to_dict()["rounds"][1]["timed_out"] is True


def test_a_cancelled_call_ends_the_loop_with_nothing_recorded():
    """Cancel is not a failed round: nobody wants the report, so the loop just stops."""
    client = ScriptedClient({"edits": []}, ModelCancelled(), define_b())

    with pytest.raises(ModelCancelled):
        correct(drawing_with_undefined_datum(), PAGE, client)

    assert len(client.calls) == 2  # round three is never asked for


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
    # Applied cleanly each time, cleared nothing, so each time it was rolled back.
    assert all(record.applied == [] for record in report.rounds)
    assert all(len(record.rolled_back) == 1 for record in report.rounds)
    assert all(not record.resolved for record in report.rounds)
    assert report.final_drawing == drawing  # nothing that failed was left on it
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

    assert report.rounds[0].applied == []
    [undone] = report.rounds[0].rolled_back
    assert undone.edit.finding_ids == ["GDT-001@F1"]
    assert undone.reason == "The re-run still reports GDT-001@F1."
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


# ---- Live callbacks ------------------------------------------------------------------


def _three_round_script() -> tuple[dict, dict, dict]:
    """Refused, applied-but-still-flagged, then the real fix — one edit per round."""
    return (
        {"edits": [{"op": "delete_fcf", "fcf_id": "F1", "finding_ids": ["GDT-001@F1"]}]},
        {
            "edits": [
                {
                    "op": "set_field",
                    "target": "fcf",
                    "id": "F1",
                    "field": "tolerance",
                    "value": 0.2,
                    "finding_ids": ["GDT-001@F1"],
                }
            ]
        },
        {"edits": [define_b()]},
    )


def test_callbacks_hear_each_round_with_the_data_the_report_keeps() -> None:
    heard: list[tuple] = []
    report = correct(
        drawing_with_undefined_datum(),
        PAGE,
        ScriptedClient(*_three_round_script()),
        on_round_start=lambda number, findings: heard.append(("start", number, findings)),
        on_proposal=lambda number, proposed, answered: heard.append(
            ("proposal", number, proposed, answered)
        ),
        on_round_end=lambda record: heard.append(("end", record)),
    )

    assert [entry[0] for entry in heard] == ["start", "proposal", "end"] * 3
    records = [entry[1] for entry in heard if entry[0] == "end"]
    # The very records the report holds, in order — not copies that could drift.
    assert records == report.rounds
    for index, record in enumerate(report.rounds):
        _, number, findings = heard[index * 3]
        _, proposed_number, proposed, answered = heard[index * 3 + 1]
        assert number == proposed_number == record.number
        assert findings == record.findings_before
        assert proposed == record.proposed
        assert answered == {
            "model": record.model,
            "provider": record.provider,
            "fell_back": record.fell_back,
            "fallback_reason": record.fallback_reason,
        }
    assert [len(r.rejected) for r in report.rounds] == [1, 0, 0]
    assert report.rounds[-1].resolved == ["GDT-001@F1"]


def test_callbacks_change_nothing_about_the_report() -> None:
    plain = correct(drawing_with_undefined_datum(), PAGE, ScriptedClient(*_three_round_script()))
    heard = correct(
        drawing_with_undefined_datum(),
        PAGE,
        ScriptedClient(*_three_round_script()),
        on_round_start=lambda *_: None,
        on_proposal=lambda *_: None,
        on_round_end=lambda *_: None,
    )
    assert heard.to_dict() == plain.to_dict()


def test_a_failed_round_is_reported_to_on_round_end_without_a_proposal() -> None:
    heard: list[str] = []
    report = correct(
        drawing_with_undefined_datum(),
        PAGE,
        ScriptedClient(ModelTimeout()),
        on_round_start=lambda *_: heard.append("start"),
        on_proposal=lambda *_: heard.append("proposal"),
        on_round_end=lambda record: heard.append(f"end:{record.timed_out}"),
    )
    assert heard == ["start", "end:True"]
    assert report.rounds[0].timed_out


def test_a_cancelled_round_never_reaches_on_round_end() -> None:
    heard: list[str] = []
    with pytest.raises(ModelCancelled):
        correct(
            drawing_with_undefined_datum(),
            PAGE,
            ScriptedClient(ModelCancelled()),
            on_round_start=lambda *_: heard.append("start"),
            on_round_end=lambda *_: heard.append("end"),
        )
    assert heard == ["start"]


# ---- Rolling back what failed ----------------------------------------------------------


def _tighten(finding: str = "GDT-001@F1") -> dict:
    """Applies cleanly to F1, and clears nothing."""
    return {
        "op": "set_field",
        "target": "fcf",
        "id": "F1",
        "field": "tolerance",
        "value": 0.2,
        "rationale": "Tighter is better.",
        "finding_ids": [finding],
    }


def test_a_failed_edit_is_rolled_back_and_the_next_round_starts_without_it():
    client = ScriptedClient({"edits": [_tighten()]}, {"edits": [define_b()]})

    report = correct(drawing_with_undefined_datum(), PAGE, client)

    first, second = report.rounds
    assert first.applied == []
    [undone] = first.rolled_back
    assert undone.reason == "The re-run still reports GDT-001@F1."
    # The rules were re-run on the reverted drawing: the same finding, nothing new.
    assert [f.rule_id for f in first.findings_after] == ["GDT-001"]
    assert first.introduced == []
    # Round two was shown the original tolerance, not the rolled-back one.
    drawing_sent = second_prompt_drawing(client)
    assert drawing_sent["fcfs"][0]["tolerance"] == 0.25
    # And the final drawing carries only the kept edit.
    assert report.final_drawing.fcfs[0].tolerance == 0.25
    assert [d.label for d in report.final_drawing.datums] == ["A", "B"]
    assert statuses(report) == {"GDT-001@F1": "resolved"}


def second_prompt_drawing(client: ScriptedClient) -> dict:
    prompt = client.calls[1]["user"]
    start = prompt.index("{")
    return json.JSONDecoder().raw_decode(prompt[start:])[0]


def test_the_next_round_is_told_what_was_rolled_back_and_why():
    client = ScriptedClient({"edits": [_tighten()]})
    correct(drawing_with_undefined_datum(), PAGE, client, max_rounds=2)

    second_prompt = client.calls[1]["user"]
    assert "rolled_back_because" in second_prompt
    assert "The re-run still reports GDT-001@F1." in second_prompt


def test_a_kept_edit_stays_when_another_in_the_same_round_is_rolled_back():
    keep = {
        "op": "remove_datum_ref",
        "fcf_id": "F1",
        "label": "B",
        "finding_ids": ["GDT-001@F1"],
    }
    # Cites a finding the drawing never had, so it cannot have cleared it.
    stray = _tighten("MFG-001@D1")
    report = correct(drawing_with_undefined_datum(), PAGE, ScriptedClient({"edits": [keep, stray]}))

    [only] = report.rounds
    assert [edit.op for edit in only.applied] == ["remove_datum_ref"]
    assert [item.edit.finding_ids for item in only.rolled_back] == [["MFG-001@D1"]]
    frame = report.final_drawing.fcfs[0]
    assert (frame.tolerance, [ref.label for ref in frame.datum_refs]) == (0.25, ["A"])
    assert report.to_dict()["rounds"][0]["rolled_back"][0]["reason"] == (
        "The re-run still reports MFG-001@D1."
    )


def test_an_edit_citing_nothing_is_rolled_back_even_if_it_helped():
    """With no citation there is nothing for the re-run to confirm, so it is not kept."""
    uncited = {**define_b(), "finding_ids": []}
    client = ScriptedClient({"edits": [uncited]})
    report = correct(drawing_with_undefined_datum(), PAGE, client, max_rounds=1)

    assert report.rounds[0].applied == []
    assert report.rounds[0].rolled_back[0].reason.startswith("It cites no finding")
    assert [d.label for d in report.final_drawing.datums] == ["A"]
