"""Tests for rendering drawings to page images. No network."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from draftcheck.render import PageImage, RenderError, load_pages
from scripts.make_sample_drawing import build

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(scope="module")
def sample_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A generated drawing to render in these tests."""
    return build(tmp_path_factory.mktemp("render"), write_png=False)["pdf"]


def open_page(page: PageImage) -> Image.Image:
    """Decode a page's PNG bytes back into a pillow image."""
    return Image.open(io.BytesIO(page.png_bytes))


def test_pdf_renders_to_one_page(sample_pdf: Path) -> None:
    pages = load_pages(sample_pdf)
    assert len(pages) == 1
    assert pages[0].png_bytes.startswith(PNG_MAGIC)


def test_reported_size_matches_the_image(sample_pdf: Path) -> None:
    page = load_pages(sample_pdf)[0]
    with open_page(page) as image:
        assert (image.width, image.height) == (page.width, page.height)


def test_landscape_page_is_wider_than_tall(sample_pdf: Path) -> None:
    page = load_pages(sample_pdf)[0]
    assert page.width > page.height


def test_default_dpi_gives_expected_pixel_size(sample_pdf: Path) -> None:
    """792 x 612 pt at 200 dpi is about 2200 x 1700 px, under the 2400 cap."""
    page = load_pages(sample_pdf, dpi=200, max_side=2400)
    assert page[0].width == pytest.approx(2200, abs=5)
    assert page[0].height == pytest.approx(1700, abs=5)


def test_higher_dpi_gives_a_bigger_image(sample_pdf: Path) -> None:
    small = load_pages(sample_pdf, dpi=72, max_side=10_000)[0]
    large = load_pages(sample_pdf, dpi=144, max_side=10_000)[0]
    assert large.width > small.width


def test_max_side_caps_the_longest_edge(sample_pdf: Path) -> None:
    page = load_pages(sample_pdf, dpi=300, max_side=500)[0]
    assert max(page.width, page.height) == 500
    # Aspect ratio is preserved: 792/612 is about 1.294.
    assert page.width / page.height == pytest.approx(792 / 612, rel=0.01)


def test_rendered_page_is_not_blank(sample_pdf: Path) -> None:
    """A drawing has dark pixels; a blank render would be pure white."""
    page = load_pages(sample_pdf, dpi=72, max_side=2400)[0]
    with open_page(page) as image:
        extrema = image.convert("L").getextrema()
    assert extrema[0] < 128, "no dark pixels found, page looks blank"


def test_png_input_round_trips(sample_pdf: Path, tmp_path: Path) -> None:
    source = load_pages(sample_pdf, dpi=100, max_side=2400)[0]
    png_path = tmp_path / "page.png"
    png_path.write_bytes(source.png_bytes)

    pages = load_pages(png_path, max_side=2400)
    assert len(pages) == 1
    assert (pages[0].width, pages[0].height) == (source.width, source.height)


def test_png_input_respects_max_side(sample_pdf: Path, tmp_path: Path) -> None:
    png_path = tmp_path / "page.png"
    png_path.write_bytes(load_pages(sample_pdf, dpi=100)[0].png_bytes)

    page = load_pages(png_path, max_side=300)[0]
    assert max(page.width, page.height) == 300


def test_jpeg_input_is_supported(tmp_path: Path) -> None:
    jpg_path = tmp_path / "drawing.jpg"
    Image.new("RGB", (640, 480), "white").save(jpg_path)

    page = load_pages(jpg_path)[0]
    assert (page.width, page.height) == (640, 480)
    assert page.png_bytes.startswith(PNG_MAGIC)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RenderError, match="No such file"):
        load_pages(tmp_path / "nope.pdf")


def test_unsupported_suffix_raises(tmp_path: Path) -> None:
    bad = tmp_path / "drawing.dwg"
    bad.write_bytes(b"not a drawing we can read")
    with pytest.raises(RenderError, match="Unsupported file type"):
        load_pages(bad)


def test_corrupt_pdf_raises_render_error(tmp_path: Path) -> None:
    """A file named .pdf that is not one. pypdfium2's own error must not escape.

    Every caller catches ``RenderError`` and nothing else, so an error class leaking from
    a dependency is an exit code or a status code that never happens.
    """
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"certainly not a pdf")
    with pytest.raises(RenderError, match="Not a readable PDF"):
        load_pages(bad)


def test_corrupt_image_raises_render_error(tmp_path: Path) -> None:
    """The same for pillow, which raises ``UnidentifiedImageError``."""
    bad = tmp_path / "corrupt.png"
    bad.write_bytes(b"certainly not a png")
    with pytest.raises(RenderError, match="Not a readable image"):
        load_pages(bad)


def test_truncated_image_raises_render_error(tmp_path: Path) -> None:
    """A half-written file opens and then fails during load, which is a different path."""
    whole = io.BytesIO()
    Image.new("RGB", (80, 60), "white").save(whole, format="PNG")
    truncated = tmp_path / "half.png"
    truncated.write_bytes(whole.getvalue()[: len(whole.getvalue()) // 2])

    with pytest.raises(RenderError):
        load_pages(truncated)


@pytest.mark.parametrize(("dpi", "max_side"), [(0, 2400), (-1, 2400), (200, 0), (200, -5)])
def test_bad_arguments_raise(sample_pdf: Path, dpi: int, max_side: int) -> None:
    with pytest.raises(RenderError):
        load_pages(sample_pdf, dpi=dpi, max_side=max_side)


def test_page_image_is_frozen(sample_pdf: Path) -> None:
    import dataclasses

    page = load_pages(sample_pdf, dpi=72)[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        page.width = 1  # type: ignore[misc]
