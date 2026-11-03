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
    assert 'getElementById("confidence-summary")' in read_app_js()


def test_repo_has_the_demo_files() -> None:
    """The ?demo=low-confidence route reads these two; they are in the repository."""
    repo_root = Path(__file__).resolve().parent.parent
    assert (repo_root / "tests" / "fixtures" / "plate_001.model.json").is_file()
    assert (repo_root / "samples" / "generated" / "plate_001.png").is_file()


def read_index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("button_id", "waiting_line"),
    [("cancel-read", 'id="status"'), ("cancel-fix", 'id="fix-status"')],
)
def test_each_waiting_line_has_a_cancel_button(button_id: str, waiting_line: str) -> None:
    """The Cancel button sits inside the waiting message it cancels, and app.js wires it."""
    index = read_index()
    start = index.index(waiting_line)
    line = index[start : index.index("</p>", start)]
    assert f'<button id="{button_id}"' in line
    assert ">Cancel</button>" in line
    assert f'getElementById("{button_id}")' in read_app_js()


def test_cancel_aborts_the_request_and_says_so() -> None:
    source = read_app_js()
    assert source.count("new AbortController()") == 2
    assert source.count("signal })") == 2  # both fetches carry the signal
    assert (
        '"Cancelled. Muse Spark may still be finishing the previous request, '
        'so your next read can take up to a minute."'
    ) in source
    for note in ("read-cancelled", "fix-cancelled"):
        assert f'id="{note}"' in read_index()
