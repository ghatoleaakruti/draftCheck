"""Assertions about the browser page that Python can make without a browser.

There is no JavaScript test runner in this project, and adding one for a handful of
facts would cost more than it is worth. What can still be checked from here is that a
number the page's behaviour depends on is written down once, in the place the code and
the docs both say it is — which is exactly the thing that rots when it is not checked.
"""

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
    assert source.count("new AbortController()") == 2
    assert source.count("signal })") == 2  # both model fetches carry the signal
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
    stylesheet = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert f"/fonts/{folder}/{font}" in stylesheet


def test_the_page_loads_nothing_from_another_site() -> None:
    for name in ("index.html", "style.css", "app.js"):
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
