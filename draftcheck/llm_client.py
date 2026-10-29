"""Thin wrapper around an OpenAI-compatible chat endpoint.

The client only moves text in and out. All pass/fail decisions live in deterministic
Python elsewhere; nothing here interprets the model's answer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from openai import OpenAI

from draftcheck.config import Provider, get_provider


@dataclass(frozen=True)
class Usage:
    """Token counts reported by the provider for one call."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ChatResult:
    """One model reply plus the timing and token accounting around it."""

    text: str
    latency_s: float
    usage: Usage
    model: str


def _read_usage(raw_usage: object) -> Usage:
    """Convert the provider's usage object into our own dataclass, tolerating gaps."""
    return Usage(
        prompt_tokens=int(getattr(raw_usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(raw_usage, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(raw_usage, "total_tokens", 0) or 0),
    )


class LLMClient:
    """Sends chat requests to the configured provider at temperature 0."""

    def __init__(self, provider: Provider | None = None) -> None:
        self.provider = provider or get_provider()
        self._client = OpenAI(
            base_url=self.provider.base_url,
            api_key=self.provider.api_key,
        )

    def chat(self, system: str, user: str) -> ChatResult:
        """Send one system + user message pair and return the reply with metrics."""
        started = time.perf_counter()
        response = self._client.chat.completions.create(
            model=self.provider.model,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        latency_s = time.perf_counter() - started

        text = response.choices[0].message.content or ""
        return ChatResult(
            text=text.strip(),
            latency_s=latency_s,
            usage=_read_usage(response.usage),
            model=getattr(response, "model", self.provider.model),
        )
