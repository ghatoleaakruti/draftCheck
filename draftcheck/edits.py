"""The only edits the correction agent is allowed to make to a drawing.

The model proposes; this module decides what actually happens. A proposal is a plain JSON
object naming one of four operations, and anything else is refused with a reason rather
than applied. That matters because the cheapest way to clear a finding is to delete the
callout that caused it — removing the feature control frame makes "references undefined
datum C" disappear without fixing anything. There is no operation that deletes a
dimension, a feature control frame, or a datum feature, and an edit that would make one
vanish is rejected even if it arrives under an allowed operation name.

Edits are applied one at a time to a copy of the drawing. After each one the whole drawing
is re-validated through ``schema.parse_drawing``; if the result does not hold up, that edit
is rolled back and reported, and the remaining edits still get their turn. Nothing here
raises on bad model output — a malformed proposal is data about the model, not an error.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from draftcheck.schema import DatumFeature, Dimension, Drawing, parse_drawing

#: Maps the ``target`` of a ``set_field`` edit to the list it lives in on a ``Drawing``.
_ITEM_LISTS = {"dimension": "dimensions", "fcf": "fcfs", "datum": "datums"}

#: The fields each target exposes to ``set_field``. A whitelist rather than a blacklist:
#: a field has to be deliberately opened up, so nothing new becomes editable by accident
#: when the schema grows. Note what is absent — ``id`` and ``bbox`` identify and locate an
#: item, and ``datum_refs`` is a list that could be emptied in one assignment, so it is
#: changed only through ``remove_datum_ref``.
_EDITABLE_FIELDS: dict[str, frozenset[str]] = {
    "dimension": frozenset(
        {
            "text_raw",
            "value",
            "unit",
            "feature_type",
            "quantity",
            "is_basic",
            "is_reference",
            "tol_plus",
            "tol_minus",
            "from_ref",
            "to_ref",
        }
    ),
    "fcf": frozenset(
        {"text_raw", "characteristic", "tolerance", "diameter_zone", "material_modifier"}
    ),
    "datum": frozenset({"label", "location"}),
    "drawing": frozenset({"units", "general_tolerance"}),
}

#: Fields that are never editable, called out separately so the refusal can say why.
_PROTECTED_FIELDS = {
    "id": "it identifies the item, and findings point at it",
    "bbox": "it is where the item was found on the sheet, not something a correction changes",
    "datum_refs": "a whole-list assignment could empty it; use remove_datum_ref instead",
    "datums": "features are never replaced wholesale",
    "dimensions": "features are never replaced wholesale",
    "fcfs": "features are never replaced wholesale",
    "parse_issues": "it is a record of what extraction could not read",
}

#: Words that suggest the model is reaching for a delete, so the refusal can say the rule
#: out loud instead of just "unknown operation".
_DELETION_WORDS = ("remove", "delete", "drop", "clear", "strip", "erase")


class EditBase(BaseModel):
    """What every proposed edit carries, whichever operation it is.

    ``extra="ignore"`` because a model that adds a stray key has still told us what it
    wants; refusing over the key would waste a round.
    """

    model_config = ConfigDict(extra="ignore")

    #: One sentence of engineering reasoning. Never acted on — it is for the person
    #: reading the output, who has to decide whether the fix is right.
    rationale: str = ""
    #: The findings this edit claims to address, as ``schema.finding_key`` names them.
    #: Also never acted on: whether a finding cleared is decided by re-running the rules.
    finding_ids: list[str] = Field(default_factory=list)


class SetField(EditBase):
    """Change one field of one item, or one of the drawing's two top-level values."""

    op: Literal["set_field"]
    target: Literal["dimension", "fcf", "datum", "drawing"]
    #: The item's id. Omitted when ``target`` is ``"drawing"``.
    id: str | None = None
    field: str
    value: Any = None

    def describe(self) -> str:
        return f"set_field  {self.id or 'drawing'}.{self.field} = {self.value!r}"


class AddDatum(EditBase):
    """Add a datum feature symbol the drawing is missing."""

    op: Literal["add_datum"]
    label: str
    #: Where the symbol goes, in words, for the person reviewing the redline.
    location: str
    bbox: list[float] | None = None

    def describe(self) -> str:
        return f"add_datum  {self.label} at {self.location!r}"


class RemoveDatumRef(EditBase):
    """Take one datum reference out of one feature control frame.

    The only removal on the menu, and it removes a reference, not a feature: the datum
    symbol and the frame both survive. It is how a form control sheds a datum it should
    never have had, and how a frame stops citing a letter the drawing does not define.
    """

    op: Literal["remove_datum_ref"]
    fcf_id: str
    label: str

    def describe(self) -> str:
        return f"remove_datum_ref  datum {self.label} from frame {self.fcf_id}"


class AddDimension(EditBase):
    """Add a dimension, given as the fields of a ``schema.Dimension``.

    Any ``id`` in the payload is discarded and a fresh unused one assigned, so a proposed
    dimension can never land on top of an existing one.
    """

    op: Literal["add_dimension"]
    dimension: dict[str, Any]

    def describe(self) -> str:
        text = self.dimension.get("text_raw") or self.dimension.get("value")
        between = (self.dimension.get("from_ref"), self.dimension.get("to_ref"))
        where = f" from {between[0]} to {between[1]}" if all(between) else ""
        return f"add_dimension  {text}{where}"


Edit = Annotated[
    SetField | AddDatum | RemoveDatumRef | AddDimension, Field(discriminator="op")
]
_EDIT_ADAPTER: TypeAdapter[Edit] = TypeAdapter(Edit)

#: The operation names the model may use, derived from the models above so the prompt,
#: the parser, and the refusal message can never drift apart.
ALLOWED_OPS: tuple[str, ...] = ("set_field", "add_datum", "remove_datum_ref", "add_dimension")


@dataclass(frozen=True)
class RejectedEdit:
    """One refused proposal, kept exactly as it arrived, with why it was refused.

    The reason is written for the model as much as for the person: it goes back in the
    next round's history so the same mistake is not proposed again.
    """

    #: The proposal as received, before any parsing — a dict, or whatever else came back.
    edit: Any
    reason: str


class _Refused(Exception):  # noqa: N818 - internal control flow, never leaves this module
    """Internal: this edit cannot be applied, and here is what to tell the caller."""


def _describe_error(error: dict) -> str:
    """Turn one pydantic error into a short phrase, the way ``schema`` does for items."""
    field = ".".join(str(part) for part in error["loc"]) or "edit"
    if error["type"] == "missing":
        return f"missing required field '{field}'"
    return f"invalid {field}: {error['msg'].lower()}"


def parse_edit(raw: Any) -> tuple[Edit | None, str]:
    """Turn one raw proposal into an ``Edit``, or explain why it is not one.

    Returns ``(edit, "")`` on success and ``(None, reason)`` on refusal. The operation name
    is checked before pydantic sees it, so an unknown op gets a sentence about what is
    allowed rather than a union-discriminator error nobody can act on.
    """
    if isinstance(raw, EditBase):
        return raw, ""
    if not isinstance(raw, dict):
        return None, f"an edit must be a JSON object, not {type(raw).__name__}"

    op = raw.get("op")
    if not isinstance(op, str) or not op.strip():
        return None, "the edit does not say which operation to run ('op' is missing)"

    op = op.strip().lower()
    if op not in ALLOWED_OPS:
        if any(word in op for word in _DELETION_WORDS):
            return None, (
                f"'{op}' is not an allowed operation. A dimension, feature control frame, "
                "or datum feature is never deleted to clear a finding — fix the callout "
                "instead. The only removal available is remove_datum_ref, which takes one "
                "datum letter out of one frame."
            )
        return None, f"'{op}' is not an allowed operation; allowed: {', '.join(ALLOWED_OPS)}"

    try:
        return _EDIT_ADAPTER.validate_python({**raw, "op": op}), ""
    except ValidationError as exc:
        return None, "; ".join(_describe_error(error) for error in exc.errors())


def _find(items: list[dict], item_id: str) -> dict | None:
    """The item in a dumped list with this id, or ``None``."""
    for item in items:
        if item.get("id") == item_id:
            return item
    return None


def _next_id(items: list[dict], prefix: str) -> str:
    """An id with this prefix that nothing in the list is already using."""
    taken = {item.get("id") for item in items}
    number = len(items) + 1
    while f"{prefix}{number}" in taken:
        number += 1
    return f"{prefix}{number}"


def _value_survived(item: Any, field: str, requested: Any) -> str | None:
    """Check that a field really took the value, not that the schema quietly dropped it.

    Parsing is deliberately lenient — a negative tolerance, an unreadable unit, or a
    malformed box becomes ``None`` instead of raising. That is right for extraction, where
    the user fixes the data afterwards, and wrong here: an edit that silently did nothing
    would be reported as applied and the finding would stay. So any request for a real
    value that comes back ``None`` is a refusal.
    """
    if requested is None:
        return None
    if getattr(item, field, None) is None:
        return f"the schema does not accept {requested!r} for '{field}', so nothing changed"
    return None


def _apply_set_field(data: dict, edit: SetField) -> Any:
    """Write one field into the dumped drawing; return a check to run after re-validation."""
    field = edit.field.strip()
    if field in _PROTECTED_FIELDS:
        raise _Refused(f"'{field}' cannot be edited: {_PROTECTED_FIELDS[field]}")

    allowed = _EDITABLE_FIELDS[edit.target]
    if field not in allowed:
        raise _Refused(
            f"'{field}' is not an editable field of a {edit.target}; "
            f"editable: {', '.join(sorted(allowed))}"
        )

    if edit.target == "drawing":
        value = edit.value
        # A general tolerance is an object, but "set the general tolerance to 0.1" is the
        # natural way to say it, so a bare number is accepted and wrapped.
        if (
            field == "general_tolerance"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            value = {
                "linear_plus_minus": float(value),
                "text_raw": f"UNLESS OTHERWISE SPECIFIED: ±{float(value):g}",
            }
        data[field] = value

        def check_drawing(drawing: Drawing) -> str | None:
            problem = _value_survived(drawing, field, edit.value)
            if problem is not None:
                return problem
            if field == "general_tolerance" and edit.value is not None:
                tolerance = drawing.general_tolerance
                if tolerance is None or tolerance.linear_plus_minus is None:
                    return (
                        f"the schema does not accept {edit.value!r} as a general tolerance; "
                        "it needs a positive linear_plus_minus value"
                    )
            return None

        return check_drawing

    if not edit.id:
        raise _Refused(f"a set_field on a {edit.target} must name the item's id")

    list_name = _ITEM_LISTS[edit.target]
    item = _find(data[list_name], edit.id)
    if item is None:
        raise _Refused(f"no {edit.target} with id '{edit.id}' is on this drawing")
    item[field] = edit.value

    def check_item(drawing: Drawing) -> str | None:
        for candidate in getattr(drawing, list_name):
            if candidate.id == edit.id:
                return _value_survived(candidate, field, edit.value)
        return f"{edit.target} '{edit.id}' is no longer on the drawing after the edit"

    return check_item


def _apply_add_datum(data: dict, edit: AddDatum) -> Any:
    """Append a datum feature symbol; return a check to run after re-validation."""
    label = edit.label.strip().upper()
    if not label.isalpha() or len(label) > 2:
        raise _Refused(
            f"'{edit.label}' is not a datum label; a datum is identified by one or two "
            "letters, e.g. A or AB"
        )
    if not edit.location.strip():
        raise _Refused("add_datum must say where on the drawing the symbol goes")

    new_id = _next_id(data["datums"], "DT")
    data["datums"].append(
        DatumFeature(
            id=new_id, label=label, location=edit.location.strip(), bbox=edit.bbox
        ).model_dump(mode="json")
    )

    def check(drawing: Drawing) -> str | None:
        if not any(datum.id == new_id and datum.label == label for datum in drawing.datums):
            return f"datum {label} did not survive validation"
        return None

    return check


def _apply_remove_datum_ref(data: dict, edit: RemoveDatumRef) -> Any:
    """Drop one datum reference from one frame; return a check to run afterwards."""
    fcf = _find(data["fcfs"], edit.fcf_id)
    if fcf is None:
        raise _Refused(f"no feature control frame with id '{edit.fcf_id}' is on this drawing")

    label = edit.label.strip().upper()
    refs: list[dict] = fcf.get("datum_refs") or []
    position = next((i for i, ref in enumerate(refs) if ref.get("label") == label), None)
    if position is None:
        cited = ", ".join(ref.get("label", "?") for ref in refs) or "none"
        raise _Refused(
            f"frame '{edit.fcf_id}' does not reference datum {label} (it references: {cited})"
        )

    # One reference, not every copy of the letter: the frame may legitimately be edited
    # again, and a single removal is the smallest change that can be made here.
    expected = len(refs) - 1
    del refs[position]
    fcf["datum_refs"] = refs

    def check(drawing: Drawing) -> str | None:
        for candidate in drawing.fcfs:
            if candidate.id == edit.fcf_id:
                if len(candidate.datum_refs) != expected:
                    return "the frame's datum references did not survive validation"
                return None
        return f"frame '{edit.fcf_id}' is no longer on the drawing after the edit"

    return check


def _apply_add_dimension(data: dict, edit: AddDimension) -> Any:
    """Append a dimension; return a check to run after re-validation."""
    if not isinstance(edit.dimension, dict) or not edit.dimension:
        raise _Refused("add_dimension needs a 'dimension' object with at least a value")

    spec = {key: value for key, value in edit.dimension.items() if key != "id"}
    new_id = _next_id(data["dimensions"], "D")
    spec["id"] = new_id
    data["dimensions"].append(spec)

    def check(drawing: Drawing) -> str | None:
        if not any(dimension.id == new_id for dimension in drawing.dimensions):
            return (
                "the proposed dimension is not valid — a dimension needs at least a "
                f"numeric value; fields given: {', '.join(sorted(spec)) or 'none'}"
            )
        return None

    return check


_APPLIERS = {
    SetField: _apply_set_field,
    AddDatum: _apply_add_datum,
    RemoveDatumRef: _apply_remove_datum_ref,
    AddDimension: _apply_add_dimension,
}


def _structural_problem(before: Drawing, after: Drawing) -> str | None:
    """Refuse an edit that lost a feature or produced something unreadable.

    This runs for every operation, including the ones that only mean to add or change
    something. It is the backstop: whatever the edit claimed to do, nothing that was on
    the drawing before is allowed to be missing after.
    """
    if len(after.parse_issues) > len(before.parse_issues):
        new = "; ".join(after.parse_issues[len(before.parse_issues) :])
        return f"the edit produced something the schema cannot read: {new}"

    for list_name, noun in (("datums", "datum"), ("dimensions", "dimension"), ("fcfs", "frame")):
        was = {item.id for item in getattr(before, list_name)}
        now = {item.id for item in getattr(after, list_name)}
        lost = sorted(identifier for identifier in was - now if identifier)
        if lost:
            return (
                f"the edit would remove {noun} {', '.join(lost)} from the drawing. "
                "A feature is never deleted to clear a finding."
            )
    return None


def apply_edits(
    drawing: Drawing, edits: Any
) -> tuple[Drawing, list[Edit], list[RejectedEdit]]:
    """Apply what can be applied to a copy of ``drawing``, and report what could not.

    Each edit is applied on its own and the whole drawing re-validated afterwards, so one
    bad proposal costs only itself — the edits around it still take effect. The drawing
    passed in is never modified.

    Args:
        drawing: The drawing to edit. Left untouched.
        edits: Whatever the model produced. A list of proposals normally, but anything
            else is handled rather than raised on.

    Returns:
        ``(new_drawing, applied, rejected)``. ``new_drawing`` carries every applied edit;
        ``rejected`` holds each refused proposal next to the reason, in the order proposed.
    """
    if not isinstance(edits, (list, tuple)):
        reason = f"expected a list of edits, got {type(edits).__name__}"
        return drawing, [], [RejectedEdit(edits, reason)]

    current = drawing
    data = drawing.model_dump(mode="json")
    applied: list[Edit] = []
    rejected: list[RejectedEdit] = []

    for raw in edits:
        edit, reason = parse_edit(raw)
        if edit is None:
            rejected.append(RejectedEdit(raw, reason))
            continue

        candidate_data = copy.deepcopy(data)
        try:
            check = _APPLIERS[type(edit)](candidate_data, edit)
        except _Refused as refusal:
            rejected.append(RejectedEdit(raw, str(refusal)))
            continue

        try:
            candidate = parse_drawing(candidate_data)
        except ValidationError as exc:
            problems = "; ".join(_describe_error(error) for error in exc.errors()[:3])
            rejected.append(RejectedEdit(raw, f"the edited drawing is not valid: {problems}"))
            continue

        problem = _structural_problem(current, candidate) or check(candidate)
        if problem is not None:
            rejected.append(RejectedEdit(raw, problem))
            continue

        current = candidate
        data = candidate.model_dump(mode="json")
        applied.append(edit)

    return current, applied, rejected


#: The field names ``add_dimension`` accepts, published so the prompt can list exactly what
#: the schema will take instead of a hand-written copy that goes stale.
DIMENSION_FIELDS: tuple[str, ...] = tuple(
    name for name in Dimension.model_fields if name not in {"id", "confidence"}
)
