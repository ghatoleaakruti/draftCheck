"""HTTP front end for one DraftCheck run.

This module decides nothing. Extraction is :func:`pipeline.extract_page`, the verdicts are
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

import asyncio
import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from draftcheck.agent import CorrectionReport, RoundRecord, correct
from draftcheck.config import DEFAULT_PROVIDER, SUPPORTED_PROVIDERS, ConfigError, get_provider
from draftcheck.crosscheck import (
    SKIP_PRIMARY_FELL_BACK,
    SKIP_PRIMARY_IS_CROSS,
    compare,
    outcome_done,
    outcome_failed,
    outcome_off,
    outcome_skipped,
    outcome_timeout,
)
from draftcheck.edits import Edit, RejectedEdit, apply_edits, parse_edit
from draftcheck.fake_model import fake_client_factory, fake_cross_check_factory
from draftcheck.limits import Cleaner, LimitExceeded, Limits, UsageLimiter, client_ip
from draftcheck.llm_client import (
    MODEL_TIMEOUT_MESSAGE,
    JSONChatResult,
    LLMClient,
    ModelCancelled,
    ModelTimeout,
)
from draftcheck.llm_parsing import JSONParseError
from draftcheck.memory import memory_mb
from draftcheck.pipeline import ExtractionResult, SupportsChatJSON, check, extract_page
from draftcheck.redline import (
    EditDisplay,
    EditNames,
    edit_display,
    plain_edit,
    render_redline,
    save_redline,
)
from draftcheck.render import RenderError, load_pages
from draftcheck.rules import RULES, checks_summary, rule_results
from draftcheck.schema import Drawing, Finding, finding_key, parse_drawing

#: Where run folders are created. A dependency, so tests point it at a tmp folder.
RUNS_ROOT = Path("outputs/web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: The repo, for the files the demo run is built from. Those live beside the code rather
#: than inside the package: they are development material, like the demo itself.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Upload limits. Ten megabytes is far more than a rendered sheet needs and small enough
#: that a request can be held in memory while it is checked.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024

#: What ``render.load_pages`` can actually open. Checked on the filename rather than on the
#: browser's content type, which is guesswork on the client's side.
ALLOWED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}

#: How page 1 is rasterized on the server. Lower than the CLI's 200 dpi / 2400 px because
#: the free host has little RAM; each is overridable without a code change.
ENV_RENDER_DPI = "DRAFTCHECK_RENDER_DPI"
ENV_MAX_SIDE = "DRAFTCHECK_MAX_SIDE"
DEFAULT_RENDER_DPI = 150
DEFAULT_MAX_SIDE = 1800

#: How long one model call may take on the server before the request gives up with a 504.
#: The CLI has no such limit beyond the HTTP library's own; a terminal can wait.
ENV_MODEL_TIMEOUT = "DRAFTCHECK_MODEL_TIMEOUT_SECONDS"
DEFAULT_MODEL_TIMEOUT_S = 90

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

#: The demo run, for looking at the review screen without spending a model call.
DEMO_EXTRACTION = REPO_ROOT / "tests" / "fixtures" / "plate_001.model.json"
DEMO_PAGE = REPO_ROOT / "samples" / "generated" / "plate_001.png"

#: The sample drawing the page's "Try the sample drawing" button uploads. Only this one
#: file is served — the folder beside it also holds the answer key (``plate_001.gt.json``).
SAMPLE_PDF = REPO_ROOT / "samples" / "generated" / "plate_001.pdf"

#: What the demo lowers two items to, far enough under the page's threshold to be obvious.
DEMO_CONFIDENCE = 0.6

#: Addresses a request can come from and still be this machine's own browser.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Set this to ``"1"`` in the environment to turn the demo route on. Off by default.
DEMO_ENV_VAR = "DRAFTCHECK_ENABLE_DEMO"

#: Set this to ``"1"`` to answer model calls from this machine with the fake model
#: (:mod:`draftcheck.fake_model`) instead of Baseten or Meta. Off by default; development only.
FAKE_MODEL_ENV_VAR = "DRAFTCHECK_FAKE_MODEL"

#: Stage lines for the server log — sizes and memory only, never images, prompts, or keys.
logger = logging.getLogger(__name__)

#: A client, given a provider name or ``None`` for the configured default.
ClientFactory = Callable[[str | None], SupportsChatJSON]


def _positive_int_env(name: str, default: int) -> int:
    """An environment variable as a positive int, or the default if unset or unusable.

    Like the limits, a bad value falls back instead of stopping the server from starting.
    """
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def render_settings() -> tuple[int, int]:
    """The ``(dpi, max_side)`` an upload is rendered with, read on every upload."""
    return (
        _positive_int_env(ENV_RENDER_DPI, DEFAULT_RENDER_DPI),
        _positive_int_env(ENV_MAX_SIDE, DEFAULT_MAX_SIDE),
    )


def model_timeout_s() -> float:
    """Seconds one model call may take, read from the environment on every request."""
    return float(_positive_int_env(ENV_MODEL_TIMEOUT, DEFAULT_MODEL_TIMEOUT_S))


def get_model_timeout() -> float:
    """The model-call time limit, as a dependency so tests can make it a fraction of a second."""
    return model_timeout_s()


#: How long the upload waits for the cross-check once the primary reading is back. The
#: second reading started at the same moment, so it is usually done by then; when it is
#: not, the upload goes on without it rather than keep the person waiting.
ENV_CROSSCHECK_GRACE = "DRAFTCHECK_CROSSCHECK_GRACE_SECONDS"
DEFAULT_CROSSCHECK_GRACE_S = 20

#: The provider the cross-check reads with, and the only primary it is run for: the point
#: is two *different* Meta models reading the same sheet.
CROSS_CHECK_PROVIDER = "meta"
CROSS_CHECKS_FROM = "baseten"


def cross_check_grace_s() -> float:
    """Seconds to wait for the cross-check after the primary, read on every upload."""
    return float(_positive_int_env(ENV_CROSSCHECK_GRACE, DEFAULT_CROSSCHECK_GRACE_S))


def get_cross_check_grace() -> float:
    """The grace period, as a dependency so tests can make it a fraction of a second."""
    return cross_check_grace_s()


#: The automatic fallback. Muse Spark Open on Baseten stays the default provider; when one
#: of its calls stalls or the service is down, the same call is sent once to the provider
#: named here instead. An empty value turns the fallback off.
ENV_FALLBACK_PROVIDER = "DRAFTCHECK_FALLBACK_PROVIDER"
DEFAULT_FALLBACK_PROVIDER = "meta"

#: How long Baseten gets before the fallback is tried. Shorter than the normal limit so a
#: stalled call costs 45 s of waiting rather than 90 before somebody else is asked.
ENV_PRIMARY_TIMEOUT = "DRAFTCHECK_PRIMARY_TIMEOUT_SECONDS"
DEFAULT_PRIMARY_TIMEOUT_S = 45

#: The only provider that falls back. Somebody who picks Meta has picked it on purpose.
FALLS_BACK_FROM = "baseten"


@dataclass(frozen=True)
class FallbackPolicy:
    """Whether model calls fall back, to whom, and how long the first provider gets."""

    #: The provider to retry on, or ``None`` for no fallback at all.
    provider: str | None
    primary_timeout_s: float


def fallback_policy() -> FallbackPolicy:
    """The fallback settings, read from the environment on every request."""
    raw = os.environ.get(ENV_FALLBACK_PROVIDER)
    name = (DEFAULT_FALLBACK_PROVIDER if raw is None else raw).strip().lower()
    return FallbackPolicy(
        provider=name or None,
        primary_timeout_s=float(_positive_int_env(ENV_PRIMARY_TIMEOUT, DEFAULT_PRIMARY_TIMEOUT_S)),
    )


def get_fallback_policy() -> FallbackPolicy:
    """The fallback settings, as a dependency so tests can switch it and shorten it."""
    return fallback_policy()


def outage_reason(exc: BaseException) -> str | None:
    """A short reason if ``exc`` means the provider is not answering, else ``None``.

    Only an outage is worth asking someone else: no answer in time, no connection, or the
    provider's own server failing (5xx). A 4xx means *our* request was refused, and a reply
    that is not JSON means the model did answer; neither would go better elsewhere, and
    retrying them would hide a real problem. A cancel is never retried — nobody is waiting.
    """
    if isinstance(exc, ModelTimeout | APITimeoutError):
        return "timeout"
    if isinstance(exc, APIConnectionError):
        return "connection error"
    if isinstance(exc, APIStatusError) and exc.status_code >= 500:
        return f"HTTP {exc.status_code}"
    return None


@dataclass(frozen=True)
class Fallback:
    """Everything :class:`GuardedClient` needs to retry a call on a backup provider."""

    #: The backup provider's name, for the log and the result.
    provider: str
    #: Builds the backup client. Called only when a fallback actually happens, so a missing
    #: Meta key costs nothing until the day Baseten fails.
    build: Callable[[], SupportsChatJSON]
    #: How long the first provider gets before the backup is asked.
    primary_seconds: float


class GuardedClient:
    """A client whose every ``chat_json`` call has a time limit and can be cancelled.

    **Time limit.** The HTTP library's own timeout is per network attempt, and it retries,
    so on its own one call could run several times longer than the limit. This puts a wall
    clock on the whole call: it runs on a helper thread, and if that thread has not finished
    in ``seconds`` the caller gets :class:`ModelTimeout` straight away. A call that overran
    keeps going in the background until the provider answers or the HTTP timeout ends it,
    and its result is dropped.

    **Cancel.** :meth:`abort` is called when the browser has gone away. It tells the inner
    client to abort too — :meth:`LLMClient.abort` closes the HTTP request, so the provider
    stops spending time on it — and the waiting call raises :class:`ModelCancelled` at
    once. Every later call raises it before it starts, which is what stops the correction
    loop before its next round.

    **Fallback.** When given a :class:`Fallback`, the first provider gets only
    ``primary_seconds``. If it runs out of time or is down (:func:`outage_reason`), the same
    call is sent once to the backup under the normal ``seconds`` limit. Each call decides
    this on its own: one round of the correction loop falling back does not move the next
    round off Baseten. The result says who answered; ``fallback_calls`` counts the extra
    calls, so the daily budget can be charged for them.

    The helper thread is a daemon, so it never holds up the server shutting down.
    """

    #: How often a waiting call looks to see whether it has been cancelled.
    POLL_S = 0.05

    def __init__(
        self,
        inner: SupportsChatJSON,
        seconds: float,
        *,
        provider: str | None = None,
        fallback: Fallback | None = None,
    ) -> None:
        self._inner = inner
        self._seconds = seconds
        self._provider = provider
        self._fallback = fallback
        self._backup: SupportsChatJSON | None = None
        self._backup_lock = threading.Lock()
        self._cancelled = threading.Event()
        #: How many calls were sent a second time, to the backup.
        self.fallback_calls = 0

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def provider(self) -> str | None:
        """The provider this guard sends to first."""
        return self._provider

    def abort(self) -> None:
        """Stop the call in flight, if any, and every call after it. Any thread.

        Both clients are told: whichever one is mid-call — Baseten, or the backup that
        took over from it — closes its request.
        """
        self._cancelled.set()
        with self._backup_lock:
            clients = [self._inner, self._backup]
        for client in clients:
            abort = getattr(client, "abort", None)
            if callable(abort):
                abort()

    def _backup_client(self) -> SupportsChatJSON:
        """The backup client, built the first time it is needed and reused after that."""
        assert self._fallback is not None  # noqa: S101 - only called with a fallback set
        with self._backup_lock:
            backup = self._backup
        if backup is None:
            # Built outside the lock, so an abort() arriving meanwhile is never held up.
            # Only this request's worker thread builds it, so it is built at most once.
            backup = self._fallback.build()
            with self._backup_lock:
                self._backup = backup
        # A cancel that arrived while the backup was being built never reached it.
        if self.cancelled:
            abort = getattr(backup, "abort", None)
            if callable(abort):
                abort()
            raise ModelCancelled()
        return backup

    def _run(
        self, client: SupportsChatJSON, seconds: float, system: str, user: str, images: list[bytes]
    ) -> JSONChatResult:
        """One call on ``client`` under a wall-clock limit, abandoned on a cancel."""
        if self.cancelled:
            raise ModelCancelled()
        outcome: dict[str, Any] = {}

        def call() -> None:
            try:
                outcome["result"] = client.chat_json(system, user, images)
            except BaseException as exc:  # noqa: BLE001 - handed back to the caller below
                outcome["error"] = exc

        worker = threading.Thread(target=call, name="model-call", daemon=True)
        worker.start()
        deadline = time.monotonic() + seconds
        while worker.is_alive():
            if self.cancelled:
                raise ModelCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.info("model call timed out after %.0f s", seconds)
                raise ModelTimeout()
            worker.join(min(remaining, self.POLL_S))
        # A reply that lands after the cancel is dropped like one that lands after the limit.
        if self.cancelled:
            raise ModelCancelled()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        fallback = self._fallback
        if fallback is None:
            result = self._run(self._inner, self._seconds, system, user, images)
            return replace(result, provider=self._provider, fell_back=False)

        started = time.monotonic()
        try:
            result = self._run(self._inner, fallback.primary_seconds, system, user, images)
        except Exception as exc:
            reason = outage_reason(exc)
            if reason is None:
                raise
            try:
                backup = self._backup_client()
            except ConfigError:
                # No usable backup (its key is not set, say). Say so, and report the
                # original failure exactly as if there had been no fallback.
                logger.warning(
                    "fallback: %s -> %s not configured", self._provider, fallback.provider
                )
                raise exc from None
            logger.info(
                "fallback: %s -> %s after %.0f s (%s)",
                self._provider,
                fallback.provider,
                time.monotonic() - started,
                reason,
            )
            self.fallback_calls += 1
            result = self._run(backup, self._seconds, system, user, images)
            return replace(
                result, provider=fallback.provider, fell_back=True, fallback_reason=reason
            )
        return replace(result, provider=self._provider, fell_back=False)


def build_client(provider: str | None = None) -> SupportsChatJSON:
    """The real factory: an ``LLMClient`` for the named provider.

    The HTTP library is given the same limit as :class:`GuardedClient`, so a call that
    overran stops in the background soon after the request has already answered 504.
    ``abortable`` lets :meth:`GuardedClient.abort` close a request whose browser has gone.
    """
    return LLMClient(provider=get_provider(provider), timeout_s=model_timeout_s(), abortable=True)


def _provider_of(client: object, requested: str | None) -> str:
    """The provider a request's client talks to: as asked, as built, or the default."""
    if requested:
        return requested
    built = getattr(getattr(client, "provider", None), "name", None)
    if built:
        return built
    return (os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()


def guard_for(
    client: SupportsChatJSON,
    requested: str | None,
    client_factory: ClientFactory,
    timeout_s: float,
    policy: FallbackPolicy,
) -> GuardedClient:
    """The guard one request's model calls go through, with a fallback when one applies.

    Only Baseten falls back, only to a different provider, and only when one is set. The
    first provider's limit never exceeds the normal one, so a short ``timeout_s`` (as in
    the tests) is not outlived by a longer Baseten limit.
    """
    primary = _provider_of(client, requested)
    backup = policy.provider
    if primary != FALLS_BACK_FROM or not backup or backup == primary:
        return GuardedClient(client, timeout_s, provider=primary)
    return GuardedClient(
        client,
        timeout_s,
        provider=primary,
        fallback=Fallback(
            provider=backup,
            build=lambda: client_factory(backup),
            primary_seconds=min(policy.primary_timeout_s, timeout_s),
        ),
    )


def fake_model_is_allowed(request: Request) -> bool:
    """True when this request's model calls go to the fake model instead of a real one.

    The same two gates as the demo route, for the same reasons: ``DRAFTCHECK_FAKE_MODEL=1``
    is the deliberate act, off unless somebody set it, and loopback is the backstop for the
    variable being inherited somewhere nobody meant it to be. A request from anywhere else
    gets the real model even with the variable set.
    """
    if os.environ.get(FAKE_MODEL_ENV_VAR, "").strip() != "1":
        return False
    return bool(request.client) and request.client.host in LOOPBACK_HOSTS


def get_client_factory(request: Request) -> ClientFactory:
    """The LLM client, as a dependency.

    A *factory* rather than a client, because the provider is chosen per request. Tests
    override this one function with a factory that returns a scripted client, and no
    request anywhere in the app reaches the network. In development the fake model can
    stand in for the real ones (:func:`fake_model_is_allowed`).
    """
    if fake_model_is_allowed(request):
        return fake_client_factory
    return build_client


def is_fake(client: object) -> bool:
    """Whether a client is the development fake, so the page can say so."""
    return bool(getattr(client, "fake", False))


#: Asks whether the browser that sent ``request`` has gone away.
DisconnectProbe = Callable[[Request], Awaitable[bool]]

#: How often a request waiting on the model checks that its browser is still there.
DISCONNECT_POLL_S = 0.25

#: The status a cancelled request ends with. Nobody receives it — the browser has already
#: gone — but the access log shows it. 499 is nginx's "client closed request".
CANCELLED_STATUS = 499


async def browser_disconnected(request: Request) -> bool:
    return await request.is_disconnected()


def get_disconnect_probe() -> DisconnectProbe:
    """The disconnect check, as a dependency.

    FastAPI's test client only reports a disconnect once the response is complete, so it
    cannot show a browser leaving mid-request. Tests override this with a probe they control.
    """
    return browser_disconnected


class ClientGone(Exception):
    """The browser disconnected while the model was working, and the call was aborted."""


class AbortGroup:
    """Several guards aborted as one, for a request with more than one call in flight."""

    def __init__(self, *guards: Any) -> None:
        self._guards = [guard for guard in guards if guard is not None]

    def abort(self) -> None:
        for guard in self._guards:
            guard.abort()


class CrossCheckCall:
    """The second reading of an upload, run on its own thread beside the primary one.

    Started as soon as the page is rendered, so both models read at the same time and the
    cross-check adds little or no waiting. Its outcome is collected by
    :func:`settle_cross_check`; :meth:`abort` stops it, as Cancel stops the primary.
    """

    def __init__(self, guard: GuardedClient) -> None:
        self.guard = guard
        #: True once the call has been sent, which is when it costs a model call.
        self.started = False
        self.result: ExtractionResult | None = None
        self.error: BaseException | None = None
        self._done = threading.Event()

    def start(self, png_bytes: bytes) -> None:
        def run() -> None:
            try:
                self.result = extract_page(png_bytes, self.guard)
            except BaseException as exc:  # noqa: BLE001 - reported by settle_cross_check
                self.error = exc
            finally:
                self._done.set()

        self.started = True
        threading.Thread(target=run, name="cross-check", daemon=True).start()

    def wait(self, seconds: float) -> bool:
        """Wait up to ``seconds`` for the reading; True if it is done."""
        return self._done.wait(seconds)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def abort(self) -> None:
        self.guard.abort()


def _cross_check_failure(exc: BaseException) -> str:
    """Why the second reading failed, in the page's words. No body, no headers, no key."""
    if isinstance(exc, ConfigError):
        return "the second model is not set up on this server."
    if isinstance(exc, JSONParseError | ValidationError):
        return "the second model's reply could not be read."
    if isinstance(exc, APIStatusError):
        return f"the second model's provider refused the call (HTTP {exc.status_code})."
    if isinstance(exc, APIConnectionError):
        return "could not reach the second model."
    if isinstance(exc, ModelCancelled):
        return "it was cancelled."
    return "the second model's call failed."


def settle_cross_check(result: ExtractionResult, cross: CrossCheckCall, grace_s: float) -> dict:
    """The cross-check's outcome once the primary reading is back.

    A primary that fell back was answered by the cross-check's own model, so comparing
    would compare the model with itself: the second call is stopped and the cross-check is
    skipped. Otherwise it gets ``grace_s`` more seconds; if it is still not back, it is
    stopped and the upload goes on without it. Only a reading that did come back is
    compared — by :func:`crosscheck.compare`, in Python.
    """
    if result.fell_back:
        cross.abort()
        return outcome_skipped(SKIP_PRIMARY_FELL_BACK)
    if not cross.wait(grace_s):
        cross.abort()
        logger.info("cross-check not back %.1f s after the primary; going on without it", grace_s)
        return outcome_timeout()
    if isinstance(cross.error, ModelTimeout):
        return outcome_timeout()
    if cross.error is not None or cross.result is None:
        logger.info("cross-check failed (%s)", type(cross.error).__name__)
        return outcome_failed(_cross_check_failure(cross.error))
    return outcome_done(compare(result.drawing, cross.result.drawing), cross.result.model)


async def _unless_disconnected(
    request: Request,
    probe: DisconnectProbe,
    guard: GuardedClient | AbortGroup,
    what: str,
    func: Callable[..., Any],
    *args: Any,
) -> Any:
    """Run ``func(*args)`` on a worker thread, aborting ``guard`` if the browser leaves.

    Uvicorn does not stop an endpoint when its client disconnects; the endpoint would run
    to the end and the model call with it, which is what kept the provider busy after a
    Cancel. So while the worker runs, this checks every :data:`DISCONNECT_POLL_S` whether
    the browser is still there. If it has gone, the model call is aborted (closing its
    HTTP request), the worker is waited for — it ends at once with ``ModelCancelled`` —
    and :class:`ClientGone` is raised so the caller saves nothing.
    """
    work = asyncio.ensure_future(run_in_threadpool(func, *args))
    while True:
        done, _ = await asyncio.wait({work}, timeout=DISCONNECT_POLL_S)
        if done:
            return work.result()
        if await probe(request):
            break
    logger.info("%s cancelled by client", what)
    guard.abort()
    try:
        await work
    except Exception:  # noqa: BLE001, S110 - the outcome no longer matters to anyone
        pass
    raise ClientGone()


def get_runs_root() -> Path:
    """Where run folders live, as a dependency so tests can redirect it."""
    return RUNS_ROOT


#: The limits this process runs under, read once at import — see :mod:`draftcheck.limits`.
LIMITS = Limits.from_env()

#: The counters and the sweep clock. One of each per process, because both are what this
#: instance has spent and what it last cleaned up.
_LIMITER = UsageLimiter(LIMITS)
_CLEANER = Cleaner(LIMITS)


def get_limiter() -> UsageLimiter:
    """The usage counters, as a dependency so each test gets its own empty set."""
    return _LIMITER


def get_cleaner() -> Cleaner:
    """The cleanup schedule, as a dependency for the same reason."""
    return _CLEANER


def _resolve(dependency: Callable[[], Any]) -> Any:
    """Call a dependency the way a request would, honouring a test's override.

    Startup runs outside any request, so FastAPI is not there to inject anything — and a
    sweep that ignored the override would delete real run folders during the test suite.
    Looking the override up by hand keeps the one job startup has (find the runs root,
    clean it) pointed at whatever the app is actually configured with.
    """
    return app.dependency_overrides.get(dependency, dependency)()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Sweep stale run folders once, at startup.

    A deploy is the one moment something is guaranteed to run, so it is where the backlog
    gets cleared; after that cleanup rides along on uploads (:class:`limits.Cleaner`).
    """
    # Uvicorn configures only its own loggers, so ours would print nothing at INFO. This is
    # a no-op when something (pytest, a host) has already set up the root logger.
    logging.basicConfig(format="%(asctime)s %(levelname)s:     %(name)s - %(message)s")
    logging.getLogger("draftcheck").setLevel(logging.INFO)
    _resolve(get_cleaner).sweep(_resolve(get_runs_root), force=True)
    yield


def demo_is_allowed(request: Request) -> bool:
    """True when the demo route may answer: switched on, *and* called from this machine.

    Two independent gates, and both have to hold.

    ``DRAFTCHECK_ENABLE_DEMO=1`` is the deliberate act — the route is off unless somebody
    turned it on for this process, so nothing has to be removed before a deploy and
    forgetting is the safe direction.

    Loopback is the backstop, because an environment variable is exactly the thing that
    gets set once and inherited somewhere nobody meant it to be. It is a property of the
    connection rather than a setting, so it cannot be misconfigured — only bypassed by a
    reverse proxy on the same host, which is a thing to know before deploying it that way.
    """
    if os.environ.get(DEMO_ENV_VAR, "").strip() != "1":
        return False
    return bool(request.client) and request.client.host in LOOPBACK_HOSTS


#: Request pieces as ``Annotated`` aliases. FastAPI reads the marker out of the annotation
#: rather than out of a default value, which keeps the signatures short and readable.
RunsRoot = Annotated[Path, Depends(get_runs_root)]
DemoAllowed = Annotated[bool, Depends(demo_is_allowed)]
Limiter = Annotated[UsageLimiter, Depends(get_limiter)]
Cleanup = Annotated[Cleaner, Depends(get_cleaner)]
Clients = Annotated[ClientFactory, Depends(get_client_factory)]
ModelTimeoutS = Annotated[float, Depends(get_model_timeout)]
FallbackSettings = Annotated[FallbackPolicy, Depends(get_fallback_policy)]
CrossCheckGraceS = Annotated[float, Depends(get_cross_check_grace)]
Disconnects = Annotated[DisconnectProbe, Depends(get_disconnect_probe)]
DrawingUpload = Annotated[UploadFile, File(description="A .pdf, .png, or .jpg drawing.")]
ProviderField = Annotated[
    str | None, Form(description="baseten or meta. Default: the LLM_PROVIDER setting.")
]
CrossCheckField = Annotated[
    bool,
    Form(description="Also read the drawing with a second Meta model and compare. Default: off."),
]
DrawingBody = Annotated[dict, Body(description="The confirmed (possibly edited) drawing.")]


def get_cross_check_factory(request: Request, client_factory: Clients) -> ClientFactory:
    """Builds the cross-check's client: the request's own factory, or the cross-check fake.

    It follows :func:`get_client_factory`, so a test that replaces the model replaces the
    cross-check's too and nothing reaches the network. In fake-model mode the cross-check
    gets its own fake, whose reading differs from the primary's by one value.
    """
    if fake_model_is_allowed(request):
        return fake_cross_check_factory
    return client_factory


CrossCheckClients = Annotated[ClientFactory, Depends(get_cross_check_factory)]


app = FastAPI(
    title="DraftCheck",
    description="Upload an engineering drawing, confirm what was read, check it, fix it.",
    version="0.1.0",
    lifespan=lifespan,
)


def _claim(limiter: UsageLimiter, request: Request, what: str) -> None:
    """Spend one slot for this request, or refuse it with a 429.

    ``what`` is ``"upload"`` or ``"fix"``, naming which per-IP budget to draw on. The
    message is the same either way (:data:`limits.LIMIT_MESSAGE`), and the page shows it
    as it shows any other refusal, because it arrives in ``detail`` like all the rest.
    """
    ip = client_ip(
        request.headers.get("x-forwarded-for"),
        request.client.host if request.client else None,
        limiter.limits.trust_proxy,
    )
    claim = limiter.claim_upload if what == "upload" else limiter.claim_fix
    try:
        claim(ip)
    except LimitExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


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


def _render_and_extract(
    source: Path,
    run_dir: Path,
    client: GuardedClient,
    cross: CrossCheckCall | None = None,
    grace_s: float = 0.0,
) -> tuple[ExtractionResult, dict | None]:
    """The slow, blocking half of an upload: render page 1, save it, read it with the model.

    Plain synchronous code, run in a worker thread by :func:`create_run`. Rendered once:
    the same PNG is saved for the browser and the correction loop, and sent to the model —
    to both models when there is a cross-check, which starts first so the two run side by
    side. Only page 1 is rasterized, at the server's settings.

    Returns the primary reading and the cross-check's outcome (``None`` without one).
    """
    dpi, max_side = render_settings()
    png_bytes = load_pages(source, dpi=dpi, max_side=max_side, max_pages=1)[0].png_bytes
    (run_dir / PAGE_PNG).write_bytes(png_bytes)
    logger.info(
        "render done (%d dpi, max side %d, PNG %.1f MB, memory %.0f MB)",
        dpi,
        max_side,
        len(png_bytes) / 1e6,
        memory_mb(),
    )

    if cross is not None:
        cross.start(png_bytes)
    # The page is on disk now and the model calls hold their own references to the bytes,
    # so this function's copy goes as soon as they return.
    result = extract_page(png_bytes, client)
    return result, (settle_cross_check(result, cross, grace_s) if cross is not None else None)


@app.post("/api/runs", status_code=201)
async def create_run(
    request: Request,
    file: DrawingUpload,
    root: RunsRoot,
    client_factory: Clients,
    limiter: Limiter,
    cleaner: Cleanup,
    timeout_s: ModelTimeoutS,
    fallback: FallbackSettings,
    disconnected: Disconnects,
    cross_factory: CrossCheckClients,
    grace_s: CrossCheckGraceS,
    provider: ProviderField = None,
    cross_check: CrossCheckField = False,
) -> dict:
    """Upload a drawing, render page 1, and read it with the model.

    The extraction comes back for the user to confirm or edit; nothing is checked yet. If
    the browser goes away first (Cancel), the model calls are aborted and the run folder is
    deleted: a cancelled upload leaves nothing behind.

    With ``cross_check`` on and Baseten reading, Meta reads the same page at the same time
    and the two readings are compared (:mod:`draftcheck.crosscheck`). The comparison is a
    hint for the reviewer; the tables and the rules use the primary reading only.
    """
    logger.info("upload received (memory %.0f MB)", memory_mb())
    suffix = _checked_suffix(file.filename)
    provider_name = _checked_provider(provider)

    # Claimed after the cheap checks, so a wrong file type does not cost the visitor one
    # of their five, and before anything is written or sent, so a refusal costs nothing.
    _claim(limiter, request, "upload")

    # The only cleanup trigger after startup. `sweep` is a no-op unless an hour has gone
    # by, so this costs a clock read on all but one upload per hour — but the one that does
    # sweep deletes folders, so it runs in a worker thread like the rest of the slow work.
    await run_in_threadpool(cleaner.sweep, root)

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

    guard: GuardedClient | None = None
    cross: CrossCheckCall | None = None
    cross_outcome: dict | None = None
    try:
        client = client_factory(provider_name)
        guard = guard_for(client, provider_name, client_factory, timeout_s, fallback)
        if not cross_check:
            cross_outcome = outcome_off()
        elif guard.provider != CROSS_CHECKS_FROM:
            cross_outcome = outcome_skipped(SKIP_PRIMARY_IS_CROSS)
        else:
            try:
                cross = CrossCheckCall(
                    GuardedClient(
                        cross_factory(CROSS_CHECK_PROVIDER),
                        timeout_s,
                        provider=CROSS_CHECK_PROVIDER,
                    )
                )
            except ConfigError:
                # No Meta key, say. The primary reading is unaffected; the page says why.
                cross_outcome = outcome_failed(_cross_check_failure(ConfigError()))
        # This endpoint is async (it streams the upload), so anything slow called directly
        # here would stall the event loop — every other request, /healthz included, would
        # wait for the model. Render restarts an instance whose health check fails for 60 s,
        # so the render and the model call go to a worker thread and this coroutine waits,
        # watching for the browser to leave while it does.
        result, settled = await _unless_disconnected(
            request,
            disconnected,
            AbortGroup(guard, cross),
            "upload",
            _render_and_extract,
            source,
            run_dir,
            guard,
            cross,
            grace_s,
        )
        cross_outcome = settled or cross_outcome
    except ClientGone:
        await run_in_threadpool(shutil.rmtree, run_dir, ignore_errors=True)
        raise HTTPException(status_code=CANCELLED_STATUS, detail="Cancelled by client.") from None
    except RenderError as exc:
        # Covers a file that is named .pdf and is not one: `load_pages` translates
        # pypdfium2's and pillow's own errors into RenderError, so a corrupt upload is the
        # client's problem with one exception to catch rather than three.
        raise HTTPException(status_code=400, detail=f"Could not read the drawing: {exc}") from exc
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Configuration error: {exc}") from exc
    except ModelTimeout as exc:
        raise HTTPException(status_code=504, detail=MODEL_TIMEOUT_MESSAGE) from exc
    except JSONParseError as exc:
        raise HTTPException(
            status_code=502, detail=f"The model did not return usable JSON: {exc}"
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=502, detail=f"Extraction does not match the drawing schema: {exc}"
        ) from exc
    except (APIStatusError, APIConnectionError) as exc:
        raise HTTPException(status_code=502, detail=_provider_error(exc)) from exc
    finally:
        # Whatever happened, a cross-check still running is no longer wanted.
        if cross is not None and not cross.done:
            cross.abort()
        # The upload's claim paid for one call. A fallback was a second, whether or not it
        # answered, and a cross-check that was sent is another; the day's budget is
        # charged for both here on every way out.
        extra = guard.fallback_calls if guard is not None else 0
        if cross is not None and cross.started:
            extra += 1
        limiter.record_model_calls(extra)

    drawing = result.drawing.model_dump(mode="json")
    meta = {
        "run_id": str(run_id),
        "source": file.filename,
        "provider": provider_name or getattr(getattr(client, "provider", None), "name", None),
        "model": result.model,
        **_answered_by(result),
        "latency_s": result.latency_s,
        "attempts": result.attempts,
        "cross_check": cross_outcome,
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
        **_answered_by(result),
        "latency_s": result.latency_s,
        "attempts": result.attempts,
        "page_url": f"/api/runs/{run_id}/page.png",
        "fake_model": is_fake(client),
        "cross_check": cross_outcome,
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

    results = rule_results(drawing, findings)

    errors = sum(1 for finding in findings if finding.severity == "error")
    return {
        "run_id": str(run_id),
        "findings": [finding_payload(finding) for finding in findings],
        # Every rule's verdict on the whole drawing, passes included, in registry order.
        "rule_results": [result.model_dump(mode="json") for result in results],
        "checks_summary": checks_summary(results),
        "parse_issues": drawing.parse_issues,
        "error_count": errors,
        "warning_count": len(findings) - errors,
        "rules_run": len(RULES),
    }


#: Each rule's short name, by rule id, for the page to head a finding with.
RULE_TITLES = {rule.id: rule.title for rule in RULES}


def finding_payload(finding: Finding) -> dict:
    """A finding as the page shows it: the finding itself plus its rule's short title.

    A ``Finding`` carries its rule's id but not its name, and the page should not keep its
    own copy of the rules to look one up. A rule id the table does not know gets its id as
    the title, so a card is never headed with nothing.
    """
    payload = finding.model_dump(mode="json")
    payload["title"] = RULE_TITLES.get(finding.rule_id, finding.rule_id)
    # The name /fix reports its verdicts under, so the page can match the two up.
    payload["key"] = finding_key(finding)
    return payload


def _display_fields(display: EditDisplay | None) -> dict:
    """The short before/after/pen strings, or ``None`` for each when there are none."""
    if display is None:
        return {"before": None, "after": None, "pen": None, "target_id": None}
    return {
        "before": display.before,
        "after": display.after,
        "pen": display.pen,
        "target_id": display.target_id,
    }


def _rejected_summary(item: RejectedEdit, names: EditNames, drawing: Drawing) -> dict:
    """A refused proposal as the page shows it, in the same words as an applied one.

    A proposal can be refused before it is understood (``{"op": "delete_fcf"}``) or after
    (an edit that names an id the drawing does not have), so the sentence is only available
    when ``parse_edit`` can make an ``Edit`` out of it. When it cannot, the page says what
    the model called the operation rather than inventing a description of it, and there is
    no before/after to show.
    """
    raw = item.edit
    edit, _ = parse_edit(raw)
    display = None
    finding_ids: list[str] = []
    if edit is not None:
        sentence, rationale = plain_edit(edit, names), edit.rationale
        display = edit_display(edit, drawing, None)
        finding_ids = edit.finding_ids
    elif isinstance(raw, dict) and isinstance(raw.get("op"), str) and raw["op"].strip():
        sentence, rationale = f"an edit the model called {raw['op'].strip()!r}", ""
    else:
        sentence, rationale = "an edit that was not shaped like an edit", ""
    return {
        "edit": sentence,
        "rationale": rationale,
        "reason": item.reason,
        "finding_ids": finding_ids,
        **_display_fields(display),
        "status": "rejected",
        "note": f"Refused before the rules ran: {item.reason}",
    }


#: Operations that can pass the rules and still need a designer. Each describes a feature
#: that is not on the sheet yet — a datum symbol or a dimension given in words — and only
#: the designer can say where on the part it really goes.
HELD_OPS = frozenset({"add_datum", "add_dimension"})


def edit_verdict(edit: Edit, record: RoundRecord) -> tuple[str, str]:
    """What the rule engine made of one applied edit: ``(status, note)``.

    ``status`` is ``"verified"`` when every finding the edit cites was gone on the re-run,
    ``"held"`` when that is so but the edit adds a feature a designer has to place
    (:data:`HELD_OPS`), and ``"failed"`` otherwise. The citation is the model's claim and
    is used only to say *which* finding an edit is about; whether that finding cleared is
    ``record.resolved``, computed by re-running the rules.
    """
    cited = list(edit.finding_ids)
    if not cited:
        return "failed", "It cites no finding, so the re-run has nothing to confirm."
    still = [key for key in cited if key not in record.resolved]
    if still:
        return "failed", f"The re-run still reports {', '.join(still)}."
    cleared = ", ".join(cited)
    if edit.op in HELD_OPS:
        return "held", f"The re-run no longer reports {cleared}; a designer has to place it."
    return "verified", f"The re-run no longer reports {cleared}."


def _applied_summary(
    edit: Edit, names: EditNames, record: RoundRecord, before: Drawing, after: Drawing
) -> dict:
    """One applied edit: its sentence, its before/after on the sheet, and its verdict."""
    status, note = edit_verdict(edit, record)
    return {
        "edit": plain_edit(edit, names),
        "rationale": edit.rationale,
        "finding_ids": edit.finding_ids,
        **_display_fields(edit_display(edit, before, after)),
        "status": status,
        "note": note,
    }


def _named_findings(findings: list[Finding], keys: list[str]) -> list[dict]:
    """Finding keys as key *and* message, so the page need not hold a copy of the rules."""
    messages = {finding_key(finding): finding.message for finding in findings}
    return [{"key": key, "message": messages.get(key, key)} for key in keys]


def _round_summary(record: RoundRecord, names: EditNames, start: Drawing) -> tuple[dict, Drawing]:
    """One round as the page shows it, and the drawing as it stands after the round.

    The edits are in plain English — the same sentences :mod:`draftcheck.redline` prints on
    page 2, built by the same function, so the screen and the PDF can never word a
    correction differently. ``corrections.json`` keeps the id-carrying form, which is what
    is wanted when you are reading the file rather than the drawing.

    The report keeps each round's edits but not the drawing between them, so the edits are
    applied again here, one at a time from ``start``, to read every edit's before and after
    off the drawing exactly as it was either side of it. ``apply_edits`` is deterministic,
    so this lands on the same drawing the loop did.
    """
    applied = []
    current = start
    for edit in record.applied:
        changed, took, _ = apply_edits(current, [edit])
        after = changed if took else current
        applied.append(_applied_summary(edit, names, record, current, after))
        current = after
    summary = {
        "round": record.number,
        "applied": applied,
        # Refused edits are shown against the drawing as the round found it.
        "rejected": [_rejected_summary(item, names, start) for item in record.rejected],
        "findings_before": len(record.findings_before),
        "findings_after": len(record.findings_after),
        # Resolved findings are gone by the end of the round, so their wording comes from
        # before it; introduced ones did not exist until after it.
        "resolved": _named_findings(record.findings_before, record.resolved),
        "introduced": _named_findings(record.findings_after, record.introduced),
        "error": record.error,
        "model": record.model,
        "provider": record.provider,
        "fell_back": record.fell_back,
        "fallback_reason": record.fallback_reason,
    }
    return summary, current


def _outcomes(report: CorrectionReport, rounds: list[dict]) -> list[dict]:
    """Each original finding's verdict, plus whether a designer still has to act on it.

    ``outcome`` is ``"unresolved"`` when the rules still report the finding, ``"held"``
    when it cleared but only through an edit a designer has to place, and ``"verified"``
    otherwise. The first comes straight from ``report.statuses``; the split of resolved
    into the other two follows each applied edit's own status.
    """
    held = {
        key
        for summary in rounds
        for edit in summary["applied"]
        if edit["status"] == "held"
        for key in edit["finding_ids"]
    }
    entries = []
    for entry in report.to_dict()["status"]:
        if entry["status"] != "resolved":
            outcome = "unresolved"
        else:
            outcome = "held" if entry["finding"] in held else "verified"
        entries.append({**entry, "outcome": outcome})
    return entries


def _summarize(report: CorrectionReport) -> dict:
    """The correction report as the UI needs it: rounds, edits, and the verdicts."""
    payload = report.to_dict()
    names = EditNames(report)
    rounds = []
    current = report.original_drawing
    for record in report.rounds:
        summary, current = _round_summary(record, names, current)
        rounds.append(summary)
    return {
        "rounds": rounds,
        "status": _outcomes(report, rounds),
        "final_findings": payload["final_findings"],
        "final_drawing": payload["final_drawing"],
        "applied_count": sum(len(record.applied) for record in report.rounds),
        "rejected_count": sum(len(record.rejected) for record in report.rounds),
        "resolved_count": report.resolved_count,
        "unresolved_count": report.unresolved_count,
    }


def _answered_by(result: ExtractionResult) -> dict:
    """Who read the drawing, for the upload response and ``meta.json``.

    ``model`` (beside this) is the model that answered. ``provider`` in ``meta.json`` stays
    the one that was asked for, because it is what the fix uses — so a fallback during the
    upload does not quietly move every later round off Baseten.
    """
    return {
        "answered_provider": result.provider,
        "fell_back": result.fell_back,
        "fallback_reason": result.fallback_reason,
    }


def stamp_model_name(meta: dict, report: CorrectionReport) -> str | None:
    """The model(s) that answered this run, for the redline's stamp.

    The extraction's model and each round's, in order and without repeats, joined with
    ``+``; ``(fallback)`` is added when any of those calls was answered by the backup.
    """
    names: list[str] = []
    for name in [meta.get("model"), *(record.model for record in report.rounds)]:
        if name and name not in names:
            names.append(name)
    if not names:
        return None
    fell_back = bool(meta.get("fell_back")) or any(r.fell_back for r in report.rounds)
    return " + ".join(names) + (" (fallback)" if fell_back else "")


def _provider_error(exc: APIStatusError | APIConnectionError) -> str:
    """What the page is told when a provider refused the call. No body, no headers."""
    if isinstance(exc, APIStatusError):
        return f"The model provider refused the request (HTTP {exc.status_code})."
    return "Could not reach the model provider."


def final_rule_results(report: CorrectionReport):
    """Every rule's verdict on the corrected drawing, from a fresh run of the rules.

    The rules are run again here on ``final_drawing`` rather than read from the report or
    from anything the model said. Issue numbers count into the *submitted* findings, so a
    finding that survived keeps its card's number and a new one is called new.
    """
    return rule_results(report.final_drawing, numbering=report.original_findings)


def _save_fix(run_dir: Path, report: CorrectionReport, page_png: bytes, meta: dict) -> None:
    """Write ``corrections.json`` and the two redline files. Slow; runs in a worker thread."""
    _write_json(run_dir / CORRECTIONS_JSON, report.to_dict())

    # The redline marks the drawing *as submitted*, so its findings and its numbering match
    # findings.json; the report adds each applied edit and the rule engine's verdict.
    pages = render_redline(
        page_png,
        report.original_findings,
        report,
        model_name=stamp_model_name(meta, report),
        rule_results=rule_results(report.original_drawing, report.original_findings),
        final_rule_results=final_rule_results(report),
    )
    save_redline(pages, run_dir / REDLINE_PDF, run_dir / REDLINE_PNG)


# ``async def`` so it can watch for the browser leaving, but everything slow — the loop's
# model calls and the redline drawing — goes to a worker thread. Calling either directly
# here would freeze the event loop, and /healthz with it, for the length of a fix.
@app.post("/api/runs/{run_id}/fix")
async def fix_run(
    request: Request,
    run_id: uuid.UUID,
    root: RunsRoot,
    client_factory: Clients,
    limiter: Limiter,
    timeout_s: ModelTimeoutS,
    fallback: FallbackSettings,
    disconnected: Disconnects,
) -> dict:
    """Run the correction loop on the confirmed drawing and write the redline.

    Whether a finding cleared is decided by ``agent.correct`` re-running the rules, never
    by the model's account of its own edits. This endpoint only reports what it is told.

    A model call that runs out of time ends the loop at that round. If it was round one
    there is nothing to report and the request is a 504; otherwise the rounds that did run
    are reported as usual, and the last one says it timed out.

    If the browser goes away (Cancel), the model call in flight is aborted, the loop stops
    before another round, and nothing is written: the run keeps its previous results.
    """
    run_dir = _run_dir(root, run_id)
    confirmed = run_dir / CONFIRMED_JSON
    if not confirmed.is_file():
        raise HTTPException(
            status_code=409,
            detail="Confirm the extraction with POST /api/runs/{run_id}/check first.",
        )

    # After the 409: a run that is not ready to be fixed should not spend a fix slot.
    _claim(limiter, request, "fix")

    drawing = parse_drawing(_read_json(confirmed))
    page_png = _existing_file(run_dir, PAGE_PNG, "the upload").read_bytes()

    meta = _read_json(run_dir / META_JSON) if (run_dir / META_JSON).is_file() else {}
    try:
        client = client_factory(meta.get("provider"))
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Configuration error: {exc}") from exc

    guard = guard_for(client, meta.get("provider"), client_factory, timeout_s, fallback)
    try:
        report = await _unless_disconnected(
            request, disconnected, guard, "fix", correct, drawing, page_png, guard
        )
    except ClientGone:
        raise HTTPException(status_code=CANCELLED_STATUS, detail="Cancelled by client.") from None
    finally:
        # Every fallback was one more model call than the rounds alone account for.
        limiter.record_model_calls(guard.fallback_calls)

    # The claim above paid for round one, which is all that could be known before the loop
    # ran. Each further round was another model call, so the day's budget is charged for
    # them now that the report says how many there were.
    limiter.record_model_calls(len(report.rounds) - 1)

    if report.rounds and report.rounds[0].timed_out:
        raise HTTPException(status_code=504, detail=MODEL_TIMEOUT_MESSAGE)

    await run_in_threadpool(_save_fix, run_dir, report, page_png, meta)

    summary = _summarize(report)
    after = final_rule_results(report)
    summary["final_rule_results"] = [result.model_dump(mode="json") for result in after]
    summary["final_checks_summary"] = checks_summary(after)
    answered = [record for record in report.rounds if record.model]
    summary["model"] = answered[-1].model if answered else None
    summary["fell_back"] = any(record.fell_back for record in report.rounds)
    summary["run_id"] = str(run_id)
    summary["fake_model"] = is_fake(client)
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


@app.post("/api/demo/low-confidence", status_code=201)
def create_demo_run(allowed: DemoAllowed, root: RunsRoot) -> dict:
    """Create a run from a saved extraction, with two items marked unsure.

    What the review screen does with low confidence is a property of the page, not of the
    model, and checking it should not cost a model call — or a key, which a reviewer
    looking at the UI may not have. So this builds a real run folder out of the plate_001
    fixture and the sample drawing: the page shows it exactly as it shows an upload, and
    ``/check`` works on it afterwards like any other run.

    **Off by default, and local only.** The route answers nothing unless
    ``DRAFTCHECK_ENABLE_DEMO=1`` is set *and* the caller is on this machine — see
    :func:`demo_is_allowed`. A deployed DraftCheck therefore carries no demo data whether
    or not anybody remembered to think about it.
    """
    if not allowed:
        raise HTTPException(status_code=404, detail="Not Found")
    if not DEMO_EXTRACTION.is_file() or not DEMO_PAGE.is_file():
        raise HTTPException(
            status_code=404,
            detail="The demo files are not here. They ship with the repository, not the package.",
        )

    drawing_json = _read_json(DEMO_EXTRACTION)
    # The first dimension and the frame: one marked row in each of two different tables,
    # which is what makes the markings worth looking at.
    for key in ("dimensions", "fcfs"):
        items = drawing_json.get(key) or []
        if items:
            items[0]["confidence"] = DEMO_CONFIDENCE

    drawing = parse_drawing(drawing_json)

    run_id = uuid.uuid4()
    run_dir = root / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / PAGE_PNG).write_bytes(DEMO_PAGE.read_bytes())

    payload = drawing.model_dump(mode="json")
    meta = {
        "run_id": str(run_id),
        "source": DEMO_PAGE.name,
        "provider": None,
        "model": "demo (no model call)",
        "latency_s": 0.0,
        "attempts": 0,
        "created": datetime.now(UTC).isoformat(),
    }
    _write_json(run_dir / EXTRACTION_JSON, payload)
    _write_json(run_dir / META_JSON, meta)

    return {
        "run_id": str(run_id),
        "drawing": payload,
        "parse_issues": drawing.parse_issues,
        "model": meta["model"],
        "latency_s": meta["latency_s"],
        "attempts": meta["attempts"],
        "page_url": f"/api/runs/{run_id}/page.png",
    }


@app.get("/api/model-mode")
def get_model_mode(request: Request) -> dict:
    """Whether this browser's model calls go to the development fake.

    Read by the page on load, so its "Fake model (dev)" badge is right before the first
    upload rather than only after it. Says nothing about keys or providers.
    """
    return {"fake_model": fake_model_is_allowed(request)}


@app.get("/samples/plate_001.pdf")
def get_sample_pdf() -> FileResponse:
    """The generated sample drawing, for the page's "Try the sample drawing" button.

    The page fetches this and uploads it through ``POST /api/runs`` exactly as it would a
    file the visitor picked, so the sample costs the same model call and the same limits.
    """
    if not SAMPLE_PDF.is_file():
        raise HTTPException(
            status_code=404,
            detail="The sample drawing is not here. It ships with the repository.",
        )
    return FileResponse(SAMPLE_PDF, media_type="application/pdf", filename=SAMPLE_PDF.name)


@app.get("/healthz")
def healthz() -> dict:
    """Is the process up and serving? Nothing more.

    A host's health check runs this every few seconds, so it must touch no model, no key,
    and no disk: a probe that called the provider would spend the day's budget on being
    asked whether we are alive, and would report us unhealthy whenever the provider
    hiccuped — which is a different question from the one being asked.
    """
    return {"status": "ok"}


# Mounted last, so every /api route above wins over the static file server.
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
