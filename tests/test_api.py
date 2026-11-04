"""Tests for the HTTP layer.

Every test overrides two dependencies: the LLM client factory, so the model is a scripted
fake and no request touches the network, and the runs root, so each test gets its own tmp
folder and runs never see each other's files.

What is being tested here is the plumbing — status codes, run state carried between
requests, and the fact that the data the user confirms is the data the rules run on. The
rules themselves, the loop, and the redline have their own tests.
"""

import dataclasses
import json
import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError, BadRequestError, InternalServerError

from draftcheck.api import (
    DEFAULT_MAX_SIDE,
    DEFAULT_MODEL_TIMEOUT_S,
    DEFAULT_RENDER_DPI,
    DEMO_ENV_VAR,
    ENV_FALLBACK_PROVIDER,
    ENV_MAX_SIDE,
    ENV_MODEL_TIMEOUT,
    ENV_PRIMARY_TIMEOUT,
    ENV_RENDER_DPI,
    FAKE_MODEL_ENV_VAR,
    Fallback,
    FallbackPolicy,
    GuardedClient,
    app,
    fake_model_is_allowed,
    fallback_policy,
    finding_payload,
    get_cleaner,
    get_client_factory,
    get_disconnect_probe,
    get_fallback_policy,
    get_limiter,
    get_model_timeout,
    get_runs_root,
    model_timeout_s,
    render_settings,
    stamp_model_name,
)
from draftcheck.config import ConfigError
from draftcheck.fake_model import FAKE_MODEL_NAME, fake_client_factory
from draftcheck.limits import LIMIT_MESSAGE, Cleaner, Limits, UsageLimiter
from draftcheck.llm_client import MODEL_TIMEOUT_MESSAGE, JSONChatResult, ModelCancelled, Usage
from draftcheck.prompts import EXTRACTION_SYSTEM
from draftcheck.rules import RULES
from draftcheck.schema import Finding

REPO_ROOT = Path(__file__).resolve().parent.parent
DRAWING = REPO_ROOT / "samples" / "generated" / "plate_001.pdf"
FIXTURE_JSON = REPO_ROOT / "tests" / "fixtures" / "plate_001.model.json"

UNKNOWN_RUN = "00000000-0000-4000-8000-000000000000"

#: The fallback switched off, for the tests that are not about it.
NO_FALLBACK = FallbackPolicy(provider=None, primary_timeout_s=45)


class FakeClient:
    """Answers extraction with a fixed drawing and correction with scripted edits."""

    def __init__(self, extraction: dict, *corrections: dict) -> None:
        self.extraction = extraction
        self.corrections = list(corrections)
        self.calls: list[str] = []

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        if system == EXTRACTION_SYSTEM:
            self.calls.append("extract")
            data = self.extraction
        else:
            self.calls.append("correct")
            data = self.corrections.pop(0) if self.corrections else {"edits": []}
        return JSONChatResult(
            data=data,
            text=json.dumps(data),
            latency_s=0.25,
            usage=Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
            model="fake-model",
            attempts=1,
        )


def extraction_data() -> dict:
    """The real extraction from the 2026-10-29 spike run: one GDT-001 error on fcf_1."""
    return json.loads(FIXTURE_JSON.read_text(encoding="utf-8"))


@pytest.fixture
def fake() -> FakeClient:
    """The model for one test. A test may replace its scripted corrections."""
    return FakeClient(extraction_data())


#: Limits for the ordinary tests: generous enough that no test hits one by accident, so a
#: 429 anywhere below is a real failure rather than a test that uploaded one time too many.
ROOMY = Limits(uploads_per_ip_per_hour=1000, fixes_per_ip_per_hour=1000, model_calls_per_day=1000)


def make_client(tmp_path: Path, fake: FakeClient, limiter: UsageLimiter, cleaner: Cleaner):
    """A TestClient with every outside edge replaced: files, model, counters, and clock.

    The counters are per test, because they are process-wide in production — one shared
    limiter would make a test's result depend on how many tests ran before it.
    """
    app.dependency_overrides[get_runs_root] = lambda: tmp_path
    app.dependency_overrides[get_client_factory] = lambda: (lambda provider=None: fake)
    app.dependency_overrides[get_limiter] = lambda: limiter
    app.dependency_overrides[get_cleaner] = lambda: cleaner
    # Off unless a test turns it on: these tests are about other things, and a fallback to
    # the same fake would only blur what they check. The fallback has its own tests below.
    app.dependency_overrides.setdefault(get_fallback_policy, lambda: NO_FALLBACK)
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path, fake: FakeClient):
    """A TestClient whose runs land in tmp_path and whose model is the fake."""
    with make_client(tmp_path, fake, UsageLimiter(ROOMY), Cleaner(ROOMY)) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def upload(client: TestClient, path: Path = DRAWING, **data) -> dict:
    """Post a drawing and return the decoded reply, asserting it was accepted."""
    with path.open("rb") as handle:
        response = client.post(
            "/api/runs",
            files={"file": (path.name, handle, "application/pdf")},
            data=data,
        )
    assert response.status_code == 201, response.text
    return response.json()


def rule_ids(findings: list[dict]) -> list[str]:
    return [finding["rule_id"] for finding in findings]


def test_happy_path_upload_check_fix_redline(client: TestClient, fake: FakeClient, tmp_path):
    """Upload, confirm, check, fix, and fetch the redline, as the UI will drive it."""
    created = upload(client)
    run_id = created["run_id"]
    assert created["model"] == "fake-model"
    assert created["parse_issues"] == []
    assert created["drawing"]["fcfs"][0]["datum_refs"][2]["label"] == "C"

    page = client.get(f"/api/runs/{run_id}/page.png")
    assert page.status_code == 200
    assert page.headers["content-type"] == "image/png"
    assert page.content.startswith(b"\x89PNG")

    # Confirmed unchanged: the seeded undefined-datum error is still there.
    checked = client.post(f"/api/runs/{run_id}/check", json=created["drawing"])
    assert checked.status_code == 200, checked.text
    assert "GDT-001" in rule_ids(checked.json()["findings"])
    assert checked.json()["error_count"] == 1

    # The model proposes the fix a real run proposed: drop the surplus datum reference.
    fake.corrections = [
        {
            "edits": [
                {
                    "op": "remove_datum_ref",
                    "fcf_id": "fcf_1",
                    "label": "C",
                    "rationale": "A and B already locate the pattern.",
                    "finding_ids": ["GDT-001@fcf_1"],
                }
            ]
        }
    ]
    fixed = client.post(f"/api/runs/{run_id}/fix")
    assert fixed.status_code == 200, fixed.text
    report = fixed.json()
    assert report["applied_count"] == 1
    assert report["rejected_count"] == 0
    assert report["resolved_count"] == 1
    assert [entry["status"] for entry in report["status"]] == ["resolved"]
    assert len(report["rounds"]) == 1

    pdf = client.get(f"/api/runs/{run_id}/redline.pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    assert client.get(f"/api/runs/{run_id}/redline.png").status_code == 200

    run_dir = tmp_path / run_id
    written = {path.name for path in run_dir.iterdir()}
    assert written == {
        "source.pdf",
        "page.png",
        "extraction.json",
        "raw.txt",
        "meta.json",
        "confirmed.json",
        "findings.json",
        "corrections.json",
        "redline.pdf",
        "redline.png",
    }
    assert fake.calls == ["extract", "correct"]


def test_fix_reports_each_round_in_plain_english(client: TestClient, fake: FakeClient):
    """The fix reply carries everything the page shows, in words, with no ids to decode.

    The page has no copy of the rules and no way to turn ``remove_datum_ref`` into a
    sentence, so the server does both: each edit is worded by ``redline.plain_edit`` — the
    same function that writes page 2 of the redline — and every finding a round touched
    comes back with its message beside its key.

    One of the two proposed edits is refused: the model is told to delete the frame, which
    is the one thing ``edits.py`` never allows. A rejected proposal has to reach the page
    with Python's reason attached, which is the half of the round that shows the rule
    engine is in charge rather than the model.
    """
    created = upload(client)
    run_id = created["run_id"]
    client.post(f"/api/runs/{run_id}/check", json=created["drawing"])

    fake.corrections = [
        {
            "edits": [
                {
                    "op": "delete_fcf",
                    "fcf_id": "fcf_1",
                    "rationale": "Removing the frame removes the problem.",
                },
                {
                    "op": "remove_datum_ref",
                    "fcf_id": "fcf_1",
                    "label": "C",
                    "rationale": "A and B already locate the pattern.",
                    "finding_ids": ["GDT-001@fcf_1"],
                },
            ]
        }
    ]
    body = client.post(f"/api/runs/{run_id}/fix").json()

    assert len(body["rounds"]) == 1
    first = body["rounds"][0]
    assert first["round"] == 1
    assert first["error"] is None

    # The applied edit, said the way the redline says it: no op name, no ids.
    assert first["applied"] == [
        {
            "edit": "Remove datum C from the position frame",
            "rationale": "A and B already locate the pattern.",
            "finding_ids": ["GDT-001@fcf_1"],
            "before": "⌖ Ø0.25 Ⓜ | A | B | C",
            "after": "⌖ Ø0.25 Ⓜ | A | B",
            "pen": "drop C",
            "target_id": "fcf_1",
            "status": "verified",
            "note": "The re-run no longer reports GDT-001@fcf_1.",
        }
    ]

    # The refused one, with the reason Python wrote for refusing it.
    assert len(first["rejected"]) == 1
    refused = first["rejected"][0]
    assert refused["edit"] == "an edit the model called 'delete_fcf'"
    assert refused["rationale"] == ""
    assert "not an allowed operation" in refused["reason"]
    # Never understood, so there is no before/after to draw and no pen note to write.
    assert (refused["before"], refused["after"], refused["pen"]) == (None, None, None)
    assert refused["status"] == "rejected"

    # "Findings: 1 before → 0 after", and which one went.
    assert (first["findings_before"], first["findings_after"]) == (1, 0)
    assert first["introduced"] == []
    assert [cleared["key"] for cleared in first["resolved"]] == ["GDT-001@fcf_1"]
    assert "datum C" in first["resolved"][0]["message"]

    # The verdict per original finding, and where the redline is.
    assert body["status"] == [
        {
            "finding": "GDT-001@fcf_1",
            "status": "resolved",
            "severity": "error",
            "message": body["status"][0]["message"],
            "outcome": "verified",
        }
    ]
    assert body["applied_count"] == 1
    assert body["rejected_count"] == 1
    assert body["redline_png_url"] == f"/api/runs/{run_id}/redline.png"
    assert body["redline_pdf_url"] == f"/api/runs/{run_id}/redline.pdf"


def test_corrections_json_keeps_the_ids_the_page_leaves_out(
    client: TestClient, fake: FakeClient, tmp_path
):
    """The file on disk stays in the id-carrying form, whatever the page is shown.

    The two are for different readers: an engineer looking at the drawing wants "the
    position frame", and anyone reading the run folder wants ``fcf_1``.
    """
    created = upload(client)
    run_id = created["run_id"]
    client.post(f"/api/runs/{run_id}/check", json=created["drawing"])
    fake.corrections = [
        {
            "edits": [
                {
                    "op": "remove_datum_ref",
                    "fcf_id": "fcf_1",
                    "label": "C",
                    "rationale": "A and B already locate the pattern.",
                    "finding_ids": ["GDT-001@fcf_1"],
                }
            ]
        }
    ]
    body = client.post(f"/api/runs/{run_id}/fix").json()

    saved = json.loads((tmp_path / run_id / "corrections.json").read_text())
    applied = saved["rounds"][0]["applied"][0]
    assert applied["description"] == "remove_datum_ref  datum C from frame fcf_1"
    assert applied["fcf_id"] == "fcf_1"
    assert body["rounds"][0]["applied"][0]["edit"] == "Remove datum C from the position frame"


def test_meta_json_never_holds_a_key(client: TestClient, tmp_path):
    """The run's metadata names the provider and model, and carries no secret."""
    created = upload(client)
    meta = json.loads((tmp_path / created["run_id"] / "meta.json").read_text())
    assert set(meta) == {
        "run_id",
        "source",
        "provider",
        "model",
        "answered_provider",
        "fell_back",
        "fallback_reason",
        "latency_s",
        "attempts",
        "created",
    }
    assert "key" not in json.dumps(meta).lower()


def test_edited_data_changes_the_findings(client: TestClient):
    """Defining datum C in the review screen clears GDT-001. The user's data is what runs."""
    created = upload(client)
    run_id = created["run_id"]

    as_read = client.post(f"/api/runs/{run_id}/check", json=created["drawing"])
    assert "GDT-001" in rule_ids(as_read.json()["findings"])

    edited = created["drawing"]
    edited["datums"].append(
        {"label": "C", "location": "top edge of the plate", "bbox": [0.5, 0.8, 0.55, 0.85]}
    )
    after = client.post(f"/api/runs/{run_id}/check", json=edited)

    assert after.status_code == 200, after.text
    assert "GDT-001" not in rule_ids(after.json()["findings"])
    assert after.json()["error_count"] == 0


def test_review_screen_payload_clears_gdt_001(client: TestClient):
    """The body the review screen builds is accepted whole, and the edits are what run.

    Shaped exactly as ``app.js`` posts it: the extraction with the user's edits written
    into it and every other field passed through, an added datum carrying an id the page
    chose rather than one the server will have to invent, and the frame's datum
    references rebuilt from the "A|B|C" cell. Defining C clears the undefined-datum
    error, and the untouched fields come back unchanged.
    """
    created = upload(client)
    run_id = created["run_id"]
    drawing = created["drawing"]

    as_read = client.post(f"/api/runs/{run_id}/check", json=drawing)
    assert "GDT-001" in rule_ids(as_read.json()["findings"])

    # Datum ids DT1 and DT2 were assigned at extraction, so the page picks DT3.
    drawing["datums"].append({"id": "DT3", "label": "C", "location": None, "bbox": None})
    # An edited number and an edited frame, to prove the rest of the body is read too.
    drawing["dimensions"][0]["tol_plus"] = 0.2
    drawing["fcfs"][0]["datum_refs"] = [
        {"label": "A", "modifier": None},
        {"label": "B", "modifier": None},
        {"label": "C", "modifier": None},
    ]

    response = client.post(f"/api/runs/{run_id}/check", json=drawing)
    assert response.status_code == 200, response.text
    body = response.json()

    assert "GDT-001" not in rule_ids(body["findings"])
    assert body["parse_issues"] == []
    # The findings name items by id, which is what the page highlights on.
    for finding in body["findings"]:
        assert finding["item_ids"]


def test_check_rewrites_confirmed_json(client: TestClient, tmp_path):
    """A second confirmation replaces the first, so /fix always uses the latest data."""
    created = upload(client)
    run_id = created["run_id"]
    edited = created["drawing"]
    edited["datums"].append({"label": "C", "location": "top edge"})

    client.post(f"/api/runs/{run_id}/check", json=created["drawing"])
    client.post(f"/api/runs/{run_id}/check", json=edited)

    confirmed = json.loads((tmp_path / run_id / "confirmed.json").read_text())
    assert [datum["label"] for datum in confirmed["datums"]] == ["A", "B", "C"]


def test_bad_file_type_is_415(client: TestClient):
    response = client.post(
        "/api/runs",
        files={"file": ("notes.txt", b"not a drawing", "text/plain")},
    )
    assert response.status_code == 415
    assert ".pdf" in response.json()["detail"]


def test_too_large_is_413(client: TestClient, tmp_path):
    """An oversized upload is refused, and leaves no half-written run behind."""
    oversized = b"\x89PNG" + b"0" * (10 * 1024 * 1024 + 1)
    response = client.post(
        "/api/runs",
        files={"file": ("huge.png", oversized, "image/png")},
    )
    assert response.status_code == 413
    assert "10 MB" in response.json()["detail"]
    assert list(tmp_path.iterdir()) == []


def test_unparseable_drawing_is_400(client: TestClient):
    """A file named .pdf that is not one fails at the render, not inside the model call."""
    response = client.post(
        "/api/runs",
        files={"file": ("broken.pdf", b"certainly not a pdf", "application/pdf")},
    )
    assert response.status_code == 400
    assert "Could not read the drawing" in response.json()["detail"]


def test_unknown_provider_is_422(client: TestClient):
    with DRAWING.open("rb") as handle:
        response = client.post(
            "/api/runs",
            files={"file": (DRAWING.name, handle, "application/pdf")},
            data={"provider": "acme"},
        )
    assert response.status_code == 422
    assert "baseten" in response.json()["detail"]


def test_unknown_run_is_404(client: TestClient):
    """A well-formed id for a run that does not exist."""
    assert client.get(f"/api/runs/{UNKNOWN_RUN}/page.png").status_code == 404
    assert client.post(f"/api/runs/{UNKNOWN_RUN}/check", json={}).status_code == 404
    assert client.post(f"/api/runs/{UNKNOWN_RUN}/fix").status_code == 404
    assert client.get(f"/api/runs/{UNKNOWN_RUN}/redline.pdf").status_code == 404


def test_malformed_run_id_never_reaches_the_disk(client: TestClient):
    """Anything that is not a UUID is refused by the path type before a file is opened.

    A traversal attempt is refused either way: ``..`` is collapsed by the URL before it is
    routed, so it misses every route and 404s, and anything that does reach the endpoint
    has to parse as a UUID first.
    """
    assert client.get("/api/runs/not-a-uuid/page.png").status_code == 422
    assert client.get("/api/runs/00000000/page.png").status_code == 422
    for run_id in ("..", "%2e%2e%2f%2e%2e%2fetc", "../../../../etc/passwd"):
        response = client.get(f"/api/runs/{run_id}/page.png")
        assert response.status_code in (404, 422), run_id


def test_fix_before_check_is_409(client: TestClient):
    created = upload(client)
    response = client.post(f"/api/runs/{created['run_id']}/fix")
    assert response.status_code == 409
    assert "check" in response.json()["detail"]


def test_redline_before_fix_is_404(client: TestClient):
    created = upload(client)
    run_id = created["run_id"]
    client.post(f"/api/runs/{run_id}/check", json=created["drawing"])

    response = client.get(f"/api/runs/{run_id}/redline.pdf")
    assert response.status_code == 404
    assert "fix" in response.json()["detail"]


def test_check_rejects_a_body_that_is_not_an_object(client: TestClient):
    """A body that is not a drawing at all is a 422. A *bad field* inside one is not —
    parsing is lenient on purpose, and that case is covered below."""
    created = upload(client)
    response = client.post(f"/api/runs/{created['run_id']}/check", json=["not", "a", "drawing"])
    assert response.status_code == 422


def test_check_reports_parse_issues_without_failing(client: TestClient):
    """One unreadable callout is dropped and named, and the rest of the drawing is checked."""
    created = upload(client)
    edited = created["drawing"]
    edited["fcfs"].append({"characteristic": "wobbliness", "tolerance": 0.2})

    response = client.post(f"/api/runs/{created['run_id']}/check", json=edited)

    assert response.status_code == 200
    assert len(response.json()["parse_issues"]) == 1
    assert "GDT-001" in rule_ids(response.json()["findings"])


def local_client(tmp_path: Path) -> TestClient:
    """A client whose requests arrive from loopback, as a browser on this machine does.

    The default ``TestClient`` says its host is ``"testclient"``, which is not loopback —
    so the demo's second gate is exercised for real here rather than overridden away.
    """
    app.dependency_overrides[get_runs_root] = lambda: tmp_path
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def demo_on(demo_off_by_default: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the demo route on for one test, and only for that test.

    Depends on the autouse fixture by name so the order is stated rather than relied on:
    the variable is cleared first, then set.
    """
    monkeypatch.setenv(DEMO_ENV_VAR, "1")


@pytest.fixture(autouse=True)
def demo_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this file sees the variable unless it asked for it.

    Autouse, because the variable is a real one a developer may well have exported in
    the shell they are running pytest from — and a test that passes because of that
    would be testing their environment rather than the code.
    """
    monkeypatch.delenv(DEMO_ENV_VAR, raising=False)


def test_demo_run_is_served_when_enabled_and_local(tmp_path: Path, demo_on: None):
    """Both gates open: the demo makes a real run with two items under 0.8."""
    with local_client(tmp_path) as client:
        response = client.post("/api/demo/low-confidence")
    app.dependency_overrides.clear()

    assert response.status_code == 201
    body = response.json()
    drawing = body["drawing"]

    low = [
        item
        for key in ("datums", "dimensions", "fcfs")
        for item in drawing[key]
        if item["confidence"] is not None and item["confidence"] < 0.8
    ]
    assert len(low) == 2
    assert {item["confidence"] for item in low} == {0.6}

    # A real run, not a payload: the page it points at is on disk, so Check works on it.
    assert (tmp_path / body["run_id"] / "page.png").is_file()
    assert (tmp_path / body["run_id"] / "extraction.json").is_file()


def test_demo_run_can_then_be_checked(tmp_path: Path, demo_on: None):
    """The demo goes through the rest of the flow like any uploaded drawing."""
    with local_client(tmp_path) as client:
        created = client.post("/api/demo/low-confidence").json()
        page = client.get(created["page_url"])
        checked = client.post(f"/api/runs/{created['run_id']}/check", json=created["drawing"])
    app.dependency_overrides.clear()

    assert page.status_code == 200
    assert checked.status_code == 200
    # The fixture is the spike extraction, which carries one seeded GD&T error.
    assert "GDT-001" in rule_ids(checked.json()["findings"])


def test_demo_run_is_off_unless_the_variable_is_set(tmp_path: Path):
    """Local but not switched on: the route does not exist. This is the default."""
    with local_client(tmp_path) as client:
        response = client.post("/api/demo/low-confidence")
    app.dependency_overrides.clear()

    assert response.status_code == 404
    # Nothing was created on the way to refusing.
    assert list(tmp_path.iterdir()) == []


def test_demo_run_is_not_served_to_a_caller_elsewhere(client: TestClient, demo_on: None):
    """Switched on, but off this machine: still 404. One gate open is not enough."""
    response = client.post("/api/demo/low-confidence")
    assert response.status_code == 404


def test_demo_run_ignores_a_variable_set_to_something_else(tmp_path: Path, monkeypatch):
    """Only "1" turns it on, so a stray "0" or "false" cannot read as "yes"."""
    for value in ("0", "false", "true", "yes", ""):
        monkeypatch.setenv(DEMO_ENV_VAR, value)
        with local_client(tmp_path) as client:
            response = client.post("/api/demo/low-confidence")
        app.dependency_overrides.clear()
        assert response.status_code == 404, f"{value!r} should not enable the demo"


def test_static_page_and_its_two_files_are_served(client: TestClient):
    """The page is at /, and the stylesheet and script it links to are reachable.

    The page is three separate files, so a working index.html is not enough: a typo in
    either href leaves a blank, unstyled page in the browser and a passing test here.
    """
    page = client.get("/")
    assert page.status_code == 200
    assert "DraftCheck" in page.text
    assert 'src="/app.js"' in page.text
    assert 'href="/style.css"' in page.text

    # The upload area is the drop zone. app.js looks this id up at load time and attaches
    # click and keyboard to it (drops are taken anywhere on the desk), so renaming it
    # would leave an empty state that cannot take a file.
    assert 'id="dropzone"' in page.text
    assert 'id="desk"' in page.text

    # The overlay on the page image and its toggle. The overlay is an SVG in image pixels
    # with a layer for the linked-highlight boxes and one for markup (step 19b).
    assert '<svg id="overlay"' in page.text
    assert 'id="boxes"' in page.text
    assert 'id="markup"' in page.text
    assert 'id="show-overlay"' in page.text

    # One primary button and one Cancel drive every stage; the stepper and the model pill
    # report where the run is. app.js looks all of these up at load time.
    assert 'id="primary"' in page.text
    assert 'id="cancel"' in page.text
    assert 'id="stepper"' in page.text
    assert 'id="model-pill"' in page.text
    assert 'id="toasts"' in page.text

    # The three tabs, and the Corrections tab's empty state.
    for tab in ("tab-extracted", "tab-issues", "tab-corrections"):
        assert f'id="{tab}"' in page.text
    assert "Corrections appear here once the agent finishes its rounds." in page.text

    # The agent half (step 19b): the rounds strip in Issues, and in Corrections the
    # before/after table and the two exports. The redline itself is drawn on the sheet.
    assert 'id="rounds-strip"' in page.text
    assert 'id="agent-panel"' in page.text
    assert 'id="corrections-rows"' in page.text
    assert 'id="redline-pdf"' in page.text
    assert 'id="export-csv"' in page.text

    # Said on the page itself rather than built in JavaScript, so it is there whatever
    # the agent returns.
    assert "verified by DraftCheck's rule engine" in page.text
    assert "engineer review." in page.text
    assert "Decided by re-running the rules, not by the model." in page.text

    script = client.get("/app.js")
    assert script.status_code == 200
    assert "/api/runs" in script.text
    assert "/samples/plate_001.pdf" in script.text

    # The drawing primitives load before app.js, which uses them.
    assert page.text.index('src="/sheet.js"') < page.text.index('src="/app.js"')
    sheet = client.get("/sheet.js")
    assert sheet.status_code == 200
    assert "const Sheet" in sheet.text

    stylesheet = client.get("/style.css")
    assert stylesheet.status_code == 200
    assert "DraftCheck" in stylesheet.text

    # Every data table sits in a scroller, and the controls inside it are sized from their
    # own value by app.js. Without either, a narrow window squeezes the columns until "100"
    # reads as "10" — which looks like data rather than like a layout problem.
    assert 'class="table-scroll"' in page.text
    assert "fitControl" in script.text
    assert ".table-scroll" in stylesheet.text

    # The confidence markings: the line above the tables, the threshold behind it, and
    # the amber classes both halves of a flagged item wear.
    assert 'id="confidence-summary"' in page.text
    assert "CONFIDENCE_THRESHOLD" in script.text
    assert ".obox.is-unsure" in stylesheet.text
    assert ".unsure" in stylesheet.text

    # Paper cannot scroll, so the print rules are what stop a wide table being cut at the
    # page edge instead of carried to the scrollbar.
    assert "@media print" in stylesheet.text

    # The page hides the spinner, the error box, and the results by setting `hidden`.
    # Several of them are `display: flex`, which outranks the browser's own rule for
    # `[hidden]` — so without this one declaration the spinner never goes away.
    assert "[hidden]" in stylesheet.text
    assert "display: none !important" in stylesheet.text


# ---- Demo limits -------------------------------------------------------------
#
# The numbers live in `draftcheck.limits` and are tested there against a clock this test
# can move. What is tested here is the HTTP behaviour: which request gets a 429, what it
# says, and which address it was counted against.


def limited(tmp_path: Path, fake: FakeClient, limits: Limits):
    """A TestClient with real (small) limits, so a few requests reach them."""
    return make_client(tmp_path, fake, UsageLimiter(limits), Cleaner(limits))


def upload_response(client: TestClient, **headers):
    """Post the drawing and return the raw response, accepted or not."""
    with DRAWING.open("rb") as handle:
        return client.post(
            "/api/runs",
            files={"file": (DRAWING.name, handle, "application/pdf")},
            headers=headers or None,
        )


def test_a_sixth_upload_in_an_hour_is_refused(tmp_path: Path, fake: FakeClient):
    """Five uploads an hour per address; the sixth is a 429 with the demo's message."""
    with limited(tmp_path, fake, Limits()) as client:
        for _ in range(5):
            assert upload_response(client).status_code == 201

        refused = upload_response(client)
        assert refused.status_code == 429
        assert refused.json()["detail"] == LIMIT_MESSAGE
        # The sixth upload cost nothing: no folder, and the model was never asked.
        assert len(list(tmp_path.iterdir())) == 5
        assert fake.calls == ["extract"] * 5
    app.dependency_overrides.clear()


def test_a_refused_upload_does_not_spend_a_slot(tmp_path: Path, fake: FakeClient):
    """A wrong file type is rejected before the claim, so it is not one of the five."""
    limits = Limits(uploads_per_ip_per_hour=1)
    with limited(tmp_path, fake, limits) as client:
        bad = client.post("/api/runs", files={"file": ("notes.txt", b"hello", "text/plain")})
        assert bad.status_code == 415
        assert upload_response(client).status_code == 201
    app.dependency_overrides.clear()


def test_the_fix_route_has_its_own_hourly_allowance(tmp_path: Path, fake: FakeClient):
    limits = Limits(uploads_per_ip_per_hour=50, fixes_per_ip_per_hour=1, model_calls_per_day=50)
    with limited(tmp_path, fake, limits) as client:
        first = upload_response(client).json()["run_id"]
        second = upload_response(client).json()["run_id"]
        for run_id in (first, second):
            confirmed = client.post(f"/api/runs/{run_id}/check", json=extraction_data())
            assert confirmed.status_code == 200

        assert client.post(f"/api/runs/{first}/fix").status_code == 200
        refused = client.post(f"/api/runs/{second}/fix")
        assert refused.status_code == 429
        assert refused.json()["detail"] == LIMIT_MESSAGE
    app.dependency_overrides.clear()


def test_the_day_cap_stops_everyone(tmp_path: Path, fake: FakeClient):
    """Two different addresses, a budget of one model call: the second is refused."""
    limits = Limits(uploads_per_ip_per_hour=50, model_calls_per_day=1, trust_proxy=True)
    with limited(tmp_path, fake, limits) as client:
        assert upload_response(client, **{"X-Forwarded-For": "1.1.1.1"}).status_code == 201
        refused = upload_response(client, **{"X-Forwarded-For": "2.2.2.2"})
        assert refused.status_code == 429
        assert refused.json()["detail"] == LIMIT_MESSAGE
    app.dependency_overrides.clear()


def test_forwarded_for_counts_only_when_the_proxy_is_trusted(tmp_path: Path, fake: FakeClient):
    """With TRUST_PROXY on, two headers are two visitors; with it off, they are one."""
    trusting = Limits(uploads_per_ip_per_hour=1, model_calls_per_day=50, trust_proxy=True)
    with limited(tmp_path, fake, trusting) as client:
        assert upload_response(client, **{"X-Forwarded-For": "1.1.1.1"}).status_code == 201
        assert upload_response(client, **{"X-Forwarded-For": "2.2.2.2"}).status_code == 201
        assert upload_response(client, **{"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    app.dependency_overrides.clear()

    plain = Limits(uploads_per_ip_per_hour=1, model_calls_per_day=50, trust_proxy=False)
    with limited(tmp_path, fake, plain) as client:
        assert upload_response(client, **{"X-Forwarded-For": "1.1.1.1"}).status_code == 201
        # Same connection, a new header, and no reason to believe it: still one visitor.
        assert upload_response(client, **{"X-Forwarded-For": "2.2.2.2"}).status_code == 429
    app.dependency_overrides.clear()


def test_healthz_answers_without_a_model(client: TestClient, fake: FakeClient):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert fake.calls == []


def test_startup_sweeps_run_folders_older_than_a_day(tmp_path: Path, fake: FakeClient):
    """Entering the app's lifespan clears the backlog and leaves today's runs alone."""
    old = tmp_path / "11111111-1111-4111-8111-111111111111"
    recent = tmp_path / "22222222-2222-4222-8222-222222222222"
    for folder in (old, recent):
        folder.mkdir()
        (folder / "page.png").write_bytes(b"\x89PNG")
    stale = time.time() - 30 * 3600
    os.utime(old, (stale, stale))

    with limited(tmp_path, fake, Limits()):
        pass
    app.dependency_overrides.clear()

    assert not old.exists()
    assert (recent / "page.png").is_file()


def test_sample_drawing_is_served_for_the_sample_button(client: TestClient):
    """The page's "Try the sample drawing" fetches this, then uploads it as usual."""
    response = client.get("/samples/plate_001.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == DRAWING.read_bytes()


def test_only_the_sample_pdf_is_served_from_samples(client: TestClient):
    """The answer key beside the sample must not be reachable from the page."""
    assert client.get("/samples/plate_001.gt.json").status_code == 404
    assert client.get("/samples/plate_001.png").status_code == 404


def test_check_findings_carry_their_rule_title(client: TestClient):
    """Each finding comes back with its rule's short name, for the Issues card heading."""
    created = upload(client)
    checked = client.post(f"/api/runs/{created['run_id']}/check", json=created["drawing"])
    findings = checked.json()["findings"]
    undefined = next(f for f in findings if f["rule_id"] == "GDT-001")
    assert undefined["title"] == "Undefined datum"


def test_finding_payload_falls_back_to_the_rule_id_for_an_unknown_rule() -> None:
    finding = Finding(rule_id="XYZ-999", severity="warning", message="m", source="s")
    payload = finding_payload(finding)
    assert payload["title"] == "XYZ-999"
    assert payload["message"] == "m"


# ---- The development fake model ---------------------------------------------------


def as_request(host: str | None) -> SimpleNamespace:
    """Just enough of a request for the gate: where it came from."""
    return SimpleNamespace(client=SimpleNamespace(host=host) if host else None)


@pytest.fixture(autouse=True)
def fake_model_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test sees the variable unless it asks for it, whatever the shell exported."""
    monkeypatch.delenv(FAKE_MODEL_ENV_VAR, raising=False)


def test_fake_model_is_off_without_the_variable() -> None:
    assert fake_model_is_allowed(as_request("127.0.0.1")) is False
    assert get_client_factory(as_request("127.0.0.1")) is not fake_client_factory


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_fake_model_needs_the_variable_to_be_exactly_one(monkeypatch, value: str) -> None:
    monkeypatch.setenv(FAKE_MODEL_ENV_VAR, value)
    assert fake_model_is_allowed(as_request("127.0.0.1")) is False


@pytest.mark.parametrize("host", ["203.0.113.7", "testclient", None])
def test_fake_model_is_off_for_a_request_from_elsewhere(monkeypatch, host) -> None:
    """With the variable set, a request that is not from this machine still gets a real model."""
    monkeypatch.setenv(FAKE_MODEL_ENV_VAR, "1")
    assert fake_model_is_allowed(as_request(host)) is False
    assert get_client_factory(as_request(host)) is not fake_client_factory


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_fake_model_is_on_only_with_both_gates(monkeypatch, host: str) -> None:
    monkeypatch.setenv(FAKE_MODEL_ENV_VAR, "1")
    assert fake_model_is_allowed(as_request(host)) is True
    assert get_client_factory(as_request(host)) is fake_client_factory


def test_fake_model_runs_the_real_check_fix_and_redline(tmp_path: Path, monkeypatch) -> None:
    """Upload, check, and fix through the fake: rules, loop, and redline all run for real."""
    monkeypatch.setenv(FAKE_MODEL_ENV_VAR, "1")
    monkeypatch.setattr("draftcheck.fake_model.FAKE_DELAY_S", 0)
    app.dependency_overrides[get_limiter] = lambda: UsageLimiter(ROOMY)
    app.dependency_overrides[get_cleaner] = lambda: Cleaner(ROOMY)
    app.dependency_overrides[get_fallback_policy] = lambda: NO_FALLBACK
    try:
        with local_client(tmp_path) as client:
            created = upload(client)
            assert created["fake_model"] is True
            assert created["model"] == FAKE_MODEL_NAME

            run_id = created["run_id"]
            checked = client.post(f"/api/runs/{run_id}/check", json=created["drawing"]).json()
            assert checked["error_count"] == 1

            fixed = client.post(f"/api/runs/{run_id}/fix")
            assert fixed.status_code == 200, fixed.text
            body = fixed.json()
            assert body["fake_model"] is True
            assert body["resolved_count"] == 1
            assert client.get(body["redline_pdf_url"]).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_model_mode_says_fake_only_with_both_gates(tmp_path: Path, monkeypatch) -> None:
    """The page's badge on load: on for loopback with the variable, off otherwise."""
    monkeypatch.setenv(FAKE_MODEL_ENV_VAR, "1")
    try:
        with local_client(tmp_path) as local:
            assert local.get("/api/model-mode").json() == {"fake_model": True}
        with TestClient(app) as remote:  # host "testclient": not loopback
            assert remote.get("/api/model-mode").json() == {"fake_model": False}
        monkeypatch.delenv(FAKE_MODEL_ENV_VAR)
        with local_client(tmp_path) as local:
            assert local.get("/api/model-mode").json() == {"fake_model": False}
    finally:
        app.dependency_overrides.clear()


def test_a_real_run_says_it_is_not_fake(client: TestClient):
    """The page's badge reads this flag, so a normal run must say ``false``."""
    assert upload(client)["fake_model"] is False


def test_the_page_says_usage_is_limited(client: TestClient):
    """Told up front, in the page itself rather than only in a 429."""
    page = client.get("/")
    assert "Demo usage is limited to keep it free for everyone." in page.text


# ---- Render settings and the upload's log lines --------------------------------


def test_render_settings_default_to_the_small_server_values(monkeypatch) -> None:
    monkeypatch.delenv(ENV_RENDER_DPI, raising=False)
    monkeypatch.delenv(ENV_MAX_SIDE, raising=False)
    assert render_settings() == (DEFAULT_RENDER_DPI, DEFAULT_MAX_SIDE) == (150, 1800)


def test_render_settings_read_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(ENV_RENDER_DPI, "100")
    monkeypatch.setenv(ENV_MAX_SIDE, " 900 ")
    assert render_settings() == (100, 900)


@pytest.mark.parametrize("value", ["", "0", "-5", "1,800", "big"])
def test_unusable_render_settings_fall_back(monkeypatch, value: str) -> None:
    monkeypatch.setenv(ENV_RENDER_DPI, value)
    monkeypatch.setenv(ENV_MAX_SIDE, value)
    assert render_settings() == (DEFAULT_RENDER_DPI, DEFAULT_MAX_SIDE)


def test_upload_renders_page_one_once_at_the_server_settings(
    client: TestClient, fake: FakeClient, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(ENV_MAX_SIDE, "600")
    run_id = upload(client)["run_id"]

    from PIL import Image

    with Image.open(tmp_path / run_id / "page.png") as page:
        assert max(page.size) == 600
    assert fake.calls == ["extract"]


def test_upload_logs_each_stage_with_memory(client: TestClient, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="draftcheck"):
        upload(client)

    messages = [r.getMessage() for r in caplog.records if r.name.startswith("draftcheck")]
    assert [m.split(" (")[0] for m in messages] == [
        "upload received",
        "render done",
        "model call started",
        "model call done",
        "parse done",
    ]
    assert all("memory" in m and " MB" in m for m in messages)


# ---- Slow work runs off the event loop ------------------------------------------


class BlockingClient(FakeClient):
    """A fake whose model call waits until the test lets it go, like a slow real model."""

    def __init__(self, extraction: dict) -> None:
        super().__init__(extraction)
        self.in_call = threading.Event()
        self.release = threading.Event()
        self.blocking = True

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        if self.blocking:
            self.in_call.set()
            self.release.wait(timeout=30)
        return super().chat_json(system, user, images)


def _healthz_answers_during(client: TestClient, slow: BlockingClient, request) -> None:
    """Start ``request`` in a thread, and while the model call hangs, ask /healthz."""
    outcome: dict = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("response", request()))
    worker.start()
    try:
        assert slow.in_call.wait(timeout=10), "the fake model call never started"
        health: dict = {}
        prober = threading.Thread(target=lambda: health.setdefault("r", client.get("/healthz")))
        prober.start()
        prober.join(timeout=2)
        # Checked before the release: a blocked event loop answers only once the call ends.
        assert "r" in health, "/healthz did not answer while the model call was in progress"
        assert health["r"].status_code == 200
    finally:
        slow.release.set()
        worker.join(timeout=30)
    assert outcome["response"].status_code in (200, 201), outcome["response"].text


@pytest.fixture
def slow_client(tmp_path: Path):
    slow = BlockingClient(extraction_data())
    with make_client(tmp_path, slow, UsageLimiter(ROOMY), Cleaner(ROOMY)) as test_client:
        yield test_client, slow
    app.dependency_overrides.clear()


def test_healthz_answers_while_an_upload_waits_on_the_model(slow_client) -> None:
    client, slow = slow_client

    def post_upload():
        with DRAWING.open("rb") as handle:
            return client.post(
                "/api/runs", files={"file": (DRAWING.name, handle, "application/pdf")}
            )

    _healthz_answers_during(client, slow, post_upload)


def test_healthz_answers_while_a_fix_waits_on_the_model(slow_client) -> None:
    client, slow = slow_client
    slow.blocking = False
    created = upload(client)
    run_id = created["run_id"]
    assert client.post(f"/api/runs/{run_id}/check", json=created["drawing"]).status_code == 200
    slow.blocking = True

    _healthz_answers_during(client, slow, lambda: client.post(f"/api/runs/{run_id}/fix"))


# ---- The model-call time limit ---------------------------------------------------


def test_model_timeout_defaults_to_ninety_seconds(monkeypatch) -> None:
    monkeypatch.delenv(ENV_MODEL_TIMEOUT, raising=False)
    assert model_timeout_s() == DEFAULT_MODEL_TIMEOUT_S == 90


def test_model_timeout_reads_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(ENV_MODEL_TIMEOUT, "30")
    assert model_timeout_s() == 30


#: The limit the timeout tests run under, so a "too slow" call costs a fraction of a second.
SHORT_LIMIT_S = 0.2


class HangingClient(FakeClient):
    """A fake whose model calls, from number ``hang_from`` on, outlast any sane limit.

    It waits on an event rather than sleeping, so the test can let the abandoned call
    finish at teardown instead of leaving it running.
    """

    def __init__(self, extraction: dict, hang_from: int, *corrections: dict) -> None:
        super().__init__(extraction, *corrections)
        self.hang_from = hang_from
        self.release = threading.Event()

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        if len(self.calls) + 1 >= self.hang_from:
            self.release.wait(timeout=10)
        return super().chat_json(system, user, images)


@pytest.fixture
def hanging(tmp_path: Path):
    """Build a TestClient around a HangingClient under a short time limit."""
    made: list[HangingClient] = []

    def build(hang_from: int, *corrections: dict):
        slow = HangingClient(extraction_data(), hang_from, *corrections)
        made.append(slow)
        app.dependency_overrides[get_model_timeout] = lambda: SHORT_LIMIT_S
        return make_client(tmp_path, slow, UsageLimiter(ROOMY), Cleaner(ROOMY)), slow

    yield build
    for slow in made:
        slow.release.set()
    app.dependency_overrides.clear()


def test_an_upload_whose_model_call_outlasts_the_limit_is_504(hanging) -> None:
    test_client, _ = hanging(hang_from=1)
    started = time.perf_counter()
    with test_client, DRAWING.open("rb") as handle:
        response = test_client.post(
            "/api/runs", files={"file": (DRAWING.name, handle, "application/pdf")}
        )
    assert response.status_code == 504
    assert response.json()["detail"] == MODEL_TIMEOUT_MESSAGE
    # The request ended at the limit, not when the fake was eventually let go.
    assert time.perf_counter() - started < 5


def _checked_run(test_client: TestClient) -> str:
    created = upload(test_client)
    run_id = created["run_id"]
    assert test_client.post(f"/api/runs/{run_id}/check", json=created["drawing"]).status_code == 200
    return run_id


def test_a_fix_whose_first_round_times_out_is_504(hanging, tmp_path: Path) -> None:
    test_client, _ = hanging(hang_from=2)  # call 1 is the extraction, call 2 is round one
    with test_client:
        run_id = _checked_run(test_client)
        response = test_client.post(f"/api/runs/{run_id}/fix")
    assert response.status_code == 504
    assert response.json()["detail"] == MODEL_TIMEOUT_MESSAGE
    assert not (tmp_path / run_id / "redline.pdf").exists()


def test_a_fix_stops_at_a_timed_out_round_and_reports_the_rounds_before_it(hanging) -> None:
    # Round one proposes nothing, so the error is still there and round two is asked for —
    # and round two never answers in time.
    test_client, slow = hanging(3, {"edits": []})
    with test_client:
        run_id = _checked_run(test_client)
        response = test_client.post(f"/api/runs/{run_id}/fix")
    assert response.status_code == 200, response.text
    rounds = response.json()["rounds"]
    assert [entry["round"] for entry in rounds] == [1, 2]
    assert rounds[0]["error"] is None
    assert rounds[1]["error"] == MODEL_TIMEOUT_MESSAGE
    assert slow.calls == ["extract", "correct"]  # the hung call never got to record itself


# ---- Cancel: the browser goes away mid-call ---------------------------------------


class AbortableClient(FakeClient):
    """A slow fake model that, like ``LLMClient``, can be aborted from another thread.

    From call number ``hang_from`` on, each call waits until it is aborted (or the test
    gives up), and an aborted call raises ``ModelCancelled`` as the real client does.
    """

    def __init__(self, extraction: dict, hang_from: int) -> None:
        super().__init__(extraction)
        self.hang_from = hang_from
        self.in_call = threading.Event()
        self.aborted = threading.Event()
        self.attempted = 0

    def abort(self) -> None:
        self.aborted.set()

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        self.attempted += 1
        if self.attempted >= self.hang_from:
            self.in_call.set()
            if self.aborted.wait(timeout=10):
                raise ModelCancelled()
        return super().chat_json(system, user, images)


@pytest.fixture
def cancelling(tmp_path: Path):
    """A TestClient whose browser "disconnects" once the slow fake model call is under way.

    The test client cannot drop a connection mid-request, so the disconnect check is
    replaced with one that says the browser has gone as soon as the model is busy.
    """
    made: list[AbortableClient] = []

    def build(hang_from: int):
        slow = AbortableClient(extraction_data(), hang_from)
        made.append(slow)

        async def gone_once_the_model_is_busy(request) -> bool:
            return slow.in_call.is_set()

        app.dependency_overrides[get_disconnect_probe] = lambda: gone_once_the_model_is_busy
        return make_client(tmp_path, slow, UsageLimiter(ROOMY), Cleaner(ROOMY)), slow

    yield build
    for slow in made:
        slow.aborted.set()
    app.dependency_overrides.clear()


def test_a_cancelled_upload_aborts_the_model_call_and_saves_nothing(
    cancelling, tmp_path: Path, caplog
) -> None:
    test_client, slow = cancelling(hang_from=1)
    started = time.perf_counter()
    with caplog.at_level(logging.INFO, logger="draftcheck"), test_client:
        with DRAWING.open("rb") as handle:
            response = test_client.post(
                "/api/runs", files={"file": (DRAWING.name, handle, "application/pdf")}
            )

    assert response.status_code == 499
    assert slow.aborted.is_set(), "the model call was left running"
    assert time.perf_counter() - started < 5  # ended by the abort, not by the fake's own wait
    assert slow.calls == []  # the call never got as far as answering
    assert list(tmp_path.iterdir()) == []  # no run folder, no page, no extraction
    assert any("cancelled by client" in r.getMessage() for r in caplog.records)


def test_a_cancelled_fix_aborts_the_round_and_saves_nothing(cancelling, tmp_path: Path) -> None:
    test_client, slow = cancelling(hang_from=2)  # call 1 is the extraction, call 2 is round one
    with test_client:
        run_id = _checked_run(test_client)
        response = test_client.post(f"/api/runs/{run_id}/fix")

    assert response.status_code == 499
    assert slow.aborted.is_set()
    assert slow.attempted == 2  # no second round was started after the abort
    run_dir = tmp_path / run_id
    for name in ("corrections.json", "redline.pdf", "redline.png"):
        assert not (run_dir / name).exists(), name
    # What the run had before the fix is untouched.
    assert (run_dir / "confirmed.json").is_file()


def test_an_aborted_guard_refuses_the_next_call_without_asking_the_model(fake) -> None:
    """What stops the correction loop before its next round once the browser has gone."""
    guard = GuardedClient(fake, seconds=5)
    guard.abort()
    with pytest.raises(ModelCancelled):
        guard.chat_json("SYSTEM", "USER", [])
    assert fake.calls == []


def test_a_guard_returns_promptly_when_aborted_mid_call() -> None:
    slow = AbortableClient(extraction_data(), hang_from=1)
    guard = GuardedClient(slow, seconds=30)
    outcome: dict = {}

    def run() -> None:
        try:
            guard.chat_json(EXTRACTION_SYSTEM, "USER", [])
        except ModelCancelled as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    assert slow.in_call.wait(timeout=5)
    guard.abort()
    worker.join(timeout=2)
    assert isinstance(outcome.get("error"), ModelCancelled)
    assert slow.aborted.is_set()  # the abort reached the client underneath



def test_a_cancelled_request_does_not_slow_the_next_one(tmp_path: Path) -> None:
    """Aborting one request's client leaves the next request's client alone.

    Each request gets its own client from the factory, as in production. The first upload
    is cancelled mid-call; the second, sent straight after, must answer at once.
    """
    clients: list[AbortableClient] = []

    def factory(provider=None) -> AbortableClient:
        # Only the first client is slow; the next one answers immediately.
        made = AbortableClient(extraction_data(), hang_from=1 if not clients else 99)
        clients.append(made)
        return made

    async def gone_while_the_first_call_is_busy(request) -> bool:
        return clients[0].in_call.is_set() and not clients[0].aborted.is_set()

    with make_client(
        tmp_path, FakeClient(extraction_data()), UsageLimiter(ROOMY), Cleaner(ROOMY)
    ) as test_client:
        app.dependency_overrides[get_client_factory] = lambda: factory
        app.dependency_overrides[get_disconnect_probe] = lambda: gone_while_the_first_call_is_busy
        try:
            assert upload_response(test_client).status_code == 499
            started = time.perf_counter()
            second = upload_response(test_client)
            elapsed = time.perf_counter() - started
            leaked = clients[1].aborted.is_set()
        finally:
            for made in clients:
                made.aborted.set()  # let any still-waiting fake go
            app.dependency_overrides.clear()

    assert second.status_code == 201, second.text
    assert elapsed < 2
    assert len(clients) == 2
    assert not leaked, "the abort leaked into the next request's client"


# ---- Automatic fallback: Baseten stalls or fails, Meta answers --------------------


def test_fallback_policy_defaults_to_meta_after_45_seconds(monkeypatch) -> None:
    monkeypatch.delenv(ENV_FALLBACK_PROVIDER, raising=False)
    monkeypatch.delenv(ENV_PRIMARY_TIMEOUT, raising=False)
    assert fallback_policy() == FallbackPolicy(provider="meta", primary_timeout_s=45)


def test_an_empty_fallback_provider_turns_the_fallback_off(monkeypatch) -> None:
    monkeypatch.setenv(ENV_FALLBACK_PROVIDER, "")
    monkeypatch.setenv(ENV_PRIMARY_TIMEOUT, "20")
    assert fallback_policy() == FallbackPolicy(provider=None, primary_timeout_s=20)


def _http_error(status: int) -> Exception:
    """The error the OpenAI library raises for an HTTP status, built without a network."""
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request)
    error_class = InternalServerError if status >= 500 else BadRequestError
    return error_class(f"HTTP {status}", response=response, body=None)


class ScriptedProvider(FakeClient):
    """A fake provider whose calls follow a script, one step per call.

    ``"ok"`` answers; ``"hang"`` waits until aborted (or the test ends) and then raises
    ``ModelCancelled`` as the real client does; ``"5xx"``, ``"4xx"``, and ``"down"`` raise
    the errors the OpenAI library raises for a server error, a refusal, and no connection.
    After the script runs out, every call answers.
    """

    def __init__(self, name: str, model: str, *script: str, corrections=()) -> None:
        super().__init__(extraction_data(), *corrections)
        self.provider = SimpleNamespace(name=name)
        self.model = model
        self.script = list(script)
        self.attempted = 0
        self.in_call = threading.Event()
        self.aborted = threading.Event()

    def abort(self) -> None:
        self.aborted.set()

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        self.attempted += 1
        step = self.script.pop(0) if self.script else "ok"
        if step == "hang":
            self.in_call.set()
            self.aborted.wait(timeout=10)
            raise ModelCancelled()
        if step == "5xx":
            raise _http_error(503)
        if step == "4xx":
            raise _http_error(400)
        if step == "down":
            raise APIConnectionError(request=httpx.Request("POST", "https://provider.invalid"))
        return dataclasses.replace(super().chat_json(system, user, images), model=self.model)


BASETEN_MODEL = "meta-models/muse-spark-open-1026"
META_MODEL = "muse-spark-1.2"


@pytest.fixture
def providers(tmp_path: Path):
    """A TestClient with a scripted Baseten and a scripted Meta, and the fallback on.

    Baseten gets a fraction of a second before the fallback; the normal limit, which the
    fallback call runs under, is long enough that it never ends a test by itself.
    """
    made: dict = {}

    def build(baseten_script=(), meta_script=(), corrections=(), policy=None):
        made["baseten"] = ScriptedProvider(
            "baseten", BASETEN_MODEL, *baseten_script, corrections=corrections
        )
        made["meta"] = ScriptedProvider("meta", META_MODEL, *meta_script, corrections=corrections)
        made["asked_for"] = []
        limiter = UsageLimiter(ROOMY)

        def factory(provider=None):
            name = provider or "baseten"
            made["asked_for"].append(name)
            return made[name]

        app.dependency_overrides[get_fallback_policy] = lambda: policy or FallbackPolicy(
            provider="meta", primary_timeout_s=SHORT_LIMIT_S
        )
        app.dependency_overrides[get_model_timeout] = lambda: 5.0
        test_client = make_client(tmp_path, FakeClient(extraction_data()), limiter, Cleaner(ROOMY))
        app.dependency_overrides[get_client_factory] = lambda: factory
        return test_client, made, limiter

    yield build
    for key in ("baseten", "meta"):
        if key in made:
            made[key].aborted.set()  # let any still-waiting fake go
    app.dependency_overrides.clear()


def test_a_stalled_baseten_call_falls_back_to_meta(providers, tmp_path, caplog) -> None:
    test_client, made, limiter = providers(baseten_script=["hang"])
    started = time.perf_counter()
    with caplog.at_level(logging.INFO, logger="draftcheck"), test_client:
        body = upload(test_client)
    assert time.perf_counter() - started < 5  # ended by the short limit, not the fake

    assert body["model"] == META_MODEL
    assert body["answered_provider"] == "meta"
    assert body["fell_back"] is True
    assert body["fallback_reason"] == "timeout"
    meta = json.loads((tmp_path / body["run_id"] / "meta.json").read_text())
    assert meta["model"] == META_MODEL
    assert meta["fell_back"] is True
    assert meta["fallback_reason"] == "timeout"
    # The run still belongs to Baseten, so the fix tries Baseten first again.
    assert meta["provider"] == "baseten"
    assert any(
        r.getMessage().startswith("fallback: baseten -> meta after ")
        and r.getMessage().endswith(" s (timeout)")
        for r in caplog.records
    )
    # The upload's own call plus the fallback.
    assert limiter.model_calls_today() == 2


def test_a_baseten_server_error_falls_back_to_meta(providers) -> None:
    test_client, made, limiter = providers(baseten_script=["5xx"])
    with test_client:
        body = upload(test_client)
    assert body["fell_back"] is True
    assert body["fallback_reason"] == "HTTP 503"
    assert body["model"] == META_MODEL
    assert limiter.model_calls_today() == 2


def test_a_baseten_connection_error_falls_back_to_meta(providers) -> None:
    test_client, _, _ = providers(baseten_script=["down"])
    with test_client:
        body = upload(test_client)
    assert body["fell_back"] is True
    assert body["fallback_reason"] == "connection error"


def test_a_baseten_answer_needs_no_fallback(providers) -> None:
    test_client, made, limiter = providers()
    with test_client:
        body = upload(test_client)
    assert body["model"] == BASETEN_MODEL
    assert body["answered_provider"] == "baseten"
    assert body["fell_back"] is False
    assert body["fallback_reason"] is None
    assert made["asked_for"] == ["baseten"]  # Meta's client was never even built
    assert limiter.model_calls_today() == 1


def test_a_baseten_refusal_is_not_retried(providers) -> None:
    """A 4xx means our request was refused. Meta would refuse it too, so it is not asked."""
    test_client, made, limiter = providers(baseten_script=["4xx"])
    with test_client, DRAWING.open("rb") as handle:
        response = test_client.post(
            "/api/runs", files={"file": (DRAWING.name, handle, "application/pdf")}
        )
    assert response.status_code == 502
    assert "HTTP 400" in response.json()["detail"]
    assert made["meta"].attempted == 0
    assert made["asked_for"] == ["baseten"]
    assert limiter.model_calls_today() == 1


def test_with_the_fallback_off_a_stalled_call_is_504_as_before(providers) -> None:
    test_client, made, _ = providers(
        baseten_script=["hang"], policy=FallbackPolicy(provider=None, primary_timeout_s=45)
    )
    app.dependency_overrides[get_model_timeout] = lambda: SHORT_LIMIT_S
    with test_client, DRAWING.open("rb") as handle:
        response = test_client.post(
            "/api/runs", files={"file": (DRAWING.name, handle, "application/pdf")}
        )
    assert response.status_code == 504
    assert response.json()["detail"] == MODEL_TIMEOUT_MESSAGE
    assert made["meta"].attempted == 0


def test_picking_meta_means_no_fallback(providers) -> None:
    test_client, made, _ = providers(meta_script=["hang"])
    app.dependency_overrides[get_model_timeout] = lambda: SHORT_LIMIT_S
    with test_client, DRAWING.open("rb") as handle:
        response = test_client.post(
            "/api/runs",
            files={"file": (DRAWING.name, handle, "application/pdf")},
            data={"provider": "meta"},
        )
    assert response.status_code == 504
    assert made["baseten"].attempted == 0


def test_a_backup_that_cannot_be_built_returns_the_original_error(providers) -> None:
    test_client, made, _ = providers(baseten_script=["5xx"])

    def factory(provider=None):
        if provider == "meta":
            raise ConfigError("Missing required environment variable: META_API_KEY.")
        return made["baseten"]

    app.dependency_overrides[get_client_factory] = lambda: factory
    with test_client, DRAWING.open("rb") as handle:
        response = test_client.post(
            "/api/runs", files={"file": (DRAWING.name, handle, "application/pdf")}
        )
    assert response.status_code == 502
    assert "HTTP 503" in response.json()["detail"]


def test_cancel_during_the_fallback_call_aborts_it(providers, tmp_path: Path) -> None:
    test_client, made, _ = providers(baseten_script=["hang"], meta_script=["hang"])

    async def gone_once_meta_is_busy(request) -> bool:
        return made["meta"].in_call.is_set()

    app.dependency_overrides[get_disconnect_probe] = lambda: gone_once_meta_is_busy
    started = time.perf_counter()
    with test_client:
        response = upload_response(test_client)

    assert response.status_code == 499
    assert made["meta"].aborted.is_set(), "the fallback call was left running"
    assert made["baseten"].aborted.is_set()
    assert time.perf_counter() - started < 5
    assert list(tmp_path.iterdir()) == []  # a cancelled upload leaves nothing


def test_an_aborted_guard_refuses_to_build_the_backup() -> None:
    """A cancel that lands while Baseten is failing stops the fallback before it starts."""
    baseten = ScriptedProvider("baseten", BASETEN_MODEL, "5xx")
    meta = ScriptedProvider("meta", META_MODEL)
    guard = None

    def build_meta():
        guard.abort()  # the browser leaves just as the backup is being made
        return meta

    guard = GuardedClient(
        baseten,
        5,
        provider="baseten",
        fallback=Fallback(provider="meta", build=build_meta, primary_seconds=5),
    )
    with pytest.raises(ModelCancelled):
        guard.chat_json(EXTRACTION_SYSTEM, "USER", [])
    assert meta.attempted == 0
    assert meta.aborted.is_set()


def test_each_fix_round_falls_back_on_its_own(providers, tmp_path: Path) -> None:
    # Call 1 is the extraction (Baseten answers), call 2 is round one (Baseten stalls, so
    # Meta answers it), and rounds two and three go to Baseten first again — and it answers.
    # Every round proposes nothing, so the GDT-001 error keeps the loop going to round three.
    test_client, made, limiter = providers(baseten_script=["ok", "hang"])
    with test_client:
        run_id = _checked_run(test_client)
        assert limiter.model_calls_today() == 1
        response = test_client.post(f"/api/runs/{run_id}/fix")
    assert response.status_code == 200, response.text
    body = response.json()

    rounds = body["rounds"]
    assert [r["model"] for r in rounds] == [META_MODEL, BASETEN_MODEL, BASETEN_MODEL]
    assert [r["fell_back"] for r in rounds] == [True, False, False]
    assert rounds[0]["fallback_reason"] == "timeout"
    assert rounds[1]["fallback_reason"] is None
    assert body["fell_back"] is True
    assert body["model"] == BASETEN_MODEL

    saved = json.loads((tmp_path / run_id / "corrections.json").read_text())
    assert [r["provider"] for r in saved["rounds"]] == ["meta", "baseten", "baseten"]

    # Extraction 1; fix claim 1, two more rounds, and the fallback: 5 in all.
    assert limiter.model_calls_today() == 5


def test_the_stamp_names_every_model_that_answered() -> None:
    record = SimpleNamespace(model=META_MODEL, fell_back=True)
    report = SimpleNamespace(rounds=[record, SimpleNamespace(model=BASETEN_MODEL, fell_back=False)])
    name = stamp_model_name({"model": BASETEN_MODEL, "fell_back": False}, report)
    assert name == f"{BASETEN_MODEL} + {META_MODEL} (fallback)"


def test_the_stamp_is_unchanged_when_nothing_fell_back() -> None:
    report = SimpleNamespace(rounds=[SimpleNamespace(model=BASETEN_MODEL, fell_back=False)])
    assert stamp_model_name({"model": BASETEN_MODEL}, report) == BASETEN_MODEL


# --- what the page draws for each edit (step 19b) ----------------------------------------


def checked_run(client: TestClient) -> str:
    """Upload and check the fixture drawing; returns the run id."""
    created = upload(client)
    run_id = created["run_id"]
    client.post(f"/api/runs/{run_id}/check", json=created["drawing"])
    return run_id


def test_check_names_each_finding_and_counts_the_rules(client: TestClient):
    """The page matches a cloud to its /fix verdict by key, and says how many rules ran."""
    created = upload(client)
    body = client.post(f"/api/runs/{created['run_id']}/check", json=created["drawing"]).json()

    assert [finding["key"] for finding in body["findings"]] == ["GDT-001@fcf_1"]
    assert body["rules_run"] == len(RULES)


def test_an_edit_that_adds_a_feature_is_held_for_the_designer(
    client: TestClient, fake: FakeClient
):
    """Adding datum C clears GDT-001, but only a designer can say where C really is."""
    run_id = checked_run(client)
    fake.corrections = [
        {
            "edits": [
                {
                    "op": "add_datum",
                    "label": "C",
                    "location": "the right edge of the plate",
                    "finding_ids": ["GDT-001@fcf_1"],
                }
            ]
        }
    ]
    body = client.post(f"/api/runs/{run_id}/fix").json()

    edit = body["rounds"][0]["applied"][0]
    assert (edit["before"], edit["after"], edit["pen"]) == ("—", "datum C", "add datum C")
    assert edit["status"] == "held"
    assert "designer" in edit["note"]
    # Resolved by the rules, but still the designer's to finish.
    assert body["status"][0]["status"] == "resolved"
    assert body["status"][0]["outcome"] == "held"


def test_an_edit_whose_finding_survives_the_rerun_has_failed(
    client: TestClient, fake: FakeClient
):
    """An edit that applies cleanly but leaves its finding in place is not a pass."""
    run_id = checked_run(client)
    tightened = {
        "op": "set_field",
        "target": "fcf",
        "id": "fcf_1",
        "field": "tolerance",
        "value": 0.2,
        "finding_ids": ["GDT-001@fcf_1"],
    }
    fake.corrections = [{"edits": [tightened]}] * 3
    body = client.post(f"/api/runs/{run_id}/fix").json()

    edit = body["rounds"][0]["applied"][0]
    assert edit["status"] == "failed"
    assert "still reports GDT-001@fcf_1" in edit["note"]
    assert (edit["before"], edit["after"], edit["pen"]) == (
        "⌖ Ø0.25 Ⓜ | A | B | C",
        "⌖ Ø0.2 Ⓜ | A | B | C",
        "Ø0.2",
    )
    assert body["status"][0]["outcome"] == "unresolved"


def test_a_refused_but_understood_edit_still_shows_what_it_asked_for(
    client: TestClient, fake: FakeClient
):
    """A proposal the guards refused is drawn struck through, so it needs its 'after'."""
    run_id = checked_run(client)
    fake.corrections = [
        {
            "edits": [
                {
                    "op": "set_field",
                    "target": "fcf",
                    "id": "fcf_1",
                    "field": "datum_refs",
                    "value": [],
                    "finding_ids": ["GDT-001@fcf_1"],
                }
            ]
        }
    ]
    body = client.post(f"/api/runs/{run_id}/fix").json()

    refused = body["rounds"][0]["rejected"][0]
    assert refused["status"] == "rejected"
    assert refused["before"] == "⌖ Ø0.25 Ⓜ | A | B | C"
    assert refused["after"] == "⌖ Ø0.25 Ⓜ"
    assert refused["finding_ids"] == ["GDT-001@fcf_1"]
    assert refused["note"].startswith("Refused before the rules ran: ")


def test_each_rounds_before_is_the_drawing_the_round_started_from(
    client: TestClient, fake: FakeClient
):
    """Round 2's 'before' carries round 1's edit, not the drawing as first submitted."""
    run_id = checked_run(client)

    def tolerance(value: float) -> dict:
        return {
            "op": "set_field",
            "target": "fcf",
            "id": "fcf_1",
            "field": "tolerance",
            "value": value,
            "finding_ids": ["GDT-001@fcf_1"],
        }

    fake.corrections = [{"edits": [tolerance(0.2)]}, {"edits": [tolerance(0.15)]}]
    body = client.post(f"/api/runs/{run_id}/fix").json()

    first, second = (round_["applied"][0] for round_ in body["rounds"][:2])
    assert first["after"] == "⌖ Ø0.2 Ⓜ | A | B | C"
    assert second["before"] == "⌖ Ø0.2 Ⓜ | A | B | C"
    assert second["after"] == "⌖ Ø0.15 Ⓜ | A | B | C"


def test_check_returns_a_result_for_every_rule(client: TestClient):
    """Passes are reported too, each with a sentence built from the drawing's values."""
    created = upload(client)
    body = client.post(f"/api/runs/{created['run_id']}/check", json=created["drawing"]).json()

    results = body["rule_results"]
    assert [r["rule_id"] for r in results] == [rule.id for rule in RULES]
    by_id = {r["rule_id"]: r for r in results}
    assert by_id["GDT-001"]["status"] == "fail"
    assert by_id["GDT-001"]["issues"] == [1]
    assert "nothing defines C" in by_id["GDT-001"]["explanation"]
    assert by_id["DIM-001"] == {
        "rule_id": "DIM-001",
        "title": "Missing units or general tolerance",
        "status": "pass",
        "explanation": "Units (mm) and a general tolerance (±0.1) are both stated.",
        "issues": [],
    }
    assert body["checks_summary"] == "6 checks: 5 passed, 1 failed"
    # Findings are unchanged by the addition.
    assert rule_ids(body["findings"]) == ["GDT-001"]


def test_check_results_follow_an_edit(client: TestClient):
    """Defining datum C turns GDT-001 from fail to pass, naming all three datums."""
    created = upload(client)
    edited = created["drawing"]
    edited["datums"].append({"label": "C"})
    body = client.post(f"/api/runs/{created['run_id']}/check", json=edited).json()

    gdt_001 = next(r for r in body["rule_results"] if r["rule_id"] == "GDT-001")
    assert gdt_001["status"] == "pass"
    assert gdt_001["explanation"] == "Every datum the frames reference is defined: A, B, C."
    assert body["checks_summary"] == "6 checks: 6 passed, 0 failed"


def test_fix_puts_the_checks_on_the_redline(client: TestClient, monkeypatch):
    """The redline is drawn with every rule's result for the drawing as submitted."""
    import draftcheck.api as api_module

    seen: dict = {}
    real = api_module.render_redline

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(api_module, "render_redline", spy)
    created = upload(client)
    run_id = created["run_id"]
    client.post(f"/api/runs/{run_id}/check", json=created["drawing"])
    assert client.post(f"/api/runs/{run_id}/fix").status_code == 200

    results = seen["rule_results"]
    assert [r.rule_id for r in results] == [rule.id for rule in RULES]
    assert next(r for r in results if r.rule_id == "GDT-001").status == "fail"


def test_fix_returns_rule_results_for_the_corrected_drawing(client: TestClient, fake: FakeClient):
    """The after-fixes checks come from re-running the rules on the corrected drawing."""
    fake.corrections = [
        {
            "edits": [
                {
                    "op": "remove_datum_ref",
                    "fcf_id": "fcf_1",
                    "label": "C",
                    "rationale": "A and B already locate the pattern.",
                    "finding_ids": ["GDT-001@fcf_1"],
                }
            ]
        }
    ]
    created = upload(client)
    run_id = created["run_id"]
    client.post(f"/api/runs/{run_id}/check", json=created["drawing"])
    body = client.post(f"/api/runs/{run_id}/fix").json()

    after = {r["rule_id"]: r for r in body["final_rule_results"]}
    assert list(after) == [rule.id for rule in RULES]
    assert after["GDT-001"]["status"] == "pass"
    assert after["GDT-001"]["explanation"] == "Every datum the frames reference is defined: A, B."
    assert body["final_checks_summary"] == "6 checks: 6 passed, 0 failed"


def test_fix_checks_ignore_what_the_model_claims(client: TestClient, fake: FakeClient):
    """An edit that cites GDT-001 but does not fix it leaves GDT-001 failed after fixes."""
    fake.corrections = [
        {
            "edits": [
                {
                    "op": "set_field",
                    "target": "dimension",
                    "id": "dim_3",
                    "field": "tol_plus",
                    "value": 0.3,
                    "rationale": "This resolves the undefined datum.",
                    "finding_ids": ["GDT-001@fcf_1"],
                }
            ]
        }
    ] * 3
    created = upload(client)
    run_id = created["run_id"]
    client.post(f"/api/runs/{run_id}/check", json=created["drawing"])
    body = client.post(f"/api/runs/{run_id}/fix").json()

    gdt = next(r for r in body["final_rule_results"] if r["rule_id"] == "GDT-001")
    assert gdt["status"] == "fail"
    assert gdt["issues"] == [1]  # still the submitted drawing's issue 1
    assert body["final_checks_summary"] == "6 checks: 5 passed, 1 failed"


def test_fix_puts_the_after_fixes_checks_on_the_redline(client: TestClient, monkeypatch):
    import draftcheck.api as api_module

    seen: dict = {}
    real = api_module.render_redline

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(api_module, "render_redline", spy)
    created = upload(client)
    run_id = created["run_id"]
    client.post(f"/api/runs/{run_id}/check", json=created["drawing"])
    client.post(f"/api/runs/{run_id}/fix")

    assert [r.rule_id for r in seen["final_rule_results"]] == [rule.id for rule in RULES]
