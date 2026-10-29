"""Thin wrapper around an OpenAI-compatible chat endpoint.

The client only moves text in and out. All pass/fail decisions live in deterministic
Python elsewhere; nothing here interprets the model's answer.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from draftcheck.config import Provider, get_provider
from draftcheck.llm_parsing import JSONParseError, extract_json

JSON_MAX_TOKENS = 8000


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


@dataclass(frozen=True)
class JSONChatResult:
    """A parsed JSON reply, kept alongside the raw text that produced it."""

    data: dict
    text: str
    latency_s: float
    usage: Usage
    model: str
    attempts: int


def _read_usage(raw_usage: object) -> Usage:
    """Convert the provider's usage object into our own dataclass, tolerating gaps."""
    return Usage(
        prompt_tokens=int(getattr(raw_usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(raw_usage, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(raw_usage, "total_tokens", 0) or 0),
    )


class LLMClient:
    """Sends chat requests to the configured provider at temperature 0."""

    def __init__(self, provider: Provider | None = None, client: Any | None = None) -> None:
        """Build a client.

        Args:
            provider: Connection settings. Read from the environment when omitted.
            client: An already-built OpenAI-compatible client. Tests pass a fake here
                so no network call is ever made.
        """
        self.provider = provider or get_provider()
        self._client = client or OpenAI(
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

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        """Send text plus page images and return the model's reply parsed as JSON.

        The images are PNG bytes, sent as OpenAI-style ``image_url`` data URIs. If the
        reply does not contain a usable JSON object the whole request is sent once more;
        a second failure is raised to the caller.

        Raises:
            JSONParseError: If both attempts fail to produce a JSON object.
        """
        content: list[dict] = [{"type": "text", "text": user}]
        for png_bytes in images:
            encoded = base64.b64encode(png_bytes).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                }
            )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]

        started = time.perf_counter()
        last_error: JSONParseError | None = None
        for attempt in (1, 2):
            response = self._client.chat.completions.create(
                model=self.provider.model,
                temperature=0,
                max_tokens=JSON_MAX_TOKENS,
                messages=messages,
            )
            text = (response.choices[0].message.content or "").strip()
            try:
                data = extract_json(text)
            except JSONParseError as exc:
                last_error = exc
                continue

            return JSONChatResult(
                data=data,
                text=text,
                latency_s=time.perf_counter() - started,
                usage=_read_usage(response.usage),
                model=getattr(response, "model", self.provider.model),
                attempts=attempt,
            )

        assert last_error is not None  # noqa: S101 - loop always sets this before here
        raise last_error
