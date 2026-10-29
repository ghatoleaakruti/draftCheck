"""Generate a synthetic engineering drawing plus its ground truth.

Run with:  python -m scripts.make_sample_drawing

Writes three files to ``samples/generated/``:
  plate_001.pdf       the drawing
  plate_001.gt.json   ground truth: what a perfect extraction should return
  plate_001.png       a rendered preview

The drawing carries one deliberate error: the position feature control frame cites
datum C, but no datum C feature symbol appears on the sheet. The rule engine is
expected to catch this.

All geometry comes from the constants below, so the drawing and the ground truth
cannot drift apart. Bounding boxes in the ground truth are normalized to 0..1 with a
TOP-LEFT origin (the convention vision models use), while reportlab draws from the
bottom-left, so ``norm_bbox`` performs the flip in one place.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas as rl_canvas

from draftcheck.render import load_pages

# --- Sheet -----------------------------------------------------------------
PAGE_W, PAGE_H = landscape(letter)  # 792 x 612 pt
MARGIN = 20.0

# --- Part geometry ---------------------------------------------------------
PT_PER_MM = 4.0
PLATE_W_MM, PLATE_H_MM = 100.0, 60.0
HOLE_DIA_MM = 8.0
HOLE_CENTERS_MM = ((20.0, 30.0), (80.0, 30.0))

PLATE_X0, PLATE_Y0 = 200.0, 270.0
PLATE_X1 = PLATE_X0 + PLATE_W_MM * PT_PER_MM  # 600
PLATE_Y1 = PLATE_Y0 + PLATE_H_MM * PT_PER_MM  # 510
HOLE_R = HOLE_DIA_MM * PT_PER_MM / 2.0  # 16

# --- Dimension line positions ----------------------------------------------
DIM_WIDTH_Y = 225.0  # overall 100, below the plate
DIM_HEIGHT_X = 150.0  # overall 60, left of the plate
DIM_20_Y = 535.0  # basic 20, above
DIM_80_Y = 562.0  # basic 80, above
DIM_30_X = 645.0  # basic 30, right

EXT_GAP = 5.0  # gap between the part and the start of an extension line
EXT_OVER = 6.0  # how far an extension line runs past the dimension line
ARROW_LEN = 8.0
ARROW_HALF_W = 2.6

# --- Annotation blocks -----------------------------------------------------
NOTE_TAIL_Y = 195.0
NOTE_TAIL_X0, NOTE_TAIL_X1 = 60.0, 120.0
NOTE_TEXT = "2X Ø8 ±0.1"
FCF_X, FCF_Y = 60.0, 160.0
FCF_H = 26.0

DATUM_A_X = 360.0  # on the bottom edge
DATUM_B_Y = 420.0  # on the left edge
DATUM_BOX = 18.0
DATUM_TRI_H = 10.0

TB_X0, TB_Y0, TB_X1, TB_Y1 = 512.0, 20.0, 772.0, 130.0

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
DIM_SIZE = 10.0

OUT_DIR = Path("samples/generated")
STEM = "plate_001"


@dataclass(frozen=True)
class Box:
    """A rectangle in reportlab coordinates (origin bottom-left)."""

    x0: float
    y0: float
    x1: float
    y1: float


def norm_bbox(box: Box) -> list[float]:
    """Convert a bottom-left-origin box to normalized top-left-origin [x0,y0,x1,y1]."""
    return [
        round(box.x0 / PAGE_W, 4),
        round((PAGE_H - box.y1) / PAGE_H, 4),
        round(box.x1 / PAGE_W, 4),
        round((PAGE_H - box.y0) / PAGE_H, 4),
    ]


def hole_center(index: int) -> tuple[float, float]:
    """Return a hole center in page points."""
    mm_x, mm_y = HOLE_CENTERS_MM[index]
    return PLATE_X0 + mm_x * PT_PER_MM, PLATE_Y0 + mm_y * PT_PER_MM


# --- Low-level drawing helpers ---------------------------------------------


def arrowhead(c: rl_canvas.Canvas, tip_x: float, tip_y: float, dx: float, dy: float) -> None:
    """Draw a filled arrowhead whose point is at (tip_x, tip_y), aimed along (dx, dy)."""
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    base_x, base_y = tip_x - ux * ARROW_LEN, tip_y - uy * ARROW_LEN
    px, py = -uy * ARROW_HALF_W, ux * ARROW_HALF_W
    path = c.beginPath()
    path.moveTo(tip_x, tip_y)
    path.lineTo(base_x + px, base_y + py)
    path.lineTo(base_x - px, base_y - py)
    path.close()
    c.drawPath(path, stroke=0, fill=1)


def dashed_line(c: rl_canvas.Canvas, x0: float, y0: float, x1: float, y1: float) -> None:
    """Draw a centerline-style dashed segment."""
    c.saveState()
    c.setDash(8, 3)
    c.setLineWidth(0.5)
    c.line(x0, y0, x1, y1)
    c.restoreState()


def centered_text(c: rl_canvas.Canvas, x: float, y: float, text: str, size: float) -> Box:
    """Draw text centered horizontally on x, baseline at y. Return its box."""
    c.setFont(FONT, size)
    width = c.stringWidth(text, FONT, size)
    c.drawCentredString(x, y, text)
    return Box(x - width / 2, y - 0.25 * size, x + width / 2, y + 0.78 * size)


def boxed_text(c: rl_canvas.Canvas, x: float, y: float, text: str, size: float) -> Box:
    """Draw text centered on x with a rectangle around it (a basic dimension)."""
    c.setFont(FONT, size)
    width = c.stringWidth(text, FONT, size)
    pad_x, pad_below, pad_above = 4.0, 3.5, 3.0
    box = Box(
        x - width / 2 - pad_x,
        y - 0.25 * size - pad_below,
        x + width / 2 + pad_x,
        y + 0.78 * size + pad_above,
    )
    c.setLineWidth(0.7)
    c.rect(box.x0, box.y0, box.x1 - box.x0, box.y1 - box.y0, stroke=1, fill=0)
    c.drawCentredString(x, y, text)
    return box


def rotated_text(c: rl_canvas.Canvas, x: float, y: float, text: str, size: float) -> Box:
    """Draw text rotated 90 degrees, centered on y, with its baseline at x."""
    c.saveState()
    c.setFont(FONT, size)
    width = c.stringWidth(text, FONT, size)
    c.translate(x, y)
    c.rotate(90)
    c.drawCentredString(0, 0, text)
    c.restoreState()
    return Box(x - 0.25 * size, y - width / 2, x + 0.78 * size, y + width / 2)


def rotated_boxed_text(c: rl_canvas.Canvas, x: float, y: float, text: str, size: float) -> Box:
    """Rotated text with a rectangle around it (a vertical basic dimension)."""
    c.saveState()
    c.setFont(FONT, size)
    width = c.stringWidth(text, FONT, size)
    pad_along, pad_below, pad_above = 4.0, 3.5, 3.0
    c.translate(x, y)
    c.rotate(90)
    c.setLineWidth(0.7)
    c.rect(
        -width / 2 - pad_along,
        -0.25 * size - pad_below,
        width + 2 * pad_along,
        size * 1.03 + pad_below + pad_above,
        stroke=1,
        fill=0,
    )
    c.drawCentredString(0, 0, text)
    c.restoreState()
    return Box(
        x - 0.25 * size - pad_below,
        y - width / 2 - pad_along,
        x + 0.78 * size + pad_above,
        y + width / 2 + pad_along,
    )


# --- Dimensions -------------------------------------------------------------


def horizontal_dim(
    c: rl_canvas.Canvas,
    x0: float,
    x1: float,
    y_dim: float,
    y_ref: float,
    text: str,
    *,
    basic: bool,
) -> Box:
    """Draw a horizontal dimension between x0 and x1, measuring from the edge at y_ref."""
    c.setLineWidth(0.7)
    sign = 1.0 if y_dim > y_ref else -1.0
    for x in (x0, x1):
        c.line(x, y_ref + sign * EXT_GAP, x, y_dim + sign * EXT_OVER)
    c.line(x0, y_dim, x1, y_dim)
    arrowhead(c, x0, y_dim, -1, 0)
    arrowhead(c, x1, y_dim, 1, 0)
    mid = (x0 + x1) / 2
    draw = boxed_text if basic else centered_text
    return draw(c, mid, y_dim + 4.0, text, DIM_SIZE)


def vertical_dim(
    c: rl_canvas.Canvas,
    y0: float,
    y1: float,
    x_dim: float,
    x_ref: float,
    text: str,
    *,
    basic: bool,
) -> Box:
    """Draw a vertical dimension between y0 and y1, measuring from the edge at x_ref."""
    c.setLineWidth(0.7)
    sign = 1.0 if x_dim > x_ref else -1.0
    for y in (y0, y1):
        c.line(x_ref + sign * EXT_GAP, y, x_dim + sign * EXT_OVER, y)
    c.line(x_dim, y0, x_dim, y1)
    arrowhead(c, x_dim, y0, 0, -1)
    arrowhead(c, x_dim, y1, 0, 1)
    mid = (y0 + y1) / 2
    draw = rotated_boxed_text if basic else rotated_text
    return draw(c, x_dim + 4.0, mid, text, DIM_SIZE)


# --- GD&T symbols -----------------------------------------------------------


def position_symbol(c: rl_canvas.Canvas, cx: float, cy: float, r: float = 6.0) -> None:
    """Draw the true-position symbol: a circle with a cross extending past it."""
    c.setLineWidth(0.8)
    c.circle(cx, cy, r, stroke=1, fill=0)
    c.line(cx - r - 2.5, cy, cx + r + 2.5, cy)
    c.line(cx, cy - r - 2.5, cx, cy + r + 2.5)


def circled_letter(c: rl_canvas.Canvas, cx: float, cy: float, letter_: str) -> None:
    """Draw a letter inside a circle, e.g. the MMC modifier."""
    c.setLineWidth(0.8)
    c.circle(cx, cy, 6.0, stroke=1, fill=0)
    c.setFont(FONT, 7.5)
    c.drawCentredString(cx, cy - 2.7, letter_)


def draw_fcf(c: rl_canvas.Canvas, x: float, y: float) -> Box:
    """Draw the position feature control frame. Returns its outer box.

    Compartments: position symbol | diameter tolerance at MMC | A | B | C
    """
    widths = (24.0, 58.0, 18.0, 18.0, 18.0)
    total = sum(widths)
    mid_y = y + FCF_H / 2

    c.setLineWidth(0.9)
    c.rect(x, y, total, FCF_H, stroke=1, fill=0)
    edge = x
    for width in widths[:-1]:
        edge += width
        c.line(edge, y, edge, y + FCF_H)

    position_symbol(c, x + widths[0] / 2, mid_y)

    tol_x = x + widths[0]
    c.setFont(FONT, 9.5)
    c.drawString(tol_x + 6.0, mid_y - 3.2, "Ø0.25")
    circled_letter(c, tol_x + widths[1] - 11.0, mid_y, "M")

    datum_x = tol_x + widths[1]
    for width, label in zip(widths[2:], ("A", "B", "C"), strict=True):
        c.setFont(FONT, 9.5)
        c.drawCentredString(datum_x + width / 2, mid_y - 3.2, label)
        datum_x += width

    return Box(x, y, x + total, y + FCF_H)


def draw_datum_bottom(c: rl_canvas.Canvas, x: float, label: str) -> Box:
    """Datum feature symbol attached to the bottom edge of the plate."""
    c.setLineWidth(0.9)
    apex_y = PLATE_Y0
    base_y = apex_y - DATUM_TRI_H
    path = c.beginPath()
    path.moveTo(x, apex_y)
    path.lineTo(x - 5.0, base_y)
    path.lineTo(x + 5.0, base_y)
    path.close()
    c.drawPath(path, stroke=1, fill=1)

    box_top = base_y
    box_bottom = box_top - DATUM_BOX
    c.line(x, base_y, x, box_top)
    c.rect(x - DATUM_BOX / 2, box_bottom, DATUM_BOX, DATUM_BOX, stroke=1, fill=0)
    c.setFont(FONT, 10)
    c.drawCentredString(x, box_bottom + 5.5, label)
    return Box(x - DATUM_BOX / 2, box_bottom, x + DATUM_BOX / 2, apex_y)


def draw_datum_left(c: rl_canvas.Canvas, y: float, label: str) -> Box:
    """Datum feature symbol attached to the left edge of the plate."""
    c.setLineWidth(0.9)
    apex_x = PLATE_X0
    base_x = apex_x - DATUM_TRI_H
    path = c.beginPath()
    path.moveTo(apex_x, y)
    path.lineTo(base_x, y - 5.0)
    path.lineTo(base_x, y + 5.0)
    path.close()
    c.drawPath(path, stroke=1, fill=1)

    box_right = base_x
    box_left = box_right - DATUM_BOX
    c.rect(box_left, y - DATUM_BOX / 2, DATUM_BOX, DATUM_BOX, stroke=1, fill=0)
    c.setFont(FONT, 10)
    c.drawCentredString(box_left + DATUM_BOX / 2, y - 3.5, label)
    return Box(box_left, y - DATUM_BOX / 2, apex_x, y + DATUM_BOX / 2)


# --- Sheet furniture --------------------------------------------------------


def draw_border(c: rl_canvas.Canvas) -> None:
    """Draw the sheet border."""
    c.setLineWidth(1.4)
    c.rect(MARGIN, MARGIN, PAGE_W - 2 * MARGIN, PAGE_H - 2 * MARGIN, stroke=1, fill=0)


def draw_plate(c: rl_canvas.Canvas) -> None:
    """Draw the plate outline, the holes, and their centerlines."""
    c.setLineWidth(1.4)
    c.rect(PLATE_X0, PLATE_Y0, PLATE_X1 - PLATE_X0, PLATE_Y1 - PLATE_Y0, stroke=1, fill=0)

    for index in range(len(HOLE_CENTERS_MM)):
        cx, cy = hole_center(index)
        c.setLineWidth(1.0)
        c.circle(cx, cy, HOLE_R, stroke=1, fill=0)
        reach = HOLE_R + 8.0
        dashed_line(c, cx - reach, cy, cx + reach, cy)
        dashed_line(c, cx, cy - reach, cx, cy + reach)


def draw_hole_note(c: rl_canvas.Canvas) -> tuple[Box, Box]:
    """Draw the leader from hole 1 to the hole note, and the note text.

    The leader leaves the plate through its left edge and drops into the open
    lower-left corner, crossing one extension line but no dimension line.
    """
    cx, cy = hole_center(0)
    offset = HOLE_R * math.sqrt(0.5)
    start_x, start_y = cx - offset, cy - offset
    elbow_x, elbow_y = NOTE_TAIL_X1, NOTE_TAIL_Y

    c.setLineWidth(0.8)
    c.line(start_x, start_y, elbow_x, elbow_y)
    c.line(elbow_x, elbow_y, NOTE_TAIL_X0, NOTE_TAIL_Y)
    arrowhead(c, start_x, start_y, start_x - elbow_x, start_y - elbow_y)

    c.setFont(FONT, 10.5)
    text_y = NOTE_TAIL_Y + 5.0
    c.drawString(NOTE_TAIL_X0 + 2.0, text_y, NOTE_TEXT)
    width = c.stringWidth(NOTE_TEXT, FONT, 10.5)
    note_box = Box(NOTE_TAIL_X0 + 2.0, text_y - 2.6, NOTE_TAIL_X0 + 2.0 + width, text_y + 8.2)

    leader_box = Box(NOTE_TAIL_X0, NOTE_TAIL_Y, start_x, start_y)
    return note_box, leader_box


def draw_title_block(c: rl_canvas.Canvas) -> Box:
    """Draw the title block in the bottom-right corner."""
    c.setLineWidth(1.2)
    c.rect(TB_X0, TB_Y0, TB_X1 - TB_X0, TB_Y1 - TB_Y0, stroke=1, fill=0)

    rows = [
        ("TITLE: BRACKET PLATE", True),
        ("DWG NO: DC-0001          REV: A", False),
        ("MATERIAL: AL 6061-T6", False),
        ("UNITS: MILLIMETERS", False),
        ("UNLESS OTHERWISE SPECIFIED: ±0.1", False),
        ("SCALE: 1:1", False),
    ]
    row_h = (TB_Y1 - TB_Y0) / len(rows)
    for index, (text, bold) in enumerate(rows):
        top = TB_Y1 - index * row_h
        if index:
            c.setLineWidth(0.6)
            c.line(TB_X0, top, TB_X1, top)
        c.setFont(FONT_BOLD if bold else FONT, 8.5)
        c.drawString(TB_X0 + 6.0, top - row_h + 5.0, text)

    return Box(TB_X0, TB_Y0, TB_X1, TB_Y1)


# --- Ground truth -----------------------------------------------------------


def build_ground_truth(boxes: dict[str, Box]) -> dict:
    """Assemble the ground-truth record from the boxes produced while drawing.

    The field names are exactly those of ``draftcheck.schema.Drawing``, so this file is
    what a perfect extraction would return and can be fed straight to ``parse_drawing``.
    Only ``bbox_convention`` and ``seeded_errors`` are extra; the schema ignores them.
    """
    return {
        "units": "mm",
        "general_tolerance": {
            "linear_plus_minus": 0.1,
            "text_raw": "UNLESS OTHERWISE SPECIFIED: ±0.1",
        },
        "title_block": {
            "title": "BRACKET PLATE",
            "drawing_number": "DC-0001",
            "revision": "A",
            "material": "AL 6061-T6",
            "scale": "1:1",
        },
        "bbox_convention": (
            "[x0, y0, x1, y1] normalized 0..1, origin top-left, covering the "
            "annotation's text or symbol"
        ),
        "datums": [
            {"label": "A", "bbox": norm_bbox(boxes["datum_a"])},
            {"label": "B", "bbox": norm_bbox(boxes["datum_b"])},
        ],
        "dimensions": [
            {
                "id": "dim_overall_width",
                "text_raw": "100 ±0.2",
                "value": 100.0,
                "unit": None,
                "feature_type": "linear",
                "quantity": 1,
                "is_basic": False,
                "is_reference": False,
                "tol_plus": 0.2,
                "tol_minus": 0.2,
                "from_ref": "left_edge",
                "to_ref": "right_edge",
                "bbox": norm_bbox(boxes["dim_overall_width"]),
            },
            {
                "id": "dim_overall_height",
                "text_raw": "60 ±0.2",
                "value": 60.0,
                "unit": None,
                "feature_type": "linear",
                "quantity": 1,
                "is_basic": False,
                "is_reference": False,
                "tol_plus": 0.2,
                "tol_minus": 0.2,
                "from_ref": "bottom_edge",
                "to_ref": "top_edge",
                "bbox": norm_bbox(boxes["dim_overall_height"]),
            },
            {
                "id": "dim_hole1_x",
                "text_raw": "20",
                "value": 20.0,
                "unit": None,
                "feature_type": "linear",
                "quantity": 1,
                "is_basic": True,
                "is_reference": False,
                "tol_plus": None,
                "tol_minus": None,
                "from_ref": "left_edge",
                "to_ref": "hole_1_center",
                "bbox": norm_bbox(boxes["dim_hole1_x"]),
            },
            {
                "id": "dim_hole2_x",
                "text_raw": "80",
                "value": 80.0,
                "unit": None,
                "feature_type": "linear",
                "quantity": 1,
                "is_basic": True,
                "is_reference": False,
                "tol_plus": None,
                "tol_minus": None,
                "from_ref": "left_edge",
                "to_ref": "hole_2_center",
                "bbox": norm_bbox(boxes["dim_hole2_x"]),
            },
            {
                "id": "dim_hole_y",
                "text_raw": "30",
                "value": 30.0,
                "unit": None,
                "feature_type": "linear",
                "quantity": 1,
                "is_basic": True,
                "is_reference": False,
                "tol_plus": None,
                "tol_minus": None,
                "from_ref": "bottom_edge",
                "to_ref": "hole_centers",
                "bbox": norm_bbox(boxes["dim_hole_y"]),
            },
            {
                "id": "dim_hole_diameter",
                "text_raw": NOTE_TEXT,
                "value": 8.0,
                "unit": None,
                "feature_type": "diameter",
                "quantity": 2,
                "is_basic": False,
                "is_reference": False,
                "tol_plus": 0.1,
                "tol_minus": 0.1,
                "from_ref": None,
                "to_ref": None,
                "bbox": norm_bbox(boxes["dim_hole_diameter"]),
            },
        ],
        "fcfs": [
            {
                "id": "fcf_position_holes",
                "text_raw": "position | Ø0.25 (M) | A | B | C",
                "characteristic": "position",
                "tolerance": 0.25,
                "diameter_zone": True,
                "material_modifier": "MMC",
                "datum_refs": [
                    {"label": "A", "modifier": None},
                    {"label": "B", "modifier": None},
                    {"label": "C", "modifier": None},
                ],
                "bbox": norm_bbox(boxes["fcf"]),
            }
        ],
        "notes": [],
        "seeded_errors": [
            {
                "id": "err_missing_datum_c",
                "type": "missing_datum_feature",
                "severity": "major",
                "description": (
                    "The position feature control frame references datum C as its "
                    "tertiary datum, but no datum feature symbol C appears on the "
                    "drawing. Only datums A and B are defined."
                ),
                "expected_finding": (
                    "Every datum referenced in a feature control frame must have a "
                    "matching datum feature symbol on the drawing."
                ),
                "related": {"fcf": "fcf_position_holes", "datum": "C"},
                "bbox": norm_bbox(boxes["fcf"]),
            }
        ],
    }


# --- Build ------------------------------------------------------------------


def draw_sheet(c: rl_canvas.Canvas) -> dict[str, Box]:
    """Draw the whole sheet and return the boxes needed for the ground truth."""
    boxes: dict[str, Box] = {}

    draw_border(c)
    draw_plate(c)

    hole1_x, _ = hole_center(0)
    hole2_x, hole_y = hole_center(1)

    boxes["dim_overall_width"] = horizontal_dim(
        c, PLATE_X0, PLATE_X1, DIM_WIDTH_Y, PLATE_Y0, "100 ±0.2", basic=False
    )
    boxes["dim_overall_height"] = vertical_dim(
        c, PLATE_Y0, PLATE_Y1, DIM_HEIGHT_X, PLATE_X0, "60 ±0.2", basic=False
    )
    boxes["dim_hole1_x"] = horizontal_dim(
        c, PLATE_X0, hole1_x, DIM_20_Y, PLATE_Y1, "20", basic=True
    )
    boxes["dim_hole2_x"] = horizontal_dim(
        c, PLATE_X0, hole2_x, DIM_80_Y, PLATE_Y1, "80", basic=True
    )
    boxes["dim_hole_y"] = vertical_dim(
        c, PLATE_Y0, hole_y, DIM_30_X, PLATE_X1, "30", basic=True
    )

    note_box, _ = draw_hole_note(c)
    boxes["dim_hole_diameter"] = note_box
    boxes["fcf"] = draw_fcf(c, FCF_X, FCF_Y)

    boxes["datum_a"] = draw_datum_bottom(c, DATUM_A_X, "A")
    boxes["datum_b"] = draw_datum_left(c, DATUM_B_Y, "B")

    draw_title_block(c)
    return boxes


def build(out_dir: Path | str = OUT_DIR, *, write_png: bool = True) -> dict[str, Path]:
    """Generate the drawing, its ground truth, and (optionally) a PNG preview."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = out_dir / f"{STEM}.pdf"
    json_path = out_dir / f"{STEM}.gt.json"

    c = rl_canvas.Canvas(str(pdf_path), pagesize=(PAGE_W, PAGE_H))
    c.setTitle("BRACKET PLATE DC-0001")
    boxes = draw_sheet(c)
    c.showPage()
    c.save()

    ground_truth = build_ground_truth(boxes)
    json_path.write_text(json.dumps(ground_truth, indent=2) + "\n", encoding="utf-8")

    paths = {"pdf": pdf_path, "ground_truth": json_path}

    if write_png:
        png_path = out_dir / f"{STEM}.png"
        page = load_pages(pdf_path, dpi=200)[0]
        png_path.write_bytes(page.png_bytes)
        paths["png"] = png_path

    return paths


def main() -> int:
    paths = build()
    for label, path in paths.items():
        print(f"{label:>13}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
