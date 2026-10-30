"""The correction loop: propose edits, apply the safe ones, re-run the rules, repeat.

One round is: show the model the drawing, the current data, the current findings, and
everything the earlier rounds tried; take back a list of edits; apply the ones
``edits.apply_edits`` allows; run the rule engine again on the result. Comparing the
findings before a round with the findings after is what tells us whether anything
improved — not the model's own account of its work, which is never read.

Three things follow from that, and they are the point of this module:

* **The model's claims are ignored.** Each edit says which findings it addresses. That
  text goes into the report for a person to read and is never used to mark anything
  resolved. A finding is resolved when the re-run stops reporting it, and at no other time.
* **New problems are counted too.** A round that clears one finding and creates two has
  made the drawing worse, and ``introduced`` says so. The next round is told about it.
* **The loop stops.** It ends when no error-severity finding is left, or after
  ``max_rounds``, whichever comes first. Whatever is still outstanding is reported
  unresolved rather than retried forever.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from draftcheck.edits import Edit, RejectedEdit, apply_edits
from draftcheck.pipeline import SupportsChatJSON
from draftcheck.prompts import CORRECTION_SYSTEM, correction_user
from draftcheck.rules import run_rules
from draftcheck.schema import Drawing, Finding, finding_key

#: How many times the model is asked before the loop gives up. A hard cap, not a target:
#: a correction that has not worked in three attempts is reported, not retried.
MAX_ROUNDS = 3


@dataclass(frozen=True)
class RoundRecord:
    """Everything that happened in one round, enough to reconstruct it afterwards."""

    number: int
    #: The edits exactly as the model proposed them, before any parsing.
    proposed: list[Any]
    applied: list[Edit]
    rejected: list[RejectedEdit]
    findings_before: list[Finding]
    findings_after: list[Finding]
    #: Finding keys that were present before this round and gone after it.
    resolved: list[str]
    #: Finding keys this round created. A round can clear one problem and cause another.
    introduced: list[str]
    #: Set when the round could not happen at all — the model call failed, or its reply
    #: was unusable. Earlier rounds keep their work; the loop stops here.
    error: str | None = None


@dataclass(frozen=True)
class FindingStatus:
    """What became of one of the findings the drawing started with."""

    key: str
    finding: Finding
    #: "resolved" or "unresolved", decided by re-running the rules. Never by the model.
    status: str


@dataclass(frozen=True)
class CorrectionReport:
    """The result of a whole correction run."""

    original_drawing: Drawing
    final_drawing: Drawing
    original_findings: list[Finding]
    final_findings: list[Finding]
    rounds: list[RoundRecord] = field(default_factory=list)
    statuses: list[FindingStatus] = field(default_factory=list)

    @property
    def resolved_count(self) -> int:
        return sum(1 for status in self.statuses if status.status == "resolved")

    @property
    def unresolved_count(self) -> int:
        return len(self.statuses) - self.resolved_count

    def to_dict(self) -> dict:
        """A JSON-ready view of the whole run, for ``corrections.json``."""
        return {
            "rounds": [
                {
                    "round": record.number,
                    "proposed": record.proposed,
                    "applied": [
                        {**edit.model_dump(mode="json"), "description": edit.describe()}
                        for edit in record.applied
                    ],
                    "rejected": [
                        {"edit": item.edit, "reason": item.reason} for item in record.rejected
                    ],
                    "findings_before": [finding_key(f) for f in record.findings_before],
                    "findings_after": [finding_key(f) for f in record.findings_after],
                    "resolved": record.resolved,
                    "introduced": record.introduced,
                    "error": record.error,
                }
                for record in self.rounds
            ],
            "status": [
                {
                    "finding": status.key,
                    "status": status.status,
                    "severity": status.finding.severity,
                    "message": status.finding.message,
                }
                for status in self.statuses
            ],
            "final_findings": [f.model_dump(mode="json") for f in self.final_findings],
            "final_drawing": self.final_drawing.model_dump(mode="json"),
        }


def _has_errors(findings: list[Finding]) -> bool:
    """True while something error-severity is still outstanding.

    Warnings do not keep the loop running. MFG-001's edge-distance threshold is shop
    practice rather than a standard, and spending rounds on a judgement call would push
    the model into changing a part's geometry to satisfy a number we chose ourselves.
    """
    return any(finding.severity == "error" for finding in findings)


def _history_entry(record: RoundRecord) -> dict[str, Any]:
    """Condense one round into what the next round's prompt needs to know."""
    return {
        "round": record.number,
        "applied": [
            {"edit": edit.describe(), "rationale": edit.rationale} for edit in record.applied
        ],
        "rejected": [
            {"edit": item.edit, "refused_because": item.reason} for item in record.rejected
        ],
        "findings_before": [finding_key(f) for f in record.findings_before],
        "findings_after": [finding_key(f) for f in record.findings_after],
        "resolved_by_this_round": record.resolved,
        "introduced_by_this_round": record.introduced,
    }


def correct(
    drawing: Drawing,
    image_png: bytes,
    client: SupportsChatJSON,
    max_rounds: int = MAX_ROUNDS,
) -> CorrectionReport:
    """Run the correction loop over ``drawing`` and report what was and was not fixed.

    Args:
        drawing: The checked drawing. Never modified — each round works on a new copy.
        image_png: The rendered page, so the model can look at the drawing rather than
            only at the data extracted from it.
        client: Anything with ``chat_json``; tests pass a fake with scripted replies.
        max_rounds: The hard cap on model calls.

    Returns:
        A :class:`CorrectionReport`: the final drawing, the final findings, one
        :class:`RoundRecord` per round, and a resolved/unresolved verdict for every
        finding the drawing started with.
    """
    original_findings = run_rules(drawing)
    current = drawing
    findings = original_findings
    rounds: list[RoundRecord] = []
    history: list[dict[str, Any]] = []

    for number in range(1, max_rounds + 1):
        if not _has_errors(findings):
            break

        drawing_json = json.dumps(current.model_dump(mode="json"), indent=2, ensure_ascii=False)
        try:
            reply = client.chat_json(
                CORRECTION_SYSTEM,
                correction_user(drawing_json, findings, history),
                [image_png],
            )
            proposed = reply.data.get("edits")
        except Exception as exc:  # noqa: BLE001 - see below
            # Deliberately broad. A provider timeout, a reply that is not JSON, a reply
            # shaped nothing like we asked for — none of those are bugs in DraftCheck, and
            # none should throw away the rounds that already worked. The failure is
            # recorded on the round and the loop stops there.
            rounds.append(
                RoundRecord(
                    number=number,
                    proposed=[],
                    applied=[],
                    rejected=[],
                    findings_before=findings,
                    findings_after=findings,
                    resolved=[],
                    introduced=[],
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            break

        new_drawing, applied, rejected = apply_edits(current, proposed)
        after = run_rules(new_drawing)

        before_keys = {finding_key(finding) for finding in findings}
        after_keys = {finding_key(finding) for finding in after}
        record = RoundRecord(
            number=number,
            proposed=proposed if isinstance(proposed, list) else [proposed],
            applied=applied,
            rejected=rejected,
            findings_before=findings,
            findings_after=after,
            resolved=sorted(before_keys - after_keys),
            introduced=sorted(after_keys - before_keys),
        )
        rounds.append(record)
        history.append(_history_entry(record))
        current, findings = new_drawing, after

    final_keys = {finding_key(finding) for finding in findings}
    statuses = [
        FindingStatus(
            key=finding_key(finding),
            finding=finding,
            status="unresolved" if finding_key(finding) in final_keys else "resolved",
        )
        for finding in original_findings
    ]

    return CorrectionReport(
        original_drawing=drawing,
        final_drawing=current,
        original_findings=original_findings,
        final_findings=findings,
        rounds=rounds,
        statuses=statuses,
    )
