"""Tests for scripts/smoke_live.py, all against httpx's mock transport: no network.

A fake site answers every endpoint the script calls; a fake clock that the fake site moves
forward stands in for real time, so the stream-timing verdict is tested exactly.
"""

from __future__ import annotations

import json

import httpx
import pytest

from scripts import smoke_live as smoke

RUN_ID = "11111111-2222-3333-4444-555555555555"


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _done_event(outcome: str = "verified") -> dict:
    return {
        "type": "done",
        "status": [{"finding": "GDT-001@fcf_1", "status": "resolved", "outcome": outcome}],
        "final_checks_summary": "6 checks: 6 passed, 0 failed",
        "model": "muse-spark-open",
    }


def _stream_body(clock: FakeClock, gaps: list[float], events: list[dict]):
    """Yield each event after moving the clock on by its gap, as a live stream would."""

    def body():
        for gap, event in zip(gaps, events, strict=True):
            clock.now += gap
            yield (json.dumps(event) + "\n").encode()

    return body()


INCREMENTAL = (
    [2.0, 5.0, 1.0, 0.5],
    [
        {"type": "round_started", "round": 1},
        {"type": "proposal", "round": 1},
        {"type": "verdict", "round": 1},
        _done_event(),
    ],
)


def fake_site(clock: FakeClock, overrides: dict | None = None):
    """A handler for the mock transport answering like the live site.

    ``overrides`` maps "METHOD /path" to a response (or a function of the request) to
    swap one endpoint's answer.
    """
    overrides = overrides or {}
    stream_gaps, stream_events = INCREMENTAL

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        if key in overrides:
            answer = overrides[key]
            return answer(request) if callable(answer) else answer
        if key == "GET /healthz":
            return httpx.Response(200, json={"status": "ok"})
        if key in ("GET /", "GET /eval.html"):
            return httpx.Response(200, text="<html></html>")
        if key == "GET /eval/results.json":
            drawings = [{"runs": {"baseten": {"x": 1}, "meta": {"x": 1}}} for _ in range(3)]
            return httpx.Response(200, json={"drawings": drawings})
        if key == "POST /api/runs":
            body = request.content
            assert b'name="cross_check"' in body and b'name="provider"' in body
            assert b"plate_001.pdf" in body
            return httpx.Response(
                201,
                json={
                    "run_id": RUN_ID,
                    "drawing": {"dimensions": []},
                    "model": "muse-spark-open",
                    "fell_back": False,
                    "latency_s": 12.3,
                    "cross_check": {
                        "status": "done",
                        "summary": "Both Meta models agree on 20 of 22 values; 2 to double-check",
                        "values_agreeing": 20,
                        "values_compared": 22,
                    },
                },
            )
        if key == f"POST /api/runs/{RUN_ID}/check":
            assert json.loads(request.content) == {"dimensions": []}
            return httpx.Response(
                200,
                json={
                    "findings": [{"rule_id": "GDT-001"}, {"rule_id": "DIM-002"}],
                    "checks_summary": "6 checks: 4 passed, 2 failed",
                },
            )
        if key == f"POST /api/runs/{RUN_ID}/fix":
            assert request.url.params.get("stream") == "1"
            return httpx.Response(
                200,
                content=_stream_body(clock, stream_gaps, stream_events),
                headers={"content-type": "application/x-ndjson"},
            )
        if key == f"GET /api/runs/{RUN_ID}/redline.pdf":
            return httpx.Response(
                200,
                content=b"%PDF-1.4" + b"x" * 20_000,
                headers={"content-type": "application/pdf"},
            )
        if key == f"POST /api/runs/{RUN_ID}/explain":
            return httpx.Response(
                200,
                json={"explanation": {"part": {"type": "plate"}}, "model": "m", "cached": False},
            )
        return httpx.Response(404, json={"detail": "Not Found"})

    return handler


def run(clock: FakeClock, overrides: dict | None = None, explain: bool = False):
    client = httpx.Client(
        base_url="https://site.test", transport=httpx.MockTransport(fake_site(clock, overrides))
    )
    ctx = smoke.Context(clock=clock, sleep=clock.sleep)
    return smoke.run_all(client, smoke.build_steps(explain), ctx)


def by_name(results: list[smoke.StepResult]) -> dict[str, smoke.StepResult]:
    return {result.name.split(".")[0]: result for result in results}


# ---- Stream timing ---------------------------------------------------------------------


def test_events_spread_over_time_are_incremental():
    assert smoke.stream_delivery([2.0, 7.0, 8.0, 8.5]) == "incremental"


def test_events_all_at_the_end_are_buffered():
    assert smoke.stream_delivery([30.0, 30.1, 30.2, 30.4]) == "buffered"


def test_one_event_is_too_few_to_tell():
    assert smoke.stream_delivery([5.0]) == "too few events"
    assert smoke.stream_delivery([]) == "too few events"


def test_read_stream_times_each_event_as_it_arrives():
    clock = FakeClock()
    client = httpx.Client(
        base_url="https://site.test", transport=httpx.MockTransport(fake_site(clock))
    )
    events = smoke.read_stream(client, f"/api/runs/{RUN_ID}/fix?stream=1", clock=clock)
    assert [item.event["type"] for item in events] == [
        "round_started",
        "proposal",
        "verdict",
        "done",
    ]
    assert [item.at_s for item in events] == [2.0, 7.0, 8.0, 8.5]


def test_verified_findings_reads_only_verified_outcomes():
    done = {
        "status": [
            {"finding": "GDT-001@fcf_1", "outcome": "verified"},
            {"finding": "DIM-002@dim_3", "outcome": "held"},
            {"finding": "DIM-004@dim_5", "outcome": "unresolved"},
        ]
    }
    assert smoke.verified_findings(done) == ["GDT-001@fcf_1"]


# ---- The whole run, against the fake site ------------------------------------------------


def test_every_step_passes_against_a_healthy_site():
    results = run(FakeClock(), explain=True)
    assert [r.status for r in results] == [smoke.PASS] * 7
    steps = by_name(results)
    assert "3 drawings, 6 runs" in steps["b"].detail
    assert "muse-spark-open" in steps["c"].detail and "20/22 agree" in steps["c"].detail
    assert "6 checks: 4 passed, 2 failed" in steps["d"].detail
    assert "4 events, incremental" in steps["e"].detail
    assert len(steps["e"].lines) == 4
    assert "plate" in steps["g"].detail


def test_buffered_stream_fails_the_fix_step():
    clock = FakeClock()

    def buffered(request):
        gaps = [30.0, 0.1, 0.1, 0.1]
        return httpx.Response(200, content=_stream_body(clock, gaps, INCREMENTAL[1]))

    steps = by_name(run(clock, {f"POST /api/runs/{RUN_ID}/fix": buffered}))
    assert steps["e"].status == smoke.FAIL
    assert "buffered" in steps["e"].detail
    # The fix did finish, so the redline is still checked.
    assert steps["f"].status == smoke.PASS


def test_fix_not_verifying_the_expected_rule_fails():
    clock = FakeClock()

    def unresolved(request):
        events = [*INCREMENTAL[1][:3], _done_event("unresolved")]
        return httpx.Response(200, content=_stream_body(clock, INCREMENTAL[0], events))

    steps = by_name(run(clock, {f"POST /api/runs/{RUN_ID}/fix": unresolved}))
    assert steps["e"].status == smoke.FAIL
    assert "not verified" in steps["e"].detail


def test_usage_limit_stops_the_run():
    limit = httpx.Response(429, json={"detail": "DraftCheck's demo limit is reached."})
    results = run(FakeClock(), {"POST /api/runs": limit}, explain=True)
    steps = by_name(results)
    assert steps["c"].status == smoke.FAIL
    assert "USAGE LIMIT (HTTP 429)" in steps["c"].detail
    assert "demo limit" in steps["c"].detail
    for later in ("d", "e", "f", "g"):
        assert steps[later].status == smoke.SKIP
        assert "usage limit" in steps[later].detail


def test_usage_limit_inside_the_stream_is_reported_as_such():
    clock = FakeClock()

    def error(request):
        return httpx.Response(
            200,
            content=_stream_body(
                clock, [1.0], [{"type": "error", "status": 429, "detail": "limit"}]
            ),
        )

    steps = by_name(run(clock, {f"POST /api/runs/{RUN_ID}/fix": error}))
    assert "USAGE LIMIT" in steps["e"].detail
    assert steps["f"].status == smoke.SKIP


def test_cold_start_is_retried_and_its_time_reported():
    clock = FakeClock()
    answers = iter(
        [httpx.Response(502), httpx.Response(503), httpx.Response(200, json={"status": "ok"})]
    )

    def waking(request):
        return next(answers, httpx.Response(200, json={"status": "ok"}))

    steps = by_name(run(clock, {"GET /healthz": waking}))
    assert steps["a"].status == smoke.PASS
    assert f"woke in {2 * smoke.HEALTH_RETRY_S:.1f} s" in steps["a"].detail


def test_site_that_never_wakes_fails_and_stops():
    results = run(FakeClock(), {"GET /healthz": httpx.Response(503)})
    assert results[0].status == smoke.FAIL
    assert "not healthy after 90 s" in results[0].detail
    assert all(r.status == smoke.SKIP for r in results[1:])


def test_check_without_the_expected_rule_fails_and_skips_the_fix():
    no_gdt = httpx.Response(200, json={"findings": [], "checks_summary": "6 checks: 6 passed"})
    steps = by_name(run(FakeClock(), {f"POST /api/runs/{RUN_ID}/check": no_gdt}))
    assert steps["d"].status == smoke.FAIL
    assert "no GDT-001" in steps["d"].detail
    assert steps["e"].status == smoke.SKIP and steps["e"].detail == "needs check"


def test_tiny_redline_fails():
    tiny = httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    steps = by_name(run(FakeClock(), {f"GET /api/runs/{RUN_ID}/redline.pdf": tiny}))
    assert steps["f"].status == smoke.FAIL


# ---- Report formatting -------------------------------------------------------------------


def test_table_lines_up_and_counts_passes():
    results = [
        smoke.StepResult("a. healthz", smoke.PASS, 1.234, "woke in 1.0 s"),
        smoke.StepResult("e. fix (stream)", smoke.FAIL, 40.0, "buffered"),
        smoke.StepResult("f. redline PDF", smoke.SKIP, None, "needs fix"),
    ]
    table = smoke.format_table(results).splitlines()
    assert table[0].split() == ["Step", "Result", "Time", "Detail"]
    rows = table[2:5]
    # The detail column starts at the same place on every row.
    starts = {
        row.index(detail) for row, detail in zip(rows, ["woke", "buffered", "needs"], strict=True)
    }
    assert len(starts) == 1
    assert "1.2 s" in rows[0] and " - " in rows[2]
    assert table[-1] == "1/3 steps passed."


def test_format_result_prints_the_timeline_under_the_step():
    result = smoke.StepResult(
        "e. fix (stream)", smoke.PASS, 8.5, "ok", ["  +   2.00 s  round_started"]
    )
    assert smoke.format_result(result).splitlines() == [
        "PASS e. fix (stream) [8.5 s]: ok",
        "  +   2.00 s  round_started",
    ]


def test_describe_event_names_round_and_error():
    assert smoke.describe_event(smoke.TimedEvent(4.2, {"type": "proposal", "round": 2})) == (
        "  +   4.20 s  proposal (round 2)"
    )
    error = smoke.TimedEvent(1.0, {"type": "error", "status": 504, "detail": "timed out"})
    assert "error (HTTP 504: timed out)" in smoke.describe_event(error)


def test_exit_code_is_nonzero_when_a_step_fails(monkeypatch: pytest.MonkeyPatch):
    failing = [smoke.StepResult("a. healthz", smoke.FAIL, 90.0, "not healthy")]
    monkeypatch.setattr(smoke, "run_all", lambda *args, **kwargs: failing)
    assert smoke.main(["--base", "https://site.test"]) == 1
    passing = [smoke.StepResult("a. healthz", smoke.PASS, 1.0, "ok")]
    monkeypatch.setattr(smoke, "run_all", lambda *args, **kwargs: passing)
    assert smoke.main(["--base", "https://site.test"]) == 0
