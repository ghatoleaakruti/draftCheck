"""Run the evaluation suite through the real pipeline, for each provider, and score it.

Run with:
    python -m scripts.run_eval                         # every drawing, both providers
    python -m scripts.run_eval --drawings plate_clean --providers baseten
    python -m scripts.run_eval --force                 # ignore the cache, run everything
    python -m scripts.run_eval --explain               # also explain each drawing and score it
    python -m scripts.run_eval --snapshot-only         # rebuild the web app's snapshot only

For each (drawing, provider) it does what the web server does: render page 1, extract with
the model, run the rules, then run the correction loop. Model calls go through the
server's own guarded client (``api.guard_for``), so Baseten falls back to Meta exactly as it
does live, and the result records whether that happened.

Scoring is plain Python against the drawing's ground truth (``<stem>.gt.json``):

* **Dimensions** are matched to the ground truth by value. A match is *correct* when its
  tolerances and basic flag also agree.
* **Frames** are matched by characteristic; *correct* when tolerance, zone, modifier, and
  datum letters (in order) also agree. **Datums** are matched by label.
* **Errors**: findings on the extracted drawing are matched to ``seeded_errors`` by rule id.
  A seeded error with no matching finding is *missed*; a finding with no seeded error to
  match is a *false alarm*. Rule ids, not finding keys, because the model names items its
  own way (``F1`` rather than ``fcf_position_holes``).
* **Fixes verified**: original findings the correction loop cleared, as decided by
  re-running the rules (``CorrectionReport.statuses``), never by the model's own claim.

**Explanations** (``--explain``): for each (drawing, provider) the "About this drawing"
explanation is also requested, on the drawing's *ground truth* and the rules' findings on
it, so it cites the ground-truth ids and does not depend on how well the drawing was read.
It is scored in plain Python: whether the part type is right, which view names it found
against the sheet's views, and the share of dimensions it placed in the right view. These
results live under their own ``explain`` key with their own version, so the extraction
results already cached stay valid.

**Drawing features**: a count, per drawing, of what its ground truth contains (views,
sections, details, frames by type, datums, dimensions, tables, balloons, notes). Derived
from the ground truth, so it is recomputed on every run rather than cached.

**Public snapshot**: after every run (or alone, with ``--snapshot-only``, which calls no
model and leaves ``docs/EVAL.md`` alone), ``draftcheck/static/eval/`` gets what the web
app's "Test results" page shows: ``results.json`` (scores, drawing features, part type,
model, latency, fallback — copied field by field, so no model text, no reasons, and no
configuration can reach it), a thumbnail and a larger image of each drawing, and the
drawing's PDF for "Try it live". These are our own generated drawings, so they can be
published.

Runs happen one at a time. Each finished run is written to ``outputs/eval/results.json``
as soon as it ends, and a rerun skips it. A provider that cannot be reached (not
configured, timed out, connection refused, 5xx) is marked ``skipped`` and is not cached,
so the next run tries it again. ``docs/EVAL.md`` is rewritten from the results file at
the end.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from draftcheck.agent import CorrectionReport, correct
from draftcheck.config import SUPPORTED_PROVIDERS, ConfigError
from draftcheck.explain import Explanation, explain_drawing
from draftcheck.pipeline import ExtractionResult, SupportsChatJSON, extract_page
from draftcheck.render import load_pages
from draftcheck.rules import run_rules
from draftcheck.schema import Dimension, Drawing, Finding, finding_key
from scripts.drawing_suite import SUITE, DrawingSpec

SAMPLES_DIR = Path("samples/generated")
RESULTS_PATH = Path("outputs/eval/results.json")
SUMMARY_PATH = Path("docs/EVAL.md")
#: Where the public snapshot goes: served by the app at /eval/.
SNAPSHOT_DIR = Path("draftcheck/static/eval")

#: plate_001 first (the original sample), then the suite in its own order.
DRAWINGS: tuple[str, ...] = ("plate_001", *SUITE)

#: plate_001's ground truth predates ``rule_id`` on seeded errors and is kept unchanged,
#: so its error type is mapped to the rule here.
LEGACY_ERROR_RULES = {"missing_datum_feature": "GDT-001"}

#: Results with these statuses are kept and skipped on a rerun. "skipped" is not.
CACHED_STATUSES = frozenset({"ok", "failed"})

#: Bumped whenever a score's meaning or fields change. A cached result scored under a
#: different version is run again, so one results file never mixes two scorings.
#: 2: "fixes verified" counts error-severity findings only.
SCORE_VERSION = 2


#: The same, for explanation results (``results["explain"]``), kept apart so adding or
#: changing explanation scoring never invalidates a cached extraction run.
#: 2: "fastener" and "assembly" part types (the prompt offers them; scoring expects them).
EXPLAIN_SCORE_VERSION = 2


def is_cached(entry: dict | None, version: int = SCORE_VERSION) -> bool:
    """True when ``entry`` is a finished result under the current scoring."""
    return (
        entry is not None
        and entry.get("status") in CACHED_STATUSES
        and entry.get("score_version") == version
    )


# --- Ground truth -----------------------------------------------------------------------


def expected_rule_ids(ground_truth: dict) -> list[str]:
    """The rule id each seeded error must be caught by, one entry per error."""
    rule_ids = []
    for error in ground_truth.get("seeded_errors", []):
        rule_id = error.get("rule_id") or LEGACY_ERROR_RULES.get(error.get("type", ""))
        if rule_id is None:
            raise ValueError(f"seeded error {error.get('id')!r} names no rule")
        rule_ids.append(rule_id)
    return sorted(rule_ids)


def load_ground_truth(stem: str, samples_dir: Path = SAMPLES_DIR) -> dict:
    return json.loads((samples_dir / f"{stem}.gt.json").read_text(encoding="utf-8"))


# --- Scoring ----------------------------------------------------------------------------


def _same(a: float | None, b: float | None) -> bool:
    """Two optional numbers are equal (both missing counts as equal)."""
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)


def _match(
    expected: list[Any],
    found: list[Any],
    pairs: Callable[[Any, Any], bool],
    correct_: Callable[[Any, Any], bool],
) -> list[tuple[Any, Any]]:
    """Pair expected items with found ones, each used at most once.

    Exact (fully correct) pairs are taken first, so a correct reading is never stolen by
    a sloppier one with the same key; then whatever still pairs on the key alone.
    """
    left = list(range(len(found)))
    matched: list[tuple[Any, Any]] = []
    remaining = []
    for item in expected:
        hit = next((i for i in left if correct_(item, found[i])), None)
        if hit is None:
            remaining.append(item)
        else:
            left.remove(hit)
            matched.append((item, found[hit]))
    for item in remaining:
        hit = next((i for i in left if pairs(item, found[i])), None)
        if hit is not None:
            left.remove(hit)
            matched.append((item, found[hit]))
    return matched


def _dim_correct(truth: Dimension, got: Dimension) -> bool:
    return (
        _same(truth.value, got.value)
        and truth.is_basic == got.is_basic
        and _same(truth.tol_plus, got.tol_plus)
        and _same(truth.tol_minus, got.tol_minus)
    )


def _fcf_correct(truth: Any, got: Any) -> bool:
    return (
        truth.characteristic == got.characteristic
        and _same(truth.tolerance, got.tolerance)
        and truth.diameter_zone == got.diameter_zone
        and truth.material_modifier == got.material_modifier
        and [r.label for r in truth.datum_refs] == [r.label for r in got.datum_refs]
    )


def score_extraction(truth: Drawing, got: Drawing) -> dict:
    """How closely the extracted drawing matches the ground truth."""
    dims = _match(
        truth.dimensions, got.dimensions, lambda t, g: _same(t.value, g.value), _dim_correct
    )
    fcfs = _match(
        truth.fcfs, got.fcfs, lambda t, g: t.characteristic == g.characteristic, _fcf_correct
    )
    truth_labels = Counter(d.label for d in truth.datums)
    got_labels = Counter(d.label for d in got.datums)
    datums_found = sum((truth_labels & got_labels).values())

    def general(drawing: Drawing) -> float | None:
        tol = drawing.general_tolerance
        return tol.linear_plus_minus if tol else None

    return {
        "dims_expected": len(truth.dimensions),
        "dims_found": len(dims),
        "dims_correct": sum(1 for t, g in dims if _dim_correct(t, g)),
        "dims_extra": len(got.dimensions) - len(dims),
        "fcfs_expected": len(truth.fcfs),
        "fcfs_found": len(fcfs),
        "fcfs_correct": sum(1 for t, g in fcfs if _fcf_correct(t, g)),
        "fcfs_extra": len(got.fcfs) - len(fcfs),
        "datums_expected": len(truth.datums),
        "datums_found": datums_found,
        "datums_extra": len(got.datums) - datums_found,
        "units_correct": truth.units == got.units,
        "general_tolerance_correct": _same(general(truth), general(got)),
        "parse_issues": list(got.parse_issues),
    }


def score_findings(expected: list[str], findings: list[Finding]) -> dict:
    """Seeded errors caught and missed, and findings nobody seeded (false alarms)."""
    want = Counter(expected)
    caught_ids: list[str] = []
    false_alarms: list[str] = []
    for finding in findings:
        if want[finding.rule_id] > 0:
            want[finding.rule_id] -= 1
            caught_ids.append(finding.rule_id)
        else:
            false_alarms.append(finding_key(finding))
    return {
        "errors_expected": len(expected),
        "errors_caught": len(caught_ids),
        "caught": sorted(caught_ids),
        "missed": sorted(want.elements()),
        "false_alarms": false_alarms,
        "findings": [finding_key(f) for f in findings],
    }


def score_fixes(report: CorrectionReport) -> dict:
    """What the correction loop achieved, as the re-run rules decided it.

    Only error-severity findings count toward ``fixes_verified``: warnings never start the
    loop (see ``agent._has_errors``), so counting them would score the loop on work it is
    designed not to do. A drawing with no error findings has nothing to fix, and the
    summary shows it as "n/a" rather than 0/0 or 0/N.
    """
    errors = [s for s in report.statuses if s.finding.severity == "error"]
    return {
        "findings_before": len(report.original_findings),
        "errors_before": len(errors),
        "fixes_verified": sum(1 for s in errors if s.status == "resolved"),
        "unresolved": sum(1 for s in errors if s.status != "resolved"),
        "findings_after": [finding_key(f) for f in report.final_findings],
        "errors_after": sum(1 for f in report.final_findings if f.severity == "error"),
        "rounds": len(report.rounds),
        "edits_applied": sum(len(r.applied) for r in report.rounds),
        "edits_rejected": sum(len(r.rejected) for r in report.rounds),
        "round_errors": [r.error for r in report.rounds if r.error],
    }


def score_run(
    ground_truth: dict,
    extraction: ExtractionResult,
    findings: list[Finding],
    report: CorrectionReport,
) -> dict:
    """Every score for one finished run. Pure: no model, no files."""
    truth = Drawing.model_validate(ground_truth)
    fell_back = extraction.fell_back or any(r.fell_back for r in report.rounds)
    answered_by = sorted(
        {p for p in [extraction.provider, *(r.provider for r in report.rounds)] if p}
    )
    return {
        "extraction": score_extraction(truth, extraction.drawing),
        "errors": score_findings(expected_rule_ids(ground_truth), findings),
        "fixes": score_fixes(report),
        "model": extraction.model,
        "answered_by": answered_by,
        "fell_back": fell_back,
        "extract_attempts": extraction.attempts,
        "tokens": extraction.usage.total_tokens,
    }


# --- Layout, features, and explanation scoring -------------------------------------------

#: Ground-truth view kinds, as the explanation names views (``prompts.VIEW_NAMES``).
VIEW_KIND_NAMES = {
    "front": "front",
    "top": "top",
    "bottom": "bottom",
    "rear": "rear",
    "right_side": "right",
    "left_side": "left",
    "section": "section",
    "detail": "detail",
    "isometric": "isometric",
}

#: Which explanation part types (``prompts.PART_TYPES``) count as right for each ground
#: truth ``expected_part_type``. A nut and a bolt expect "fastener" and the bolted joint
#: "assembly"; "other" is still accepted for those three as a fallback. A gripper claw may
#: fairly be called a bracket.
PART_TYPE_MATCHES: dict[str, frozenset[str]] = {
    "plate": frozenset({"plate"}),
    "bracket": frozenset({"bracket"}),
    "spur_gear": frozenset({"gear"}),
    "hex_nut": frozenset({"fastener", "other"}),
    "hex_bolt": frozenset({"fastener", "other"}),
    "flange": frozenset({"flange"}),
    "stepped_shaft": frozenset({"shaft"}),
    "gripper_claw": frozenset({"bracket", "other"}),
    "machined_block": frozenset({"block"}),
    "bolted_joint_assembly": frozenset({"assembly", "other"}),
}


def drawing_layout(stem: str, ground_truth: dict) -> dict:
    """The part type, views, and each dimension's view, for scoring an explanation.

    Part sheets record these in their ground truth. The first nine drawings predate those
    fields and their ground truth is kept unchanged, so their layout is read from their
    recipe instead: a plate is one front view; bracket_01 adds a right side view.
    """
    if "views" in ground_truth:
        return {
            "part_type": ground_truth.get("expected_part_type"),
            "views": [(v["name"], v["kind"]) for v in ground_truth["views"]],
            "dim_views": {
                d["id"]: d["view"] for d in ground_truth.get("dimensions", []) if d.get("view")
            },
        }
    spec = SUITE.get(stem)
    ids = [d["id"] for d in ground_truth.get("dimensions", []) if d.get("id")]
    dim_views = dict.fromkeys(ids, "front")
    views = [("front", "front")]
    if isinstance(spec, DrawingSpec):
        dim_views.update({d.id: d.view for d in spec.dims})
        if spec.side is not None:
            views.append(("side", "right_side"))
    part_type = "bracket" if stem.startswith("bracket") else "plate"
    return {"part_type": part_type, "views": views, "dim_views": dim_views}


def drawing_features(stem: str, ground_truth: dict) -> dict:
    """What one drawing contains, counted from its ground truth."""
    layout = drawing_layout(stem, ground_truth)
    kinds = [kind for _, kind in layout["views"]]
    frames = Counter(f["characteristic"] for f in ground_truth.get("fcfs", []))
    tables = [
        kind for kind in ("hole_table", "parts_list") if ground_truth.get(kind)
    ]
    return {
        "part_type": layout["part_type"],
        "views": len(kinds),
        "view_kinds": kinds,
        "sections": kinds.count("section"),
        "details": kinds.count("detail"),
        "gdt_frames": dict(sorted(frames.items())),
        "datums": len(ground_truth.get("datums", [])),
        "dimensions": len(ground_truth.get("dimensions", [])),
        "tables": tables,
        "balloons": len(ground_truth.get("balloons", [])),
        "notes": len(ground_truth.get("notes", [])),
    }


def score_explanation(stem: str, ground_truth: dict, explanation: Explanation) -> dict:
    """How well an explanation describes the sheet's layout. Pure: no model, no files.

    * Part type: right when it is one of :data:`PART_TYPE_MATCHES` for the drawing.
    * Views: the explanation's view names against the sheet's, as multisets (two sections
      need two "section" entries); extra names are views the sheet does not have.
    * Dimensions in the right view: of the dimensions with a known view, those whose note
      names that view. A dimension with no note, or a note with no view, counts as wrong.
    """
    layout = drawing_layout(stem, ground_truth)
    expected_type = layout["part_type"]
    accepted = PART_TYPE_MATCHES.get(expected_type or "", frozenset())
    got_type = explanation.part.type if explanation.part else None

    names = {name: VIEW_KIND_NAMES.get(kind, "other") for name, kind in layout["views"]}
    want = Counter(names.values())
    got = Counter(view.name for view in explanation.views)
    found = sum((want & got).values())

    notes = {note.id: note for note in explanation.dimensions}
    placed = layout["dim_views"]
    right = sum(
        1
        for dim_id, view in placed.items()
        if dim_id in notes and notes[dim_id].view == names.get(view)
    )
    return {
        "part_type": got_type,
        "part_type_expected": expected_type,
        "part_type_correct": got_type in accepted if accepted else None,
        "views_expected": sum(want.values()),
        "views_found": found,
        "views_extra": sum(got.values()) - found,
        "view_names": [view.name for view in explanation.views],
        "dims_with_view": len(placed),
        "dims_view_correct": right,
        "projection": explanation.projection,
        "tables": [table.kind for table in explanation.tables],
        "parse_issues": list(explanation.parse_issues),
    }


# --- Running ----------------------------------------------------------------------------

ClientFactory = Callable[[str], SupportsChatJSON]


def _real_guard(provider: str) -> SupportsChatJSON:
    """The same guarded client (time limit + Baseten→Meta fallback) the server uses."""
    from draftcheck.api import build_client, fallback_policy, guard_for, model_timeout_s

    return guard_for(
        build_client(provider), provider, build_client, model_timeout_s(), fallback_policy()
    )


def run_one(
    stem: str,
    provider: str,
    *,
    samples_dir: Path = SAMPLES_DIR,
    client_for: ClientFactory = _real_guard,
) -> dict:
    """Run one drawing through extract → rules → fix with one provider, and score it."""
    from draftcheck.api import render_settings

    ground_truth = load_ground_truth(stem, samples_dir)
    dpi, max_side = render_settings()
    png = load_pages(samples_dir / f"{stem}.pdf", dpi=dpi, max_side=max_side, max_pages=1)[0]

    client = client_for(provider)
    started = time.monotonic()
    extraction = extract_page(png.png_bytes, client)
    extracted = time.monotonic()
    findings = run_rules(extraction.drawing)
    report = correct(extraction.drawing, png.png_bytes, client)
    fixed = time.monotonic()

    return {
        "status": "ok",
        **score_run(ground_truth, extraction, findings, report),
        "latency_s": {
            "extract": round(extracted - started, 2),
            "fix": round(fixed - extracted, 2),
            "total": round(fixed - started, 2),
        },
    }


def run_explain_one(
    stem: str,
    provider: str,
    *,
    samples_dir: Path = SAMPLES_DIR,
    client_for: ClientFactory = _real_guard,
) -> dict:
    """Explain one drawing's ground truth with one provider, and score the explanation."""
    from draftcheck.api import render_settings

    ground_truth = load_ground_truth(stem, samples_dir)
    truth = Drawing.model_validate(ground_truth)
    dpi, max_side = render_settings()
    png = load_pages(samples_dir / f"{stem}.pdf", dpi=dpi, max_side=max_side, max_pages=1)[0]

    started = time.monotonic()
    explanation, result = explain_drawing(
        truth, run_rules(truth), png.png_bytes, client_for(provider)
    )
    return {
        "status": "ok",
        **score_explanation(stem, ground_truth, explanation),
        "model": result.model,
        "fell_back": bool(getattr(result, "fell_back", False)),
        "latency_s": {"total": round(time.monotonic() - started, 2)},
    }


def _unreachable(exc: BaseException) -> str | None:
    """Why a provider could not be used at all, or ``None`` for a real failure."""
    from draftcheck.api import outage_reason

    if isinstance(exc, ConfigError):
        return f"not configured: {exc}"
    reason = outage_reason(exc)
    return f"unreachable ({reason})" if reason else None


def run_key(stem: str, provider: str) -> str:
    return f"{stem}|{provider}"


def load_results(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"runs": {}}


def save_results(results: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _attempt(
    job: Callable[[str, str], dict], stem: str, provider: str, unreachable: dict[str, str]
) -> dict:
    """Run one scored job; a provider that cannot be reached is remembered and skipped."""
    started = time.monotonic()
    try:
        return job(stem, provider)
    except Exception as exc:  # noqa: BLE001 - recorded, and the eval moves on
        reason = _unreachable(exc)
        if reason is not None:
            unreachable[provider] = reason
            return {"status": "skipped", "reason": reason}
        return {
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}"[:500],
            "latency_s": {"total": round(time.monotonic() - started, 2)},
        }


def run_eval(
    drawings: Iterable[str],
    providers: Iterable[str],
    *,
    results_path: Path = RESULTS_PATH,
    samples_dir: Path = SAMPLES_DIR,
    client_for: ClientFactory = _real_guard,
    force: bool = False,
    explain: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """Run every (drawing, provider) pair one at a time, skipping cached ones.

    With ``explain``, each pair's explanation is run and scored too, cached separately
    under ``results["explain"]``. ``results["drawing_features"]`` is refreshed for every
    drawing in ``drawings`` from its ground truth.
    """
    results = load_results(results_path)
    runs: dict = results.setdefault("runs", {})
    drawings, providers = list(drawings), list(providers)
    unreachable: dict[str, str] = {}

    refresh_features(results, drawings, samples_dir)
    save_results(results, results_path)

    jobs: list[tuple[str, dict, int, Callable[[str, str], dict]]] = [
        ("", runs, SCORE_VERSION,
         lambda stem, provider: run_one(stem, provider, samples_dir=samples_dir,
                                        client_for=client_for)),
    ]
    if explain:
        jobs.append(
            ("explain ", results.setdefault("explain", {}), EXPLAIN_SCORE_VERSION,
             lambda stem, provider: run_explain_one(stem, provider, samples_dir=samples_dir,
                                                    client_for=client_for)),
        )

    for stem in drawings:
        for provider in providers:
            key = run_key(stem, provider)
            for label, store, version, job in jobs:
                cached = store.get(key)
                if not force and is_cached(cached, version):
                    log(f"{label}{key}: cached ({cached['status']})")
                    continue
                if provider in unreachable:
                    # Already failed to connect this session; do not wait on it again.
                    store[key] = {"status": "skipped", "reason": unreachable[provider]}
                    log(f"{label}{key}: skipped ({unreachable[provider]})")
                    save_results(results, results_path)
                    continue

                log(f"{label}{key}: running...")
                entry = _attempt(job, stem, provider, unreachable)
                entry["score_version"] = version
                entry["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
                store[key] = entry
                save_results(results, results_path)
                reason = f" ({entry['reason']})" if "reason" in entry else ""
                log(f"{label}{key}: {entry['status']}{reason}")

    return results


# --- Summary ----------------------------------------------------------------------------


def _frac(num: int, den: int) -> str:
    return f"{num}/{den}"


def _fixes(verified: int, errors_before: int) -> str:
    """Fixes verified out of error findings, or "n/a" when there were none to fix."""
    return _frac(verified, errors_before) if errors_before else "n/a"


def _row(stem: str, provider: str, run: dict | None) -> str:
    if run is None:
        return f"| {stem} | {provider} | not run |" + " |" * 10
    if run["status"] == "ok" and run.get("score_version") != SCORE_VERSION:
        return f"| {stem} | {provider} | stale (rerun) |" + " |" * 10
    if run["status"] != "ok":
        return f"| {stem} | {provider} | {run['status']} |" + " |" * 10
    ex, er, fx = run["extraction"], run["errors"], run["fixes"]
    alarms = str(len(er["false_alarms"]))
    if er["false_alarms"]:
        alarms += " (" + ", ".join(a.split("@")[0] for a in er["false_alarms"]) + ")"
    return (
        f"| {stem} | {provider} | ok "
        f"| {_frac(ex['dims_found'], ex['dims_expected'])} "
        f"| {_frac(ex['dims_correct'], ex['dims_expected'])} "
        f"| {_frac(ex['fcfs_correct'], ex['fcfs_expected'])} "
        f"| {_frac(ex['datums_found'], ex['datums_expected'])} "
        f"| {_frac(er['errors_caught'], er['errors_expected'])} "
        f"| {alarms} "
        f"| {_fixes(fx['fixes_verified'], fx['errors_before'])} "
        f"| {fx['rounds']} "
        f"| {run['latency_s']['total']:.1f} "
        f"| {'yes' if run['fell_back'] else 'no'} |"
    )


def totals(runs: dict, drawings: list[str], provider: str) -> dict:
    """Sums over every finished run of one provider."""
    done = [runs[k] for s in drawings if (k := run_key(s, provider)) in runs]
    ok = [r for r in done if r["status"] == "ok" and r.get("score_version") == SCORE_VERSION]

    def add(section: str, field: str) -> int:
        return sum(r[section][field] for r in ok)

    return {
        "ok": len(ok),
        "skipped": sum(1 for r in done if r["status"] == "skipped"),
        "failed": sum(1 for r in done if r["status"] == "failed"),
        "dims_found": add("extraction", "dims_found"),
        "dims_correct": add("extraction", "dims_correct"),
        "dims_expected": add("extraction", "dims_expected"),
        "fcfs_correct": add("extraction", "fcfs_correct"),
        "fcfs_expected": add("extraction", "fcfs_expected"),
        "datums_found": add("extraction", "datums_found"),
        "datums_expected": add("extraction", "datums_expected"),
        "errors_caught": add("errors", "errors_caught"),
        "errors_expected": add("errors", "errors_expected"),
        "false_alarms": sum(len(r["errors"]["false_alarms"]) for r in ok),
        "fixes_verified": add("fixes", "fixes_verified"),
        "errors_before": add("fixes", "errors_before"),
        "rounds": add("fixes", "rounds"),
        "latency_s": round(sum(r["latency_s"]["total"] for r in ok), 1),
        "fallbacks": sum(1 for r in ok if r["fell_back"]),
    }


HEADER = (
    "| Drawing | Provider | Status | Dims found | Dims correct | FCFs correct | Datums "
    "| Errors caught | False alarms | Fixes verified | Rounds | Latency (s) | Fallback |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
)


def summary_markdown(results: dict, drawings: list[str], providers: list[str]) -> str:
    """The ``docs/EVAL.md`` page: one row per run, then totals per provider."""
    runs = results.get("runs", {})
    lines = [
        "# DraftCheck — evaluation results",
        "",
        "Generated by `python -m scripts.run_eval` from `outputs/eval/results.json`. "
        "Do not edit by hand.",
        "",
        "Each drawing in `samples/generated/` runs through the real pipeline — extract, "
        "rules, then the correction loop — once per provider. Scores are computed in "
        "Python against the drawing's ground truth:",
        "",
        "- **Dims found**: ground-truth dimensions matched by value. **Dims correct**: "
        "of those, value, tolerances and basic flag all right.",
        "- **FCFs correct**: characteristic, tolerance, zone, modifier and datum letters "
        "all right. **Datums**: labels found.",
        "- **Errors caught**: seeded errors a finding of the same rule caught. **False "
        "alarms**: findings that match no seeded error.",
        "- **Fixes verified**: error-severity findings the correction loop cleared, as "
        "decided by re-running the rules. Warnings (MFG-001) do not start the loop and are "
        "not counted; a drawing with no error findings shows n/a.",
        "- **Fallback**: Baseten did not answer in time and Meta answered instead, for "
        "at least one call.",
        "",
        "## Per drawing",
        "",
        HEADER,
    ]
    for stem in drawings:
        for provider in providers:
            lines.append(_row(stem, provider, runs.get(run_key(stem, provider))))

    lines += [
        "",
        "## Totals",
        "",
        "| Provider | Runs ok | Skipped | Failed | Dims found | Dims correct | FCFs correct "
        "| Datums | Errors caught | False alarms | Fixes verified | Rounds | Latency (s) "
        "| Fallbacks |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for provider in providers:
        t = totals(runs, drawings, provider)
        lines.append(
            f"| {provider} | {t['ok']} | {t['skipped']} | {t['failed']} "
            f"| {_frac(t['dims_found'], t['dims_expected'])} "
            f"| {_frac(t['dims_correct'], t['dims_expected'])} "
            f"| {_frac(t['fcfs_correct'], t['fcfs_expected'])} "
            f"| {_frac(t['datums_found'], t['datums_expected'])} "
            f"| {_frac(t['errors_caught'], t['errors_expected'])} "
            f"| {t['false_alarms']} "
            f"| {_fixes(t['fixes_verified'], t['errors_before'])} "
            f"| {t['rounds']} | {t['latency_s']} | {t['fallbacks']} |"
        )

    lines += _features_section(results.get("drawing_features", {}), drawings)
    lines += _explain_section(results.get("explain", {}), drawings, providers)

    skipped = [
        f"- `{key}`: {run.get('reason', '')}"
        for key, run in runs.items()
        if run.get("status") in {"skipped", "failed"}
    ]
    skipped += [
        f"- `{key}` (explanation): {run.get('reason', '')}"
        for key, run in results.get("explain", {}).items()
        if run.get("status") in {"skipped", "failed"}
    ]
    if skipped:
        lines += ["", "## Skipped and failed runs", "", *skipped]
    return "\n".join(lines) + "\n"


def _frames_text(frames: dict[str, int]) -> str:
    """{"flatness": 1, "position": 2} -> "position ×2, flatness"."""
    if not frames:
        return "—"
    ordered = sorted(frames.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(name if n == 1 else f"{name} ×{n}" for name, n in ordered)


def _features_section(features: dict, drawings: list[str]) -> list[str]:
    """The "Drawing features" table: what each drawing contains, from its ground truth."""
    lines = [
        "",
        "## Drawing features",
        "",
        "Counted from each drawing's ground truth, so they describe the sheet, not a model's "
        "reading of it.",
        "",
        "| Drawing | Part | Views | Sections | Details | GD&T frames | Datums | Dimensions "
        "| Tables | Balloons | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for stem in drawings:
        f = features.get(stem)
        if f is None:
            lines.append(f"| `{stem}` | not counted |" + " |" * 9)
            continue
        views = f"{f['views']} ({', '.join(f['view_kinds'])})"
        lines.append(
            f"| `{stem}` | {f['part_type']} | {views} | {f['sections']} | {f['details']} "
            f"| {_frames_text(f['gdt_frames'])} | {f['datums']} | {f['dimensions']} "
            f"| {', '.join(f['tables']) or '—'} | {f['balloons']} | {f['notes']} |"
        )
    return lines


#: Printed at the top of EVAL.md's explanation section.
EXPLAIN_GROUND_TRUTH_NOTE = (
    "Explanations are scored on the ground-truth drawing, so reading errors don't affect them."
)


def _explain_section(explains: dict, drawings: list[str], providers: list[str]) -> list[str]:
    """The "Explanations" table, when ``--explain`` has been run."""
    lines = ["", "## Explanations", "", EXPLAIN_GROUND_TRUTH_NOTE, ""]
    if not explains:
        return [*lines, "Not run. Use `python -m scripts.run_eval --explain`."]
    lines += [
        "The \"About this drawing\" explanation of each drawing's ground truth, scored in "
        "Python: part type right, view names found against the sheet's views (extra names in "
        "brackets), and dimensions placed in the right view.",
        "",
        "| Drawing | Provider | Status | Part type | Views found | Dims in right view "
        "| Projection | Latency (s) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    totals_by: dict[str, Counter] = {p: Counter() for p in providers}
    for stem in drawings:
        for provider in providers:
            run = explains.get(run_key(stem, provider))
            if run is None:
                lines.append(f"| `{stem}` | {provider} | not run |" + " |" * 5)
                continue
            if run["status"] == "ok" and run.get("score_version") != EXPLAIN_SCORE_VERSION:
                lines.append(f"| `{stem}` | {provider} | stale (rerun) |" + " |" * 5)
                continue
            if run["status"] != "ok":
                lines.append(f"| `{stem}` | {provider} | {run['status']} |" + " |" * 5)
                continue
            correct = run["part_type_correct"]
            mark = "n/a" if correct is None else ("yes" if correct else "no")
            views = _frac(run["views_found"], run["views_expected"])
            if run["views_extra"]:
                views += f" (+{run['views_extra']})"
            lines.append(
                f"| `{stem}` | {provider} | ok | {run['part_type']} ({mark}) | {views} "
                f"| {_frac(run['dims_view_correct'], run['dims_with_view'])} "
                f"| {run['projection']} | {run['latency_s']['total']:.1f} |"
            )
            t = totals_by[provider]
            t["ok"] += 1
            t["type_right"] += bool(correct)
            t["type_scored"] += correct is not None
            for field in ("views_found", "views_expected", "dims_view_correct",
                          "dims_with_view"):
                t[field] += run[field]
    lines += [
        "",
        "| Provider | Runs ok | Part type right | Views found | Dims in right view |",
        "| --- | --- | --- | --- | --- |",
    ]
    for provider in providers:
        t = totals_by[provider]
        lines.append(
            f"| {provider} | {t['ok']} | {_frac(t['type_right'], t['type_scored'])} "
            f"| {_frac(t['views_found'], t['views_expected'])} "
            f"| {_frac(t['dims_view_correct'], t['dims_with_view'])} |"
        )
    return lines


def refresh_features(
    results: dict, drawings: Iterable[str], samples_dir: Path = SAMPLES_DIR
) -> None:
    """Recount ``results["drawing_features"]`` for ``drawings`` from their ground truth."""
    features = results.setdefault("drawing_features", {})
    for stem in drawings:
        features[stem] = drawing_features(stem, load_ground_truth(stem, samples_dir))


# --- Public snapshot for the web app ---------------------------------------------------

#: Bumped when the snapshot's shape changes; the page reads it.
SNAPSHOT_SCHEMA = 1

#: The one-line method note the page shows at the top.
METHOD_NOTE = (
    "Generated drawings with known answers; scores computed by Python against ground "
    "truth; see docs/EVAL.md."
)

#: How each provider is named on the page.
PROVIDER_LABELS = {
    "baseten": "Muse Spark Open (Baseten)",
    "meta": "Muse Spark (Meta Model API)",
}

#: Longest side, in pixels, of a gallery thumbnail and of the detail panel's image.
THUMB_SIDE = 360
SHEET_SIDE = 1400


def _status(entry: dict | None, version: int) -> str:
    """"ok", "failed", "skipped", "stale" (scored under an older version), or "not run"."""
    if entry is None:
        return "not run"
    if entry.get("status") == "ok" and entry.get("score_version") != version:
        return "stale"
    return str(entry.get("status", "not run"))


def _rule_ids(keys: list[str]) -> list[str]:
    """Finding keys reduced to their rule ids: item ids are the model's own names."""
    return [key.split("@")[0] for key in keys]


def _public_run(entry: dict | None) -> dict:
    """One extraction run, copied field by field: nothing the model wrote, no reasons."""
    status = _status(entry, SCORE_VERSION)
    if status != "ok" or entry is None:
        return {"status": status}
    ex, er, fx = entry["extraction"], entry["errors"], entry["fixes"]
    extraction_fields = (
        "dims_expected", "dims_found", "dims_correct", "fcfs_expected", "fcfs_found",
        "fcfs_correct", "datums_expected", "datums_found", "units_correct",
        "general_tolerance_correct",
    )
    return {
        "status": "ok",
        "model": str(entry.get("model", "")),
        "fell_back": bool(entry.get("fell_back")),
        "latency_s": float(entry.get("latency_s", {}).get("total", 0.0)),
        "extraction": {field: ex[field] for field in extraction_fields if field in ex},
        "errors": {
            "errors_expected": er["errors_expected"],
            "errors_caught": er["errors_caught"],
            "caught": list(er["caught"]),
            "missed": list(er["missed"]),
            "false_alarms": _rule_ids(er["false_alarms"]),
        },
        "fixes": {
            "errors_before": fx["errors_before"],
            "fixes_verified": fx["fixes_verified"],
            "rounds": fx["rounds"],
        },
    }


def _public_explain(entry: dict | None) -> dict:
    """One explanation score, copied field by field (never the explanation's text)."""
    status = _status(entry, EXPLAIN_SCORE_VERSION)
    if status != "ok" or entry is None:
        return {"status": status}
    fields = (
        "part_type", "part_type_correct", "views_expected", "views_found", "views_extra",
        "dims_with_view", "dims_view_correct", "projection",
    )
    return {
        "status": "ok",
        **{field: entry.get(field) for field in fields},
        "latency_s": float(entry.get("latency_s", {}).get("total", 0.0)),
    }


def _caught_by(seeded: list[dict], runs: dict[str, dict]) -> list[dict]:
    """Each seeded error, and for each provider whether a finding of its rule caught it.

    ``None`` when that provider has no finished run. Two seeded errors of the same rule
    need two findings of it, exactly as :func:`score_findings` counts them.
    """
    left = {
        provider: Counter(run["errors"]["caught"]) if run["status"] == "ok" else None
        for provider, run in runs.items()
    }
    out = []
    for error in seeded:
        rule = error.get("rule_id") or LEGACY_ERROR_RULES.get(error.get("type", ""), "")
        caught: dict[str, bool | None] = {}
        for provider, counter in left.items():
            if counter is None:
                caught[provider] = None
            elif counter[rule] > 0:
                counter[rule] -= 1
                caught[provider] = True
            else:
                caught[provider] = False
        out.append({"rule_id": rule, "description": error.get("description", ""),
                    "caught_by": caught})
    return out


def build_snapshot(
    results: dict,
    drawings: list[str],
    providers: list[str],
    samples_dir: Path = SAMPLES_DIR,
) -> dict:
    """The public test-results snapshot. Pure apart from reading the ground truth."""
    runs = results.get("runs", {})
    explains = results.get("explain", {})
    entries = []
    for stem in drawings:
        truth = load_ground_truth(stem, samples_dir)
        features = drawing_features(stem, truth)
        public_runs = {p: _public_run(runs.get(run_key(stem, p))) for p in providers}
        entries.append({
            "stem": stem,
            "title": (truth.get("title_block") or {}).get("title"),
            "part_type": features.pop("part_type"),
            "features": features,
            "seeded_errors": _caught_by(truth.get("seeded_errors", []), public_runs),
            "thumb": f"eval/thumbs/{stem}.png",
            "sheet": f"eval/sheets/{stem}.png",
            "pdf": f"eval/pdfs/{stem}.pdf",
            "runs": public_runs,
            "explain": {p: _public_explain(explains.get(run_key(stem, p))) for p in providers},
        })

    model_totals = {}
    for provider in providers:
        t = totals(runs, drawings, provider)
        scored = [e["explain"][provider] for e in entries
                  if e["explain"][provider]["status"] == "ok"
                  and e["explain"][provider]["part_type_correct"] is not None]
        model_totals[provider] = {
            "label": PROVIDER_LABELS.get(provider, provider),
            "runs_ok": t["ok"],
            "errors_caught": t["errors_caught"],
            "errors_expected": t["errors_expected"],
            "false_alarms": t["false_alarms"],
            "fixes_verified": t["fixes_verified"],
            "errors_before": t["errors_before"],
            "fallbacks": t["fallbacks"],
            "latency_s": t["latency_s"],
            "part_types_recognized": sum(1 for e in scored if e["part_type_correct"]),
            "part_types_scored": len(scored),
        }

    return {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method": METHOD_NOTE,
        "providers": [{"id": p, "label": PROVIDER_LABELS.get(p, p)} for p in providers],
        "suite": {
            "drawings": len(entries),
            "part_types": len({e["part_type"] for e in entries}),
            "views": sum(e["features"]["views"] for e in entries),
            "gdt_frames": sum(sum(e["features"]["gdt_frames"].values()) for e in entries),
            "seeded_errors": sum(len(e["seeded_errors"]) for e in entries),
        },
        "totals": model_totals,
        "drawings": entries,
    }


def write_snapshot(
    results: dict,
    drawings: list[str],
    providers: list[str],
    out_dir: Path = SNAPSHOT_DIR,
    samples_dir: Path = SAMPLES_DIR,
) -> Path:
    """Write the snapshot JSON, each drawing's thumbnail and larger image, and its PDF."""
    snapshot = build_snapshot(results, drawings, providers, samples_dir)
    for folder in ("thumbs", "sheets", "pdfs"):
        (out_dir / folder).mkdir(parents=True, exist_ok=True)
    for stem in drawings:
        pdf = samples_dir / f"{stem}.pdf"
        for folder, side in (("thumbs", THUMB_SIDE), ("sheets", SHEET_SIDE)):
            page = load_pages(pdf, dpi=200, max_side=side, max_pages=1)[0]
            (out_dir / folder / f"{stem}.png").write_bytes(page.png_bytes)
        (out_dir / "pdfs" / f"{stem}.pdf").write_bytes(pdf.read_bytes())
    path = out_dir / "results.json"
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the DraftCheck evaluation suite.")
    parser.add_argument("--drawings", nargs="+", default=list(DRAWINGS), choices=DRAWINGS)
    parser.add_argument(
        "--providers", nargs="+", default=list(SUPPORTED_PROVIDERS), choices=SUPPORTED_PROVIDERS
    )
    parser.add_argument("--results", type=Path, default=RESULTS_PATH)
    parser.add_argument("--summary", type=Path, default=SUMMARY_PATH)
    parser.add_argument("--force", action="store_true", help="rerun cached results too")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="also run and score the 'About this drawing' explanation (one more call each)",
    )
    parser.add_argument(
        "--snapshot-only",
        action="store_true",
        help="only rebuild the web app's snapshot from the results file (no model calls; "
        "docs/EVAL.md is left alone)",
    )
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT_DIR)
    args = parser.parse_args(argv)

    if args.snapshot_only:
        results = load_results(args.results)
        refresh_features(results, DRAWINGS)
        path = write_snapshot(results, list(DRAWINGS), list(SUPPORTED_PROVIDERS), args.snapshot)
        print(f"snapshot: {path}")
        return 0

    results = run_eval(
        args.drawings, args.providers, results_path=args.results, force=args.force,
        explain=args.explain,
    )
    # The summary covers the whole suite, so every drawing's features are counted.
    refresh_features(results, DRAWINGS)
    save_results(results, args.results)
    # The summary always covers the whole suite, so a partial run does not hide old rows.
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        summary_markdown(results, list(DRAWINGS), list(SUPPORTED_PROVIDERS)), encoding="utf-8"
    )
    snapshot = write_snapshot(results, list(DRAWINGS), list(SUPPORTED_PROVIDERS), args.snapshot)
    print(f"results: {args.results}")
    print(f"summary: {args.summary}")
    print(f"snapshot: {snapshot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
