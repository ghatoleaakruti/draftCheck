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
from itertools import pairwise
from pathlib import Path

from reportlab.pdfgen import canvas as rl_canvas

from draftcheck.render import load_pages
from scripts.make_sample_drawing import (
    DATUM_BOX,
    DATUM_TRI_H,
    DIM_SIZE,
    EXT_GAP,
    EXT_OVER,
    FONT,
    FONT_BOLD,
    OUT_DIR,
    PAGE_H,
    PAGE_W,
    Box,
    arrowhead,
    boxed_text,
    centered_text,
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
    #: "third" draws the third-angle projection symbol in the title block; ``None`` leaves
    #: it out (the original eight sheets predate it and stay byte-identical).
    projection: str | None = None
    notes_heading: str = "NOTES:"


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


def circular_runout_symbol(c: rl_canvas.Canvas, cx: float, cy: float) -> None:
    """A single arrow pointing up and to the right."""
    c.setLineWidth(0.8)
    x0, y0, x1, y1 = cx - 5.0, cy - 5.0, cx + 5.0, cy + 5.0
    c.line(x0, y0, x1, y1)
    arrowhead(c, x1, y1, 1.0, 1.0)


def profile_surface_symbol(c: rl_canvas.Canvas, cx: float, cy: float) -> None:
    """A closed half circle: an arc standing on a base line."""
    c.setLineWidth(0.8)
    r = 6.5
    c.arc(cx - r, cy - 4.0 - r, cx + r, cy - 4.0 + r, 0, 180)
    c.line(cx - r, cy - 4.0, cx + r, cy - 4.0)


SYMBOLS = {
    "position": position_symbol,
    "profile_surface": profile_surface_symbol,
    "flatness": flatness_symbol,
    "perpendicularity": perpendicularity_symbol,
    "circular_runout": circular_runout_symbol,
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
        tip_x, tip_y = frame.leader_to
        # The leader leaves the frame's top edge, or its bottom edge when the feature it
        # points at is below the frame, so it never runs back across the frame.
        start_x = frame.x + total / 2
        start_y = frame.y if tip_y < frame.y else frame.y + FCF_H
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


def draw_notes(c: rl_canvas.Canvas, spec: DrawingSpec | PartSpec) -> None:
    if not spec.notes:
        return
    x, y = spec.notes_at
    c.setFont(FONT_BOLD, 9.0)
    c.drawString(x, y, spec.notes_heading)
    c.setFont(FONT, 9.0)
    for index, note in enumerate(spec.notes, start=1):
        c.drawString(x, y - 12.0 * index, note)


def title_rows(spec: DrawingSpec | PartSpec) -> list[tuple[str, bool]]:
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


#: Width of the title-block cell that holds the projection symbol.
PROJECTION_CELL_W = 72.0


def projection_symbol(c: rl_canvas.Canvas, x0: float, y0: float, x1: float, y1: float) -> None:
    """The third-angle symbol in a cell: a truncated cone seen from the side and end-on.

    Third angle: the side view (a trapezoid) is on the left with its small end facing the
    end view (two concentric circles) on the right.
    """
    cy = y0 + (y1 - y0) * 0.62
    left = x0 + 8.0
    small, large = 5.0, 10.0
    c.setLineWidth(0.9)
    path = c.beginPath()
    path.moveTo(left, cy - large)
    path.lineTo(left + 24.0, cy - small)
    path.lineTo(left + 24.0, cy + small)
    path.lineTo(left, cy + large)
    path.close()
    c.drawPath(path, stroke=1, fill=0)
    circle_x = left + 46.0
    c.circle(circle_x, cy, large, stroke=1, fill=0)
    c.circle(circle_x, cy, small, stroke=1, fill=0)
    dashed_line(c, left - 4.0, cy, circle_x + large + 4.0, cy)
    dashed_line(c, circle_x, cy - large - 4.0, circle_x, cy + large + 4.0)
    c.setFont(FONT, 6.5)
    mid = (x0 + x1) / 2
    c.drawCentredString(mid, y0 + 16.0, "THIRD ANGLE")
    c.drawCentredString(mid, y0 + 8.0, "PROJECTION")


def draw_title_block(c: rl_canvas.Canvas, spec: DrawingSpec | PartSpec) -> None:
    c.setLineWidth(1.2)
    c.rect(TB_X0, TB_Y0, TB_X1 - TB_X0, TB_Y1 - TB_Y0, stroke=1, fill=0)
    rows_x1 = TB_X1
    if spec.projection == "third":
        rows_x1 = TB_X1 - PROJECTION_CELL_W
        c.setLineWidth(0.9)
        c.line(rows_x1, TB_Y0, rows_x1, TB_Y1)
        projection_symbol(c, rows_x1, TB_Y0, TB_X1, TB_Y1)
    elif spec.projection is not None:
        raise ValueError(f"{spec.stem}: unknown projection {spec.projection!r}")
    rows = title_rows(spec)
    row_h = (TB_Y1 - TB_Y0) / len(rows)
    for index, (text, bold) in enumerate(rows):
        top = TB_Y1 - index * row_h
        if index:
            c.setLineWidth(0.6)
            c.line(TB_X0, top, rows_x1, top)
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


# --- Part recipes: views, shapes, and point-to-point dimensions -------------------------
#
# The plate recipe above knows one shape: a rectangle with round holes. Gears, nuts, and
# bolts need more, so a :class:`PartSpec` describes a sheet as named orthographic views,
# a list of shapes drawn in each view's own part coordinates (mm, origin wherever is
# natural for the part, often the axis), dimensions between any two feature points, and
# leader callouts. The title block, notes, and frames are shared with the plate recipe.

#: Line weight and dash pattern for each line type. "center" is the thin chain line used
#: for centrelines and pitch circles; "thin" is for thread roots and chamfer circles.
LINE_STYLES: dict[str, tuple[float, tuple[float, ...] | None]] = {
    "visible": (1.4, None),
    "thin": (0.6, None),
    "hidden": (0.7, (4.0, 2.5)),
    "center": (0.5, (12.0, 3.0, 2.0, 3.0)),
}

Point = tuple[float, float]


@dataclass(frozen=True)
class PartView:
    """One named orthographic view: where its part-coordinate origin sits on the page."""

    name: str
    #: What kind of view it is: "front", "top", "right_side", "left_side".
    kind: str
    ox: float
    oy: float
    #: Page points per mm (72 / 25.4 x the drawing scale).
    scale: float
    #: Lines printed under a section or detail view, e.g. ("SECTION A-A",), centred on
    #: ``label_at`` (page point of the first line's baseline). The first line is bold.
    label: tuple[str, ...] = ()
    label_at: Point | None = None

    def x(self, u: float) -> float:
        return self.ox + u * self.scale

    def y(self, v: float) -> float:
        return self.oy + v * self.scale

    def pt(self, u: float, v: float) -> Point:
        return self.x(u), self.y(v)


@dataclass(frozen=True)
class Shape:
    """One piece of part geometry in one view.

    ``kind`` is "path" (``points``, optionally ``closed``), "circle" (``points[0]`` is the
    centre, ``r`` the radius), "arc" (centre, radius, ``start`` and ``extent`` in degrees,
    counter-clockwise), "bow" (a smooth curve from ``points[0]`` through ``points[1]``
    to ``points[2]``, used for the chamfer curves on hex flats), "hatch" (45° section
    lining clipped to the closed polygon ``points``), or "text" (``text`` centred on
    ``points[0]``, e.g. the letter beside a detail circle).
    """

    view: str
    style: str
    kind: str
    points: tuple[Point, ...]
    closed: bool = False
    r: float = 0.0
    start: float = 0.0
    extent: float = 360.0
    text: str = ""


def hatch(view: str, *points: Point) -> Shape:
    return Shape(view, "thin", "hatch", tuple(points), closed=True)


def label_text(view: str, at: Point, text: str) -> Shape:
    return Shape(view, "thin", "text", (at,), text=text)


def path(view: str, style: str, *points: Point, closed: bool = False) -> Shape:
    return Shape(view, style, "path", tuple(points), closed=closed)


def circle(view: str, style: str, centre: Point, r: float) -> Shape:
    return Shape(view, style, "circle", (centre,), r=r)


def arc(view: str, style: str, centre: Point, r: float, start: float, extent: float) -> Shape:
    return Shape(view, style, "arc", (centre,), r=r, start=start, extent=extent)


def bow(view: str, style: str, start: Point, apex: Point, end: Point) -> Shape:
    return Shape(view, style, "bow", (start, apex, end))


def centre_cross(
    view: str, centre: Point, reach: float, reach_v: float | None = None
) -> tuple[Shape, Shape]:
    """Crossed centrelines through ``centre``: ``reach`` mm across, ``reach_v`` up and down."""
    u, v = centre
    up = reach if reach_v is None else reach_v
    return (
        path(view, "center", (u - reach, v), (u + reach, v)),
        path(view, "center", (u, v - up), (u, v + up)),
    )


@dataclass(frozen=True)
class PointDim:
    """A horizontal ("x") or vertical ("y") dimension between two feature points.

    Each extension line starts at its own feature point, so the two ends may sit on
    different edges (a bolt's bearing face and its chamfered tip, say). ``at`` is the
    part coordinate of the dimension line: v for a horizontal one, u for a vertical one.
    """

    id: str
    text: str
    value: float
    view: str
    axis: str
    a: Point
    b: Point
    at: float
    from_ref: str
    to_ref: str
    tol: float | None = None
    feature_type: str = "linear"
    reference: bool = False
    basic: bool = False
    #: A datum feature symbol in line with the dimension line, beyond its ``b``-side
    #: end: that names the feature of size (its axis) as the datum.
    datum: str | None = None


@dataclass(frozen=True)
class AngleDim:
    """An angle between two rays from ``vertex``, drawn as an arc of ``radius`` mm.

    ``start`` and ``end`` are the rays' directions in degrees, counter-clockwise from +u.
    """

    id: str
    text: str
    value: float
    view: str
    vertex: Point
    radius: float
    start: float
    end: float
    from_ref: str
    to_ref: str
    tol: float | None = None


@dataclass(frozen=True)
class SurfaceDatum:
    """A datum feature symbol standing on a surface: triangle base on the edge at ``at``.

    ``direction`` is the way the symbol points away from the part: up, down, left, right.
    """

    id: str
    label: str
    view: str
    at: Point
    direction: str


@dataclass(frozen=True)
class CuttingPlane:
    """A section's cutting-plane line, from ``a`` to ``b`` in part coordinates.

    ``sight`` is the direction of sight as a page unit vector, e.g. ``(-1, 0)`` for
    looking left; both ends turn into arrows that way, each tagged with ``label``.
    """

    view: str
    a: Point
    b: Point
    label: str
    sight: Point


@dataclass(frozen=True)
class Callout:
    """A leader from a feature to a horizontal shoulder with text on it, e.g. ``M10x1.5``.

    With ``datum`` set, a datum feature symbol hangs from the shoulder: that is how a
    datum is put on a feature of size (the bore of a gear, say) rather than on a surface.
    """

    id: str
    text: str
    value: float
    view: str
    #: Part point the arrow touches.
    target: Point
    #: Page point where the leader bends, and where the shoulder ends.
    elbow: Point
    tail_x: float
    feature_type: str
    tol: float | None = None
    quantity: int = 1
    reference: bool = False
    datum: str | None = None
    basic: bool = False
    #: Page point where the datum symbol's triangle sits, when it should hang from
    #: something other than the middle of the shoulder (the bottom of a frame, say).
    datum_at: Point | None = None


@dataclass(frozen=True)
class PartSpec:
    """A sheet for a part that is not a plate: views, shapes, dimensions, callouts."""

    stem: str
    title: str
    drawing_number: str
    revision: str
    material: str
    units: str
    units_text: str
    general_tol: float | None
    general_tol_text: str | None
    scale_text: str
    #: What the part is, e.g. "spur_gear"; written to the ground truth as
    #: ``expected_part_type``.
    part_type: str
    views: tuple[PartView, ...]
    shapes: tuple[Shape, ...]
    dims: tuple[PointDim, ...]
    callouts: tuple[Callout, ...] = ()
    frames: tuple[Frame, ...] = ()
    seeded: tuple[SeededError, ...] = ()
    notes: tuple[str, ...] = ()
    notes_at: tuple[float, float] = (40.0, 578.0)
    extra_title_rows: tuple[str, ...] = ()
    angles: tuple[AngleDim, ...] = ()
    surface_datums: tuple[SurfaceDatum, ...] = ()
    cutting_planes: tuple[CuttingPlane, ...] = ()
    projection: str | None = "third"
    notes_heading: str = "GENERAL NOTES:"

    def view(self, name: str) -> PartView:
        for view in self.views:
            if view.name == name:
                return view
        raise ValueError(f"{self.stem}: no view named {name!r}")


# --- Part geometry ---------------------------------------------------------------------


def polar(r: float, degrees: float, centre: Point = (0.0, 0.0)) -> Point:
    angle = math.radians(degrees)
    return centre[0] + r * math.cos(angle), centre[1] + r * math.sin(angle)


def arc_points(r: float, start: float, end: float, steps: int = 24) -> list[Point]:
    """Points on a circle about the origin from ``start`` to ``end`` degrees (inclusive)."""
    return [polar(r, start + (end - start) * i / steps) for i in range(steps + 1)]


def hexagon(across_flats: float, *, pointy_top: bool) -> list[Point]:
    """A regular hexagon about the origin, corners first at 0° (flat top) or 90°."""
    r = across_flats / math.sqrt(3.0)  # the corner radius: across corners / 2
    first = 90.0 if pointy_top else 0.0
    return [polar(r, first + 60.0 * k) for k in range(6)]


def _involute(alpha: float) -> float:
    return math.tan(alpha) - alpha


def spur_gear_outline(
    teeth: int, module: float, pressure_deg: float = 20.0, flank_steps: int = 6
) -> list[Point]:
    """The tooth outline of a standard spur gear about the origin, as one closed polygon.

    Addendum 1 module, dedendum 1.25 modules, involute flanks above the base circle and
    radial flanks below it. The first tooth is centred on the +v axis (straight up).
    """
    rp = module * teeth / 2
    ra = rp + module
    rf = rp - 1.25 * module
    rb = rp * math.cos(math.radians(pressure_deg))
    inv_p = _involute(math.radians(pressure_deg))

    def half_thickness(r: float) -> float:
        """Half the tooth's angular thickness at radius ``r`` (radians)."""
        r = max(r, rb)
        return math.pi / (2 * teeth) + inv_p - _involute(math.acos(rb / r))

    flank = [rf + (ra - rf) * i / flank_steps for i in range(flank_steps + 1)]
    pitch = 2 * math.pi / teeth
    points: list[Point] = []
    for k in range(teeth):
        centre = math.pi / 2 + k * pitch
        # Up the leading flank (clockwise side), across the tip, down the trailing flank.
        for r in flank:
            a = centre - half_thickness(r)
            points.append((r * math.cos(a), r * math.sin(a)))
        for r in reversed(flank):
            a = centre + half_thickness(r)
            points.append((r * math.cos(a), r * math.sin(a)))
        # Root arc to the next tooth.
        gap_start = centre + half_thickness(rf)
        gap_end = centre + pitch - half_thickness(rf)
        for i in range(1, 4):
            a = gap_start + (gap_end - gap_start) * i / 4
            points.append((rf * math.cos(a), rf * math.sin(a)))
    return points


def keyed_circle(dia: float, key_width: float, key_flat: float) -> list[Point]:
    """A circle with a keyway at the top, as one closed outline.

    ``key_flat`` is the keyway's flat, measured up from the centre. For a bore (the flat
    outside the circle) it outlines the hole; for a shaft (the flat inside) the solid.
    """
    r = dia / 2
    half = key_width / 2
    edge = math.degrees(math.atan2(math.sqrt(r * r - half * half), half))
    # Clockwise from the right side of the keyway, round the bottom, to the left side.
    points = arc_points(r, edge, 180.0 - edge - 360.0, steps=40)
    points += [(-half, key_flat), (half, key_flat)]
    return points


def hex_face_bows(
    view: str,
    corners: list[float],
    ends: tuple[tuple[float, float], ...],
    axis: str,
) -> list[Shape]:
    """The chamfer curves on each visible hex face of a nut or bolt head.

    ``corners`` are the face edges across the part, in order. Each of ``ends`` is
    ``(end, inward)``: the end face's position along the axis, and how far (signed, toward
    the part) the curve dips at the face edges. ``axis`` is "v" when the part's axis runs
    up the page, "u" when it runs across.
    """
    shapes: list[Shape] = []
    for a, b in pairwise(corners):
        mid = (a + b) / 2
        for end, inward in ends:
            pts = ((a, end + inward), (mid, end), (b, end + inward))
            if axis == "u":
                pts = tuple((v, u) for u, v in pts)
            shapes.append(bow(view, "visible", *pts))
    return shapes


# --- Part drawing ----------------------------------------------------------------------


def draw_shape(c: rl_canvas.Canvas, view: PartView, shape: Shape) -> None:
    width, dash = LINE_STYLES[shape.style]
    c.saveState()
    c.setLineWidth(width)
    if dash:
        c.setDash(list(dash), 0)
    if shape.kind == "path":
        p = c.beginPath()
        p.moveTo(*view.pt(*shape.points[0]))
        for point in shape.points[1:]:
            p.lineTo(*view.pt(*point))
        if shape.closed:
            p.close()
        c.drawPath(p, stroke=1, fill=0)
    elif shape.kind in ("circle", "arc"):
        cx, cy = view.pt(*shape.points[0])
        r = shape.r * view.scale
        if shape.kind == "circle":
            c.circle(cx, cy, r, stroke=1, fill=0)
        else:
            c.arc(cx - r, cy - r, cx + r, cy + r, shape.start, shape.extent)
    elif shape.kind == "bow":
        # A quadratic through the apex, written as the equivalent cubic.
        (x0, y0), (xa, ya), (x2, y2) = (view.pt(*p) for p in shape.points)
        qx, qy = 2 * xa - (x0 + x2) / 2, 2 * ya - (y0 + y2) / 2
        p = c.beginPath()
        p.moveTo(x0, y0)
        p.curveTo(
            x0 + 2 / 3 * (qx - x0), y0 + 2 / 3 * (qy - y0),
            x2 + 2 / 3 * (qx - x2), y2 + 2 / 3 * (qy - y2),
            x2, y2,
        )
        c.drawPath(p, stroke=1, fill=0)
    elif shape.kind == "hatch":
        draw_hatch(c, [view.pt(*point) for point in shape.points])
    elif shape.kind == "text":
        x, y = view.pt(*shape.points[0])
        c.setFont(FONT_BOLD, 11.0)
        c.drawCentredString(x, y - 4.0, shape.text)
    else:
        raise ValueError(f"unknown shape kind {shape.kind!r}")
    c.restoreState()


#: Gap between section-lining lines, measured square to the lines, in page points.
HATCH_PITCH = 4.0


def draw_hatch(c: rl_canvas.Canvas, points: list[Point]) -> None:
    """45° section lining inside a closed polygon given in page points."""
    clip = c.beginPath()
    clip.moveTo(*points[0])
    for point in points[1:]:
        clip.lineTo(*point)
    clip.close()
    c.saveState()
    c.clipPath(clip, stroke=0, fill=0)
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    lo_y, hi_y = min(ys), max(ys)
    step = HATCH_PITCH * math.sqrt(2.0)
    offset = min(xs) - hi_y
    while offset < max(xs) - lo_y:
        # The line y = x - offset, across the polygon's whole height.
        c.line(lo_y + offset, lo_y, hi_y + offset, hi_y)
        offset += step
    c.restoreState()


def vertical_label(c: rl_canvas.Canvas, x_line: float, y: float, text: str) -> Box:
    """Dimension text reading bottom-to-top, just left of a vertical dimension line."""
    size = DIM_SIZE
    width = c.stringWidth(text, FONT, size)
    baseline = x_line - 3.0
    c.saveState()
    c.setFont(FONT, size)
    c.translate(baseline, y)
    c.rotate(90)
    c.drawCentredString(0, 0, text)
    c.restoreState()
    return Box(baseline - 0.78 * size, y - width / 2, baseline + 0.25 * size, y + width / 2)


#: How far the dimension line runs past each arrow on a dimension too narrow for its text.
NARROW_RUN = 14.0


def _extension(c: rl_canvas.Canvas, x0: float, y0: float, x1: float, y1: float) -> None:
    """One extension line, skipped when the feature already sits on the dimension line."""
    if math.hypot(x1 - x0, y1 - y0) > EXT_GAP + EXT_OVER + 1.0:
        c.line(x0, y0, x1, y1)


def draw_point_dim(c: rl_canvas.Canvas, view: PartView, dim: PointDim) -> Box:
    """Draw one dimension; returns its text box (and records nothing else)."""
    box, _ = draw_point_dim_with_datum(c, view, dim)
    return box


def draw_point_dim_with_datum(
    c: rl_canvas.Canvas, view: PartView, dim: PointDim
) -> tuple[Box, Box | None]:
    (ax, ay), (bx, by) = view.pt(*dim.a), view.pt(*dim.b)
    c.setLineWidth(0.7)
    datum_box = None
    if dim.axis == "x":
        y_dim = view.y(dim.at)
        for px, py in ((ax, ay), (bx, by)):
            sign = 1.0 if y_dim > py else -1.0
            _extension(c, px, py + sign * EXT_GAP, px, y_dim + sign * EXT_OVER)
        lo, hi = sorted((ax, bx))
        width = c.stringWidth(dim.text, FONT, DIM_SIZE)
        if dim.datum is not None:
            datum_box = surface_datum(c, bx, y_dim, "right" if bx >= ax else "left", dim.datum)
            c.setLineWidth(0.7)
        if width + 6.0 <= hi - lo:
            c.line(lo, y_dim, hi, y_dim)
            arrowhead(c, lo, y_dim, -1, 0)
            arrowhead(c, hi, y_dim, 1, 0)
            draw = boxed_text if dim.basic else centered_text
            return draw(c, (lo + hi) / 2, y_dim + 4.0, dim.text, DIM_SIZE), datum_box
        # Too narrow for the text: arrows outside pointing in, text beyond the right one.
        c.line(lo - NARROW_RUN, y_dim, hi + NARROW_RUN, y_dim)
        arrowhead(c, lo, y_dim, 1, 0)
        arrowhead(c, hi, y_dim, -1, 0)
        text_x = hi + NARROW_RUN + 2.0
        draw = boxed_text if dim.basic else centered_text
        return draw(c, text_x + width / 2, y_dim - 3.5, dim.text, DIM_SIZE), datum_box
    if dim.axis == "y":
        x_dim = view.x(dim.at)
        for px, py in ((ax, ay), (bx, by)):
            sign = 1.0 if x_dim > px else -1.0
            _extension(c, px + sign * EXT_GAP, py, x_dim + sign * EXT_OVER, py)
        lo, hi = sorted((ay, by))
        c.line(x_dim, lo, x_dim, hi)
        arrowhead(c, x_dim, lo, 0, -1)
        arrowhead(c, x_dim, hi, 0, 1)
        if dim.datum is not None:
            datum_box = surface_datum(c, x_dim, by, "up" if by >= ay else "down", dim.datum)
        box = vertical_label(c, x_dim, (lo + hi) / 2, dim.text)
        if dim.basic:
            c.setLineWidth(0.7)
            c.rect(box.x0 - 3.0, box.y0 - 4.0, box.x1 - box.x0 + 6.0, box.y1 - box.y0 + 8.0)
            box = Box(box.x0 - 3.0, box.y0 - 4.0, box.x1 + 3.0, box.y1 + 4.0)
        return box, datum_box
    raise ValueError(f"{dim.id}: axis must be 'x' or 'y', not {dim.axis!r}")


def draw_angle_dim(c: rl_canvas.Canvas, view: PartView, dim: AngleDim) -> Box:
    """An angle: extension lines along both rays, an arc between them, arrows, text."""
    vx, vy = view.pt(*dim.vertex)
    r = dim.radius * view.scale
    c.setLineWidth(0.7)
    for angle in (dim.start, dim.end):
        ux, uy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        c.line(vx + ux * EXT_GAP, vy + uy * EXT_GAP, vx + ux * (r + EXT_OVER),
               vy + uy * (r + EXT_OVER))
    c.arc(vx - r, vy - r, vx + r, vy + r, dim.start, dim.end - dim.start)
    a0, a1 = math.radians(dim.start), math.radians(dim.end)
    arrowhead(c, vx + r * math.cos(a0), vy + r * math.sin(a0), math.sin(a0), -math.cos(a0))
    arrowhead(c, vx + r * math.cos(a1), vy + r * math.sin(a1), -math.sin(a1), math.cos(a1))

    mid = math.radians((dim.start + dim.end) / 2)
    tx, ty = vx + (r + 6.0) * math.cos(mid), vy + (r + 6.0) * math.sin(mid) - 3.5
    c.setFont(FONT, DIM_SIZE)
    width = c.stringWidth(dim.text, FONT, DIM_SIZE)
    if math.cos(mid) < 0:
        tx -= width
    c.drawString(tx, ty, dim.text)
    return Box(tx, ty - 0.25 * DIM_SIZE, tx + width, ty + 0.78 * DIM_SIZE)


def surface_datum(c: rl_canvas.Canvas, x: float, y: float, direction: str, label: str) -> Box:
    """A datum feature symbol whose triangle's base sits at (x, y), pointing ``direction``.

    Filled triangle, a short stem, then the lettered box. Returns the whole symbol's box.
    """
    if direction == "down":
        return hanging_datum(c, x, y, label)
    dx, dy = {"up": (0.0, 1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}[direction]
    px, py = -dy, dx  # along the base
    tri_h = 8.0
    c.setLineWidth(0.9)
    p = c.beginPath()
    p.moveTo(x - 5.0 * px, y - 5.0 * py)
    p.lineTo(x + 5.0 * px, y + 5.0 * py)
    p.lineTo(x + tri_h * dx, y + tri_h * dy)
    p.close()
    c.drawPath(p, stroke=1, fill=1)
    stem = tri_h + 4.0
    c.line(x + tri_h * dx, y + tri_h * dy, x + stem * dx, y + stem * dy)
    half = DATUM_BOX / 2
    cx, cy = x + (stem + half) * dx, y + (stem + half) * dy
    c.rect(cx - half, cy - half, DATUM_BOX, DATUM_BOX, stroke=1, fill=0)
    c.setFont(FONT, 10)
    c.drawCentredString(cx, cy - 3.5, label)
    xs = (x - 5.0 * abs(px), x + 5.0 * abs(px), cx - half, cx + half)
    ys = (y - 5.0 * abs(py), y + 5.0 * abs(py), cy - half, cy + half)
    return Box(min(xs), min(ys), max(xs), max(ys))


def draw_cutting_plane(c: rl_canvas.Canvas, view: PartView, plane: CuttingPlane) -> None:
    """Thick chain line; each end turns 90° into an arrow along the sight, with a letter."""
    (ax, ay), (bx, by) = view.pt(*plane.a), view.pt(*plane.b)
    sx, sy = plane.sight
    c.saveState()
    c.setLineWidth(1.3)
    c.setDash([18.0, 3.0, 3.0, 3.0], 0)
    c.line(ax, ay, bx, by)
    c.restoreState()
    arm = 16.0
    c.setLineWidth(1.3)
    for ex, ey in ((ax, ay), (bx, by)):
        tip_x, tip_y = ex + sx * arm, ey + sy * arm
        c.line(ex, ey, tip_x, tip_y)
        arrowhead(c, tip_x, tip_y, sx, sy)
        c.setFont(FONT_BOLD, 11.0)
        c.drawCentredString(tip_x + sx * 9.0, tip_y + sy * 9.0 - 4.0, plane.label)


def draw_view_label(c: rl_canvas.Canvas, view: PartView) -> None:
    if not view.label or view.label_at is None:
        return
    x, y = view.label_at
    for index, line in enumerate(view.label):
        c.setFont(FONT_BOLD if index == 0 else FONT, 10.0 if index == 0 else 9.0)
        c.drawCentredString(x, y - 12.0 * index, line)


def hanging_datum(c: rl_canvas.Canvas, x: float, y: float, label: str) -> Box:
    """A datum feature symbol below a leader shoulder: triangle base on the line, box below."""
    tri_h = 8.0
    c.setLineWidth(0.9)
    p = c.beginPath()
    p.moveTo(x - 5.0, y)
    p.lineTo(x + 5.0, y)
    p.lineTo(x, y - tri_h)
    p.close()
    c.drawPath(p, stroke=1, fill=1)
    box_top = y - tri_h - 4.0
    c.line(x, y - tri_h, x, box_top)
    half = DATUM_BOX / 2
    c.rect(x - half, box_top - DATUM_BOX, DATUM_BOX, DATUM_BOX, stroke=1, fill=0)
    c.setFont(FONT, 10)
    c.drawCentredString(x, box_top - DATUM_BOX + 5.5, label)
    return Box(x - half, box_top - DATUM_BOX, x + half, y)


def draw_callout(
    c: rl_canvas.Canvas, view: PartView, callout: Callout
) -> tuple[Box, Box | None]:
    """Draw a leader callout; returns its text box and its datum symbol's box (if any)."""
    tx, ty = view.pt(*callout.target)
    ex, ey = callout.elbow
    c.setLineWidth(0.8)
    c.line(tx, ty, ex, ey)
    c.line(ex, ey, callout.tail_x, ey)
    arrowhead(c, tx, ty, tx - ex, ty - ey)

    c.setFont(FONT, NOTE_SIZE)
    text_x = min(ex, callout.tail_x) + (6.0 if callout.basic else 2.0)
    text_y = ey + (8.0 if callout.basic else 5.0)
    c.drawString(text_x, text_y, callout.text)
    width = c.stringWidth(callout.text, FONT, NOTE_SIZE)
    text_box = Box(text_x, text_y - 2.6, text_x + width, text_y + 8.2)
    if callout.basic:
        text_box = Box(text_box.x0 - 3.0, text_box.y0 - 1.5, text_box.x1 + 3.0,
                       text_box.y1 + 1.5)
        c.setLineWidth(0.7)
        c.rect(text_box.x0, text_box.y0, text_box.x1 - text_box.x0,
               text_box.y1 - text_box.y0, stroke=1, fill=0)

    datum_box = None
    if callout.datum is not None:
        dx, dy = callout.datum_at or ((ex + callout.tail_x) / 2, ey)
        datum_box = hanging_datum(c, dx, dy, callout.datum)
    return text_box, datum_box


def datum_id(label: str) -> str:
    return f"datum_{label.lower()}"


def draw_part_sheet(c: rl_canvas.Canvas, spec: PartSpec) -> dict[str, Box]:
    """Draw one part sheet and return each item's box, keyed by item id."""
    boxes: dict[str, Box] = {}
    draw_border(c)
    for shape in spec.shapes:
        draw_shape(c, spec.view(shape.view), shape)
    for plane in spec.cutting_planes:
        draw_cutting_plane(c, spec.view(plane.view), plane)
    for view in spec.views:
        draw_view_label(c, view)
    for dim in spec.dims:
        text_box, datum_box = draw_point_dim_with_datum(c, spec.view(dim.view), dim)
        boxes[dim.id] = text_box
        if datum_box is not None and dim.datum is not None:
            boxes[datum_id(dim.datum)] = datum_box
    for angle in spec.angles:
        boxes[angle.id] = draw_angle_dim(c, spec.view(angle.view), angle)
    for symbol in spec.surface_datums:
        boxes[symbol.id] = surface_datum(
            c, *spec.view(symbol.view).pt(*symbol.at), symbol.direction, symbol.label
        )
    for callout in spec.callouts:
        text_box, datum_box = draw_callout(c, spec.view(callout.view), callout)
        boxes[callout.id] = text_box
        if datum_box is not None and callout.datum is not None:
            boxes[datum_id(callout.datum)] = datum_box
    for frame in spec.frames:
        boxes[frame.id] = draw_frame(c, frame)
    draw_notes(c, spec)
    draw_title_block(c, spec)
    return boxes


def _dimension_record(
    *,
    id: str,
    text: str,
    value: float,
    feature_type: str,
    quantity: int,
    reference: bool,
    tol: float | None,
    basic: bool = False,
    from_ref: str | None,
    to_ref: str | None,
    box: Box,
    view: str,
) -> dict:
    return {
        "id": id,
        "text_raw": text,
        "value": float(value),
        "unit": None,
        "feature_type": feature_type,
        "quantity": quantity,
        "is_basic": basic,
        "is_reference": reference,
        "tol_plus": tol,
        "tol_minus": tol,
        "from_ref": from_ref,
        "to_ref": to_ref,
        "bbox": norm_bbox(box),
        "view": view,
    }


def part_ground_truth(spec: PartSpec, boxes: dict[str, Box]) -> dict:
    """What a perfect extraction of a part sheet returns, plus its seeded errors.

    Beyond the schema fields: ``expected_part_type``, ``views`` (name and kind of each),
    and a ``view`` on every dimension naming the view it is placed in.
    """
    general = None
    if spec.general_tol is not None:
        general = {"linear_plus_minus": spec.general_tol, "text_raw": spec.general_tol_text}

    dimensions = [
        _dimension_record(
            id=dim.id, text=dim.text, value=dim.value, feature_type=dim.feature_type,
            quantity=1, reference=dim.reference, tol=dim.tol, from_ref=dim.from_ref,
            to_ref=dim.to_ref, box=boxes[dim.id], view=dim.view, basic=dim.basic,
        )
        for dim in spec.dims
    ]
    dimensions += [
        _dimension_record(
            id=co.id, text=co.text, value=co.value, feature_type=co.feature_type,
            quantity=co.quantity, reference=co.reference, tol=co.tol, from_ref=None,
            to_ref=None, box=boxes[co.id], view=co.view, basic=co.basic,
        )
        for co in spec.callouts
    ]
    dimensions += [
        _dimension_record(
            id=a.id, text=a.text, value=a.value, feature_type="angular", quantity=1,
            reference=False, tol=a.tol, from_ref=a.from_ref, to_ref=a.to_ref,
            box=boxes[a.id], view=a.view,
        )
        for a in spec.angles
    ]
    labels = [co.datum for co in spec.callouts if co.datum is not None]
    labels += [dim.datum for dim in spec.dims if dim.datum is not None]
    datums = [
        {"id": datum_id(label), "label": label, "bbox": norm_bbox(boxes[datum_id(label)])}
        for label in labels
    ]
    datums += [
        {"id": symbol.id, "label": symbol.label, "bbox": norm_bbox(boxes[symbol.id])}
        for symbol in spec.surface_datums
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
        "expected_part_type": spec.part_type,
        "views": [{"name": view.name, "kind": view.kind} for view in spec.views],
        "bbox_convention": BBOX_CONVENTION,
        "datums": datums,
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


# --- Part drawings (step 26a): gear, nut, bolt ----------------------------------------

#: Page points per mm at full size.
PT_PER_MM_1TO1 = 72.0 / 25.4


def _per_mm(scale: float) -> float:
    return PT_PER_MM_1TO1 * scale


# Spur gear: 24 teeth, module 2 -> pitch Ø48, outside Ø52, root Ø43. Bore Ø16 with a
# 5 mm keyway (18.3 over bore and keyway). Face 20, hub Ø30 projecting 12 behind.
_G_SCALE = _per_mm(1.5)
_G_FRONT = PartView("front", "front", 230.0, 330.0, _G_SCALE)
_G_SIDE = PartView("side", "right_side", 430.0, 330.0, _G_SCALE)
_G_KEY_TOP = 18.3 - 8.0  # keyway flat above the bore centre

GEAR_SPUR = PartSpec(
    stem="gear_spur",
    title="SPUR GEAR 24T M2",
    drawing_number="DC-0301",
    revision="A",
    material="STEEL 4140",
    units="mm",
    units_text="MILLIMETERS",
    general_tol=0.1,
    general_tol_text="UNLESS OTHERWISE SPECIFIED: ±0.1",
    scale_text="1.5:1",
    part_type="spur_gear",
    views=(_G_FRONT, _G_SIDE),
    shapes=(
        # Front: teeth, pitch circle as a thin chain line, keyed bore, centrelines.
        path("front", "visible", *spur_gear_outline(24, 2.0), closed=True),
        circle("front", "center", (0.0, 0.0), 24.0),
        path("front", "visible", *keyed_circle(16.0, 5.0, _G_KEY_TOP), closed=True),
        *centre_cross("front", (0.0, 0.0), 28.5),
        # Side: gear blank and hub as one outline, pitch line, root, bore and keyway hidden.
        path("side", "visible", (0, -26), (20, -26), (20, -15), (32, -15), (32, 15),
             (20, 15), (20, 26), (0, 26), closed=True),
        path("side", "center", (-1.5, 24), (21.5, 24)),
        path("side", "center", (-1.5, -24), (21.5, -24)),
        path("side", "hidden", (0, 21.5), (20, 21.5)),
        path("side", "hidden", (0, -21.5), (20, -21.5)),
        path("side", "hidden", (0, 8), (32, 8)),
        path("side", "hidden", (0, -8), (32, -8)),
        path("side", "hidden", (0, _G_KEY_TOP), (32, _G_KEY_TOP)),
        path("side", "center", (-3, 0), (35, 0)),
    ),
    dims=(
        PointDim("dim_keyway_width", "5 ±0.02", 5, "front", "x", (-2.5, _G_KEY_TOP),
                 (2.5, _G_KEY_TOP), 14.5, "keyway_left_side", "keyway_right_side", tol=0.02),
        PointDim("dim_keyway_depth", "18.3 ±0.1", 18.3, "front", "y", (-2.5, _G_KEY_TOP),
                 (0, -8), -12.0, "bore_bottom", "keyway_flat", tol=0.1),
        PointDim("dim_outside_diameter", "Ø52 ±0.05", 52, "side", "y", (0, -26), (0, 26),
                 -6.0, "outside_diameter", "outside_diameter", tol=0.05,
                 feature_type="diameter"),
        PointDim("dim_hub_diameter", "Ø30 ±0.1", 30, "side", "y", (32, -15), (32, 15),
                 37.0, "hub_diameter", "hub_diameter", tol=0.1, feature_type="diameter"),
        PointDim("dim_face_width", "20 ±0.1", 20, "side", "x", (0, 26), (20, 26), 31.0,
                 "front_face", "rear_face", tol=0.1),
        PointDim("dim_overall_length", "32 ±0.1", 32, "side", "x", (0, 26), (32, 15), 37.0,
                 "front_face", "hub_end_face", tol=0.1),
    ),
    callouts=(
        Callout("dim_bore_diameter", "Ø16 ±0.01", 16, "front", polar(8.0, 290.0),
                (265.0, 192.0), 330.0, "diameter", tol=0.01, datum="A"),
        Callout("dim_pitch_diameter", "(Ø48)", 48, "front", polar(24.0, 135.0),
                (110.0, 470.0), 62.0, "diameter", reference=True),
    ),
    frames=(
        Frame("fcf_runout_od", "circular_runout", 0.05, "0.05", False, None, ("A",),
              415.0, 150.0, leader_to=_G_SIDE.pt(10.0, -26.0)),
    ),
    notes=(
        "1. 24 TEETH, MODULE 2.",
        "2. PRESSURE ANGLE 20°, STANDARD ADDENDUM.",
        "3. HEAT TREAT TO 28-32 HRC.",
        "4. BREAK SHARP EDGES 0.2 MAX.",
    ),
)

# Hex nut M10x1.5: across flats 16, across corners 18.48 (theoretical), thickness 8.4.
_N_SCALE = _per_mm(4.0)
_N_TOP = PartView("top", "top", 380.0, 410.0, _N_SCALE)
_N_FRONT = PartView("front", "front", 380.0, 185.0, _N_SCALE)
_N_R = 16.0 / math.sqrt(3.0)  # corner radius
_N_T = 8.4
_N_BOW = 0.6
_N_MINOR = (10.0 - 1.0825 * 1.5) / 2  # tapped minor radius
_N_TOP_EDGE = 0.75 * _N_R  # where the chamfer circle meets the outer faces

HEX_NUT = PartSpec(
    stem="hex_nut",
    title="HEX NUT M10",
    drawing_number="DC-0302",
    revision="A",
    material="MEDIUM CARBON STEEL",
    units="mm",
    units_text="MILLIMETERS",
    general_tol=0.2,
    general_tol_text="UNLESS OTHERWISE SPECIFIED: ±0.2",
    scale_text="4:1",
    part_type="hex_nut",
    views=(_N_TOP, _N_FRONT),
    shapes=(
        # Top: hexagon (corners left and right), bearing chamfer circle, thread circles
        # (minor as a full circle, major as a thin three-quarter arc).
        path("top", "visible", *hexagon(16.0, pointy_top=False), closed=True),
        circle("top", "visible", (0.0, 0.0), 7.3),
        circle("top", "visible", (0.0, 0.0), _N_MINOR),
        arc("top", "thin", (0.0, 0.0), 5.0, 90.0, 270.0),
        *centre_cross("top", (0.0, 0.0), _N_R + 1.2, 8.0 + 1.2),
        # Front: three faces, chamfer curves at both ends, thread hidden, centreline.
        path("front", "visible", (-_N_R, _N_BOW), (-_N_R, _N_T - _N_BOW)),
        path("front", "visible", (_N_R, _N_BOW), (_N_R, _N_T - _N_BOW)),
        path("front", "visible", (-_N_R / 2, _N_BOW), (-_N_R / 2, _N_T - _N_BOW)),
        path("front", "visible", (_N_R / 2, _N_BOW), (_N_R / 2, _N_T - _N_BOW)),
        path("front", "visible", (-_N_TOP_EDGE, _N_T), (_N_TOP_EDGE, _N_T)),
        path("front", "visible", (-_N_TOP_EDGE, 0), (_N_TOP_EDGE, 0)),
        *hex_face_bows("front", [-_N_R, -_N_R / 2, _N_R / 2, _N_R],
                       ((_N_T, -_N_BOW), (0.0, _N_BOW)), "v"),
        path("front", "hidden", (-_N_MINOR, 0), (-_N_MINOR, _N_T)),
        path("front", "hidden", (_N_MINOR, 0), (_N_MINOR, _N_T)),
        path("front", "center", (0, -1.5), (0, _N_T + 1.5)),
    ),
    dims=(
        PointDim("dim_across_corners", "(18.48)", 18.48, "top", "x", (-_N_R, 0), (_N_R, 0),
                 10.5, "corner_left", "corner_right", reference=True),
        PointDim("dim_across_flats", "16", 16, "top", "y", (_N_R / 2, 8), (_N_R / 2, -8),
                 _N_R + 3.5, "flat_bottom", "flat_top"),
        PointDim("dim_thickness", "8.4", 8.4, "front", "y", (_N_R, 0), (_N_R, _N_T),
                 _N_R + 3.5, "bottom_face", "top_face"),
    ),
    callouts=(
        Callout("dim_thread", "M10x1.5", 10, "top", polar(5.0, 225.0), (250.0, 330.0),
                188.0, "thread"),
    ),
    notes=(
        "1. THREAD TOLERANCE CLASS 6H.",
        "2. CHAMFER BOTH FACES 30°.",
        "3. PROPERTY CLASS 8.",
        "4. FINISH: ZINC PLATE.",
    ),
)

# Hex bolt M8x1.25 x 40: across flats 13, head height 5.3, thread length 22, 45° tip
# chamfer down to the thread minor diameter. Length is under the head, as is standard.
_B_SCALE = _per_mm(2.0)
_B_FRONT = PartView("front", "front", 235.0, 330.0, _B_SCALE)
_B_END = PartView("end", "left_side", 130.0, 330.0, _B_SCALE)
_B_R = 13.0 / math.sqrt(3.0)
_B_K = 5.3
_B_TIP = _B_K + 40.0
_B_THREAD = _B_TIP - 22.0
_B_MINOR = (8.0 - 1.226 * 1.25) / 2
_B_CHAMFER = _B_TIP - (4.0 - _B_MINOR)
_B_BOW = 0.5

HEX_BOLT = PartSpec(
    stem="hex_bolt",
    title="HEX BOLT M8 x 40",
    drawing_number="DC-0303",
    revision="A",
    material="ALLOY STEEL",
    units="mm",
    units_text="MILLIMETERS",
    general_tol=0.2,
    general_tol_text="UNLESS OTHERWISE SPECIFIED: ±0.2",
    scale_text="2:1",
    part_type="hex_bolt",
    views=(_B_FRONT, _B_END),
    shapes=(
        # Front, head: corners top and bottom, two face edges, chamfer curves on top.
        path("front", "visible", (_B_BOW, _B_R), (_B_K, _B_R)),
        path("front", "visible", (_B_BOW, -_B_R), (_B_K, -_B_R)),
        path("front", "visible", (_B_BOW, _B_R / 2), (_B_K, _B_R / 2)),
        path("front", "visible", (_B_BOW, -_B_R / 2), (_B_K, -_B_R / 2)),
        path("front", "visible", (_B_K, -_B_R), (_B_K, _B_R)),
        path("front", "visible", (0, -0.75 * _B_R), (0, 0.75 * _B_R)),
        *hex_face_bows("front", [-_B_R, -_B_R / 2, _B_R / 2, _B_R], ((0.0, _B_BOW),), "u"),
        # Front, shank: outline with the chamfered tip, thread end, thread roots.
        path("front", "visible", (_B_K, 4), (_B_CHAMFER, 4), (_B_TIP, _B_MINOR),
             (_B_TIP, -_B_MINOR), (_B_CHAMFER, -4), (_B_K, -4)),
        path("front", "visible", (_B_CHAMFER, -4), (_B_CHAMFER, 4)),
        path("front", "visible", (_B_THREAD, -4), (_B_THREAD, 4)),
        path("front", "thin", (_B_THREAD, _B_MINOR), (_B_TIP, _B_MINOR)),
        path("front", "thin", (_B_THREAD, -_B_MINOR), (_B_TIP, -_B_MINOR)),
        path("front", "center", (-1.5, 0), (_B_TIP + 2.0, 0)),
        # End view, looking at the head: hexagon (corners up and down), chamfer circle.
        path("end", "visible", *hexagon(13.0, pointy_top=True), closed=True),
        circle("end", "visible", (0.0, 0.0), 6.2),
        *centre_cross("end", (0.0, 0.0), _B_R + 1.5),
    ),
    dims=(
        PointDim("dim_head_height", "5.3", 5.3, "front", "x", (0, _B_R), (_B_K, _B_R),
                 _B_R + 4.0, "head_top", "bearing_face"),
        PointDim("dim_thread_length", "22", 22, "front", "x", (_B_THREAD, 4),
                 (_B_TIP, _B_MINOR), _B_R + 4.0, "thread_end", "bolt_tip"),
        PointDim("dim_length", "40", 40, "front", "x", (_B_K, -_B_R), (_B_TIP, -_B_MINOR),
                 -_B_R - 4.0, "bearing_face", "bolt_tip"),
        PointDim("dim_across_flats", "13", 13, "end", "x", (-6.5, -_B_R / 2),
                 (6.5, -_B_R / 2), -_B_R - 4.0, "flat_left", "flat_right"),
    ),
    callouts=(
        Callout("dim_thread", "M8x1.25", 8, "front", (42.0, -4.0), (530.0, 240.0), 592.0,
                "thread"),
    ),
    notes=(
        "1. THREAD TOLERANCE CLASS 6g.",
        "2. PROPERTY CLASS 8.8, MARK ON HEAD.",
        "3. FINISH: ZINC PLATE.",
        "4. LENGTH IS MEASURED UNDER THE HEAD.",
    ),
)


# --- Part drawings (step 26b): flange, stepped shaft, gripper claw ---------------------


def rim_toward(view: PartView, centre: Point, r: float, elbow: Point) -> Point:
    """The point on a circle's rim (part coords) facing a page point, for a leader tip."""
    cx, cy = view.pt(*centre)
    dx, dy = elbow[0] - cx, elbow[1] - cy
    length = math.hypot(dx, dy)
    return centre[0] + r * dx / length, centre[1] + r * dy / length


def _mirror_v(points: tuple[Point, ...]) -> tuple[Point, ...]:
    return tuple((u, -v) for u, v in points)


# Flange: OD Ø100, raised face Ø56 x 3 high, body 12 thick (15 overall), bore Ø32,
# 6X Ø9 on a basic Ø76 PCD. The section is cut on the vertical centreline, through two
# holes, and seen from the right.
_F_SCALE = _per_mm(1.0)
_F_FRONT = PartView("front", "front", 190.0, 320.0, _F_SCALE)
_F_SECTION = PartView("section_a_a", "section", 450.0, 320.0, _F_SCALE,
                      label=("SECTION A-A",), label_at=(466.0, 138.0))
_F_HOLE_ANGLES = (90.0, 150.0, 210.0, 270.0, 330.0, 30.0)
_F_HOLES = tuple(polar(38.0, angle) for angle in _F_HOLE_ANGLES)
#: Section regions, upper half: raised face and body inside the hole, and outside it.
_F_INNER = ((0, 16), (0, 28), (3, 28), (3, 33.5), (15, 33.5), (15, 16))
_F_OUTER = ((3, 42.5), (3, 50), (15, 50), (15, 42.5))
_F_HOLE_ELBOW = (80.0, 150.0)
_F_BORE_ELBOW = (300.0, 160.0)
_F_POSITION = Frame("fcf_position_holes", "position", 0.25, "0.25", True, "MMC",
                    ("A", "B", "C"), 24.0, 118.0)
_F_PERP = Frame("fcf_perpendicularity_bore", "perpendicularity", 0.05, "0.05", True, None,
                ("A",), 302.0, 128.0)

FLANGE = PartSpec(
    stem="flange",
    title="FLANGE 6-BOLT",
    drawing_number="DC-0304",
    revision="A",
    material="CARBON STEEL A105",
    units="mm",
    units_text="MILLIMETERS",
    general_tol=0.2,
    general_tol_text="UNLESS OTHERWISE SPECIFIED: ±0.2",
    scale_text="1:1",
    part_type="flange",
    views=(_F_FRONT, _F_SECTION),
    shapes=(
        circle("front", "visible", (0.0, 0.0), 50.0),
        circle("front", "visible", (0.0, 0.0), 28.0),
        circle("front", "visible", (0.0, 0.0), 16.0),
        circle("front", "center", (0.0, 0.0), 38.0),
        *(circle("front", "visible", hole, 4.5) for hole in _F_HOLES),
        *(line for hole in _F_HOLES for line in centre_cross("front", hole, 6.5)),
        *centre_cross("front", (0.0, 0.0), 54.0),
        *(hatch("section_a_a", *region) for region in
          (_F_INNER, _F_OUTER, _mirror_v(_F_INNER), _mirror_v(_F_OUTER))),
        *(path("section_a_a", "visible", *region, closed=True) for region in
          (_F_INNER, _F_OUTER, _mirror_v(_F_INNER), _mirror_v(_F_OUTER))),
        path("section_a_a", "center", (-2, 0), (17, 0)),
        path("section_a_a", "center", (1, 38), (17, 38)),
        path("section_a_a", "center", (1, -38), (17, -38)),
    ),
    cutting_planes=(CuttingPlane("front", (0.0, 56.0), (0.0, -56.0), "A", (-1.0, 0.0)),),
    dims=(
        PointDim("dim_outside_diameter", "Ø100 ±0.2", 100, "section_a_a", "y", (3, -50),
                 (3, 50), -14.0, "outside_diameter", "outside_diameter", tol=0.2,
                 feature_type="diameter"),
        PointDim("dim_raised_face_diameter", "Ø56 ±0.1", 56, "section_a_a", "y", (0, -28),
                 (0, 28), -6.0, "raised_face_diameter", "raised_face_diameter", tol=0.1,
                 feature_type="diameter"),
        PointDim("dim_overall_thickness", "15 ±0.1", 15, "section_a_a", "x", (0, 28),
                 (15, 50), 56.0, "raised_face", "back_face", tol=0.1),
        PointDim("dim_raised_face_height", "3 ±0.05", 3, "section_a_a", "x", (0, -28),
                 (3, -50), -56.0, "raised_face", "flange_front_face", tol=0.05),
    ),
    callouts=(
        Callout("dim_hole_diameter", "6X Ø9 ±0.1", 9, "front",
                rim_toward(_F_FRONT, _F_HOLES[2], 4.5, _F_HOLE_ELBOW), _F_HOLE_ELBOW, 22.0,
                "diameter", tol=0.1, quantity=6),
        Callout("dim_bore_diameter", "Ø32 ±0.05", 32, "front",
                rim_toward(_F_FRONT, (0.0, 0.0), 16.0, _F_BORE_ELBOW), _F_BORE_ELBOW, 390.0,
                "diameter", tol=0.05, datum="B",
                datum_at=(_F_PERP.x + 43.0, _F_PERP.y)),
        Callout("dim_bolt_circle", "Ø76", 76, "front", polar(38.0, 120.0), (75.0, 490.0),
                30.0, "diameter", basic=True),
    ),
    frames=(_F_POSITION, _F_PERP),
    surface_datums=(SurfaceDatum("datum_a", "A", "section_a_a", (15.0, -25.0), "right"),),
    notes=(
        "1. 6 HOLES EQUALLY SPACED ON THE BASIC BOLT CIRCLE.",
        "2. RAISED FACE FINISH Ra 3.2.",
        "3. BREAK SHARP EDGES 0.2 MAX.",
        "4. FINISH: ZINC PLATE.",
    ),
    seeded=(
        SeededError(
            "GDT-001",
            "GDT-001@fcf_position_holes",
            "The hole position frame references datum C, but the drawing defines only A "
            "(the back face) and B (the bore).",
            "fcf_position_holes",
        ),
    ),
)

# Stepped shaft: Ø25 x 35 drive end with an 8 mm keyway 25 long, Ø40 x 20 collar,
# Ø30 x 45 bearing journal; 1 x 45° chamfers, R1 fillet at the Ø25 shoulder.
_S_FRONT = PartView("front", "front", 60.0, 380.0, _per_mm(1.0))
_S_SECTION = PartView("section_b_b", "section", 480.0, 380.0, _per_mm(2.0),
                      label=("SECTION B-B", "SCALE 2:1"), label_at=(480.0, 285.0))
_S_DETAIL_C = (34.5, 13.0)
_S_DETAIL_R = 4.5
_S_DETAIL_SCALE = _per_mm(4.0)
_S_DETAIL = PartView("detail_c", "detail", 230.0 - _S_DETAIL_C[0] * _S_DETAIL_SCALE,
                     200.0 - _S_DETAIL_C[1] * _S_DETAIL_SCALE, _S_DETAIL_SCALE,
                     label=("DETAIL C", "SCALE 4:1"), label_at=(230.0, 133.0))
_S_KEY_WALL = math.sqrt(12.5**2 - 4.0**2)  # where the keyway walls meet the Ø25
_S_CHAMFER_ELBOW = (395.0, 240.0)
#: Where the detail circle crosses the Ø25 surface and the shoulder face.
_S_DETAIL_SPAN = math.sqrt(_S_DETAIL_R**2 - 0.5**2)


def _fillet(centre: Point, start: float, end: float) -> list[Point]:
    return [polar(1.0, start + (end - start) * i / 6, centre) for i in range(7)]


STEPPED_SHAFT = PartSpec(
    stem="stepped_shaft",
    title="STEPPED SHAFT",
    drawing_number="DC-0305",
    revision="A",
    material="STEEL 1045",
    units="mm",
    units_text="MILLIMETERS",
    general_tol=None,
    general_tol_text=None,
    scale_text="1:1",
    part_type="stepped_shaft",
    views=(_S_FRONT, _S_SECTION, _S_DETAIL),
    shapes=(
        path("front", "visible",
             (0, 11.5), (1, 12.5), (5, 12.5), (5, 8.5), (30, 8.5), (30, 12.5),
             *_fillet((34, 13.5), -90.0, 0.0), (35, 20), (55, 20), (55, 15), (99, 15),
             (100, 14), (100, -14), (99, -15), (55, -15), (55, -20), (35, -20),
             *_fillet((34, -13.5), 0.0, 90.0), (1, -12.5), (0, -11.5), closed=True),
        path("front", "visible", (1, -12.5), (1, 12.5)),
        path("front", "visible", (35, -13.5), (35, 13.5)),
        path("front", "visible", (55, -15), (55, 15)),
        path("front", "visible", (99, -15), (99, 15)),
        path("front", "visible", (5, _S_KEY_WALL), (30, _S_KEY_WALL)),
        path("front", "center", (-3, 0), (103, 0)),
        circle("front", "thin", _S_DETAIL_C, _S_DETAIL_R),
        label_text("front", (40.0, 24.0), "C"),
        # Section B-B: the Ø25 cut through the keyway, hatched.
        hatch("section_b_b", *keyed_circle(25.0, 8.0, 8.5)),
        path("section_b_b", "visible", *keyed_circle(25.0, 8.0, 8.5), closed=True),
        *centre_cross("section_b_b", (0.0, 0.0), 15.0),
        # Detail C: the shoulder fillet, inside its boundary circle.
        circle("detail_c", "thin", _S_DETAIL_C, _S_DETAIL_R),
        path("detail_c", "visible", (_S_DETAIL_C[0] - _S_DETAIL_SPAN, 12.5), (34, 12.5)),
        arc("detail_c", "visible", (34.0, 13.5), 1.0, 270.0, 90.0),
        path("detail_c", "visible", (35, 13.5), (35, _S_DETAIL_C[1] + _S_DETAIL_SPAN)),
        path("detail_c", "visible", (35, _S_DETAIL_C[1] - _S_DETAIL_SPAN), (35, 13.5)),
    ),
    cutting_planes=(CuttingPlane("front", (17.5, 15.0), (17.5, -15.0), "B", (-1.0, 0.0)),),
    dims=(
        PointDim("dim_drive_diameter", "Ø25 ±0.01", 25, "front", "y", (1, -12.5),
                 (1, 12.5), -8.0, "drive_diameter", "drive_diameter", tol=0.01,
                 feature_type="diameter", datum="A"),
        PointDim("dim_collar_diameter", "Ø40", 40, "front", "y", (45, -20), (45, 20), 45.0,
                 "collar_diameter", "collar_diameter", feature_type="diameter"),
        PointDim("dim_journal_diameter", "Ø30 ±0.01", 30, "front", "y", (99, -15),
                 (99, 15), 108.0, "journal_diameter", "journal_diameter", tol=0.01,
                 feature_type="diameter"),
        PointDim("dim_drive_length", "35 ±0.1", 35, "front", "x", (0, -11.5), (35, -20),
                 -26.0, "drive_end_face", "shoulder_face", tol=0.1),
        PointDim("dim_collar_length", "20 ±0.1", 20, "front", "x", (35, -20), (55, -20),
                 -26.0, "shoulder_face", "collar_face", tol=0.1),
        PointDim("dim_overall_length", "100 ±0.2", 100, "front", "x", (0, -11.5),
                 (100, -14), -33.0, "drive_end_face", "journal_end_face", tol=0.2),
        PointDim("dim_keyway_length", "25 ±0.2", 25, "front", "x", (5, 12.5), (30, 12.5),
                 21.0, "keyway_start", "keyway_end", tol=0.2),
        PointDim("dim_keyway_offset", "5 ±0.1", 5, "front", "x", (0, 11.5), (5, 12.5),
                 27.0, "drive_end_face", "keyway_start", tol=0.1),
        PointDim("dim_keyway_width", "8 ±0.02", 8, "section_b_b", "x", (-4, _S_KEY_WALL),
                 (4, _S_KEY_WALL), 16.0, "keyway_side", "keyway_side", tol=0.02),
        PointDim("dim_keyway_depth", "21 ±0.1", 21, "section_b_b", "y", (4, 8.5),
                 (0, -12.5), 17.0, "keyway_floor", "drive_diameter_bottom", tol=0.1),
    ),
    callouts=(
        Callout("dim_chamfer", "2X 1 ±0.2 X 45°", 1, "front", (99.5, -14.5),
                _S_CHAMFER_ELBOW, 480.0, "chamfer", tol=0.2, quantity=2),
        Callout("dim_fillet_radius", "R1 ±0.1", 1, "detail_c", polar(1.0, 315.0, (34, 13.5)),
                (300.0, 165.0), 350.0, "radius", tol=0.1),
    ),
    frames=(
        Frame("fcf_runout_journal", "circular_runout", 0.02, "0.02", False, None, ("A",),
              265.0, 455.0, leader_to=_S_FRONT.pt(80.0, 15.0)),
    ),
    notes=(
        "1. BREAK SHARP EDGES 0.2 MAX.",
        "2. HARDEN BEARING JOURNAL 50-55 HRC.",
        "3. KEY 8 x 7 TO SUIT KEYWAY.",
    ),
    seeded=(
        SeededError(
            "DIM-001",
            "DIM-001@dim_collar_diameter",
            "The title block gives no general tolerance, and the collar diameter Ø40 prints "
            "no tolerance of its own.",
            "dim_collar_diameter",
        ),
    ),
)

# Gripper claw: a 40-high base with two Ø6.5 mounting holes, a top edge that slopes down
# to a jaw whose tip is an R12 arc (centre 72, 22, basic), and a lower edge tapering up
# at 15° to meet the arc's lowest point. 8 thick, 1 x 45° chamfer on the front-left edge.
_C_FRONT = PartView("front", "front", 110.0, 200.0, _per_mm(1.5))
_C_TOP = PartView("top", "top", 110.0, 470.0, _per_mm(1.5))
_C_JAW = (72.0, 22.0)
_C_JAW_R = 12.0
_C_TAPER_START = _C_JAW[0] - (_C_JAW[1] - _C_JAW_R) / math.tan(math.radians(15.0))
_C_JAW_TOP = polar(_C_JAW_R, 60.0, _C_JAW)
_C_HOLES = ((12.0, 11.0), (12.0, 29.0))
_C_HOLE_ELBOW = (95.0, 175.0)

GRIPPER_CLAW = PartSpec(
    stem="gripper_claw",
    title="GRIPPER CLAW",
    drawing_number="DC-0306",
    revision="A",
    material="AL 7075-T6",
    units="mm",
    units_text="MILLIMETERS",
    general_tol=0.1,
    general_tol_text="UNLESS OTHERWISE SPECIFIED: ±0.1",
    scale_text="1.5:1",
    part_type="gripper_claw",
    views=(_C_FRONT, _C_TOP),
    shapes=(
        path("front", "visible", (0, 0), (_C_TAPER_START, 0),
             *(polar(_C_JAW_R, -90.0 + 150.0 * i / 30, _C_JAW) for i in range(31)),
             (45, 40), (0, 40), closed=True),
        path("front", "visible", (1, 0), (1, 40)),
        *(circle("front", "visible", hole, 3.25) for hole in _C_HOLES),
        *(line for hole in _C_HOLES for line in centre_cross("front", hole, 5.0)),
        *centre_cross("front", _C_JAW, 3.0),
        path("top", "visible", (1, 0), (84, 0), (84, 8), (0, 8), (0, 1), closed=True),
        path("top", "visible", (45, 0), (45, 8)),
        path("top", "visible", (_C_JAW_TOP[0], 0), (_C_JAW_TOP[0], 8)),
        path("top", "hidden", (8.75, 0), (8.75, 8)),
        path("top", "hidden", (15.25, 0), (15.25, 8)),
        path("top", "center", (12, -1.5), (12, 9.5)),
    ),
    dims=(
        PointDim("dim_overall_height", "40 ±0.1", 40, "front", "y", (0, 0), (0, 40), -12.0,
                 "bottom_edge", "top_edge", tol=0.1),
        PointDim("dim_hole1_y", "11 ±0.1", 11, "front", "y", (0, 0), _C_HOLES[0], -6.0,
                 "bottom_edge", "hole_1_center", tol=0.1),
        PointDim("dim_hole_pitch", "18 ±0.1", 18, "front", "y", _C_HOLES[0], _C_HOLES[1],
                 -6.0, "hole_1_center", "hole_2_center", tol=0.1),
        PointDim("dim_hole_x", "12 ±0.1", 12, "front", "x", (0, 40), _C_HOLES[1], 46.0,
                 "left_edge", "hole_1_center", tol=0.1),
        PointDim("dim_top_flat", "45 ±0.2", 45, "front", "x", (0, 40), (45, 40), 52.0,
                 "left_edge", "slope_start", tol=0.2),
        PointDim("dim_jaw_center_x", "72", 72, "front", "x", (0, 0), _C_JAW, -10.0,
                 "left_edge", "jaw_arc_center", basic=True),
        PointDim("dim_overall_length", "84 ±0.2", 84, "front", "x", (0, 0),
                 (_C_JAW[0] + _C_JAW_R, _C_JAW[1]), -16.0, "left_edge", "jaw_tip", tol=0.2),
        PointDim("dim_jaw_center_y", "22", 22, "front", "y", (_C_TAPER_START, 0), _C_JAW,
                 90.0, "bottom_edge", "jaw_arc_center", basic=True),
        PointDim("dim_thickness", "8 ±0.1", 8, "top", "y", (84, 0), (84, 8), 92.0,
                 "front_face", "back_face", tol=0.1),
    ),
    angles=(
        AngleDim("dim_taper_angle", "15° ±0.5°", 15, "front", (_C_TAPER_START, 0.0), 25.0,
                 0.0, 15.0, "bottom_edge", "taper_edge", tol=0.5),
    ),
    callouts=(
        Callout("dim_hole_diameter", "2X Ø6.5 ±0.1", 6.5, "front",
                rim_toward(_C_FRONT, _C_HOLES[0], 3.25, _C_HOLE_ELBOW), _C_HOLE_ELBOW, 28.0,
                "diameter", tol=0.1, quantity=2),
        Callout("dim_jaw_radius", "R12", 12, "front", polar(_C_JAW_R, 45.0, _C_JAW),
                (500.0, 380.0), 545.0, "radius", basic=True),
        Callout("dim_chamfer", "1 X 45°", 1, "top", (0.5, 0.5), (75.0, 445.0), 28.0,
                "chamfer"),
    ),
    frames=(
        Frame("fcf_profile_jaw", "profile_surface", 0.1, "0.1", False, None, ("A", "B"),
              520.0, 330.0, leader_to=_C_FRONT.pt(*polar(_C_JAW_R, 20.0, _C_JAW))),
    ),
    surface_datums=(
        SurfaceDatum("datum_a", "A", "top", (60.0, 0.0), "down"),
        SurfaceDatum("datum_b", "B", "front", (20.0, 0.0), "down"),
    ),
    notes=(
        "1. JAW PROFILE DEFINED BY BASIC DIMENSIONS.",
        "2. BREAK SHARP EDGES 0.2 MAX.",
        "3. FINISH: HARD ANODIZE.",
    ),
)

#: Every drawing in the suite, by stem, in the order the eval runs them.
SUITE: dict[str, DrawingSpec | PartSpec] = {
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
        GEAR_SPUR,
        HEX_NUT,
        HEX_BOLT,
        FLANGE,
        STEPPED_SHAFT,
        GRIPPER_CLAW,
    )
}


# --- Build -----------------------------------------------------------------------------


def build_one(
    spec: DrawingSpec | PartSpec, out_dir: Path, *, write_png: bool = True
) -> dict[str, Path]:
    """Draw one spec to ``<stem>.pdf`` and write ``<stem>.gt.json`` (and ``<stem>.png``)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"{spec.stem}.pdf"
    json_path = out_dir / f"{spec.stem}.gt.json"

    # invariant=1 leaves the creation date out, so rebuilding an unchanged spec gives a
    # byte-identical PDF and git shows no change.
    c = rl_canvas.Canvas(str(pdf_path), pagesize=(PAGE_W, PAGE_H), invariant=1)
    c.setTitle(f"{spec.title} {spec.drawing_number}")
    if isinstance(spec, PartSpec):
        boxes = draw_part_sheet(c, spec)
    else:
        boxes = draw_sheet(c, spec)
    c.showPage()
    c.save()

    truth = part_ground_truth(spec, boxes) if isinstance(spec, PartSpec) else ground_truth(
        spec, boxes
    )
    json_path.write_text(json.dumps(truth, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    paths = {"pdf": pdf_path, "ground_truth": json_path}

    if write_png:
        png_path = out_dir / f"{spec.stem}.png"
        png_path.write_bytes(load_pages(pdf_path, dpi=200)[0].png_bytes)
        paths["png"] = png_path
    return paths


def build_suite(
    out_dir: Path | str = OUT_DIR,
    *,
    write_png: bool = True,
    stems: list[str] | None = None,
) -> dict[str, dict[str, Path]]:
    """Build the drawings in :data:`SUITE` (or just ``stems``). Returns paths per stem."""
    out_dir = Path(out_dir)
    chosen = SUITE if stems is None else {stem: SUITE[stem] for stem in stems}
    return {stem: build_one(spec, out_dir, write_png=write_png) for stem, spec in chosen.items()}
