"""Tests for the short before/after strings and pen labels the page draws for each edit.

These are worded in ``redline.py`` beside ``plain_edit`` so the browser never makes up a
sentence of its own; the API hands them over with every edit in the ``/fix`` reply.
"""

from __future__ import annotations

import pytest

from draftcheck.edits import AddDatum, AddDimension, RemoveDatumRef, SetField, apply_edits
from draftcheck.redline import (
    NOTHING,
    edit_display,
    pen_label,
    short_dimension,
    short_fcf,
)
from draftcheck.schema import FCF, Dimension, Drawing, parse_drawing


@pytest.fixture
def drawing() -> Drawing:
    return parse_drawing(
        {
            "units": "mm",
            "general_tolerance": {"linear_plus_minus": 0.1},
            "datums": [{"id": "DT1", "label": "A"}, {"id": "DT2", "label": "B"}],
            "dimensions": [
                {"id": "D1", "value": 60, "tol_plus": 0.2, "tol_minus": 0.2},
                {"id": "D2", "value": 8, "feature_type": "diameter", "quantity": 2},
                {"id": "D3", "value": 80, "is_basic": True},
            ],
            "fcfs": [
                {
                    "id": "F1",
                    "characteristic": "position",
                    "tolerance": 0.25,
                    "diameter_zone": True,
                    "material_modifier": "MMC",
                    "datum_refs": [{"label": "A"}, {"label": "B"}, {"label": "C"}],
                },
                {
                    "id": "F2",
                    "characteristic": "flatness",
                    "tolerance": 0.05,
                },
            ],
        }
    )


def applied(drawing: Drawing, edit) -> Drawing:
    """The drawing after ``edit``, asserting the guards let it through."""
    after, took, refused = apply_edits(drawing, [edit])
    assert took and not refused, refused
    return after


# --- the items, written short -----------------------------------------------------------


def test_a_frame_reads_as_it_is_printed(drawing: Drawing):
    assert short_fcf(drawing.fcfs[0]) == "⌖ Ø0.25 Ⓜ | A | B | C"


def test_a_frame_with_no_datums_or_modifiers_is_just_its_zone(drawing: Drawing):
    assert short_fcf(drawing.fcfs[1]) == "⏥ 0.05"


def test_a_datum_reference_keeps_its_own_modifier():
    frame = FCF(
        characteristic="position", tolerance=0.1, datum_refs=[{"label": "A", "modifier": "MMC"}]
    )
    assert short_fcf(frame) == "⌖ 0.1 | A Ⓜ"


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"value": 60, "tol_plus": 0.2, "tol_minus": 0.2}, "60 ±0.2"),
        ({"value": 8, "feature_type": "diameter", "quantity": 2}, "2X Ø8"),
        ({"value": 80, "is_basic": True}, "[80]"),
        ({"value": 12, "is_reference": True}, "(12)"),
        ({"value": 10, "tol_plus": 0.1, "tol_minus": 0.05}, "10 +0.1/−0.05"),
        ({"value": 10, "tol_plus": 0.1}, "10 +0.1/−0"),
        ({"value": 25.5}, "25.5"),
    ],
)
def test_a_dimension_reads_as_it_is_printed(fields: dict, expected: str):
    assert short_dimension(Dimension(**fields)) == expected


# --- before and after -------------------------------------------------------------------


def test_removing_a_datum_ref_shows_the_frame_either_side(drawing: Drawing):
    edit = RemoveDatumRef(op="remove_datum_ref", fcf_id="F1", label="C")
    shown = edit_display(edit, drawing, applied(drawing, edit))

    assert shown.before == "⌖ Ø0.25 Ⓜ | A | B | C"
    assert shown.after == "⌖ Ø0.25 Ⓜ | A | B"
    assert shown.pen == "drop C"
    assert shown.target_id == "F1"


def test_setting_a_tolerance_shows_the_dimension_either_side(drawing: Drawing):
    edit = SetField(op="set_field", target="dimension", id="D1", field="tol_plus", value=0.025)
    shown = edit_display(edit, drawing, applied(drawing, edit))

    assert shown.before == "60 ±0.2"
    assert shown.after == "60 +0.025/−0.2"
    assert shown.pen == "±0.025"


def test_setting_a_value_writes_the_new_value(drawing: Drawing):
    edit = SetField(op="set_field", target="dimension", id="D1", field="value", value=62)
    shown = edit_display(edit, drawing, applied(drawing, edit))

    assert (shown.before, shown.after, shown.pen) == ("60 ±0.2", "62 ±0.2", "62")


def test_an_added_datum_had_nothing_before_it(drawing: Drawing):
    edit = AddDatum(op="add_datum", label="c", location="the right edge")
    shown = edit_display(edit, drawing, applied(drawing, edit))

    assert (shown.before, shown.after, shown.pen) == (NOTHING, "datum C", "add datum C")
    assert shown.target_id is None


def test_an_added_dimension_is_read_off_the_corrected_drawing(drawing: Drawing):
    edit = AddDimension(op="add_dimension", dimension={"value": 30, "is_basic": True})
    shown = edit_display(edit, drawing, applied(drawing, edit))

    assert (shown.before, shown.after, shown.pen) == (NOTHING, "[30]", "add 30")


def test_a_drawing_level_field_is_shown_without_an_item(drawing: Drawing):
    edit = SetField(op="set_field", target="drawing", field="general_tolerance", value=0.2)
    shown = edit_display(edit, drawing, applied(drawing, edit))

    assert (shown.before, shown.after, shown.pen) == ("general ±0.1", "general ±0.2", "±0.2")


def test_a_refused_edit_shows_what_it_asked_for(drawing: Drawing):
    """With no corrected drawing to read, the 'after' is worked out on a copy."""
    edit = RemoveDatumRef(op="remove_datum_ref", fcf_id="F1", label="B")
    shown = edit_display(edit, drawing, None)

    assert shown.after == "⌖ Ø0.25 Ⓜ | A | C"
    # The drawing itself is not touched.
    assert short_fcf(drawing.fcfs[0]) == "⌖ Ø0.25 Ⓜ | A | B | C"


def test_a_refused_edit_with_an_unreadable_value_says_what_was_sent(drawing: Drawing):
    edit = SetField(op="set_field", target="dimension", id="D1", field="value", value="wide")
    shown = edit_display(edit, drawing, None)

    assert shown.before == "60 ±0.2"
    assert shown.after == "value wide"


def test_a_refused_edit_on_a_missing_item_has_nothing_before(drawing: Drawing):
    edit = SetField(op="set_field", target="dimension", id="D99", field="value", value=5)
    shown = edit_display(edit, drawing, None)

    assert shown.before == NOTHING
    assert shown.after == "value 5"


# --- pen labels -------------------------------------------------------------------------


def sf(target: str, **fields) -> SetField:
    return SetField(op="set_field", target=target, **fields)


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (sf("dimension", id="D1", field="tol_minus", value=0.1), "±0.1"),
        (sf("fcf", id="F1", field="tolerance", value=0.3), "Ø0.3"),
        (sf("fcf", id="F2", field="tolerance", value=0.02), "0.02"),
        (sf("dimension", id="D1", field="is_basic", value=True), "make basic"),
        (sf("dimension", id="D3", field="is_basic", value=False), "not basic"),
        (sf("fcf", id="F1", field="material_modifier", value="lmc"), "LMC"),
        (sf("drawing", field="units", value="in"), "units in"),
        (
            sf("dimension", id="D1", field="from_ref", value="left_edge"),
            "set measured-from reference",
        ),
        (sf("dimension", id="D1", field="tol_plus", value=None), "clear upper tolerance"),
        (sf("dimension", id="D1", field="feature_type", value=None), "clear feature type"),
        (AddDimension(op="add_dimension", dimension={"text_raw": "R5"}), "add dimension"),
    ],
)
def test_pen_labels_are_two_or_three_words(drawing: Drawing, edit, expected: str):
    assert pen_label(edit, drawing) == expected
    assert len(expected.split()) <= 3
