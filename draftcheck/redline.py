"""Turn a run's findings into a two-page redline a person can read.

Page 1 is the drawing itself with a numbered red marker over each finding's bounding box.
Page 2 is a plain white sheet listing every finding in words, and — when the correction
loop ran — the edits it applied, why, and whether the rule engine confirmed the finding
cleared.

The markup is drawn on the *rendered image* of the drawing rather than onto the PDF's own
content stream. That means one code path covers PDF and image inputs alike, and it needs
no library beyond pillow.

A mark is the shape a checker draws by hand: a red rectangle around the thing that is
wrong, and a numbered balloon on a short leader just outside it.

Nothing here decides anything. Severity, message, and resolved/unresolved all arrive
already decided — the last two from :mod:`draftcheck.agent`, which gets them by re-running
the rules. This module only lays them out.
"""

from __future__ import annotations

import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_type
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from draftcheck.agent import CorrectionReport
from draftcheck.edits import AddDatum, AddDimension, Edit, RemoveDatumRef, SetField
from draftcheck.schema import Characteristic, Finding, finding_key

RED = (200, 30, 30)
INK = (25, 25, 25)
GREY = (95, 95, 95)
WHITE = (255, 255, 255)

#: Marker geometry, all as a fraction of the page width so a mark looks the same on a
#: small sheet and a large one. The rectangle encloses the finding's bounding box; the
#: balloon carries its number and sits just outside one corner.
BOX_PAD_RATIO = 0.005
BOX_LINE_RATIO = 0.003
BALLOON_WIDTH_RATIO = 0.03

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


def _balloon_spot(
    box: tuple[float, float, float, float],
    radius: float,
    size: tuple[int, int],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Where the balloon goes, and which corner of the box its leader starts from.

    Top-right is the convention, and the other three corners are tried in turn when it
    would hang off the sheet — a balloon half over the edge is worse than one on the
    wrong corner. If the box is up against two edges at once, the last candidate is
    clamped into the page rather than dropped, so a finding never loses its number.
    """
    x0, y0, x1, y1 = box
    width, height = size
    offset = radius * 0.9

    candidates = (
        ((x1, y0), (x1 + offset, y0 - offset)),
        ((x0, y0), (x0 - offset, y0 - offset)),
        ((x1, y1), (x1 + offset, y1 + offset)),
        ((x0, y1), (x0 - offset, y1 + offset)),
    )
    for corner, (cx, cy) in candidates:
        if radius <= cx <= width - radius and radius <= cy <= height - radius:
            return corner, (cx, cy)

    corner, (cx, cy) = candidates[0]
    return corner, (
        min(max(cx, radius), width - radius),
        min(max(cy, radius), height - radius),
    )


def _draw_marker(
    draw: ImageDraw.ImageDraw,
    box: tuple[float, float, float, float],
    number: int,
    size: tuple[int, int],
) -> None:
    """Draw one redline mark: a rectangle round the box, and a numbered balloon."""
    width = size[0]
    line = max(2, round(width * BOX_LINE_RATIO))
    radius = width * BALLOON_WIDTH_RATIO / 2

    draw.rectangle(list(box), outline=RED, width=line)

    corner, (cx, cy) = _balloon_spot(box, radius, size)
    draw.line([corner, (cx, cy)], fill=RED, width=line)
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=RED)

    # No bold cut in the bundled font, so the stroke thickens the glyph instead. White on
    # a filled balloon is what makes the number readable over whatever it is sitting on.
    draw.text(
        (cx, cy),
        str(number),
        fill=WHITE,
        font=_font(radius * 1.15),
        anchor="mm",
        stroke_width=max(1, round(radius / 14)),
        stroke_fill=WHITE,
    )


def _draw_stamp(
    draw: ImageDraw.ImageDraw, width: int, count: int, model_name: str, when: date_type
) -> None:
    """Put the review's header in the top-left corner of the drawing."""
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
    box = [margin, margin, margin + text_width + 2 * pad, margin + line_height + 2 * pad]

    draw.rectangle(box, fill=WHITE, outline=RED, width=max(2, round(width * 0.0015)))
    draw.text((box[0] + pad, box[1] + pad), title, fill=RED, font=title_font)
    draw.text(
        (box[0] + pad, box[1] + pad + title_font.size + pad / 2),
        detail,
        fill=GREY,
        font=small_font,
    )


def _mark_drawing(
    page_png: bytes, findings: list[Finding], model_name: str, when: date_type
) -> Image.Image:
    """Page 1: the drawing with a numbered marker over every finding that has a bbox."""
    with Image.open(io.BytesIO(page_png)) as source:
        source.load()
        image = source.convert("RGB")

    draw = ImageDraw.Draw(image)
    pad = image.width * BOX_PAD_RATIO

    for number, finding in enumerate(findings, start=1):
        if finding.bbox is None:
            continue
        box = _box_pixels(finding.bbox, image.width, image.height, pad)
        _draw_marker(draw, box, number, image.size)

    _draw_stamp(draw, image.width, len(findings), model_name, when)
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


#: ``(style, indent level)`` for each kind of line page 2 can carry. Keeping the wording
#: separate from the drawing is what lets the tests read the page as text instead of
#: guessing at pixels.
HEADING = "heading"
ENTRY = "entry"
BODY = "body"
DETAIL = "detail"
VERDICT = "verdict"
NOTE = "note"


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
    findings: list[Finding], report: CorrectionReport | None = None
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

    return lines


def _list_findings(
    size: tuple[int, int], findings: list[Finding], report: CorrectionReport | None
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
    }

    footer_lines = _wrap(draw, FOOTER, footer_font, width - 2 * margin)
    footer_height = len(footer_lines) * footer_font.size * 1.35 + margin * 0.5

    column = _Column(draw, margin, width - margin, height - margin - footer_height)
    column.y = margin

    lines = findings_text(findings, report)
    for index, line in enumerate(lines):
        if column.full:
            # Overflow is rare — a sheet with this many findings has bigger problems — but
            # it must not look like the list simply ended.
            remaining = sum(1 for rest in lines[index:] if rest.style == ENTRY)
            column.bottom = height  # the note itself still has to fit
            column.write(
                f"... {remaining} more finding(s) not shown; see findings.json.",
                *styles[NOTE],
            )
            break
        if line.style in (HEADING, ENTRY):
            column.space(body_font.size * 0.6)
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

    Returns:
        ``(page_1, page_2)`` as pillow images, both the size of the rendered drawing.
    """
    marked = _mark_drawing(page_png, findings, model_name or NO_MODEL, when or date_type.today())
    return marked, _list_findings(marked.size, findings, report)


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
