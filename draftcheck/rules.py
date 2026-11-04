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
    RuleResult,
    RuleStatus,
    finding_key,
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
    #: Words the rule's verdict on the whole drawing. Called with ``failed=True`` when
    #: ``check`` produced findings, and then must return ``"fail"``; otherwise returns
    #: ``"pass"`` or ``"not_applicable"``. The status is never decided here on its own:
    #: failing is ``check``'s call, and this only says what was looked at.
    describe: Callable[[Drawing, bool], tuple[RuleStatus, str]]


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


def _join(words: list[str]) -> str:
    """``["A"]`` -> ``"A"``, ``["A", "B", "C"]`` -> ``"A, B, C"``."""
    return ", ".join(words)


def _times(count: int, word: str) -> str:
    """``(1, "frame")`` -> ``"1 frame"``, ``(2, "frame")`` -> ``"2 frames"``."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


NO_FRAMES = "No feature control frames on this drawing."


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


def _describe_undefined_datum(drawing: Drawing, failed: bool) -> tuple[RuleStatus, str]:
    if not drawing.fcfs:
        return "not_applicable", NO_FRAMES
    referenced = sorted({ref.label for fcf in drawing.fcfs for ref in fcf.datum_refs})
    if not referenced:
        return "not_applicable", "No feature control frame references a datum."
    defined = {datum.label for datum in drawing.datums}
    missing = [label for label in referenced if label not in defined]
    if failed:
        return "fail", (
            f"The frames reference {_join(referenced)}, but nothing defines "
            f"{_join(missing)}."
        )
    return "pass", f"Every datum the frames reference is defined: {_join(referenced)}."


GDT_001 = Rule(
    id="GDT-001",
    title="Undefined datum",
    category="gdt",
    severity="error",
    source="ASME Y14.5-2018, datum reference frames",
    check=_check_undefined_datum,
    describe=_describe_undefined_datum,
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


def _describe_duplicate_datum(drawing: Drawing, failed: bool) -> tuple[RuleStatus, str]:
    if not drawing.datums:
        return "not_applicable", "No datum feature symbols on this drawing."
    counts: dict[str, int] = defaultdict(int)
    for datum in drawing.datums:
        counts[datum.label] += 1
    labels = sorted(counts)
    if failed:
        repeated = [f"{label} ({_times(counts[label], 'time')})" for label in labels
                    if counts[label] > 1]
        return "fail", f"More than one symbol claims the same label: {_join(repeated)}."
    return "pass", f"Each datum label is used once: {_join(labels)}."


GDT_002 = Rule(
    id="GDT-002",
    title="Duplicate datum",
    category="gdt",
    severity="error",
    source="ASME Y14.5-2018, datum feature identification",
    check=_check_duplicate_datum,
    describe=_describe_duplicate_datum,
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


#: The form controls, named in a fixed order for the sentences below.
_FORM_NAMES = ", ".join(
    c.value
    for c in (
        Characteristic.FLATNESS,
        Characteristic.STRAIGHTNESS,
        Characteristic.CIRCULARITY,
        Characteristic.CYLINDRICITY,
    )
)


def _describe_form_control_with_datum(
    drawing: Drawing, failed: bool
) -> tuple[RuleStatus, str]:
    if not drawing.fcfs:
        return "not_applicable", NO_FRAMES
    forms = [fcf for fcf in drawing.fcfs if fcf.characteristic in FORM_CONTROLS]
    if failed:
        offenders = [
            f"{fcf.characteristic.value} to {_join([ref.label for ref in fcf.datum_refs])}"
            for fcf in forms
            if fcf.datum_refs
        ]
        return "fail", f"A form control references a datum: {_join(offenders)}."
    sentence = f"No form control ({_FORM_NAMES}) references a datum."
    if forms:
        checked = _join(sorted({fcf.characteristic.value for fcf in forms}))
        sentence = sentence[:-1] + f" (checked: {checked})."
    return "pass", sentence


GDT_003 = Rule(
    id="GDT-003",
    title="Form control with datum",
    category="gdt",
    severity="error",
    source="ASME Y14.5-2018, form tolerances",
    check=_check_form_control_with_datum,
    describe=_describe_form_control_with_datum,
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


def _describe_position_without_basic(
    drawing: Drawing, failed: bool
) -> tuple[RuleStatus, str]:
    positions = [fcf for fcf in drawing.fcfs if fcf.characteristic == Characteristic.POSITION]
    if not positions:
        return "not_applicable", "No position tolerances on this drawing."
    count = _times(len(positions), "position tolerance")
    if failed:
        return "fail", f"The drawing has {count} but no basic dimensions to locate them."
    basics = sum(1 for dimension in drawing.dimensions if dimension.is_basic)
    return "pass", f"The drawing has {count} and {_times(basics, 'basic dimension')}."


GDT_004 = Rule(
    id="GDT-004",
    title="Position without basic dimensions",
    category="gdt",
    severity="error",
    source="ASME Y14.5-2018, position tolerancing and basic dimensions",
    check=_check_position_without_basic,
    describe=_describe_position_without_basic,
)


# --- DIM-001: missing units or general tolerance --------------------------------------


def _has_general_tolerance(drawing: Drawing) -> bool:
    """True when the title block gives a blanket linear tolerance to fall back on."""
    return (
        drawing.general_tolerance is not None
        and drawing.general_tolerance.linear_plus_minus is not None
    )


def _inspected(drawing: Drawing) -> list[Dimension]:
    """Dimensions that need a permitted range.

    Basic dimensions carry no tolerance by definition, and reference dimensions are not
    inspected, so neither needs a fallback.
    """
    return [d for d in drawing.dimensions if not d.is_basic and not d.is_reference]


def _untoleranced(drawing: Drawing) -> list[Dimension]:
    """Inspected dimensions that print no tolerance of their own."""
    return [d for d in _inspected(drawing) if d.tol_plus is None and d.tol_minus is None]


def _describe_units_and_general_tolerance(
    drawing: Drawing, failed: bool
) -> tuple[RuleStatus, str]:
    general = drawing.general_tolerance
    if failed:
        problems = []
        if drawing.units is None:
            problems.append("no units are stated")
        if not _has_general_tolerance(drawing):
            untoleranced = _untoleranced(drawing)
            if untoleranced:
                problems.append(
                    f"there is no general tolerance and {_times(len(untoleranced), 'dimension')}"
                    f" carry none of their own ({_join([d.id or '' for d in untoleranced])})"
                )
        text = "; ".join(problems)
        return "fail", text[0].upper() + text[1:] + "."
    units = drawing.units.value if drawing.units else ""
    if general is not None and general.linear_plus_minus is not None:
        return "pass", (
            f"Units ({units}) and a general tolerance "
            f"(±{_number(general.linear_plus_minus)}) are both stated."
        )
    inspected = len(_inspected(drawing))
    return "pass", (
        f"Units ({units}) are stated; there is no general tolerance, but all "
        f"{_times(inspected, 'inspected dimension')} carry their own."
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
        untoleranced = _untoleranced(drawing)
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
    describe=_describe_units_and_general_tolerance,
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


#: (hole number, edge) -> (material left, dimension ids, bbox).
EdgeMaterial = dict[tuple[int, str], tuple[float, list[str], BBox]]


def _edge_material(drawing: Drawing, diameter: float) -> EdgeMaterial:
    """How much material each located hole leaves to each edge we can work out."""
    radius = diameter / 2

    # Which overall size dimension, if any, spans the part across each edge.
    overall: dict[str, Dimension] = {}
    for edge_a, edge_b in _OVERALL_SPANS:
        span = _overall_size(drawing, edge_a, edge_b)
        if span is not None:
            overall[edge_a] = span
            overall[edge_b] = span

    # Measured distances are collected separately from derived ones so a printed dimension
    # always wins over a value we worked out ourselves.
    measured: EdgeMaterial = {}
    derived: EdgeMaterial = {}

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

    return dict(sorted({**derived, **measured}.items()))


def _edge_words(edge: str) -> str:
    """``"left_edge"`` -> ``"left edge"``."""
    return edge.replace("_", " ")


def _length(drawing: Drawing, value: float) -> str:
    """A length with the drawing's unit when it has one, e.g. ``"16 mm"``."""
    return f"{_number(value)} {drawing.units.value}" if drawing.units else _number(value)


def _check_hole_edge_distance(drawing: Drawing) -> list[Finding]:
    diameter = _hole_diameter(drawing)
    if diameter is None:
        return []
    minimum = MIN_EDGE_MATERIAL_RATIO * diameter

    findings: list[Finding] = []
    for (hole, edge), (material, ids, bbox) in _edge_material(drawing, diameter).items():
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


def _describe_hole_edge_distance(drawing: Drawing, failed: bool) -> tuple[RuleStatus, str]:
    diameter = _hole_diameter(drawing)
    if diameter is None:
        return "not_applicable", "No hole diameter on this drawing, so there is nothing to measure."
    distances = _edge_material(drawing, diameter)
    if not distances:
        return "not_applicable", (
            "No dimension locates a hole from an edge, so edge distance cannot be worked out."
        )
    minimum = MIN_EDGE_MATERIAL_RATIO * diameter
    need = _length(drawing, minimum)
    if failed:
        short = [
            f"hole {hole} leaves {_length(drawing, material)} to the {_edge_words(edge)}"
            for (hole, edge), (material, _, _) in distances.items()
            if material < minimum
        ]
        text = _join(short)
        return "fail", f"{text[0].upper()}{text[1:]}, under the {need} minimum."
    (hole, edge), (material, _, _) = min(distances.items(), key=lambda item: item[1][0])
    return "pass", (
        f"Every hole keeps at least {need} of material to the edge (closest: "
        f"{_length(drawing, material)}, hole {hole} to the {_edge_words(edge)})."
    )


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
    describe=_describe_hole_edge_distance,
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


def _issue_refs(numbers: list[int]) -> str:
    """``[2]`` -> ``"issue 2"``, ``[2, 3]`` -> ``"issues 2, 3"``."""
    word = "issue" if len(numbers) == 1 else "issues"
    return f"{word} {', '.join(str(number) for number in numbers)}"


def _new_refs(count: int) -> str:
    """``1`` -> ``"1 new finding"``, ``2`` -> ``"2 new findings"``."""
    return _times(count, "new finding")


def rule_results(
    drawing: Drawing,
    findings: list[Finding] | None = None,
    numbering: list[Finding] | None = None,
) -> list[RuleResult]:
    """Every rule's verdict on the whole drawing, in registry order.

    A rule fails exactly when it produced findings; that is decided by the rule's
    ``check``, never by its sentence. ``findings`` is ``run_rules(drawing)`` — pass it
    when you already have it, so the issue numbers match the list you are showing.

    ``numbering`` is the finding list the issue numbers count into; it defaults to
    ``findings``. For a corrected drawing, pass the *submitted* drawing's findings, so a
    finding that survived the fixes keeps its original number (its card, cloud, and
    redline marker). A finding that is not on that list has no number, and the sentence
    says it is new.
    """
    if findings is None:
        findings = run_rules(drawing)
    reference = findings if numbering is None else numbering
    # Two findings can share a key (one frame missing two datums), so each key holds its
    # numbers in order and each matching finding takes the next one.
    numbers_by_key: dict[str, list[int]] = defaultdict(list)
    for number, finding in enumerate(reference, start=1):
        numbers_by_key[finding_key(finding)].append(number)

    numbers: dict[str, list[int]] = defaultdict(list)
    new: dict[str, int] = defaultdict(int)
    for finding in findings:
        waiting = numbers_by_key.get(finding_key(finding))
        if waiting:
            numbers[finding.rule_id].append(waiting.pop(0))
        else:
            new[finding.rule_id] += 1

    results: list[RuleResult] = []
    for rule in RULES:
        issues = sorted(numbers.get(rule.id, []))
        fresh = new.get(rule.id, 0)
        failed = bool(issues) or fresh > 0
        status, explanation = rule.describe(drawing, failed)
        if failed != (status == "fail"):
            raise ValueError(f"{rule.id} described itself as {status!r} against its findings")
        body = explanation[:-1]
        if issues and fresh:
            explanation = f"{body} (see {_issue_refs(issues)}, plus {_new_refs(fresh)})."
        elif issues:
            explanation = f"{body} (see {_issue_refs(issues)})."
        elif fresh:
            explanation = f"{body} ({_new_refs(fresh)}, not on the submitted drawing)."
        results.append(
            RuleResult(
                rule_id=rule.id,
                title=rule.title,
                status=status,
                explanation=explanation,
                issues=issues,
            )
        )
    return results


def checks_summary(results: list[RuleResult]) -> str:
    """One line for the whole run, e.g. ``"6 checks: 5 passed, 1 failed"``.

    "not applicable" is only mentioned when some rule had nothing to look at.
    """
    passed = sum(1 for result in results if result.status == "pass")
    failed = sum(1 for result in results if result.status == "fail")
    skipped = len(results) - passed - failed
    line = f"{len(results)} checks: {passed} passed, {failed} failed"
    return f"{line}, {skipped} not applicable" if skipped else line
