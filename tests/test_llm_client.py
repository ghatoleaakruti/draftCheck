"""Tests for chat_json. A fake client stands in for the provider, so no network."""

import base64
from dataclasses import dataclass

import pytest

from draftcheck.config import Provider
from draftcheck.llm_client import JSON_MAX_TOKENS, LLMClient
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
