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


def _to_page_image(image: Image.Image, max_side: int) -> PageImage:
    """Fit, flatten to RGB, and encode a pillow image as a PageImage."""
    image = _fit_to_max_side(image, max_side)
    if image.mode != "RGB":
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return PageImage(png_bytes=buffer.getvalue(), width=image.width, height=image.height)


def load_pages(path: str | Path, dpi: int = 200, max_side: int = 2400) -> list[PageImage]:
    """Load a drawing as a list of page images.

    Args:
        path: A .pdf (all pages) or .png/.jpg/.jpeg (one page) file.
        dpi: Rasterization resolution for PDFs. Ignored for image files.
        max_side: Longest allowed pixel side; larger pages are scaled down.

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
        scale = dpi / POINTS_PER_INCH
        for page in pdf:
            bitmap = page.render(scale=scale)
            pages.append(_to_page_image(bitmap.to_pil(), max_side))
    except pdfium.PdfiumError as exc:
        # A document that loads can still have a page that will not draw.
        raise RenderError(f"Could not render {path}: {exc}") from exc
    finally:
        pdf.close()

    return pages
