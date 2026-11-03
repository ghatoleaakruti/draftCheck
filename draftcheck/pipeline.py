"""The two stages of one DraftCheck run: extract, then check.

``extract`` is the only place a model is involved, and all it does is turn pixels into a
validated :class:`~draftcheck.schema.Drawing`. ``check`` is plain Python over that drawing.
Neither function prints anything or touches the filesystem beyond reading the drawing, so
the CLI and, later, the API can both drive them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from draftcheck.llm_client import JSONChatResult, Usage
from draftcheck.memory import memory_mb
from draftcheck.prompts import EXTRACTION_SYSTEM, EXTRACTION_USER
from draftcheck.render import load_pages
from draftcheck.rules import run_rules
from draftcheck.schema import Drawing, Finding, parse_drawing

#: Stage lines for the server log. Messages carry sizes and memory only — never the image,
#: the prompt, or the model's reply.
logger = logging.getLogger(__name__)


class SupportsChatJSON(Protocol):
    """The one method ``extract`` needs from a client, so tests can pass a fake."""

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult: ...


@dataclass(frozen=True)
class ExtractionResult:
    """A parsed drawing plus the accounting from the model call that produced it."""

    drawing: Drawing
    #: The model's reply exactly as received, saved so a bad parse can be looked at later.
    raw_text: str
    latency_s: float
    model: str
    usage: Usage
    attempts: int


def extract(path: str | Path, client: SupportsChatJSON, dpi: int = 200) -> ExtractionResult:
    """Read one drawing with the model and return it as a validated ``Drawing``.

    Only page 1 is sent. Multi-page sheets are a later problem, and sending every page of a
    long PDF would cost tokens without being asked for.

    Args:
        path: A .pdf, .png, or .jpg drawing.
        client: Anything with ``chat_json`` — normally an ``LLMClient``.
        dpi: Rasterization resolution for PDFs.

    Raises:
        RenderError: If the file cannot be turned into a page image.
        JSONParseError: If the model's reply contains no usable JSON object.
        pydantic.ValidationError: If the JSON is not shaped like a drawing at all. Single
            unreadable items do not raise; they land in ``Drawing.parse_issues``.
    """
    png_bytes = load_pages(path, dpi=dpi, max_pages=1)[0].png_bytes
    return extract_page(png_bytes, client)


def extract_page(png_bytes: bytes, client: SupportsChatJSON) -> ExtractionResult:
    """Read one already-rendered page with the model and return it as a ``Drawing``.

    The API renders page 1 itself (it keeps the PNG for the browser), so it calls this
    directly rather than :func:`extract`, which would render the same page a second time.

    Raises:
        JSONParseError: If the model's reply contains no usable JSON object.
        pydantic.ValidationError: If the JSON is not shaped like a drawing at all.
    """
    logger.info(
        "model call started (image %.1f MB, memory %.0f MB)", len(png_bytes) / 1e6, memory_mb()
    )
    result = client.chat_json(EXTRACTION_SYSTEM, EXTRACTION_USER, [png_bytes])
    logger.info("model call done (%.1f s, memory %.0f MB)", result.latency_s, memory_mb())
    drawing = parse_drawing(result.data)
    logger.info("parse done (memory %.0f MB)", memory_mb())
    return ExtractionResult(
        drawing=drawing,
        raw_text=result.text,
        latency_s=result.latency_s,
        model=result.model,
        usage=result.usage,
        attempts=result.attempts,
    )


def check(drawing: Drawing) -> list[Finding]:
    """Run every rule against the drawing. No model, so the same input always agrees."""
    return run_rules(drawing)
