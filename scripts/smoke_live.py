"""Smoke test against the live DraftCheck site.

Run with:  python -m scripts.smoke_live [--base https://draftcheck.onrender.com] [--explain]

Walks one drawing through the public site exactly as the browser does — upload with the
cross-check on, check, the streamed fix, the redline PDF, and optionally the explanation —
timing each step and printing PASS/FAIL with a short reason. Needs no API keys: it only
calls the site's public endpoints.

**It makes real model calls** and counts toward the live usage limits like any visitor:
one upload (two models with the cross-check), one fix, and one explanation with --explain.
If the site answers 429 (usage limit reached) the run says so and stops.

Exits 0 only if every step passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

DEFAULT_BASE = "https://draftcheck.onrender.com"
SAMPLE_PDF = Path(__file__).resolve().parent.parent / "samples" / "generated" / "plate_001.pdf"
#: The provider the page's drop-down starts on, sent as the page sends it.
DEFAULT_PROVIDER = "baseten"
#: The rule the sample drawing breaks on purpose; the fix must clear it.
EXPECTED_RULE = "GDT-001"

#: A free Render instance asleep can take most of a minute to wake.
COLD_START_BUDGET_S = 90.0
HEALTH_RETRY_S = 3.0
#: Model calls (upload, fix, explain) can take minutes; everything else is quick.
MODEL_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=30.0)
QUICK_TIMEOUT = httpx.Timeout(30.0)
#: Events all landing within this many seconds of each other means a proxy held them back.
BUFFERED_WINDOW_S = 1.0
#: A real two-page redline is far bigger than this; anything smaller is an error page.
MIN_PDF_BYTES = 5_000

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

Clock = Callable[[], float]


@dataclass
class StepResult:
    """One step's outcome, as a row of the final table."""

    name: str
    status: str
    seconds: float | None
    detail: str
    #: Extra lines printed under the step as it finishes (the stream's timeline).
    lines: list[str] = field(default_factory=list)


@dataclass
class TimedEvent:
    """One line of the fix stream and when it arrived, in seconds after the request."""

    at_s: float
    event: dict


@dataclass
class Context:
    """What one step leaves for the next, plus the clock and sleep (swappable in tests)."""

    provider: str = DEFAULT_PROVIDER
    clock: Clock = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    run_id: str | None = None
    drawing: dict | None = None
    checked: bool = False
    fixed: bool = False


class StepFailed(Exception):
    """A step's check did not hold; the message says why."""


class UsageLimited(Exception):
    """The site answered 429: the usage limit is reached, so further calls would fail too."""


# ---- Small helpers -----------------------------------------------------------------------


def _error_text(response: httpx.Response) -> str:
    """The server's reason for a failed response, short enough for one table cell."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:200] or response.reason_phrase
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)[:200]


def expect_status(response: httpx.Response, *codes: int) -> None:
    """Raise unless the response has one of ``codes``; a 429 raises :class:`UsageLimited`."""
    if response.status_code == 429:
        raise UsageLimited(_error_text(response))
    if response.status_code not in codes:
        raise StepFailed(f"HTTP {response.status_code}: {_error_text(response)}")


def stream_delivery(times: list[float], window_s: float = BUFFERED_WINDOW_S) -> str:
    """Did the stream's events arrive as they happened, or all together at the end?

    ``times`` are arrival times in seconds after the request. Returns ``"incremental"``
    when the first and last event are more than ``window_s`` apart, ``"buffered"`` when
    they all arrived within it (a proxy held them and released them at once), and
    ``"too few events"`` when there were fewer than two to compare.
    """
    if len(times) < 2:
        return "too few events"
    spread = max(times) - min(times)
    return "incremental" if spread > window_s else "buffered"


def verified_findings(done: dict) -> list[str]:
    """The finding keys the fix's ``done`` event reports as verified resolved."""
    return [
        entry.get("finding", "")
        for entry in done.get("status") or []
        if entry.get("outcome") == "verified"
    ]


def describe_event(item: TimedEvent) -> str:
    """One timeline line, e.g. ``"  +  4.21 s  proposal (round 1)"``."""
    event = item.event
    kind = event.get("type", "?")
    extra = ""
    if "round" in event:
        extra = f" (round {event['round']})"
    elif kind == "error":
        extra = f" (HTTP {event.get('status')}: {event.get('detail')})"
    return f"  + {item.at_s:6.2f} s  {kind}{extra}"


def read_stream(client: httpx.Client, url: str, clock: Clock = time.monotonic) -> list[TimedEvent]:
    """POST ``url`` and read its newline-delimited JSON, timing each line as it arrives."""
    start = clock()
    events: list[TimedEvent] = []
    with client.stream("POST", url, timeout=MODEL_TIMEOUT) as response:
        if response.status_code != 200:
            response.read()
            expect_status(response, 200)
        for line in response.iter_lines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError as exc:
                raise StepFailed(f"stream line is not JSON: {line[:80]!r}") from exc
            events.append(TimedEvent(clock() - start, event))
    return events


def wait_for_health(
    client: httpx.Client,
    budget_s: float = COLD_START_BUDGET_S,
    clock: Clock = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> float:
    """Poll /healthz until it answers ``{"status": "ok"}``; return how long that took.

    A sleeping instance may hold the first request open while it starts, or answer 502/503
    from the host's proxy, so both are retried until ``budget_s`` runs out.
    """
    start = clock()
    last = "no answer"
    while True:
        remaining = max(1.0, budget_s - (clock() - start))
        try:
            response = client.get("/healthz", timeout=remaining)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return clock() - start
            last = f"HTTP {response.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            last = type(exc).__name__
        if clock() - start >= budget_s:
            raise StepFailed(f"not healthy after {budget_s:.0f} s (last: {last})")
        sleep(HEALTH_RETRY_S)


# ---- The steps ---------------------------------------------------------------------------
#
# Each takes the client and the shared context and returns (detail, extra lines), or
# raises StepFailed / UsageLimited. A step's time is measured by the runner around it.

StepOutput = tuple[str, list[str]]


def step_health(client: httpx.Client, ctx: Context) -> StepOutput:
    wake_s = wait_for_health(client, clock=ctx.clock, sleep=ctx.sleep)
    start = ctx.clock()
    response = client.get("/healthz", timeout=QUICK_TIMEOUT)
    expect_status(response, 200)
    warm_s = ctx.clock() - start
    return f"woke in {wake_s:.1f} s; warm ping {warm_s:.2f} s", []


def step_pages(client: httpx.Client, ctx: Context) -> StepOutput:
    for path in ("/", "/eval.html"):
        response = client.get(path, timeout=QUICK_TIMEOUT)
        if response.status_code != 200:
            raise StepFailed(f"{path} answered HTTP {response.status_code}")
    response = client.get("/eval/results.json", timeout=QUICK_TIMEOUT)
    if response.status_code != 200:
        raise StepFailed(f"/eval/results.json answered HTTP {response.status_code}")
    try:
        drawings = response.json()["drawings"]
    except (ValueError, KeyError, TypeError) as exc:
        raise StepFailed("dashboard snapshot is not the expected JSON") from exc
    runs = sum(1 for drawing in drawings for run in (drawing.get("runs") or {}).values() if run)
    return f"/ and /eval.html 200; snapshot: {len(drawings)} drawings, {runs} runs", []


def step_upload(client: httpx.Client, ctx: Context) -> StepOutput:
    if not SAMPLE_PDF.is_file():
        raise StepFailed(f"sample not found: {SAMPLE_PDF}")
    response = client.post(
        "/api/runs",
        files={"file": (SAMPLE_PDF.name, SAMPLE_PDF.read_bytes(), "application/pdf")},
        # The same form fields the page sends.
        data={"provider": ctx.provider, "cross_check": "1"},
        timeout=MODEL_TIMEOUT,
    )
    expect_status(response, 201)
    body = response.json()
    ctx.run_id = body["run_id"]
    ctx.drawing = body["drawing"]

    cross = body.get("cross_check") or {}
    agreement = ""
    if cross.get("status") == "done":
        agreement = f" ({cross.get('values_agreeing')}/{cross.get('values_compared')} agree)"
    latency = body.get("latency_s")
    latency_text = f"{latency:.1f} s" if isinstance(latency, int | float) else "?"
    detail = (
        f"HTTP {response.status_code}; model {body.get('model')} in {latency_text}; "
        f"fell_back={body.get('fell_back')}; "
        f"cross-check {cross.get('status', 'missing')}: {cross.get('summary')}{agreement}"
    )
    if body.get("fake_model"):
        detail += "; WARNING: answered by the fake model"
    return detail, []


def step_check(client: httpx.Client, ctx: Context) -> StepOutput:
    response = client.post(f"/api/runs/{ctx.run_id}/check", json=ctx.drawing, timeout=QUICK_TIMEOUT)
    expect_status(response, 200)
    body = response.json()
    rules = sorted({finding.get("rule_id") for finding in body.get("findings") or []})
    summary = body.get("checks_summary")
    if EXPECTED_RULE not in rules:
        raise StepFailed(f"no {EXPECTED_RULE} finding; got {rules or 'none'}; {summary}")
    ctx.checked = True
    return f"{summary}; findings: {', '.join(rules)}", []


def step_fix(client: httpx.Client, ctx: Context) -> StepOutput:
    events = read_stream(client, f"/api/runs/{ctx.run_id}/fix?stream=1", clock=ctx.clock)
    lines = [describe_event(item) for item in events]
    delivery = stream_delivery([item.at_s for item in events])
    span = f"{len(events)} events, {delivery}"

    for item in events:
        if item.event.get("type") == "error":
            status, reason = item.event.get("status"), item.event.get("detail")
            if status == 429:
                raise UsageLimited(str(reason))
            raise StepFailed(f"stream error HTTP {status}: {reason} ({span})")
    done = next((item.event for item in events if item.event.get("type") == "done"), None)
    if done is None:
        raise StepFailed(f"stream ended without a done event ({span})")
    ctx.fixed = True

    verified = verified_findings(done)
    if not any(key.startswith(f"{EXPECTED_RULE}@") for key in verified):
        raise StepFailed(f"{EXPECTED_RULE} not verified resolved; verified: {verified} ({span})")
    if delivery != "incremental":
        raise StepFailed(f"{EXPECTED_RULE} verified, but the stream arrived {delivery} ({span})")
    return (
        f"{span}; {EXPECTED_RULE} verified resolved; "
        f"{done.get('final_checks_summary')}; model {done.get('model')}",
        lines,
    )


def step_redline(client: httpx.Client, ctx: Context) -> StepOutput:
    response = client.get(f"/api/runs/{ctx.run_id}/redline.pdf", timeout=QUICK_TIMEOUT)
    expect_status(response, 200)
    kind = response.headers.get("content-type", "")
    size = len(response.content)
    if not kind.startswith("application/pdf"):
        raise StepFailed(f"content type is {kind!r}, not application/pdf")
    if size < MIN_PDF_BYTES or not response.content.startswith(b"%PDF"):
        raise StepFailed(f"only {size} bytes, or not a PDF")
    return f"application/pdf, {size / 1024:.0f} KB", []


def step_explain(client: httpx.Client, ctx: Context) -> StepOutput:
    response = client.post(f"/api/runs/{ctx.run_id}/explain", timeout=MODEL_TIMEOUT)
    expect_status(response, 200)
    body = response.json()
    part = (body.get("explanation") or {}).get("part") or {}
    if not part.get("type"):
        raise StepFailed("explanation has no part type")
    return (
        f"part type {part['type']!r} ({part.get('name_guess')}); model {body.get('model')}; "
        f"cached={body.get('cached')}",
        [],
    )


@dataclass
class Step:
    name: str
    run: Callable[[httpx.Client, Context], StepOutput]
    #: Says why the step cannot run yet (an earlier step it needs failed), or None.
    blocked: Callable[[Context], str | None] = lambda ctx: None


def _needs(attribute: str, step: str) -> Callable[[Context], str | None]:
    return lambda ctx: None if getattr(ctx, attribute) else f"needs {step}"


def build_steps(explain: bool) -> list[Step]:
    """The steps in order; the explanation only when asked for."""
    steps = [
        Step("a. healthz", step_health),
        Step("b. pages + snapshot", step_pages),
        Step("c. upload (cross-check)", step_upload),
        Step("d. check", step_check, _needs("run_id", "upload")),
        Step("e. fix (stream)", step_fix, _needs("checked", "check")),
        Step("f. redline PDF", step_redline, _needs("fixed", "fix")),
    ]
    if explain:
        steps.append(Step("g. explain", step_explain, _needs("run_id", "upload")))
    return steps


def run_all(
    client: httpx.Client,
    steps: list[Step],
    ctx: Context,
    on_result: Callable[[StepResult], None] = lambda result: None,
) -> list[StepResult]:
    """Run ``steps`` in order, timing each, and stop early on a 429 or a dead site."""
    results: list[StepResult] = []
    stopped: str | None = None
    for step in steps:
        reason = stopped or step.blocked(ctx)
        if reason:
            result = StepResult(step.name, SKIP, None, reason)
        else:
            start = ctx.clock()
            try:
                detail, lines = step.run(client, ctx)
                result = StepResult(step.name, PASS, ctx.clock() - start, detail, lines)
            except UsageLimited as exc:
                result = StepResult(
                    step.name, FAIL, ctx.clock() - start, f"USAGE LIMIT (HTTP 429): {exc}"
                )
                stopped = "stopped: usage limit reached"
            except StepFailed as exc:
                result = StepResult(step.name, FAIL, ctx.clock() - start, str(exc))
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                result = StepResult(
                    step.name, FAIL, ctx.clock() - start, f"{type(exc).__name__}: {exc}"
                )
            if result.status == FAIL and step.run is step_health:
                stopped = "stopped: site not healthy"
        results.append(result)
        on_result(result)
    return results


def format_result(result: StepResult) -> str:
    """One step as it finishes, with any extra lines under it."""
    seconds = "" if result.seconds is None else f" [{result.seconds:.1f} s]"
    lines = [f"{result.status} {result.name}{seconds}: {result.detail}", *result.lines]
    return "\n".join(lines)


def format_table(results: list[StepResult]) -> str:
    """The final table: step, result, time, and the short reason."""
    rows = [
        (r.name, r.status, "-" if r.seconds is None else f"{r.seconds:.1f} s", r.detail)
        for r in results
    ]
    header = ("Step", "Result", "Time", "Detail")
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(3)]

    def line(row: tuple[str, str, str, str]) -> str:
        cells = [row[i].ljust(widths[i]) for i in range(3)]
        return "  ".join([*cells, row[3]]).rstrip()

    passed = sum(1 for r in results if r.status == PASS)
    out = [line(header), "  ".join("-" * w for w in widths) + "  ------"]
    out += [line(row) for row in rows]
    out.append(f"\n{passed}/{len(results)} steps passed.")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.smoke_live",
        description=(
            "Smoke-test the live DraftCheck site. Makes real model calls that count "
            "toward the site's usage limits."
        ),
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"Site URL. Default: {DEFAULT_BASE}")
    parser.add_argument(
        "--explain", action="store_true", help="Also ask for the explanation (one more call)."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = args.base.rstrip("/")
    print(f"Smoke test against {base} (real model calls; counts toward the usage limits)\n")
    with httpx.Client(
        base_url=base,
        headers={"User-Agent": "draftcheck-smoke-live"},
        timeout=QUICK_TIMEOUT,
        follow_redirects=True,
    ) as client:
        results = run_all(
            client,
            build_steps(args.explain),
            Context(),
            on_result=lambda result: print(format_result(result), flush=True),
        )
    print()
    print(format_table(results))
    if any("USAGE LIMIT" in r.detail for r in results):
        print("Stopped early: the site's usage limit is reached. Try again later.")
    return 0 if all(r.status == PASS for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
