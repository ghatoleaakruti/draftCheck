"""Tests for provider configuration loading. No network calls, no real .env."""

from __future__ import annotations

import dataclasses

import pytest

from draftcheck.config import ConfigError, Provider, get_provider

REQUIRED_VARS = ("BASETEN_BASE_URL", "BASETEN_API_KEY", "BASETEN_MODEL")

FAKE_ENV = {
    "LLM_PROVIDER": "baseten",
    "BASETEN_BASE_URL": "https://example.invalid/v1",
    "BASETEN_API_KEY": "fake-key-not-real",
    "BASETEN_MODEL": "fake-model-id",
}


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set fake provider variables and clear anything inherited from the real shell."""
    for key, value in FAKE_ENV.items():
        monkeypatch.setenv(key, value)


def load() -> Provider:
    """Call get_provider without ever touching a real .env file."""
    return get_provider(load_env=False)


def test_get_provider_reads_env_vars(fake_env: None) -> None:
    provider = load()
    assert provider.name == "baseten"
    assert provider.base_url == "https://example.invalid/v1"
    assert provider.api_key == "fake-key-not-real"
    assert provider.model == "fake-model-id"


def test_provider_defaults_to_baseten_when_llm_provider_unset(
    fake_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_PROVIDER")
    assert load().name == "baseten"


def test_values_are_stripped(fake_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASETEN_MODEL", "  spaced-model  ")
    assert load().model == "spaced-model"


@pytest.mark.parametrize("missing", REQUIRED_VARS)
def test_missing_variable_raises_naming_it(
    fake_env: None, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)
    with pytest.raises(ConfigError) as excinfo:
        load()
    assert missing in str(excinfo.value)


@pytest.mark.parametrize("blank", REQUIRED_VARS)
def test_blank_variable_raises_naming_it(
    fake_env: None, monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    monkeypatch.setenv(blank, "   ")
    with pytest.raises(ConfigError) as excinfo:
        load()
    assert blank in str(excinfo.value)


def test_unsupported_provider_raises(fake_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "some-other-vendor")
    with pytest.raises(ConfigError, match="some-other-vendor"):
        load()


def test_provider_is_frozen(fake_env: None) -> None:
    provider = load()
    with pytest.raises(dataclasses.FrozenInstanceError):
        provider.model = "changed"  # type: ignore[misc]


def test_repr_masks_api_key(fake_env: None) -> None:
    provider = load()
    assert "fake-key-not-real" not in repr(provider)
    assert "***" in repr(provider)
