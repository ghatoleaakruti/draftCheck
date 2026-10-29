import json
from pathlib import Path

from draftcheck.rules import RULES, run_rules
from draftcheck.schema import parse_drawing

FIXTURES = Path(__file__).parent / "fixtures"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def drawing(**overrides) -> object:
    """A small drawing that passes every rule, with the given fields replaced.

    Each rule's failing case starts from this and breaks exactly one thing, so a finding
    can only come from the change under test.
    """
    base = {
        "units": "mm",
        "general_tolerance": {"linear_plus_minus": 0.1, "text_raw": "±0.1"},
        "datums": [{"label": "A", "bbox": [0.1, 0.1, 0.2, 0.2]}],
        "dimensions": [
            {
                "id": "dim_1",
                "value": 50,
                "is_basic": True,
                "bbox": [0.3, 0.3, 0.4, 0.4],
            }
        ],
        "fcfs": [
            {
                "id": "fcf_1",
                "characteristic": "position",
                "tolerance": 0.25,
                "datum_refs": [{"label": "A"}],
                "bbox": [0.5, 0.5, 0.6, 0.6],
            }
        ],
    }
    base.update(overrides)
    return parse_drawing(base)


def rule_ids(findings) -> list[str]:
    return [finding.rule_id for finding in findings]


# --- the baseline itself --------------------------------------------------------------


def test_clean_drawing_has_no_findings():
    assert run_rules(drawing()) == []


# --- GDT-001 undefined datum ----------------------------------------------------------


def test_gdt_001_passes_when_every_datum_is_defined():
    passing = drawing(
        datums=[{"label": "A"}, {"label": "B"}],
        fcfs=[
            {
                "id": "fcf_1",
                "characteristic": "position",
                "tolerance": 0.25,
                "datum_refs": [{"label": "A"}, {"label": "B"}],
            }
        ],
    )
    assert "GDT-001" not in rule_ids(run_rules(passing))


def test_gdt_001_flags_a_datum_nothing_defines():
    failing = drawing(
        datums=[{"label": "A"}],
        fcfs=[
            {
                "id": "fcf_1",
                "characteristic": "position",
                "tolerance": 0.25,
                "datum_refs": [{"label": "A"}, {"label": "B"}],
                "bbox": [0.5, 0.5, 0.6, 0.6],
            }
        ],
    )
    findings = [f for f in run_rules(failing) if f.rule_id == "GDT-001"]

    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].item_ids == ["fcf_1"]
    assert findings[0].bbox == [0.5, 0.5, 0.6, 0.6]
    assert "B" in findings[0].message


def test_gdt_001_reports_each_missing_datum_once_per_frame():
    failing = drawing(
        datums=[],
        fcfs=[
            {
                "id": "fcf_1",
                "characteristic": "position",
                "tolerance": 0.25,
                "datum_refs": [{"label": "A"}, {"label": "A"}, {"label": "B"}],
            }
        ],
    )
    findings = [f for f in run_rules(failing) if f.rule_id == "GDT-001"]
    assert len(findings) == 2


# --- GDT-002 duplicate datum ----------------------------------------------------------


def test_gdt_002_passes_when_labels_are_unique():
    passing = drawing(datums=[{"label": "A"}, {"label": "B"}])
    assert "GDT-002" not in rule_ids(run_rules(passing))


def test_gdt_002_flags_a_label_defined_twice():
    failing = drawing(
        datums=[
            {"label": "A", "bbox": [0.1, 0.1, 0.2, 0.2]},
            {"label": "B"},
            {"label": "a", "bbox": [0.7, 0.7, 0.8, 0.8]},
        ]
    )
    findings = [f for f in run_rules(failing) if f.rule_id == "GDT-002"]

    assert len(findings) == 1
    assert findings[0].item_ids == ["DT1", "DT3"]
    assert findings[0].bbox == [0.1, 0.1, 0.2, 0.2]
    assert "A" in findings[0].message


# --- GDT-003 form control with datum --------------------------------------------------


def test_gdt_003_passes_when_a_form_control_has_no_datums():
    passing = drawing(
        fcfs=[{"id": "fcf_1", "characteristic": "flatness", "tolerance": 0.05}],
        dimensions=[{"id": "dim_1", "value": 50, "tol_plus": 0.1, "tol_minus": 0.1}],
    )
    assert "GDT-003" not in rule_ids(run_rules(passing))


def test_gdt_003_passes_when_an_orientation_control_has_datums():
    """Perpendicularity is not a form control, so a datum on it is correct."""
    passing = drawing(
        fcfs=[
            {
                "id": "fcf_1",
                "characteristic": "perpendicularity",
                "tolerance": 0.05,
                "datum_refs": [{"label": "A"}],
            }
        ]
    )
    assert "GDT-003" not in rule_ids(run_rules(passing))


def test_gdt_003_flags_a_form_control_with_a_datum():
    failing = drawing(
        fcfs=[
            {
                "id": "fcf_1",
                "characteristic": "flatness",
                "tolerance": 0.05,
                "datum_refs": [{"label": "A"}],
                "bbox": [0.5, 0.5, 0.6, 0.6],
            }
        ]
    )
    findings = [f for f in run_rules(failing) if f.rule_id == "GDT-003"]

    assert len(findings) == 1
    assert findings[0].item_ids == ["fcf_1"]
    assert findings[0].bbox == [0.5, 0.5, 0.6, 0.6]
    assert "flatness" in findings[0].message


# --- GDT-004 position without basic dimensions ----------------------------------------


def test_gdt_004_passes_when_a_basic_dimension_exists():
    passing = drawing(dimensions=[{"id": "dim_1", "value": 50, "is_basic": True}])
    assert "GDT-004" not in rule_ids(run_rules(passing))


def test_gdt_004_passes_without_a_position_tolerance():
    """No position frame, so basic dimensions are not required."""
    passing = drawing(
        fcfs=[{"id": "fcf_1", "characteristic": "flatness", "tolerance": 0.05}],
        dimensions=[{"id": "dim_1", "value": 50, "tol_plus": 0.1, "tol_minus": 0.1}],
    )
    assert "GDT-004" not in rule_ids(run_rules(passing))


def test_gdt_004_flags_a_position_with_no_basic_dimensions():
    failing = drawing(
        dimensions=[{"id": "dim_1", "value": 50, "tol_plus": 0.1, "tol_minus": 0.1}],
        fcfs=[
            {
                "id": "fcf_1",
                "characteristic": "position",
                "tolerance": 0.25,
                "datum_refs": [{"label": "A"}],
                "bbox": [0.5, 0.5, 0.6, 0.6],
            }
        ],
    )
    findings = [f for f in run_rules(failing) if f.rule_id == "GDT-004"]

    assert len(findings) == 1
    assert findings[0].item_ids == ["fcf_1"]
    assert findings[0].bbox == [0.5, 0.5, 0.6, 0.6]


# --- DIM-001 missing units or general tolerance ---------------------------------------


def test_dim_001_passes_with_units_and_a_general_tolerance():
    assert "DIM-001" not in rule_ids(run_rules(drawing()))


def test_dim_001_passes_without_a_general_tolerance_when_every_dimension_has_one():
    passing = drawing(
        general_tolerance=None,
        dimensions=[
            {"id": "dim_1", "value": 50, "tol_plus": 0.1, "tol_minus": 0.1},
            {"id": "dim_2", "value": 20, "is_basic": True},
            {"id": "dim_3", "value": 10, "is_reference": True},
        ],
    )
    assert "DIM-001" not in rule_ids(run_rules(passing))


def test_dim_001_flags_missing_units():
    failing = drawing(units="furlongs")
    findings = [f for f in run_rules(failing) if f.rule_id == "DIM-001"]

    assert len(findings) == 1
    assert findings[0].item_ids == []
    assert findings[0].bbox is None


def test_dim_001_flags_an_untoleranced_dimension_with_no_general_tolerance():
    failing = drawing(
        general_tolerance={"text_raw": "SEE NOTES"},
        dimensions=[
            {"id": "dim_1", "value": 50, "bbox": [0.3, 0.3, 0.4, 0.4]},
            {"id": "dim_2", "value": 20, "is_basic": True},
        ],
    )
    findings = [f for f in run_rules(failing) if f.rule_id == "DIM-001"]

    assert len(findings) == 1
    assert findings[0].item_ids == ["dim_1"]
    assert findings[0].bbox == [0.3, 0.3, 0.4, 0.4]


def test_dim_001_can_report_both_problems_at_once():
    failing = drawing(
        units=None,
        general_tolerance=None,
        dimensions=[{"id": "dim_1", "value": 50}],
    )
    findings = [f for f in run_rules(failing) if f.rule_id == "DIM-001"]
    assert len(findings) == 2


# --- MFG-001 hole too close to edge ---------------------------------------------------


def plate() -> dict:
    """The real plate_001 extraction, as a plain dict a test can edit before parsing."""
    return load(FIXTURES / "plate_001.model.json")


def dimension_named(data: dict, text: str) -> dict:
    """The dimension entry whose printed text is ``text``."""
    return next(d for d in data["dimensions"] if d["text_raw"] == text)


def test_mfg_001_passes_on_the_real_plate():
    """Holes are 20 and 80 across a 100 plate, Ø8: 16 of material each side, minimum 8."""
    assert "MFG-001" not in rule_ids(run_rules(parse_drawing(plate())))


def test_mfg_001_flags_a_hole_too_close_to_the_left_edge():
    data = plate()
    dimension_named(data, "20")["value"] = 5
    findings = [f for f in run_rules(parse_drawing(data)) if f.rule_id == "MFG-001"]

    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert findings[0].item_ids == ["dim_2"]
    assert "left edge" in findings[0].message
    # 5 to the centre of a Ø8 hole leaves 1 of material, against a minimum of 8.
    assert "1 of material" in findings[0].message
    assert "8 minimum" in findings[0].message


def test_mfg_001_derives_the_distance_to_the_facing_edge():
    """Only the left distance is printed; the right one is the overall width minus it."""
    data = plate()
    dimension_named(data, "20")["value"] = 97
    findings = [f for f in run_rules(parse_drawing(data)) if f.rule_id == "MFG-001"]

    assert len(findings) == 1
    # Derived from the hole dimension and the overall width, so both are named.
    assert findings[0].item_ids == ["dim_2", "dim_4"]
    assert "right edge" in findings[0].message


def test_mfg_001_prefers_a_printed_distance_over_a_derived_one():
    """Both edges are dimensioned and both fail, so each gets its own finding."""
    data = plate()
    dimension_named(data, "20")["value"] = 5
    dimension_named(data, "80")["from_ref"] = "right_edge"
    dimension_named(data, "80")["to_ref"] = "hole_1_center"
    dimension_named(data, "80")["value"] = 6
    findings = [f for f in run_rules(parse_drawing(data)) if f.rule_id == "MFG-001"]

    assert len(findings) == 2
    assert [f.item_ids for f in findings] == [["dim_1"], ["dim_2"]]


def test_mfg_001_is_silent_without_a_hole_diameter():
    data = plate()
    dimension_named(data, "20")["value"] = 5
    data["dimensions"] = [d for d in data["dimensions"] if d["feature_type"] != "diameter"]
    assert "MFG-001" not in rule_ids(run_rules(parse_drawing(data)))


def test_mfg_001_uses_the_largest_diameter_when_several_are_given():
    """With no way to match a diameter to a hole, the biggest one is the safe read."""
    data = plate()
    dimension_named(data, "20")["value"] = 14
    passing = [f for f in run_rules(parse_drawing(data)) if f.rule_id == "MFG-001"]
    assert passing == []  # 14 - 4 = 10 of material, against a minimum of 8

    data["dimensions"].append(
        {"id": "dim_big", "text_raw": "Ø12", "value": 12, "feature_type": "diameter"}
    )
    findings = [f for f in run_rules(parse_drawing(data)) if f.rule_id == "MFG-001"]
    assert [f.item_ids for f in findings] == [["dim_2"]]  # 14 - 6 = 8, minimum now 12


def test_mfg_001_ignores_dimensions_with_no_refs():
    data = plate()
    for entry in data["dimensions"]:
        entry["from_ref"] = None
        entry["to_ref"] = None
    assert "MFG-001" not in rule_ids(run_rules(parse_drawing(data)))


# --- end to end on the real extraction ------------------------------------------------


def test_real_extraction_yields_only_the_seeded_undefined_datum():
    """plate_001 was drawn with one seeded error: the frame cites datum C, undefined."""
    model_output = parse_drawing(load(FIXTURES / "plate_001.model.json"))
    findings = run_rules(model_output)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "GDT-001"
    assert finding.severity == "error"
    assert "C" in finding.message
    assert finding.item_ids == ["fcf_1"]
    assert finding.bbox == [0.073, 0.694, 0.249, 0.741]


# --- the registry and ordering --------------------------------------------------------


def test_registry_ids_are_unique_and_well_formed():
    ids = [rule.id for rule in RULES]
    assert len(ids) == len(set(ids))
    for rule in RULES:
        assert rule.category in {"gdt", "dimensioning", "manufacturability"}
        assert rule.severity in {"error", "warning"}
        assert rule.source


def test_output_order_is_stable_and_sorted():
    messy = drawing(
        units=None,
        general_tolerance=None,
        datums=[{"label": "A"}, {"label": "A"}],
        dimensions=[{"id": "dim_1", "value": 50}],
        fcfs=[
            {
                "id": "fcf_2",
                "characteristic": "flatness",
                "tolerance": 0.05,
                "datum_refs": [{"label": "Z"}],
            },
            {
                "id": "fcf_1",
                "characteristic": "position",
                "tolerance": 0.25,
                "datum_refs": [{"label": "Y"}],
            },
        ],
    )

    findings = run_rules(messy)
    keys = [(finding.rule_id, finding.item_ids) for finding in findings]

    assert keys == sorted(keys)
    assert rule_ids(findings) == [
        "DIM-001",
        "DIM-001",
        "GDT-001",
        "GDT-001",
        "GDT-002",
        "GDT-003",
        "GDT-004",
    ]
    # Same input, same output, every time.
    assert [f.model_dump() for f in run_rules(messy)] == [f.model_dump() for f in findings]
