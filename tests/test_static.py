"""Assertions about the browser page that Python can make without a browser.

There is no JavaScript test runner in this project, and adding one for a handful of
facts would cost more than it is worth. What can still be checked from here is that a
number the page's behaviour depends on is written down once, in the place the code and
the docs both say it is — which is exactly the thing that rots when it is not checked.
"""

from pathlib import Path

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
