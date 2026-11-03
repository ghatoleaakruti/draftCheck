"""Tests for the memory reading used in the upload log lines."""

from __future__ import annotations

from draftcheck.memory import memory_mb, parse_vmrss_mb, peak_rss_mb

STATUS = """Name:\tuvicorn
VmPeak:\t  600000 kB
VmRSS:\t  204800 kB
Threads:\t4
"""


def test_vmrss_line_is_read_in_megabytes() -> None:
    assert parse_vmrss_mb(STATUS) == 200.0


def test_missing_or_garbled_vmrss_is_none() -> None:
    assert parse_vmrss_mb("Name:\tpython\n") is None
    assert parse_vmrss_mb("VmRSS:\tlots kB\n") is None


def test_memory_is_a_plausible_number_on_this_machine() -> None:
    """Whichever source is used, a running Python is somewhere between 1 MB and 100 GB."""
    assert 1 < memory_mb() < 100_000
    assert 1 < peak_rss_mb() < 100_000
