"""Tests for the synthetic drawing generator. No network; everything lands in tmp_path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from draftcheck.schema import Units, parse_drawing
from scripts.make_sample_drawing import MARGIN, PAGE_H, PAGE_W, build

EXPECTED_DIM_IDS = {
    "dim_overall_width",
    "dim_overall_height",
    "dim_hole1_x",
    "dim_hole2_x",
    "dim_hole_y",
    "dim_hole_diameter",
}


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Generate the drawing once for this module."""
    return build(tmp_path_factory.mktemp("generated"))


@pytest.fixture(scope="module")
def ground_truth(generated: dict[str, Path]) -> dict:
    return json.loads(generated["ground_truth"].read_text(encoding="utf-8"))


def test_all_files_written(generated: dict[str, Path]) -> None:
    for path in generated.values():
        assert path.is_file(), path
        assert path.stat().st_size > 0, path


def test_pdf_is_a_pdf(generated: dict[str, Path]) -> None:
    assert generated["pdf"].read_bytes().startswith(b"%PDF-")


def test_sheet_metadata(ground_truth: dict) -> None:
    assert ground_truth["units"] == "mm"
    assert ground_truth["general_tolerance"]["linear_plus_minus"] == 0.1
    assert ground_truth["title_block"] == {
        "title": "BRACKET PLATE",
        "drawing_number": "DC-0001",
        "revision": "A",
        "material": "AL 6061-T6",
        "scale": "1:1",
    }


def test_ground_truth_parses_against_the_schema(ground_truth: dict) -> None:
    """The ground truth is written in the schema's field names, so it parses cleanly."""
    drawing = parse_drawing(ground_truth)
    assert drawing.parse_issues == []
    assert drawing.units == Units.MM
    assert len(drawing.dimensions) == 6
    assert len(drawing.fcfs) == 1
    assert [datum.label for datum in drawing.datums] == ["A", "B"]


def test_has_six_dimensions(ground_truth: dict) -> None:
    dimensions = ground_truth["dimensions"]
    assert len(dimensions) == 6
    assert {dim["id"] for dim in dimensions} == EXPECTED_DIM_IDS


def test_every_dimension_is_fully_described(ground_truth: dict) -> None:
    for dim in ground_truth["dimensions"]:
        assert isinstance(dim["value"], float), dim["id"]
        assert isinstance(dim["is_basic"], bool), dim["id"]
        assert dim["quantity"] >= 1, dim["id"]
        # A basic dimension carries no tolerance; a toleranced one must have both limits.
        if dim["is_basic"]:
            assert dim["tol_plus"] is None and dim["tol_minus"] is None, dim["id"]
        else:
            assert dim["tol_plus"] > 0 and dim["tol_minus"] > 0, dim["id"]


def test_basic_dimensions_are_the_locating_ones(ground_truth: dict) -> None:
    basic = {dim["id"] for dim in ground_truth["dimensions"] if dim["is_basic"]}
    assert basic == {"dim_hole1_x", "dim_hole2_x", "dim_hole_y"}


def test_hole_note_covers_two_holes(ground_truth: dict) -> None:
    hole = next(d for d in ground_truth["dimensions"] if d["id"] == "dim_hole_diameter")
    assert hole["quantity"] == 2
    assert hole["value"] == 8.0
    assert hole["feature_type"] == "diameter"


def test_has_one_feature_control_frame(ground_truth: dict) -> None:
    frames = ground_truth["fcfs"]
    assert len(frames) == 1
    fcf = frames[0]
    assert fcf["characteristic"] == "position"
    assert fcf["tolerance"] == 0.25
    assert fcf["diameter_zone"] is True
    assert fcf["material_modifier"] == "MMC"
    assert [ref["label"] for ref in fcf["datum_refs"]] == ["A", "B", "C"]


def test_only_datums_a_and_b_are_defined(ground_truth: dict) -> None:
    assert [datum["label"] for datum in ground_truth["datums"]] == ["A", "B"]


def test_seeded_error_is_the_missing_datum_c(ground_truth: dict) -> None:
    errors = ground_truth["seeded_errors"]
    assert len(errors) == 1
    error = errors[0]
    assert error["type"] == "missing_datum_feature"
    assert error["related"]["datum"] == "C"

    # The error must be real: the FCF cites a datum that no datum symbol defines.
    defined = {datum["label"] for datum in ground_truth["datums"]}
    referenced = {ref["label"] for ref in ground_truth["fcfs"][0]["datum_refs"]}
    assert referenced - defined == {"C"}


def _iter_bboxes(ground_truth: dict):
    """Yield (label, bbox) for every item in the ground truth that carries one."""
    for key in ("datums", "dimensions", "fcfs", "seeded_errors"):
        for item in ground_truth.get(key, []):
            # Datums are identified by their label; everything else carries an id.
            yield item.get("id") or item["label"], item["bbox"]


def test_every_item_has_a_bbox(ground_truth: dict) -> None:
    labels = [label for label, _ in _iter_bboxes(ground_truth)]
    assert len(labels) == 2 + 6 + 1 + 1


@pytest.mark.parametrize("key", ["datums", "dimensions", "fcfs"])
def test_bboxes_are_valid(ground_truth: dict, key: str) -> None:
    for label, bbox in _iter_bboxes({key: ground_truth[key]}):
        assert len(bbox) == 4, label
        x0, y0, x1, y1 = bbox
        assert 0.0 <= x0 < x1 <= 1.0, (label, bbox)
        assert 0.0 <= y0 < y1 <= 1.0, (label, bbox)


def test_bboxes_sit_inside_the_sheet_border(ground_truth: dict) -> None:
    """Nothing should be drawn in the 20 pt margin outside the border."""
    margin_x = MARGIN / PAGE_W
    margin_y = MARGIN / PAGE_H
    for label, (x0, y0, x1, y1) in _iter_bboxes(ground_truth):
        assert x0 >= margin_x, label
        assert y0 >= margin_y, label
        assert x1 <= 1.0 - margin_x, label
        assert y1 <= 1.0 - margin_y, label


def test_build_can_skip_the_png(tmp_path: Path) -> None:
    paths = build(tmp_path, write_png=False)
    assert "png" not in paths
    assert not (tmp_path / "plate_001.png").exists()
