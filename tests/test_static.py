"""Assertions about the browser page that Python can make without a browser.

There is no JavaScript test runner in this project, and adding one for a handful of
facts would cost more than it is worth. What can still be checked from here is that a
number the page's behaviour depends on is written down once, in the place the code and
the docs both say it is — which is exactly the thing that rots when it is not checked.
"""

import re
from pathlib import Path

import pytest

from draftcheck.api import STATIC_DIR

APP_JS = STATIC_DIR / "app.js"


def read_app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_static_dir_holds_the_page() -> None:
    assert APP_JS.is_file()
    assert (STATIC_DIR / "index.html").is_file()


def test_confidence_threshold_is_declared_once() -> None:
    """The 0.8 cutoff is a named constant, and there is only one of it."""
    source = read_app_js()
    assert "const CONFIDENCE_THRESHOLD = 0.8;" in source
    # One declaration and one assignment: a second would be a second source of truth.
    assert source.count("CONFIDENCE_THRESHOLD =") == 1


def test_confidence_threshold_is_used_by_name() -> None:
    """The comparison reads the constant rather than repeating the number."""
    source = read_app_js()
    assert "value < CONFIDENCE_THRESHOLD" in source


def test_the_page_has_somewhere_to_put_the_confidence_summary() -> None:
    """app.js writes into `#confidence-summary`; index.html has to supply it."""
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="confidence-summary"' in index
    assert '$("confidence-summary")' in read_app_js()


def test_repo_has_the_demo_files() -> None:
    """The ?demo=low-confidence route reads these two; they are in the repository."""
    repo_root = Path(__file__).resolve().parent.parent
    assert (repo_root / "tests" / "fixtures" / "plate_001.model.json").is_file()
    assert (repo_root / "samples" / "generated" / "plate_001.png").is_file()


def read_index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_one_cancel_button_sits_beside_the_primary_action() -> None:
    """Reading and fixing share one Cancel button in the action bar, and app.js wires it."""
    index = read_index()
    start = index.index('class="actionbar"')
    bar = index[start : index.index("</div>\n        </section>", start)]
    assert '<button id="cancel"' in bar
    assert '<button id="primary"' in bar
    assert ">Cancel</button>" in bar
    assert '$("cancel")' in read_app_js()


def test_cancel_aborts_the_request_and_says_so() -> None:
    source = read_app_js()
    # Reading, fixing, and explaining: each model request can be cancelled.
    assert source.count("new AbortController()") == 3
    assert source.count("signal })") == 3  # every model fetch carries the signal
    assert (
        '"Cancelled. Muse Spark may still be finishing the previous request, '
        'so your next read can take up to a minute."'
    ) in source


@pytest.mark.parametrize(
    "label",
    [
        "Read drawing",
        "Confirm and run checks",
        "Let the agent fix them",
        "Download redlined PDF",
    ],
)
def test_primary_button_labels_follow_the_stage(label: str) -> None:
    """Each stage's label is written in app.js, where `primaryFor` chooses between them."""
    assert f'label: "{label}"' in read_app_js()


@pytest.mark.parametrize("step", ["Read", "Confirm", "Check", "Fix", "Export"])
def test_stepper_has_the_five_stages(step: str) -> None:
    assert f'<span class="step-name">{step}</span>' in read_index()


FONTS = STATIC_DIR / "fonts"


@pytest.mark.parametrize(
    ("folder", "font"),
    [
        ("archivo", "Archivo-Variable.ttf"),
        ("jetbrains-mono", "JetBrainsMono-Variable.ttf"),
        ("nanum-pen-script", "NanumPenScript-Regular.ttf"),
    ],
)
def test_each_bundled_font_ships_with_its_licence(folder: str, font: str) -> None:
    """Fonts are bundled, not loaded from a CDN, and each sits beside its OFL text."""
    assert (FONTS / folder / font).is_file()
    licence = (FONTS / folder / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL Open Font License" in licence
    # The pen font is loaded by sheet.js when it is first needed; the other two by CSS.
    loader = "sheet.js" if folder == "nanum-pen-script" else "style.css"
    assert f"/fonts/{folder}/{font}" in (STATIC_DIR / loader).read_text(encoding="utf-8")


def test_the_pen_font_waits_until_a_pen_note_is_drawn() -> None:
    """3 MB of handwriting is not fetched on first load: no @font-face, no preload."""
    stylesheet = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*\{[^}]*\}", stylesheet)
    assert len(faces) == 2
    assert not any("Nanum" in face for face in faces)
    assert "preload" not in read_index()
    sheet = (STATIC_DIR / "sheet.js").read_text(encoding="utf-8")
    assert 'display: "swap"' in sheet
    # Asked for from the code that writes the first note, and nowhere else.
    assert sheet.count("new FontFace(") == 1
    assert "Sheet.loadPenFont()" in read_app_js()


def test_the_page_loads_nothing_from_another_site() -> None:
    for name in ("index.html", "style.css", "app.js", "sheet.js"):
        source = (STATIC_DIR / name).read_text(encoding="utf-8")
        assert "https://" not in source.replace("http://www.w3.org/2000/svg", ""), name


def test_the_fake_model_badge_is_on_the_page_and_off_by_default() -> None:
    """The pill carries a badge for the dev fake; it starts hidden and app.js drives it."""
    index = read_index()
    assert '<span id="fake-badge" class="fake-badge" hidden>Fake model (dev)</span>' in index
    source = read_app_js()
    assert "fakeBadge.hidden = !state.fake;" in source
    assert "run.fake_model" in source


def test_toasts_last_five_seconds_and_are_not_repeated() -> None:
    source = read_app_js()
    assert "const TOAST_MS = 5000;" in source
    # One timer for every toast, errors included, and a repeat restarts it instead.
    assert source.count("setTimeout(() => item.remove(), TOAST_MS)") == 1
    assert "item.dataset.message === message" in source
    # Lifted clear of both bottom bars rather than parked over them.
    assert "for (const bar of [actionBar, statusBar])" in source


def test_the_issues_tab_has_a_checks_list_below_the_rounds_strip() -> None:
    """index.html supplies the Checks box; it sits after the rounds strip and before cards."""
    index = read_index()
    for element_id in ("checks", "checks-summary", "checks-list"):
        assert f'id="{element_id}"' in index
        assert f'$("{element_id}")' in read_app_js()
    assert index.index('id="rounds-strip"') < index.index('id="checks"')
    assert index.index('id="checks"') < index.index('id="findings-list"')


def test_checks_use_the_servers_results_and_icons() -> None:
    """The page lays out rule_results and checks_summary; it decides no status itself."""
    source = read_app_js()
    assert "result.rule_results" in source
    assert "result.checks_summary" in source
    for icon in ('icon: "✓"', 'icon: "✗"', 'icon: "–"'):
        assert icon in source
    # A failed check opens its first issue, which selects the card and the cloud.
    assert "openIssue(first)" in source


def test_check_icons_have_pass_fail_and_na_colours() -> None:
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert ".check.is-pass .check-icon" in css
    assert ".check.is-fail .check-icon" in css
    assert ".check.is-na .check-icon" in css


def test_checks_can_switch_between_submitted_and_after_fixes() -> None:
    index = read_index()
    source = read_app_js()
    for element_id in ("checks-toggle", "checks-summary-after"):
        assert f'id="{element_id}"' in index
        assert f'$("{element_id}")' in source
    assert 'data-view="after"' in index and 'data-view="submitted"' in index
    assert 'const SUBMITTED_LEAD = "On the submitted drawing";' in source
    assert "const AFTER_FIXES_LEAD = \"After the agent's verified fixes\";" in source
    # The fix reply's re-run results are what the after view shows, by default.
    assert "result.final_rule_results" in source
    assert "result.final_checks_summary" in source
    assert 'checks.view = "after";' in source


def test_page_and_redline_use_the_same_lead_ins() -> None:
    from draftcheck.redline import AFTER_FIXES_LEAD, SUBMITTED_LEAD

    source = read_app_js()
    assert f'"{SUBMITTED_LEAD}"' in source
    assert f'"{AFTER_FIXES_LEAD}"' in source


@pytest.mark.parametrize("name", ["app.js", "sheet.js"])
def test_page_scripts_parse(name: str) -> None:
    """A syntax error stops the whole page; `node --check` catches it without a browser."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "--check", str(STATIC_DIR / name)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


# ---- Two-model cross-check -------------------------------------------------------------


def test_the_cross_check_checkbox_sits_beside_the_model_selector_and_is_on() -> None:
    index = read_index()
    selector = index.index('id="provider"')
    checkbox = index.index('id="cross-check"')
    actions_end = index.index("</div>", index.index('class="desk-actions"'))
    assert selector < checkbox, "the checkbox comes after the model selector"
    tag = index[index.rfind("<input", 0, checkbox) : index.index("/>", checkbox)]
    assert "checked" in tag and 'type="checkbox"' in tag
    assert "Cross-check with a second Meta model" in index
    # Its one-line description says what it does and what it costs.
    note = index[index.index('id="cross-check-note"') :]
    note = note[: note.index("</p>")]
    assert "extra model call" in note
    assert checkbox < actions_end, "the checkbox is in the desk bar's actions"


def test_the_page_sends_the_checkbox_with_the_upload() -> None:
    source = read_app_js()
    assert '$("cross-check")' in source
    assert 'body.append("cross_check", crossCheckBox.checked ? "1" : "0")' in source


def test_disagreements_get_their_own_marker_not_the_confidence_style() -> None:
    source = read_app_js()
    assert 'const DISAGREE_MARK = "⇄";' in source
    assert 'mark.className = "xcheck";' in source
    # The tooltip is the server's note: "Muse Spark Open read X; Muse Spark read Y."
    assert "mark.title = notes.join" in source
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    rule = css[css.index(".xcheck {") :]
    rule = rule[: rule.index("}")]
    assert "var(--xcheck)" in rule
    assert "amber" not in rule and "warn" not in rule


def test_rows_only_one_model_read_say_so() -> None:
    source = read_app_js()
    assert 'const ONE_SIDED_TEXT = "only one model read this";' in source
    assert "oneSidedMarker(id)" in source


def test_disagreeing_boxes_are_dashed_in_their_own_colour() -> None:
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    rule = css[css.index(".obox.is-disagree .obox-frame {") :]
    rule = rule[: rule.index("}")]
    assert "stroke-dasharray" in rule
    assert "var(--xcheck)" in rule
    assert 'box.classList.toggle("is-disagree", Boolean(item.disagree));' in read_app_js()


def test_the_summary_line_jumps_to_the_first_disagreement() -> None:
    index = read_index()
    assert 'id="crosscheck-summary"' in index
    source = read_app_js()
    assert '$("crosscheck-summary")' in source
    assert 'button.addEventListener("click", goToFirstDisagreement);' in source


# ---- AI explanations (step 25) -------------------------------------------------------

AI_LABEL = "AI explanation · not checked by the rules"


def test_the_extracted_tab_opens_with_the_about_card() -> None:
    """The card sits at the top of the Extracted content, before the run's metadata."""
    index = read_index()
    extracted = index.index('id="extracted"')
    assert index.index('id="about"') > extracted
    assert index.index('id="about"') < index.index('id="meta-model"')
    assert "About this drawing" in index
    assert 'id="explain-button"' in index
    assert ">Explain this drawing<" in index


def test_every_section_with_explanations_carries_the_label_once() -> None:
    """One label each: the card, the three tables, and the issue cards."""
    index = read_index()
    assert index.count(AI_LABEL) == 5
    for section in ("datums", "dimensions", "fcfs", "issues"):
        assert index.count(f'id="ai-label-{section}"') == 1
        assert f'$("ai-label-{section}")' in read_app_js()


def test_explanations_are_requested_and_drawn_as_text() -> None:
    source = read_app_js()
    assert "/explain`" in source
    assert '"Why it matters"' in source
    assert '"How to think about fixing"' in source
    # Model text is written as text, never parsed as HTML.
    body = source[source.index("/* ---- AI explanations for learners") :]
    body = body[: body.index("/* ---- Rendering one run")]
    assert "innerHTML" not in body


def test_cancel_also_stops_an_explanation() -> None:
    assert "if (explainController) explainController.abort();" in read_app_js()


def test_the_about_card_has_views_tables_and_balloons_rows() -> None:
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for element in ("about-views-row", "about-views", "about-projection-note",
                    "about-tables-row", "about-tables", "about-balloons-row"):
        assert f'id="{element}"' in page
    source = read_app_js()
    assert "function renderAboutRows(" in source
    assert "renderAboutRows(explanation);" in source


def test_dimension_notes_say_which_views_show_them() -> None:
    source = read_app_js()
    assert "${whereSeen(note)}" in source
    assert "also seen in ${joinWords(also)}" in source
    assert "` · in ${named(note.view)}${tail}`" in source


def test_every_part_type_has_a_chip_label() -> None:
    """The chip names each type the explanation can give, and nothing else."""
    from draftcheck.prompts import PART_TYPES

    source = read_app_js()
    block = source[source.index("const PART_TYPE_LABELS = {"):]
    block = block[: block.index("};")]
    labels = re.findall(r"^\s+(\w+): \"", block, flags=re.M)
    assert labels == list(PART_TYPES)
    assert "PART_TYPE_LABELS[part.type] || PART_TYPE_LABELS.other" in source
