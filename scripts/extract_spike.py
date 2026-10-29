"""Extraction spike: send one drawing to the model and look at what comes back.

Run with:
    python -m scripts.extract_spike samples/generated/plate_001.pdf \
        --gt samples/generated/plate_001.gt.json

Writes three files into outputs/spike/:
    <name>.raw.txt      the model's reply exactly as received
    <name>.json         the parsed JSON object
    <name>.overlay.png  page 1 with red boxes (model) and green boxes (ground truth)

This is a look-at-it tool, not a checker. Nothing here decides pass or fail.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from draftcheck.config import ConfigError
from draftcheck.llm_client import LLMClient
from draftcheck.llm_parsing import JSONParseError
from draftcheck.prompts import EXTRACTION_SYSTEM, EXTRACTION_USER
from draftcheck.render import RenderError, load_pages

OUTPUT_DIR = Path("outputs/spike")
MODEL_COLOR = "red"
GROUND_TRUTH_COLOR = "green"

# Ground truth and model output now use the same list names, from draftcheck.schema.
GROUND_TRUTH_LISTS = ("datums", "dimensions", "fcfs")
MODEL_LISTS = GROUND_TRUTH_LISTS


def _collect_boxes(payload: dict, keys: tuple[str, ...]) -> list[list[float]]:
    """Pull every usable bbox out of the named top-level lists."""
    boxes: list[list[float]] = []
    for key in keys:
        for entry in payload.get(key) or []:
            if not isinstance(entry, dict):
                continue
            bbox = entry.get("bbox")
            if isinstance(bbox, list) and len(bbox) == 4 and all_numbers(bbox):
                boxes.append([float(v) for v in bbox])
    return boxes


def all_numbers(values: list) -> bool:
    """True when every item is a real number (booleans do not count)."""
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)


def draw_overlay(
    png_bytes: bytes,
    model_boxes: list[list[float]],
    gt_boxes: list[list[float]],
) -> bytes:
    """Draw normalized boxes onto the page image and return it as PNG bytes."""
    with Image.open(io.BytesIO(png_bytes)) as page:
        image = page.convert("RGB")

    draw = ImageDraw.Draw(image)
    for boxes, color in ((gt_boxes, GROUND_TRUTH_COLOR), (model_boxes, MODEL_COLOR)):
        for x0, y0, x1, y1 in boxes:
            left, right = sorted((x0 * image.width, x1 * image.width))
            top, bottom = sorted((y0 * image.height, y1 * image.height))
            draw.rectangle([left, top, right, bottom], outline=color, width=3)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def summarize(data: dict) -> dict:
    """Build the small counts dictionary printed at the end of a run."""
    title_block = data.get("title_block") or {}
    return {
        "units": data.get("units"),
        "title": title_block.get("title") if isinstance(title_block, dict) else None,
        "drawing_number": (
            title_block.get("drawing_number") if isinstance(title_block, dict) else None
        ),
        "datums": len(data.get("datums") or []),
        "dimensions": len(data.get("dimensions") or []),
        "fcfs": len(data.get("fcfs") or []),
        "notes": len(data.get("notes") or []),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one extraction pass on a drawing.")
    parser.add_argument("drawing", help="Path to a .pdf, .png, or .jpg drawing.")
    parser.add_argument("--gt", help="Optional ground truth JSON to draw in green.")
    parser.add_argument("--dpi", type=int, default=200, help="PDF rasterization DPI.")
    parser.add_argument(
        "--out", default=str(OUTPUT_DIR), help="Directory for the spike output files."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    drawing_path = Path(args.drawing)

    try:
        pages = load_pages(drawing_path, dpi=args.dpi)
    except RenderError as exc:
        print(f"Render error: {exc}", file=sys.stderr)
        return 1

    try:
        client = LLMClient()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    print(f"Drawing:  {drawing_path}  ({len(pages)} page(s))")
    print("Calling the model...")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = drawing_path.stem

    try:
        result = client.chat_json(
            EXTRACTION_SYSTEM, EXTRACTION_USER, [page.png_bytes for page in pages]
        )
    except JSONParseError as exc:
        raw_path = out_dir / f"{stem}.raw.txt"
        raw_path.write_text(exc.raw_text, encoding="utf-8")
        print(f"Could not parse JSON after a retry: {exc}", file=sys.stderr)
        print(f"Raw reply saved to {raw_path}", file=sys.stderr)
        return 1

    raw_path = out_dir / f"{stem}.raw.txt"
    json_path = out_dir / f"{stem}.json"
    overlay_path = out_dir / f"{stem}.overlay.png"

    raw_path.write_text(result.text, encoding="utf-8")
    json_path.write_text(json.dumps(result.data, indent=2, ensure_ascii=False), encoding="utf-8")

    gt_boxes: list[list[float]] = []
    if args.gt:
        ground_truth = json.loads(Path(args.gt).read_text(encoding="utf-8"))
        gt_boxes = _collect_boxes(ground_truth, GROUND_TRUTH_LISTS)

    model_boxes = _collect_boxes(result.data, MODEL_LISTS)
    overlay_path.write_bytes(draw_overlay(pages[0].png_bytes, model_boxes, gt_boxes))

    print(f"Model:    {result.model}")
    print(f"Latency:  {result.latency_s:.2f}s  (attempts: {result.attempts})")
    print(
        "Usage:    "
        f"prompt={result.usage.prompt_tokens} "
        f"completion={result.usage.completion_tokens} "
        f"total={result.usage.total_tokens}"
    )
    print(f"Boxes:    model={len(model_boxes)} ground_truth={len(gt_boxes)}")
    print("Summary:")
    print(json.dumps(summarize(result.data), indent=2, ensure_ascii=False))
    print(f"Wrote:    {raw_path}")
    print(f"          {json_path}")
    print(f"          {overlay_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
