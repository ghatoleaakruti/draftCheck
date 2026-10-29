"""Deterministic rule checks over an extracted drawing.

Every rule is plain Python: given a :class:`~draftcheck.schema.Drawing`, it returns zero or
more :class:`~draftcheck.schema.Finding` objects. No model is involved in deciding whether a
rule passes, so the same drawing always produces the same findings.

Rule wording is our own. Each rule's ``source`` names the standard and the topic it comes
from; no standards text is reproduced here, and no section numbers are claimed.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from draftcheck.schema import FORM_CONTROLS, BBox, Characteristic, Drawing, Finding


@dataclass(frozen=True)
class Rule:
    """One check: what it is called, how bad a violation is, and the function that runs it."""

    id: str
    title: str
    #: One of "gdt", "dimensioning", "manufacturability".
    category: str
    #: One of "error", "warning".
    severity: str
    #: Where the rule comes from — standard and topic, cited rather than quoted.
    source: str
    check: Callable[[Drawing], list[Finding]]


def _finding(rule: Rule, message: str, item_ids: list[str], bbox: BBox = None) -> Finding:
    """Build a finding that carries the rule's own id, severity, and citation."""
    return Finding(
        rule_id=rule.id,
        severity=rule.severity,
        message=message,
        source=rule.source,
        item_ids=item_ids,
        bbox=bbox,
    )


def _first_bbox(boxes: list[BBox]) -> BBox:
    """The first usable box in a list, or ``None`` if nothing was located."""
    for box in boxes:
        if box is not None:
            return box
    return None


# --- GDT-001: undefined datum ---------------------------------------------------------


def _check_undefined_datum(drawing: Drawing) -> list[Finding]:
    defined = {datum.label for datum in drawing.datums}
    findings: list[Finding] = []

    for fcf in drawing.fcfs:
        # Report each missing label separately, and only once per frame even if the frame
        # happens to name the same label twice.
        seen: set[str] = set()
        for ref in fcf.datum_refs:
            if ref.label in defined or ref.label in seen:
                continue
            seen.add(ref.label)
            findings.append(
                _finding(
                    GDT_001,
                    f"Feature control frame {fcf.id} references datum {ref.label}, but no "
                    f"datum feature symbol on the drawing defines {ref.label}. The tolerance "
                    "cannot be measured without it.",
                    [fcf.id or ""],
                    fcf.bbox,
                )
            )
    return findings


GDT_001 = Rule(
    id="GDT-001",
    title="Undefined datum",
    category="gdt",
    severity="error",
    source="ASME Y14.5-2018, datum reference frames",
    check=_check_undefined_datum,
)


# --- GDT-002: duplicate datum ---------------------------------------------------------


def _check_duplicate_datum(drawing: Drawing) -> list[Finding]:
    positions: dict[str, list[int]] = defaultdict(list)
    for index, datum in enumerate(drawing.datums):
        positions[datum.label].append(index)

    findings: list[Finding] = []
    for label, indices in positions.items():
        if len(indices) < 2:
            continue
        findings.append(
            _finding(
                GDT_002,
                f"Datum {label} is defined {len(indices)} times. Each datum label must "
                "identify one feature, so an inspector cannot tell which to measure from.",
                [f"datum[{index}]" for index in indices],
                _first_bbox([drawing.datums[index].bbox for index in indices]),
            )
        )
    return findings


GDT_002 = Rule(
    id="GDT-002",
    title="Duplicate datum",
    category="gdt",
    severity="error",
    source="ASME Y14.5-2018, datum feature identification",
    check=_check_duplicate_datum,
)


# --- GDT-003: form control with datum -------------------------------------------------


def _check_form_control_with_datum(drawing: Drawing) -> list[Finding]:
    findings: list[Finding] = []
    for fcf in drawing.fcfs:
        if fcf.characteristic not in FORM_CONTROLS or not fcf.datum_refs:
            continue
        labels = ", ".join(ref.label for ref in fcf.datum_refs)
        findings.append(
            _finding(
                GDT_003,
                f"Feature control frame {fcf.id} applies {fcf.characteristic.value} and also "
                f"references datum {labels}. A form control describes the feature's own shape "
                "and is measured against nothing else, so the datum reference has no meaning.",
                [fcf.id or ""],
                fcf.bbox,
            )
        )
    return findings


GDT_003 = Rule(
    id="GDT-003",
    title="Form control with datum",
    category="gdt",
    severity="error",
    source="ASME Y14.5-2018, form tolerances",
    check=_check_form_control_with_datum,
)


# --- GDT-004: position without basic dimensions ---------------------------------------


def _check_position_without_basic(drawing: Drawing) -> list[Finding]:
    # KNOWN LIMITATION: this is a drawing-level check, not a per-feature one. It only asks
    # whether the drawing has any basic dimensions at all. It cannot yet tell whether the
    # basic dimensions that exist actually locate the feature each position frame applies
    # to — that needs the from_ref/to_ref links, which extraction does not fill in reliably
    # today. So a drawing with basic dimensions somewhere will pass even if a particular
    # positioned hole is located by toleranced dimensions.
    positions = [fcf for fcf in drawing.fcfs if fcf.characteristic == Characteristic.POSITION]
    if not positions:
        return []
    if any(dimension.is_basic for dimension in drawing.dimensions):
        return []

    return [
        _finding(
            GDT_004,
            f"The drawing has {len(positions)} position tolerance(s) but no basic dimensions. "
            "A position tolerance measures deviation from a true location, and that location "
            "has to be established by basic dimensions.",
            [fcf.id or "" for fcf in positions],
            _first_bbox([fcf.bbox for fcf in positions]),
        )
    ]


GDT_004 = Rule(
    id="GDT-004",
    title="Position without basic dimensions",
    category="gdt",
    severity="error",
    source="ASME Y14.5-2018, position tolerancing and basic dimensions",
    check=_check_position_without_basic,
)


# --- DIM-001: missing units or general tolerance --------------------------------------


def _has_general_tolerance(drawing: Drawing) -> bool:
    """True when the title block gives a blanket linear tolerance to fall back on."""
    return (
        drawing.general_tolerance is not None
        and drawing.general_tolerance.linear_plus_minus is not None
    )


def _check_units_and_general_tolerance(drawing: Drawing) -> list[Finding]:
    findings: list[Finding] = []

    if drawing.units is None:
        findings.append(
            _finding(
                DIM_001,
                "The drawing does not state its units. Every dimension on it is ambiguous "
                "until the unit system is given.",
                [],
            )
        )

    if not _has_general_tolerance(drawing):
        # Basic dimensions carry no tolerance by definition, and reference dimensions are
        # not inspected, so neither needs a fallback.
        untoleranced = [
            dimension
            for dimension in drawing.dimensions
            if not dimension.is_basic
            and not dimension.is_reference
            and dimension.tol_plus is None
            and dimension.tol_minus is None
        ]
        if untoleranced:
            ids = [dimension.id or "" for dimension in untoleranced]
            findings.append(
                _finding(
                    DIM_001,
                    f"The drawing has no general tolerance, and {len(untoleranced)} "
                    f"dimension(s) carry no tolerance of their own ({', '.join(ids)}). Those "
                    "dimensions have no permitted range, so they cannot be inspected.",
                    ids,
                    _first_bbox([dimension.bbox for dimension in untoleranced]),
                )
            )

    return findings


DIM_001 = Rule(
    id="DIM-001",
    title="Missing units or general tolerance",
    category="dimensioning",
    severity="error",
    source="ASME Y14.5-2018, units of measure and general tolerances",
    check=_check_units_and_general_tolerance,
)


#: Every rule the engine runs, in registry order. ``run_rules`` sorts the output itself,
#: so the order here does not affect results.
RULES: list[Rule] = [GDT_001, GDT_002, GDT_003, GDT_004, DIM_001]


def run_rules(drawing: Drawing) -> list[Finding]:
    """Run every rule against ``drawing`` and return the findings in a stable order.

    Sorted by rule id, then by the ids of the items each finding points at, so the same
    drawing always yields the same list — which is what lets the correction loop compare
    one run against the next.
    """
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule.check(drawing))
    return sorted(findings, key=lambda finding: (finding.rule_id, finding.item_ids))
