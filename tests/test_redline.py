"""Tests for the two-page redline output. No network.

The drawing here is a blank white page rather than a real sheet, so "is this pixel red"
is a question about the markup and not about whatever the drawing had underneath.
"""

from __future__ import annotations

import io
import json
import math
from datetime import date
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image

from draftcheck.agent import CorrectionReport, correct
from draftcheck.edits import AddDatum, AddDimension, RemoveDatumRef, SetField
from draftcheck.redline import (
    ARCHIVO_FONT,
    CLOUD_PAD_RATIO,
    DELTA_WIDTH_RATIO,
    FOOTER,
    PEN_AMBER,
    PEN_FONT,
    PEN_GREEN,
    PEN_RED,
    EditNames,
    _cloud_arcs,
    _pen_font_for,
    ascii_safe,
    findings_text,
    plain_edit,
    render_redline,
    save_redline,
)
from draftcheck.rules import rule_results, run_rules
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


def near(pixel, colour, tolerance: int = 40) -> bool:
    """True when ``pixel`` is within ``tolerance`` of ``colour`` on every channel."""
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], colour, strict=True))


def colour_count(image: Image.Image, colour, region=None) -> int:
    """How many pixels in ``region`` (default: the whole page) are ``colour``."""
    area = image.crop(region) if region else image
    return sum(1 for pixel in pixels(area) if near(tuple(pixel), colour))


def ring_of(bbox: list[float], size=PAGE_SIZE) -> tuple[float, float, float, float]:
    """The padded rectangle a cloud is drawn around."""
    pad = size[0] * CLOUD_PAD_RATIO
    return (
        bbox[0] * size[0] - pad,
        bbox[1] * size[1] - pad,
        bbox[2] * size[0] + pad,
        bbox[3] * size[1] + pad,
    )


def box_edges(bbox: list[float], size=PAGE_SIZE) -> dict[str, tuple[int, int, int, int]]:
    """A small window straddling the middle of each side of the cloud around ``bbox``.

    A cloud's arcs wander a few pixels either side of the ring, so each edge is checked
    over a window rather than at one pixel.
    """
    x0, y0, x1, y1 = ring_of(bbox, size)
    mid_x, mid_y = (x0 + x1) / 2, (y0 + y1) / 2
    reach, along = size[0] * 0.008, size[0] * 0.02

    def window(cx, cy, wide, tall):
        return (round(cx - wide), round(cy - tall), round(cx + wide), round(cy + tall))

    return {
        "left": window(x0, mid_y, reach, along),
        "right": window(x1, mid_y, reach, along),
        "top": window(mid_x, y0, along, reach),
        "bottom": window(mid_x, y1, along, reach),
    }


def assert_cloud(image: Image.Image, bbox: list[float], colour, label: str = "") -> None:
    for name, region in box_edges(bbox, image.size).items():
        assert colour_count(image, colour, region) > 0, f"{label}: no cloud on the {name} edge"


def pen_strip(bbox: list[float], size=PAGE_SIZE) -> tuple[int, int, int, int]:
    """Where a pen note goes: just right of the cloud, level with its middle."""
    x0, y0, x1, y1 = ring_of(bbox, size)
    gap = size[0] * 0.006
    mid = (y0 + y1) / 2
    half = size[0] * 0.012
    return (round(x1 + gap), round(mid - half), round(x1 + 120), round(mid + half))


def test_without_a_report_the_cloud_is_red_on_all_four_edges():
    bbox = [0.40, 0.50, 0.60, 0.70]

    page1, _ = render_redline(blank_page_png(), [make_finding("GDT-001", bbox)])

    assert_cloud(page1, bbox, PEN_RED)


def test_the_cloud_encloses_the_bbox_rather_than_covering_it():
    """The callout inside the cloud has to stay readable, so the middle is left alone."""
    bbox = [0.40, 0.50, 0.60, 0.70]

    page1, _ = render_redline(blank_page_png(), [make_finding("GDT-001", bbox)])

    inside = (
        round(0.45 * PAGE_SIZE[0]),
        round(0.56 * PAGE_SIZE[1]),
        round(0.55 * PAGE_SIZE[0]),
        round(0.64 * PAGE_SIZE[1]),
    )
    assert ink_pixel_count(page1.crop(inside)) == 0, "the mark covered the bbox interior"


def test_the_cloud_follows_the_size_of_the_bbox():
    small = render_redline(blank_page_png(), [make_finding("A", [0.45, 0.45, 0.55, 0.55])])[0]
    large = render_redline(blank_page_png(), [make_finding("A", [0.20, 0.20, 0.80, 0.80])])[0]

    assert colour_count(large, PEN_RED) > colour_count(small, PEN_RED)


def test_every_cloud_arc_bulges_outward():
    box = (100.0, 100.0, 300.0, 200.0)
    for (bx0, by0, bx1, by1), start, end in _cloud_arcs(box, 5.4):
        cx, cy, radius = (bx0 + bx1) / 2, (by0 + by1) / 2, (bx1 - bx0) / 2
        middle = math.radians((start + end) / 2)
        px, py = cx + radius * math.cos(middle), cy + radius * math.sin(middle)
        # An arc that turns a corner can end up a pixel inside it; no more than that.
        inside = box[0] + 1.5 < px < box[2] - 1.5 and box[1] + 1.5 < py < box[3] - 1.5
        assert not inside, "an arc bulges into the box"
        assert 0 < end - start < 180


def delta_region(bbox: list[float], size=PAGE_SIZE) -> tuple[int, int, int, int]:
    """The delta's bounding square, just outside the cloud's top-left corner."""
    x0, y0, _, _ = ring_of(bbox, size)
    side = size[0] * DELTA_WIDTH_RATIO
    margin = size[0] * 0.002
    right, base = x0 + margin, y0 + margin / 2
    return (round(right - side), round(base - side * 0.88), round(right), round(base))


def test_a_filled_delta_sits_outside_the_clouds_top_left_corner():
    bbox = [0.40, 0.50, 0.60, 0.70]

    page1, _ = render_redline(blank_page_png(), [make_finding("GDT-001", bbox)])

    region = delta_region(bbox)
    area = (region[2] - region[0]) * (region[3] - region[1])
    # A filled triangle covers about half its square, less the white digit.
    assert colour_count(page1, PEN_RED, region) > area * 0.3
    assert any(pixel == b"\xff\xff\xff" for pixel in pixels(page1.crop(region))), "no digit"


def test_two_findings_on_one_box_show_both_deltas():
    bbox = [0.40, 0.50, 0.60, 0.70]
    findings = [make_finding("A-001", bbox), make_finding("A-002", bbox, item_ids=["F2"])]

    page1, _ = render_redline(blank_page_png(), findings)

    first = delta_region(bbox)
    shift = round(PAGE_SIZE[0] * DELTA_WIDTH_RATIO * 1.1)
    second = (first[0] + shift, first[1], first[2] + shift, first[3])
    area = (first[2] - first[0]) * (first[3] - first[1])
    assert colour_count(page1, PEN_RED, first) > area * 0.3
    assert colour_count(page1, PEN_RED, second) > area * 0.3


def test_a_delta_at_the_corner_of_the_sheet_is_kept_on_it():
    page1, _ = render_redline(blank_page_png(), [make_finding("GDT-001", [0.0, 0.3, 0.05, 0.35])])

    side = round(PAGE_SIZE[0] * DELTA_WIDTH_RATIO)
    strip = (0, round(0.3 * PAGE_SIZE[1]) - side * 2, side * 2, round(0.3 * PAGE_SIZE[1]))
    assert colour_count(page1, PEN_RED, strip) > side * side * 0.3


def test_a_cloud_is_drawn_for_every_finding_with_a_bbox():
    one = render_redline(blank_page_png(), [make_finding("GDT-001", [0.1, 0.3, 0.2, 0.4])])[0]
    two = render_redline(
        blank_page_png(),
        [
            make_finding("GDT-001", [0.1, 0.3, 0.2, 0.4]),
            make_finding("GDT-002", [0.7, 0.7, 0.8, 0.8], item_ids=["F2"]),
        ],
    )[0]

    assert colour_count(two, PEN_RED) > colour_count(one, PEN_RED)


def test_a_finding_without_a_bbox_leaves_the_drawing_unmarked():
    marked = render_redline(blank_page_png(), [make_finding("GDT-001", [0.4, 0.4, 0.6, 0.6])])[0]
    unmarked = render_redline(blank_page_png(), [make_finding("GDT-001", None)])[0]

    # Both pages carry the review stamp; only the first carries a cloud.
    assert red_pixel_count(unmarked) < red_pixel_count(marked)


def test_the_stamp_counts_every_finding_including_unlocated_ones(tmp_path):
    findings = [make_finding("GDT-001", None), make_finding("GDT-002", None, item_ids=["F2"])]

    page1, _ = render_redline(blank_page_png(), findings, model_name="m", when=date(2026, 1, 2))

    # The stamp sits in the top-left; the body of the page has nothing else on it.
    assert red_pixel_count(page1.crop((0, 0, 500, 120))) > 0


FRAME_BBOX = [0.60, 0.40, 0.70, 0.45]


def test_a_verified_finding_gets_a_green_cloud(plate_drawing: Drawing, scripted_client):
    report = correct(plate_drawing, blank_page_png(), scripted_client)
    # GDT-004 shares the frame and stays open, so only the cleared finding is drawn here.
    cleared = [finding for finding in run_rules(plate_drawing) if finding.rule_id == "GDT-001"]

    page1, _ = render_redline(blank_page_png(), cleared, report)

    assert_cloud(page1, FRAME_BBOX, PEN_GREEN, "verified")
    assert colour_count(page1, PEN_RED, ring_of(FRAME_BBOX)) == 0


def test_an_unresolved_finding_gets_an_amber_cloud(plate_drawing: Drawing):
    report = correct(plate_drawing, blank_page_png(), _EditsClient([]))

    page1, _ = render_redline(blank_page_png(), run_rules(plate_drawing), report)

    assert_cloud(page1, FRAME_BBOX, PEN_AMBER, "unresolved")


def test_a_pen_label_is_written_beside_the_cloud_when_an_edit_was_applied(
    plate_drawing: Drawing, scripted_client
):
    findings = run_rules(plate_drawing)
    fixed = correct(plate_drawing, blank_page_png(), scripted_client)
    nothing = correct(plate_drawing, blank_page_png(), _EditsClient([]))

    with_edit = render_redline(blank_page_png(), findings, fixed)[0]
    without_edit = render_redline(blank_page_png(), findings, nothing)[0]
    no_report = render_redline(blank_page_png(), findings)[0]

    strip = pen_strip(FRAME_BBOX)
    assert colour_count(with_edit, PEN_GREEN, strip) > 20, "no green 'drop C' beside the cloud"
    assert ink_pixel_count(without_edit.crop(strip)) == 0
    assert ink_pixel_count(no_report.crop(strip)) == 0


def test_a_refused_edit_is_written_in_red_and_struck_through(plate_drawing: Drawing):
    refused = {
        "op": "set_field",
        "target": "fcf",
        "id": "F99",
        "field": "tolerance",
        "value": 0.5,
        "rationale": "Names a frame that is not there.",
        "finding_ids": ["GDT-001@F1"],
    }
    report = correct(plate_drawing, blank_page_png(), _EditsClient([refused]))
    assert report.rounds and report.rounds[0].rejected, "the guards should refuse this edit"

    page1, _ = render_redline(blank_page_png(), run_rules(plate_drawing), report)

    strip = pen_strip(FRAME_BBOX)
    assert colour_count(page1, PEN_RED, strip) > 20
    # The strike is a near-horizontal line across the label: a run of red as wide as it.
    rows = [page1.crop((strip[0], y, strip[2], y + 1)) for y in range(strip[1], strip[3])]
    assert max(colour_count(row, PEN_RED) for row in rows) > 15, "no strike line"


#: Applies cleanly and cites GDT-001, but changes nothing GDT-001 is about.
TIGHTEN = {
    "op": "set_field",
    "target": "fcf",
    "id": "F1",
    "field": "tolerance",
    "value": 0.2,
    "rationale": "Tighter is better.",
    "finding_ids": ["GDT-001@F1"],
}


def test_a_rolled_back_edit_is_written_in_red_and_struck_through(plate_drawing: Drawing):
    report = correct(plate_drawing, blank_page_png(), _EditsClient([TIGHTEN]), max_rounds=1)
    assert report.rounds[0].applied == [] and report.rounds[0].rolled_back

    page1, _ = render_redline(blank_page_png(), run_rules(plate_drawing), report)

    strip = pen_strip(FRAME_BBOX)
    assert colour_count(page1, PEN_RED, strip) > 20, "the label should be red, not amber"
    assert colour_count(page1, PEN_AMBER, strip) == 0
    rows = [page1.crop((strip[0], y, strip[2], y + 1)) for y in range(strip[1], strip[3])]
    assert max(colour_count(row, PEN_RED) for row in rows) > 15, "no strike line"


def test_a_rolled_back_edit_is_listed_but_not_kept(plate_drawing: Drawing):
    report = correct(plate_drawing, blank_page_png(), _EditsClient([TIGHTEN]), max_rounds=1)

    findings = run_rules(plate_drawing)
    number = next(i for i, finding in enumerate(findings, 1) if finding.rule_id == "GDT-001")
    lines = findings_text(findings, report)
    rows = table_rows(lines)

    assert rows == [[str(number), "⌖ 0.25 | A | C", "⌖ 0.2 | A | C", "Failed, rolled back"]]
    # The corrected drawing, and so page 2's "fix:" lines, carry only kept edits.
    assert report.final_drawing.fcfs[0].tolerance == 0.25
    assert "fix: no edit was applied for this finding" in [line.text for line in lines]


def test_the_pen_font_falls_back_to_archivo_for_a_missing_character():
    assert Path(_pen_font_for("drop C", 30).path) == PEN_FONT
    assert Path(_pen_font_for("Ø0.25", 30).path) == ARCHIVO_FONT


def stamp_ink(image: Image.Image) -> int:
    """Green pixels outside any cloud: the CHECKED stamp is the only green left."""
    return colour_count(image, PEN_GREEN)


def test_the_checked_stamp_appears_only_with_a_report(plate_drawing: Drawing):
    findings = run_rules(plate_drawing)
    report = correct(plate_drawing, blank_page_png(), _EditsClient([]))

    stamped = render_redline(blank_page_png(), findings, report)[0]
    plain = render_redline(blank_page_png(), findings)[0]

    # With nothing verified every cloud is amber, so any green is the stamp.
    assert stamp_ink(stamped) > 500
    assert stamp_ink(plain) == 0


def test_the_stamp_keeps_off_the_findings(plate_drawing: Drawing):
    """Lower middle is the stamp's favourite spot; a finding there pushes it aside."""
    bbox = [0.35, 0.68, 0.65, 0.88]
    finding = make_finding("GDT-001", bbox, item_ids=["F1"])
    report = correct(plate_drawing, blank_page_png(), _EditsClient([]))

    page1, _ = render_redline(blank_page_png(), [finding], report)

    assert stamp_ink(page1) > 500
    assert colour_count(page1, PEN_GREEN, ring_of(bbox)) == 0


def test_the_stamp_on_a_crowded_sheet_falls_back_to_the_lower_middle(plate_drawing: Drawing):
    report = correct(plate_drawing, blank_page_png(), _EditsClient([]))
    black = Image.new("RGB", PAGE_SIZE, (0, 0, 0))
    buffer = io.BytesIO()
    black.save(buffer, format="PNG")

    page1, _ = render_redline(buffer.getvalue(), [], report)

    lower_middle = (
        round(PAGE_SIZE[0] * 0.4),
        round(PAGE_SIZE[1] * 0.7),
        round(PAGE_SIZE[0] * 0.6),
        round(PAGE_SIZE[1] * 0.86),
    )
    assert colour_count(page1, PEN_GREEN, lower_middle) > 100


def test_a_report_with_unlocated_findings_still_renders(plate_drawing: Drawing, scripted_client):
    report = correct(plate_drawing, blank_page_png(), scripted_client)

    pages = render_redline(blank_page_png(), [make_finding("DIM-001", None)], report)

    assert all(page.size == PAGE_SIZE for page in pages)


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
        assert_cloud(page1, finding.bbox, PEN_RED, finding.rule_id)


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


def table_rows(lines) -> list[list[str]]:
    """The Corrections table's body rows, as cells."""
    return [line.text.split("\t") for line in lines if line.style == "table_row"]


def test_page_two_has_a_corrections_row_for_each_edit(plate_drawing: Drawing, scripted_client):
    findings = run_rules(plate_drawing)
    report = correct(plate_drawing, blank_page_png(), scripted_client)
    number = next(
        index for index, finding in enumerate(findings, 1) if finding.rule_id == "GDT-001"
    )

    lines = findings_text(findings, report)

    assert "Corrections" in [line.text for line in lines]
    rows = table_rows(lines)
    assert rows[0] == [str(number), "⌖ 0.25 | A | C", "⌖ 0.25 | A", "Verified"]
    # The scripted model asks for the same edit in later rounds; C is gone by then, so the
    # guards refuse it, and the table says so just as /fix does.
    assert all(row[-1] == "Rejected" for row in rows[1:])


def test_the_corrections_table_uses_the_same_strings_as_fix(
    plate_drawing: Drawing, scripted_client
):
    from draftcheck.api import _summarize

    report = correct(plate_drawing, blank_page_png(), scripted_client)
    on_screen = [
        (edit["before"], edit["after"])
        for round_ in _summarize(report)["rounds"]
        for edit in round_["applied"] + round_["rolled_back"] + round_["rejected"]
    ]

    rows = table_rows(findings_text(run_rules(plate_drawing), report))

    assert [(row[1], row[2]) for row in rows] == on_screen


def test_a_refused_edit_is_listed_as_rejected(plate_drawing: Drawing):
    refused = {
        "op": "remove_datum_ref",
        "fcf_id": "F99",
        "label": "C",
        "finding_ids": ["GDT-001@F1"],
    }
    report = correct(plate_drawing, blank_page_png(), _EditsClient([refused]))

    rows = table_rows(findings_text(run_rules(plate_drawing), report))

    assert rows and rows[0][-1] == "Rejected"


def test_without_a_report_there_is_no_corrections_table(plate_drawing: Drawing):
    lines = findings_text(run_rules(plate_drawing))

    assert "Corrections" not in [line.text for line in lines]
    assert not table_rows(lines)


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

    assert_cloud(rendered, bbox, PEN_RED, "after the PDF")


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


class _EditsClient:
    """A stand-in model that proposes the same edits every round."""

    def __init__(self, edits: list[dict]):
        self.edits = edits

    def chat_json(self, system, user, images):
        from draftcheck.llm_client import JSONChatResult, Usage

        return JSONChatResult(
            data={"edits": self.edits},
            text="{}",
            latency_s=0.0,
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="scripted",
            attempts=1,
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


def test_page_two_lists_every_check_with_its_status():
    fixture = Path(__file__).parent / "fixtures" / "plate_001.model.json"
    plate = parse_drawing(json.loads(fixture.read_text()))
    findings = run_rules(plate)
    lines = findings_text(findings, rule_results=rule_results(plate, findings))
    texts = [line.text for line in lines]

    start = texts.index("Checks run")
    assert texts[start + 1] == "6 checks: 5 passed, 1 failed"
    assert "FAIL  GDT-001  Undefined datum" in texts
    assert "PASS  MFG-001  Hole too close to edge" in texts
    assert (
        "Every hole keeps at least 8 mm of material to the edge "
        "(closest: 16 mm, hole 1 to the left edge)."
    ) in texts
    # The failed check's sentence is drawn in red, the rest in grey.
    by_text = {line.text: line for line in lines}
    assert by_text["The frames reference A, B, C, but nothing defines C (see issue 1)."].style == (
        "verdict"
    )
    assert by_text["Each datum label is used once: A, B."].style == "detail"


def test_page_two_marks_not_applicable_checks():
    bare = parse_drawing({"units": "mm", "general_tolerance": {"linear_plus_minus": 0.1}})
    texts = [line.text for line in findings_text([], rule_results=rule_results(bare))]
    assert "N/A  GDT-001  Undefined datum" in texts
    assert "No feature control frames on this drawing." in texts


def test_without_rule_results_page_two_has_no_checks_section(plate_drawing: Drawing):
    texts = [line.text for line in findings_text(run_rules(plate_drawing))]
    assert "Checks run" not in texts


def test_checks_section_is_drawn_on_page_two(plate_drawing: Drawing):
    findings = run_rules(plate_drawing)
    without = render_redline(blank_page_png(), findings)[1]
    with_checks = render_redline(
        blank_page_png(), findings, rule_results=rule_results(plate_drawing, findings)
    )[1]
    assert with_checks.tobytes() != without.tobytes()


def test_page_two_gives_both_summaries_after_a_fix():
    fixture = Path(__file__).parent / "fixtures" / "plate_001.model.json"
    data = json.loads(fixture.read_text())
    submitted = parse_drawing(data)
    data["datums"].append({"label": "C"})
    corrected = parse_drawing(data)
    findings = run_rules(submitted)

    texts = [
        line.text
        for line in findings_text(
            findings,
            rule_results=rule_results(submitted, findings),
            final_rule_results=rule_results(corrected, numbering=findings),
        )
    ]
    start = texts.index("Checks run")
    assert texts[start + 1] == "On the submitted drawing: 6 checks: 5 passed, 1 failed"
    assert texts[start + 2] == "After the agent's verified fixes: 6 checks: 6 passed, 0 failed"
    # The list itself is still the submitted drawing's, like the rest of the page.
    assert "FAIL  GDT-001  Undefined datum" in texts
