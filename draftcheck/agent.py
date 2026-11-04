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
* **Edits that did not work are taken back.** An applied edit whose cited findings are
  not all gone on the re-run has failed, and is rolled back before the next round: the
  round's other edits are applied again without it and the rules re-run on that. So the
  drawing only ever carries edits the rule engine confirmed (or ones held for a
  designer), and a failed edit is reported, never kept.
* **The loop stops.** It ends when no error-severity finding is left, or after
  ``max_rounds``, whichever comes first. Whatever is still outstanding is reported
  unresolved rather than retried forever.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from draftcheck.edits import Edit, RejectedEdit, apply_edits
from draftcheck.llm_client import ModelCancelled, ModelTimeout
from draftcheck.pipeline import SupportsChatJSON
from draftcheck.prompts import CORRECTION_SYSTEM, correction_user
from draftcheck.rules import run_rules
from draftcheck.schema import Drawing, Finding, finding_key

#: How many times the model is asked before the loop gives up. A hard cap, not a target:
#: a correction that has not worked in three attempts is reported, not retried.
MAX_ROUNDS = 3


@dataclass(frozen=True)
class RolledBackEdit:
    """An edit that applied cleanly but did not clear what it cited, so it was undone."""

    edit: Edit
    #: Why, as a sentence: decided by the re-run of the rules, never by the model.
    reason: str


@dataclass(frozen=True)
class RoundRecord:
    """Everything that happened in one round, enough to reconstruct it afterwards."""

    number: int
    #: The edits exactly as the model proposed them, before any parsing.
    proposed: list[Any]
    #: The edits this round left on the drawing: each one cleared every finding it cites.
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
    #: True when that failure was the model call running out of time.
    timed_out: bool = False
    #: The model and provider that answered this round. ``None`` when nobody did.
    model: str | None = None
    provider: str | None = None
    #: True when the first provider failed and a backup answered this round instead.
    fell_back: bool = False
    fallback_reason: str | None = None
    #: Edits that applied but did not clear what they cite, undone before the next round.
    #: ``findings_after`` is the re-run *after* they were undone.
    rolled_back: list[RolledBackEdit] = field(default_factory=list)


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
                    "rolled_back": [
                        {
                            **item.edit.model_dump(mode="json"),
                            "description": item.edit.describe(),
                            "reason": item.reason,
                        }
                        for item in record.rolled_back
                    ],
                    "findings_before": [finding_key(f) for f in record.findings_before],
                    "findings_after": [finding_key(f) for f in record.findings_after],
                    "resolved": record.resolved,
                    "introduced": record.introduced,
                    "error": record.error,
                    "timed_out": record.timed_out,
                    "model": record.model,
                    "provider": record.provider,
                    "fell_back": record.fell_back,
                    "fallback_reason": record.fallback_reason,
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
        "rolled_back": [
            {
                "edit": item.edit.describe(),
                "rationale": item.edit.rationale,
                "rolled_back_because": item.reason,
            }
            for item in record.rolled_back
        ],
        "findings_before": [finding_key(f) for f in record.findings_before],
        "findings_after": [finding_key(f) for f in record.findings_after],
        "resolved_by_this_round": record.resolved,
        "introduced_by_this_round": record.introduced,
    }


#: Told a round is starting: its number and the findings it starts from.
OnRoundStart = Callable[[int, list[Finding]], None]
#: Told what the model proposed in a round, exactly as ``RoundRecord.proposed`` keeps it,
#: and who answered: the ``model``, ``provider``, ``fell_back`` and ``fallback_reason`` the
#: round's record will carry.
OnProposal = Callable[[int, list[Any], dict[str, Any]], None]
#: Told a round is over, with the same record that goes into the report.
OnRoundEnd = Callable[[RoundRecord], None]


def failure_reason(edit: Edit, resolved: set[str]) -> str | None:
    """Why an applied edit has failed, or ``None`` if it cleared everything it cites.

    The citation (``finding_ids``) is the model's claim and only says which findings to
    look at; whether each one cleared is ``resolved``, from re-running the rules.
    """
    cited = list(edit.finding_ids)
    if not cited:
        return "It cites no finding, so the re-run has nothing to confirm."
    still = [key for key in cited if key not in resolved]
    if still:
        return f"The re-run still reports {', '.join(still)}."
    return None


def _keep_what_worked(
    start: Drawing, before_keys: set[str], drawing: Drawing, applied: list[Edit]
) -> tuple[Drawing, list[Finding], list[Edit], list[RolledBackEdit]]:
    """Roll back the round's failed edits: ``(drawing, findings, kept, rolled_back)``.

    The kept edits are applied again from ``start`` and the rules re-run. Taking an edit
    out can change what the others achieve — one may only have cleared its finding with
    the failed one's help, or no longer apply at all — so this repeats until every kept
    edit stands on its own. Each pass keeps strictly fewer edits, so it ends.
    """
    kept = list(applied)
    rolled_back: list[RolledBackEdit] = []
    findings = run_rules(drawing)
    while True:
        resolved = before_keys - {finding_key(finding) for finding in findings}
        failing = [(edit, failure_reason(edit, resolved)) for edit in kept]
        failing = [(edit, reason) for edit, reason in failing if reason is not None]
        if not failing:
            return drawing, findings, kept, rolled_back
        rolled_back.extend(RolledBackEdit(edit, reason) for edit, reason in failing)
        dropped = {id(edit) for edit, _ in failing}
        kept = [edit for edit in kept if id(edit) not in dropped]
        drawing, reapplied, refused = apply_edits(start, kept)
        for item in refused:
            rolled_back.append(
                RolledBackEdit(
                    item.edit,
                    f"Without the rolled-back edits it no longer applies: {item.reason}",
                )
            )
        kept = reapplied
        findings = run_rules(drawing)


def correct(
    drawing: Drawing,
    image_png: bytes,
    client: SupportsChatJSON,
    max_rounds: int = MAX_ROUNDS,
    *,
    on_round_start: OnRoundStart | None = None,
    on_proposal: OnProposal | None = None,
    on_round_end: OnRoundEnd | None = None,
) -> CorrectionReport:
    """Run the correction loop over ``drawing`` and report what was and was not fixed.

    Args:
        drawing: The checked drawing. Never modified — each round works on a new copy.
        image_png: The rendered page, so the model can look at the drawing rather than
            only at the data extracted from it.
        client: Anything with ``chat_json``; tests pass a fake with scripted replies.
        max_rounds: The hard cap on model calls.
        on_round_start, on_proposal, on_round_end: Optional callbacks, so a caller can
            show a run while it happens (the web page's live rounds). Each is given data
            the final report also holds — nothing more — and none of them can change what
            the loop does. Left out, the loop runs exactly as it always has.

    Returns:
        A :class:`CorrectionReport`: the final drawing, the final findings, one
        :class:`RoundRecord` per round, and a resolved/unresolved verdict for every
        finding the drawing started with.

    Raises:
        ModelCancelled: If the client was aborted. The loop stops at that round.
    """
    original_findings = run_rules(drawing)
    current = drawing
    findings = original_findings
    rounds: list[RoundRecord] = []
    history: list[dict[str, Any]] = []

    for number in range(1, max_rounds + 1):
        if not _has_errors(findings):
            break

        if on_round_start is not None:
            on_round_start(number, findings)

        drawing_json = json.dumps(current.model_dump(mode="json"), indent=2, ensure_ascii=False)
        try:
            reply = client.chat_json(
                CORRECTION_SYSTEM,
                correction_user(drawing_json, findings, history),
                [image_png],
            )
            proposed = reply.data.get("edits")
        except ModelCancelled:
            # Nobody is waiting for this report any more, so there is nothing to record:
            # the caller gets the exception and saves nothing.
            raise
        except Exception as exc:  # noqa: BLE001 - see below
            # Deliberately broad. A provider timeout, a reply that is not JSON, a reply
            # shaped nothing like we asked for — none of those are bugs in DraftCheck, and
            # none should throw away the rounds that already worked. The failure is
            # recorded on the round and the loop stops there.
            timed_out = isinstance(exc, ModelTimeout)
            failed = RoundRecord(
                number=number,
                proposed=[],
                applied=[],
                rejected=[],
                findings_before=findings,
                findings_after=findings,
                resolved=[],
                introduced=[],
                # A timeout is said in its own plain sentence; anything else keeps
                # the exception's name, which is what someone debugging it needs.
                error=str(exc) if timed_out else f"{type(exc).__name__}: {exc}",
                timed_out=timed_out,
            )
            rounds.append(failed)
            if on_round_end is not None:
                on_round_end(failed)
            break

        proposed_list = proposed if isinstance(proposed, list) else [proposed]
        answered = {
            "model": getattr(reply, "model", None),
            "provider": getattr(reply, "provider", None),
            "fell_back": bool(getattr(reply, "fell_back", False)),
            "fallback_reason": getattr(reply, "fallback_reason", None),
        }
        if on_proposal is not None:
            on_proposal(number, proposed_list, dict(answered))

        new_drawing, applied, rejected = apply_edits(current, proposed)
        before_keys = {finding_key(finding) for finding in findings}
        new_drawing, after, kept, rolled_back = _keep_what_worked(
            current, before_keys, new_drawing, applied
        )

        after_keys = {finding_key(finding) for finding in after}
        record = RoundRecord(
            number=number,
            proposed=proposed_list,
            applied=kept,
            rejected=rejected,
            rolled_back=rolled_back,
            findings_before=findings,
            findings_after=after,
            resolved=sorted(before_keys - after_keys),
            introduced=sorted(after_keys - before_keys),
            **answered,
        )
        rounds.append(record)
        if on_round_end is not None:
            on_round_end(record)
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
