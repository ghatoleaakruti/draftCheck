"""AI explanations of a drawing, for students: what the part is and why each item is there.

This is a teaching aid, not a check. The model writes the sentences; this module only
makes sure every sentence is *about something that exists*. Each entry the model returns
names an item — a dimension id, a frame id, a datum label, or a finding key — and an entry
whose name is not on the drawing (or among the findings it was given) is dropped and
described in ``parse_issues``. That is the whole defence against the model describing a
hole the drawing does not have.

Views work the same way. A view must have one of the names in :data:`VIEW_NAMES` or it is
dropped, and a dimension's ``view`` / ``also_seen_in`` may only name views the explanation
itself describes — a dimension "seen in the top view" of a drawing with no top view is
cleared (the dimension's note is kept) and recorded.

Nothing here is read by the rules. The explanation is saved beside the run as
``explain.json`` and shown on the page labelled "AI explanation · not checked by the
rules"; whether anything passes is still decided by :mod:`draftcheck.rules` alone.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from draftcheck.llm_client import JSONChatResult
from draftcheck.prompts import (
    EXPLAIN_SYSTEM,
    PART_TYPES,
    PROJECTIONS,
    TABLE_KINDS,
    VIEW_NAMES,
    explain_user,
)
from draftcheck.schema import Drawing, Finding, finding_key

DimensionKind = Literal["size", "location", "form", "reference"]
DatumRole = Literal["primary", "secondary", "tertiary", "unclear"]
Projection = Literal["first-angle", "third-angle", "unclear"]

#: Bumped whenever the explanation's shape changes, so an ``explain.json`` saved under an
#: older shape is not served from the cache. 2: views, projection, tables, balloons.
EXPLANATION_VERSION = 2


def _clean_text(value: Any) -> Any:
    """Strip a string; leave anything else for pydantic to accept or refuse."""
    return value.strip() if isinstance(value, str) else value


def _lower(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _upper(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


def _part_type(value: Any) -> str:
    """One of :data:`PART_TYPES`; anything the page has no label for is ``"other"``."""
    if isinstance(value, str) and value.strip().lower() in PART_TYPES:
        return value.strip().lower()
    return "other"


def _projection(value: Any) -> str:
    """One of :data:`PROJECTIONS`; anything else (or nothing) is ``"unclear"``."""
    if isinstance(value, str):
        word = value.strip().lower().replace("_", "-").replace(" ", "-")
        if word in PROJECTIONS:
            return word
    return "unclear"


def _table_kind(value: Any) -> str:
    """One of :data:`TABLE_KINDS`; a kind the page has no label for is ``"other"``."""
    if isinstance(value, str):
        word = value.strip().lower().replace(" ", "_").replace("-", "_")
        if word in TABLE_KINDS:
            return word
    return "other"


def _view_names(value: Any) -> list[str]:
    """A list of view names as given (lower-cased); checked against the views later."""
    if not isinstance(value, list):
        return []
    return [item.strip().lower() for item in value if isinstance(item, str) and item.strip()]


def _optional_text(value: Any) -> str | None:
    """A stripped string, or ``None`` for anything empty or not a string."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _confidence(value: Any) -> float | None:
    """A number clamped to 0..1, or ``None`` when it is not a number at all."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(number, 0.0), 1.0)


def _features(value: Any) -> list[str]:
    """The features list, keeping only non-empty strings."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


Text = Annotated[str, BeforeValidator(_clean_text), Field(min_length=1)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PartSummary(_Base):
    """What the model thinks the part is."""

    type: Annotated[str, BeforeValidator(_part_type)] = "other"
    name_guess: Annotated[str | None, BeforeValidator(_clean_text)] = None
    summary: Annotated[str | None, BeforeValidator(_clean_text)] = None
    features: Annotated[list[str], BeforeValidator(_features)] = Field(default_factory=list)
    confidence: Annotated[float | None, BeforeValidator(_confidence)] = None


class ViewNote(_Base):
    """One view on the sheet: what kind it is, its printed label, and what it shows."""

    name: Annotated[str, BeforeValidator(_lower)]
    label: Annotated[str | None, BeforeValidator(_optional_text)] = None
    shows: Text


class TableNote(_Base):
    """One table on the sheet (parts list, hole table, ...) and what it holds."""

    kind: Annotated[str, BeforeValidator(_table_kind)] = "other"
    summary: Text


class DimensionNote(_Base):
    id: Text
    kind: Annotated[DimensionKind, BeforeValidator(_lower)]
    purpose: Text
    #: The view the dimension is placed in, by view name (e.g. "front").
    view: Annotated[str | None, BeforeValidator(_optional_text)] = None
    #: Other views that show the same feature.
    also_seen_in: Annotated[list[str], BeforeValidator(_view_names)] = Field(
        default_factory=list
    )


class FcfNote(_Base):
    id: Text
    meaning: Text


class DatumNote(_Base):
    label: Annotated[str, BeforeValidator(_upper)]
    role: Annotated[DatumRole, BeforeValidator(_lower)]
    why: Text


class FindingNote(_Base):
    key: Text
    why_it_matters: Text
    how_to_think_about_fixing: Text


class Explanation(_Base):
    """A validated explanation: every entry names something that really exists."""

    part: PartSummary | None = None
    views: list[ViewNote] = Field(default_factory=list)
    projection: Projection = "unclear"
    #: One or two sentences for a student on how the views relate to each other.
    projection_note: str | None = None
    tables: list[TableNote] = Field(default_factory=list)
    #: One sentence on the item balloons, if the sheet has any.
    balloons: str | None = None
    dimensions: list[DimensionNote] = Field(default_factory=list)
    fcfs: list[FcfNote] = Field(default_factory=list)
    datums: list[DatumNote] = Field(default_factory=list)
    findings: list[FindingNote] = Field(default_factory=list)
    #: One line per entry that was dropped, e.g. ``"dimensions[3]: unknown id 'dim_9'"``.
    parse_issues: list[str] = Field(default_factory=list)


#: For each list: its model, the field that names the item, and what that name is called.
_LISTS: tuple[tuple[str, type[_Base], str, str], ...] = (
    ("dimensions", DimensionNote, "id", "id"),
    ("fcfs", FcfNote, "id", "id"),
    ("datums", DatumNote, "label", "datum label"),
    ("findings", FindingNote, "key", "finding key"),
)


def _known_names(drawing: Drawing, findings: list[Finding]) -> dict[str, set[str]]:
    """What each list's entries may name: the drawing's own ids, labels, and finding keys.

    Datums are named by label, and only labels that a datum feature symbol on the drawing
    actually carries count — a letter cited inside a frame but never defined is exactly
    the kind of thing a student should not be told a story about.
    """
    return {
        "dimensions": {d.id for d in drawing.dimensions if d.id},
        "fcfs": {f.id for f in drawing.fcfs if f.id},
        "datums": {d.label for d in drawing.datums if d.label},
        "findings": {finding_key(f) for f in findings},
    }


def _short_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first["loc"]) or "entry"
    if first["type"] == "missing":
        return f"missing {field}"
    return f"invalid {field}"


def parse_explanation(data: Any, drawing: Drawing, findings: list[Finding]) -> Explanation:
    """Validate the model's explanation against the drawing it is about.

    Lenient in the same way as :func:`draftcheck.schema.parse_drawing`: one bad entry is
    dropped and described, never the whole answer. An entry is dropped when it is not
    shaped right, when the id, label, or key it names is not on the drawing or among
    ``findings``, or when it names the same item as an earlier entry (the first wins).
    """
    if not isinstance(data, dict):
        return Explanation(parse_issues=["the explanation was not a JSON object"])

    issues: list[str] = []
    known = _known_names(drawing, findings)

    part = None
    raw_part = data.get("part")
    if isinstance(raw_part, dict):
        part = PartSummary.model_validate(raw_part)
    elif raw_part is not None:
        issues.append("part: not an object")

    views = _parse_list(data, "views", ViewNote, issues)
    kept_views = []
    for index, view in views:
        if view.name not in VIEW_NAMES:
            issues.append(f"views[{index}]: unknown view name {view.name!r}")
            continue
        kept_views.append(view)
    tables = [table for _, table in _parse_list(data, "tables", TableNote, issues)]

    raw_projection = data.get("projection")
    projection = _projection(raw_projection)
    said_unclear = isinstance(raw_projection, str) and raw_projection.strip().lower() == "unclear"
    if raw_projection is not None and projection == "unclear" and not said_unclear:
        issues.append(f"projection: unknown value {raw_projection!r}, shown as unclear")

    kept: dict[str, list[_Base]] = {}
    for key, model, name_field, what in _LISTS:
        raw = data.get(key)
        entries: list[_Base] = []
        seen: set[str] = set()
        if raw is not None and not isinstance(raw, list):
            issues.append(f"{key}: not a list")
            raw = []
        for index, item in enumerate(raw or []):
            try:
                entry = model.model_validate(item)
            except ValidationError as exc:
                issues.append(f"{key}[{index}]: {_short_error(exc)}")
                continue
            name = getattr(entry, name_field)
            if name not in known[key]:
                issues.append(f"{key}[{index}]: unknown {what} {name!r}")
                continue
            if name in seen:
                issues.append(f"{key}[{index}]: second entry for {name!r}")
                continue
            seen.add(name)
            entries.append(entry)
        kept[key] = entries

    described = {view.name for view in kept_views}
    kept["dimensions"] = [
        _check_dimension_views(entry, described, issues) for entry in kept["dimensions"]
    ]

    return Explanation(
        part=part,
        views=kept_views,
        projection=projection,
        projection_note=_optional_text(data.get("projection_note")),
        tables=tables,
        balloons=_optional_text(data.get("balloons")),
        parse_issues=issues,
        **kept,
    )


def _parse_list(
    data: dict, key: str, model: type[_Base], issues: list[str]
) -> list[tuple[int, Any]]:
    """Validate one list entry by entry; returns ``(index, entry)`` for those that parse."""
    raw = data.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        issues.append(f"{key}: not a list")
        return []
    entries = []
    for index, item in enumerate(raw):
        try:
            entries.append((index, model.model_validate(item)))
        except ValidationError as exc:
            issues.append(f"{key}[{index}]: {_short_error(exc)}")
    return entries


def _check_dimension_views(
    note: DimensionNote, described: set[str], issues: list[str]
) -> DimensionNote:
    """Keep only view names that the explanation's own ``views`` describe.

    The note itself stays — its purpose is about a real dimension — but a view it names
    that is not on the sheet is cleared and recorded, as is a repeat of its own view.
    """
    view = note.view.lower() if note.view else None
    if view is not None and view not in described:
        issues.append(f"dimensions[{note.id}]: unknown view {note.view!r}")
        view = None
    also: list[str] = []
    for name in note.also_seen_in:
        if name not in described:
            issues.append(f"dimensions[{note.id}]: unknown view {name!r} in also_seen_in")
        elif name != view and name not in also:
            also.append(name)
    return note.model_copy(update={"view": view, "also_seen_in": also})


def explanation_fingerprint(drawing: Drawing, findings: list[Finding]) -> str:
    """A short hash of exactly what an explanation is about, for the cache.

    The drawing's items and the findings' keys and messages, in a canonical form. The
    drawing's own ``parse_issues`` are left out: they describe how it was read, not what
    it says. Two requests with the same fingerprint would send the model the same data.
    """
    payload = {
        "version": EXPLANATION_VERSION,
        "drawing": drawing.model_dump(mode="json", exclude={"parse_issues"}),
        "findings": [[finding_key(f), f.message] for f in findings],
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class SupportsChatJSON(Protocol):
    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult: ...


def explain_drawing(
    drawing: Drawing, findings: list[Finding], page_png: bytes, client: SupportsChatJSON
) -> tuple[Explanation, JSONChatResult]:
    """Ask the model to explain one drawing, and validate the answer against it.

    One model call: the page image plus the drawing's data and the findings. The drawing's
    ``parse_issues`` are not sent — they are about how it was read, not what it shows.

    Raises:
        JSONParseError: If the reply has no usable JSON object (raised by the client).
    """
    drawing_json = json.dumps(
        drawing.model_dump(mode="json", exclude={"parse_issues"}), indent=2, ensure_ascii=False
    )
    result = client.chat_json(EXPLAIN_SYSTEM, explain_user(drawing_json, findings), [page_png])
    return parse_explanation(result.data, drawing, findings), result
