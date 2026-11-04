"""Run the evaluation suite through the real pipeline, for each provider, and score it.

Run with:
    python -m scripts.run_eval                         # every drawing, both providers
    python -m scripts.run_eval --drawings plate_clean --providers baseten
    python -m scripts.run_eval --force                 # ignore the cache, run everything

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
from draftcheck.pipeline import ExtractionResult, SupportsChatJSON, extract_page
from draftcheck.render import load_pages
from draftcheck.rules import run_rules
from draftcheck.schema import Dimension, Drawing, Finding, finding_key
from scripts.drawing_suite import SUITE

SAMPLES_DIR = Path("samples/generated")
RESULTS_PATH = Path("outputs/eval/results.json")
SUMMARY_PATH = Path("docs/EVAL.md")

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


def is_cached(entry: dict | None) -> bool:
    """True when ``entry`` is a finished result under the current scoring."""
    return (
        entry is not None
        and entry.get("status") in CACHED_STATUSES
        and entry.get("score_version") == SCORE_VERSION
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


def run_eval(
    drawings: Iterable[str],
    providers: Iterable[str],
    *,
    results_path: Path = RESULTS_PATH,
    samples_dir: Path = SAMPLES_DIR,
    client_for: ClientFactory = _real_guard,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """Run every (drawing, provider) pair one at a time, skipping cached ones."""
    results = load_results(results_path)
    runs: dict = results.setdefault("runs", {})
    drawings, providers = list(drawings), list(providers)
    unreachable: dict[str, str] = {}

    for stem in drawings:
        for provider in providers:
            key = run_key(stem, provider)
            cached = runs.get(key)
            if not force and is_cached(cached):
                log(f"{key}: cached ({cached['status']})")
                continue
            if provider in unreachable:
                # Already failed to connect this session; do not wait on it eight more times.
                runs[key] = {"status": "skipped", "reason": unreachable[provider]}
                log(f"{key}: skipped ({unreachable[provider]})")
                save_results(results, results_path)
                continue

            log(f"{key}: running...")
            started = time.monotonic()
            try:
                entry = run_one(stem, provider, samples_dir=samples_dir, client_for=client_for)
            except Exception as exc:  # noqa: BLE001 - recorded, and the eval moves on
                reason = _unreachable(exc)
                if reason is not None:
                    unreachable[provider] = reason
                    entry = {"status": "skipped", "reason": reason}
                else:
                    entry = {
                        "status": "failed",
                        "reason": f"{type(exc).__name__}: {exc}"[:500],
                        "latency_s": {"total": round(time.monotonic() - started, 2)},
                    }
            entry["score_version"] = SCORE_VERSION
            entry["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            runs[key] = entry
            save_results(results, results_path)
            reason = f" ({entry['reason']})" if "reason" in entry else ""
            log(f"{key}: {entry['status']}{reason}")

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

    skipped = [
        f"- `{key}`: {run.get('reason', '')}"
        for key, run in runs.items()
        if run.get("status") in {"skipped", "failed"}
    ]
    if skipped:
        lines += ["", "## Skipped and failed runs", "", *skipped]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the DraftCheck evaluation suite.")
    parser.add_argument("--drawings", nargs="+", default=list(DRAWINGS), choices=DRAWINGS)
    parser.add_argument(
        "--providers", nargs="+", default=list(SUPPORTED_PROVIDERS), choices=SUPPORTED_PROVIDERS
    )
    parser.add_argument("--results", type=Path, default=RESULTS_PATH)
    parser.add_argument("--summary", type=Path, default=SUMMARY_PATH)
    parser.add_argument("--force", action="store_true", help="rerun cached results too")
    args = parser.parse_args(argv)

    results = run_eval(args.drawings, args.providers, results_path=args.results, force=args.force)
    # The summary always covers the whole suite, so a partial run does not hide old rows.
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        summary_markdown(results, list(DRAWINGS), list(SUPPORTED_PROVIDERS)), encoding="utf-8"
    )
    print(f"results: {args.results}")
    print(f"summary: {args.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
