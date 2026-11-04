"""Thin wrapper around an OpenAI-compatible chat endpoint.

The client only moves text in and out. All pass/fail decisions live in deterministic
Python elsewhere; nothing here interprets the model's answer.
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openai import APITimeoutError, AsyncOpenAI, OpenAI

from draftcheck.config import Provider, get_provider
from draftcheck.llm_parsing import JSONParseError, extract_json

JSON_MAX_TOKENS = 8000

#: What a person is told when a model call runs out of time. Said by the web page as-is.
MODEL_TIMEOUT_MESSAGE = "Muse Spark took too long to answer. Please try again."


class ModelTimeout(RuntimeError):
    """A model call ran past its time limit. Raised only where a limit was set."""

    def __init__(self, message: str = MODEL_TIMEOUT_MESSAGE) -> None:
        super().__init__(message)


class ModelCancelled(RuntimeError):
    """A model call was stopped by :meth:`LLMClient.abort` — nobody is waiting for it."""

    def __init__(self, message: str = "cancelled by client") -> None:
        super().__init__(message)


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
    #: Which provider answered (``"baseten"``, ``"meta"``). Set by the server's guard,
    #: which is the one place that knows whether a backup provider stepped in.
    provider: str | None = None
    #: True when the first provider failed and this reply came from the backup.
    fell_back: bool = False
    #: Why the first provider was given up on, e.g. ``"timeout"`` or ``"HTTP 503"``.
    fallback_reason: str | None = None


def _read_usage(raw_usage: object) -> Usage:
    """Convert the provider's usage object into our own dataclass, tolerating gaps."""
    return Usage(
        prompt_tokens=int(getattr(raw_usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(raw_usage, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(raw_usage, "total_tokens", 0) or 0),
    )


class LLMClient:
    """Sends chat requests to the configured provider at temperature 0."""

    def __init__(
        self,
        provider: Provider | None = None,
        client: Any | None = None,
        timeout_s: float | None = None,
        abortable: bool = False,
        async_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        """Build a client.

        Args:
            provider: Connection settings. Read from the environment when omitted.
            client: An already-built OpenAI-compatible client. Tests pass a fake here
                so no network call is ever made.
            timeout_s: How long the HTTP library waits before giving up. ``None`` keeps
                the library's own default, which is what the CLI uses.
            abortable: Send ``chat_json`` requests so that :meth:`abort` can stop them
                part-way. The web server turns this on; the CLI has nobody to cancel.
            async_client_factory: Builds the async OpenAI-compatible client an abortable
                request is sent with. Tests pass a fake; normally an ``AsyncOpenAI``.
        """
        self.provider = provider or get_provider()
        options: dict[str, Any] = {} if timeout_s is None else {"timeout": timeout_s}
        self._client = client or OpenAI(
            base_url=self.provider.base_url,
            api_key=self.provider.api_key,
            **options,
        )
        self._abortable = abortable or async_client_factory is not None
        self._async_client_factory = async_client_factory or (
            lambda: AsyncOpenAI(
                base_url=self.provider.base_url, api_key=self.provider.api_key, **options
            )
        )
        #: The requests :meth:`abort` would stop: each one's task and the loop it runs on.
        self._in_flight: dict[asyncio.Task, asyncio.AbstractEventLoop] = {}
        self._abort_lock = threading.Lock()
        self._aborted = False

    def abort(self) -> None:
        """Stop every request this client has in flight, and refuse any further ones.

        Safe to call from any thread. Each abortable request is an asyncio task on its own
        event loop; cancelling that task makes the HTTP library close the connection, so
        the provider sees the request go away rather than finishing it for nobody. The
        waiting ``chat_json`` call raises :class:`ModelCancelled`.
        """
        with self._abort_lock:
            self._aborted = True
            for task, loop in self._in_flight.items():
                loop.call_soon_threadsafe(task.cancel)

    def _create(self, **request: Any) -> Any:
        """One chat-completion request, abortable when this client was built to be."""
        if not self._abortable:
            return self._client.chat.completions.create(**request)
        return asyncio.run(self._create_abortable(request))

    async def _create_abortable(self, request: dict[str, Any]) -> Any:
        """Send ``request`` as a task :meth:`abort` can cancel from another thread.

        Runs on a fresh event loop in the calling thread, so the async client is built and
        closed inside it: an async HTTP client cannot be shared between event loops.
        """
        task = asyncio.current_task()
        assert task is not None  # noqa: S101 - always true inside asyncio.run
        with self._abort_lock:
            if self._aborted:
                raise ModelCancelled()
            self._in_flight[task] = asyncio.get_running_loop()
        try:
            async with self._async_client_factory() as client:
                return await client.chat.completions.create(**request)
        except asyncio.CancelledError:
            if self._aborted:
                raise ModelCancelled() from None
            raise
        finally:
            with self._abort_lock:
                self._in_flight.pop(task, None)

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
            ModelTimeout: If the HTTP library gave up waiting for the provider.
            ModelCancelled: If :meth:`abort` stopped the request.
        """
        content: list[dict] = [{"type": "text", "text": user}]
        for png_bytes in images:
            # One expression, so the base64 bytes and the bare string are dropped at once
            # and only the finished data URI stays alive for the request.
            url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": url}})

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]

        started = time.perf_counter()
        last_error: JSONParseError | None = None
        for attempt in (1, 2):
            try:
                response = self._create(
                    model=self.provider.model,
                    temperature=0,
                    max_tokens=JSON_MAX_TOKENS,
                    messages=messages,
                )
            except APITimeoutError as exc:
                raise ModelTimeout() from exc
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
