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
from draftcheck.schema import finding_key, parse_drawing
from scripts.drawing_suite import (
    BLOCK_3VIEW,
    BOLTED_JOINT,
    FLANGE,
    GEAR_SPUR,
    GRIPPER_CLAW,
    HEX_BOLT,
    HEX_NUT,
    STEPPED_SHAFT,
    SUITE,
    AngleDim,
    Balloon,
    Callout,
    PartSpec,
    PartView,
    PointDim,
    Table,
    build_one,
    centre_cross,
    draw_angle_dim,
    draw_balloon,
    draw_callout,
    draw_hatch,
    draw_point_dim,
    draw_point_dim_with_datum,
    draw_rich,
    draw_table,
    hex_face_bows,
    hexagon,
    iso,
    keyed_circle,
    plain_text,
    rim_toward,
    spur_gear_outline,
    surface_datum,
)
from scripts.make_sample_drawing import PAGE_H, PAGE_W
from scripts.run_eval import load_ground_truth

PARTS = (GEAR_SPUR, HEX_NUT, HEX_BOLT)
PARTS_B = (FLANGE, STEPPED_SHAFT, GRIPPER_CLAW)
PARTS_C = (BLOCK_3VIEW, BOLTED_JOINT)
PART_STEMS = [spec.stem for spec in (*PARTS, *PARTS_B, *PARTS_C)]


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


def test_keyed_circle_reaches_the_keyway_flat() -> None:
    points = keyed_circle(16.0, 5.0, 10.3)
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
    if stem == "stepped_shaft":
        # Its missing general tolerance is the seeded DIM-001 error.
        assert truth["general_tolerance"] is None
    else:
        assert truth["general_tolerance"]["linear_plus_minus"] is not None


@pytest.mark.parametrize(
    "stem", ["gear_spur", "hex_nut", "hex_bolt", "gripper_claw", "bolted_joint"]
)
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


# --- Part b helpers ---------------------------------------------------------------------


def test_keyed_circle_outlines_a_shaft_keyway_inside_the_circle() -> None:
    points = keyed_circle(25.0, 8.0, 8.5)
    assert max(y for _, y in points) == pytest.approx(math.sqrt(12.5**2 - 16))
    assert {x for x, y in points if y == pytest.approx(8.5)} == {-4.0, 4.0}


def test_rim_toward_lands_on_the_rim_facing_the_elbow() -> None:
    view = PartView("f", "front", 100.0, 100.0, 2.0)
    u, v = rim_toward(view, (10.0, 0.0), 5.0, (300.0, 100.0))  # elbow straight right
    assert (u, v) == pytest.approx((15.0, 0.0))


def test_hatch_draws_inside_a_clip(tmp_path: Path) -> None:
    c = _canvas(tmp_path)
    draw_hatch(c, [(100.0, 100.0), (140.0, 100.0), (140.0, 140.0), (100.0, 140.0)])
    c.showPage()
    c.save()  # no error, and the clip is balanced by its restoreState


@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
def test_surface_datum_box_points_away_from_the_surface(
    tmp_path: Path, direction: str
) -> None:
    box = surface_datum(_canvas(tmp_path), 200.0, 200.0, direction, "A")
    centre_x, centre_y = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    expected = {"up": centre_y > 200, "down": centre_y < 200, "left": centre_x < 200,
                "right": centre_x > 200}
    assert expected[direction]


def test_size_dimension_datum_sits_beyond_its_b_end(tmp_path: Path) -> None:
    view = PartView("f", "front", 100.0, 300.0, 2.0)
    dim = PointDim("d", "Ø25", 25, "f", "y", (0, -12.5), (0, 12.5), -8.0, "a", "b",
                   tol=0.01, datum="A")
    _, datum_box = draw_point_dim_with_datum(_canvas(tmp_path), view, dim)
    assert datum_box is not None and datum_box.y0 >= view.y(12.5) - 0.01


def test_angle_dimension_text_is_beside_the_arc(tmp_path: Path) -> None:
    view = PartView("f", "front", 100.0, 100.0, 4.0)
    dim = AngleDim("a", "15°", 15, "f", (0.0, 0.0), 20.0, 0.0, 15.0, "x", "y")
    box = draw_angle_dim(_canvas(tmp_path), view, dim)
    assert box.x0 > view.x(20.0)  # outside the 20 mm arc, to the right


def test_inside_dimension_draws_no_extension_lines(tmp_path: Path) -> None:
    """A diameter dimensioned across the part: points already on the dimension line."""
    view = PartView("f", "front", 100.0, 300.0, 2.0)
    dim = PointDim("d", "Ø40", 40, "f", "y", (45, -20), (45, 20), 45.0, "a", "b")
    box = draw_point_dim(_canvas(tmp_path), view, dim)
    assert box.x1 <= view.x(45.0)


# --- Part b sheets ----------------------------------------------------------------------


def test_part_b_views_and_labels() -> None:
    front, section = FLANGE.view("front"), FLANGE.view("section_a_a")
    assert section.kind == "section" and section.label == ("SECTION A-A",)
    assert section.oy == front.oy and section.ox > front.ox  # in projection, to the right
    assert [plane.label for plane in FLANGE.cutting_planes] == ["A"]

    kinds = {view.name: view.kind for view in STEPPED_SHAFT.views}
    assert kinds == {"front": "front", "section_b_b": "section", "detail_c": "detail"}
    detail = STEPPED_SHAFT.view("detail_c")
    assert detail.scale == pytest.approx(4 * STEPPED_SHAFT.view("front").scale)
    assert detail.label == ("DETAIL C", "SCALE 4:1")
    assert [plane.label for plane in STEPPED_SHAFT.cutting_planes] == ["B"]

    claw_front, claw_top = GRIPPER_CLAW.view("front"), GRIPPER_CLAW.view("top")
    assert claw_top.ox == claw_front.ox and claw_top.oy > claw_front.oy


def test_flange_seeds_an_undefined_datum_c_on_the_hole_position() -> None:
    truth = load_ground_truth("flange")
    frames = {f["id"]: f for f in truth["fcfs"]}
    position = frames["fcf_position_holes"]
    assert [r["label"] for r in position["datum_refs"]] == ["A", "B", "C"]
    assert sorted(d["label"] for d in truth["datums"]) == ["A", "B"]
    assert frames["fcf_perpendicularity_bore"]["characteristic"] == "perpendicularity"
    assert [r["label"] for r in frames["fcf_perpendicularity_bore"]["datum_refs"]] == ["A"]
    dims = {d["id"]: d for d in truth["dimensions"]}
    assert dims["dim_bolt_circle"]["is_basic"] is True
    assert dims["dim_hole_diameter"]["quantity"] == 6
    findings = run_rules(parse_drawing(truth))
    assert [finding_key(f) for f in findings] == ["GDT-001@fcf_position_holes"]


def test_shaft_has_no_general_tolerance_and_one_bare_dimension() -> None:
    truth = load_ground_truth("stepped_shaft")
    assert truth["general_tolerance"] is None
    bare = [d["id"] for d in truth["dimensions"]
            if not d["is_basic"] and not d["is_reference"] and d["tol_plus"] is None]
    assert bare == ["dim_collar_diameter"]
    (frame,) = truth["fcfs"]
    assert frame["characteristic"] == "circular_runout"
    views = {d["id"]: d["view"] for d in truth["dimensions"]}
    assert views["dim_keyway_width"] == "section_b_b"
    assert views["dim_fillet_radius"] == "detail_c"
    findings = run_rules(parse_drawing(truth))
    assert [finding_key(f) for f in findings] == ["DIM-001@dim_collar_diameter"]


def test_claw_profile_and_angle() -> None:
    truth = load_ground_truth("gripper_claw")
    (frame,) = truth["fcfs"]
    assert frame["characteristic"] == "profile_surface"
    assert [r["label"] for r in frame["datum_refs"]] == ["A", "B"]
    dims = {d["id"]: d for d in truth["dimensions"]}
    assert dims["dim_taper_angle"]["feature_type"] == "angular"
    assert dims["dim_taper_angle"]["value"] == 15.0
    assert dims["dim_jaw_radius"]["is_basic"] is True
    assert dims["dim_thickness"]["view"] == "top"


# --- Part c helpers ---------------------------------------------------------------------


def test_iso_projects_the_three_axes_at_120_degrees() -> None:
    angles = sorted(
        math.degrees(math.atan2(v, u)) % 360
        for u, v in (iso(1, 0, 0), iso(0, 1, 0), iso(0, 0, 1))
    )
    assert angles == pytest.approx([30.0, 90.0, 330.0])
    assert all(math.hypot(*iso(*axis)) == pytest.approx(math.sqrt(2 / 3))
               for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1)))


def test_plain_text_spells_symbol_tokens_as_unicode() -> None:
    assert plain_text("[CB]Ø11 [DP]6.8") == "⌴Ø11 ↧6.8"


def test_rich_text_counts_symbols_in_its_width(tmp_path: Path) -> None:
    c = _canvas(tmp_path)
    plain = draw_rich(c, 0.0, 0.0, "Ø11 6.8", 10.0)
    with_symbols = draw_rich(c, 0.0, 0.0, "[CB]Ø11 [DP]6.8", 10.0)
    assert with_symbols == pytest.approx(plain + 18.0)


def test_table_header_at_bottom_puts_the_first_row_just_above_it(tmp_path: Path) -> None:
    table = Table("t", "parts_list", 100.0, 100.0, (20.0, 40.0), ("A", "B"),
                  (("1", "x"), ("2", "y")), header_at_bottom=True)
    box, cells = draw_table(_canvas(tmp_path), table)
    assert (box.y0, box.y1) == (100.0, 139.0)
    assert cells[0][0].y0 == pytest.approx(113.0)  # row 1 sits on the header
    assert cells[1][0].y0 == pytest.approx(126.0)


def test_table_header_on_top_by_default(tmp_path: Path) -> None:
    table = Table("t", "hole_table", 100.0, 100.0, (20.0,), ("H",), (("A1",), ("A2",)),
                  title="HOLE TABLE")
    box, cells = draw_table(_canvas(tmp_path), table)
    assert cells[0][0].y1 == pytest.approx(126.0)  # first row right under the header
    assert box.y1 > 139.0  # the title sits above the table


def test_balloon_box_is_its_circle(tmp_path: Path) -> None:
    view = PartView("f", "front", 100.0, 100.0, 2.0)
    box = draw_balloon(_canvas(tmp_path), view, Balloon("b", 3, "f", (0.0, 0.0),
                                                       (200.0, 200.0)))
    assert (box.x0, box.y0, box.x1, box.y1) == (190.0, 190.0, 210.0, 210.0)


# --- Part c sheets ----------------------------------------------------------------------


def test_block_views_are_third_angle_with_an_isometric() -> None:
    front, top, right = (BLOCK_3VIEW.view(n) for n in ("front", "top", "right"))
    assert top.ox == front.ox and top.oy > front.oy
    assert right.oy == front.oy and right.ox > front.ox
    assert BLOCK_3VIEW.view("iso").kind == "isometric"


def test_block_counterbore_callout_and_hole_table() -> None:
    truth = load_ground_truth("block_3view")
    dims = {d["id"]: d for d in truth["dimensions"]}
    assert dims["dim_cbore_hole"]["text_raw"] == "Ø6.6 THRU ⌴Ø11 ↧6.8"
    assert dims["dim_cbore_diameter"]["value"] == 11.0
    assert dims["dim_cbore_depth"]["value"] == 6.8
    assert dims["dim_slot_width"]["view"] == "front"
    assert dims["dim_chamfer"]["view"] == "right"
    assert [row["hole"] for row in truth["hole_table"]] == ["A1", "A2", "A3", "A4"]
    assert {(row["x"], row["y"]) for row in truth["hole_table"]} == {
        (60.0, 10.0), (72.0, 10.0), (60.0, 40.0), (72.0, 40.0)}
    assert dims["dim_hole_a3_y"]["value"] == 40.0
    assert dims["dim_hole_a3_size"]["feature_type"] == "thread"
    assert dims["dim_hole_a3_size"]["value"] == 5.0


def test_block_seeds_flatness_to_a_and_keeps_parallelism() -> None:
    truth = load_ground_truth("block_3view")
    frames = {f["id"]: f for f in truth["fcfs"]}
    assert frames["fcf_parallelism_top"]["characteristic"] == "parallelism"
    assert frames["fcf_flatness_bottom"]["characteristic"] == "flatness"
    findings = run_rules(parse_drawing(truth))
    assert [finding_key(f) for f in findings] == ["GDT-003@fcf_flatness_bottom"]


def test_assembly_parts_list_and_balloons_agree() -> None:
    truth = load_ground_truth("bolted_joint")
    items = [row["item"] for row in truth["parts_list"]]
    assert items == [1, 2, 3, 4, 5]
    assert sorted(b["item"] for b in truth["balloons"]) == items
    assert {row["description"] for row in truth["parts_list"]} >= {
        "HEX BOLT M10x1.5 x 35", "PLAIN WASHER M10", "HEX NUT M10x1.5"}
    dims = {d["id"]: d for d in truth["dimensions"]}
    assert dims["dim_stack_height"]["is_reference"] is True
    assert dims["dim_plate_overlap"]["value"] == 40.0


def test_assembly_section_hatches_plates_only_in_opposite_directions() -> None:
    hatches = [shape for shape in BOLTED_JOINT.shapes if shape.kind == "hatch"]
    assert sorted({shape.angle for shape in hatches}) == [45.0, 135.0]
    # Every hatched region lies within the two plates (Z 0 to -22): no fastener is cut.
    assert all(-22 <= v <= 0 for shape in hatches for _, v in shape.points)


def test_older_ground_truth_has_no_table_keys() -> None:
    for stem in ("gear_spur", "flange", "plate_clean"):
        truth = load_ground_truth(stem)
        assert not {"hole_table", "parts_list", "balloons"} & truth.keys()
