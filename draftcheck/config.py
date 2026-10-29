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

#: Environment variable names per provider: (base URL, model id, API key).
PROVIDER_ENV_VARS: dict[str, tuple[str, str, str]] = {
    "baseten": ("BASETEN_BASE_URL", "BASETEN_MODEL", "BASETEN_API_KEY"),
    "meta": ("META_BASE_URL", "META_MODEL", "META_API_KEY"),
}

SUPPORTED_PROVIDERS = tuple(PROVIDER_ENV_VARS)


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


def get_provider(name: str | None = None, *, load_env: bool = True) -> Provider:
    """Build a Provider from environment variables.

    Args:
        name: Which provider to load, ``"baseten"`` or ``"meta"``. When omitted, falls
            back to ``LLM_PROVIDER`` and then to :data:`DEFAULT_PROVIDER`.
        load_env: When True, load a ``.env`` file found by searching parent folders.
            Tests pass False so they never read the real file.

    Raises:
        ConfigError: If the provider is unsupported or a required variable is missing.
    """
    if load_env:
        load_dotenv(find_dotenv(usecwd=True))

    chosen = (name or os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    env_vars = PROVIDER_ENV_VARS.get(chosen)
    if env_vars is None:
        supported = ", ".join(repr(key) for key in SUPPORTED_PROVIDERS)
        raise ConfigError(
            f"Unsupported LLM provider: {chosen!r}. Supported providers are: {supported}."
        )

    base_url_var, model_var, api_key_var = env_vars
    return Provider(
        name=chosen,
        base_url=_require(base_url_var),
        model=_require(model_var),
        api_key=_require(api_key_var),
    )
