"""Turn a run's findings into a two-page redline a person can read.

Page 1 is the drawing itself, marked up the way the on-screen sheet is: a revision cloud
around each finding's bounding box with a numbered delta at its top-left, a handwritten
pen note beside it for each edit the correction loop proposed, and — when the loop ran —
a green CHECKED stamp. Page 2 is a plain white sheet listing every finding in words, the
corrections as a before/after table, and every check the rules ran.

The markup is drawn on the *rendered image* of the drawing rather than onto the PDF's own
content stream. That means one code path covers PDF and image inputs alike, and it needs
no library beyond pillow. The pen and stamp fonts are the ones the page serves, from
``static/fonts``.

A cloud is red while no correction has run, then green when the rules confirm its finding
cleared and amber when a designer still has to act (held, or still reported).

Nothing here decides anything. Severity, message, and resolved/unresolved all arrive
already decided — the last two from :mod:`draftcheck.agent`, which gets them by re-running
the rules. This module only lays them out.
"""

from __future__ import annotations

import functools
import io
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_type
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from pydantic import ValidationError

from draftcheck.agent import CorrectionReport
from draftcheck.edits import (
    AddDatum,
    AddDimension,
    Edit,
    RemoveDatumRef,
    SetField,
    apply_edits,
    parse_edit,
)
from draftcheck.rules import checks_summary
from draftcheck.schema import (
    FCF,
    Characteristic,
    Dimension,
    Drawing,
    Finding,
    RuleResult,
    finding_key,
)

RED = (200, 30, 30)
INK = (25, 25, 25)
GREY = (95, 95, 95)
WHITE = (255, 255, 255)

#: The checker's pens, the same three colours the on-screen sheet uses (style.css).
PEN_RED = (212, 42, 26)
PEN_GREEN = (22, 128, 73)
PEN_AMBER = (194, 108, 0)

#: A finding's final outcome, and the pen its cloud is drawn in. ``open`` is a finding no
#: correction run has looked at yet.
OUTCOME_COLOURS = {
    "open": PEN_RED,
    "verified": PEN_GREEN,
    "held": PEN_AMBER,
    "unresolved": PEN_AMBER,
}

#: Mark geometry, all as a fraction of the page width so a mark looks the same on a small
#: sheet and a large one. The cloud encloses the finding's bounding box; the delta carries
#: its number and sits just outside the cloud's top-left corner.
CLOUD_PAD_RATIO = 0.005
CLOUD_LINE_RATIO = 0.003
DELTA_WIDTH_RATIO = 0.025
#: Roughly one arc per this fraction of the page width of cloud outline.
CLOUD_ARC_RATIO = 0.011
PEN_SIZE_RATIO = 0.024
STAMP_WIDTH_RATIO = 0.2
STAMP_HEIGHT_RATIO = 0.074
#: The stamp's ink: the verified green at about 90% opacity.
STAMP_ALPHA = 230

FONTS_DIR = Path(__file__).resolve().parent / "static" / "fonts"
PEN_FONT = FONTS_DIR / "nanum-pen-script" / "NanumPenScript-Regular.ttf"
ARCHIVO_FONT = FONTS_DIR / "archivo" / "Archivo-Variable.ttf"
MONO_FONT = FONTS_DIR / "jetbrains-mono" / "JetBrainsMono-Variable.ttf"

#: Written into the PDF so a page comes out at its real physical size rather than at
#: pillow's 72 dpi default, which would make a 2200 px sheet about 30 inches wide.
PDF_DPI = 200.0

FOOTER = (
    "Proposed corrections are verified by DraftCheck's rule engine and require "
    "engineer review."
)

NO_MODEL = "model not called"

#: Characters the bundled font cannot draw, and what to put there instead. Pillow's
#: default font covers Latin-1 and little else, and a missing glyph comes out as a blank
#: box — which on a redline looks like markup rather than like a font problem. Rule
#: messages are written by us but quote what is on the drawing, so this runs over every
#: string the page draws rather than only over our own wording.
_SUBSTITUTIONS = {
    "—": "-",  # em dash
    "–": "-",  # en dash
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
    "•": "*",
    " ": " ",  # non-breaking space
    "′": "'",  # prime
    "″": '"',  # double prime
}


def ascii_safe(text: str) -> str:
    """Replace characters the font cannot draw with ones it can.

    Latin-1 is kept as it is: ``±``, ``Ø``, ``×`` and ``°`` all carry engineering meaning
    and the font has them. Anything above that range is substituted if we have a sensible
    stand-in and dropped to ``?`` if we do not, so a stray character never silently turns
    into a box the reader mistakes for part of the markup.
    """
    out: list[str] = []
    for character in text:
        if character in _SUBSTITUTIONS:
            out.append(_SUBSTITUTIONS[character])
        elif ord(character) <= 0xFF or character in "\n\t":
            out.append(character)
        else:
            out.append("?")
    return "".join(out)


def _font(size: float) -> ImageFont.FreeTypeFont:
    """Pillow's bundled font at a given pixel size.

    ``load_default(size=...)`` keeps us free of any font file on disk, and unlike the
    size-less default it returns a scalable font, so text can grow with the page and
    ``anchor`` works for centring a number inside a circle.
    """
    return ImageFont.load_default(size=max(8, int(round(size))))


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: float) -> list[str]:
    """Break ``text`` into lines that fit ``max_width``, measuring the real font."""
    lines: list[str] = []
    for paragraph in ascii_safe(text).split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _box_pixels(
    bbox: list[float], width: int, height: int, pad: float
) -> tuple[float, float, float, float]:
    """A normalized ``[x0, y0, x1, y1]`` box as padded pixels, origin top-left."""
    x0, y0, x1, y1 = bbox
    return (x0 * width - pad, y0 * height - pad, x1 * width + pad, y1 * height + pad)


@functools.lru_cache(maxsize=64)
def _bundled_font(path: Path, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """One of the fonts in ``static/fonts`` at a pixel size; ``bold`` picks the Bold cut."""
    font = ImageFont.truetype(str(path), max(8, size))
    # A variable font's default instance is not always Regular, so the weight is always
    # named rather than left to the file.
    try:
        font.set_variation_by_name("Bold" if bold else "Regular")
    except (OSError, ValueError):
        pass  # not a variable font (Nanum Pen), or no such instance: the file's own weight
    return font


#: A code point no font here has, so it always comes out as the "missing glyph" box.
_NEVER_A_GLYPH = "\U0010fffd"


@functools.lru_cache(maxsize=2048)
def _has_glyph(path: Path, character: str) -> bool:
    """Whether a bundled font can draw ``character``.

    Pillow has no direct question for this, so the character is rendered and compared
    with what the font draws for a code point it certainly lacks.
    """
    if character.isspace():
        return True
    font = _bundled_font(path, 40)
    return bytes(font.getmask(character)) != bytes(font.getmask(_NEVER_A_GLYPH))


def _pen_font_for(text: str, size: float) -> ImageFont.FreeTypeFont:
    """Nanum Pen Script for a label, or Archivo if the pen font is missing any character."""
    if all(_has_glyph(PEN_FONT, character) for character in text):
        return _bundled_font(PEN_FONT, round(size))
    # Archivo is a print face and reads larger than the handwriting at the same size.
    return _bundled_font(ARCHIVO_FONT, round(size * 0.72))


Box = tuple[float, float, float, float]


def _cloud_arcs(box: Box, arc_length: float) -> list[tuple[Box, float, float]]:
    """The arcs of a revision cloud around ``box``, as pillow ``arc`` arguments.

    The rectangle is walked clockwise (on screen, y down) and cut into equal steps. Each
    step becomes an arc from one point to the next whose centre sits inside the box, so it
    bulges outward. Same construction as ``Sheet.cloudPath`` in sheet.js.
    """
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    perimeter = 2 * (w + h)
    count = max(8, round(perimeter / arc_length))
    step = perimeter / count
    radius = step * 0.62

    def point_at(distance: float) -> tuple[float, float]:
        d = distance % perimeter
        if d < w:
            return x0 + d, y0
        d -= w
        if d < h:
            return x1, y0 + d
        d -= h
        if d < w:
            return x1 - d, y1
        d -= w
        return x0, y1 - d

    arcs = []
    for index in range(count):
        (px, py), (qx, qy) = point_at(index * step), point_at((index + 1) * step)
        dx, dy = qx - px, qy - py
        chord = math.hypot(dx, dy) or 1.0
        # Clockwise on screen, (dy, -dx) points out of the box.
        nx, ny = dy / chord, -dx / chord
        sink = math.sqrt(max(radius**2 - (chord / 2) ** 2, 0.0))
        cx, cy = (px + qx) / 2 - nx * sink, (py + qy) / 2 - ny * sink
        start = math.degrees(math.atan2(py - cy, px - cx))
        end = math.degrees(math.atan2(qy - cy, qx - cx))
        if end < start:
            end += 360
        arcs.append(((cx - radius, cy - radius, cx + radius, cy + radius), start, end))
    return arcs


def _draw_cloud(draw: ImageDraw.ImageDraw, ring: Box, colour, width: int) -> None:
    """A closed chain of outward arcs around ``ring``: a revision cloud."""
    line = max(2, round(width * CLOUD_LINE_RATIO))
    for bounds, start, end in _cloud_arcs(ring, width * CLOUD_ARC_RATIO):
        draw.arc(bounds, start, end, fill=colour, width=line)


def _delta_box(ring: Box, size: tuple[int, int], shift: int = 0) -> Box:
    """Where the delta goes: just outside the cloud's top-left corner, kept on the page.

    ``shift`` moves it one triangle to the right per earlier finding on the same box, so
    two findings on one callout show both numbers instead of one hiding the other.
    """
    width, height = size
    side = width * DELTA_WIDTH_RATIO
    tall = side * 0.88
    margin = width * 0.002
    right = min(max(ring[0] + margin + shift * side * 1.1, side + margin), width - margin)
    base = min(max(ring[1] + margin / 2, tall + margin), height - margin)
    return (right - side, base - tall, right, base)


def _draw_delta(draw: ImageDraw.ImageDraw, box: Box, number: int, colour) -> None:
    """A filled triangle with the finding's number in white bold."""
    left, top, right, base = box
    side, tall = right - left, base - top
    draw.polygon([(left, base), (right, base), (left + side / 2, top)], fill=colour)
    font = _bundled_font(ARCHIVO_FONT, round(tall * (0.5 if number < 10 else 0.4)), bold=True)
    draw.text((left + side / 2, base - tall * 0.3), str(number), fill=WHITE, font=font, anchor="mm")


def _draw_review_stamp(
    draw: ImageDraw.ImageDraw, width: int, count: int, model_name: str, when: date_type
) -> Box:
    """Put the review's header in the top-left corner of the drawing; returns its box."""
    margin = max(12, round(width * 0.012))
    title_font = _font(width * 0.013)
    small_font = _font(width * 0.0095)

    title = ascii_safe(f"DraftCheck review: {count} finding(s)")
    detail = ascii_safe(f"{model_name} | {when.isoformat()}")

    pad = max(6, round(width * 0.005))
    text_width = max(
        draw.textlength(title, font=title_font), draw.textlength(detail, font=small_font)
    )
    line_height = title_font.size + small_font.size + pad
    box = (margin, margin, margin + text_width + 2 * pad, margin + line_height + 2 * pad)

    draw.rectangle(box, fill=WHITE, outline=RED, width=max(2, round(width * 0.0015)))
    draw.text((box[0] + pad, box[1] + pad), title, fill=RED, font=title_font)
    draw.text(
        (box[0] + pad, box[1] + pad + title_font.size + pad / 2),
        detail,
        fill=GREY,
        font=small_font,
    )
    return box


def _draw_pen_label(
    draw: ImageDraw.ImageDraw,
    text: str,
    anchor: Box,
    slot: int,
    colour,
    struck: bool,
    size: tuple[int, int],
) -> Box:
    """Write a short note beside ``anchor``: to its right, or to its left if that runs off.

    ``slot`` stacks several notes for the same cloud. A refused edit's note gets a line
    through it, rising a little as a hand does. Returns the note's box.
    """
    width, height = size
    pen = width * PEN_SIZE_RATIO
    unit = width / 1000
    font = _pen_font_for(text, pen)
    text_width = draw.textlength(text, font=font)
    gap = 6 * unit
    x = anchor[2] + gap
    if x + text_width > width - 4 * unit:
        x = anchor[0] - gap - text_width
    x = min(max(x, 4 * unit), width - 4 * unit - text_width)
    middle = (anchor[1] + anchor[3]) / 2 + slot * pen * 1.05
    y = min(max(middle, pen / 2), height - pen / 2)
    draw.text((x, y), text, fill=colour, font=font, anchor="lm")
    if struck:
        line = max(2, round(width * CLOUD_LINE_RATIO * 0.8))
        draw.line(
            [(x - 2 * unit, y + 1.5 * unit), (x + text_width + 2 * unit, y - 1.5 * unit)],
            fill=colour,
            width=line,
        )
    return (x, y - pen / 2, x + text_width, y + pen / 2)


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _stamp_spot(image: Image.Image, avoid: list[Box]) -> tuple[float, float]:
    """The clearest spot for the stamp's centre, nearest the lower middle; same rule as
    ``Sheet.stampSpot``: touch no mark, sit on (almost) no ink, else go lower middle."""
    width, height = image.size
    w, h = width * STAMP_WIDTH_RATIO * 1.06, width * STAMP_HEIGHT_RATIO * 1.25
    margin = width * 0.01
    fallback = (width / 2, height * 0.78)

    scale = 240 / width
    small = image.convert("L").resize(
        (max(1, round(width * scale)), max(1, round(height * scale)))
    )
    grey = small.load()

    def darkness(box: Box) -> float:
        cx0, cy0 = max(0, math.floor(box[0] * scale)), max(0, math.floor(box[1] * scale))
        cx1 = min(small.width, math.ceil(box[2] * scale))
        cy1 = min(small.height, math.ceil(box[3] * scale))
        cells = [(x, y) for y in range(cy0, cy1) for x in range(cx0, cx1)]
        if not cells:
            return 1.0
        return sum(1 for x, y in cells if grey[x, y] < 200) / len(cells)

    pad = width * 0.004
    best: tuple[float, float, float] | None = None
    steps = 9
    for row in range(steps):
        for column in range(steps):
            cx = margin + w / 2 + column / (steps - 1) * (width - 2 * margin - w)
            cy = margin + h / 2 + row / (steps - 1) * (height - 2 * margin - h)
            box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            if any(_overlaps(box, (o[0] - pad, o[1] - pad, o[2] + pad, o[3] + pad)) for o in avoid):
                continue
            dark = darkness(box)
            if dark > 0.02:
                continue
            score = dark * 20 + math.hypot((cx - fallback[0]) / width, (cy - fallback[1]) / height)
            if best is None or score < best[0]:
                best = (score, cx, cy)
    return (best[1], best[2]) if best else fallback


def _checked_stamp(width: int, when: date_type, verified: int, designer: int) -> Image.Image:
    """The CHECKED stamp on its own transparent layer, already rotated -7°."""
    w, h = width * STAMP_WIDTH_RATIO, width * STAMP_HEIGHT_RATIO
    unit = width / 1000
    layer = Image.new("RGBA", (math.ceil(w + 8 * unit), math.ceil(h + 8 * unit)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    ink = (*PEN_GREEN, STAMP_ALPHA)
    x0, y0 = 4 * unit, 4 * unit
    draw.rounded_rectangle(
        (x0, y0, x0 + w, y0 + h), radius=7 * unit, outline=ink, width=max(2, round(3 * unit))
    )
    inset = 4.5 * unit
    draw.rounded_rectangle(
        (x0 + inset, y0 + inset, x0 + w - inset, y0 + h - inset),
        radius=4 * unit,
        outline=ink,
        width=max(1, round(1.4 * unit)),
    )
    middle = x0 + w / 2
    title = _bundled_font(ARCHIVO_FONT, round(h * 0.34), bold=True)
    small = _bundled_font(ARCHIVO_FONT, round(h * 0.105), bold=True)
    draw.text((middle, y0 + h * 0.36), "CHECKED", fill=ink, font=title, anchor="mm")
    draw.text(
        (middle, y0 + h * 0.66),
        f"DRAFTCHECK RULE ENGINE  {when.isoformat()}",
        fill=ink,
        font=small,
        anchor="mm",
    )
    draw.text(
        (middle, y0 + h * 0.83),
        f"{verified} VERIFIED, {designer} FOR DESIGNER",
        fill=ink,
        font=small,
        anchor="mm",
    )
    # Pillow turns counter-clockwise for a positive angle, which is SVG's rotate(-7).
    return layer.rotate(7, resample=Image.Resampling.BICUBIC, expand=True)


def _item_bbox(drawing: Drawing, item_id: str | None) -> list[float] | None:
    """The bounding box of the dimension, frame, or datum with this id, if it has one."""
    for list_name in ("dimensions", "fcfs", "datums"):
        item = _find_item(drawing, list_name, item_id)
        if item is not None:
            return item.bbox
    return None


def _mark_drawing(
    page_png: bytes,
    findings: list[Finding],
    report: CorrectionReport | None,
    model_name: str,
    when: date_type,
) -> Image.Image:
    """Page 1: the drawing with a cloud, delta, and pen notes over every located finding."""
    with Image.open(io.BytesIO(page_png)) as source:
        source.load()
        image = source.convert("RGB")

    size = image.size
    draw = ImageDraw.Draw(image)
    pad = image.width * CLOUD_PAD_RATIO
    outcomes = finding_outcomes(report) if report else {}

    rings: dict[str, Box] = {}
    colours: dict[str, tuple[int, int, int]] = {}
    marks: list[Box] = []
    shared: dict[Box, int] = defaultdict(int)
    for number, finding in enumerate(findings, start=1):
        if finding.bbox is None:
            continue
        key = finding_key(finding)
        colour = OUTCOME_COLOURS[outcomes.get(key, "unresolved") if report else "open"]
        ring = _box_pixels(finding.bbox, image.width, image.height, pad)
        _draw_cloud(draw, ring, colour, image.width)
        delta = _delta_box(ring, size, shared[ring])
        shared[ring] += 1
        _draw_delta(draw, delta, number, colour)
        rings[key], colours[key] = ring, colour
        marks.extend([ring, delta])

    if report is not None:
        slots: dict[Box, int] = defaultdict(int)
        for row in corrections(report):
            anchor_key = next((key for key in row.finding_ids if key in rings), None)
            if anchor_key is not None:
                anchor, colour = rings[anchor_key], colours[anchor_key]
            else:
                bbox = _item_bbox(report.original_drawing, row.display.target_id)
                if bbox is None:
                    continue
                anchor = _box_pixels(bbox, image.width, image.height, 0)
                colour = PEN_GREEN if row.status == "verified" else PEN_AMBER
            rejected = row.status == "rejected"
            note = _draw_pen_label(
                draw,
                row.display.pen,
                anchor,
                slots[anchor],
                PEN_RED if rejected else colour,
                rejected,
                size,
            )
            slots[anchor] += 1
            marks.append(note)

    marks.append(_draw_review_stamp(draw, image.width, len(findings), model_name, when))

    if report is not None:
        verified = sum(1 for outcome in outcomes.values() if outcome == "verified")
        stamp = _checked_stamp(image.width, when, verified, len(outcomes) - verified)
        # A sheet shorter than the stamp (a thin strip) still gets it, cut to fit.
        stamp = stamp.crop((0, 0, min(stamp.width, image.width), min(stamp.height, image.height)))
        cx, cy = _stamp_spot(image, marks)
        left = round(min(max(cx - stamp.width / 2, 0), max(image.width - stamp.width, 0)))
        top = round(min(max(cy - stamp.height / 2, 0), max(image.height - stamp.height, 0)))
        canvas = image.convert("RGBA")
        canvas.alpha_composite(stamp, dest=(left, top))
        image = canvas.convert("RGB")

    return image


def _edits_by_finding(
    report: CorrectionReport,
) -> tuple[dict[str, list[Edit]], list[Edit]]:
    """Group the applied edits by the finding each one says it addresses.

    ``finding_ids`` is the model's own claim. It is used here to decide which heading an
    edit is printed under and for nothing else — whether a finding actually cleared comes
    from ``report.statuses``, which the agent computes by re-running the rules. An edit
    that cites no finding is returned separately so it still appears on the sheet.
    """
    grouped: dict[str, list[Edit]] = defaultdict(list)
    unattributed: list[Edit] = []
    for record in report.rounds:
        for edit in record.applied:
            if edit.finding_ids:
                for key in edit.finding_ids:
                    grouped[key].append(edit)
            else:
                unattributed.append(edit)
    return grouped, unattributed


#: How a geometric characteristic is said out loud, where that differs from the enum.
_CHARACTERISTIC_WORDS = {
    Characteristic.PROFILE_LINE: "line profile",
    Characteristic.PROFILE_SURFACE: "surface profile",
}

#: What each editable field is called in plain English, per target.
_FIELD_WORDS = {
    "text_raw": "callout text",
    "value": "value",
    "unit": "unit",
    "feature_type": "feature type",
    "quantity": "quantity",
    "tol_plus": "upper tolerance",
    "tol_minus": "lower tolerance",
    "from_ref": "measured-from reference",
    "to_ref": "measured-to reference",
    "characteristic": "characteristic",
    "tolerance": "tolerance",
    "diameter_zone": "diameter zone",
    "material_modifier": "material modifier",
    "label": "label",
    "location": "location",
    "units": "units",
    "general_tolerance": "general tolerance",
}


def _number(value: Any) -> str:
    """A number the way a drawing writes it: ``50``, not ``50.0``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f"{value:g}"


def _words(reference: str) -> str:
    """Turn an identifier like ``hole_1_center`` into ``hole 1 center``."""
    return reference.replace("_", " ")


def _plain_value(value: Any) -> str:
    """Say a field's new value the way a person would read it off the sheet."""
    if isinstance(value, dict):
        # A general tolerance arrives either as a bare number or as its object form.
        tolerance = value.get("linear_plus_minus")
        return f"±{_number(tolerance)}" if tolerance is not None else "a new value"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _number(value)


class EditNames:
    """Says which item an edit is about, in the words printed on the drawing.

    Items are named from the drawing as it was *submitted*, so "the 20 dimension" means
    the one the reader is looking at rather than the one an earlier edit turned it into.
    Anything the loop added along the way is looked up in the corrected drawing instead,
    since it does not exist in the original at all.

    Public because the API says the same sentences on the page as the redline prints on
    paper: ``api._summarize`` builds one of these per report and hands it to
    :func:`plain_edit`, so the wording lives here and nowhere else.
    """

    def __init__(self, report: CorrectionReport):
        self.drawings = (report.original_drawing, report.final_drawing)

    def _find(self, list_name: str, item_id: str | None):
        for drawing in self.drawings:
            for item in getattr(drawing, list_name):
                if item.id == item_id:
                    return item
        return None

    def fcf(self, fcf_id: str | None) -> str:
        frame = self._find("fcfs", fcf_id)
        if frame is None:
            return f"frame {fcf_id}"
        characteristic = _CHARACTERISTIC_WORDS.get(
            frame.characteristic, _words(frame.characteristic.value)
        )
        name = f"the {characteristic} frame"
        # Two position frames on one sheet need telling apart, and the tolerance is what
        # the reader can see printed in them.
        siblings = [
            other
            for other in self.drawings[0].fcfs + self.drawings[1].fcfs
            if other.characteristic == frame.characteristic
        ]
        if len({other.id for other in siblings}) > 1:
            name = f"{name} ({_number(frame.tolerance)})"
        return name

    def dimension(self, dimension_id: str | None) -> str:
        dimension = self._find("dimensions", dimension_id)
        if dimension is None:
            return f"dimension {dimension_id}"
        if dimension.text_raw:
            return f"the {dimension.text_raw} dimension"
        unit = f" {dimension.unit.value}" if dimension.unit else ""
        return f"the {_number(dimension.value)}{unit} dimension"

    def datum(self, datum_id: str | None) -> str:
        datum = self._find("datums", datum_id)
        return f"datum {datum.label}" if datum is not None else f"datum {datum_id}"

    def item(self, target: str, item_id: str | None) -> str:
        if target == "fcf":
            return self.fcf(item_id)
        if target == "dimension":
            return self.dimension(item_id)
        if target == "datum":
            return self.datum(item_id)
        return "the drawing"


def _plain_set_field(edit: SetField, names: EditNames) -> str:
    """Plain English for a ``set_field``, which covers most of what the agent does."""
    subject = names.item(edit.target, edit.id)
    if edit.target == "drawing":
        field = _FIELD_WORDS.get(edit.field, _words(edit.field))
        value = _plain_value(edit.value)
        # "Set the general tolerance to 0.1" loses the half the reader cares about: a
        # general tolerance is a band either side, and the sheet prints it with the sign.
        if edit.field == "general_tolerance" and not value.startswith("±"):
            value = f"±{value}"
        return f"Set the {field} to {value}"

    # The two flags read as a statement about the dimension, not as a field assignment.
    if edit.field in ("is_basic", "is_reference"):
        kind = "basic" if edit.field == "is_basic" else "a reference dimension"
        return f"Mark {subject} as {kind}" if edit.value else f"Mark {subject} as not {kind}"

    field = _FIELD_WORDS.get(edit.field, _words(edit.field))
    if edit.value is None:
        return f"Remove the {field} from {subject}"
    return f"Set the {field} of {subject} to {_plain_value(edit.value)}"


def _plain_add_dimension(edit: AddDimension) -> str:
    """Plain English for an added dimension, from whichever fields were given."""
    spec = edit.dimension
    unit = f" {spec['unit']}" if spec.get("unit") else ""
    size = _number(spec.get("value")) if spec.get("value") is not None else "a"
    basic = "basic " if spec.get("is_basic") else ""
    sentence = f"Add a {basic}{size}{unit} dimension"

    start, end = spec.get("from_ref"), spec.get("to_ref")
    if start and end:
        sentence = f"{sentence} from the {_words(start)} to the {_words(end)}"
    return sentence


def plain_edit(edit: Edit, names: EditNames) -> str:
    """Say what one applied edit did, without ids or operation names.

    The redline is read by an engineer deciding whether the correction is right, not by
    anyone debugging the agent. ``remove_datum_ref datum C from frame fcf_1`` is the right
    wording for ``corrections.json`` and the terminal, where the ids are what you need;
    here it is a sentence. ``Edit.describe()`` is left alone so those keep the ids.
    """
    if isinstance(edit, SetField):
        return _plain_set_field(edit, names)
    if isinstance(edit, AddDatum):
        return f"Add datum {edit.label.upper()} at {edit.location}"
    if isinstance(edit, RemoveDatumRef):
        return f"Remove datum {edit.label.upper()} from {names.fcf(edit.fcf_id)}"
    if isinstance(edit, AddDimension):
        return _plain_add_dimension(edit)
    return edit.describe()


# ---- Short forms, for the page's diff lines and pen labels ----------------------------
#
# The page shows each correction twice: as a before/after pair in the Issues card, the way
# the callout reads on the sheet, and as a two-word note written beside its cloud. Both
# are worded here, beside ``plain_edit``, so the browser never builds a sentence of its own.

#: The symbol each characteristic is printed with in a feature control frame.
_CHARACTERISTIC_SYMBOLS = {
    Characteristic.POSITION: "⌖",
    Characteristic.FLATNESS: "⏥",
    Characteristic.STRAIGHTNESS: "⏤",
    Characteristic.CIRCULARITY: "○",
    Characteristic.CYLINDRICITY: "⌭",
    Characteristic.PERPENDICULARITY: "⟂",
    Characteristic.PARALLELISM: "∥",
    Characteristic.ANGULARITY: "∠",
    Characteristic.PROFILE_LINE: "⌒",
    Characteristic.PROFILE_SURFACE: "⌓",
    Characteristic.CIRCULAR_RUNOUT: "↗",
    Characteristic.TOTAL_RUNOUT: "⌰",
    Characteristic.CONCENTRICITY: "◎",
    Characteristic.SYMMETRY: "⌯",
}

#: Material condition modifiers as the circled letters a frame prints.
_MODIFIER_SYMBOLS = {"MMC": "Ⓜ", "LMC": "Ⓛ"}

#: What the "before" of an addition reads as: there was nothing there.
NOTHING = "—"

#: Fields whose new value is a ± band, so the pen label carries the sign.
_PLUS_MINUS_FIELDS = ("tol_plus", "tol_minus", "general_tolerance")


def short_fcf(frame: FCF) -> str:
    """A frame the way it reads on the sheet, e.g. ``⌖ Ø0.25 Ⓜ | A | B | C``."""
    symbol = _CHARACTERISTIC_SYMBOLS.get(frame.characteristic, frame.characteristic.value)
    zone = "Ø" if frame.diameter_zone else ""
    head = f"{symbol} {zone}{_number(frame.tolerance)}"
    if frame.material_modifier in _MODIFIER_SYMBOLS:
        head = f"{head} {_MODIFIER_SYMBOLS[frame.material_modifier]}"
    refs = [
        f"{ref.label} {_MODIFIER_SYMBOLS[ref.modifier]}"
        if ref.modifier in _MODIFIER_SYMBOLS
        else ref.label
        for ref in frame.datum_refs
    ]
    return " | ".join([head, *refs])


def short_dimension(dimension: Dimension) -> str:
    """A dimension the way it reads on the sheet: ``2X Ø8 ±0.1``, ``[80]`` when basic."""
    count = f"{dimension.quantity}X " if dimension.quantity > 1 else ""
    diameter = "Ø" if dimension.feature_type == "diameter" else ""
    size = f"{count}{diameter}{_number(dimension.value)}"
    if dimension.is_basic:
        return f"[{size}]"
    if dimension.is_reference:
        return f"({size})"
    plus, minus = dimension.tol_plus, dimension.tol_minus
    if plus is not None and plus == minus:
        return f"{size} ±{_number(plus)}"
    if plus is not None or minus is not None:
        return f"{size} +{_number(plus or 0)}/−{_number(minus or 0)}"
    return size


def _short_drawing_field(drawing: Drawing, field: str) -> str:
    """One of the drawing's own two values, as a short line."""
    if field == "units":
        return f"units {drawing.units.value}" if drawing.units else "no units"
    tolerance = drawing.general_tolerance
    if tolerance is not None and tolerance.linear_plus_minus is not None:
        return f"general ±{_number(tolerance.linear_plus_minus)}"
    return "no general tolerance"


def _find_item(drawing: Drawing, list_name: str, item_id: str | None):
    for item in getattr(drawing, list_name):
        if item.id == item_id:
            return item
    return None


#: Which list a ``set_field`` target lives in, and how an item of it is written short.
_TARGETS = {
    "fcf": ("fcfs", short_fcf),
    "dimension": ("dimensions", short_dimension),
    "datum": ("datums", lambda datum: f"datum {datum.label}"),
}


def edit_target_id(edit: Edit) -> str | None:
    """The id of the item an edit changes, or ``None`` for an addition or the drawing."""
    if isinstance(edit, RemoveDatumRef):
        return edit.fcf_id
    if isinstance(edit, SetField) and edit.target != "drawing":
        return edit.id
    return None


def _short_item(drawing: Drawing, edit: Edit) -> str | None:
    """The item ``edit`` touches as it reads in ``drawing``, or ``None`` if it is not there."""
    if isinstance(edit, RemoveDatumRef):
        frame = _find_item(drawing, "fcfs", edit.fcf_id)
        return short_fcf(frame) if frame is not None else None
    if isinstance(edit, SetField):
        if edit.target == "drawing":
            return _short_drawing_field(drawing, edit.field)
        list_name, short = _TARGETS[edit.target]
        item = _find_item(drawing, list_name, edit.id)
        return short(item) if item is not None else None
    return None


def _proposed_item(drawing: Drawing, edit: Edit) -> str:
    """What a *refused* edit would have made the item read, worked out on a copy.

    A refused edit never reached the corrected drawing, so there is nothing to read its
    "after" from. The page still shows what was asked for — struck through — so this
    applies the one change to a copy of the item, re-validated, and writes that. If even
    that cannot be read, the requested value is written as it was sent.
    """
    if isinstance(edit, RemoveDatumRef):
        frame = _find_item(drawing, "fcfs", edit.fcf_id)
        if frame is None:
            return f"drop {edit.label.upper()}"
        kept = [ref for ref in frame.datum_refs if ref.label != edit.label.upper()]
        return short_fcf(frame.model_copy(update={"datum_refs": kept}))
    if isinstance(edit, SetField):
        fallback = f"{_FIELD_WORDS.get(edit.field, _words(edit.field))} {_plain_value(edit.value)}"
        if edit.target == "drawing":
            try:
                changed = Drawing.model_validate({**drawing.model_dump(), edit.field: edit.value})
            except ValidationError:
                return fallback
            return _short_drawing_field(changed, edit.field)
        list_name, short = _TARGETS[edit.target]
        item = _find_item(drawing, list_name, edit.id)
        if item is None:
            return fallback
        try:
            changed = type(item).model_validate({**item.model_dump(), edit.field: edit.value})
        except ValidationError:
            return fallback
        return short(changed)
    if isinstance(edit, AddDatum):
        return f"datum {edit.label.upper()}"
    if isinstance(edit, AddDimension):
        try:
            return short_dimension(Dimension.model_validate(edit.dimension))
        except ValidationError:
            return "a new dimension"
    return NOTHING


def _added_dimension(before: Drawing, after: Drawing) -> Dimension | None:
    """The dimension that is in ``after`` and was not in ``before``."""
    was = {dimension.id for dimension in before.dimensions}
    for dimension in after.dimensions:
        if dimension.id not in was:
            return dimension
    return None


def pen_label(edit: Edit, drawing: Drawing) -> str:
    """Two or three words a checker would write beside the cloud, e.g. ``drop C``.

    ``drawing`` is the drawing the edit was made to, for the one case (a frame's tolerance)
    where how a value is written depends on the item it lands on.
    """
    if isinstance(edit, RemoveDatumRef):
        return f"drop {edit.label.upper()}"
    if isinstance(edit, AddDatum):
        return f"add datum {edit.label.upper()}"
    if isinstance(edit, AddDimension):
        value = edit.dimension.get("value")
        return f"add {_number(value)}" if isinstance(value, (int, float)) else "add dimension"

    field, value = edit.field, edit.value
    if field in _PLUS_MINUS_FIELDS and value is not None:
        written = _plain_value(value)
        return written if written.startswith("±") else f"±{written}"
    if field == "tolerance" and isinstance(value, (int, float)):
        # A frame's zone is a width, not a band either side, so it carries no ±.
        frame = _find_item(drawing, "fcfs", edit.id)
        zone = "Ø" if frame is not None and frame.diameter_zone else ""
        return f"{zone}{_number(value)}"
    if field == "value":
        return _plain_value(value)
    if field in ("is_basic", "is_reference"):
        kind = "basic" if field == "is_basic" else "ref"
        return f"make {kind}" if value else f"not {kind}"
    if field == "material_modifier":
        return str(value).upper() if value else "no modifier"
    if field == "units" and value:
        return f"units {value}"
    words = _FIELD_WORDS.get(field, _words(field)).split()
    verb = "clear" if value is None else "set"
    return " ".join([verb, *words][:3])


@dataclass(frozen=True)
class EditDisplay:
    """One edit as the page draws it: the callout before and after, and the pen note."""

    before: str
    after: str
    pen: str
    #: The item the edit changes, so the page can put the pen note beside its box.
    target_id: str | None


def edit_display(edit: Edit, before: Drawing, after: Drawing | None) -> EditDisplay:
    """Short before/after strings for one edit, read off the drawing either side of it.

    ``after`` is the drawing with this edit applied, or ``None`` for an edit the guards
    refused, whose "after" is then what it asked for (:func:`_proposed_item`).
    """
    was = _short_item(before, edit)
    if after is None:
        now = _proposed_item(before, edit)
    elif isinstance(edit, AddDimension):
        added = _added_dimension(before, after)
        now = short_dimension(added) if added is not None else _proposed_item(before, edit)
    elif isinstance(edit, AddDatum):
        now = f"datum {edit.label.upper()}"
    else:
        now = _short_item(after, edit) or _proposed_item(before, edit)
    return EditDisplay(
        before=was if was is not None else NOTHING,
        after=now,
        pen=pen_label(edit, before),
        target_id=edit_target_id(edit),
    )


@dataclass(frozen=True)
class Correction:
    """One proposed edit as the redline shows it: what changed, and what came of it."""

    display: EditDisplay
    #: ``verified``, ``held``, ``failed``, or ``rejected`` — the same words /fix uses.
    status: str
    finding_ids: tuple[str, ...]


def corrections(report: CorrectionReport) -> list[Correction]:
    """Every applied and refused edit, in order, with the same strings and verdicts as /fix.

    The report keeps each round's edits but not the drawing between them, so the applied
    edits are replayed one at a time to read each before/after off the drawing as it was
    either side — the same walk ``api._round_summary`` does. A refused edit is shown against
    the drawing as its round found it, and one that cannot be read as an edit is left out:
    there is nothing to write beside a cloud for it.
    """
    # Imported here because the API imports this module; the verdict is worded in one place.
    from draftcheck.api import edit_verdict

    rows: list[Correction] = []
    current = report.original_drawing
    for record in report.rounds:
        start = current
        for edit in record.applied:
            changed, took, _ = apply_edits(current, [edit])
            after = changed if took else current
            status, _ = edit_verdict(edit, record)
            rows.append(
                Correction(edit_display(edit, current, after), status, tuple(edit.finding_ids))
            )
            current = after
        for item in record.rejected:
            edit, _ = parse_edit(item.edit)
            if edit is None:
                continue
            rows.append(
                Correction(edit_display(edit, start, None), "rejected", tuple(edit.finding_ids))
            )
    return rows


def finding_outcomes(report: CorrectionReport) -> dict[str, str]:
    """Each original finding's outcome: ``verified``, ``held``, or ``unresolved``.

    Whether a finding cleared comes from ``report.statuses`` (the rules, re-run). A cleared
    finding is ``held`` when the edit that cleared it adds something a designer must place.
    Same split as ``api._outcomes``, so a cloud is the colour its card is on screen.
    """
    held = {
        key for row in corrections(report) if row.status == "held" for key in row.finding_ids
    }
    outcomes = {}
    for status in report.statuses:
        if status.status != "resolved":
            outcomes[status.key] = "unresolved"
        else:
            outcomes[status.key] = "held" if status.key in held else "verified"
    return outcomes


class _Column:
    """A cursor that lays text down a page and knows when it has run out of room."""

    def __init__(self, draw: ImageDraw.ImageDraw, left: float, right: float, bottom: float):
        self.draw = draw
        self.left = left
        self.right = right
        self.bottom = bottom
        self.y = 0.0

    @property
    def full(self) -> bool:
        return self.y >= self.bottom

    def write(self, text: str, font, fill=INK, indent: float = 0.0) -> None:
        """Write one wrapped block of text, stopping at the bottom of the page."""
        x = self.left + indent
        for line in _wrap(self.draw, text, font, self.right - x):
            if self.full:
                return
            self.draw.text((x, self.y), line, fill=fill, font=font)
            self.y += font.size * 1.35

    def space(self, amount: float) -> None:
        self.y += amount

    def row(self, cells: list[str], size: float, fill=INK, bold: bool = False) -> None:
        """Write one table row, each cell at its column and cut short if it is too wide."""
        if self.full:
            return
        span = self.right - self.left
        edges = [self.left + span * start for start in TABLE_COLUMNS] + [self.right]
        for index, cell in enumerate(cells[: len(TABLE_COLUMNS)]):
            room = edges[index + 1] - edges[index] - size * 0.6
            _draw_mixed(self.draw, (edges[index], self.y), cell, size, fill, room, bold)
        self.y += size * 1.35


#: What to write for a frame symbol none of the bundled fonts can draw.
_SYMBOL_WORDS = {
    "⌖": "pos",
    "Ⓜ": "(M)",
    "Ⓛ": "(L)",
    "⏥": "flat",
    "⏤": "straight",
    "○": "circ",
    "⌭": "cyl",
    "⟂": "perp",
    "∥": "par",
    "∠": "ang",
    "⌒": "line prof",
    "⌓": "surf prof",
    "↗": "runout",
    "⌰": "total runout",
    "◎": "conc",
    "⌯": "sym",
}


def _runs(text: str) -> list[tuple[str, Path]]:
    """Split ``text`` into runs, each in the first bundled font that has its glyphs."""
    runs: list[tuple[str, Path]] = []
    for character in text:
        path = next((p for p in (ARCHIVO_FONT, MONO_FONT) if _has_glyph(p, character)), None)
        if path is None:
            character, path = _SYMBOL_WORDS.get(character, ascii_safe(character)), ARCHIVO_FONT
        if runs and runs[-1][1] == path:
            runs[-1] = (runs[-1][0] + character, path)
        else:
            runs.append((character, path))
    return runs


def _draw_mixed(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    size: float,
    fill,
    room: float,
    bold: bool = False,
) -> None:
    """Draw ``text`` with per-character font fallback, stopping with ``...`` at ``room``."""
    x, y = xy
    limit = x + room
    for chunk, path in _runs(text):
        font = _bundled_font(path, round(size), bold=bold)
        for character in chunk:
            advance = draw.textlength(character, font=font)
            if x + advance > limit:
                draw.text((x, y), "...", fill=fill, font=_bundled_font(ARCHIVO_FONT, round(size)))
                return
            draw.text((x, y), character, fill=fill, font=font)
            x += advance


#: ``(style, indent level)`` for each kind of line page 2 can carry. Keeping the wording
#: separate from the drawing is what lets the tests read the page as text instead of
#: guessing at pixels.
HEADING = "heading"
ENTRY = "entry"
BODY = "body"
DETAIL = "detail"
VERDICT = "verdict"
NOTE = "note"
#: One rule's line in "Checks run": spaced like an entry, but not counted as a finding.
CHECK = "check"
#: The Corrections table's header and its rows; cells are separated by tabs.
TABLE_HEAD = "table_head"
TABLE_ROW = "table_row"


@dataclass(frozen=True)
class Line:
    """One line of page 2, before it is laid out."""

    text: str
    style: str
    #: How far in from the left margin, in multiples of the body text size.
    indent: float = 0.0


def _describe_status(status: str) -> str:
    """The words that go on the sheet for a resolved/unresolved verdict."""
    return "verified resolved" if status == "resolved" else "unresolved"


def findings_text(
    findings: list[Finding],
    report: CorrectionReport | None = None,
    rule_results: list[RuleResult] | None = None,
    final_rule_results: list[RuleResult] | None = None,
) -> list[Line]:
    """Everything page 2 says, in order, as styled lines.

    Findings are numbered to match the markers on page 1, which means they are numbered in
    ``run_rules`` order. A finding with no bounding box has nothing to mark, so its entry
    says ``location not marked`` — the list is the only place it appears.

    Item ids are deliberately absent. The reader of this page has the drawing in front of
    them and decides whether a correction is right; ``F1`` means nothing to them. The ids
    stay in ``findings.json`` and ``corrections.json``, where they are the point.
    """
    statuses = {status.key: status.status for status in report.statuses} if report else {}
    grouped, unattributed = _edits_by_finding(report) if report else ({}, [])
    names = EditNames(report) if report else None

    lines: list[Line] = [Line("DraftCheck findings", HEADING)]
    if not findings:
        lines.append(Line("No findings.", BODY))

    for number, finding in enumerate(findings, start=1):
        key = finding_key(finding)
        lines.append(Line(f"{number}. {finding.rule_id}  [{finding.severity}]", ENTRY))
        lines.append(Line(finding.message, BODY, indent=1.6))

        if finding.bbox is None:
            lines.append(Line("location not marked", DETAIL, indent=1.6))

        if report is None:
            continue

        edits = grouped.get(key, [])
        for edit in edits:
            lines.append(Line(f"fix: {plain_edit(edit, names)}", BODY, indent=1.6))
            if edit.rationale:
                lines.append(Line(f"why: {edit.rationale}", DETAIL, indent=3.0))
        if not edits:
            lines.append(Line("fix: no edit was applied for this finding", BODY, indent=1.6))

        status = statuses.get(key, "unresolved")
        style = DETAIL if status == "resolved" else VERDICT
        lines.append(Line(f"status: {_describe_status(status)}", style, indent=1.6))

    if unattributed:
        lines.append(Line("Other applied edits", ENTRY))
        for edit in unattributed:
            lines.append(Line(f"fix: {plain_edit(edit, names)}", DETAIL, indent=1.6))

    if report is not None:
        lines.extend(corrections_text(findings, report))

    if rule_results:
        lines.extend(checks_text(rule_results, final_rule_results))

    return lines


#: How each edit's verdict is written in the Corrections table, as the page words it.
CORRECTION_STATUS_WORDS = {
    "verified": "Verified",
    "held": "Held for designer",
    "failed": "Not verified",
    "rejected": "Rejected",
}

#: Each Corrections cell's left edge, as a fraction of the text width.
TABLE_COLUMNS = (0.0, 0.08, 0.42, 0.76)


def corrections_text(findings: list[Finding], report: CorrectionReport) -> list[Line]:
    """The "Corrections" table: issue number, the callout before and after, and verdict.

    Cells are tab-separated in ``Line.text`` so the tests can read the table as text; the
    layout puts each cell in its column. The before/after strings are ``edit_display``'s,
    the same ones the page shows in its Issues card.
    """
    numbers = {finding_key(finding): number for number, finding in enumerate(findings, 1)}
    lines = [Line("Corrections", HEADING), Line("#\tBefore\tAfter\tStatus", TABLE_HEAD)]
    rows = corrections(report)
    if not rows:
        lines.append(Line("No edits were proposed.", DETAIL))
    for row in rows:
        cited = ", ".join(str(numbers[key]) for key in row.finding_ids if key in numbers)
        status = CORRECTION_STATUS_WORDS.get(row.status, row.status)
        cells = [cited or "-", row.display.before, row.display.after, status]
        lines.append(Line("\t".join(cells), TABLE_ROW))
    return lines


#: How each rule status is written on the sheet.
_STATUS_WORDS = {"pass": "PASS", "fail": "FAIL", "not_applicable": "N/A"}


#: The two summary lines' lead-ins, shared with the page's wording.
SUBMITTED_LEAD = "On the submitted drawing"
AFTER_FIXES_LEAD = "After the agent's verified fixes"


def checks_text(
    rule_results: list[RuleResult], final_rule_results: list[RuleResult] | None = None
) -> list[Line]:
    """The "Checks run" section: every rule, its status, and the sentence explaining it.

    Passing rules are listed too, so the reader can see what was looked at and found
    fine, not only what went wrong. The list is for the drawing as submitted, like the
    rest of the page; with ``final_rule_results`` (a fix ran) a second summary line gives
    the count after the fixes, from a fresh run of the rules on the corrected drawing.
    """
    lines = [Line("Checks run", HEADING)]
    if final_rule_results:
        lines.append(Line(f"{SUBMITTED_LEAD}: {checks_summary(rule_results)}", DETAIL))
        lines.append(
            Line(f"{AFTER_FIXES_LEAD}: {checks_summary(final_rule_results)}", DETAIL)
        )
    else:
        lines.append(Line(checks_summary(rule_results), DETAIL))
    for result in rule_results:
        status = _STATUS_WORDS.get(result.status, result.status.upper())
        lines.append(Line(f"{status}  {result.rule_id}  {result.title}", CHECK))
        style = VERDICT if result.status == "fail" else DETAIL
        lines.append(Line(result.explanation, style, indent=1.6))
    return lines


def _list_findings(
    size: tuple[int, int],
    findings: list[Finding],
    report: CorrectionReport | None,
    rule_results: list[RuleResult] | None = None,
    final_rule_results: list[RuleResult] | None = None,
) -> Image.Image:
    """Page 2: a clean white sheet describing every finding, and any applied edits."""
    width, height = size
    image = Image.new("RGB", size, WHITE)
    draw = ImageDraw.Draw(image)

    # Sized so the sheet is readable on screen at full width, which is how anyone reviewing
    # it will actually look at it — a findings page nobody can read without zooming is not
    # an output, it is a file.
    margin = width * 0.05
    body_font = _font(width * 0.016)
    small_font = _font(width * 0.0145)
    footer_font = _font(width * 0.013)
    styles = {
        HEADING: (_font(width * 0.025), RED),
        ENTRY: (body_font, INK),
        BODY: (body_font, INK),
        DETAIL: (small_font, GREY),
        VERDICT: (small_font, RED),
        NOTE: (small_font, GREY),
        CHECK: (body_font, INK),
    }

    footer_lines = _wrap(draw, FOOTER, footer_font, width - 2 * margin)
    footer_height = len(footer_lines) * footer_font.size * 1.35 + margin * 0.5

    column = _Column(draw, margin, width - margin, height - margin - footer_height)
    column.y = margin

    lines = findings_text(findings, report, rule_results, final_rule_results)
    for index, line in enumerate(lines):
        if column.full:
            # Overflow is rare — a sheet with this many findings has bigger problems — but
            # it must not look like the list simply ended.
            remaining = sum(1 for rest in lines[index:] if rest.style == ENTRY)
            checks = sum(1 for rest in lines[index:] if rest.style == CHECK)
            column.bottom = height  # the note itself still has to fit
            if remaining:
                note = f"... {remaining} more finding(s) not shown; see findings.json."
            elif checks:
                note = f"... {checks} more check(s) not shown."
            else:
                note = "... the rest of this list is not shown."
            column.write(note, *styles[NOTE])
            break
        if line.style in (HEADING, ENTRY, CHECK):
            column.space(body_font.size * 0.6)
        if line.style in (TABLE_HEAD, TABLE_ROW):
            head = line.style == TABLE_HEAD
            column.row(line.text.split("\t"), body_font.size, GREY if head else INK, bold=head)
            continue
        font, fill = styles[line.style]
        column.write(line.text, font, fill=fill, indent=line.indent * body_font.size)

    footer_y = height - margin - footer_height + margin * 0.5
    for line_text in footer_lines:
        draw.text((margin, footer_y), line_text, fill=GREY, font=footer_font)
        footer_y += footer_font.size * 1.35

    return image


def render_redline(
    page_png: bytes,
    findings: list[Finding],
    report: CorrectionReport | None = None,
    model_name: str | None = None,
    when: date_type | None = None,
    rule_results: list[RuleResult] | None = None,
    final_rule_results: list[RuleResult] | None = None,
) -> tuple[Image.Image, Image.Image]:
    """Build the two redline pages for one run.

    Args:
        page_png: The rendered drawing, as ``render.load_pages`` returns it.
        findings: The findings for the drawing as submitted, in ``run_rules`` order. That
            order is what the markers are numbered by, so page 1, page 2, and
            ``findings.json`` all use the same numbering.
        report: The correction loop's report, if ``--fix`` ran. Adds each applied edit,
            its rationale, and the rule engine's resolved/unresolved verdict to page 2.
        model_name: Shown in the stamp. Defaults to saying no model was called.
        when: The review date in the stamp. Defaults to today.
        rule_results: Every rule's verdict on the submitted drawing, from
            ``rules.rule_results``. Adds a "Checks run" section to page 2.
        final_rule_results: The same for the corrected drawing, when a fix ran. Adds an
            "after the agent's verified fixes" summary line to that section.

    Returns:
        ``(page_1, page_2)`` as pillow images, both the size of the rendered drawing.
    """
    marked = _mark_drawing(
        page_png, findings, report, model_name or NO_MODEL, when or date_type.today()
    )
    return marked, _list_findings(
        marked.size, findings, report, rule_results, final_rule_results
    )


def save_redline(
    pages: tuple[Image.Image, Image.Image], path_pdf: str | Path, path_png: str | Path
) -> None:
    """Write the pages as one multi-page PDF, and page 1 on its own as a PNG."""
    first, *rest = list(pages)
    for path in (Path(path_pdf), Path(path_png)):
        path.parent.mkdir(parents=True, exist_ok=True)

    first.save(
        Path(path_pdf),
        format="PDF",
        save_all=True,
        append_images=rest,
        resolution=PDF_DPI,
    )
    first.save(Path(path_png), format="PNG")
