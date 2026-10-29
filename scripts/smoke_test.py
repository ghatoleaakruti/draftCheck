"""Smoke test: prove we can reach the configured model and get text back.

Run with:  python -m scripts.smoke_test
Prints the reply, latency, model name, and token usage. Never prints the API key.
"""

from __future__ import annotations

import sys

from draftcheck.config import ConfigError
from draftcheck.llm_client import LLMClient

SYSTEM_PROMPT = "You are a connection test. Follow the instruction exactly."
USER_PROMPT = "Reply with exactly: DraftCheck connected"


def main() -> int:
    try:
        client = LLMClient()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    result = client.chat(SYSTEM_PROMPT, USER_PROMPT)

    print(f"Reply:    {result.text}")
    print(f"Latency:  {result.latency_s:.2f}s")
    print(f"Model:    {result.model}")
    print(
        "Usage:    "
        f"prompt={result.usage.prompt_tokens} "
        f"completion={result.usage.completion_tokens} "
        f"total={result.usage.total_tokens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
