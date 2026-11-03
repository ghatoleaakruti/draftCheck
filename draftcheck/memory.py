"""How much memory this process is using, for the log lines on the upload path.

Render's free plan has little RAM and kills an instance that goes over without a Python
error, so the last log line before a restart is the only clue to where it happened. Each
stage of an upload therefore logs this number. No library is needed: Linux (where Render
runs) reports the current resident size in ``/proc/self/status``; elsewhere the standard
library only knows the peak, which is still a useful upper bound on a laptop.
"""

from __future__ import annotations

import resource
import sys
from pathlib import Path

PROC_STATUS = Path("/proc/self/status")
KB_PER_MB = 1024


def parse_vmrss_mb(status_text: str) -> float | None:
    """The ``VmRSS`` line of a ``/proc/<pid>/status`` file, in MB, or None if absent."""
    for line in status_text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            # "VmRSS:    123456 kB" — the kernel always reports this one in kB.
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) / KB_PER_MB
    return None


def peak_rss_mb() -> float:
    """The most memory this process has ever held, in MB. macOS counts bytes, Linux kB."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = KB_PER_MB * KB_PER_MB if sys.platform == "darwin" else KB_PER_MB
    return peak / divisor


def memory_mb() -> float:
    """Current resident memory in MB where the OS says so (Linux), else the peak so far."""
    try:
        current = parse_vmrss_mb(PROC_STATUS.read_text())
    except OSError:
        current = None
    return current if current is not None else peak_rss_mb()
