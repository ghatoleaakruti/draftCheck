"""Tests for the public test-results snapshot that run_eval.py writes for the web app.

No network: runs come from a scripted client, and nothing is written outside tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from draftcheck.config import SUPPORTED_PROVIDERS
from scripts import run_eval
from scripts.run_eval import (
    DRAWINGS,
    EXPLAIN_SCORE_VERSION,
    METHOD_NOTE,
    SCORE_VERSION,
    SHEET_SIDE,
    SNAPSHOT_SCHEMA,
    THUMB_SIDE,
    build_snapshot,
    load_ground_truth,
    write_snapshot,
)
from tests.test_eval_suite import ScriptedClient, _perfect_explanation

PROVIDERS = list(SUPPORTED_PROVIDERS)


def _results_with_runs(tmp_path: Path, stems: list[str]) -> dict:
    """Real result entries for ``stems`` on baseten, from the scripted client."""
    clients = {}
    for stem in stems:
        explanation = _perfect_explanation(stem)
        if stem == "flange":
            explanation["part"]["type"] = "flange"
        clients[stem] = ScriptedClient(load_ground_truth(stem), explanation=explanation)
    results: dict = {}
    for stem in stems:
        results = run_eval.run_eval(
            [stem], ["baseten"], results_path=tmp_path / "r.json",
            client_for=lambda _, s=stem: clients[s], explain=True, log=lambda _: None,
        )
    return results


def test_the_snapshot_lists_every_drawing_with_its_features() -> None:
    snapshot = build_snapshot({"runs": {}}, list(DRAWINGS), PROVIDERS)
    assert snapshot["schema"] == SNAPSHOT_SCHEMA
    assert snapshot["method"] == METHOD_NOTE
    assert [p["id"] for p in snapshot["providers"]] == PROVIDERS
    assert [d["stem"] for d in snapshot["drawings"]] == list(DRAWINGS)
    suite = snapshot["suite"]
    assert suite["drawings"] == len(DRAWINGS)
    assert suite["seeded_errors"] == sum(
        len(load_ground_truth(s)["seeded_errors"]) for s in DRAWINGS)
    joint = next(d for d in snapshot["drawings"] if d["stem"] == "bolted_joint")
    assert joint["part_type"] == "bolted_joint_assembly"
    assert joint["features"]["balloons"] == 5 and joint["features"]["tables"] == ["parts_list"]
    assert joint["thumb"] == "eval/thumbs/bolted_joint.png"
    assert joint["pdf"] == "eval/pdfs/bolted_joint.pdf"


def test_without_results_every_run_is_not_run() -> None:
    snapshot = build_snapshot({"runs": {}}, ["flange"], PROVIDERS)
    (flange,) = snapshot["drawings"]
    assert all(run == {"status": "not run"} for run in flange["runs"].values())
    assert all(e == {"status": "not run"} for e in flange["explain"].values())
    (seeded,) = flange["seeded_errors"]
    assert seeded["rule_id"] == "GDT-001"
    assert seeded["caught_by"] == dict.fromkeys(PROVIDERS)
    assert snapshot["totals"]["baseten"]["runs_ok"] == 0


def test_finished_runs_carry_scores_and_who_caught_each_error(tmp_path: Path) -> None:
    results = _results_with_runs(tmp_path, ["flange", "gear_spur"])
    snapshot = build_snapshot(results, ["flange", "gear_spur"], PROVIDERS)
    flange, gear = snapshot["drawings"]

    run = flange["runs"]["baseten"]
    assert run["status"] == "ok" and run["errors"]["errors_caught"] == 1
    assert set(run) == {"status", "model", "fell_back", "latency_s", "extraction", "errors",
                        "fixes"}
    assert flange["seeded_errors"][0]["caught_by"] == {"baseten": True, "meta": None}
    assert flange["runs"]["meta"] == {"status": "not run"}

    explain = gear["explain"]["baseten"]
    assert explain["status"] == "ok" and explain["part_type_correct"] is True
    totals = snapshot["totals"]["baseten"]
    assert totals["runs_ok"] == 2
    assert totals["part_types_recognized"] == totals["part_types_scored"] == 2


def test_an_old_scoring_shows_as_stale() -> None:
    results = {
        "runs": {"plate_clean|baseten": {"status": "ok", "score_version": SCORE_VERSION - 1}},
        "explain": {"plate_clean|meta": {"status": "ok",
                                          "score_version": EXPLAIN_SCORE_VERSION - 1}},
    }
    (drawing,) = build_snapshot(results, ["plate_clean"], PROVIDERS)["drawings"]
    assert drawing["runs"]["baseten"] == {"status": "stale"}
    assert drawing["explain"]["meta"] == {"status": "stale"}


def test_no_model_text_reasons_or_secrets_reach_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BASETEN_API_KEY", "sk-planted-baseten-key")
    monkeypatch.setenv("META_API_KEY", "planted-meta-key")
    results = _results_with_runs(tmp_path, ["flange"])
    run = results["runs"]["flange|baseten"]
    run["extraction"]["parse_issues"] = ["MODEL-TEXT-dimensions[9]: something the model wrote"]
    run["errors"]["findings"] = ["GDT-001@MODEL-TEXT-fcf_secret"]
    run["errors"]["false_alarms"] = ["DIM-001@MODEL-TEXT-dim_7"]
    run["fixes"]["round_errors"] = ["MODEL-TEXT a round failed"]
    run["reason"] = "MODEL-TEXT reason"
    results["explain"]["flange|baseten"]["parse_issues"] = ["MODEL-TEXT views[0]"]
    results["runs"]["flange|meta"] = {"status": "failed", "reason": "MODEL-TEXT boom",
                                      "score_version": SCORE_VERSION}

    text = json.dumps(build_snapshot(results, ["flange"], PROVIDERS))
    assert "MODEL-TEXT" not in text
    assert "planted" not in text and "sk-" not in text
    for key in ('"reason"', '"parse_issues"', '"findings"', '"round_errors"'):
        assert key not in text
    # A false alarm is kept only as its rule id.
    snapshot = json.loads(text)
    assert snapshot["drawings"][0]["runs"]["baseten"]["errors"]["false_alarms"] == ["DIM-001"]
    assert snapshot["drawings"][0]["runs"]["meta"] == {"status": "failed"}


def test_write_snapshot_writes_json_images_and_pdfs(tmp_path: Path) -> None:
    out = tmp_path / "eval"
    path = write_snapshot({"runs": {}}, ["hex_nut", "flange"], PROVIDERS, out)
    assert path == out / "results.json"
    assert [d["stem"] for d in json.loads(path.read_text())["drawings"]] == ["hex_nut", "flange"]
    for stem in ("hex_nut", "flange"):
        with Image.open(out / "thumbs" / f"{stem}.png") as thumb:
            assert max(thumb.size) <= THUMB_SIDE
        with Image.open(out / "sheets" / f"{stem}.png") as sheet:
            assert THUMB_SIDE < max(sheet.size) <= SHEET_SIDE
        sample = Path("samples/generated") / f"{stem}.pdf"
        assert (out / "pdfs" / f"{stem}.pdf").read_bytes() == sample.read_bytes()


def test_snapshot_only_calls_no_model_and_leaves_the_summary_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_runs(*args, **kwargs):
        raise AssertionError("--snapshot-only must not run the eval")

    monkeypatch.setattr(run_eval, "run_eval", no_runs)
    summary = tmp_path / "EVAL.md"
    code = run_eval.main(["--snapshot-only", "--results", str(tmp_path / "missing.json"),
                          "--summary", str(summary), "--snapshot", str(tmp_path / "snap")])
    assert code == 0
    assert not summary.exists()
    snapshot = json.loads((tmp_path / "snap" / "results.json").read_text())
    assert len(snapshot["drawings"]) == len(DRAWINGS)
    assert not (tmp_path / "missing.json").exists()  # the results file is not written either


def test_a_full_run_also_writes_the_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_eval, "run_eval", lambda *a, **k: {"runs": {}})
    monkeypatch.setattr(run_eval, "DRAWINGS", ("plate_clean",))
    run_eval.main(["--drawings", "plate_clean", "--providers", "baseten",
                   "--results", str(tmp_path / "r.json"), "--summary", str(tmp_path / "E.md"),
                   "--snapshot", str(tmp_path / "snap")])
    assert (tmp_path / "snap" / "thumbs" / "plate_clean.png").is_file()
