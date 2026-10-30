"""What one demo deployment is willing to spend, and on whom.

DraftCheck is a free demo paid for out of one person's model credits, so the thing that
has to be true after a deploy is that no single visitor — and no single day — can empty
the account. That is three separate limits, and they fail for different reasons:

======================= ==========================================================
per-IP uploads          one visitor cannot loop the expensive path
per-IP fix rounds       the correction loop is several model calls, not one
global model calls/day  a crowd, or a visitor behind many addresses, still stops
======================= ==========================================================

Every number lives in :class:`Limits` and nowhere else, and each one reads an environment
variable with the default written here, so a deployment can be tightened without a code
change and the defaults are still visible in one place.

**State is in memory.** The app runs as a single instance, so a dict of timestamps is the
whole implementation and a restart simply forgives everyone. A second instance would need
a shared store — that is a property of this deployment, not an oversight.

This module raises :class:`LimitExceeded` rather than an ``HTTPException``: deciding what
a limit *is* belongs here, and turning a refusal into a status code belongs to
:mod:`draftcheck.api`, the same split :class:`~draftcheck.render.RenderError` has.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: What a refused request says, whichever limit it hit. Deliberately the same sentence for
#: all three: which budget ran out is our business, and a visitor can do nothing with the
#: difference except learn how to get around it.
LIMIT_MESSAGE = "DraftCheck's demo limit is reached; please try again later."

#: The defaults, as the environment variable that overrides each one.
ENV_UPLOADS_PER_HOUR = "DRAFTCHECK_MAX_UPLOADS_PER_HOUR"
ENV_FIXES_PER_HOUR = "DRAFTCHECK_MAX_FIXES_PER_HOUR"
ENV_MODEL_CALLS_PER_DAY = "DRAFTCHECK_MAX_MODEL_CALLS_PER_DAY"
ENV_RUN_RETENTION_HOURS = "DRAFTCHECK_RUN_RETENTION_HOURS"
ENV_CLEANUP_INTERVAL_SECONDS = "DRAFTCHECK_CLEANUP_INTERVAL_SECONDS"
ENV_TRUST_PROXY = "TRUST_PROXY"

DEFAULT_UPLOADS_PER_HOUR = 5
DEFAULT_FIXES_PER_HOUR = 10
DEFAULT_MODEL_CALLS_PER_DAY = 100
DEFAULT_RUN_RETENTION_HOURS = 24
DEFAULT_CLEANUP_INTERVAL_SECONDS = 3600

HOUR_SECONDS = 3600
DAY_SECONDS = 24 * HOUR_SECONDS


class LimitExceeded(RuntimeError):
    """A request that would go over one of the limits. The API answers it with a 429."""

    def __init__(self, message: str = LIMIT_MESSAGE) -> None:
        super().__init__(message)


def _int_env(name: str, default: int) -> int:
    """An environment variable as a positive int, or the default if it is not usable.

    A misread variable falls back rather than crashing the process: a deploy that starts
    with the documented default is a better outcome than one that will not start at all
    because a value was typed with a comma in it.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class Limits:
    """Every number this module enforces, in one object.

    Built with :meth:`from_env` at import time in :mod:`draftcheck.api`. Tests construct
    one directly with small numbers instead of setting environment variables.
    """

    uploads_per_ip_per_hour: int = DEFAULT_UPLOADS_PER_HOUR
    fixes_per_ip_per_hour: int = DEFAULT_FIXES_PER_HOUR
    model_calls_per_day: int = DEFAULT_MODEL_CALLS_PER_DAY
    run_retention_hours: int = DEFAULT_RUN_RETENTION_HOURS
    cleanup_interval_seconds: int = DEFAULT_CLEANUP_INTERVAL_SECONDS

    #: Whether ``X-Forwarded-For`` may be believed — see :func:`client_ip`.
    trust_proxy: bool = False

    @classmethod
    def from_env(cls) -> Limits:
        return cls(
            uploads_per_ip_per_hour=_int_env(ENV_UPLOADS_PER_HOUR, DEFAULT_UPLOADS_PER_HOUR),
            fixes_per_ip_per_hour=_int_env(ENV_FIXES_PER_HOUR, DEFAULT_FIXES_PER_HOUR),
            model_calls_per_day=_int_env(ENV_MODEL_CALLS_PER_DAY, DEFAULT_MODEL_CALLS_PER_DAY),
            run_retention_hours=_int_env(ENV_RUN_RETENTION_HOURS, DEFAULT_RUN_RETENTION_HOURS),
            cleanup_interval_seconds=_int_env(
                ENV_CLEANUP_INTERVAL_SECONDS, DEFAULT_CLEANUP_INTERVAL_SECONDS
            ),
            trust_proxy=os.environ.get(ENV_TRUST_PROXY, "").strip() == "1",
        )


#: What an address is called when the server cannot tell. All such requests share one
#: bucket, which is the safe direction: unknown callers are limited together.
UNKNOWN_IP = "unknown"


def client_ip(forwarded_for: str | None, connection_host: str | None, trust_proxy: bool) -> str:
    """Which address a per-IP limit should count this request against.

    ``X-Forwarded-For`` is a header, so it is whatever the client typed unless something
    in front of us overwrites it. Believing it unconditionally would make every per-IP
    limit free to bypass — send a new address each time. So it is read **only** when
    ``TRUST_PROXY=1`` says this process really is behind a proxy that sets it; otherwise
    the address the connection actually came from is used, which cannot be forged.

    The first entry is the one taken: a proxy chain appends, so the left-most value is the
    original client and the rest are the hops. (That left-most value is also the part the
    client itself can write, which is exactly why the flag has to be deliberate.)
    """
    if trust_proxy and forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return (connection_host or "").strip() or UNKNOWN_IP


class UsageLimiter:
    """Counts recent requests per address and recent model calls overall.

    Each window is a deque of timestamps with the stale end dropped before every decision,
    which keeps "the last hour" literal — a sliding window rather than a bucket that
    everyone gets back at the top of the hour.

    The clock is :func:`time.monotonic` so the windows are immune to the system clock being
    corrected underneath them, and injectable so a test can move an hour without waiting
    one. Mutations are under a lock because FastAPI runs synchronous endpoints in a thread
    pool, so two uploads really can land at once.
    """

    def __init__(
        self,
        limits: Limits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits or Limits()
        self._clock = clock
        self._lock = threading.Lock()
        self._uploads: dict[str, deque[float]] = {}
        self._fixes: dict[str, deque[float]] = {}
        self._model_calls: deque[float] = deque()

    @staticmethod
    def _prune(stamps: deque[float], cutoff: float) -> None:
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()

    def _claim(self, per_ip: dict[str, deque[float]], ip: str, allowed: int, window: float) -> None:
        """Spend one per-IP slot *and* one global model call, or spend neither.

        Both are taken under one lock, and the per-IP slot is only appended once the global
        budget has also been checked. A request refused by the day cap therefore does not
        quietly consume the visitor's hourly allowance on the way out.
        """
        now = self._clock()
        with self._lock:
            stamps = per_ip.setdefault(ip, deque())
            self._prune(stamps, now - window)
            self._prune(self._model_calls, now - DAY_SECONDS)

            if len(stamps) >= allowed:
                raise LimitExceeded()
            if len(self._model_calls) >= self.limits.model_calls_per_day:
                raise LimitExceeded()

            stamps.append(now)
            self._model_calls.append(now)

    def claim_upload(self, ip: str) -> None:
        """One upload: one per-IP upload slot and one model call. Raises when over."""
        self._claim(self._uploads, ip, self.limits.uploads_per_ip_per_hour, HOUR_SECONDS)

    def claim_fix(self, ip: str) -> None:
        """One fix request: one per-IP fix slot and the loop's first round."""
        self._claim(self._fixes, ip, self.limits.fixes_per_ip_per_hour, HOUR_SECONDS)

    def record_model_calls(self, count: int) -> None:
        """Charge model calls already made, over and above the one that was claimed.

        The correction loop runs up to three rounds and only says how many afterwards, so
        the day's budget is charged for round one up front — never start what cannot be
        paid for — and the rest are added here once the report exists. The day cap is
        therefore an accurate count rather than an estimate, and it may be pushed slightly
        past its limit by a loop already in flight, which is the right way round.
        """
        if count <= 0:
            return
        now = self._clock()
        with self._lock:
            self._prune(self._model_calls, now - DAY_SECONDS)
            self._model_calls.extend([now] * count)

    def model_calls_today(self) -> int:
        """How much of the day's budget is spent. For tests and for looking at."""
        now = self._clock()
        with self._lock:
            self._prune(self._model_calls, now - DAY_SECONDS)
            return len(self._model_calls)


def sweep_runs(root: Path, max_age_hours: int, now: float | None = None) -> list[str]:
    """Delete run folders last touched more than ``max_age_hours`` ago.

    A run folder is a visitor's uploaded drawing. Keeping those forever is both a disk
    bill and a promise nobody made, so a demo forgets them the next day.

    Age is the folder's own modification time, which every write into the run bumps, so a
    run being worked on is never swept out from under its own browser tab. Only
    directories are considered — anything else under the root was not put there by a run.
    Returns the names removed, which is what lets a test say what happened; a folder that
    cannot be read or removed is skipped rather than taking the request down with it.
    """
    if not root.is_dir():
        return []

    cutoff = (time.time() if now is None else now) - max_age_hours * HOUR_SECONDS
    removed: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(entry)
        except OSError:
            continue
        removed.append(entry.name)
    return removed


class Cleaner:
    """Runs :func:`sweep_runs`, and not more often than it is worth running.

    Cleanup has no scheduler behind it — there is no background task and nothing to keep
    alive between requests — so it happens at startup and then rides along on uploads.
    That makes "once an hour" a ceiling rather than a promise: a server nobody uploads to
    does not sweep, which is fine, because a server nobody uploads to is not accumulating
    anything either.
    """

    def __init__(
        self,
        limits: Limits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits or Limits()
        self._clock = clock
        self._lock = threading.Lock()
        self._last_sweep: float | None = None

    def _due(self) -> bool:
        """True when a sweep is owed, marking it started so two uploads do not both go."""
        now = self._clock()
        with self._lock:
            last = self._last_sweep
            if last is not None and now - last < self.limits.cleanup_interval_seconds:
                return False
            self._last_sweep = now
            return True

    def sweep(self, root: Path, *, force: bool = False) -> list[str]:
        """Sweep if one is due, or unconditionally with ``force`` (used at startup)."""
        if force:
            with self._lock:
                self._last_sweep = self._clock()
        elif not self._due():
            return []
        return sweep_runs(root, self.limits.run_retention_hours)
