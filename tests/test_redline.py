"""Tests for the two-page redline output. No network.

The drawing here is a blank white page rather than a real sheet, so "is this pixel red"
is a question about the markup and not about whatever the drawing had underneath.
"""

from __future__ import annotations

import io
from datetime import date

import pypdfium2 as pdfium
import pytest
from PIL import Image

from draftcheck.agent import CorrectionReport, correct
from draftcheck.edits import AddDatum, AddDimension, RemoveDatumRef, SetField
from draftcheck.redline import (
    BALLOON_WIDTH_RATIO,
    BOX_PAD_RATIO,
    FOOTER,
    EditNames,
    ascii_safe,
    findings_text,
    plain_edit,
    render_redline,
    save_redline,
)
from draftcheck.rules import run_rules
from draftcheck.schema import Drawing, Finding, parse_drawing

PAGE_SIZE = (1200, 900)


def blank_page_png(size: tuple[int, int] = PAGE_SIZE) -> bytes:
    """A white page in PNG bytes, standing in for a rendered drawing."""
    buffer = io.BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def make_finding(rule_id: str, bbox: list[float] | None, **overrides) -> Finding:
    """A finding with the fields these tests care about and defaults for the rest."""
    fields = {
        "rule_id": rule_id,
        "severity": "error",
        "message": f"{rule_id} went wrong.",
        "source": "test",
        "item_ids": ["F1"],
        "bbox": bbox,
    }
    fields.update(overrides)
    return Finding(**fields)


def is_red(pixel: tuple[int, int, int]) -> bool:
    """True for a clearly red pixel: strong red channel, weak green and blue."""
    red, green, blue = pixel[:3]
    return red > 130 and green < 110 and blue < 110


def pixels(image: Image.Image):
    """Every pixel as an ``(r, g, b)`` tuple, without the deprecated ``getdata``."""
    data = image.convert("RGB").tobytes()
    return (data[index : index + 3] for index in range(0, len(data), 3))


def red_pixel_count(image: Image.Image) -> int:
    return sum(1 for pixel in pixels(image) if is_red(pixel))


def ink_pixel_count(image: Image.Image) -> int:
    """How many pixels are not page-white — a stand-in for "how much is written here"."""
    return sum(1 for pixel in pixels(image) if pixel != b"\xff\xff\xff")


def text_of(lines) -> str:
    """Page 2's wording as one string, for substring assertions."""
    return "\n".join(line.text for line in lines)


# --- page 1 -----------------------------------------------------------------


def box_edges(bbox: list[float], size=PAGE_SIZE) -> dict[str, tuple[int, int]]:
    """A point on the middle of each edge of the rectangle drawn around ``bbox``."""
    pad = size[0] * BOX_PAD_RATIO
    x0, y0 = bbox[0] * size[0] - pad, bbox[1] * size[1] - pad
    x1, y1 = bbox[2] * size[0] + pad, bbox[3] * size[1] + pad
    mid_x, mid_y = round((x0 + x1) / 2), round((y0 + y1) / 2)
    return {
        "left": (round(x0), mid_y),
        "right": (round(x1), mid_y),
        "top": (mid_x, round(y0)),
        "bottom": (mid_x, round(y1)),
    }


def test_the_rectangle_is_drawn_on_all_four_edges_of_the_bbox():
    bbox = [0.40, 0.50, 0.60, 0.70]

    page1, _ = render_redline(blank_page_png(), [make_finding("GDT-001", bbox)])

    for name, point in box_edges(bbox).items():
        assert is_red(page1.getpixel(point)), f"no red line on the {name} edge"


def test_the_rectangle_encloses_the_bbox_rather_than_covering_it():
    """The callout inside the box has to stay readable, so the middle is left alone."""
    bbox = [0.40, 0.50, 0.60, 0.70]

    page1, _ = render_redline(blank_page_png(), [make_finding("GDT-001", bbox)])

    center = (round(0.50 * PAGE_SIZE[0]), round(0.60 * PAGE_SIZE[1]))
    assert page1.getpixel(center) == (255, 255, 255), "the mark covered the bbox interior"


def test_the_rectangle_follows_the_size_of_the_bbox():
    small = render_redline(blank_page_png(), [make_finding("A", [0.45, 0.45, 0.55, 0.55])])[0]
    large = render_redline(blank_page_png(), [make_finding("A", [0.20, 0.20, 0.80, 0.80])])[0]

    assert red_pixel_count(large) > red_pixel_count(small)


def balloon_center(bbox: list[float], size=PAGE_SIZE) -> tuple[int, int]:
    """Where the balloon sits by default: diagonally off the rectangle's top-right."""
    pad = size[0] * BOX_PAD_RATIO
    radius = size[0] * BALLOON_WIDTH_RATIO / 2
    x1, y0 = bbox[2] * size[0] + pad, bbox[1] * size[1] - pad
    return (round(x1 + radius * 0.9), round(y0 - radius * 0.9))


def test_a_balloon_is_drawn_outside_the_rectangles_top_right_corner():
    bbox = [0.40, 0.50, 0.60, 0.70]

    page1, _ = render_redline(blank_page_png(), [make_finding("GDT-001", bbox)])

    center = balloon_center(bbox)
    radius = PAGE_SIZE[0] * BALLOON_WIDTH_RATIO / 2
    # Filled, so a ring of samples inside it is solid red; the number is white, so the
    # samples are taken off-centre.
    for offset in (-radius * 0.6, radius * 0.6):
        assert is_red(page1.getpixel((center[0], round(center[1] + offset))))


def test_the_balloon_carries_a_white_number():
    bbox = [0.40, 0.50, 0.60, 0.70]

    page1, _ = render_redline(blank_page_png(), [make_finding("GDT-001", bbox)])

    center = balloon_center(bbox)
    radius = round(PAGE_SIZE[0] * BALLOON_WIDTH_RATIO / 2)
    disc = page1.crop(
        (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius)
    )
    assert any(pixel == b"\xff\xff\xff" for pixel in pixels(disc)), "no white digit"


def test_a_balloon_near_the_top_right_corner_stays_on_the_page():
    """Top-right is the convention, but it loses to staying on the sheet."""
    page1, _ = render_redline(blank_page_png(), [make_finding("GDT-001", [0.90, 0.02, 0.99, 0.08])])

    radius = round(PAGE_SIZE[0] * BALLOON_WIDTH_RATIO / 2)
    # Every red pixel must be inside the page, and a balloon's worth of them must exist
    # somewhere other than the default corner, which is off the sheet here.
    assert red_pixel_count(page1) > 0
    strip = page1.crop((PAGE_SIZE[0] - 2, 0, PAGE_SIZE[0], PAGE_SIZE[1]))
    assert red_pixel_count(strip) < 2 * radius, "the mark ran off the right edge"


def test_a_marker_is_drawn_for_every_finding_with_a_bbox():
    one = render_redline(blank_page_png(), [make_finding("GDT-001", [0.1, 0.1, 0.2, 0.2])])[0]
    two = render_redline(
        blank_page_png(),
        [
            make_finding("GDT-001", [0.1, 0.1, 0.2, 0.2]),
            make_finding("GDT-002", [0.7, 0.7, 0.8, 0.8], item_ids=["F2"]),
        ],
    )[0]

    assert red_pixel_count(two) > red_pixel_count(one)


def test_a_finding_without_a_bbox_leaves_the_drawing_unmarked():
    marked = render_redline(blank_page_png(), [make_finding("GDT-001", [0.4, 0.4, 0.6, 0.6])])[0]
    unmarked = render_redline(blank_page_png(), [make_finding("GDT-001", None)])[0]

    # Both pages carry the stamp; only the first carries a marker.
    assert red_pixel_count(unmarked) < red_pixel_count(marked)


def test_the_stamp_counts_every_finding_including_unlocated_ones(tmp_path):
    findings = [make_finding("GDT-001", None), make_finding("GDT-002", None, item_ids=["F2"])]

    page1, _ = render_redline(blank_page_png(), findings, model_name="m", when=date(2026, 1, 2))

    # The stamp sits in the top-left; the body of the page has nothing else on it.
    assert red_pixel_count(page1.crop((0, 0, 500, 120))) > 0


# --- numbering --------------------------------------------------------------


def test_numbering_follows_run_rules_order(plate_drawing: Drawing):
    findings = run_rules(plate_drawing)
    assert len(findings) >= 2, "this test needs a drawing with more than one finding"

    entries = [
        line.text
        for line in findings_text(findings)
        if line.text[:1].isdigit() and ". " in line.text
    ]

    # The list on page 2 is numbered 1..N in exactly the order run_rules returned, which
    # is the order findings.json uses, so a marker number names the same finding anywhere.
    assert entries == [
        f"{number}. {finding.rule_id}  [{finding.severity}]"
        for number, finding in enumerate(findings, start=1)
    ]


def test_each_located_finding_is_marked_at_its_own_bbox(plate_drawing: Drawing):
    findings = run_rules(plate_drawing)
    page1, _ = render_redline(blank_page_png(), findings)

    located = [finding for finding in findings if finding.bbox]
    assert located, "this test needs at least one finding with a bbox"
    for finding in located:
        for name, point in box_edges(finding.bbox).items():
            assert is_red(page1.getpixel(point)), f"{finding.rule_id}: no {name} edge"


def test_markers_are_numbered_differently_for_each_finding():
    """Two findings at the same spot must not paint the same glyph."""
    first = render_redline(blank_page_png(), [make_finding("A-001", [0.4, 0.4, 0.6, 0.6])])[0]
    second = render_redline(
        blank_page_png(),
        [
            make_finding("A-001", None),
            make_finding("A-002", [0.4, 0.4, 0.6, 0.6], item_ids=["F2"]),
        ],
    )[0]

    assert first.tobytes() != second.tobytes(), "'1' and '2' rendered identically"


# --- page 2 -----------------------------------------------------------------


def test_page_two_is_a_clean_white_sheet_the_size_of_page_one():
    page1, page2 = render_redline(blank_page_png(), [make_finding("GDT-001", None)])

    assert page2.size == page1.size
    assert page2.getpixel((page2.width - 5, 5)) == (255, 255, 255)


def test_page_two_is_written_even_with_no_findings():
    _, page2 = render_redline(blank_page_png(), [])

    # The heading and the footer are always there, so the page is never blank.
    assert ink_pixel_count(page2) > 0


def test_more_findings_make_page_two_longer():
    one = render_redline(blank_page_png(), [make_finding("GDT-001", None)])[1]
    three = render_redline(
        blank_page_png(),
        [
            make_finding("GDT-001", None),
            make_finding("GDT-002", None, item_ids=["F2"]),
            make_finding("GDT-003", None, item_ids=["F3"]),
        ],
    )[1]

    assert ink_pixel_count(three) > ink_pixel_count(one)


def test_a_finding_without_a_bbox_is_listed_as_location_not_marked(plate_drawing: Drawing):
    findings = run_rules(plate_drawing)
    unlocated = [finding for finding in findings if finding.bbox is None]
    assert unlocated, "this test needs a finding the rules could not place"

    text = text_of(findings_text(findings))

    assert "location not marked" in text
    assert text.count("location not marked") == len(unlocated)
    for finding in unlocated:
        assert finding.rule_id in text


def test_page_two_lists_every_findings_rule_message_and_severity(plate_drawing: Drawing):
    findings = run_rules(plate_drawing)

    text = text_of(findings_text(findings))

    for finding in findings:
        assert finding.rule_id in text
        assert finding.message in text
        assert f"[{finding.severity}]" in text


def test_a_report_adds_the_edit_its_rationale_and_the_verified_verdict(
    plate_drawing: Drawing, scripted_client
):
    findings = run_rules(plate_drawing)
    report = correct(plate_drawing, blank_page_png(), scripted_client)
    assert report.resolved_count >= 1, "the fixture should clear at least one finding"

    text = text_of(findings_text(findings, report))

    assert "fix: Remove datum C from the position frame" in text
    assert "Nothing on the sheet defines datum C." in text
    assert "verified resolved" in text
    # DIM-001 (units missing) is not addressed by the scripted edit, so it stays open.
    assert "unresolved" in text


def fix_lines(findings, report) -> list[str]:
    """Just the ``fix:`` lines from page 2."""
    return [
        line.text for line in findings_text(findings, report) if line.text.startswith("fix: ")
    ]


def test_edits_are_written_in_plain_english_not_operation_names(
    plate_drawing: Drawing, scripted_client
):
    report = correct(plate_drawing, blank_page_png(), scripted_client)

    fixes = fix_lines(run_rules(plate_drawing), report)

    assert "fix: Remove datum C from the position frame" in fixes
    for line in fixes:
        assert "remove_datum_ref" not in line, "an operation name reached the sheet"
        assert "F1" not in line, "an item id reached the sheet"


def test_page_two_does_not_list_item_ids(plate_drawing: Drawing):
    """The ids belong in the JSON files, not on the sheet an engineer reads."""
    text = text_of(findings_text(run_rules(plate_drawing)))

    assert "items:" not in text


def test_the_ids_are_still_in_the_findings_themselves(plate_drawing: Drawing):
    """Dropping them from page 2 must not mean dropping them from the data."""
    findings = run_rules(plate_drawing)

    assert any(finding.item_ids for finding in findings)
    assert any("F1" in finding.item_ids for finding in findings)


def test_a_finding_the_rules_could_not_place_still_says_so(plate_drawing: Drawing):
    findings = run_rules(plate_drawing)
    unlocated = [finding for finding in findings if finding.bbox is None]
    assert unlocated, "this test needs a finding the rules could not place"

    lines = [line.text for line in findings_text(findings)]

    assert lines.count("location not marked") == len(unlocated)


def names_for(drawing: Drawing) -> EditNames:
    """A name resolver over one drawing, without running the loop to get a report."""
    return EditNames(
        CorrectionReport(
            original_drawing=drawing,
            final_drawing=drawing,
            original_findings=[],
            final_findings=[],
        )
    )


def test_plain_edit_names_a_frame_by_its_characteristic(plate_drawing: Drawing):
    edit = RemoveDatumRef(op="remove_datum_ref", fcf_id="F1", label="c")

    assert plain_edit(edit, names_for(plate_drawing)) == (
        "Remove datum C from the position frame"
    )


def test_plain_edit_names_a_dimension_by_what_is_printed_on_it(plate_drawing: Drawing):
    edit = SetField(op="set_field", target="dimension", id="D1", field="tol_plus", value=0.2)

    assert plain_edit(edit, names_for(plate_drawing)) == (
        "Set the upper tolerance of the 50 dimension to 0.2"
    )


def test_plain_edit_says_the_drawings_own_fields_without_an_item(plate_drawing: Drawing):
    edit = SetField(op="set_field", target="drawing", field="units", value="mm")

    assert plain_edit(edit, names_for(plate_drawing)) == "Set the units to mm"


def test_plain_edit_reads_a_general_tolerance_as_plus_minus(plate_drawing: Drawing):
    edit = SetField(op="set_field", target="drawing", field="general_tolerance", value=0.1)

    assert plain_edit(edit, names_for(plate_drawing)) == (
        "Set the general tolerance to ±0.1"
    )


def test_plain_edit_reads_the_basic_flag_as_a_statement(plate_drawing: Drawing):
    edit = SetField(op="set_field", target="dimension", id="D1", field="is_basic", value=True)

    assert plain_edit(edit, names_for(plate_drawing)) == "Mark the 50 dimension as basic"


def test_plain_edit_says_a_cleared_field_is_removed(plate_drawing: Drawing):
    edit = SetField(op="set_field", target="dimension", id="D1", field="tol_minus", value=None)

    assert plain_edit(edit, names_for(plate_drawing)) == (
        "Remove the lower tolerance from the 50 dimension"
    )


def test_plain_edit_describes_an_added_datum(plate_drawing: Drawing):
    edit = AddDatum(op="add_datum", label="c", location="the right edge of the plate")

    assert plain_edit(edit, names_for(plate_drawing)) == (
        "Add datum C at the right edge of the plate"
    )


def test_plain_edit_describes_an_added_dimension_in_words(plate_drawing: Drawing):
    edit = AddDimension(
        op="add_dimension",
        dimension={
            "value": 30,
            "unit": "mm",
            "is_basic": True,
            "from_ref": "left_edge",
            "to_ref": "hole_1_center",
        },
    )

    assert plain_edit(edit, names_for(plate_drawing)) == (
        "Add a basic 30 mm dimension from the left edge to the hole 1 center"
    )


def test_an_unknown_id_falls_back_to_naming_it(plate_drawing: Drawing):
    """A name we cannot resolve is still better than nothing on the page."""
    edit = RemoveDatumRef(op="remove_datum_ref", fcf_id="F99", label="A")

    assert "F99" in plain_edit(edit, names_for(plate_drawing))


def test_without_a_report_page_two_says_nothing_about_status(plate_drawing: Drawing):
    text = text_of(findings_text(run_rules(plate_drawing)))

    assert "status:" not in text
    assert "verified resolved" not in text


def test_render_works_without_a_report(plate_drawing: Drawing):
    pages = render_redline(blank_page_png(), run_rules(plate_drawing))

    assert len(pages) == 2
    assert all(page.size == PAGE_SIZE for page in pages)


def test_render_works_with_a_report(plate_drawing: Drawing, scripted_client):
    report = correct(plate_drawing, blank_page_png(), scripted_client)

    pages = render_redline(blank_page_png(), run_rules(plate_drawing), report)

    assert len(pages) == 2
    assert all(page.size == PAGE_SIZE for page in pages)


def test_a_long_list_says_how_many_findings_it_could_not_show():
    many = [
        make_finding(f"X-{index:03d}", None, item_ids=[f"F{index}"], message="m " * 40)
        for index in range(60)
    ]

    _, page2 = render_redline(blank_page_png(), many)

    # Not readable as text, but the page must still end inside its own footer area.
    assert page2.height == PAGE_SIZE[1]


# --- saving -----------------------------------------------------------------


def test_save_redline_writes_a_two_page_pdf_and_a_png(tmp_path):
    pages = render_redline(blank_page_png(), [make_finding("GDT-001", [0.4, 0.4, 0.6, 0.6])])
    pdf_path = tmp_path / "out" / "redline.pdf"
    png_path = tmp_path / "out" / "redline.png"

    save_redline(pages, pdf_path, png_path)

    pdf = pdfium.PdfDocument(pdf_path)
    try:
        assert len(pdf) == 2
    finally:
        pdf.close()

    with Image.open(png_path) as png:
        assert png.size == PAGE_SIZE


def test_the_saved_pdf_still_carries_the_marker(tmp_path):
    """Round-trip through the PDF: the mark survives to what the engineer opens."""
    bbox = [0.4, 0.5, 0.6, 0.7]
    pages = render_redline(blank_page_png(), [make_finding("GDT-001", bbox)])
    pdf_path = tmp_path / "redline.pdf"
    save_redline(pages, pdf_path, tmp_path / "redline.png")

    pdf = pdfium.PdfDocument(pdf_path)
    try:
        # Render back at the pixel size that went in; at the PDF's own 72 dpi the page
        # comes back a third of the size and the thin lines blur into pink.
        page = pdf[0]
        rendered = page.render(scale=PAGE_SIZE[0] / page.get_width()).to_pil().convert("RGB")
    finally:
        pdf.close()

    for name, point in box_edges(bbox, rendered.size).items():
        assert is_red(rendered.getpixel(point)), f"the {name} edge did not survive the PDF"


# --- ASCII safety -----------------------------------------------------------


def test_ascii_safe_replaces_characters_the_font_cannot_draw():
    assert ascii_safe("a — b") == "a - b"
    assert ascii_safe("“quoted”") == '"quoted"'
    assert ascii_safe("wait…") == "wait..."


def test_ascii_safe_keeps_engineering_symbols():
    # Latin-1, and all meaningful on a drawing: the font has them, so they stay.
    assert ascii_safe("Ø8 ±0.1 2×45°") == "Ø8 ±0.1 2×45°"


def test_page_two_wording_is_drawable(plate_drawing: Drawing):
    """Nothing page 2 says should need a glyph the font lacks."""
    text = text_of(findings_text(run_rules(plate_drawing)))

    assert ascii_safe(text) == text
    assert "?" not in ascii_safe(text), "a character was dropped rather than substituted"


def test_the_footer_text_is_the_reviewed_by_an_engineer_line():
    assert "require engineer review" in FOOTER


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def plate_drawing() -> Drawing:
    """A small drawing with a located finding and an unlocated one.

    GDT-001 fires on the frame, which has a bbox, so it is marked on page 1. DIM-001 fires
    on the drawing as a whole, which has none, so it must be listed as "location not
    marked" instead.
    """
    return parse_drawing(
        {
            "units": None,
            "datums": [{"label": "A", "bbox": [0.05, 0.05, 0.1, 0.1]}],
            "dimensions": [{"id": "D1", "value": 50.0, "bbox": [0.3, 0.3, 0.35, 0.35]}],
            "fcfs": [
                {
                    "id": "F1",
                    "characteristic": "position",
                    "tolerance": 0.25,
                    "datum_refs": [{"label": "A"}, {"label": "C"}],
                    "bbox": [0.60, 0.40, 0.70, 0.45],
                }
            ],
        }
    )


class _ScriptedClient:
    """Returns one edit that really clears GDT-001, so a report has something in it."""

    def chat_json(self, system, user, images):
        from draftcheck.llm_client import JSONChatResult, Usage

        edits = [
            {
                "op": "remove_datum_ref",
                "fcf_id": "F1",
                "label": "C",
                "rationale": "Nothing on the sheet defines datum C.",
                "finding_ids": ["GDT-001@F1"],
            }
        ]
        return JSONChatResult(
            data={"edits": edits},
            text="{}",
            latency_s=0.0,
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="scripted",
            attempts=1,
        )


@pytest.fixture
def scripted_client() -> _ScriptedClient:
    return _ScriptedClient()
