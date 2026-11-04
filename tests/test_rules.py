import json
from pathlib import Path

from draftcheck.rules import RULES, checks_summary, rule_results, run_rules
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


# --- rule results: one verdict per rule for the whole drawing -------------------------


def result_for(data_or_drawing, rule_id: str):
    """The one RuleResult for ``rule_id``, from a Drawing or a raw dict."""
    if isinstance(data_or_drawing, dict):
        data_or_drawing = parse_drawing(data_or_drawing)
    results = rule_results(data_or_drawing)
    return next(result for result in results if result.rule_id == rule_id)


def test_every_rule_reports_once_in_registry_order():
    results = rule_results(drawing())
    assert [r.rule_id for r in results] == [rule.id for rule in RULES]
    assert [r.title for r in results] == [rule.title for rule in RULES]


def test_status_is_fail_exactly_when_the_rule_has_findings():
    """Both a clean drawing and a messy one: fail <-> findings, never anything else."""
    messy = drawing(
        units=None,
        general_tolerance=None,
        datums=[{"label": "A"}, {"label": "A"}],
        dimensions=[{"id": "dim_1", "value": 50}],
        fcfs=[
            {
                "id": "fcf_1",
                "characteristic": "flatness",
                "tolerance": 0.05,
                "datum_refs": [{"label": "Z"}],
            }
        ],
    )
    for sample in (drawing(), messy):
        findings = run_rules(sample)
        for result in rule_results(sample, findings):
            has_findings = any(f.rule_id == result.rule_id for f in findings)
            assert (result.status == "fail") == has_findings, result


def test_failed_results_point_at_their_issue_numbers():
    """Issue numbers are positions in run_rules order — the same as the cards and markers."""
    messy = drawing(
        units=None,
        datums=[{"label": "A"}, {"label": "A"}],
        fcfs=[
            {"id": "fcf_1", "characteristic": "position", "tolerance": 0.2,
             "datum_refs": [{"label": "Y"}]},
        ],
    )
    findings = run_rules(messy)
    results = {r.rule_id: r for r in rule_results(messy, findings)}

    # run_rules order: DIM-001 (units), GDT-001, GDT-002.
    assert [f.rule_id for f in findings] == ["DIM-001", "GDT-001", "GDT-002"]
    assert results["DIM-001"].issues == [1]
    assert results["GDT-001"].issues == [2]
    assert results["GDT-002"].issues == [3]
    assert results["GDT-002"].explanation.endswith("(see issue 3).")
    assert results["GDT-003"].issues == []


def test_several_findings_are_all_pointed_at():
    failing = drawing(
        datums=[],
        fcfs=[
            {"id": "fcf_1", "characteristic": "position", "tolerance": 0.2,
             "datum_refs": [{"label": "A"}, {"label": "B"}]},
        ],
    )
    result = result_for(failing, "GDT-001")
    assert result.issues == [1, 2]
    assert result.explanation.endswith("(see issues 1, 2).")


def test_checks_summary_counts_each_status():
    assert checks_summary(rule_results(parse_drawing(plate()))) == "6 checks: 5 passed, 1 failed"
    bare = parse_drawing({"units": "mm", "general_tolerance": {"linear_plus_minus": 0.1}})
    # Nothing but units and a tolerance: only DIM-001 has anything to look at.
    assert checks_summary(rule_results(bare)) == (
        "6 checks: 1 passed, 0 failed, 5 not applicable"
    )


# GDT-001


def test_gdt_001_result_pass_names_the_referenced_datums():
    data = plate()
    data["datums"].append({"label": "C"})
    result = result_for(data, "GDT-001")
    assert result.status == "pass"
    assert result.explanation == "Every datum the frames reference is defined: A, B, C."


def test_gdt_001_result_fail_names_the_missing_datum():
    result = result_for(plate(), "GDT-001")
    assert result.status == "fail"
    assert result.explanation == (
        "The frames reference A, B, C, but nothing defines C (see issue 1)."
    )


def test_gdt_001_result_not_applicable_without_frames():
    result = result_for(drawing(fcfs=[]), "GDT-001")
    assert result.status == "not_applicable"
    assert result.explanation == "No feature control frames on this drawing."


def test_gdt_001_result_not_applicable_when_no_frame_names_a_datum():
    flat = [{"id": "fcf_1", "characteristic": "flatness", "tolerance": 0.05}]
    result = result_for(drawing(fcfs=flat), "GDT-001")
    assert result.status == "not_applicable"
    assert "references a datum" in result.explanation


# GDT-002


def test_gdt_002_result_pass_names_each_label():
    result = result_for(plate(), "GDT-002")
    assert result.status == "pass"
    assert result.explanation == "Each datum label is used once: A, B."


def test_gdt_002_result_fail_names_the_label_and_count():
    result = result_for(drawing(datums=[{"label": "A"}, {"label": "B"}, {"label": "B"},
                                        {"label": "B"}]), "GDT-002")
    assert result.status == "fail"
    assert result.explanation == (
        "More than one symbol claims the same label: B (3 times) (see issue 1)."
    )


def test_gdt_002_result_not_applicable_without_datums():
    result = result_for(drawing(datums=[], fcfs=[]), "GDT-002")
    assert result.status == "not_applicable"
    assert result.explanation == "No datum feature symbols on this drawing."


# GDT-003


def test_gdt_003_result_pass_with_no_form_controls():
    result = result_for(plate(), "GDT-003")
    assert result.status == "pass"
    assert result.explanation == (
        "No form control (flatness, straightness, circularity, cylindricity) "
        "references a datum."
    )


def test_gdt_003_result_pass_names_the_form_controls_it_checked():
    fcfs = [{"id": "fcf_1", "characteristic": "flatness", "tolerance": 0.05}]
    result = result_for(drawing(fcfs=fcfs), "GDT-003")
    assert result.status == "pass"
    assert result.explanation.endswith("references a datum (checked: flatness).")


def test_gdt_003_result_fail_names_the_control_and_its_datum():
    fcfs = [{"id": "fcf_1", "characteristic": "cylindricity", "tolerance": 0.05,
             "datum_refs": [{"label": "A"}]}]
    result = result_for(drawing(fcfs=fcfs), "GDT-003")
    assert result.status == "fail"
    assert result.explanation == (
        "A form control references a datum: cylindricity to A (see issue 1)."
    )


def test_gdt_003_result_not_applicable_without_frames():
    result = result_for(drawing(fcfs=[]), "GDT-003")
    assert result.status == "not_applicable"
    assert result.explanation == "No feature control frames on this drawing."


# GDT-004


def test_gdt_004_result_pass_counts_positions_and_basics():
    result = result_for(plate(), "GDT-004")
    assert result.status == "pass"
    assert result.explanation == "The drawing has 1 position tolerance and 3 basic dimensions."


def test_gdt_004_result_fail_counts_the_positions():
    fcfs = [
        {"id": f"fcf_{n}", "characteristic": "position", "tolerance": 0.2,
         "datum_refs": [{"label": "A"}]}
        for n in (1, 2)
    ]
    result = result_for(drawing(fcfs=fcfs, dimensions=[{"id": "d", "value": 5,
                                                        "tol_plus": 0.1, "tol_minus": 0.1}]),
                        "GDT-004")
    assert result.status == "fail"
    assert result.explanation == (
        "The drawing has 2 position tolerances but no basic dimensions to locate them "
        "(see issue 1)."
    )


def test_gdt_004_result_not_applicable_without_a_position():
    fcfs = [{"id": "fcf_1", "characteristic": "flatness", "tolerance": 0.05}]
    result = result_for(drawing(fcfs=fcfs), "GDT-004")
    assert result.status == "not_applicable"
    assert result.explanation == "No position tolerances on this drawing."


# DIM-001


def test_dim_001_result_pass_names_units_and_tolerance():
    result = result_for(plate(), "DIM-001")
    assert result.status == "pass"
    assert result.explanation == "Units (mm) and a general tolerance (±0.1) are both stated."


def test_dim_001_result_pass_without_general_tolerance_counts_own_tolerances():
    sample = drawing(
        units="in",
        general_tolerance=None,
        dimensions=[
            {"id": "d1", "value": 1, "is_basic": True},
            {"id": "d2", "value": 2, "tol_plus": 0.01, "tol_minus": 0.01},
            {"id": "d3", "value": 3, "tol_plus": 0.02},
        ],
    )
    result = result_for(sample, "DIM-001")
    assert result.status == "pass"
    assert result.explanation == (
        "Units (in) are stated; there is no general tolerance, but all 2 inspected "
        "dimensions carry their own."
    )


def test_dim_001_result_fail_names_both_problems_and_the_dimensions():
    sample = drawing(
        units=None,
        general_tolerance=None,
        dimensions=[{"id": "d1", "value": 1}, {"id": "d2", "value": 2}],
        fcfs=[],
    )
    result = result_for(sample, "DIM-001")
    assert result.status == "fail"
    assert result.explanation == (
        "No units are stated; there is no general tolerance and 2 dimensions carry none "
        "of their own (d1, d2) (see issues 1, 2)."
    )


# MFG-001


def test_mfg_001_result_pass_names_the_closest_hole_and_edge():
    result = result_for(plate(), "MFG-001")
    assert result.status == "pass"
    assert result.explanation == (
        "Every hole keeps at least 8 mm of material to the edge "
        "(closest: 16 mm, hole 1 to the left edge)."
    )


def test_mfg_001_result_pass_follows_the_real_closest_distance():
    data = plate()
    dimension_named(data, "80")["value"] = 85  # hole 2 now 11 from the right edge
    result = result_for(data, "MFG-001")
    assert result.status == "pass"
    assert "(closest: 11 mm, hole 2 to the right edge)" in result.explanation


def test_mfg_001_result_fail_names_hole_edge_and_minimum():
    data = plate()
    dimension_named(data, "20")["value"] = 5
    result = result_for(data, "MFG-001")
    assert result.status == "fail"
    assert result.explanation == (
        "Hole 1 leaves 1 mm to the left edge, under the 8 mm minimum (see issue 2)."
    )


def test_mfg_001_result_not_applicable_without_a_diameter():
    data = plate()
    data["dimensions"] = [d for d in data["dimensions"] if d.get("feature_type") != "diameter"]
    result = result_for(data, "MFG-001")
    assert result.status == "not_applicable"
    assert "No hole diameter" in result.explanation


def test_mfg_001_result_not_applicable_when_no_hole_is_located():
    sample = drawing(dimensions=[{"id": "d1", "value": 8, "feature_type": "diameter",
                                  "tol_plus": 0.1, "tol_minus": 0.1}])
    result = result_for(sample, "MFG-001")
    assert result.status == "not_applicable"
    assert "No dimension locates a hole from an edge" in result.explanation


# --- numbering a corrected drawing against the submitted findings ---------------------


def test_numbering_keeps_a_surviving_findings_original_number():
    """MFG-001 was issue 2 on the submitted plate; after GDT-001 is fixed it is still 2."""
    data = plate()
    dimension_named(data, "20")["value"] = 5
    submitted = parse_drawing(data)
    data["datums"].append({"label": "C"})
    corrected = parse_drawing(data)

    results = {r.rule_id: r for r in rule_results(corrected, numbering=run_rules(submitted))}
    assert results["GDT-001"].status == "pass"
    assert results["MFG-001"].issues == [2]
    assert results["MFG-001"].explanation.endswith("(see issue 2).")


def test_numbering_calls_a_finding_new_when_the_submitted_drawing_lacked_it():
    submitted = parse_drawing(plate())
    data = plate()
    dimension_named(data, "20")["value"] = 5  # a fix that broke the edge distance
    data["datums"].append({"label": "C"})
    corrected = parse_drawing(data)

    mfg = next(
        r for r in rule_results(corrected, numbering=run_rules(submitted))
        if r.rule_id == "MFG-001"
    )
    assert mfg.status == "fail"
    assert mfg.issues == []
    assert mfg.explanation.endswith(
        "under the 8 mm minimum (1 new finding, not on the submitted drawing)."
    )


def test_numbering_mixes_old_and_new_findings():
    submitted = drawing(
        datums=[],
        fcfs=[{"id": "fcf_1", "characteristic": "position", "tolerance": 0.2,
               "datum_refs": [{"label": "A"}]}],
    )
    corrected = drawing(
        datums=[],
        fcfs=[
            {"id": "fcf_1", "characteristic": "position", "tolerance": 0.2,
             "datum_refs": [{"label": "A"}]},
            {"id": "fcf_2", "characteristic": "position", "tolerance": 0.2,
             "datum_refs": [{"label": "B"}]},
        ],
    )
    gdt = rule_results(corrected, numbering=run_rules(submitted))[0]
    assert gdt.issues == [1]
    assert gdt.explanation.endswith("(see issue 1, plus 1 new finding).")
