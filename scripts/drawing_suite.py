"""The evaluation suite: synthetic drawings, each with its ground truth and seeded errors.

Run with:  python -m scripts.make_sample_drawing --suite

Every drawing here is described as data — a :class:`DrawingSpec` naming the part's size,
its holes, dimensions, datum symbols, feature control frames, notes, and title block. One
renderer draws the PDF from that spec, and the ground truth is written from the same spec,
so a drawing and its ground truth cannot disagree.

Each ground truth uses ``draftcheck.schema`` field names (so it parses as a ``Drawing``),
plus ``bbox_convention`` and ``seeded_errors``. Every seeded error names the rule that must
catch it (``rule_id``) and the exact finding it must produce (``finding_key``, as
``schema.finding_key`` spells it). ``tests/test_eval_suite.py`` runs the rules on every
ground truth and requires exactly those findings and nothing else.

plate_001 is not built here; ``scripts/make_sample_drawing.py`` still owns it, unchanged.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

from reportlab.pdfgen import canvas as rl_canvas

from draftcheck.render import load_pages
from scripts.make_sample_drawing import (
    DATUM_BOX,
    DATUM_TRI_H,
    FONT,
    FONT_BOLD,
    OUT_DIR,
    PAGE_H,
    PAGE_W,
    Box,
    arrowhead,
    circled_letter,
    dashed_line,
    draw_border,
    horizontal_dim,
    norm_bbox,
    position_symbol,
    vertical_dim,
)

BBOX_CONVENTION = (
    "[x0, y0, x1, y1] normalized 0..1, origin top-left, covering the annotation's text or "
    "symbol"
)

TB_X0, TB_Y0, TB_X1, TB_Y1 = 512.0, 20.0, 772.0, 130.0
FCF_H = 26.0
NOTE_SIZE = 10.5


# --- Spec ------------------------------------------------------------------------------


@dataclass(frozen=True)
class View:
    """One orthographic view: where the part's bottom-left corner sits, and its scale."""

    x0: float
    y0: float
    #: Part size in drawing units (mm or inches).
    width: float
    height: float
    #: Page points per drawing unit.
    scale: float

    def x(self, u: float) -> float:
        return self.x0 + u * self.scale

    def y(self, v: float) -> float:
        return self.y0 + v * self.scale

    @property
    def x1(self) -> float:
        return self.x(self.width)

    @property
    def y1(self) -> float:
        return self.y(self.height)

    def edge(self, side: str) -> float:
        """The page coordinate of one outline edge: left/right are x, bottom/top are y."""
        return {"left": self.x0, "right": self.x1, "bottom": self.y0, "top": self.y1}[side]


@dataclass(frozen=True)
class Hole:
    """A through hole, in part coordinates (drawing units)."""

    x: float
    y: float
    dia: float


@dataclass(frozen=True)
class Dim:
    """A linear dimension between two part coordinates along one axis."""

    id: str
    text: str
    value: float
    #: "x" for a horizontal dimension, "y" for a vertical one.
    axis: str
    a: float
    b: float
    #: The outline edge the extension lines start from (top/bottom for x, left/right for y).
    side: str
    #: Page coordinate of the dimension line (y for a horizontal one, x for a vertical one).
    at: float
    from_ref: str
    to_ref: str
    basic: bool = False
    #: Symmetric tolerance printed with the value, or ``None`` when none is printed.
    tol: float | None = None
    #: Which view it is drawn on: "front" or "side".
    view: str = "front"


@dataclass(frozen=True)
class HoleNote:
    """A diameter callout on a leader, e.g. ``2X Ø8 ±0.1``."""

    id: str
    text: str
    value: float
    quantity: int
    tol: float | None
    #: Index into ``DrawingSpec.holes`` of the hole the leader points at.
    hole: int
    #: Page point where the leader bends, and where its horizontal tail ends.
    elbow: tuple[float, float]
    tail_x: float


@dataclass(frozen=True)
class DatumSymbol:
    """A datum feature symbol attached to one outline edge of a view."""

    id: str
    label: str
    edge: str
    #: Position along that edge, in part coordinates.
    at: float
    view: str = "front"


@dataclass(frozen=True)
class Frame:
    """A feature control frame placed at a page point, optionally with a leader."""

    id: str
    characteristic: str
    tolerance: float
    #: The tolerance as printed, without the diameter sign, e.g. "0.25" or ".010".
    tol_text: str
    diameter_zone: bool
    modifier: str | None
    datums: tuple[str, ...]
    x: float
    y: float
    #: Page point the leader arrow touches, or ``None`` for no leader.
    leader_to: tuple[float, float] | None = None


@dataclass(frozen=True)
class SeededError:
    """One deliberate error, and the finding the rule engine must produce for it."""

    rule_id: str
    finding_key: str
    description: str
    #: Id of the item whose box locates the error on the sheet.
    at_item: str


@dataclass(frozen=True)
class DrawingSpec:
    """Everything needed to draw one sheet and write its ground truth."""

    stem: str
    title: str
    drawing_number: str
    revision: str
    material: str
    #: Schema value: "mm" or "in".
    units: str
    units_text: str
    #: Blanket linear tolerance, or ``None`` to leave it out of the title block.
    general_tol: float | None
    general_tol_text: str | None
    scale_text: str
    front: View
    holes: tuple[Hole, ...]
    dims: tuple[Dim, ...]
    hole_notes: tuple[HoleNote, ...]
    datums: tuple[DatumSymbol, ...]
    frames: tuple[Frame, ...]
    seeded: tuple[SeededError, ...]
    side: View | None = None
    notes: tuple[str, ...] = ()
    notes_at: tuple[float, float] = (40.0, 578.0)
    extra_title_rows: tuple[str, ...] = ()


# --- Drawing primitives ----------------------------------------------------------------


def hidden_line(c: rl_canvas.Canvas, x0: float, y0: float, x1: float, y1: float) -> None:
    """A short-dash hidden line, for edges seen through the part."""
    c.saveState()
    c.setDash(4, 2.5)
    c.setLineWidth(0.7)
    c.line(x0, y0, x1, y1)
    c.restoreState()


def flatness_symbol(c: rl_canvas.Canvas, cx: float, cy: float) -> None:
    """A slanted parallelogram."""
    c.setLineWidth(0.8)
    path = c.beginPath()
    path.moveTo(cx - 7.0, cy - 3.5)
    path.lineTo(cx + 3.0, cy - 3.5)
    path.lineTo(cx + 7.0, cy + 3.5)
    path.lineTo(cx - 3.0, cy + 3.5)
    path.close()
    c.drawPath(path, stroke=1, fill=0)


def perpendicularity_symbol(c: rl_canvas.Canvas, cx: float, cy: float) -> None:
    """An upright line standing on a base line."""
    c.setLineWidth(0.8)
    c.line(cx - 6.0, cy - 5.0, cx + 6.0, cy - 5.0)
    c.line(cx, cy - 5.0, cx, cy + 6.0)


SYMBOLS = {
    "position": position_symbol,
    "flatness": flatness_symbol,
    "perpendicularity": perpendicularity_symbol,
}


def draw_frame(c: rl_canvas.Canvas, frame: Frame) -> Box:
    """Draw a feature control frame: symbol | tolerance [modifier] | datum letters."""
    tol_w = 58.0 if frame.modifier else 44.0
    widths = (24.0, tol_w, *([18.0] * len(frame.datums)))
    total = sum(widths)
    mid_y = frame.y + FCF_H / 2

    c.setLineWidth(0.9)
    c.rect(frame.x, frame.y, total, FCF_H, stroke=1, fill=0)
    edge = frame.x
    for width in widths[:-1]:
        edge += width
        c.line(edge, frame.y, edge, frame.y + FCF_H)

    SYMBOLS[frame.characteristic](c, frame.x + widths[0] / 2, mid_y)

    tol_x = frame.x + widths[0]
    c.setFont(FONT, 9.5)
    sign = "Ø" if frame.diameter_zone else ""
    c.drawString(tol_x + 6.0, mid_y - 3.2, f"{sign}{frame.tol_text}")
    if frame.modifier == "MMC":
        circled_letter(c, tol_x + tol_w - 11.0, mid_y, "M")

    datum_x = tol_x + tol_w
    for width, label in zip(widths[2:], frame.datums, strict=True):
        c.setFont(FONT, 9.5)
        c.drawCentredString(datum_x + width / 2, mid_y - 3.2, label)
        datum_x += width

    if frame.leader_to is not None:
        start_x, start_y = frame.x + total / 2, frame.y + FCF_H
        tip_x, tip_y = frame.leader_to
        c.setLineWidth(0.8)
        c.line(start_x, start_y, tip_x, tip_y)
        arrowhead(c, tip_x, tip_y, tip_x - start_x, tip_y - start_y)

    return Box(frame.x, frame.y, frame.x + total, frame.y + FCF_H)


def draw_datum(c: rl_canvas.Canvas, view: View, symbol: DatumSymbol) -> Box:
    """Draw a datum feature symbol: a filled triangle on the edge and a lettered box."""
    c.setLineWidth(0.9)
    half = DATUM_BOX / 2
    path = c.beginPath()
    if symbol.edge in ("bottom", "top"):
        x = view.x(symbol.at)
        apex = view.edge(symbol.edge)
        out = -1.0 if symbol.edge == "bottom" else 1.0
        base = apex + out * DATUM_TRI_H
        path.moveTo(x, apex)
        path.lineTo(x - 5.0, base)
        path.lineTo(x + 5.0, base)
        far = base + out * DATUM_BOX
        lo, hi = min(base, far), max(base, far)
        box = Box(x - half, lo, x + half, hi)
        outer = Box(x - half, min(lo, apex), x + half, max(hi, apex))
    else:
        y = view.y(symbol.at)
        apex = view.edge(symbol.edge)
        out = -1.0 if symbol.edge == "left" else 1.0
        base = apex + out * DATUM_TRI_H
        path.moveTo(apex, y)
        path.lineTo(base, y - 5.0)
        path.lineTo(base, y + 5.0)
        far = base + out * DATUM_BOX
        lo, hi = min(base, far), max(base, far)
        box = Box(lo, y - half, hi, y + half)
        outer = Box(min(lo, apex), y - half, max(hi, apex), y + half)
    path.close()
    c.drawPath(path, stroke=1, fill=1)
    c.rect(box.x0, box.y0, DATUM_BOX, DATUM_BOX, stroke=1, fill=0)
    c.setFont(FONT, 10)
    c.drawCentredString((box.x0 + box.x1) / 2, box.y0 + 5.5, symbol.label)
    return outer


def draw_outline(c: rl_canvas.Canvas, view: View) -> None:
    c.setLineWidth(1.4)
    c.rect(view.x0, view.y0, view.x1 - view.x0, view.y1 - view.y0, stroke=1, fill=0)


def draw_holes(c: rl_canvas.Canvas, view: View, holes: tuple[Hole, ...]) -> None:
    """Holes seen end-on, each with crossed centerlines."""
    for hole in holes:
        cx, cy = view.x(hole.x), view.y(hole.y)
        r = hole.dia * view.scale / 2
        c.setLineWidth(1.0)
        c.circle(cx, cy, r, stroke=1, fill=0)
        reach = r + 6.0
        dashed_line(c, cx - reach, cy, cx + reach, cy)
        dashed_line(c, cx, cy - reach, cx, cy + reach)


def draw_side_holes(c: rl_canvas.Canvas, side: View, holes: tuple[Hole, ...]) -> None:
    """Holes seen from the side: two hidden lines for the wall, a centerline between."""
    for hole in holes:
        cy = side.y(hole.y)
        r = hole.dia * side.scale / 2
        hidden_line(c, side.x0, cy - r, side.x1, cy - r)
        hidden_line(c, side.x0, cy + r, side.x1, cy + r)
        dashed_line(c, side.x0 - 5.0, cy, side.x1 + 5.0, cy)


def draw_dim(c: rl_canvas.Canvas, view: View, dim: Dim) -> Box:
    if dim.axis == "x":
        return horizontal_dim(
            c, view.x(dim.a), view.x(dim.b), dim.at, view.edge(dim.side), dim.text,
            basic=dim.basic,
        )
    return vertical_dim(
        c, view.y(dim.a), view.y(dim.b), dim.at, view.edge(dim.side), dim.text,
        basic=dim.basic,
    )


def draw_hole_note(c: rl_canvas.Canvas, view: View, hole: Hole, note: HoleNote) -> Box:
    """A leader from the hole's rim to a horizontal tail, with the callout above the tail."""
    cx, cy = view.x(hole.x), view.y(hole.y)
    r = hole.dia * view.scale / 2
    ex, ey = note.elbow
    length = math.hypot(ex - cx, ey - cy)
    sx, sy = cx + r * (ex - cx) / length, cy + r * (ey - cy) / length

    c.setLineWidth(0.8)
    c.line(sx, sy, ex, ey)
    c.line(ex, ey, note.tail_x, ey)
    arrowhead(c, sx, sy, sx - ex, sy - ey)

    c.setFont(FONT, NOTE_SIZE)
    text_x = min(ex, note.tail_x) + 2.0
    text_y = ey + 5.0
    c.drawString(text_x, text_y, note.text)
    width = c.stringWidth(note.text, FONT, NOTE_SIZE)
    return Box(text_x, text_y - 2.6, text_x + width, text_y + 8.2)


def draw_notes(c: rl_canvas.Canvas, spec: DrawingSpec) -> None:
    if not spec.notes:
        return
    x, y = spec.notes_at
    c.setFont(FONT_BOLD, 9.0)
    c.drawString(x, y, "NOTES:")
    c.setFont(FONT, 9.0)
    for index, note in enumerate(spec.notes, start=1):
        c.drawString(x, y - 12.0 * index, note)


def title_rows(spec: DrawingSpec) -> list[tuple[str, bool]]:
    rows = [
        (f"TITLE: {spec.title}", True),
        (f"DWG NO: {spec.drawing_number}          REV: {spec.revision}", False),
        (f"MATERIAL: {spec.material}", False),
        (f"UNITS: {spec.units_text}", False),
    ]
    if spec.general_tol_text:
        rows.append((spec.general_tol_text, False))
    rows.append((f"SCALE: {spec.scale_text}", False))
    rows.extend((row, False) for row in spec.extra_title_rows)
    return rows


def draw_title_block(c: rl_canvas.Canvas, spec: DrawingSpec) -> None:
    c.setLineWidth(1.2)
    c.rect(TB_X0, TB_Y0, TB_X1 - TB_X0, TB_Y1 - TB_Y0, stroke=1, fill=0)
    rows = title_rows(spec)
    row_h = (TB_Y1 - TB_Y0) / len(rows)
    for index, (text, bold) in enumerate(rows):
        top = TB_Y1 - index * row_h
        if index:
            c.setLineWidth(0.6)
            c.line(TB_X0, top, TB_X1, top)
        c.setFont(FONT_BOLD if bold else FONT, 8.5)
        c.drawString(TB_X0 + 6.0, top - row_h + (row_h - 8.5) / 2 + 1.5, text)


def _view(spec: DrawingSpec, name: str) -> View:
    if name == "side":
        if spec.side is None:
            raise ValueError(f"{spec.stem}: an item is drawn on a side view it does not have")
        return spec.side
    return spec.front


def draw_sheet(c: rl_canvas.Canvas, spec: DrawingSpec) -> dict[str, Box]:
    """Draw one whole sheet and return each item's box, keyed by item id."""
    boxes: dict[str, Box] = {}
    draw_border(c)
    draw_outline(c, spec.front)
    draw_holes(c, spec.front, spec.holes)
    if spec.side is not None:
        draw_outline(c, spec.side)
        draw_side_holes(c, spec.side, spec.holes)

    for dim in spec.dims:
        boxes[dim.id] = draw_dim(c, _view(spec, dim.view), dim)
    for note in spec.hole_notes:
        boxes[note.id] = draw_hole_note(c, spec.front, spec.holes[note.hole], note)
    for frame in spec.frames:
        boxes[frame.id] = draw_frame(c, frame)
    for symbol in spec.datums:
        boxes[symbol.id] = draw_datum(c, _view(spec, symbol.view), symbol)

    draw_notes(c, spec)
    draw_title_block(c, spec)
    return boxes


# --- Ground truth ----------------------------------------------------------------------


def _fcf_text(frame: Frame) -> str:
    sign = "Ø" if frame.diameter_zone else ""
    modifier = " (M)" if frame.modifier == "MMC" else ""
    parts = [frame.characteristic, f"{sign}{frame.tol_text}{modifier}", *frame.datums]
    return " | ".join(parts)


def ground_truth(spec: DrawingSpec, boxes: dict[str, Box]) -> dict:
    """What a perfect extraction of ``spec`` returns, plus the errors seeded into it."""
    general = None
    if spec.general_tol is not None:
        general = {"linear_plus_minus": spec.general_tol, "text_raw": spec.general_tol_text}

    dimensions = [
        {
            "id": dim.id,
            "text_raw": dim.text,
            "value": float(dim.value),
            "unit": None,
            "feature_type": "linear",
            "quantity": 1,
            "is_basic": dim.basic,
            "is_reference": False,
            "tol_plus": dim.tol,
            "tol_minus": dim.tol,
            "from_ref": dim.from_ref,
            "to_ref": dim.to_ref,
            "bbox": norm_bbox(boxes[dim.id]),
        }
        for dim in spec.dims
    ]
    dimensions += [
        {
            "id": note.id,
            "text_raw": note.text,
            "value": float(note.value),
            "unit": None,
            "feature_type": "diameter",
            "quantity": note.quantity,
            "is_basic": False,
            "is_reference": False,
            "tol_plus": note.tol,
            "tol_minus": note.tol,
            "from_ref": None,
            "to_ref": None,
            "bbox": norm_bbox(boxes[note.id]),
        }
        for note in spec.hole_notes
    ]

    return {
        "units": spec.units,
        "general_tolerance": general,
        "title_block": {
            "title": spec.title,
            "drawing_number": spec.drawing_number,
            "revision": spec.revision,
            "material": spec.material,
            "scale": spec.scale_text,
        },
        "bbox_convention": BBOX_CONVENTION,
        "datums": [
            {"id": symbol.id, "label": symbol.label, "bbox": norm_bbox(boxes[symbol.id])}
            for symbol in spec.datums
        ],
        "dimensions": dimensions,
        "fcfs": [
            {
                "id": frame.id,
                "text_raw": _fcf_text(frame),
                "characteristic": frame.characteristic,
                "tolerance": frame.tolerance,
                "diameter_zone": frame.diameter_zone,
                "material_modifier": frame.modifier,
                "datum_refs": [{"label": label, "modifier": None} for label in frame.datums],
                "bbox": norm_bbox(boxes[frame.id]),
            }
            for frame in spec.frames
        ],
        "notes": list(spec.notes),
        "seeded_errors": [
            {
                "id": f"err_{index}",
                "rule_id": error.rule_id,
                "finding_key": error.finding_key,
                "description": error.description,
                "bbox": norm_bbox(boxes[error.at_item]),
            }
            for index, error in enumerate(spec.seeded, start=1)
        ],
    }


# --- The suite -------------------------------------------------------------------------

_POSITION_AB = Frame(
    id="fcf_position_holes",
    characteristic="position",
    tolerance=0.25,
    tol_text="0.25",
    diameter_zone=True,
    modifier="MMC",
    datums=("A", "B"),
    x=60.0,
    y=160.0,
)

#: The plate the mm variants start from: plate_001's geometry, with a frame that is valid.
_PLATE = DrawingSpec(
    stem="plate_base",
    title="BASE PLATE",
    drawing_number="DC-0010",
    revision="A",
    material="AL 6061-T6",
    units="mm",
    units_text="MILLIMETERS",
    general_tol=0.1,
    general_tol_text="UNLESS OTHERWISE SPECIFIED: ±0.1",
    scale_text="1:1",
    front=View(200.0, 270.0, 100.0, 60.0, 4.0),
    holes=(Hole(20.0, 30.0, 8.0), Hole(80.0, 30.0, 8.0)),
    dims=(
        Dim("dim_overall_width", "100 ±0.2", 100, "x", 0, 100, "bottom", 225.0,
            "left_edge", "right_edge", tol=0.2),
        Dim("dim_overall_height", "60 ±0.2", 60, "y", 0, 60, "left", 150.0,
            "bottom_edge", "top_edge", tol=0.2),
        Dim("dim_hole1_x", "20", 20, "x", 0, 20, "top", 535.0,
            "left_edge", "hole_1_center", basic=True),
        Dim("dim_hole2_x", "80", 80, "x", 0, 80, "top", 562.0,
            "left_edge", "hole_2_center", basic=True),
        Dim("dim_hole_y", "30", 30, "y", 0, 30, "right", 645.0,
            "bottom_edge", "hole_centers", basic=True),
    ),
    hole_notes=(
        HoleNote("dim_hole_diameter", "2X Ø8 ±0.1", 8, 2, 0.1, 0, (120.0, 195.0), 60.0),
    ),
    datums=(
        DatumSymbol("datum_a", "A", "bottom", 40.0),
        DatumSymbol("datum_b", "B", "left", 37.5),
    ),
    frames=(_POSITION_AB,),
    seeded=(),
)


def _plate(stem: str, number: str, **changes: object) -> DrawingSpec:
    return replace(_PLATE, stem=stem, drawing_number=number, **changes)


PLATE_CLEAN = _plate("plate_clean", "DC-0011")

PLATE_DUP_DATUM = _plate(
    "plate_dup_datum",
    "DC-0012",
    datums=(*_PLATE.datums, DatumSymbol("datum_a_dup", "A", "top", 70.0)),
    seeded=(
        SeededError(
            "GDT-002",
            "GDT-002@datum_a,datum_a_dup",
            "Two datum feature symbols are both labelled A: one on the bottom edge and one "
            "on the top edge.",
            "datum_a_dup",
        ),
    ),
)

PLATE_FLATNESS_DATUM = _plate(
    "plate_flatness_datum",
    "DC-0013",
    frames=(
        _POSITION_AB,
        Frame("fcf_flatness", "flatness", 0.05, "0.05", False, None, ("A",), 60.0, 120.0,
              leader_to=(240.0, 270.0)),
    ),
    seeded=(
        SeededError(
            "GDT-003",
            "GDT-003@fcf_flatness",
            "The flatness frame references datum A. Flatness is a form control and takes "
            "no datum.",
            "fcf_flatness",
        ),
    ),
)

PLATE_NO_BASIC = _plate(
    "plate_no_basic",
    "DC-0014",
    dims=tuple(replace(dim, basic=False) for dim in _PLATE.dims),
    seeded=(
        SeededError(
            "GDT-004",
            "GDT-004@fcf_position_holes",
            "The holes carry a position tolerance, but their locating dimensions (20, 80, "
            "30) are plain dimensions, not basic ones.",
            "fcf_position_holes",
        ),
    ),
)

PLATE_NO_GENTOL = _plate(
    "plate_no_gentol",
    "DC-0015",
    general_tol=None,
    general_tol_text=None,
    dims=tuple(
        replace(dim, text="60", tol=None) if dim.id == "dim_overall_height" else dim
        for dim in _PLATE.dims
    ),
    seeded=(
        SeededError(
            "DIM-001",
            "DIM-001@dim_overall_height",
            "The title block gives no general tolerance, and the overall height 60 prints "
            "no tolerance of its own.",
            "dim_overall_height",
        ),
    ),
)

PLATE_EDGE_HOLE = _plate(
    "plate_edge_hole",
    "DC-0016",
    holes=(*_PLATE.holes, Hole(95.0, 45.0, 6.0)),
    dims=(
        *_PLATE.dims,
        Dim("dim_hole3_x", "5", 5, "x", 95, 100, "top", 535.0, "hole_3_center", "right_edge"),
        Dim("dim_hole3_y", "45", 45, "y", 0, 45, "right", 670.0, "bottom_edge",
            "hole_3_center"),
    ),
    hole_notes=(
        *_PLATE.hole_notes,
        HoleNote("dim_hole3_diameter", "Ø6 ±0.1", 6, 1, 0.1, 2, (630.0, 500.0), 672.0),
    ),
    seeded=(
        SeededError(
            "MFG-001",
            "MFG-001@dim_hole3_x",
            "Hole 3 (Ø6) has its centre 5 mm from the right edge, leaving a web far thinner "
            "than one hole diameter.",
            "dim_hole3_x",
        ),
    ),
)

BRACKET_01 = DrawingSpec(
    stem="bracket_01",
    title="MOUNTING BRACKET",
    drawing_number="DC-0102",
    revision="B",
    material="AL 6061-T6",
    units="mm",
    units_text="MILLIMETERS",
    general_tol=0.1,
    general_tol_text="UNLESS OTHERWISE SPECIFIED: ±0.1",
    scale_text="1:1",
    front=View(130.0, 280.0, 120.0, 60.0, 3.5),
    side=View(600.0, 280.0, 10.0, 60.0, 3.5),
    holes=(Hole(30.0, 30.0, 10.0), Hole(90.0, 30.0, 10.0), Hole(115.0, 40.0, 6.0)),
    dims=(
        Dim("dim_overall_width", "120 ±0.2", 120, "x", 0, 120, "bottom", 235.0,
            "left_edge", "right_edge", tol=0.2),
        Dim("dim_overall_height", "60 ±0.2", 60, "y", 0, 60, "left", 60.0,
            "bottom_edge", "top_edge", tol=0.2),
        Dim("dim_hole1_x", "30", 30, "x", 0, 30, "top", 515.0,
            "left_edge", "hole_1_center", basic=True),
        Dim("dim_hole_pitch", "60", 60, "x", 30, 90, "top", 515.0,
            "hole_1_center", "hole_2_center", basic=True),
        Dim("dim_hole2_x", "30", 30, "x", 90, 120, "top", 515.0,
            "hole_2_center", "right_edge", basic=True),
        Dim("dim_hole_y", "30", 30, "y", 0, 30, "left", 90.0,
            "bottom_edge", "hole_centers", basic=True),
        Dim("dim_hole3_x", "5", 5, "x", 115, 120, "bottom", 258.0,
            "hole_3_center", "right_edge"),
        Dim("dim_hole3_y", "40", 40, "y", 0, 40, "right", 572.0,
            "bottom_edge", "hole_3_center"),
        Dim("dim_thickness", "10", 10, "x", 0, 10, "top", 515.0,
            "front_face", "back_face", view="side"),
    ),
    hole_notes=(
        HoleNote("dim_hole_diameter", "2X Ø10 ±0.1", 10, 2, 0.1, 0, (110.0, 200.0), 46.0),
        HoleNote("dim_hole3_diameter", "Ø6", 6, 1, None, 2, (566.0, 470.0), 596.0),
    ),
    datums=(
        DatumSymbol("datum_b", "B", "bottom", 60.0),
        DatumSymbol("datum_a", "A", "right", 45.0, view="side"),
    ),
    frames=(
        Frame("fcf_position_holes", "position", 0.25, "0.25", True, "MMC", ("A", "B", "D"),
              46.0, 160.0),
        Frame("fcf_flatness", "flatness", 0.05, "0.05", False, None, ("A",), 590.0, 200.0,
              leader_to=(635.0, 320.0)),
    ),
    notes=(
        "1. BREAK ALL SHARP EDGES 0.2 MAX.",
        "2. DEBURR ALL HOLES.",
        "3. FINISH: CLEAR ANODIZE.",
    ),
    extra_title_rows=("DRAWN: DRAFTCHECK EVAL SUITE",),
    seeded=(
        SeededError(
            "GDT-001",
            "GDT-001@fcf_position_holes",
            "The position frame references datum D, but the drawing defines only A and B.",
            "fcf_position_holes",
        ),
        SeededError(
            "GDT-003",
            "GDT-003@fcf_flatness",
            "The flatness frame on the back face references datum A.",
            "fcf_flatness",
        ),
        SeededError(
            "MFG-001",
            "MFG-001@dim_hole3_x",
            "The Ø6 hole has its centre 5 mm from the right edge.",
            "dim_hole3_x",
        ),
    ),
)

PLATE_INCH_MULTI = DrawingSpec(
    stem="plate_inch_multi",
    title="COVER PLATE",
    drawing_number="DC-0203",
    revision="A",
    material="STEEL 1018",
    units="in",
    units_text="INCHES",
    general_tol=0.01,
    general_tol_text="UNLESS OTHERWISE SPECIFIED: ±.010",
    scale_text="1:1",
    front=View(210.0, 260.0, 4.0, 3.0, 90.0),
    holes=(
        Hole(0.5, 0.5, 0.25),
        Hole(3.5, 0.5, 0.25),
        Hole(0.5, 2.5, 0.25),
        Hole(3.5, 2.5, 0.25),
    ),
    dims=(
        Dim("dim_overall_width", "4.00 ±.010", 4.0, "x", 0, 4.0, "bottom", 215.0,
            "left_edge", "right_edge", tol=0.01),
        Dim("dim_overall_height", "3.00 ±.010", 3.0, "y", 0, 3.0, "left", 150.0,
            "bottom_edge", "top_edge", tol=0.01),
        Dim("dim_hole1_x", ".50", 0.5, "x", 0, 0.5, "top", 548.0,
            "left_edge", "hole_1_center", basic=True),
        Dim("dim_hole2_x", "3.50", 3.5, "x", 0, 3.5, "top", 570.0,
            "left_edge", "hole_2_center", basic=True),
        Dim("dim_hole1_y", ".50", 0.5, "y", 0, 0.5, "right", 615.0,
            "bottom_edge", "hole_1_center", basic=True),
        Dim("dim_hole3_y", "2.50", 2.5, "y", 0, 2.5, "right", 640.0,
            "bottom_edge", "hole_3_center", basic=True),
    ),
    hole_notes=(
        HoleNote("dim_hole_diameter", "4X Ø.250 ±.005", 0.25, 4, 0.005, 0, (140.0, 200.0),
                 50.0),
    ),
    datums=(
        DatumSymbol("datum_a", "A", "bottom", 2.0),
        DatumSymbol("datum_b", "B", "left", 1.5),
        DatumSymbol("datum_b_dup", "B", "right", 1.5),
    ),
    frames=(
        Frame("fcf_position_holes", "position", 0.01, ".010", True, "MMC", ("A", "B"),
              50.0, 160.0),
        Frame("fcf_perpendicularity_holes", "perpendicularity", 0.005, ".005", False, None,
              ("A",), 50.0, 134.0),
    ),
    seeded=(
        SeededError(
            "GDT-002",
            "GDT-002@datum_b,datum_b_dup",
            "Datum B is placed twice: on the left edge and again on the right edge.",
            "datum_b_dup",
        ),
    ),
)

#: Every drawing in the suite, by stem, in the order the eval runs them.
SUITE: dict[str, DrawingSpec] = {
    spec.stem: spec
    for spec in (
        PLATE_CLEAN,
        PLATE_DUP_DATUM,
        PLATE_FLATNESS_DATUM,
        PLATE_NO_BASIC,
        PLATE_NO_GENTOL,
        PLATE_EDGE_HOLE,
        BRACKET_01,
        PLATE_INCH_MULTI,
    )
}


# --- Build -----------------------------------------------------------------------------


def build_one(spec: DrawingSpec, out_dir: Path, *, write_png: bool = True) -> dict[str, Path]:
    """Draw one spec to ``<stem>.pdf`` and write ``<stem>.gt.json`` (and ``<stem>.png``)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"{spec.stem}.pdf"
    json_path = out_dir / f"{spec.stem}.gt.json"

    # invariant=1 leaves the creation date out, so rebuilding an unchanged spec gives a
    # byte-identical PDF and git shows no change.
    c = rl_canvas.Canvas(str(pdf_path), pagesize=(PAGE_W, PAGE_H), invariant=1)
    c.setTitle(f"{spec.title} {spec.drawing_number}")
    boxes = draw_sheet(c, spec)
    c.showPage()
    c.save()

    truth = ground_truth(spec, boxes)
    json_path.write_text(json.dumps(truth, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    paths = {"pdf": pdf_path, "ground_truth": json_path}

    if write_png:
        png_path = out_dir / f"{spec.stem}.png"
        png_path.write_bytes(load_pages(pdf_path, dpi=200)[0].png_bytes)
        paths["png"] = png_path
    return paths


def build_suite(
    out_dir: Path | str = OUT_DIR, *, write_png: bool = True
) -> dict[str, dict[str, Path]]:
    """Build every drawing in :data:`SUITE`. Returns the written paths per stem."""
    out_dir = Path(out_dir)
    return {stem: build_one(spec, out_dir, write_png=write_png) for stem, spec in SUITE.items()}
