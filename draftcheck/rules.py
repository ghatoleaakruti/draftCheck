"""Deterministic rule checks over an extracted drawing.

Every rule is plain Python: given a :class:`~draftcheck.schema.Drawing`, it returns zero or
more :class:`~draftcheck.schema.Finding` objects. No model is involved in deciding whether a
rule passes, so the same drawing always produces the same findings.

Rule wording is our own. Each rule's ``source`` names the standard and the topic it comes
from; no standards text is reproduced here, and no section numbers are claimed.
"""

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from draftcheck.schema import (
    FORM_CONTROLS,
    BBox,
    Characteristic,
    DatumFeature,
    Dimension,
    Drawing,
    Finding,
)


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
    by_label: dict[str, list[DatumFeature]] = defaultdict(list)
    for datum in drawing.datums:
        by_label[datum.label].append(datum)

    findings: list[Finding] = []
    for label, datums in by_label.items():
        if len(datums) < 2:
            continue
        findings.append(
            _finding(
                GDT_002,
                f"Datum {label} is defined {len(datums)} times. Each datum label must "
                "identify one feature, so an inspector cannot tell which to measure from.",
                [datum.id or "" for datum in datums],
                _first_bbox([datum.bbox for datum in datums]),
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


# --- MFG-001: hole too close to edge --------------------------------------------------

#: How much solid material must be left between a hole's wall and the part edge, as a
#: multiple of the hole diameter. One diameter is a common shop rule of thumb for drilled
#: and punched holes in plate; thinner webs tend to bulge, tear, or break out. Tune this
#: for a specific process or material — it is a default, not a law.
MIN_EDGE_MATERIAL_RATIO = 1.0

#: The outer edges a dimension can run to, and which edge faces each one across the part.
_OPPOSITE_EDGE = {
    "left_edge": "right_edge",
    "right_edge": "left_edge",
    "bottom_edge": "top_edge",
    "top_edge": "bottom_edge",
}

#: The two edge pairs an overall size dimension can span.
_OVERALL_SPANS = (("left_edge", "right_edge"), ("bottom_edge", "top_edge"))

_HOLE_REF = re.compile(r"^hole_(\d+)_center$")


def _ref(value: str | None) -> str | None:
    """Normalize one ``from_ref``/``to_ref`` value for comparison."""
    return value.strip().lower() if isinstance(value, str) else None


def _hole_number(ref: str | None) -> int | None:
    """The hole number in a ``hole_N_center`` reference, or ``None`` for anything else."""
    match = _HOLE_REF.match(ref) if ref else None
    return int(match.group(1)) if match else None


def _hole_diameter(drawing: Drawing) -> float | None:
    """The hole diameter to check against, or ``None`` when the drawing gives none.

    With several diameter callouts we cannot yet tell which hole each belongs to, so we
    take the largest: the biggest hole leaves the least material, which is the
    conservative reading. Without a diameter there is nothing to compute, and the rule
    stays silent rather than guessing.
    """
    diameters = [
        dimension.value
        for dimension in drawing.dimensions
        if (dimension.feature_type or "").strip().lower() == "diameter"
    ]
    return max(diameters) if diameters else None


def _overall_size(drawing: Drawing, edge_a: str, edge_b: str) -> Dimension | None:
    """The dimension that spans the part from ``edge_a`` to ``edge_b``, if one exists."""
    for dimension in drawing.dimensions:
        if {_ref(dimension.from_ref), _ref(dimension.to_ref)} == {edge_a, edge_b}:
            return dimension
    return None


def _number(value: float) -> str:
    """Format a measurement without trailing zeros, e.g. 16.0 -> "16"."""
    return f"{value:g}"


def _check_hole_edge_distance(drawing: Drawing) -> list[Finding]:
    diameter = _hole_diameter(drawing)
    if diameter is None:
        return []

    minimum = MIN_EDGE_MATERIAL_RATIO * diameter
    radius = diameter / 2

    # Which overall size dimension, if any, spans the part across each edge.
    overall: dict[str, Dimension] = {}
    for edge_a, edge_b in _OVERALL_SPANS:
        span = _overall_size(drawing, edge_a, edge_b)
        if span is not None:
            overall[edge_a] = span
            overall[edge_b] = span

    # (hole number, edge) -> (material left, dimension ids, bbox). Measured distances are
    # collected separately from derived ones so a printed dimension always wins over a
    # value we worked out ourselves.
    measured: dict[tuple[int, str], tuple[float, list[str], BBox]] = {}
    derived: dict[tuple[int, str], tuple[float, list[str], BBox]] = {}

    for dimension in drawing.dimensions:
        refs = [_ref(dimension.from_ref), _ref(dimension.to_ref)]
        edges = [ref for ref in refs if ref in _OPPOSITE_EDGE]
        holes = [number for number in map(_hole_number, refs) if number is not None]
        if len(edges) != 1 or len(holes) != 1:
            continue
        edge, hole = edges[0], holes[0]
        measured[(hole, edge)] = (dimension.value - radius, [dimension.id or ""], dimension.bbox)

        # The drawing locates the hole from one edge only. If it also gives the overall
        # size across that direction, the distance to the facing edge is the remainder.
        span = overall.get(edge)
        if span is not None:
            derived[(hole, _OPPOSITE_EDGE[edge])] = (
                span.value - dimension.value - radius,
                [dimension.id or "", span.id or ""],
                dimension.bbox,
            )

    findings: list[Finding] = []
    for (hole, edge), (material, ids, bbox) in sorted({**derived, **measured}.items()):
        if material >= minimum:
            continue
        findings.append(
            _finding(
                MFG_001,
                f"Hole {hole} leaves {_number(material)} of material between its wall and "
                f"the {edge.replace('_', ' ')}, under the {_number(minimum)} minimum for a "
                f"{_number(diameter)} hole ({_number(MIN_EDGE_MATERIAL_RATIO)}x the "
                "diameter). A web this thin can tear out or bulge when the hole is made.",
                ids,
                bbox,
            )
        )
    return findings


MFG_001 = Rule(
    id="MFG-001",
    title="Hole too close to edge",
    category="manufacturability",
    severity="warning",
    source=(
        "General design-for-manufacturing practice for hole edge distance; the threshold "
        "is configurable via MIN_EDGE_MATERIAL_RATIO"
    ),
    check=_check_hole_edge_distance,
)


#: Every rule the engine runs, in registry order. ``run_rules`` sorts the output itself,
#: so the order here does not affect results.
RULES: list[Rule] = [GDT_001, GDT_002, GDT_003, GDT_004, DIM_001, MFG_001]


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
