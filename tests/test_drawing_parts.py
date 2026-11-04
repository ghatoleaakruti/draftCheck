"""Tests for the part recipes (gear, nut, bolt) in scripts/drawing_suite.py.

The suite-wide checks (PDF renders, ground truth parses, committed files are current, rules
find exactly the seeded errors) already run over these drawings in test_eval_suite.py; this
file covers the recipe geometry and the extra ground-truth fields part sheets carry.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas as rl_canvas

from draftcheck.rules import run_rules
from draftcheck.schema import parse_drawing
from scripts.drawing_suite import (
    GEAR_SPUR,
    HEX_BOLT,
    HEX_NUT,
    SUITE,
    Callout,
    PartSpec,
    PartView,
    PointDim,
    bore_with_keyway,
    build_one,
    centre_cross,
    draw_callout,
    draw_point_dim,
    hex_face_bows,
    hexagon,
    spur_gear_outline,
)
from scripts.make_sample_drawing import PAGE_H, PAGE_W
from scripts.run_eval import load_ground_truth

PARTS = (GEAR_SPUR, HEX_NUT, HEX_BOLT)
PART_STEMS = [spec.stem for spec in PARTS]


def _radius(point: tuple[float, float]) -> float:
    return math.hypot(*point)


# --- Geometry --------------------------------------------------------------------------


def test_gear_outline_stays_between_root_and_tip() -> None:
    points = spur_gear_outline(24, 2.0)
    radii = [_radius(p) for p in points]
    assert min(radii) == pytest.approx(21.5)  # root: pitch 24 - 1.25 module
    assert max(radii) == pytest.approx(26.0)  # tip: pitch 24 + 1 module


def test_gear_outline_has_one_tip_per_tooth() -> None:
    points = spur_gear_outline(24, 2.0)
    angles = [math.atan2(y, x) for x, y in points if _radius((x, y)) == pytest.approx(26.0)]
    # Two tip points per tooth (one per flank), so 24 teeth give 48 tip points.
    assert len(angles) == 48


def test_gear_first_tooth_is_centred_on_the_vertical_axis() -> None:
    points = spur_gear_outline(24, 2.0)
    tip = [p for p in points if _radius(p) == pytest.approx(26.0)][:2]
    assert tip[0][0] == pytest.approx(-tip[1][0])
    assert tip[0][1] > 0


def test_hexagon_across_flats_and_corners() -> None:
    flat_top = hexagon(16.0, pointy_top=False)
    xs = [x for x, _ in flat_top]
    ys = [y for _, y in flat_top]
    assert max(ys) - min(ys) == pytest.approx(16.0)  # across flats, vertical
    assert max(xs) - min(xs) == pytest.approx(16.0 / math.cos(math.radians(30)))

    pointy = hexagon(13.0, pointy_top=True)
    assert max(x for x, _ in pointy) - min(x for x, _ in pointy) == pytest.approx(13.0)


def test_bore_with_keyway_reaches_the_keyway_flat() -> None:
    points = bore_with_keyway(16.0, 5.0, 10.3)
    assert min(y for _, y in points) == pytest.approx(-8.0, abs=1e-6)
    assert max(y for _, y in points) == pytest.approx(10.3)
    assert {x for x, y in points if y == pytest.approx(10.3)} == {-2.5, 2.5}


def test_centre_cross_reach_can_differ_by_direction() -> None:
    across, up = centre_cross("v", (1.0, 2.0), 5.0, 3.0)
    assert across.points == ((-4.0, 2.0), (6.0, 2.0))
    assert up.points == ((1.0, -1.0), (1.0, 5.0))


def test_hex_face_bows_swap_axes_for_a_horizontal_part() -> None:
    upright = hex_face_bows("v", [0.0, 2.0], ((5.0, -0.5),), "v")
    across = hex_face_bows("v", [0.0, 2.0], ((5.0, -0.5),), "u")
    assert upright[0].points == ((0.0, 4.5), (1.0, 5.0), (2.0, 4.5))
    assert across[0].points == ((4.5, 0.0), (5.0, 1.0), (4.5, 2.0))


# --- Drawing helpers --------------------------------------------------------------------


def _canvas(tmp_path: Path) -> rl_canvas.Canvas:
    return rl_canvas.Canvas(str(tmp_path / "t.pdf"), pagesize=(PAGE_W, PAGE_H))


def test_wide_dimension_puts_its_text_between_the_arrows(tmp_path: Path) -> None:
    view = PartView("f", "front", 100.0, 100.0, 4.0)
    dim = PointDim("d", "40", 40, "f", "x", (0, 0), (40, 0), -5.0, "a", "b")
    box = draw_point_dim(_canvas(tmp_path), view, dim)
    assert 100.0 < box.x0 < box.x1 < 260.0


def test_narrow_dimension_puts_its_text_beside_the_span(tmp_path: Path) -> None:
    view = PartView("f", "front", 100.0, 100.0, 4.0)
    dim = PointDim("d", "5 ±0.02", 5, "f", "x", (0, 0), (5, 0), -5.0, "a", "b")
    box = draw_point_dim(_canvas(tmp_path), view, dim)
    assert box.x0 > 120.0  # right of the span's right end


def test_vertical_dimension_text_sits_left_of_its_line(tmp_path: Path) -> None:
    view = PartView("f", "front", 100.0, 100.0, 4.0)
    dim = PointDim("d", "8.4", 8.4, "f", "y", (0, 0), (0, 8.4), 10.0, "a", "b")
    box = draw_point_dim(_canvas(tmp_path), view, dim)
    assert box.x1 <= view.x(10.0)


def test_callout_with_datum_hangs_the_symbol_below_the_shoulder(tmp_path: Path) -> None:
    view = PartView("f", "front", 300.0, 300.0, 4.0)
    callout = Callout("c", "Ø16", 16, "f", (0, -8), (250.0, 200.0), 320.0, "diameter",
                      datum="A")
    text_box, datum_box = draw_callout(_canvas(tmp_path), view, callout)
    assert datum_box is not None
    assert text_box.y0 > 200.0 and datum_box.y1 == pytest.approx(200.0)


def test_unknown_view_name_is_an_error() -> None:
    with pytest.raises(ValueError):
        GEAR_SPUR.view("top")


# --- The three sheets -------------------------------------------------------------------


def test_part_specs_are_in_the_suite() -> None:
    for spec in PARTS:
        assert isinstance(SUITE[spec.stem], PartSpec)


def test_views_are_aligned_third_angle() -> None:
    gear_front, gear_side = GEAR_SPUR.view("front"), GEAR_SPUR.view("side")
    assert gear_side.oy == gear_front.oy and gear_side.ox > gear_front.ox  # right side

    nut_top, nut_front = HEX_NUT.view("top"), HEX_NUT.view("front")
    assert nut_top.ox == nut_front.ox and nut_top.oy > nut_front.oy  # top above front

    bolt_front, bolt_end = HEX_BOLT.view("front"), HEX_BOLT.view("end")
    assert bolt_end.oy == bolt_front.oy and bolt_end.ox < bolt_front.ox  # left side

    for spec in PARTS:
        assert len({view.scale for view in spec.views}) == 1, spec.stem


@pytest.mark.parametrize("stem", PART_STEMS)
def test_part_ground_truth_has_part_type_views_and_dimension_views(stem: str) -> None:
    truth = load_ground_truth(stem)
    spec = SUITE[stem]
    assert truth["expected_part_type"] == spec.part_type
    assert truth["views"] == [{"name": v.name, "kind": v.kind} for v in spec.views]
    names = {view["name"] for view in truth["views"]}
    assert truth["dimensions"]
    for dimension in truth["dimensions"]:
        assert dimension["view"] in names, dimension["id"]


@pytest.mark.parametrize("stem", PART_STEMS)
def test_part_sheet_has_title_block_essentials_and_general_notes(stem: str) -> None:
    spec = SUITE[stem]
    assert spec.projection == "third"
    assert spec.notes_heading == "GENERAL NOTES:"
    assert spec.notes and spec.revision and spec.material
    truth = load_ground_truth(stem)
    assert truth["units"] == "mm"
    assert truth["general_tolerance"]["linear_plus_minus"] is not None


@pytest.mark.parametrize("stem", PART_STEMS)
def test_clean_parts_raise_no_findings(stem: str) -> None:
    truth = load_ground_truth(stem)
    assert truth["seeded_errors"] == []
    assert run_rules(parse_drawing(truth)) == []


def test_gear_runout_is_to_datum_a_on_the_bore() -> None:
    truth = load_ground_truth("gear_spur")
    (frame,) = truth["fcfs"]
    assert frame["characteristic"] == "circular_runout"
    assert [ref["label"] for ref in frame["datum_refs"]] == ["A"]
    assert [datum["label"] for datum in truth["datums"]] == ["A"]
    bore = next(d for d in truth["dimensions"] if d["id"] == "dim_bore_diameter")
    assert bore["value"] == 16.0 and bore["feature_type"] == "diameter"
    assert "1. 24 TEETH, MODULE 2." in truth["notes"]


def test_nut_across_corners_is_a_reference_dimension() -> None:
    truth = load_ground_truth("hex_nut")
    dims = {d["id"]: d for d in truth["dimensions"]}
    assert dims["dim_across_corners"]["is_reference"] is True
    assert dims["dim_across_corners"]["text_raw"] == "(18.48)"
    assert dims["dim_across_flats"]["value"] == 16.0
    assert dims["dim_thread"]["text_raw"] == "M10x1.5"


def test_bolt_has_the_requested_dimensions() -> None:
    truth = load_ground_truth("hex_bolt")
    values = {d["id"]: (d["value"], d["view"]) for d in truth["dimensions"]}
    assert values == {
        "dim_head_height": (5.3, "front"),
        "dim_thread_length": (22.0, "front"),
        "dim_length": (40.0, "front"),
        "dim_across_flats": (13.0, "end"),
        "dim_thread": (8.0, "front"),
    }


def test_rebuilding_a_part_is_byte_identical(tmp_path: Path) -> None:
    first = build_one(HEX_NUT, tmp_path / "a", write_png=False)
    second = build_one(HEX_NUT, tmp_path / "b", write_png=False)
    assert first["pdf"].read_bytes() == second["pdf"].read_bytes()
    assert json.loads(first["ground_truth"].read_text()) == json.loads(
        second["ground_truth"].read_text()
    )
