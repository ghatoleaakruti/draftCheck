"""Tests for the allowed edit operations and the guards around them.

No model and no network here — ``apply_edits`` takes plain dicts, which is exactly what a
model reply decomposes into.
"""

from draftcheck.edits import apply_edits
from draftcheck.schema import Characteristic, Units, parse_drawing


def base_drawing():
    """A small drawing with one dimension, one datum, and one frame citing datum B."""
    return parse_drawing(
        {
            "units": "mm",
            "general_tolerance": {"linear_plus_minus": 0.1},
            "datums": [{"id": "DT1", "label": "A", "bbox": [0.1, 0.1, 0.2, 0.2]}],
            "dimensions": [{"id": "D1", "value": 50.0, "is_basic": True}],
            "fcfs": [
                {
                    "id": "F1",
                    "characteristic": "position",
                    "tolerance": 0.25,
                    "datum_refs": [{"label": "A"}, {"label": "B"}],
                }
            ],
        }
    )


def only_rejection(edit) -> str:
    """Apply one edit expected to fail, and return the reason it was refused."""
    _, applied, rejected = apply_edits(base_drawing(), [edit])
    assert not applied, "this edit was supposed to be refused"
    assert len(rejected) == 1
    return rejected[0].reason


# --- edits that should work ----------------------------------------------------------


def test_set_field_changes_one_dimension_field():
    drawing, applied, rejected = apply_edits(
        base_drawing(),
        [{"op": "set_field", "target": "dimension", "id": "D1", "field": "value", "value": 60}],
    )

    assert not rejected
    assert len(applied) == 1
    assert drawing.dimensions[0].value == 60.0


def test_set_field_changes_the_drawings_units():
    drawing, _, rejected = apply_edits(
        base_drawing(),
        [{"op": "set_field", "target": "drawing", "field": "units", "value": "inches"}],
    )

    assert not rejected
    assert drawing.units is Units.INCH, "the schema's unit spellings should still apply"


def test_a_bare_number_is_accepted_as_a_general_tolerance():
    drawing, _, rejected = apply_edits(
        base_drawing(),
        [{"op": "set_field", "target": "drawing", "field": "general_tolerance", "value": 0.2}],
    )

    assert not rejected
    assert drawing.general_tolerance.linear_plus_minus == 0.2
    assert "0.2" in drawing.general_tolerance.text_raw


def test_add_datum_appends_a_symbol_with_a_fresh_id_and_a_location():
    drawing, _, rejected = apply_edits(
        base_drawing(),
        [{"op": "add_datum", "label": "b", "location": "the bottom edge of the plate"}],
    )

    assert not rejected
    assert [datum.label for datum in drawing.datums] == ["A", "B"], "label is uppercased"
    assert drawing.datums[1].id == "DT2"
    assert drawing.datums[1].location == "the bottom edge of the plate"


def test_remove_datum_ref_takes_one_letter_out_and_leaves_the_frame():
    drawing, _, rejected = apply_edits(
        base_drawing(), [{"op": "remove_datum_ref", "fcf_id": "F1", "label": "B"}]
    )

    assert not rejected
    assert len(drawing.fcfs) == 1, "the frame itself must survive"
    assert [ref.label for ref in drawing.fcfs[0].datum_refs] == ["A"]


def test_add_dimension_appends_and_assigns_its_own_id():
    drawing, _, rejected = apply_edits(
        base_drawing(),
        [
            {
                "op": "add_dimension",
                "dimension": {"id": "D1", "value": 20.0, "is_basic": True, "text_raw": "20"},
            }
        ],
    )

    assert not rejected
    assert len(drawing.dimensions) == 2
    assert drawing.dimensions[1].id == "D2", "a proposed id must never overwrite an existing item"
    assert drawing.dimensions[0].value == 50.0


def test_the_original_drawing_is_never_modified():
    original = base_drawing()

    apply_edits(original, [{"op": "add_datum", "label": "B", "location": "bottom edge"}])

    assert len(original.datums) == 1


def test_a_refused_edit_does_not_stop_the_ones_after_it():
    drawing, applied, rejected = apply_edits(
        base_drawing(),
        [
            {"op": "delete_fcf", "id": "F1"},
            {"op": "add_datum", "label": "B", "location": "bottom edge"},
        ],
    )

    assert len(applied) == 1
    assert len(rejected) == 1
    assert len(drawing.datums) == 2


# --- edits that must be refused -------------------------------------------------------


def test_deleting_a_feature_is_refused_and_says_why():
    reason = only_rejection({"op": "remove_fcf", "id": "F1"})

    assert "not an allowed operation" in reason
    assert "never deleted" in reason


def test_an_unknown_operation_is_refused():
    assert "not an allowed operation" in only_rejection({"op": "rotate_part", "degrees": 90})


def test_an_edit_that_is_not_an_object_is_refused():
    assert "JSON object" in only_rejection("please fix the datum")


def test_an_edit_with_no_op_is_refused():
    assert "which operation" in only_rejection({"target": "dimension", "field": "value"})


def test_an_edit_missing_a_required_field_is_refused():
    assert "missing required field" in only_rejection({"op": "remove_datum_ref", "label": "B"})


def test_an_unknown_item_id_is_refused():
    reason = only_rejection(
        {"op": "set_field", "target": "dimension", "id": "D99", "field": "value", "value": 1}
    )

    assert "D99" in reason


def test_changing_an_id_is_refused():
    reason = only_rejection(
        {"op": "set_field", "target": "fcf", "id": "F1", "field": "id", "value": "F9"}
    )

    assert "'id' cannot be edited" in reason


def test_changing_a_bbox_is_refused():
    reason = only_rejection(
        {"op": "set_field", "target": "datum", "id": "DT1", "field": "bbox", "value": None}
    )

    assert "'bbox' cannot be edited" in reason


def test_overwriting_datum_refs_wholesale_is_refused():
    reason = only_rejection(
        {"op": "set_field", "target": "fcf", "id": "F1", "field": "datum_refs", "value": []}
    )

    assert "remove_datum_ref" in reason, "the refusal should point at the operation that is allowed"


def test_an_unlisted_field_is_refused():
    assert "not an editable field" in only_rejection(
        {"op": "set_field", "target": "dimension", "id": "D1", "field": "colour", "value": "red"}
    )


def test_a_value_the_schema_quietly_discards_is_refused():
    # Lenient parsing turns a negative tolerance into None rather than raising, so the
    # edit would look applied while changing nothing.
    reason = only_rejection(
        {"op": "set_field", "target": "dimension", "id": "D1", "field": "tol_plus", "value": -5}
    )

    assert "does not accept" in reason


def test_a_value_that_breaks_the_item_is_refused_and_rolled_back():
    drawing, applied, rejected = apply_edits(
        base_drawing(),
        [{"op": "set_field", "target": "fcf", "id": "F1", "field": "tolerance", "value": "big"}],
    )

    assert not applied
    assert len(rejected) == 1
    assert len(drawing.fcfs) == 1, "the frame must not be lost to a bad edit"
    assert drawing.fcfs[0].tolerance == 0.25


def test_an_unreadable_characteristic_is_refused_rather_than_dropping_the_frame():
    reason = only_rejection(
        {
            "op": "set_field",
            "target": "fcf",
            "id": "F1",
            "field": "characteristic",
            "value": "wobbliness",
        }
    )

    assert "cannot read" in reason


def test_a_valid_characteristic_change_still_works():
    drawing, _, rejected = apply_edits(
        base_drawing(),
        [
            {
                "op": "set_field",
                "target": "fcf",
                "id": "F1",
                "field": "characteristic",
                "value": "perpendicularity",
            }
        ],
    )

    assert not rejected
    assert drawing.fcfs[0].characteristic is Characteristic.PERPENDICULARITY


def test_removing_a_datum_ref_the_frame_does_not_have_is_refused():
    reason = only_rejection({"op": "remove_datum_ref", "fcf_id": "F1", "label": "Z"})

    assert "does not reference datum Z" in reason


def test_removing_a_datum_ref_from_an_unknown_frame_is_refused():
    assert "F9" in only_rejection({"op": "remove_datum_ref", "fcf_id": "F9", "label": "A"})


def test_a_datum_label_that_is_not_letters_is_refused():
    assert "not a datum label" in only_rejection(
        {"op": "add_datum", "label": "left side", "location": "the left edge"}
    )


def test_add_datum_without_a_location_is_refused():
    assert "where on the drawing" in only_rejection(
        {"op": "add_datum", "label": "C", "location": "   "}
    )


def test_add_dimension_without_a_value_is_refused():
    reason = only_rejection({"op": "add_dimension", "dimension": {"text_raw": "about 20"}})

    assert "value" in reason


def test_edits_that_are_not_a_list_are_refused_without_crashing():
    drawing, applied, rejected = apply_edits(base_drawing(), {"op": "add_datum"})

    assert not applied
    assert len(rejected) == 1
    assert "expected a list" in rejected[0].reason
    assert drawing == base_drawing()


def test_none_instead_of_edits_is_refused_without_crashing():
    _, applied, rejected = apply_edits(base_drawing(), None)

    assert not applied
    assert "NoneType" in rejected[0].reason
