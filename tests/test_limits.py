"""Tests for the demo's spending limits and its housekeeping.

Nothing here touches the network or the real clock: :class:`UsageLimiter` and
:class:`Cleaner` both take a clock, so "an hour later" is a number this file hands them.
The HTTP side of the same rules — which requests answer 429 — is in ``test_api.py``.
"""

import os
import time
from pathlib import Path

import pytest

from draftcheck.limits import (
    DAY_SECONDS,
    HOUR_SECONDS,
    UNKNOWN_IP,
    Cleaner,
    LimitExceeded,
    Limits,
    UsageLimiter,
    client_ip,
    sweep_runs,
)


class FakeClock:
    """A clock the test moves by hand, in seconds."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_run(root: Path, name: str, age_hours: float) -> Path:
    """A run folder with one file in it, last modified ``age_hours`` ago."""
    run = root / name
    run.mkdir(parents=True)
    (run / "page.png").write_bytes(b"\x89PNG")
    when = time.time() - age_hours * HOUR_SECONDS
    os.utime(run, (when, when))
    return run


# ---- The defaults and their environment variables ---------------------------


def test_defaults_are_the_documented_numbers() -> None:
    limits = Limits()
    assert limits.uploads_per_ip_per_hour == 5
    assert limits.fixes_per_ip_per_hour == 10
    assert limits.model_calls_per_day == 100
    assert limits.run_retention_hours == 24
    assert limits.trust_proxy is False


def test_environment_overrides_each_number(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRAFTCHECK_MAX_UPLOADS_PER_HOUR", "2")
    monkeypatch.setenv("DRAFTCHECK_MAX_FIXES_PER_HOUR", "3")
    monkeypatch.setenv("DRAFTCHECK_MAX_MODEL_CALLS_PER_DAY", "7")
    monkeypatch.setenv("DRAFTCHECK_RUN_RETENTION_HOURS", "1")
    monkeypatch.setenv("TRUST_PROXY", "1")

    limits = Limits.from_env()
    assert (limits.uploads_per_ip_per_hour, limits.fixes_per_ip_per_hour) == (2, 3)
    assert limits.model_calls_per_day == 7
    assert limits.run_retention_hours == 1
    assert limits.trust_proxy is True


def test_an_unusable_value_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in a variable must not stop the process from starting."""
    monkeypatch.setenv("DRAFTCHECK_MAX_UPLOADS_PER_HOUR", "five")
    monkeypatch.setenv("DRAFTCHECK_MAX_FIXES_PER_HOUR", "-1")
    limits = Limits.from_env()
    assert limits.uploads_per_ip_per_hour == 5
    assert limits.fixes_per_ip_per_hour == 10


def test_trust_proxy_is_only_the_exact_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything other than ``1`` leaves the header untrusted."""
    for value in ("", "0", "true", "yes"):
        monkeypatch.setenv("TRUST_PROXY", value)
        assert Limits.from_env().trust_proxy is False


# ---- Which address a request is counted against -----------------------------


def test_forwarded_for_is_ignored_unless_the_proxy_is_trusted() -> None:
    """The header is the client's to write, so it counts for nothing by default."""
    assert client_ip("9.9.9.9", "10.0.0.1", trust_proxy=False) == "10.0.0.1"


def test_forwarded_for_is_used_when_the_proxy_is_trusted() -> None:
    assert client_ip("9.9.9.9", "10.0.0.1", trust_proxy=True) == "9.9.9.9"


def test_the_first_forwarded_address_is_the_client() -> None:
    """A proxy chain appends, so the left-most entry is who actually called."""
    assert client_ip("9.9.9.9, 10.0.0.5, 10.0.0.6", "10.0.0.1", trust_proxy=True) == "9.9.9.9"


def test_a_missing_or_empty_header_falls_back_to_the_connection() -> None:
    assert client_ip(None, "10.0.0.1", trust_proxy=True) == "10.0.0.1"
    assert client_ip("  ,  ", "10.0.0.1", trust_proxy=True) == "10.0.0.1"


def test_no_address_at_all_is_one_shared_bucket() -> None:
    """Unknown callers are limited together, which is the safe direction."""
    assert client_ip(None, None, trust_proxy=False) == UNKNOWN_IP


# ---- The counters -----------------------------------------------------------


def test_uploads_are_counted_per_address() -> None:
    limiter = UsageLimiter(Limits(uploads_per_ip_per_hour=2))
    limiter.claim_upload("1.1.1.1")
    limiter.claim_upload("1.1.1.1")
    with pytest.raises(LimitExceeded):
        limiter.claim_upload("1.1.1.1")

    # A different visitor has their own allowance.
    limiter.claim_upload("2.2.2.2")


def test_uploads_and_fixes_have_separate_allowances() -> None:
    """Using up the uploads must not also block the fixes, and the other way round."""
    limiter = UsageLimiter(Limits(uploads_per_ip_per_hour=1, fixes_per_ip_per_hour=1))
    limiter.claim_upload("1.1.1.1")
    limiter.claim_fix("1.1.1.1")
    with pytest.raises(LimitExceeded):
        limiter.claim_upload("1.1.1.1")
    with pytest.raises(LimitExceeded):
        limiter.claim_fix("1.1.1.1")


def test_the_hour_slides_rather_than_resetting() -> None:
    clock = FakeClock()
    limiter = UsageLimiter(Limits(uploads_per_ip_per_hour=1), clock=clock)
    limiter.claim_upload("1.1.1.1")

    clock.advance(HOUR_SECONDS - 1)
    with pytest.raises(LimitExceeded):
        limiter.claim_upload("1.1.1.1")

    clock.advance(2)
    limiter.claim_upload("1.1.1.1")


def test_the_day_cap_applies_across_every_visitor() -> None:
    """The per-IP limits protect us from one person; this one protects us from a crowd."""
    limiter = UsageLimiter(Limits(model_calls_per_day=3))
    for n in range(3):
        limiter.claim_upload(f"10.0.0.{n}")
    with pytest.raises(LimitExceeded):
        limiter.claim_upload("10.0.0.99")
    with pytest.raises(LimitExceeded):
        limiter.claim_fix("10.0.0.99")


def test_a_request_refused_by_the_day_cap_costs_no_hourly_slot() -> None:
    """Both budgets are spent together or not at all."""
    limiter = UsageLimiter(Limits(uploads_per_ip_per_hour=2, model_calls_per_day=1))
    limiter.claim_upload("1.1.1.1")
    with pytest.raises(LimitExceeded):
        limiter.claim_upload("1.1.1.1")

    # Raise the day's budget without moving the clock: the visitor must still have the
    # second of their two uploads, because the refused one took nothing.
    limiter.limits = Limits(uploads_per_ip_per_hour=2, model_calls_per_day=10)
    limiter.claim_upload("1.1.1.1")
    with pytest.raises(LimitExceeded):
        limiter.claim_upload("1.1.1.1")


def test_extra_rounds_are_charged_to_the_day() -> None:
    """A fix is one claim plus however many further rounds the loop turned out to run."""
    limiter = UsageLimiter(Limits(model_calls_per_day=10))
    limiter.claim_fix("1.1.1.1")
    limiter.record_model_calls(2)
    assert limiter.model_calls_today() == 3

    # A single-round loop charges nothing extra, and a nonsense count is ignored.
    limiter.record_model_calls(0)
    limiter.record_model_calls(-1)
    assert limiter.model_calls_today() == 3


def test_the_day_cap_forgets_after_a_day() -> None:
    clock = FakeClock()
    limiter = UsageLimiter(Limits(model_calls_per_day=1), clock=clock)
    limiter.claim_upload("1.1.1.1")
    clock.advance(DAY_SECONDS + 1)
    assert limiter.model_calls_today() == 0
    limiter.claim_upload("9.9.9.9")


# ---- Cleanup ----------------------------------------------------------------


def test_old_runs_go_and_recent_ones_stay(tmp_path: Path) -> None:
    old = make_run(tmp_path, "old-run", age_hours=25)
    recent = make_run(tmp_path, "recent-run", age_hours=1)
    just_inside = make_run(tmp_path, "just-inside", age_hours=23.9)

    assert sweep_runs(tmp_path, max_age_hours=24) == ["old-run"]
    assert not old.exists()
    assert (recent / "page.png").is_file()
    assert just_inside.is_dir()


def test_the_sweep_leaves_loose_files_alone(tmp_path: Path) -> None:
    """Only run folders were put there by a run; anything else is not ours to delete."""
    stray = tmp_path / "notes.txt"
    stray.write_text("keep me", encoding="utf-8")
    when = time.time() - 100 * HOUR_SECONDS
    os.utime(stray, (when, when))

    assert sweep_runs(tmp_path, max_age_hours=24) == []
    assert stray.is_file()


def test_sweeping_a_root_that_is_not_there_is_fine(tmp_path: Path) -> None:
    assert sweep_runs(tmp_path / "nothing-here", max_age_hours=24) == []


def test_cleanup_runs_at_most_once_an_hour(tmp_path: Path) -> None:
    clock = FakeClock()
    cleaner = Cleaner(Limits(run_retention_hours=24, cleanup_interval_seconds=3600), clock=clock)

    # The first sweep is the startup one: it runs, and it starts the hour.
    make_run(tmp_path, "old-one", age_hours=30)
    assert cleaner.sweep(tmp_path, force=True) == ["old-one"]

    # Another old folder appears, but no hour has passed, so nothing looks for it.
    make_run(tmp_path, "old-two", age_hours=30)
    clock.advance(HOUR_SECONDS - 1)
    assert cleaner.sweep(tmp_path) == []
    assert (tmp_path / "old-two").is_dir()

    clock.advance(2)
    assert cleaner.sweep(tmp_path) == ["old-two"]
