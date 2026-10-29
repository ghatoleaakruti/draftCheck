"""Smoke test: prove we can reach the configured model and get text back.

Run with:  python -m scripts.smoke_test [--provider baseten|meta]

With no ``--provider`` the provider comes from ``LLM_PROVIDER`` in ``.env``.
Prints the reply, latency, provider, model name, and token usage. Never prints the API key.
"""

from __future__ import annotations

import argparse
import sys

from draftcheck.config import SUPPORTED_PROVIDERS, ConfigError, get_provider
from draftcheck.llm_client import LLMClient

SYSTEM_PROMPT = "You are a connection test. Follow the instruction exactly."
USER_PROMPT = "Reply with exactly: DraftCheck connected"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the smoke test."""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.smoke_test",
        description="Check that the configured model answers over the network.",
    )
    parser.add_argument(
        "--provider",
        choices=SUPPORTED_PROVIDERS,
        default=None,
        help="Which provider to call. Default: the LLM_PROVIDER value in .env.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        client = LLMClient(provider=get_provider(args.provider))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    result = client.chat(SYSTEM_PROMPT, USER_PROMPT)

    print(f"Reply:    {result.text}")
    print(f"Latency:  {result.latency_s:.2f}s")
    print(f"Provider: {client.provider.name}")
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
