"""Tests for the HTTP layer.

Every test overrides two dependencies: the LLM client factory, so the model is a scripted
fake and no request touches the network, and the runs root, so each test gets its own tmp
folder and runs never see each other's files.

What is being tested here is the plumbing — status codes, run state carried between
requests, and the fact that the data the user confirms is the data the rules run on. The
rules themselves, the loop, and the redline have their own tests.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from draftcheck.api import app, get_client_factory, get_runs_root
from draftcheck.llm_client import JSONChatResult, Usage
from draftcheck.prompts import EXTRACTION_SYSTEM

REPO_ROOT = Path(__file__).resolve().parent.parent
DRAWING = REPO_ROOT / "samples" / "generated" / "plate_001.pdf"
FIXTURE_JSON = REPO_ROOT / "tests" / "fixtures" / "plate_001.model.json"

UNKNOWN_RUN = "00000000-0000-4000-8000-000000000000"


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


@pytest.fixture
def client(tmp_path: Path, fake: FakeClient):
    """A TestClient whose runs land in tmp_path and whose model is the fake."""
    app.dependency_overrides[get_runs_root] = lambda: tmp_path
    app.dependency_overrides[get_client_factory] = lambda: (lambda provider=None: fake)
    with TestClient(app) as test_client:
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


def test_meta_json_never_holds_a_key(client: TestClient, tmp_path):
    """The run's metadata names the provider and model, and carries no secret."""
    created = upload(client)
    meta = json.loads((tmp_path / created["run_id"] / "meta.json").read_text())
    assert set(meta) == {
        "run_id",
        "source",
        "provider",
        "model",
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
    # every way in — drop, click, keyboard — to it, so renaming it would leave a page
    # that cannot take a file at all.
    assert 'id="dropzone"' in page.text

    # The overlay on the page image and its toggle. app.js looks both ids up at load
    # time: without them the boxes that link the tables to the drawing never appear.
    assert 'id="overlay"' in page.text
    assert 'id="show-overlay"' in page.text

    script = client.get("/app.js")
    assert script.status_code == 200
    assert "/api/runs" in script.text

    stylesheet = client.get("/style.css")
    assert stylesheet.status_code == 200
    assert "DraftCheck" in stylesheet.text

    # The page hides the spinner, the error box, and the results by setting `hidden`.
    # Several of them are `display: flex`, which outranks the browser's own rule for
    # `[hidden]` — so without this one declaration the spinner never goes away.
    assert "[hidden]" in stylesheet.text
    assert "display: none !important" in stylesheet.text
