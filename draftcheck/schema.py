"""Typed models for an extracted drawing, plus the shape of a rule finding.

Parsing here is deliberately lenient. Model output is imperfect, the user confirms the
data in the UI before any checking happens, and the rules flag missing values instead of
crashing on them. So a malformed bounding box becomes ``None`` rather than an error, an
unrecognized unit becomes ``None`` rather than an error, and an item that cannot be read
at all is dropped and described in ``Drawing.parse_issues`` rather than taking the whole
drawing down with it.
"""

from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)


class Units(StrEnum):
    """The two unit systems a drawing can be dimensioned in."""

    MM = "mm"
    INCH = "in"


# Spellings seen on drawings and in model output, mapped to the canonical value.
_UNIT_SPELLINGS = {
    "mm": Units.MM,
    "millimeter": Units.MM,
    "millimeters": Units.MM,
    "millimetre": Units.MM,
    "millimetres": Units.MM,
    "in": Units.INCH,
    "inch": Units.INCH,
    "inches": Units.INCH,
    '"': Units.INCH,
}


def _normalize_units(value: Any) -> Units | None:
    """Map a unit spelling to ``Units``; anything unrecognized becomes ``None``."""
    if isinstance(value, Units):
        return value
    if not isinstance(value, str):
        return None
    return _UNIT_SPELLINGS.get(value.strip().lower())


def _normalize_bbox(value: Any) -> list[float] | None:
    """Return a valid ``[x0, y0, x1, y1]`` box, or ``None``.

    A box must be four numbers in 0..1 with ``x0 < x1`` and ``y0 < y1``. Anything else —
    wrong length, non-numeric, out of range, reversed — becomes ``None``. This never
    raises: the user can correct locations later.
    """
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None

    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            return None
        try:
            numbers.append(float(item))
        except (TypeError, ValueError):
            return None

    x0, y0, x1, y1 = numbers
    if not all(0.0 <= n <= 1.0 for n in numbers):
        return None
    if x0 >= x1 or y0 >= y1:
        return None
    return numbers


def _upper_label(value: Any) -> Any:
    """Uppercase and strip a datum label, leaving non-strings for pydantic to reject."""
    if isinstance(value, str):
        return value.strip().upper()
    return value


def _positive_or_none(value: Any) -> float | None:
    """Keep a tolerance only if it is a positive number; anything else becomes ``None``.

    A tolerance of zero or less is not a tolerance, and a missing one is something the
    rules report rather than something parsing should fail on.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


UnitsField = Annotated[Units | None, BeforeValidator(_normalize_units)]
BBox = Annotated[list[float] | None, BeforeValidator(_normalize_bbox)]
Label = Annotated[str, BeforeValidator(_upper_label)]
PositiveTol = Annotated[float | None, BeforeValidator(_positive_or_none)]


class Base(BaseModel):
    """Shared config: ignore keys we do not model, so extra output never breaks parsing."""

    model_config = ConfigDict(extra="ignore")


class GeneralTolerance(Base):
    """The blanket tolerance from the title block, e.g. "UNLESS OTHERWISE SPECIFIED ±0.1"."""

    linear_plus_minus: float | None = None
    text_raw: str | None = None


class TitleBlock(Base):
    """Identifying fields from the drawing's title block."""

    title: str | None = None
    drawing_number: str | None = None
    revision: str | None = None
    material: str | None = None
    scale: str | None = None


class DatumFeature(Base):
    """A datum feature symbol placed on the drawing, e.g. the box labelled A."""

    id: str | None = None
    label: Label
    bbox: BBox = None
    confidence: float | None = None


class DatumRef(Base):
    """A datum called out inside a feature control frame."""

    label: Label
    modifier: str | None = None

    @model_validator(mode="after")
    def _check_modifier(self) -> "DatumRef":
        if self.modifier is not None:
            upper = self.modifier.strip().upper()
            self.modifier = upper if upper in {"MMC", "LMC"} else None
        return self


class Dimension(Base):
    """One dimension callout: its value, tolerance, and where it sits on the page."""

    id: str | None = None
    text_raw: str | None = None
    value: float
    unit: UnitsField = None
    feature_type: str | None = None
    quantity: int = 1
    is_basic: bool = False
    is_reference: bool = False
    tol_plus: PositiveTol = None
    tol_minus: PositiveTol = None
    # What the dimension measures between, e.g. "left_edge" and "hole_1_center".
    # Optional; used later by the hole-to-edge rule.
    from_ref: str | None = None
    to_ref: str | None = None
    bbox: BBox = None
    confidence: float | None = None


class Characteristic(StrEnum):
    """The fourteen geometric characteristics a feature control frame can specify."""

    POSITION = "position"
    FLATNESS = "flatness"
    STRAIGHTNESS = "straightness"
    CIRCULARITY = "circularity"
    CYLINDRICITY = "cylindricity"
    PERPENDICULARITY = "perpendicularity"
    PARALLELISM = "parallelism"
    ANGULARITY = "angularity"
    PROFILE_LINE = "profile_line"
    PROFILE_SURFACE = "profile_surface"
    CIRCULAR_RUNOUT = "circular_runout"
    TOTAL_RUNOUT = "total_runout"
    CONCENTRICITY = "concentricity"
    SYMMETRY = "symmetry"


#: Form controls describe a single feature's own shape, so they take no datum references.
FORM_CONTROLS = frozenset(
    {
        Characteristic.FLATNESS,
        Characteristic.STRAIGHTNESS,
        Characteristic.CIRCULARITY,
        Characteristic.CYLINDRICITY,
    }
)


class FCF(Base):
    """A feature control frame: one geometric characteristic, its zone, and its datums."""

    id: str | None = None
    text_raw: str | None = None
    characteristic: Characteristic
    tolerance: float
    diameter_zone: bool = False
    material_modifier: str | None = None
    datum_refs: list[DatumRef] = Field(default_factory=list)
    bbox: BBox = None
    confidence: float | None = None

    @model_validator(mode="after")
    def _check_modifier(self) -> "FCF":
        if self.material_modifier is not None:
            upper = self.material_modifier.strip().upper()
            self.material_modifier = upper if upper in {"MMC", "LMC"} else None
        return self


#: The item lists that are salvaged one entry at a time rather than all-or-nothing.
_ITEM_LISTS: tuple[tuple[str, type[Base]], ...] = (
    ("datums", DatumFeature),
    ("dimensions", Dimension),
    ("fcfs", FCF),
)


def _describe(error: dict) -> str:
    """Turn one pydantic error into a short phrase a person can act on."""
    field = ".".join(str(part) for part in error["loc"]) or "item"
    kind = error["type"]
    if kind == "missing":
        return f"missing required field '{field}'"
    if kind == "enum":
        return f"unknown {field} {error.get('input')!r}"
    if not error["loc"]:
        return "not an object"
    return f"invalid {field}: {error['msg'].lower()}"


class Drawing(Base):
    """Everything extracted from one drawing — the input to the rule engine."""

    units: UnitsField = None
    general_tolerance: GeneralTolerance | None = None
    title_block: TitleBlock | None = None
    datums: list[DatumFeature] = Field(default_factory=list)
    dimensions: list[Dimension] = Field(default_factory=list)
    fcfs: list[FCF] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    #: One line per item that could not be parsed and was dropped, e.g.
    #: ``"fcfs[2]: unknown characteristic 'wobbliness'"``. Empty on a clean parse.
    parse_issues: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _drop_unparsable_items(cls, data: Any) -> Any:
        """Validate list items one by one, dropping the bad ones into ``parse_issues``.

        One unreadable callout must not cost us the rest of the drawing: the item is
        left out and described here, and the rules report what is missing.
        """
        if not isinstance(data, dict):
            return data

        data = dict(data)
        issues = [str(issue) for issue in data.get("parse_issues") or []]

        for key, model in _ITEM_LISTS:
            raw = data.get(key)
            if not isinstance(raw, (list, tuple)):
                continue
            kept: list[Base] = []
            for index, item in enumerate(raw):
                try:
                    kept.append(model.model_validate(item))
                except ValidationError as exc:
                    issues.extend(f"{key}[{index}]: {_describe(e)}" for e in exc.errors())
            data[key] = kept

        data["parse_issues"] = issues
        return data

    @model_validator(mode="after")
    def _assign_ids(self) -> "Drawing":
        """Give every datum, dimension, and frame an id, so findings point at one item.

        Datum ids are assigned per symbol, not per label, so two symbols that both claim
        label A still get distinct ids and a duplicate-datum finding can name them both.
        """
        for index, datum in enumerate(self.datums, start=1):
            if not datum.id:
                datum.id = f"DT{index}"
        for index, dimension in enumerate(self.dimensions, start=1):
            if not dimension.id:
                dimension.id = f"D{index}"
        for index, fcf in enumerate(self.fcfs, start=1):
            if not fcf.id:
                fcf.id = f"F{index}"
        return self


class Finding(Base):
    """One rule violation, as every rule function reports it."""

    rule_id: str
    severity: str = Field(pattern="^(error|warning)$")
    message: str
    source: str
    item_ids: list[str] = Field(default_factory=list)
    bbox: BBox = None


def parse_drawing(data: dict) -> Drawing:
    """Turn an extraction dictionary into a validated :class:`Drawing`."""
    return Drawing.model_validate(data)
