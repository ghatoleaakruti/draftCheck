"""Command line entry point for DraftCheck.

    python -m draftcheck.cli check samples/generated/plate_001.pdf
    python -m draftcheck.cli check samples/generated/plate_001.pdf \
        --from-json tests/fixtures/plate_001.model.json
    python -m draftcheck.cli check samples/generated/plate_001.pdf --provider meta

``--provider`` picks between the configured providers (``baseten`` or ``meta``); with no
flag the provider comes from ``LLM_PROVIDER`` in ``.env``.

``--from-json`` loads a saved extraction instead of calling the model, which makes a run
free, offline, and repeatable. It is how the tests exercise the whole command.

Exit codes:
    0  ran, and no finding was error severity
    1  ran, and at least one error-severity finding was reported
    2  could not run: unreadable drawing, bad JSON, missing config, or a model failure
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from draftcheck.config import SUPPORTED_PROVIDERS, ConfigError, get_provider
from draftcheck.llm_client import LLMClient
from draftcheck.llm_parsing import JSONParseError
from draftcheck.pipeline import ExtractionResult, check, extract
from draftcheck.render import RenderError, load_pages
from draftcheck.schema import Drawing, Finding, parse_drawing

DEFAULT_OUTPUT_ROOT = Path("outputs/runs")

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_FAILED = 2


def _write_json(path: Path, payload: object) -> None:
    """Save a JSON file in a stable, human-readable form."""
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_extraction_file(path: Path) -> Drawing:
    """Read a saved extraction JSON file and validate it as a ``Drawing``.

    Raises:
        OSError: If the file cannot be read.
        json.JSONDecodeError: If it is not valid JSON.
        ValidationError: If the JSON is not shaped like a drawing.
    """
    return parse_drawing(json.loads(path.read_text(encoding="utf-8")))


def format_table(findings: list[Finding]) -> str:
    """Lay the findings out as aligned columns, message last so the columns stay straight."""
    if not findings:
        return "No findings."

    rows = [
        (
            finding.rule_id,
            finding.severity,
            ", ".join(finding.item_ids) or "-",
            finding.message,
        )
        for finding in findings
    ]
    headers = ("RULE", "SEVERITY", "ITEMS", "MESSAGE")

    # The message column is last and never padded, so only the first three need widths.
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(3)]

    def line(cells: tuple[str, ...]) -> str:
        padded = [cells[i].ljust(widths[i]) for i in range(3)]
        return "  ".join([*padded, cells[3]]).rstrip()

    return "\n".join([line(headers), *(line(row) for row in rows)])


def _print_summary(findings: list[Finding], drawing: Drawing) -> None:
    """Print the findings table, any parse issues, and the counts underneath."""
    print()
    print(format_table(findings))

    if drawing.parse_issues:
        print()
        print(f"Parse issues ({len(drawing.parse_issues)}) — items dropped during parsing:")
        for issue in drawing.parse_issues:
            print(f"  - {issue}")

    errors = sum(1 for finding in findings if finding.severity == "error")
    warnings = len(findings) - errors
    print()
    print(f"{errors} error(s), {warnings} warning(s).")


def _run_model(drawing_path: Path, dpi: int, provider_name: str | None) -> ExtractionResult:
    """Build a real client and run one extraction pass, reporting progress as it goes."""
    client = LLMClient(provider=get_provider(provider_name))
    print(f"Provider: {client.provider.name}")
    print(f"Model:    {client.provider.model}")
    print("Calling the model...")
    return extract(drawing_path, client, dpi=dpi)


def run_check(args: argparse.Namespace) -> int:
    """Execute the ``check`` subcommand and return its exit code."""
    drawing_path = Path(args.drawing)
    out_dir = Path(args.out) if args.out else DEFAULT_OUTPUT_ROOT / drawing_path.stem

    extraction: ExtractionResult | None = None
    try:
        if args.from_json:
            # Still render the drawing, so a broken path or unsupported file fails here
            # rather than silently "passing" on a saved extraction.
            load_pages(drawing_path, dpi=args.dpi)
            drawing = _load_extraction_file(Path(args.from_json))
            print(f"Drawing:  {drawing_path}")
            print(f"Source:   {args.from_json} (model not called)")
        else:
            print(f"Drawing:  {drawing_path}")
            extraction = _run_model(drawing_path, args.dpi, args.provider)
            drawing = extraction.drawing
    except RenderError as exc:
        print(f"Could not read the drawing: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except JSONParseError as exc:
        print(f"The model did not return usable JSON: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read {args.from_json or drawing_path}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except ValidationError as exc:
        print(f"Extraction does not match the drawing schema: {exc}", file=sys.stderr)
        return EXIT_FAILED

    findings = check(drawing)

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "extraction.json", drawing.model_dump(mode="json"))
    _write_json(out_dir / "findings.json", [f.model_dump(mode="json") for f in findings])
    written = ["extraction.json", "findings.json"]
    if extraction is not None:
        (out_dir / "raw.txt").write_text(extraction.raw_text, encoding="utf-8")
        written.append("raw.txt")
        print(f"Latency:  {extraction.latency_s:.2f}s  (attempts: {extraction.attempts})")

    _print_summary(findings, drawing)
    print(f"Wrote {', '.join(written)} to {out_dir}/")

    return EXIT_FINDINGS if any(f.severity == "error" for f in findings) else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the whole CLI."""
    parser = argparse.ArgumentParser(
        prog="python -m draftcheck.cli",
        description="Check an engineering drawing for dimensioning and GD&T errors.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="Extract a drawing and run the rules.")
    check_parser.add_argument("drawing", help="Path to a .pdf, .png, or .jpg drawing.")
    check_parser.add_argument(
        "--from-json",
        help="Load this extraction JSON instead of calling the model.",
    )
    check_parser.add_argument(
        "--out",
        help=f"Directory for the run's output files. Default: {DEFAULT_OUTPUT_ROOT}/<name>/",
    )
    check_parser.add_argument(
        "--provider",
        choices=SUPPORTED_PROVIDERS,
        default=None,
        help="Which provider to call. Default: the LLM_PROVIDER value in .env.",
    )
    check_parser.add_argument("--dpi", type=int, default=200, help="PDF rasterization DPI.")
    check_parser.set_defaults(handler=run_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the chosen subcommand."""
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
