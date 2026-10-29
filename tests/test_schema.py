import json
from pathlib import Path

import pytest

from draftcheck.schema import (
    FORM_CONTROLS,
    Characteristic,
    Drawing,
    Finding,
    Units,
    parse_drawing,
)

FIXTURES = Path(__file__).parent / "fixtures"
GROUND_TRUTH = Path(__file__).parent.parent / "samples" / "generated" / "plate_001.gt.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


# --- real files -----------------------------------------------------------------


def test_model_output_parses():
    """The actual extraction from the step-10e spike run parses end to end."""
    drawing = parse_drawing(load(FIXTURES / "plate_001.model.json"))

    assert drawing.units == Units.MM
    assert len(drawing.dimensions) == 6
    assert len(drawing.fcfs) == 1

    fcf = drawing.fcfs[0]
    assert fcf.characteristic == Characteristic.POSITION
    assert fcf.tolerance == 0.25
    assert fcf.diameter_zone is True
    assert fcf.material_modifier == "MMC"
    assert [ref.label for ref in fcf.datum_refs] == ["A", "B", "C"]

    # The seeded error: the frame cites C, but only A and B are defined on the drawing.
    assert [datum.label for datum in drawing.datums] == ["A", "B"]


def test_model_output_details():
    drawing = parse_drawing(load(FIXTURES / "plate_001.model.json"))

    assert drawing.title_block.drawing_number == "DC-0001"
    assert drawing.general_tolerance.linear_plus_minus == 0.1

    hole = next(d for d in drawing.dimensions if d.feature_type == "diameter")
    assert hole.value == 8.0
    assert hole.quantity == 2
    assert hole.tol_plus == 0.1
    assert hole.tol_minus == 0.1
    assert hole.bbox == [0.074, 0.658, 0.148, 0.678]


def test_ground_truth_parses():
    """The generator's ground truth is written in the schema's own field names."""
    drawing = parse_drawing(load(GROUND_TRUTH))

    assert drawing.units == Units.MM
    assert drawing.parse_issues == []
    assert [datum.label for datum in drawing.datums] == ["A", "B"]

    assert len(drawing.fcfs) == 1
    fcf = drawing.fcfs[0]
    assert fcf.characteristic == Characteristic.POSITION
    assert fcf.tolerance == 0.25
    assert fcf.diameter_zone is True
    assert fcf.material_modifier == "MMC"
    assert [ref.label for ref in fcf.datum_refs] == ["A", "B", "C"]


def test_ground_truth_dimensions():
    """All six dimensions, each with the tolerance the drawing actually prints."""
    drawing = parse_drawing(load(GROUND_TRUTH))
    assert len(drawing.dimensions) == 6

    tolerances = {d.id: (d.value, d.tol_plus, d.tol_minus) for d in drawing.dimensions}
    assert tolerances == {
        "dim_overall_width": (100.0, 0.2, 0.2),
        "dim_overall_height": (60.0, 0.2, 0.2),
        "dim_hole1_x": (20.0, None, None),
        "dim_hole2_x": (80.0, None, None),
        "dim_hole_y": (30.0, None, None),
        "dim_hole_diameter": (8.0, 0.1, 0.1),
    }

    # The three basic dimensions locate the holes, and basics carry no tolerance.
    basic = {d.id for d in drawing.dimensions if d.is_basic}
    assert basic == {"dim_hole1_x", "dim_hole2_x", "dim_hole_y"}
    assert all(d.tol_plus is None and d.tol_minus is None for d in drawing.dimensions if d.is_basic)


def test_ground_truth_and_model_output_agree():
    """Both files describe the same drawing, so the parsed facts must line up."""
    truth = parse_drawing(load(GROUND_TRUTH))
    model = parse_drawing(load(FIXTURES / "plate_001.model.json"))

    assert truth.units == model.units
    assert sorted(d.value for d in truth.dimensions) == sorted(d.value for d in model.dimensions)
    assert [d.label for d in truth.datums] == [d.label for d in model.datums]
    assert [r.label for r in truth.fcfs[0].datum_refs] == [
        r.label for r in model.fcfs[0].datum_refs
    ]


# --- units ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("MILLIMETERS", Units.MM),
        ("millimeter", Units.MM),
        ("MM", Units.MM),
        ("  mm  ", Units.MM),
        ("INCHES", Units.INCH),
        ("inch", Units.INCH),
        ("IN", Units.INCH),
        ("cubits", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_units_normalization(raw, expected):
    assert parse_drawing({"units": raw}).units == expected


def test_units_missing_is_none():
    assert parse_drawing({}).units is None


# --- bounding boxes -------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        [0.1, 0.2, 0.3],  # too few numbers
        [0.1, 0.2, 0.3, 0.4, 0.5],  # too many
        [0.9, 0.2, 0.1, 0.4],  # x0 >= x1
        [0.1, 0.9, 0.3, 0.4],  # y0 >= y1
        [0.1, 0.2, 1.4, 0.4],  # outside 0..1
        [-0.1, 0.2, 0.3, 0.4],  # negative
        [0.1, 0.1, 0.1, 0.1],  # zero area
        ["a", "b", "c", "d"],  # not numeric
        "0.1,0.2,0.3,0.4",  # not a list
        {},
    ],
)
def test_invalid_bbox_becomes_none(bad):
    drawing = parse_drawing({"datums": [{"label": "A", "bbox": bad}]})
    assert drawing.datums[0].bbox is None


def test_valid_bbox_kept():
    drawing = parse_drawing({"datums": [{"label": "A", "bbox": [0.1, 0.2, 0.3, 0.4]}]})
    assert drawing.datums[0].bbox == [0.1, 0.2, 0.3, 0.4]


def test_bbox_strings_become_floats():
    drawing = parse_drawing({"datums": [{"label": "A", "bbox": ["0.1", "0.2", "0.3", "0.4"]}]})
    assert drawing.datums[0].bbox == [0.1, 0.2, 0.3, 0.4]


# --- coercion and defaults ------------------------------------------------------


def test_string_numbers_become_floats():
    drawing = parse_drawing(
        {
            "dimensions": [{"value": "12.5", "quantity": "2", "tol_plus": "0.05"}],
            "fcfs": [{"characteristic": "flatness", "tolerance": "0.1"}],
        }
    )
    dimension = drawing.dimensions[0]
    assert dimension.value == 12.5
    assert dimension.quantity == 2
    assert dimension.tol_plus == 0.05
    assert drawing.fcfs[0].tolerance == 0.1


def test_missing_ids_get_assigned():
    drawing = parse_drawing(
        {
            "dimensions": [{"value": 1}, {"id": "dim_keep", "value": 2}, {"value": 3}],
            "fcfs": [
                {"characteristic": "position", "tolerance": 0.1},
                {"characteristic": "flatness", "tolerance": 0.2},
            ],
        }
    )
    assert [d.id for d in drawing.dimensions] == ["D1", "dim_keep", "D3"]
    assert [f.id for f in drawing.fcfs] == ["F1", "F2"]


def test_datums_get_ids():
    drawing = parse_drawing(
        {"datums": [{"label": "A"}, {"id": "datum_keep", "label": "B"}, {"label": "C"}]}
    )
    assert [d.id for d in drawing.datums] == ["DT1", "datum_keep", "DT3"]


def test_duplicate_datum_labels_still_get_distinct_ids():
    """Two symbols both claiming A are separate items, so a finding can name them both."""
    drawing = parse_drawing({"datums": [{"label": "A"}, {"label": "A"}]})
    assert [d.id for d in drawing.datums] == ["DT1", "DT2"]


def test_extra_keys_ignored():
    drawing = parse_drawing(
        {
            "units": "mm",
            "drawing_id": "DC-0001",
            "page": {"width_pt": 792.0},
            "dimensions": [{"value": 1, "orientation": "horizontal", "measures": "width"}],
        }
    )
    assert drawing.units == Units.MM
    assert not hasattr(drawing, "drawing_id")
    assert drawing.dimensions[0].value == 1.0


def test_dimension_defaults():
    dimension = parse_drawing({"dimensions": [{"value": 5}]}).dimensions[0]
    assert dimension.quantity == 1
    assert dimension.is_basic is False
    assert dimension.is_reference is False
    assert dimension.unit is None
    assert dimension.from_ref is None
    assert dimension.to_ref is None


def test_from_ref_and_to_ref_kept():
    dimension = parse_drawing(
        {"dimensions": [{"value": 20, "from_ref": "left_edge", "to_ref": "hole_1_center"}]}
    ).dimensions[0]
    assert dimension.from_ref == "left_edge"
    assert dimension.to_ref == "hole_1_center"


def test_empty_drawing_parses():
    drawing = parse_drawing({})
    assert drawing.dimensions == []
    assert drawing.fcfs == []
    assert drawing.datums == []
    assert drawing.notes == []


# --- labels and modifiers -------------------------------------------------------


def test_datum_labels_uppercased_and_stripped():
    drawing = parse_drawing(
        {
            "datums": [{"label": " a "}],
            "fcfs": [
                {"characteristic": "position", "tolerance": 0.1, "datum_refs": [{"label": "b"}]}
            ],
        }
    )
    assert drawing.datums[0].label == "A"
    assert drawing.fcfs[0].datum_refs[0].label == "B"


@pytest.mark.parametrize(
    "raw, expected",
    [("MMC", "MMC"), ("mmc", "MMC"), ("LMC", "LMC"), ("RFS", None), ("junk", None)],
)
def test_material_modifiers(raw, expected):
    drawing = parse_drawing(
        {
            "fcfs": [
                {
                    "characteristic": "position",
                    "tolerance": 0.1,
                    "material_modifier": raw,
                    "datum_refs": [{"label": "A", "modifier": raw}],
                }
            ]
        }
    )
    assert drawing.fcfs[0].material_modifier == expected
    assert drawing.fcfs[0].datum_refs[0].modifier == expected


def test_negative_tolerance_becomes_none():
    dimension = parse_drawing(
        {"dimensions": [{"value": 10, "tol_plus": -0.1, "tol_minus": 0}]}
    ).dimensions[0]
    assert dimension.tol_plus is None
    assert dimension.tol_minus is None


# --- enums and findings ---------------------------------------------------------


def test_fourteen_characteristics():
    assert len(Characteristic) == 14


def test_form_controls():
    assert FORM_CONTROLS == {
        Characteristic.FLATNESS,
        Characteristic.STRAIGHTNESS,
        Characteristic.CIRCULARITY,
        Characteristic.CYLINDRICITY,
    }
    assert Characteristic.POSITION not in FORM_CONTROLS


# --- dropping items we cannot read ----------------------------------------------


def test_unknown_characteristic_drops_only_that_frame():
    drawing = parse_drawing(
        {
            "fcfs": [
                {"id": "keep_1", "characteristic": "position", "tolerance": 0.1},
                {"id": "keep_2", "characteristic": "flatness", "tolerance": 0.2},
                {"id": "bad", "characteristic": "wobbliness", "tolerance": 0.3},
            ]
        }
    )
    assert [f.id for f in drawing.fcfs] == ["keep_1", "keep_2"]
    assert drawing.parse_issues == ["fcfs[2]: unknown characteristic 'wobbliness'"]


def test_missing_value_drops_only_that_dimension():
    drawing = parse_drawing(
        {
            "dimensions": [
                {"id": "bad", "text_raw": "100 ±0.2"},
                {"id": "keep", "value": 60},
            ]
        }
    )
    assert [d.id for d in drawing.dimensions] == ["keep"]
    assert drawing.parse_issues == ["dimensions[0]: missing required field 'value'"]


def test_rest_of_the_drawing_survives_a_bad_item():
    """One unreadable callout must not cost us the other 95% of the extraction."""
    drawing = parse_drawing(
        {
            "units": "MILLIMETERS",
            "title_block": {"drawing_number": "DC-0001"},
            "datums": [{"label": "A"}, {"label": "B"}],
            "dimensions": [{"value": 100}, {"text_raw": "no value here"}, {"value": 60}],
            "fcfs": [{"characteristic": "wobbliness", "tolerance": 0.3}],
            "notes": ["FINISH ALL OVER"],
        }
    )
    assert drawing.units == Units.MM
    assert drawing.title_block.drawing_number == "DC-0001"
    assert [d.label for d in drawing.datums] == ["A", "B"]
    assert [d.value for d in drawing.dimensions] == [100.0, 60.0]
    assert drawing.fcfs == []
    assert drawing.notes == ["FINISH ALL OVER"]
    assert drawing.parse_issues == [
        "dimensions[1]: missing required field 'value'",
        "fcfs[0]: unknown characteristic 'wobbliness'",
    ]


def test_unreadable_datum_dropped():
    drawing = parse_drawing({"datums": [{"label": "A"}, {"bbox": [0.1, 0.2, 0.3, 0.4]}]})
    assert [d.label for d in drawing.datums] == ["A"]
    assert drawing.parse_issues == ["datums[1]: missing required field 'label'"]


def test_item_that_is_not_an_object_dropped():
    drawing = parse_drawing({"dimensions": [{"value": 1}, "100 ±0.2"]})
    assert len(drawing.dimensions) == 1
    assert len(drawing.parse_issues) == 1
    assert drawing.parse_issues[0].startswith("dimensions[1]: ")


def test_unparsable_value_names_the_field():
    drawing = parse_drawing({"dimensions": [{"value": "not a number"}]})
    assert drawing.dimensions == []
    assert drawing.parse_issues[0].startswith("dimensions[0]: invalid value: ")


def test_surviving_items_still_get_ids():
    """Ids number the items we kept, so every finding can point at a real one."""
    drawing = parse_drawing({"dimensions": [{"text_raw": "bad"}, {"value": 60}, {"value": 80}]})
    assert [d.id for d in drawing.dimensions] == ["D1", "D2"]


def test_parse_issues_empty_on_a_clean_parse():
    assert parse_drawing(load(FIXTURES / "plate_001.model.json")).parse_issues == []
    assert parse_drawing({}).parse_issues == []


def test_finding():
    finding = Finding(
        rule_id="GDT-001",
        severity="error",
        message="Datum C is referenced but never defined.",
        source="ASME Y14.5 datum reference framework",
        item_ids=["F1"],
        bbox=[0.07, 0.69, 0.25, 0.74],
    )
    assert finding.item_ids == ["F1"]
    assert finding.bbox == [0.07, 0.69, 0.25, 0.74]


def test_finding_bad_bbox_becomes_none():
    finding = Finding(rule_id="X", severity="warning", message="m", source="s", bbox=[9, 9, 9, 9])
    assert finding.bbox is None


def test_finding_severity_must_be_known():
    with pytest.raises(ValueError):
        Finding(rule_id="X", severity="catastrophe", message="m", source="s")


def test_parse_drawing_returns_drawing():
    assert isinstance(parse_drawing({}), Drawing)
