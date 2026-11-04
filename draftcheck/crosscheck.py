"""Compare two models' readings of the same drawing, item by item.

When cross-check is on, the upload reads the drawing twice — once with the primary model
(Muse Spark Open on Baseten) and once with a second Meta model (Muse Spark on the Meta
Model API). The primary reading is the one the tables show and the rules run on. This
module only lines the two readings up and says where they differ, so a person knows which
values deserve a second look.

**It decides nothing.** A disagreement is a hint for the reviewer, never a verdict: the
rules do not read this report, and nothing here changes the drawing. Everything is plain
Python over two validated :class:`~draftcheck.schema.Drawing` objects — no model call.

Matching happens in passes, each taking only the items earlier passes left over:

* dimensions — same printed text, then same value, then the nearest box centre;
* datums — same label, then the nearest box centre;
* frames — same characteristic and tolerance, then same characteristic near by, then the
  nearest box centre.

Within a pass, when several pairs qualify, the closest boxes are paired first, so two
dimensions both printed "20" pair with their own counterparts rather than crosswise.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from draftcheck.schema import FCF, DatumFeature, DatumRef, Dimension, Drawing

#: What the page and the notes call the two readers.
PRIMARY_NAME = "Muse Spark Open"
CROSS_CHECK_NAME = "Muse Spark"

#: Farthest two box centres may be apart, as a fraction of the sheet, and still be taken
#: for the same item when nothing else ties them together.
MAX_CENTRE_DISTANCE = 0.05

#: Two numbers closer than this are the same reading (they come from JSON floats).
NUMBER_TOLERANCE = 1e-9

#: The fields each kind of item is compared on: the ones the rules read.
DIMENSION_FIELDS = ("value", "tol_plus", "tol_minus", "is_basic", "from_ref", "to_ref")
FCF_FIELDS = ("characteristic", "tolerance", "material_modifier", "datum_refs")
DATUM_FIELDS = ("label",)
DRAWING_FIELDS = ("units", "general_tolerance")

#: The ``item_id`` a disagreement about the whole drawing (units, general tolerance) uses.
DRAWING_ID = "drawing"


@dataclass(frozen=True)
class Disagreement:
    """One value the two models read differently."""

    #: The primary reading's id for the item — the id its table row carries — or
    #: :data:`DRAWING_ID` for units and the general tolerance.
    item_id: str
    kind: str
    field: str
    primary: Any
    cross_check: Any
    primary_text: str
    cross_check_text: str
    #: The sentence the page shows on hover: "Muse Spark Open read 30; Muse Spark read 35."
    note: str
    #: The second reading's id for the same item, which may differ from ``item_id``.
    cross_check_id: str | None = None


@dataclass(frozen=True)
class OneSided:
    """An item only one of the two models read."""

    item_id: str
    kind: str
    #: A few words saying what the item is, e.g. ``"dimension 30"``.
    label: str
    bbox: list[float] | None = None


@dataclass
class CrossCheckReport:
    """Where two readings of one drawing agree and where they do not."""

    disagreements: list[Disagreement] = field(default_factory=list)
    #: Items in the primary reading (the tables) that the second model did not read.
    only_primary: list[OneSided] = field(default_factory=list)
    #: Items the second model read that the primary reading does not have.
    only_cross_check: list[OneSided] = field(default_factory=list)
    values_compared: int = 0
    values_agreeing: int = 0

    def summary(self) -> str:
        """One line for the page, e.g. "Both Meta models agree on 23 of 25 values; 2 to
        double-check"."""
        total, agree = self.values_compared, self.values_agreeing
        one_sided = len(self.only_primary) + len(self.only_cross_check)
        if agree == total:
            parts = [f"Both Meta models agree on all {_count(total, 'value')}"]
        else:
            parts = [
                f"Both Meta models agree on {agree} of {_count(total, 'value')}",
                f"{total - agree} to double-check",
            ]
        if one_sided:
            parts.append(f"{_count(one_sided, 'item')} only one model read")
        return "; ".join(parts)

    def to_dict(self) -> dict:
        return {
            "disagreements": [asdict(item) for item in self.disagreements],
            "only_primary": [asdict(item) for item in self.only_primary],
            "only_cross_check": [asdict(item) for item in self.only_cross_check],
            "values_compared": self.values_compared,
            "values_agreeing": self.values_agreeing,
        }


def _count(number: int, word: str) -> str:
    return f"{number} {word}" if number == 1 else f"{number} {word}s"


# ---- Normalizing values so equal readings compare equal ---------------------------------


def _norm_text(value: str | None) -> str | None:
    """Printed text with case and spacing ignored; blank is ``None``."""
    if value is None:
        return None
    collapsed = re.sub(r"\s+", " ", value).strip().upper()
    return collapsed or None


def _norm_name(value: Any) -> str | None:
    """A short name (a reference, a modifier, a unit) with case ignored; blank is ``None``."""
    if value is None:
        return None
    text = str(getattr(value, "value", value)).strip().lower()
    return text or None


def _refs(refs: Sequence[DatumRef]) -> tuple[tuple[str, str | None], ...]:
    return tuple((ref.label, ref.modifier) for ref in refs)


def _same(a: Any, b: Any) -> bool:
    """Whether two normalized readings are the same; numbers within a hair are."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(a, b, rel_tol=0, abs_tol=NUMBER_TOLERANCE)
    return a == b


def _shown(value: Any) -> str:
    """A reading as a person would say it: 30, 0.25, yes, A|B|C, nothing."""
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int | float):
        return f"{value:g}"
    if isinstance(value, tuple):
        if not value:
            return "no datums"
        return "|".join(label + (f"({mod})" if mod else "") for label, mod in value)
    return str(getattr(value, "value", value))


def _raw(value: Any) -> Any:
    """A reading as JSON can carry it, for the report."""
    if isinstance(value, tuple):
        return [{"label": label, "modifier": mod} for label, mod in value]
    return getattr(value, "value", value)


# ---- Reading the compared fields off each kind of item ---------------------------------


def _dimension_values(item: Dimension) -> dict[str, Any]:
    return {
        "value": item.value,
        "tol_plus": item.tol_plus,
        "tol_minus": item.tol_minus,
        "is_basic": item.is_basic,
        "from_ref": _norm_name(item.from_ref),
        "to_ref": _norm_name(item.to_ref),
    }


def _fcf_values(item: FCF) -> dict[str, Any]:
    return {
        "characteristic": _norm_name(item.characteristic),
        "tolerance": item.tolerance,
        # Already MMC, LMC, or None: the schema normalizes it.
        "material_modifier": item.material_modifier,
        "datum_refs": _refs(item.datum_refs),
    }


def _datum_values(item: DatumFeature) -> dict[str, Any]:
    return {"label": item.label}


def _drawing_values(drawing: Drawing) -> dict[str, Any]:
    tolerance = drawing.general_tolerance
    return {
        "units": _norm_name(drawing.units),
        "general_tolerance": tolerance.linear_plus_minus if tolerance else None,
    }


# ---- Matching ---------------------------------------------------------------------------


def _centre(bbox: list[float] | None) -> tuple[float, float] | None:
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    return ((x0 + x1) / 2, (y0 + y1) / 2)


def _distance(a: Any, b: Any) -> float | None:
    """How far apart two items' box centres are, or ``None`` if either has no box."""
    ca, cb = _centre(a.bbox), _centre(b.bbox)
    if ca is None or cb is None:
        return None
    return math.dist(ca, cb)


#: Sorts pairs without a distance after every pair with one.
_NO_DISTANCE = 10.0


def _pass(
    left: list, right: list, pairs: list, same: Callable[[Any, Any], bool], near: bool
) -> None:
    """Pair what qualifies, closest boxes first, removing each pair from ``left``/``right``.

    ``near`` limits the pass to pairs whose box centres are both known and within
    :data:`MAX_CENTRE_DISTANCE`; otherwise ``same`` alone decides who may pair.
    """
    candidates = []
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            if not same(a, b):
                continue
            distance = _distance(a, b)
            if near and (distance is None or distance > MAX_CENTRE_DISTANCE):
                continue
            candidates.append((_NO_DISTANCE if distance is None else distance, i, j))
    candidates.sort()

    taken_left: set[int] = set()
    taken_right: set[int] = set()
    for _, i, j in candidates:
        if i in taken_left or j in taken_right:
            continue
        taken_left.add(i)
        taken_right.add(j)
        pairs.append((left[i], right[j]))
    left[:] = [a for i, a in enumerate(left) if i not in taken_left]
    right[:] = [b for j, b in enumerate(right) if j not in taken_right]


def _keyed(key: Callable[[Any], Any]) -> Callable[[Any, Any], bool]:
    """Two items qualify when they share a key that is not ``None``."""

    def same(a: Any, b: Any) -> bool:
        ka = key(a)
        return ka is not None and ka == key(b)

    return same


def _anything(a: Any, b: Any) -> bool:
    return True


def _rounded(number: float | None) -> float | None:
    return None if number is None else round(number, 6)


def match_dimensions(
    primary: list[Dimension], cross: list[Dimension]
) -> tuple[list[tuple[Dimension, Dimension]], list[Dimension], list[Dimension]]:
    """Pair dimensions by printed text, then value, then nearest box. Returns
    ``(pairs, only_primary, only_cross)``."""
    left, right, pairs = list(primary), list(cross), []
    _pass(left, right, pairs, _keyed(lambda d: _norm_text(d.text_raw)), near=False)
    _pass(left, right, pairs, _keyed(lambda d: _rounded(d.value)), near=False)
    _pass(left, right, pairs, _anything, near=True)
    return pairs, left, right


def match_datums(
    primary: list[DatumFeature], cross: list[DatumFeature]
) -> tuple[list[tuple[DatumFeature, DatumFeature]], list[DatumFeature], list[DatumFeature]]:
    """Pair datum symbols by label, then nearest box."""
    left, right, pairs = list(primary), list(cross), []
    _pass(left, right, pairs, _keyed(lambda d: d.label or None), near=False)
    _pass(left, right, pairs, _anything, near=True)
    return pairs, left, right


def match_fcfs(
    primary: list[FCF], cross: list[FCF]
) -> tuple[list[tuple[FCF, FCF]], list[FCF], list[FCF]]:
    """Pair frames by characteristic and tolerance, then characteristic near by, then box."""
    left, right, pairs = list(primary), list(cross), []
    _pass(
        left,
        right,
        pairs,
        _keyed(lambda f: (_norm_name(f.characteristic), _rounded(f.tolerance))),
        near=False,
    )
    _pass(left, right, pairs, _keyed(lambda f: _norm_name(f.characteristic)), near=True)
    _pass(left, right, pairs, _anything, near=True)
    return pairs, left, right


# ---- Comparing -------------------------------------------------------------------------


def _compare_values(
    report: CrossCheckReport,
    kind: str,
    item_id: str,
    cross_id: str | None,
    primary: dict[str, Any],
    cross: dict[str, Any],
) -> None:
    for name, mine in primary.items():
        theirs = cross[name]
        report.values_compared += 1
        if _same(mine, theirs):
            report.values_agreeing += 1
            continue
        mine_text, theirs_text = _shown(mine), _shown(theirs)
        report.disagreements.append(
            Disagreement(
                item_id=item_id,
                kind=kind,
                field=name,
                primary=_raw(mine),
                cross_check=_raw(theirs),
                primary_text=mine_text,
                cross_check_text=theirs_text,
                note=f"{PRIMARY_NAME} read {mine_text}; {CROSS_CHECK_NAME} read {theirs_text}.",
                cross_check_id=cross_id,
            )
        )


def _describe(kind: str, item: Any) -> str:
    if kind == "dimension":
        return f"dimension {item.text_raw or _shown(item.value)}"
    if kind == "datum":
        return f"datum {item.label}"
    return f"{_shown(item.characteristic)} frame"


def _one_sided(kind: str, items: list) -> list[OneSided]:
    return [
        OneSided(item_id=item.id or "", kind=kind, label=_describe(kind, item), bbox=item.bbox)
        for item in items
    ]


def compare(primary: Drawing, cross: Drawing) -> CrossCheckReport:
    """Line two readings of one drawing up and report where they differ.

    ``primary`` is the reading the tables show; disagreements and one-sided items carry its
    ids, so the page can mark the right rows. Order follows the page: the drawing's own
    fields, then datums, dimensions, and frames, each in the primary reading's order.
    """
    report = CrossCheckReport()
    _compare_values(
        report, "drawing", DRAWING_ID, DRAWING_ID, _drawing_values(primary), _drawing_values(cross)
    )

    groups: list[tuple[str, Callable, Callable, list, list]] = [
        ("datum", match_datums, _datum_values, primary.datums, cross.datums),
        ("dimension", match_dimensions, _dimension_values, primary.dimensions, cross.dimensions),
        ("fcf", match_fcfs, _fcf_values, primary.fcfs, cross.fcfs),
    ]
    for kind, match, values, mine, theirs in groups:
        pairs, only_mine, only_theirs = match(mine, theirs)
        # Pairs come out in matching order; report them in the order the tables show.
        position = {id(item): index for index, item in enumerate(mine)}
        pairs.sort(key=lambda pair: position[id(pair[0])])
        for a, b in pairs:
            _compare_values(report, kind, a.id or "", b.id, values(a), values(b))
        report.only_primary.extend(_one_sided(kind, only_mine))
        report.only_cross_check.extend(_one_sided(kind, only_theirs))
    return report


# ---- What the upload reports -----------------------------------------------------------

#: Every outcome carries the same keys, so the page never has to ask whether one is there.
_EMPTY = CrossCheckReport().to_dict()


def _outcome(status: str, summary: str, **extra: Any) -> dict:
    return {"status": status, "summary": summary, "model": None, **_EMPTY, **extra}


def outcome_off() -> dict:
    """The person turned the cross-check off."""
    return _outcome("off", "Cross-check off.")


def outcome_skipped(reason: str) -> dict:
    """A cross-check that was not run, or was dropped, and why."""
    return _outcome("skipped", f"Cross-check skipped: {reason}", reason=reason)


def outcome_timeout() -> dict:
    """The second reading was not back within the grace period."""
    return _outcome("timeout", "Cross-check didn't finish in time.")


def outcome_failed(reason: str) -> dict:
    """The second model's call failed; the primary reading stands as it is."""
    return _outcome("failed", f"Cross-check failed: {reason}", reason=reason)


def outcome_done(report: CrossCheckReport, model: str | None) -> dict:
    """Both readings came back and were compared."""
    return {"status": "done", "summary": report.summary(), "model": model, **report.to_dict()}


#: Why a cross-check is skipped, in the page's words.
SKIP_PRIMARY_FELL_BACK = (
    f"{PRIMARY_NAME} didn't answer, so {CROSS_CHECK_NAME} read the drawing instead; "
    "both readings would come from the same model."
)
SKIP_PRIMARY_IS_CROSS = (
    f"the drawing was read with {CROSS_CHECK_NAME}, which is the second model, "
    "so there is nothing to compare it with."
)
