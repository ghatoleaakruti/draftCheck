"""Tests for comparing two models' readings of one drawing (no model, no network)."""

import copy
import json
from pathlib import Path

import pytest

from draftcheck.crosscheck import (
    CrossCheckReport,
    compare,
    match_datums,
    match_dimensions,
    match_fcfs,
    outcome_done,
    outcome_failed,
    outcome_off,
    outcome_skipped,
    outcome_timeout,
)
from draftcheck.schema import parse_drawing

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plate_001.model.json"


def reading() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def item(data: dict, kind: str, item_id: str) -> dict:
    return next(entry for entry in data[kind] if entry.get("id") == item_id)


def compared(primary: dict, cross: dict) -> CrossCheckReport:
    return compare(parse_drawing(primary), parse_drawing(cross))


# ---- Agreement --------------------------------------------------------------------------


def test_identical_readings_agree_on_every_value() -> None:
    report = compared(reading(), reading())
    assert report.disagreements == []
    assert report.only_primary == [] and report.only_cross_check == []
    assert report.values_compared == report.values_agreeing
    # 2 drawing fields + 2 datums x 1 + 6 dimensions x 6 + 1 frame x 4.
    assert report.values_compared == 2 + 2 + 36 + 4
    assert report.summary() == "Both Meta models agree on all 44 values"


def test_ids_and_order_do_not_matter_only_what_was_read() -> None:
    primary, cross = reading(), reading()
    cross["dimensions"].reverse()
    for index, dimension in enumerate(cross["dimensions"]):
        dimension["id"] = f"other_{index}"
    report = compared(primary, cross)
    assert report.disagreements == []
    assert report.values_agreeing == report.values_compared


def test_case_and_spacing_are_not_disagreements() -> None:
    primary, cross = reading(), reading()
    item(cross, "dimensions", "dim_1")["from_ref"] = " LEFT_EDGE "
    assert compared(primary, cross).disagreements == []


# ---- Disagreement on each compared field -------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "mine", "theirs"),
    [
        ("value", 35, "30", "35"),
        ("tol_plus", 0.3, "nothing", "0.3"),
        ("tol_minus", 0.3, "nothing", "0.3"),
        ("is_basic", False, "yes", "no"),
        ("from_ref", "right_edge", None, "right_edge"),
        ("to_ref", "hole_9_center", None, "hole_9_center"),
    ],
)
def test_a_dimension_field_read_differently_is_reported(field, value, mine, theirs) -> None:
    primary, cross = reading(), reading()
    item(cross, "dimensions", "dim_5")[field] = value
    report = compared(primary, cross)

    assert len(report.disagreements) == 1
    found = report.disagreements[0]
    assert (found.item_id, found.kind, found.field) == ("dim_5", "dimension", field)
    assert found.cross_check_text == theirs
    if mine is not None:
        assert found.primary_text == mine
    assert found.note == (
        f"Muse Spark Open read {found.primary_text}; Muse Spark read {found.cross_check_text}."
    )
    assert report.values_agreeing == report.values_compared - 1


def test_the_page_summary_counts_the_values_to_double_check() -> None:
    primary, cross = reading(), reading()
    item(cross, "dimensions", "dim_5")["value"] = 35
    item(cross, "dimensions", "dim_5")["tol_plus"] = 0.5
    report = compared(primary, cross)
    assert report.summary() == "Both Meta models agree on 42 of 44 values; 2 to double-check"


@pytest.mark.parametrize(
    ("field", "value", "theirs"),
    [
        ("characteristic", "perpendicularity", "perpendicularity"),
        ("tolerance", 0.5, "0.5"),
        ("material_modifier", "LMC", "LMC"),
        ("datum_refs", [{"label": "A"}, {"label": "B"}], "A|B"),
    ],
)
def test_a_frame_field_read_differently_is_reported(field, value, theirs) -> None:
    primary, cross = reading(), reading()
    item(cross, "fcfs", "fcf_1")[field] = value
    report = compared(primary, cross)

    assert [(d.item_id, d.field) for d in report.disagreements] == [("fcf_1", field)]
    assert report.disagreements[0].cross_check_text == theirs
    assert report.only_primary == [] and report.only_cross_check == []


def test_a_datum_label_read_differently_is_matched_by_place_and_reported() -> None:
    primary, cross = reading(), reading()
    cross["datums"][1]["label"] = "C"  # same symbol, same box, another letter
    report = compared(primary, cross)
    found = [(d.item_id, d.field, d.primary_text, d.cross_check_text) for d in report.disagreements]
    assert found == [("DT2", "label", "B", "C")]


def test_units_and_general_tolerance_are_compared() -> None:
    primary, cross = reading(), reading()
    cross["units"] = "in"
    cross["general_tolerance"]["linear_plus_minus"] = 0.2
    report = compared(primary, cross)
    fields = [(d.item_id, d.kind, d.field) for d in report.disagreements]
    assert fields == [
        ("drawing", "drawing", "units"),
        ("drawing", "drawing", "general_tolerance"),
    ]
    assert report.disagreements[0].note == "Muse Spark Open read mm; Muse Spark read in."


# ---- Items only one model read --------------------------------------------------------


def test_items_only_one_model_read_are_listed_on_their_side() -> None:
    primary, cross = reading(), reading()
    del cross["dimensions"][5]  # dim_6, the hole
    cross["fcfs"].append(
        {"characteristic": "flatness", "tolerance": 0.05, "bbox": [0.05, 0.9, 0.1, 0.95]}
    )
    report = compared(primary, cross)

    assert [(i.item_id, i.kind) for i in report.only_primary] == [("dim_6", "dimension")]
    assert [(i.kind, i.label) for i in report.only_cross_check] == [("fcf", "flatness frame")]
    assert report.disagreements == []
    assert report.summary() == (
        "Both Meta models agree on all 38 values; 2 items only one model read"
    )


def test_a_far_away_box_with_nothing_in_common_is_not_a_match() -> None:
    primary, cross = reading(), reading()
    dimension = item(cross, "dimensions", "dim_5")
    dimension.update(text_raw="99", value=99, bbox=[0.01, 0.01, 0.02, 0.02])
    report = compared(primary, cross)
    assert [i.item_id for i in report.only_primary] == ["dim_5"]
    assert [i.label for i in report.only_cross_check] == ["dimension 99"]


# ---- Matching passes -------------------------------------------------------------------


def test_dimensions_match_by_text_then_value_then_box() -> None:
    primary = parse_drawing(reading())
    cross_data = reading()
    item(cross_data, "dimensions", "dim_1")["text_raw"] = "80.0"  # value still 80
    item(cross_data, "dimensions", "dim_2").update(text_raw="21", value=21)  # same box only
    cross = parse_drawing(cross_data)

    pairs, left, right = match_dimensions(primary.dimensions, cross.dimensions)
    assert left == [] and right == []
    assert sorted((a.id, b.id) for a, b in pairs) == [(f"dim_{n}", f"dim_{n}") for n in range(1, 7)]


def test_two_dimensions_printed_alike_pair_with_their_nearest_counterparts() -> None:
    data = reading()
    twin = copy.deepcopy(item(data, "dimensions", "dim_5"))
    twin.update(id="dim_7", bbox=[0.7, 0.7, 0.75, 0.73])
    data["dimensions"].append(twin)
    primary = parse_drawing(data)
    cross = copy.deepcopy(data)
    cross["dimensions"].reverse()
    for dimension in cross["dimensions"]:
        dimension["id"] = "x_" + dimension["id"]
    pairs, _, _ = match_dimensions(primary.dimensions, parse_drawing(cross).dimensions)
    assert {(a.id, b.id) for a, b in pairs} >= {("dim_5", "x_dim_5"), ("dim_7", "x_dim_7")}


def test_datums_match_by_label() -> None:
    primary = parse_drawing(reading())
    cross_data = reading()
    cross_data["datums"].reverse()
    pairs, left, right = match_datums(primary.datums, parse_drawing(cross_data).datums)
    assert sorted((a.label, b.label) for a, b in pairs) == [("A", "A"), ("B", "B")]
    assert left == [] and right == []


def test_frames_match_by_characteristic_and_tolerance_then_box() -> None:
    primary = parse_drawing(reading())
    cross_data = reading()
    item(cross_data, "fcfs", "fcf_1")["tolerance"] = 0.4  # same frame, misread size
    pairs, left, right = match_fcfs(primary.fcfs, parse_drawing(cross_data).fcfs)
    assert [(a.id, b.id) for a, b in pairs] == [("fcf_1", "fcf_1")]
    assert left == [] and right == []


# ---- What the upload reports -----------------------------------------------------------


def test_every_outcome_has_the_same_keys_and_says_what_happened() -> None:
    report = compared(reading(), reading())
    outcomes = {
        "off": outcome_off(),
        "skipped": outcome_skipped("both readings would come from the same model."),
        "timeout": outcome_timeout(),
        "failed": outcome_failed("could not reach the second model."),
        "done": outcome_done(report, "muse-spark-1.2"),
    }
    keys = set(outcomes["done"])
    for status, outcome in outcomes.items():
        assert outcome["status"] == status
        assert keys <= set(outcome)
    assert outcomes["off"]["summary"] == "Cross-check off."
    assert outcomes["timeout"]["summary"] == "Cross-check didn't finish in time."
    assert outcomes["skipped"]["summary"].startswith("Cross-check skipped: ")
    assert outcomes["done"]["model"] == "muse-spark-1.2"
    json.dumps(outcomes)  # all of it goes into meta.json
