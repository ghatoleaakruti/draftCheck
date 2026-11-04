"""A stand-in model for working on the page when the real providers are slow or down.

**Development only.** The API uses this client instead of Baseten or Meta only when
``DRAFTCHECK_FAKE_MODEL=1`` is set *and* the request comes from this machine — see
:func:`draftcheck.api.fake_model_is_allowed`. A deployed DraftCheck never answers with it.

It replaces the model call and nothing else. The rules, the correction loop, the redline,
and the usage limits all run for real on what it returns:

* **Extraction** returns ``tests/fixtures/plate_001.model.json``, a real reading of the
  sample drawing saved from a live run, after about two seconds.
* **The cross-check** (a second Meta model reading the same page) returns the same
  fixture with one value changed — ``dim_5`` read as 35 instead of 30 — so the page's
  disagreement markers have something to show.
* **Each correction round** returns one scripted edit — remove datum C from ``fcf_1`` —
  after about two seconds. The rule engine decides, as always, whether that edit is
  allowed and whether it cleared anything.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from draftcheck.llm_client import JSONChatResult, ModelCancelled, Usage
from draftcheck.prompts import EXTRACTION_SYSTEM

#: The saved extraction the fake reads back. Lives beside the tests, like the demo's.
REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_EXTRACTION = REPO_ROOT / "tests" / "fixtures" / "plate_001.model.json"

#: How long each fake call takes, so the page's waiting states can be seen.
FAKE_DELAY_S = 2.0

#: What the page and ``meta.json`` record as the model that answered.
FAKE_MODEL_NAME = "fake-model (dev)"

#: What the cross-check fake reads differently: (dimension id, field, value). The fixture
#: has ``dim_5`` = 30; the second "model" reads 35.
CROSS_CHECK_CHANGE = ("dim_5", "value", 35)

#: What ``meta.json`` records as the cross-check's model in fake mode.
FAKE_CROSS_CHECK_MODEL_NAME = "fake-model (dev, cross-check)"

#: The one edit every correction round proposes. The fixture's frame cites datum C, which
#: the drawing never defines, so this is the edit a real model made for the same finding.
SCRIPTED_EDIT = {
    "op": "remove_datum_ref",
    "fcf_id": "fcf_1",
    "label": "C",
    "rationale": "Datum C is not defined on the drawing; A and B already locate the frame.",
    "finding_ids": ["GDT-001@fcf_1"],
}


class FakeModelClient:
    """Answers like the model would, from fixed data, after a short pause."""

    #: Lets the API tell the page it is talking to this, not a real model.
    fake = True

    def __init__(
        self,
        delay_s: float = FAKE_DELAY_S,
        extraction: Path = FAKE_EXTRACTION,
        change: tuple[str, str, object] | None = None,
        model: str = FAKE_MODEL_NAME,
    ) -> None:
        self._delay_s = delay_s
        self._extraction = extraction
        #: One ``(dimension id, field, value)`` to read differently from the fixture.
        self._change = change
        self._model = model
        self._cancelled = threading.Event()

    def abort(self) -> None:
        """Stop a pause in progress, as the real client closes its request on Cancel."""
        self._cancelled.set()

    def chat_json(self, system: str, user: str, images: list[bytes]) -> JSONChatResult:
        started = time.monotonic()
        # Waiting on the event rather than sleeping, so Cancel ends the pause at once.
        if self._cancelled.wait(self._delay_s):
            raise ModelCancelled()

        if system == EXTRACTION_SYSTEM:
            data = json.loads(self._extraction.read_text(encoding="utf-8"))
            if self._change is not None:
                item_id, field, value = self._change
                for dimension in data.get("dimensions", []):
                    if dimension.get("id") == item_id:
                        dimension[field] = value
        else:
            data = {"edits": [dict(SCRIPTED_EDIT)]}

        return JSONChatResult(
            data=data,
            text=json.dumps(data),
            latency_s=time.monotonic() - started,
            usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            model=self._model,
            attempts=1,
        )


def fake_client_factory(provider: str | None = None) -> FakeModelClient:
    """A fresh fake for each request, whatever provider was asked for.

    The delay is read here rather than bound as a default, so a test can shorten it.
    """
    return FakeModelClient(delay_s=FAKE_DELAY_S)


def fake_cross_check_factory(provider: str | None = None) -> FakeModelClient:
    """The cross-check's fake: the same reading with :data:`CROSS_CHECK_CHANGE` applied."""
    return FakeModelClient(
        delay_s=FAKE_DELAY_S, change=CROSS_CHECK_CHANGE, model=FAKE_CROSS_CHECK_MODEL_NAME
    )
