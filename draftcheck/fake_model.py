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
* **``DRAFTCHECK_FAKE_SCENARIO=reject_then_fix``** scripts three rounds instead, so the
  page's live rounds can be watched failing before they succeed: round 1 asks to delete
  the frame (refused by ``edits.apply_edits``), round 2 changes the frame's tolerance
  (applied, but the rules still report the finding), and round 3 removes datum C (the
  rules stop reporting it). Unset, or any other value, keeps the one-edit default.
* **The explanation** ("Explain this drawing") returns :data:`SCRIPTED_EXPLANATION`, a
  hand-written explanation of plate_001, including its one front view, an "unclear"
  projection (one view, no projection symbol), and no tables. Its note on the GDT-001
  finding is included only when that finding was in the request, as a real model would
  only explain findings it was given. It is validated against the drawing like any real answer.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path

from draftcheck.llm_client import JSONChatResult, ModelCancelled, Usage
from draftcheck.prompts import EXPLAIN_SYSTEM, EXTRACTION_SYSTEM

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

#: Picks a scripted correction run. Development only, like the fake itself.
FAKE_SCENARIO_ENV_VAR = "DRAFTCHECK_FAKE_SCENARIO"
REJECT_THEN_FIX = "reject_then_fix"

#: The three rounds of :data:`REJECT_THEN_FIX`, one edit each. Whether each is refused,
#: applied, or clears the finding is decided by the edit guards and the rules, not here.
REJECT_THEN_FIX_ROUNDS = (
    {
        "op": "delete_fcf",
        "fcf_id": "fcf_1",
        "rationale": "The frame cites a datum that is not defined, so remove the frame.",
        "finding_ids": ["GDT-001@fcf_1"],
    },
    {
        "op": "set_field",
        "target": "fcf",
        "id": "fcf_1",
        "field": "tolerance",
        "value": 0.2,
        "rationale": "Tighten the position tolerance so the frame is easier to inspect.",
        "finding_ids": ["GDT-001@fcf_1"],
    },
    SCRIPTED_EDIT,
)


#: The finding the scripted explanation knows about, and its note.
EXPLAINED_FINDING = "GDT-001@fcf_1"

#: A hand-written explanation of plate_001, in our own words, for working on the page
#: offline. Every id and label in it is one the fixture really has.
SCRIPTED_EXPLANATION = {
    "part": {
        "type": "plate",
        "name_guess": "Bracket mounting plate",
        "summary": (
            "This appears to be a flat aluminium plate, 100 by 60 mm, with two bolt holes "
            "near its middle. It is likely bolted to another part, so the two holes have "
            "to line up with holes or studs on that part."
        ),
        "features": [
            "Rectangular plate, 100 × 60 mm",
            "2 through holes, Ø8",
            "Holes on one line, 60 mm apart",
            "Material: AL 6061-T6",
        ],
        "confidence": 0.8,
    },
    "views": [
        {
            "name": "front",
            "label": None,
            "shows": "The face of the plate, with both holes and every dimension on the "
            "sheet.",
        }
    ],
    "projection": "unclear",
    "projection_note": "Only one view is drawn and there is no projection symbol, so there "
    "is nothing to tell first- from third-angle. A flat plate often needs just one view, "
    "with its thickness given in a note or the title block.",
    "tables": [],
    "balloons": "There are no item balloons, so this is likely a single part rather than "
    "an assembly.",
    "dimensions": [
        {
            "id": "dim_1",
            "kind": "location",
            "view": "front",
            "purpose": "Locates hole 2 from the left edge (datum B), so the two holes end "
            "up the bolt spacing apart.",
        },
        {
            "id": "dim_2",
            "kind": "location",
            "view": "front",
            "purpose": "Locates hole 1 from the left edge (datum B).",
        },
        {
            "id": "dim_3",
            "kind": "size",
            "view": "front",
            "purpose": "Sets the overall height of the plate, likely so it fits the space "
            "it is mounted in.",
        },
        {
            "id": "dim_4",
            "kind": "size",
            "view": "front",
            "purpose": "Sets the overall width of the plate.",
        },
        {
            "id": "dim_5",
            "kind": "location",
            "view": "front",
            "purpose": "Locates the holes up from the bottom edge (datum A).",
        },
        {
            "id": "dim_6",
            "kind": "size",
            "view": "front",
            "purpose": "Sizes both holes at once; Ø8 is likely a clearance hole for an M6 or "
            "similar bolt.",
        },
    ],
    "fcfs": [
        {
            "id": "fcf_1",
            "meaning": "Each hole centre must sit within a Ø0.25 circle around its exact "
            "position, measured from datums A and B, so the bolts can pass through the "
            "mating part; the MMC modifier gives extra room when the holes come out larger.",
        }
    ],
    "datums": [
        {
            "label": "A",
            "role": "primary",
            "why": "The bottom edge is listed first in the frame, so it is likely the edge "
            "the plate rests on when it is measured.",
        },
        {
            "label": "B",
            "role": "secondary",
            "why": "The left edge likely stops the plate sliding sideways, and the hole "
            "positions are measured from it.",
        },
    ],
    "findings": [
        {
            "key": EXPLAINED_FINDING,
            "why_it_matters": "An inspector cannot set the part up to measure the hole "
            "positions if one of the reference surfaces is never shown on the drawing.",
            "how_to_think_about_fixing": "Ask which surfaces really locate the part when it "
            "is assembled: either show where datum C is, or leave it out of the frame.",
        }
    ],
}


def scripted_explanation(user: str) -> dict:
    """The scripted explanation, with the finding note only if that finding was sent."""
    data = copy.deepcopy(SCRIPTED_EXPLANATION)
    if EXPLAINED_FINDING not in user:
        data["findings"] = []
    return data


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
        scenario: str | None = None,
    ) -> None:
        self._delay_s = delay_s
        #: Which scripted correction run to give; ``None`` is the one-edit default.
        self._scenario = scenario
        self._correction_calls = 0
        self._extraction = extraction
        #: One ``(dimension id, field, value)`` to read differently from the fixture.
        self._change = change
        self._model = model
        #: The model id this client answers as, for the page to say before a round starts.
        self.model = model
        self._cancelled = threading.Event()

    def abort(self) -> None:
        """Stop a pause in progress, as the real client closes its request on Cancel."""
        self._cancelled.set()

    def _correction_edit(self) -> dict:
        """The edit for the next correction round, by scenario."""
        self._correction_calls += 1
        if self._scenario == REJECT_THEN_FIX:
            index = min(self._correction_calls, len(REJECT_THEN_FIX_ROUNDS)) - 1
            return copy.deepcopy(REJECT_THEN_FIX_ROUNDS[index])
        return dict(SCRIPTED_EDIT)

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
        elif system == EXPLAIN_SYSTEM:
            data = scripted_explanation(user)
        else:
            data = {"edits": [self._correction_edit()]}

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

    The delay is read here rather than bound as a default, so a test can shorten it. So is
    the scenario (:data:`FAKE_SCENARIO_ENV_VAR`), so it can be switched without a restart.
    """
    return FakeModelClient(delay_s=FAKE_DELAY_S, scenario=fake_scenario())


def fake_scenario() -> str | None:
    """The scripted correction run asked for, or ``None`` for the default one."""
    value = os.environ.get(FAKE_SCENARIO_ENV_VAR, "").strip().lower()
    return value if value == REJECT_THEN_FIX else None


def fake_cross_check_factory(provider: str | None = None) -> FakeModelClient:
    """The cross-check's fake: the same reading with :data:`CROSS_CHECK_CHANGE` applied."""
    return FakeModelClient(
        delay_s=FAKE_DELAY_S, change=CROSS_CHECK_CHANGE, model=FAKE_CROSS_CHECK_MODEL_NAME
    )
