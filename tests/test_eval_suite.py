"""Tests for the evaluation suite: generator, ground-truth sanity, and eval scoring.

No network anywhere: model calls go to a scripted fake client.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from draftcheck.config import ConfigError
from draftcheck.llm_client import JSONChatResult, Usage
from draftcheck.prompts import EXTRACTION_SYSTEM
from draftcheck.render import load_pages
from draftcheck.rules import run_rules
from draftcheck.schema import Drawing, finding_key, parse_drawing
from scripts import run_eval
from scripts.drawing_suite import SUITE, build_suite
from scripts.run_eval import (
    DRAWINGS,
    expected_rule_ids,
    load_ground_truth,
    score_extraction,
    score_findings,
    summary_markdown,
)

SAMPLES = Path("samples/generated")


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Path]]:
    return build_suite(tmp_path_factory.mktemp("suite"), write_png=False)


# --- Generator --------------------------------------------------------------------------


def test_suite_has_the_requested_drawings() -> None:
    assert set(SUITE) == {
        "plate_clean",
        "plate_dup_datum",
        "plate_flatness_datum",
        "plate_no_basic",
        "plate_no_gentol",
        "plate_edge_hole",
        "bracket_01",
        "plate_inch_multi",
        "gear_spur",
        "hex_nut",
        "hex_bolt",
        "flange",
        "stepped_shaft",
        "gripper_claw",
        "block_3view",
        "bolted_joint",
    }


@pytest.mark.parametrize("stem", list(SUITE))
def test_generated_pdf_renders(built: dict, stem: str) -> None:
    pdf = built[stem]["pdf"]
    assert pdf.read_bytes().startswith(b"%PDF-")
    page = load_pages(pdf, dpi=50, max_pages=1)[0]
    assert page.width > page.height  # landscape sheet


@pytest.mark.parametrize("stem", list(SUITE))
def test_generated_ground_truth_parses_cleanly(built: dict, stem: str) -> None:
    truth = json.loads(built[stem]["ground_truth"].read_text(encoding="utf-8"))
    drawing = parse_drawing(truth)
    assert drawing.parse_issues == []
    assert len(drawing.dimensions) == len(truth["dimensions"])
    assert len(drawing.fcfs) == len(truth["fcfs"])
    assert len(drawing.datums) == len(truth["datums"])
    for key in ("datums", "dimensions", "fcfs", "seeded_errors"):
        for item in truth[key]:
            x0, y0, x1, y1 = item["bbox"]
            assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1, (stem, item)


@pytest.mark.parametrize("stem", list(SUITE))
def test_committed_ground_truth_is_current(built: dict, stem: str) -> None:
    """samples/generated/ must match what the generator produces today."""
    fresh = built[stem]["ground_truth"].read_text(encoding="utf-8")
    assert (SAMPLES / f"{stem}.gt.json").read_text(encoding="utf-8") == fresh


def test_inch_drawing_is_in_inches_with_four_holes_and_two_frames() -> None:
    truth = load_ground_truth("plate_inch_multi")
    assert truth["units"] == "in"
    note = next(d for d in truth["dimensions"] if d["feature_type"] == "diameter")
    assert note["quantity"] == 4
    assert len(truth["fcfs"]) == 2


def test_no_gentol_drawing_has_no_general_tolerance() -> None:
    assert load_ground_truth("plate_no_gentol")["general_tolerance"] is None


# --- Ground-truth sanity: the rules find exactly the seeded errors ----------------------


@pytest.mark.parametrize("stem", DRAWINGS)
def test_rules_find_exactly_the_seeded_errors(stem: str) -> None:
    truth = load_ground_truth(stem)
    findings = run_rules(parse_drawing(truth))
    assert sorted(f.rule_id for f in findings) == expected_rule_ids(truth)
    keys = [error["finding_key"] for error in truth["seeded_errors"] if "finding_key" in error]
    if keys:
        assert sorted(finding_key(f) for f in findings) == sorted(keys)


def test_expected_rule_ids_maps_plate_001_legacy_type() -> None:
    assert expected_rule_ids(load_ground_truth("plate_001")) == ["GDT-001"]


def test_expected_rule_ids_rejects_an_error_with_no_rule() -> None:
    with pytest.raises(ValueError):
        expected_rule_ids({"seeded_errors": [{"id": "x", "type": "mystery"}]})


# --- Scoring on fake results ------------------------------------------------------------


def _truth(stem: str = "bracket_01") -> Drawing:
    return parse_drawing(load_ground_truth(stem))


def test_perfect_extraction_scores_full_marks() -> None:
    truth = _truth()
    score = score_extraction(truth, truth)
    assert score["dims_found"] == score["dims_correct"] == score["dims_expected"] == 11
    assert score["fcfs_correct"] == score["fcfs_expected"] == 2
    assert score["datums_found"] == score["datums_expected"] == 2
    assert score["dims_extra"] == score["fcfs_extra"] == score["datums_extra"] == 0
    assert score["units_correct"] and score["general_tolerance_correct"]


def test_wrong_tolerance_is_found_but_not_correct() -> None:
    truth = _truth()
    data = truth.model_dump(mode="json")
    data["dimensions"][0]["tol_plus"] = 0.5  # 120 ±0.2 read as +0.5
    data["dimensions"].pop()  # lose the Ø6 callout
    data["fcfs"][0]["datum_refs"] = [{"label": "A"}, {"label": "B"}]  # dropped D
    data["datums"].append({"label": "C"})
    score = score_extraction(truth, parse_drawing(data))
    assert score["dims_found"] == 10
    assert score["dims_correct"] == 9
    assert score["fcfs_found"] == 2 and score["fcfs_correct"] == 1
    assert score["datums_found"] == 2 and score["datums_extra"] == 1


def test_duplicate_values_match_the_right_twin() -> None:
    """Three 30s on the bracket: a correct reading must not be spent on a wrong one."""
    truth = _truth()
    score = score_extraction(truth, truth.model_copy(deep=True))
    assert score["dims_correct"] == score["dims_expected"]


def test_findings_scoring_counts_caught_missed_and_false_alarms() -> None:
    truth = load_ground_truth("bracket_01")
    data = copy.deepcopy(truth)
    data["datums"].append({"id": "x", "label": "B"})  # adds a GDT-002 false alarm
    data["fcfs"][1]["datum_refs"] = []  # GDT-003 no longer present -> missed
    findings = run_rules(parse_drawing(data))
    score = score_findings(expected_rule_ids(truth), findings)
    assert score["errors_expected"] == 3
    assert score["errors_caught"] == 2
    assert score["missed"] == ["GDT-003"]
    assert [key.split("@")[0] for key in score["false_alarms"]] == ["GDT-002"]


# --- run_eval end to end with a fake client ---------------------------------------------


class ScriptedClient:
    """Extraction returns a given dict; every correction round proposes ``edits``."""

    def __init__(self, extraction: dict, edits: list | None = None) -> None:
        self.extraction = extraction
        self.edits = edits or []
        self.calls = 0

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        self.calls += 1
        data = self.extraction if system == EXTRACTION_SYSTEM else {"edits": self.edits}
        return JSONChatResult(
            data=data,
            text=json.dumps(data),
            latency_s=0.01,
            usage=Usage(1, 1, 2),
            model="scripted",
            attempts=1,
            provider="baseten",
        )


def test_run_one_scores_a_perfect_reading_and_a_verified_fix() -> None:
    truth = load_ground_truth("plate_flatness_datum")
    edit = {
        "op": "remove_datum_ref",
        "fcf_id": "fcf_flatness",
        "label": "A",
        "rationale": "Flatness takes no datum.",
        "finding_ids": ["GDT-003@fcf_flatness"],
    }
    client = ScriptedClient(truth, [edit])
    result = run_eval.run_one("plate_flatness_datum", "baseten", client_for=lambda _: client)

    assert result["status"] == "ok"
    assert result["errors"]["errors_caught"] == 1
    assert result["errors"]["false_alarms"] == []
    assert result["fixes"]["fixes_verified"] == 1
    assert result["fixes"]["findings_after"] == []
    assert result["fixes"]["rounds"] == 1
    assert result["fell_back"] is False
    assert result["extraction"]["dims_correct"] == result["extraction"]["dims_expected"]


def _remove(fcf_id: str, label: str) -> dict:
    return {
        "op": "remove_datum_ref",
        "fcf_id": fcf_id,
        "label": label,
        "rationale": "test",
        "finding_ids": [],
    }


def test_fixes_verified_counts_errors_only() -> None:
    """bracket_01: two errors (GDT-001, GDT-003) and one warning (MFG-001)."""
    truth = load_ground_truth("bracket_01")
    edits = [_remove("fcf_position_holes", "D"), _remove("fcf_flatness", "A")]
    client = ScriptedClient(truth, edits)
    fixes = run_eval.run_one("bracket_01", "baseten", client_for=lambda _: client)["fixes"]

    assert fixes["findings_before"] == 3
    assert fixes["errors_before"] == 2
    assert fixes["fixes_verified"] == 2
    assert fixes["unresolved"] == 0
    # The warning is still there, and does not count against the loop.
    assert fixes["findings_after"] == ["MFG-001@dim_hole3_x"]


@pytest.mark.parametrize("stem", ["plate_edge_hole", "plate_clean"])
def test_no_error_findings_shows_fixes_as_na(tmp_path: Path, stem: str) -> None:
    truth = load_ground_truth(stem)
    results = run_eval.run_eval(
        [stem],
        ["baseten"],
        results_path=tmp_path / "r.json",
        client_for=lambda _: ScriptedClient(truth),
        log=lambda _: None,
    )
    fixes = results["runs"][f"{stem}|baseten"]["fixes"]
    assert fixes["errors_before"] == 0
    assert fixes["fixes_verified"] == 0
    assert fixes["rounds"] == 0

    page = summary_markdown(results, [stem], ["baseten"])
    row = next(line for line in page.splitlines() if line.startswith(f"| {stem} | baseten"))
    assert [cell.strip() for cell in row.strip("|").split("|")][9] == "n/a"
    total = next(line for line in page.splitlines() if line.startswith("| baseten |"))
    assert [cell.strip() for cell in total.strip("|").split("|")][10] == "n/a"


def test_warning_only_drawing_scores_its_warning_as_caught(tmp_path: Path) -> None:
    """n/a is about fixes only: the MFG-001 warning is still a caught seeded error."""
    truth = load_ground_truth("plate_edge_hole")
    result = run_eval.run_one(
        "plate_edge_hole", "baseten", client_for=lambda _: ScriptedClient(truth)
    )
    assert result["errors"]["errors_caught"] == 1
    assert result["fixes"]["findings_before"] == 1


def test_results_from_an_older_scoring_are_rerun_and_shown_stale(tmp_path: Path) -> None:
    results_path = tmp_path / "r.json"
    old = {"status": "ok", "fixes": {"findings_before": 1, "fixes_verified": 1}}
    results_path.write_text(json.dumps({"runs": {"plate_clean|baseten": old}}))

    page = summary_markdown(json.loads(results_path.read_text()), ["plate_clean"], ["baseten"])
    assert "| plate_clean | baseten | stale (rerun) |" in page

    client = ScriptedClient(load_ground_truth("plate_clean"))
    results = run_eval.run_eval(
        ["plate_clean"], ["baseten"], results_path=results_path,
        client_for=lambda _: client, log=lambda _: None,
    )
    assert client.calls == 1
    assert results["runs"]["plate_clean|baseten"]["score_version"] == run_eval.SCORE_VERSION


def test_run_eval_caches_finished_runs_and_retries_skipped(tmp_path: Path) -> None:
    results_path = tmp_path / "results.json"
    truth = load_ground_truth("plate_clean")
    clients = {"baseten": ScriptedClient(truth)}

    def client_for(provider: str) -> ScriptedClient:
        if provider not in clients:
            raise ConfigError("Missing required environment variable: META_API_KEY.")
        return clients[provider]

    quiet = {"log": lambda _: None, "results_path": results_path, "client_for": client_for}
    results = run_eval.run_eval(["plate_clean"], ["baseten", "meta"], **quiet)
    runs = results["runs"]
    assert runs["plate_clean|baseten"]["status"] == "ok"
    assert runs["plate_clean|baseten"]["errors"]["false_alarms"] == []
    assert runs["plate_clean|meta"]["status"] == "skipped"
    assert "META_API_KEY" in runs["plate_clean|meta"]["reason"]
    assert json.loads(results_path.read_text())["runs"].keys() == runs.keys()

    # Second run: baseten is cached (no new calls), meta is tried again.
    calls = clients["baseten"].calls
    clients["meta"] = ScriptedClient(truth)
    again = run_eval.run_eval(["plate_clean"], ["baseten", "meta"], **quiet)
    assert clients["baseten"].calls == calls
    assert again["runs"]["plate_clean|meta"]["status"] == "ok"


def test_run_eval_records_a_real_failure_and_moves_on(tmp_path: Path) -> None:
    class Broken:
        def chat_json(self, *args: object) -> JSONChatResult:
            raise RuntimeError("model said something odd")

    results = run_eval.run_eval(
        ["plate_clean", "plate_no_basic"],
        ["baseten"],
        results_path=tmp_path / "r.json",
        client_for=lambda _: Broken(),
        log=lambda _: None,
    )
    for stem in ("plate_clean", "plate_no_basic"):
        run = results["runs"][f"{stem}|baseten"]
        assert run["status"] == "failed"
        assert "RuntimeError" in run["reason"]


def test_summary_has_a_row_per_run_and_totals(tmp_path: Path) -> None:
    truth = load_ground_truth("plate_no_basic")
    results = run_eval.run_eval(
        ["plate_no_basic"],
        ["baseten"],
        results_path=tmp_path / "r.json",
        client_for=lambda _: ScriptedClient(truth),
        log=lambda _: None,
    )
    results["runs"]["plate_clean|meta"] = {"status": "skipped", "reason": "not configured"}
    page = summary_markdown(results, ["plate_no_basic", "plate_clean"], ["baseten", "meta"])

    row = next(line for line in page.splitlines() if line.startswith("| plate_no_basic | baseten"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[2] == "ok"
    assert cells[3] == "6/6"  # dims found
    assert cells[7] == "1/1"  # errors caught
    assert cells[8] == "0"  # false alarms
    assert "| plate_clean | meta | skipped |" in page
    assert "| plate_clean | baseten | not run |" in page
    assert "## Totals" in page
    header_cols = page.splitlines()[page.splitlines().index("## Per drawing") + 2].count("|")
    for line in page.splitlines():
        if line.startswith("| plate_"):
            assert line.count("|") == header_cols, line
