"""Tests for chat_json. A fake client stands in for the provider, so no network."""

import asyncio
import base64
import socket
import threading
from dataclasses import dataclass

import httpx
import pytest
from openai import APITimeoutError

from draftcheck.config import Provider
from draftcheck.llm_client import (
    JSON_MAX_TOKENS,
    MODEL_TIMEOUT_MESSAGE,
    LLMClient,
    ModelCancelled,
    ModelTimeout,
)
from draftcheck.llm_parsing import JSONParseError

PNG_A = b"\x89PNG\r\n\x1a\nfirst-page-bytes"
PNG_B = b"\x89PNG\r\n\x1a\nsecond-page-bytes"


@dataclass
class FakeUsage:
    prompt_tokens: int = 11
    completion_tokens: int = 22
    total_tokens: int = 33


class FakeCompletions:
    """Returns canned replies in order and records every request it was given."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._replies.pop(0)

        class Message:
            content = text

        class Choice:
            message = Message()

        class Response:
            choices = [Choice()]
            usage = FakeUsage()
            model = "fake-model"

        return Response()


class FakeClient:
    def __init__(self, replies: list[str]) -> None:
        self.completions = FakeCompletions(replies)
        self.chat = self

    @property
    def calls(self) -> list[dict]:
        return self.completions.calls


def make_client(replies: list[str]) -> tuple[LLMClient, FakeClient]:
    provider = Provider(
        name="baseten",
        base_url="https://example.invalid",
        model="test-model",
        api_key="not-a-real-key",
    )
    fake = FakeClient(replies)
    return LLMClient(provider=provider, client=fake), fake


def test_sends_text_then_one_image_part_per_page():
    client, fake = make_client(['{"units": "millimeters"}'])

    result = client.chat_json("SYSTEM", "USER", [PNG_A, PNG_B])

    assert result.data == {"units": "millimeters"}
    assert result.attempts == 1
    assert result.usage.total_tokens == 33
    assert result.latency_s >= 0

    request = fake.calls[0]
    assert request["temperature"] == 0
    assert request["max_tokens"] == JSON_MAX_TOKENS

    system_message, user_message = request["messages"]
    assert system_message == {"role": "system", "content": "SYSTEM"}

    content = user_message["content"]
    assert content[0] == {"type": "text", "text": "USER"}
    assert [part["type"] for part in content[1:]] == ["image_url", "image_url"]
    assert content[1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(PNG_A).decode("ascii")
    )
    assert content[2]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(PNG_B).decode("ascii")
    )


def test_no_images_sends_only_the_text_part():
    client, fake = make_client(['{"notes": []}'])

    client.chat_json("SYSTEM", "USER", [])

    content = fake.calls[0]["messages"][1]["content"]
    assert content == [{"type": "text", "text": "USER"}]


def test_retries_once_when_the_first_reply_is_not_json():
    client, fake = make_client(["Sorry, I cannot do that.", '{"datums": []}'])

    result = client.chat_json("SYSTEM", "USER", [PNG_A])

    assert result.data == {"datums": []}
    assert result.attempts == 2
    assert len(fake.calls) == 2


def test_raises_after_a_second_failure():
    client, fake = make_client(["no json here", "still no json"])

    with pytest.raises(JSONParseError) as excinfo:
        client.chat_json("SYSTEM", "USER", [PNG_A])

    assert excinfo.value.raw_text == "still no json"
    assert len(fake.calls) == 2


def test_a_provider_timeout_becomes_model_timeout():
    """The HTTP library's own timeout reaches the caller as our exception, with our words."""
    client, fake = make_client([])

    def time_out(**kwargs):
        raise APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))

    fake.completions.create = time_out

    with pytest.raises(ModelTimeout) as excinfo:
        client.chat_json("SYSTEM", "USER", [PNG_A])

    assert str(excinfo.value) == MODEL_TIMEOUT_MESSAGE


# ---- abort() ------------------------------------------------------------------------


class HangingAsyncClient:
    """An async OpenAI-shaped client whose request never answers until it is cancelled."""

    def __init__(self) -> None:
        self.chat = self
        self.completions = self
        self.started = threading.Event()
        self.cancelled = False
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.closed = True

    async def create(self, **kwargs):
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def call_in_thread(client: LLMClient) -> tuple[threading.Thread, dict]:
    """Start ``chat_json`` on another thread, as the server does, and keep its outcome."""
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["result"] = client.chat_json("SYSTEM", "USER", [PNG_A])
        except Exception as exc:  # noqa: BLE001 - inspected by the test
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, outcome


def test_abort_stops_a_request_in_flight():
    base, fake = make_client([])
    fake_async = HangingAsyncClient()
    client = LLMClient(provider=base.provider, client=fake, async_client_factory=lambda: fake_async)

    worker, outcome = call_in_thread(client)
    assert fake_async.started.wait(timeout=5)
    client.abort()
    worker.join(timeout=5)

    assert not worker.is_alive(), "chat_json was still waiting after abort()"
    assert isinstance(outcome.get("error"), ModelCancelled)
    assert fake_async.cancelled
    assert fake_async.closed


def test_an_aborted_client_sends_nothing_more():
    base, fake = make_client([])
    fake_async = HangingAsyncClient()
    client = LLMClient(provider=base.provider, client=fake, async_client_factory=lambda: fake_async)
    client.abort()

    with pytest.raises(ModelCancelled):
        client.chat_json("SYSTEM", "USER", [PNG_A])
    assert not fake_async.started.is_set()


def test_abort_closes_the_http_connection():
    """Against a local socket that never replies: abort() makes the client hang up."""
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    seen: dict = {}

    def serve() -> None:
        connection, _ = listener.accept()
        connection.settimeout(10)
        seen["request"] = connection.recv(65536)
        # Never answer. The next recv returns b"" only once the client closes its end.
        try:
            while connection.recv(65536):
                pass
            seen["closed"] = True
        except TimeoutError:
            seen["closed"] = False
        connection.close()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    provider = Provider(
        name="baseten",
        base_url=f"http://127.0.0.1:{port}/v1",
        model="test-model",
        api_key="not-a-real-key",
    )
    client = LLMClient(provider=provider, abortable=True)

    worker, outcome = call_in_thread(client)
    for _ in range(100):
        if "request" in seen:
            break
        threading.Event().wait(0.05)
    client.abort()
    worker.join(timeout=5)
    server.join(timeout=10)
    listener.close()

    assert seen.get("request", b"").startswith(b"POST /v1/chat/completions")
    assert isinstance(outcome.get("error"), ModelCancelled)
    assert seen.get("closed") is True, "the connection stayed open after abort()"



class AnsweringAsyncClient(HangingAsyncClient):
    """An async OpenAI-shaped client that answers straight away."""

    async def create(self, **kwargs):
        self.started.set()
        return FakeCompletions(['{"ok": true}']).create(**kwargs)


def test_aborting_one_client_leaves_another_working():
    """abort() belongs to one client: a second client built the same way still answers."""
    base, fake = make_client([])
    hanging = HangingAsyncClient()
    first = LLMClient(provider=base.provider, client=fake, async_client_factory=lambda: hanging)
    worker, outcome = call_in_thread(first)
    assert hanging.started.wait(timeout=5)
    first.abort()
    worker.join(timeout=5)
    assert isinstance(outcome.get("error"), ModelCancelled)

    second = LLMClient(
        provider=base.provider, client=fake, async_client_factory=AnsweringAsyncClient
    )
    assert second.chat_json("SYSTEM", "USER", [PNG_A]).data == {"ok": True}
