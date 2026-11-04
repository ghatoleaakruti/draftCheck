"""Tests for the AI explanation ("About this drawing"). No network anywhere.

Validation is tested directly against the plate_001 fixture: an entry naming an id, datum
label, or finding key the drawing does not have must be dropped and said so. The endpoint
is tested with a scripted client on a run folder built by hand, so no upload or render is
needed: what it explains, the cache, the limit, cancel, and the fake model.
"""

import json
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from draftcheck.api import (
    EXPLAIN_JSON,
    FAKE_MODEL_ENV_VAR,
    FallbackPolicy,
    app,
    get_cleaner,
    get_client_factory,
    get_disconnect_probe,
    get_fallback_policy,
    get_limiter,
    get_runs_root,
)
from draftcheck.explain import explanation_fingerprint, parse_explanation
from draftcheck.fake_model import EXPLAINED_FINDING, SCRIPTED_EXPLANATION, scripted_explanation
from draftcheck.limits import (
    DEFAULT_EXPLAINS_PER_HOUR,
    ENV_EXPLAINS_PER_HOUR,
    LIMIT_MESSAGE,
    Cleaner,
    LimitExceeded,
    Limits,
    UsageLimiter,
)
from draftcheck.llm_client import JSONChatResult, ModelCancelled, Usage
from draftcheck.pipeline import check
from draftcheck.prompts import EXPLAIN_SYSTEM, EXTRACTION_SYSTEM, explain_user
from draftcheck.schema import parse_drawing

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_JSON = REPO_ROOT / "tests" / "fixtures" / "plate_001.model.json"
PAGE_PNG = REPO_ROOT / "samples" / "generated" / "plate_001.png"

NO_FALLBACK = FallbackPolicy(provider=None, primary_timeout_s=45)
ROOMY = Limits(
    uploads_per_ip_per_hour=1000,
    fixes_per_ip_per_hour=1000,
    explains_per_ip_per_hour=1000,
    model_calls_per_day=1000,
)


def fixture_data() -> dict:
    return json.loads(FIXTURE_JSON.read_text(encoding="utf-8"))


def fixture_drawing():
    return parse_drawing(fixture_data())


# ---- Validation ---------------------------------------------------------------------


def test_the_scripted_explanation_is_fully_valid_for_plate_001() -> None:
    drawing = fixture_drawing()
    explanation = parse_explanation(SCRIPTED_EXPLANATION, drawing, check(drawing))
    assert explanation.parse_issues == []
    assert explanation.part.type == "plate"
    assert [d.id for d in explanation.dimensions] == [f"dim_{n}" for n in range(1, 7)]
    assert [f.id for f in explanation.fcfs] == ["fcf_1"]
    assert [d.label for d in explanation.datums] == ["A", "B"]
    assert [f.key for f in explanation.findings] == [EXPLAINED_FINDING]


def test_entries_naming_items_that_do_not_exist_are_dropped_and_recorded() -> None:
    drawing = fixture_drawing()
    data = {
        "part": {"type": "plate", "name_guess": "Plate", "summary": "A plate.", "confidence": 0.7},
        "dimensions": [
            {"id": "dim_1", "kind": "location", "purpose": "Locates hole 2."},
            {"id": "dim_99", "kind": "size", "purpose": "A dimension the model imagined."},
        ],
        "fcfs": [{"id": "fcf_7", "meaning": "A frame that is not there."}],
        # C is cited in fcf_1 but never defined by a datum feature symbol.
        "datums": [
            {"label": "a", "role": "Primary", "why": "Bottom edge."},
            {"label": "C", "role": "tertiary", "why": "Not on the drawing."},
        ],
        "findings": [
            {"key": "MFG-001@dim_3", "why_it_matters": "x", "how_to_think_about_fixing": "y"}
        ],
    }
    explanation = parse_explanation(data, drawing, check(drawing))

    assert [d.id for d in explanation.dimensions] == ["dim_1"]
    assert explanation.fcfs == []
    assert [(d.label, d.role) for d in explanation.datums] == [("A", "primary")]
    assert explanation.findings == []
    assert explanation.parse_issues == [
        "dimensions[1]: unknown id 'dim_99'",
        "fcfs[0]: unknown id 'fcf_7'",
        "datums[1]: unknown datum label 'C'",
        "findings[0]: unknown finding key 'MFG-001@dim_3'",
    ]


def test_finding_notes_are_dropped_when_no_findings_were_sent() -> None:
    drawing = fixture_drawing()
    explanation = parse_explanation(SCRIPTED_EXPLANATION, drawing, [])
    assert explanation.findings == []
    assert explanation.parse_issues == [f"findings[0]: unknown finding key {EXPLAINED_FINDING!r}"]


def test_malformed_and_duplicate_entries_are_dropped_one_at_a_time() -> None:
    drawing = fixture_drawing()
    data = {
        "part": {"type": "spaceship", "confidence": 7, "features": ["Ø8 hole", 3, ""]},
        "dimensions": [
            {"id": "dim_1", "kind": "wobble", "purpose": "Bad kind."},
            {"id": "dim_2", "kind": "location"},
            {"id": "dim_3", "kind": "size", "purpose": "Height."},
            {"id": "dim_3", "kind": "size", "purpose": "Said twice."},
            "not an object",
        ],
        "fcfs": "not a list",
    }
    explanation = parse_explanation(data, drawing, [])

    assert explanation.part.type == "other"
    assert explanation.part.confidence == 1.0
    assert explanation.part.features == ["Ø8 hole"]
    assert [d.purpose for d in explanation.dimensions] == ["Height."]
    assert explanation.parse_issues == [
        "dimensions[0]: invalid kind",
        "dimensions[1]: missing purpose",
        "dimensions[3]: second entry for 'dim_3'",
        "dimensions[4]: invalid entry",
        "fcfs: not a list",
    ]


def test_a_reply_that_is_not_an_object_explains_nothing() -> None:
    explanation = parse_explanation(["no"], fixture_drawing(), [])
    assert explanation.part is None
    assert explanation.parse_issues == ["the explanation was not a JSON object"]


def test_fingerprint_follows_the_data_and_ignores_parse_issues() -> None:
    drawing = fixture_drawing()
    findings = check(drawing)
    same = explanation_fingerprint(drawing, findings)
    assert explanation_fingerprint(fixture_drawing(), check(fixture_drawing())) == same

    noisy = fixture_drawing()
    noisy.parse_issues.append("dimensions[9]: missing required field 'value'")
    assert explanation_fingerprint(noisy, findings) == same

    assert explanation_fingerprint(drawing, []) != same
    edited = fixture_drawing()
    edited.dimensions[0].value = 81
    assert explanation_fingerprint(edited, findings) != same


def test_the_prompt_names_every_id_and_finding_key_and_never_asks_for_a_verdict() -> None:
    drawing = fixture_drawing()
    findings = check(drawing)
    user = explain_user(json.dumps(drawing.model_dump(mode="json")), findings)
    assert EXPLAINED_FINDING in user
    assert "dim_6" in user
    assert "Never say whether anything passes" in EXPLAIN_SYSTEM
    # A separate prompt: the extraction prompt is untouched by this step.
    assert EXPLAIN_SYSTEM != EXTRACTION_SYSTEM
    assert "explain" not in EXTRACTION_SYSTEM.lower()


def test_the_fake_only_explains_the_finding_when_it_was_sent() -> None:
    assert scripted_explanation("no findings here")["findings"] == []
    assert len(scripted_explanation(f"- {EXPLAINED_FINDING} [error]")["findings"]) == 1
    # Never mutates the module-level script.
    assert len(SCRIPTED_EXPLANATION["findings"]) == 1


# ---- Limits -------------------------------------------------------------------------


def test_explanations_have_their_own_hourly_allowance_and_spend_model_calls() -> None:
    limiter = UsageLimiter(Limits(explains_per_ip_per_hour=2, model_calls_per_day=100))
    limiter.claim_explain("1.1.1.1")
    limiter.claim_explain("1.1.1.1")
    with pytest.raises(LimitExceeded):
        limiter.claim_explain("1.1.1.1")
    limiter.claim_explain("2.2.2.2")  # another address has its own allowance
    limiter.claim_fix("1.1.1.1")  # and the fix allowance is untouched
    assert limiter.model_calls_today() == 4


def test_explain_limit_reads_its_environment_variable(monkeypatch) -> None:
    monkeypatch.delenv(ENV_EXPLAINS_PER_HOUR, raising=False)
    assert Limits.from_env().explains_per_ip_per_hour == DEFAULT_EXPLAINS_PER_HOUR == 10
    monkeypatch.setenv(ENV_EXPLAINS_PER_HOUR, "3")
    assert Limits.from_env().explains_per_ip_per_hour == 3


# ---- The endpoint -------------------------------------------------------------------


class ScriptedClient:
    """Answers the explanation prompt with a fixed reply and counts the calls."""

    def __init__(self, reply: dict | None = None) -> None:
        self.reply = reply
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        assert system == EXPLAIN_SYSTEM
        assert images and images[0][:4] == b"\x89PNG"
        self.calls.append("explain")
        self.prompts.append(user)
        data = self.reply if self.reply is not None else scripted_explanation(user)
        return JSONChatResult(
            data=data,
            text=json.dumps(data),
            latency_s=0.1,
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="scripted-model",
            attempts=1,
        )


def make_run(root: Path) -> str:
    """A run folder as an upload leaves it, without rendering or calling a model."""
    run_id = str(uuid.uuid4())
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "page.png").write_bytes(PAGE_PNG.read_bytes())
    (run_dir / "extraction.json").write_text(
        json.dumps(fixture_drawing().model_dump(mode="json")), encoding="utf-8"
    )
    (run_dir / "meta.json").write_text(json.dumps({"provider": None}), encoding="utf-8")
    return run_id


@pytest.fixture
def setup(tmp_path: Path):
    """Returns ``build(client, limits)`` -> a TestClient wired to tmp_path and that client."""

    def build(client, limits: Limits = ROOMY) -> TestClient:
        limiter = UsageLimiter(limits)
        app.dependency_overrides[get_runs_root] = lambda: tmp_path
        app.dependency_overrides[get_client_factory] = lambda: (lambda provider=None: client)
        app.dependency_overrides[get_limiter] = lambda: limiter
        app.dependency_overrides[get_cleaner] = lambda: Cleaner(limits)
        app.dependency_overrides[get_fallback_policy] = lambda: NO_FALLBACK
        test_client = TestClient(app)
        test_client.limiter = limiter
        return test_client

    yield build
    app.dependency_overrides.clear()


def test_explain_before_a_check_uses_the_extraction(setup, tmp_path: Path) -> None:
    model = ScriptedClient()
    client = setup(model)
    run_id = make_run(tmp_path)

    response = client.post(f"/api/runs/{run_id}/explain")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["based_on"] == "extraction"
    assert body["cached"] is False
    assert body["model"] == "scripted-model"
    assert body["explanation"]["part"]["type"] == "plate"
    assert body["explanation"]["findings"] == []
    assert body["explanation"]["parse_issues"] == []
    assert "No findings were given" in model.prompts[0]

    saved = json.loads((tmp_path / run_id / EXPLAIN_JSON).read_text(encoding="utf-8"))
    assert saved["fingerprint"] == body["fingerprint"]


def test_explain_after_a_check_uses_the_confirmed_data_and_findings(setup, tmp_path) -> None:
    model = ScriptedClient()
    client = setup(model)
    run_id = make_run(tmp_path)
    assert client.post(f"/api/runs/{run_id}/check", json=fixture_data()).status_code == 200

    body = client.post(f"/api/runs/{run_id}/explain").json()
    assert body["based_on"] == "confirmed"
    assert [f["key"] for f in body["explanation"]["findings"]] == [EXPLAINED_FINDING]
    assert EXPLAINED_FINDING in model.prompts[0]


def test_the_endpoint_drops_entries_for_items_that_do_not_exist(setup, tmp_path) -> None:
    reply = {
        "part": {"type": "plate"},
        "dimensions": [{"id": "dim_42", "kind": "size", "purpose": "Imagined."}],
    }
    client = setup(ScriptedClient(reply))
    run_id = make_run(tmp_path)
    body = client.post(f"/api/runs/{run_id}/explain").json()
    assert body["explanation"]["dimensions"] == []
    assert body["explanation"]["parse_issues"] == ["dimensions[0]: unknown id 'dim_42'"]


def test_a_second_request_is_served_from_the_cache(setup, tmp_path: Path) -> None:
    model = ScriptedClient()
    client = setup(model)
    run_id = make_run(tmp_path)

    first = client.post(f"/api/runs/{run_id}/explain").json()
    second = client.post(f"/api/runs/{run_id}/explain").json()
    assert model.calls == ["explain"]
    assert second["cached"] is True
    assert second["explanation"] == first["explanation"]
    assert client.limiter.model_calls_today() == 1


def test_changed_confirmed_data_is_explained_afresh(setup, tmp_path: Path) -> None:
    model = ScriptedClient()
    client = setup(model)
    run_id = make_run(tmp_path)
    client.post(f"/api/runs/{run_id}/explain")

    data = fixture_data()
    client.post(f"/api/runs/{run_id}/check", json=data)
    assert client.post(f"/api/runs/{run_id}/explain").json()["cached"] is False
    assert client.post(f"/api/runs/{run_id}/explain").json()["cached"] is True

    data["dimensions"][0]["value"] = 81
    client.post(f"/api/runs/{run_id}/check", json=data)
    assert client.post(f"/api/runs/{run_id}/explain").json()["cached"] is False
    assert model.calls == ["explain"] * 3


def test_the_explain_limit_refuses_with_the_demo_message(setup, tmp_path: Path) -> None:
    model = ScriptedClient()
    client = setup(model, Limits(explains_per_ip_per_hour=1, model_calls_per_day=100))
    first, second = make_run(tmp_path), make_run(tmp_path)

    assert client.post(f"/api/runs/{first}/explain").status_code == 200
    # A cached answer is free, even with the allowance spent.
    assert client.post(f"/api/runs/{first}/explain").status_code == 200
    refused = client.post(f"/api/runs/{second}/explain")
    assert refused.status_code == 429
    assert refused.json()["detail"] == LIMIT_MESSAGE
    assert model.calls == ["explain"]
    assert not (tmp_path / second / EXPLAIN_JSON).exists()


def test_explaining_an_unknown_run_is_404(setup) -> None:
    client = setup(ScriptedClient())
    assert client.post(f"/api/runs/{uuid.uuid4()}/explain").status_code == 404


class HangingClient:
    """Waits inside the call until it is aborted, as a slow provider would."""

    def __init__(self) -> None:
        self.in_call = threading.Event()
        self.aborted = threading.Event()

    def abort(self) -> None:
        self.aborted.set()

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        self.in_call.set()
        if self.aborted.wait(timeout=10):
            raise ModelCancelled()
        raise AssertionError("never aborted")


def test_a_cancelled_explanation_aborts_the_call_and_saves_nothing(setup, tmp_path) -> None:
    slow = HangingClient()
    client = setup(slow)

    async def gone_once_the_model_is_busy(request) -> bool:
        return slow.in_call.is_set()

    app.dependency_overrides[get_disconnect_probe] = lambda: gone_once_the_model_is_busy
    run_id = make_run(tmp_path)
    try:
        response = client.post(f"/api/runs/{run_id}/explain")
    finally:
        slow.aborted.set()
    assert response.status_code == 499
    assert not (tmp_path / run_id / EXPLAIN_JSON).exists()


def test_fake_model_mode_explains_plate_001(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(FAKE_MODEL_ENV_VAR, "1")
    monkeypatch.setattr("draftcheck.fake_model.FAKE_DELAY_S", 0)
    app.dependency_overrides[get_runs_root] = lambda: tmp_path
    app.dependency_overrides[get_limiter] = lambda: UsageLimiter(ROOMY)
    app.dependency_overrides[get_cleaner] = lambda: Cleaner(ROOMY)
    app.dependency_overrides[get_fallback_policy] = lambda: NO_FALLBACK
    try:
        client = TestClient(app, client=("127.0.0.1", 50000))
        run_id = make_run(tmp_path)
        client.post(f"/api/runs/{run_id}/check", json=fixture_data())
        body = client.post(f"/api/runs/{run_id}/explain").json()
        assert body["fake_model"] is True
        assert body["explanation"]["parse_issues"] == []
        assert body["explanation"]["findings"][0]["key"] == EXPLAINED_FINDING
    finally:
        app.dependency_overrides.clear()
