"""HTTP front end for one DraftCheck run.

This module decides nothing. Extraction is :func:`pipeline.extract`, the verdicts are
:func:`pipeline.check`, the correction loop is :func:`agent.correct`, and the redline is
:mod:`draftcheck.redline` — exactly as the CLI drives them. What is here is the plumbing
the CLI gets for free from a terminal: receiving a file, keeping one run's state between
requests, and turning an exception into a status code.

**A run is a folder.** ``outputs/web/<uuid4>/`` holds everything one drawing produces, and
each request reads what the previous one left behind:

===================== =====================================================
``source.<ext>``      the uploaded drawing, under its original extension
``page.png``          page 1 rendered, served to the browser and shown to the model
``extraction.json``   the ``Drawing`` the model returned
``raw.txt``           the model's reply exactly as received
``meta.json``         provider name, model id, latency — never a key
``confirmed.json``    the drawing the user confirmed, written by ``/check``
``findings.json``     the findings for that confirmed drawing
``corrections.json``  the correction loop's full report
``redline.pdf/.png``  the two-page redline
===================== =====================================================

That makes the step a request is at a fact on disk rather than something to remember:
``/check`` needs the folder, ``/fix`` needs ``confirmed.json``, and a request arriving out
of order is refused by looking.

``run_id`` is declared as a ``UUID`` in every path, so a value that is not one is rejected
by FastAPI before any of this code runs and a run folder is always ``root / <uuid>``. A
path cannot be traversed out of.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from draftcheck.agent import CorrectionReport, RoundRecord, correct
from draftcheck.config import SUPPORTED_PROVIDERS, ConfigError, get_provider
from draftcheck.edits import RejectedEdit, parse_edit
from draftcheck.llm_client import LLMClient
from draftcheck.llm_parsing import JSONParseError
from draftcheck.pipeline import SupportsChatJSON, check, extract
from draftcheck.redline import EditNames, plain_edit, render_redline, save_redline
from draftcheck.render import RenderError, load_pages
from draftcheck.schema import Finding, finding_key, parse_drawing

#: Where run folders are created. A dependency, so tests point it at a tmp folder.
RUNS_ROOT = Path("outputs/web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Upload limits. Ten megabytes is far more than a rendered sheet needs and small enough
#: that a request can be held in memory while it is checked.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024

#: What ``render.load_pages`` can actually open. Checked on the filename rather than on the
#: browser's content type, which is guesswork on the client's side.
ALLOWED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}

DPI = 200

#: Each run's files, by the name the code uses for them.
PAGE_PNG = "page.png"
EXTRACTION_JSON = "extraction.json"
RAW_TXT = "raw.txt"
META_JSON = "meta.json"
CONFIRMED_JSON = "confirmed.json"
FINDINGS_JSON = "findings.json"
CORRECTIONS_JSON = "corrections.json"
REDLINE_PDF = "redline.pdf"
REDLINE_PNG = "redline.png"

#: A client, given a provider name or ``None`` for the configured default.
ClientFactory = Callable[[str | None], SupportsChatJSON]


def build_client(provider: str | None = None) -> SupportsChatJSON:
    """The real factory: an ``LLMClient`` for the named provider."""
    return LLMClient(provider=get_provider(provider))


def get_client_factory() -> ClientFactory:
    """The LLM client, as a dependency.

    A *factory* rather than a client, because the provider is chosen per request. Tests
    override this one function with a factory that returns a scripted client, and no
    request anywhere in the app reaches the network.
    """
    return build_client


def get_runs_root() -> Path:
    """Where run folders live, as a dependency so tests can redirect it."""
    return RUNS_ROOT


#: Request pieces as ``Annotated`` aliases. FastAPI reads the marker out of the annotation
#: rather than out of a default value, which keeps the signatures short and readable.
RunsRoot = Annotated[Path, Depends(get_runs_root)]
Clients = Annotated[ClientFactory, Depends(get_client_factory)]
DrawingUpload = Annotated[UploadFile, File(description="A .pdf, .png, or .jpg drawing.")]
ProviderField = Annotated[
    str | None, Form(description="baseten or meta. Default: the LLM_PROVIDER setting.")
]
DrawingBody = Annotated[dict, Body(description="The confirmed (possibly edited) drawing.")]


app = FastAPI(
    title="DraftCheck",
    description="Upload an engineering drawing, confirm what was read, check it, fix it.",
    version="0.1.0",
)


def _write_json(path: Path, payload: object) -> None:
    """Save a JSON file in a stable, human-readable form."""
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_dir(root: Path, run_id: uuid.UUID) -> Path:
    """The folder for an existing run, or 404.

    ``run_id`` has already been validated as a UUID by FastAPI, so this cannot escape
    ``root`` no matter what was sent.
    """
    run_dir = root / str(run_id)
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    return run_dir


def _existing_file(run_dir: Path, name: str, produced_by: str) -> Path:
    """One of a run's files, or 404 saying which step produces it."""
    path = run_dir / name
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"This run has no {name} yet. It is written by {produced_by}.",
        )
    return path


async def _save_upload(upload: UploadFile, destination: Path) -> int:
    """Stream the upload to disk, refusing anything over :data:`MAX_UPLOAD_BYTES`.

    Read in chunks rather than in one go: the point of a size limit is that an oversized
    request is never held whole, and a ``Content-Length`` header is the client's claim
    rather than a measurement.
    """
    written = 0
    with destination.open("wb") as handle:
        while chunk := await upload.read(CHUNK_BYTES):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                handle.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"The file is larger than the "
                        f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
                    ),
                )
            handle.write(chunk)
    if written == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    return written


def _checked_suffix(filename: str | None) -> str:
    """The upload's extension, if it is one we can render."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        supported = ", ".join(sorted(ALLOWED_SUFFIXES))
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type {suffix or '(none)'!r}. Supported: {supported}.",
        )
    return suffix


def _checked_provider(provider: str | None) -> str | None:
    """A provider name we know, or ``None`` to follow ``LLM_PROVIDER``."""
    if provider is None or not provider.strip():
        return None
    name = provider.strip().lower()
    if name not in SUPPORTED_PROVIDERS:
        supported = ", ".join(SUPPORTED_PROVIDERS)
        raise HTTPException(
            status_code=422,
            detail=f"Unknown provider {provider!r}. Supported: {supported}.",
        )
    return name


@app.post("/api/runs", status_code=201)
async def create_run(
    file: DrawingUpload,
    root: RunsRoot,
    client_factory: Clients,
    provider: ProviderField = None,
) -> dict:
    """Upload a drawing, render page 1, and read it with the model.

    The extraction comes back for the user to confirm or edit; nothing is checked yet.
    """
    suffix = _checked_suffix(file.filename)
    provider_name = _checked_provider(provider)

    run_id = uuid.uuid4()
    run_dir = root / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    source = run_dir / f"source{suffix}"
    try:
        await _save_upload(file, source)
    except HTTPException:
        # A refused upload leaves no run at all, rather than an empty folder that later
        # looks like a run whose files went missing.
        run_dir.rmdir()
        raise

    try:
        # Rendered here so the browser has a page to show and the correction loop has one
        # to send later. ``extract`` renders page 1 again for itself, which keeps this
        # endpoint free of a second copy of the extraction flow.
        page = load_pages(source, dpi=DPI)[0]
        (run_dir / PAGE_PNG).write_bytes(page.png_bytes)

        client = client_factory(provider_name)
        result = extract(source, client, dpi=DPI)
    except RenderError as exc:
        # Covers a file that is named .pdf and is not one: `load_pages` translates
        # pypdfium2's and pillow's own errors into RenderError, so a corrupt upload is the
        # client's problem with one exception to catch rather than three.
        raise HTTPException(status_code=400, detail=f"Could not read the drawing: {exc}") from exc
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Configuration error: {exc}") from exc
    except JSONParseError as exc:
        raise HTTPException(
            status_code=502, detail=f"The model did not return usable JSON: {exc}"
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=502, detail=f"Extraction does not match the drawing schema: {exc}"
        ) from exc

    drawing = result.drawing.model_dump(mode="json")
    meta = {
        "run_id": str(run_id),
        "source": file.filename,
        "provider": provider_name or getattr(getattr(client, "provider", None), "name", None),
        "model": result.model,
        "latency_s": result.latency_s,
        "attempts": result.attempts,
        "created": datetime.now(UTC).isoformat(),
    }

    _write_json(run_dir / EXTRACTION_JSON, drawing)
    (run_dir / RAW_TXT).write_text(result.raw_text, encoding="utf-8")
    _write_json(run_dir / META_JSON, meta)

    return {
        "run_id": str(run_id),
        "drawing": drawing,
        "parse_issues": result.drawing.parse_issues,
        "model": result.model,
        "latency_s": result.latency_s,
        "attempts": result.attempts,
        "page_url": f"/api/runs/{run_id}/page.png",
    }


@app.get("/api/runs/{run_id}/page.png")
def get_page(run_id: uuid.UUID, root: RunsRoot) -> FileResponse:
    """The rendered page 1, for the review screen to draw on."""
    run_dir = _run_dir(root, run_id)
    return FileResponse(_existing_file(run_dir, PAGE_PNG, "the upload"), media_type="image/png")


@app.post("/api/runs/{run_id}/check")
def check_run(
    run_id: uuid.UUID,
    drawing_json: DrawingBody,
    root: RunsRoot,
) -> dict:
    """Validate the drawing the user confirmed and run the rules over it.

    The body is whatever the review screen holds, so it goes through ``parse_drawing``
    like any other input: unreadable items are dropped into ``parse_issues`` rather than
    failing the request, and the saved ``confirmed.json`` is the validated result.
    """
    run_dir = _run_dir(root, run_id)

    try:
        drawing = parse_drawing(drawing_json)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=f"That is not shaped like a drawing: {exc}"
        ) from exc

    findings = check(drawing)
    _write_json(run_dir / CONFIRMED_JSON, drawing.model_dump(mode="json"))
    _write_json(run_dir / FINDINGS_JSON, [f.model_dump(mode="json") for f in findings])

    errors = sum(1 for finding in findings if finding.severity == "error")
    return {
        "run_id": str(run_id),
        "findings": [finding.model_dump(mode="json") for finding in findings],
        "parse_issues": drawing.parse_issues,
        "error_count": errors,
        "warning_count": len(findings) - errors,
    }


def _rejected_summary(item: RejectedEdit, names: EditNames) -> dict:
    """A refused proposal as the page shows it, in the same words as an applied one.

    A proposal can be refused before it is understood (``{"op": "delete_fcf"}``) or after
    (an edit that names an id the drawing does not have), so the sentence is only available
    when ``parse_edit`` can make an ``Edit`` out of it. When it cannot, the page says what
    the model called the operation rather than inventing a description of it.
    """
    raw = item.edit
    edit, _ = parse_edit(raw)
    if edit is not None:
        sentence, rationale = plain_edit(edit, names), edit.rationale
    elif isinstance(raw, dict) and isinstance(raw.get("op"), str) and raw["op"].strip():
        sentence, rationale = f"an edit the model called {raw['op'].strip()!r}", ""
    else:
        sentence, rationale = "an edit that was not shaped like an edit", ""
    return {"edit": sentence, "rationale": rationale, "reason": item.reason}


def _named_findings(findings: list[Finding], keys: list[str]) -> list[dict]:
    """Finding keys as key *and* message, so the page need not hold a copy of the rules."""
    messages = {finding_key(finding): finding.message for finding in findings}
    return [{"key": key, "message": messages.get(key, key)} for key in keys]


def _round_summary(record: RoundRecord, names: EditNames) -> dict:
    """One round as the page shows it: what was proposed, what stuck, and what changed.

    The edits are in plain English — the same sentences :mod:`draftcheck.redline` prints on
    page 2, built by the same function, so the screen and the PDF can never word a
    correction differently. ``corrections.json`` keeps the id-carrying form, which is what
    is wanted when you are reading the file rather than the drawing.
    """
    return {
        "round": record.number,
        "applied": [
            {
                "edit": plain_edit(edit, names),
                "rationale": edit.rationale,
                "finding_ids": edit.finding_ids,
            }
            for edit in record.applied
        ],
        "rejected": [_rejected_summary(item, names) for item in record.rejected],
        "findings_before": len(record.findings_before),
        "findings_after": len(record.findings_after),
        # Resolved findings are gone by the end of the round, so their wording comes from
        # before it; introduced ones did not exist until after it.
        "resolved": _named_findings(record.findings_before, record.resolved),
        "introduced": _named_findings(record.findings_after, record.introduced),
        "error": record.error,
    }


def _summarize(report: CorrectionReport) -> dict:
    """The correction report as the UI needs it: rounds, edits, and the verdicts."""
    payload = report.to_dict()
    names = EditNames(report)
    return {
        "rounds": [_round_summary(record, names) for record in report.rounds],
        "status": payload["status"],
        "final_findings": payload["final_findings"],
        "final_drawing": payload["final_drawing"],
        "applied_count": sum(len(record.applied) for record in report.rounds),
        "rejected_count": sum(len(record.rejected) for record in report.rounds),
        "resolved_count": report.resolved_count,
        "unresolved_count": report.unresolved_count,
    }


@app.post("/api/runs/{run_id}/fix")
def fix_run(
    run_id: uuid.UUID,
    root: RunsRoot,
    client_factory: Clients,
) -> dict:
    """Run the correction loop on the confirmed drawing and write the redline.

    Whether a finding cleared is decided by ``agent.correct`` re-running the rules, never
    by the model's account of its own edits. This endpoint only reports what it is told.
    """
    run_dir = _run_dir(root, run_id)
    confirmed = run_dir / CONFIRMED_JSON
    if not confirmed.is_file():
        raise HTTPException(
            status_code=409,
            detail="Confirm the extraction with POST /api/runs/{run_id}/check first.",
        )

    drawing = parse_drawing(_read_json(confirmed))
    page_png = _existing_file(run_dir, PAGE_PNG, "the upload").read_bytes()

    meta = _read_json(run_dir / META_JSON) if (run_dir / META_JSON).is_file() else {}
    try:
        client = client_factory(meta.get("provider"))
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Configuration error: {exc}") from exc

    report = correct(drawing, page_png, client)
    _write_json(run_dir / CORRECTIONS_JSON, report.to_dict())

    # The redline marks the drawing *as submitted*, so its findings and its numbering match
    # findings.json; the report adds each applied edit and the rule engine's verdict.
    pages = render_redline(
        page_png,
        report.original_findings,
        report,
        model_name=meta.get("model"),
    )
    save_redline(pages, run_dir / REDLINE_PDF, run_dir / REDLINE_PNG)

    summary = _summarize(report)
    summary["run_id"] = str(run_id)
    summary["redline_pdf_url"] = f"/api/runs/{run_id}/redline.pdf"
    summary["redline_png_url"] = f"/api/runs/{run_id}/redline.png"
    return summary


@app.get("/api/runs/{run_id}/redline.pdf")
def get_redline_pdf(run_id: uuid.UUID, root: RunsRoot) -> FileResponse:
    """The two-page redline: marked drawing, then the findings page."""
    run_dir = _run_dir(root, run_id)
    path = _existing_file(run_dir, REDLINE_PDF, "POST /api/runs/{run_id}/fix")
    return FileResponse(path, media_type="application/pdf", filename=REDLINE_PDF)


@app.get("/api/runs/{run_id}/redline.png")
def get_redline_png(run_id: uuid.UUID, root: RunsRoot) -> FileResponse:
    """Page 1 of the redline on its own, for embedding in the page."""
    run_dir = _run_dir(root, run_id)
    path = _existing_file(run_dir, REDLINE_PNG, "POST /api/runs/{run_id}/fix")
    return FileResponse(path, media_type="image/png")


# Mounted last, so every /api route above wins over the static file server.
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
