"""Helpers for turning messy model output into usable Python data."""

import json
import re

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\s*|```")


class JSONParseError(ValueError):
    """Raised when model output does not contain a usable JSON object.

    The original model text is kept in ``raw_text`` so failures can be inspected.
    """

    def __init__(self, message: str, raw_text: str) -> None:
        super().__init__(message)
        self.raw_text = raw_text


def extract_json(text: str) -> dict:
    """Pull a single JSON object out of model output.

    Removes ``<think>...</think>`` blocks and Markdown code fences, then takes the
    text from the first ``{`` to the last ``}`` and parses it.

    Raises:
        JSONParseError: if no JSON object is present, the JSON is invalid, or the
            top-level value is not an object.
    """
    cleaned = _FENCE.sub("", _THINK_BLOCK.sub("", text)).strip()

    # If the whole payload is already valid JSON, trust it — this is how we catch a
    # top-level list rather than silently digging an object out of it.
    try:
        whole = json.loads(cleaned)
    except ValueError:
        pass
    else:
        if not isinstance(whole, dict):
            raise JSONParseError(
                f"Top-level JSON value is {type(whole).__name__}, not an object.", text
            )
        return whole

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise JSONParseError("No JSON object found in model output.", text)

    candidate = cleaned[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except ValueError as exc:
        raise JSONParseError(f"Invalid JSON in model output: {exc}", text) from exc

    if not isinstance(parsed, dict):
        raise JSONParseError(
            f"Top-level JSON value is {type(parsed).__name__}, not an object.", text
        )
    return parsed
