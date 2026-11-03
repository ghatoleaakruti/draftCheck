"""Turn a drawing file into page images the model can see.

PDFs are rasterized with pypdfium2; PNG/JPG files are loaded directly with pillow.
Either way every page comes back as PNG bytes, capped at ``max_side`` pixels so a
large sheet does not blow up the payload sent to a vision model.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
POINTS_PER_INCH = 72.0


class RenderError(ValueError):
    """Raised when a file cannot be rendered to page images."""


@dataclass(frozen=True)
class PageImage:
    """One rendered page as PNG bytes, with its pixel size."""

    png_bytes: bytes
    width: int
    height: int


def _fit_to_max_side(image: Image.Image, max_side: int) -> Image.Image:
    """Scale the image down proportionally if its longest side exceeds max_side."""
    longest = max(image.width, image.height)
    if longest <= max_side:
        return image
    ratio = max_side / longest
    new_size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
    return image.resize(new_size, Image.LANCZOS)


def render_scale(width_pt: float, height_pt: float, dpi: int, max_side: int) -> float:
    """The pdfium scale that renders a page at ``dpi`` but no bigger than ``max_side``.

    Rendering at full dpi and shrinking afterwards would first build the full-size bitmap,
    which for a large sheet (A0 at 200 dpi is about 9400 x 6600 px, roughly 250 MB) is
    most of a small server's memory. Capping the scale here means the bitmap pdfium makes
    is already about the size that will be sent.
    """
    scale = dpi / POINTS_PER_INCH
    longest_pt = max(width_pt, height_pt)
    if longest_pt > 0:
        scale = min(scale, max_side / longest_pt)
    return scale


def _to_page_image(image: Image.Image, max_side: int) -> PageImage:
    """Fit, flatten to RGB, and encode a pillow image as a PageImage.

    Each intermediate image (resized, converted) is closed as soon as the next one exists,
    so at most two full-size copies of the page are alive at once. The caller's own image
    is left for the caller to close.
    """
    working = _fit_to_max_side(image, max_side)
    if working.mode != "RGB":
        converted = working.convert("RGB")
        if working is not image:
            working.close()
        working = converted
    try:
        buffer = io.BytesIO()
        working.save(buffer, format="PNG")
        return PageImage(png_bytes=buffer.getvalue(), width=working.width, height=working.height)
    finally:
        if working is not image:
            working.close()


def load_pages(
    path: str | Path, dpi: int = 200, max_side: int = 2400, max_pages: int | None = None
) -> list[PageImage]:
    """Load a drawing as a list of page images.

    Args:
        path: A .pdf (all pages) or .png/.jpg/.jpeg (one page) file.
        dpi: Rasterization resolution for PDFs. Ignored for image files.
        max_side: Longest allowed pixel side; larger pages are scaled down.
        max_pages: Render at most this many pages from the front of a PDF. ``None`` renders
            them all. Callers that only use page 1 pass 1, so a long PDF does not cost a
            full-size bitmap per page it will never send.

    Raises:
        RenderError: If the file is missing, empty, an unsupported type, or cannot be
            read as the kind of file its extension claims. A file named ``.pdf`` that is
            not one is the caller's problem, not a crash: pypdfium2 and pillow each raise
            their own error for it, and both are translated here so that every caller —
            the CLI's exit code, the API's status code — has one exception to catch.
    """
    path = Path(path)
    if dpi <= 0:
        raise RenderError(f"dpi must be positive, got {dpi}")
    if max_side <= 0:
        raise RenderError(f"max_side must be positive, got {max_side}")
    if max_pages is not None and max_pages <= 0:
        raise RenderError(f"max_pages must be positive, got {max_pages}")
    if not path.is_file():
        raise RenderError(f"No such file: {path}")

    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        try:
            with Image.open(path) as image:
                image.load()
                return [_to_page_image(image, max_side)]
        except UnidentifiedImageError as exc:
            raise RenderError(f"Not a readable image: {path}") from exc
        except OSError as exc:
            # A truncated or half-written file opens and then fails during load.
            raise RenderError(f"Could not read the image {path}: {exc}") from exc

    if suffix not in PDF_SUFFIXES:
        supported = ", ".join(sorted(PDF_SUFFIXES | IMAGE_SUFFIXES))
        raise RenderError(f"Unsupported file type {suffix!r} for {path}. Supported: {supported}")

    pages: list[PageImage] = []
    try:
        pdf = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        raise RenderError(f"Not a readable PDF: {path} ({exc})") from exc

    try:
        if len(pdf) == 0:
            raise RenderError(f"PDF has no pages: {path}")
        count = len(pdf) if max_pages is None else min(len(pdf), max_pages)
        for index in range(count):
            page = pdf[index]
            width_pt, height_pt = page.get_size()
            bitmap = page.render(scale=render_scale(width_pt, height_pt, dpi, max_side))
            try:
                with bitmap.to_pil() as image:
                    pages.append(_to_page_image(image, max_side))
            finally:
                # The raw bitmap is the biggest thing in memory; let it go before the next
                # page rather than whenever the garbage collector gets round to it.
                bitmap.close()
                page.close()
    except pdfium.PdfiumError as exc:
        # A document that loads can still have a page that will not draw.
        raise RenderError(f"Could not render {path}: {exc}") from exc
    finally:
        pdf.close()

    return pages
