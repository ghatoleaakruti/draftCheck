"""Configuration loading for DraftCheck.

Reads LLM provider settings from environment variables, which are populated from a
``.env`` file found by searching upward from the current working directory. Secrets
are never printed: the provider's ``repr`` masks the API key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import find_dotenv, load_dotenv

DEFAULT_PROVIDER = "baseten"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or blank."""


@dataclass(frozen=True)
class Provider:
    """Connection settings for an OpenAI-compatible LLM endpoint."""

    name: str
    base_url: str
    model: str
    api_key: str = field(repr=False)

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"Provider(name={self.name!r}, base_url={self.base_url!r}, "
            f"model={self.model!r}, api_key='***')"
        )


def _require(var_name: str) -> str:
    """Return the environment variable's value, or raise naming the missing variable."""
    value = os.environ.get(var_name, "")
    if not value.strip():
        raise ConfigError(
            f"Missing required environment variable: {var_name}. "
            f"Add it to your .env file at the project root."
        )
    return value.strip()


def get_provider(*, load_env: bool = True) -> Provider:
    """Build a Provider from environment variables.

    Args:
        load_env: When True, load a ``.env`` file found by searching parent folders.
            Tests pass False so they never read the real file.

    Raises:
        ConfigError: If the provider is unsupported or a required variable is missing.
    """
    if load_env:
        load_dotenv(find_dotenv(usecwd=True))

    name = (os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if name != "baseten":
        raise ConfigError(
            f"Unsupported LLM_PROVIDER: {name!r}. Only 'baseten' is supported right now."
        )

    return Provider(
        name=name,
        base_url=_require("BASETEN_BASE_URL"),
        model=_require("BASETEN_MODEL"),
        api_key=_require("BASETEN_API_KEY"),
    )
