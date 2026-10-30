"""Prompt text for the two passes that involve a model: extraction and correction.

The extraction prompts ask only for a transcription of what is printed. The correction
prompts ask for proposed edits. Neither asks the model whether a rule passes — the
findings it is shown were computed in Python, and whether a proposed edit clears one is
computed in Python afterwards. The model's job is to read the drawing and suggest a fix;
it never gets a vote on the verdict.
"""

from __future__ import annotations

import json
from typing import Any

from draftcheck.edits import ALLOWED_OPS, DIMENSION_FIELDS
from draftcheck.schema import Finding, finding_key

EXTRACTION_SYSTEM = """\
You are a transcription engine for mechanical engineering drawings. Your only job is \
to report exactly what is printed on the sheet.

Rules:
- Transcribe. Never invent, infer, complete, correct, or tidy up a callout. If the \
drawing is wrong, your output must be wrong in the same way.
- Do not add anything that is not printed. Example: if a feature control frame \
references datum C but datum C is never defined anywhere on the drawing, report the \
reference exactly as printed inside that frame, and do NOT add C to the datum list. \
The datum list contains only datums that actually appear as datum feature symbols on \
the sheet.
- Do not merge, split, reorder, or deduplicate callouts. One printed annotation is \
one entry.
- Copy symbols as printed, including Ø, □, ±, ⌀, °, and the GD&T characteristic \
symbols.
- If a value or symbol is unreadable, set that field to null and give the entry a low \
confidence. Never guess a plausible number.
- Confidence is a number from 0 to 1: how sure you are that you read the characters \
correctly.

Output exactly one JSON object. No prose, no explanation, no markdown code fences.\
"""

EXTRACTION_USER = """\
Transcribe every annotation on this drawing into one JSON object with exactly these keys:

{
  "units": string or null,
  "general_tolerance": {
    "linear_plus_minus": number or null,
    "text_raw": string or null
  },
  "title_block": {
    "title": string or null,
    "drawing_number": string or null,
    "revision": string or null,
    "material": string or null,
    "scale": string or null
  },
  "datums": [
    {"label": string, "bbox": [x0, y0, x1, y1], "confidence": number}
  ],
  "dimensions": [
    {
      "id": string,
      "text_raw": string,
      "value": number or null,
      "unit": string or null,
      "feature_type": string or null,
      "quantity": integer,
      "is_basic": boolean,
      "is_reference": boolean,
      "tol_plus": number or null,
      "tol_minus": number or null,
      "from_ref": string or null,
      "to_ref": string or null,
      "bbox": [x0, y0, x1, y1],
      "confidence": number
    }
  ],
  "fcfs": [
    {
      "id": string,
      "text_raw": string,
      "characteristic": string or null,
      "tolerance": number or null,
      "diameter_zone": boolean,
      "material_modifier": string or null,
      "datum_refs": [{"label": string, "modifier": string or null}],
      "bbox": [x0, y0, x1, y1],
      "confidence": number
    }
  ],
  "notes": [string]
}

Field rules:
- id: a short unique id you assign, e.g. "dim_1", "fcf_1".
- text_raw: the full annotation exactly as printed, symbols included.
- value: the nominal number only, with no symbols or tolerance.
- feature_type: what the dimension measures as printed, e.g. "linear", "diameter", \
"radius", "angle", "thread", "chamfer".
- quantity: the count from an "NX" prefix (e.g. "2X Ø8" is quantity 2). If there is no \
such prefix, quantity is 1.
- is_basic: true only when the value is printed inside a rectangle (a basic dimension).
- is_reference: true only when the value is printed inside parentheses.
- tol_plus and tol_minus: positive magnitudes, never signed. "+0.2 / -0.1" gives \
tol_plus 0.2 and tol_minus 0.1. "±0.05" gives 0.05 for both. If the dimension carries \
no tolerance of its own, both are null.
- from_ref and to_ref: the two things the dimension measures between — where its \
extension lines land. Use exactly one of these values for each:
    "left_edge", "right_edge", "top_edge", "bottom_edge" — an outer edge of the part
    "hole_N_center" — the centre of a hole, numbering the holes left to right, then \
top to bottom, so the leftmost hole in the top row is "hole_1_center"
    "datum_X" — a datum feature symbol, where X is its letter, e.g. "datum_A"
    null — when you cannot tell what the dimension measures between
  When a dimension starts or ends at a physical edge of the part, use the edge name \
even if that same edge also carries a datum feature symbol. Report what you see the \
extension lines touching; do not reason about what the dimension ought to measure.
- characteristic: the GD&T characteristic name as you read the symbol, e.g. "position", \
"flatness", "perpendicularity", "profile of a surface".
- diameter_zone: true when the tolerance compartment shows a Ø before the value.
- material_modifier: "MMC", "LMC", "RFS" as printed, else null.
- datum_refs: in the printed order, left to right. modifier is "MMC", "LMC", or null.
- notes: every general note line on the sheet, one string per line, as printed.
- bbox: [x0, y0, x1, y1] normalized to 0..1 of the page, origin at the top-left \
corner, tightly around the printed text or symbol.

If a section has nothing on the drawing, use an empty list or null. Output only the \
JSON object.\
"""


CORRECTION_SYSTEM = """\
You are a senior mechanical drawing checker. You are given a drawing, the structured data \
extracted from it, and a list of findings that a deterministic rule engine has already \
produced. Your job is to propose the smallest set of edits that makes the drawing correct.

How to think:
- Work out what the designer meant, then make the drawing say it. A finding is a symptom; \
fix the cause.
- Prefer the smallest change that preserves design intent. Changing one field beats adding \
a feature; adding one feature beats restructuring the drawing.
- Never remove a dimension, a feature control frame, or a datum feature to make a finding \
go away. Deleting the callout that carries the error is not a correction, and the system \
will refuse it. If a frame references a datum that does not exist, either define that datum \
or stop the frame from referencing it — do not delete the frame.
- If a finding cannot be fixed with the operations below, propose nothing for it and say \
so in the rationale of another edit, or return an empty edit list. Do not invent an \
operation, and do not force an unrelated edit to look productive.
- Only change what you can justify from the drawing. If the drawing does not tell you \
where a missing datum belongs, say where you believe it belongs and why, in the rationale.

Every edit must carry:
- "rationale": one sentence of engineering reasoning. Not what you changed — why the \
drawing is better for it.
- "finding_ids": the ids of the findings this edit addresses, exactly as given to you.

You are not the judge of whether your fix worked. After you answer, the rules are re-run \
against the edited drawing and the result is compared with the result before. Claiming a \
finding is resolved does not resolve it.

Output exactly one JSON object with a single key "edits", whose value is a list. No prose, \
no explanation outside the JSON, no markdown code fences.\
"""


_OPERATIONS = """\
set_field — change one field of one item, or one of the drawing's two top-level values.
  {"op": "set_field", "target": "dimension" | "fcf" | "datum", "id": "<item id>",
   "field": "<field name>", "value": <new value>,
   "rationale": "...", "finding_ids": ["..."]}
  {"op": "set_field", "target": "drawing", "field": "units" | "general_tolerance",
   "value": <new value>, "rationale": "...", "finding_ids": ["..."]}
  Editable fields — dimension: text_raw, value, unit, feature_type, quantity, is_basic,
  is_reference, tol_plus, tol_minus, from_ref, to_ref. fcf: text_raw, characteristic,
  tolerance, diameter_zone, material_modifier. datum: label, location. drawing: units,
  general_tolerance (a number, or an object with linear_plus_minus and text_raw).
  An item's id and bbox can never be changed, and datum_refs can only be changed with
  remove_datum_ref.

add_datum — add a datum feature symbol the drawing is missing.
  {"op": "add_datum", "label": "C", "location": "<where the symbol goes, in words>",
   "bbox": [x0, y0, x1, y1] or omitted,
   "rationale": "...", "finding_ids": ["..."]}
  The label is one or two letters. The location is for the person reviewing the redline,
  so write it as you would tell a colleague: "the bottom edge of the plate", not "near B".

remove_datum_ref — take one datum reference out of one feature control frame. The frame
  and the datum both survive; only the reference is removed.
  {"op": "remove_datum_ref", "fcf_id": "<frame id>", "label": "C",
   "rationale": "...", "finding_ids": ["..."]}

add_dimension — add a dimension to the drawing.
  {"op": "add_dimension",
   "dimension": {"value": <number>, "text_raw": "...", ...},
   "rationale": "...", "finding_ids": ["..."]}
  The dimension object takes the same fields extraction produces; at minimum a numeric
  value. Set is_basic true for a dimension that establishes a true position.
  Fields: """ + ", ".join(DIMENSION_FIELDS) + "\n"


def _findings_block(findings: list[Finding]) -> str:
    """List the findings with the ids the model must cite, worst first as the rules sorted them."""
    if not findings:
        return "No findings. Return an empty edit list."
    lines = []
    for finding in findings:
        items = ", ".join(finding.item_ids) or "the drawing as a whole"
        lines.append(
            f"- {finding_key(finding)}  [{finding.severity}]  on {items}\n"
            f"  {finding.message}\n"
            f"  reference: {finding.source}"
        )
    return "\n".join(lines)


def _history_block(history: list[dict[str, Any]]) -> str:
    """Replay the earlier rounds, so the model does not retry what has already failed."""
    if not history:
        return "This is the first round. Nothing has been tried yet."
    return json.dumps(history, indent=2, ensure_ascii=False, default=str)


def correction_user(
    drawing_json: str, findings: list[Finding], history: list[dict[str, Any]]
) -> str:
    """Build the correction request for one round.

    Args:
        drawing_json: The current drawing, as JSON, including every item's id.
        findings: What the rules say about that drawing right now.
        history: One entry per earlier round — what was applied, what was rejected and
            why, which findings cleared, and which ones the round introduced. Earlier
            rounds are the only way the model learns that an edit was refused, so this is
            what stops it proposing the same refused edit three times in a row.
    """
    return f"""\
Here is the drawing, the data extracted from it, and what the rule engine found.

=== CURRENT DRAWING DATA ===
{drawing_json}

=== FINDINGS TO ADDRESS ===
{_findings_block(findings)}

=== WHAT HAS ALREADY BEEN TRIED ===
{_history_block(history)}

=== OPERATIONS YOU MAY USE ===
Only these {len(ALLOWED_OPS)} operations exist. Anything else is refused unapplied.

{_OPERATIONS}
=== YOUR ANSWER ===
Propose the smallest set of edits that corrects the findings above. Read the image to
decide what the drawing means; use the ids from the data above to say what to change.

If a previous round's edit was rejected, do not propose it again — read the reason and
either fix the proposal or take a different approach. If a previous round introduced a new
finding, fix that too.

Output one JSON object: {{"edits": [...]}}. Nothing else.\
"""
