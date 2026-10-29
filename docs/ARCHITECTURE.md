# DraftCheck — Architecture

Every item below is marked **BUILT** (exists in this repo today) or **PLANNED** (designed,
not yet written).

Last updated: 2026-10-28

## 1. Overview

DraftCheck reads an engineering drawing (PDF or image) and extracts its dimensions, GD&T
callouts, notes, and title-block data into structured JSON. It then checks that data
against a rule set covering dimensioning, GD&T, and manufacturability, and proposes a
correction for every violation it finds. The result is a redlined PDF showing each error
and the suggested fix.

## 2. Core principle

**The LLM reads and reasons; deterministic Python decides pass/fail.**

The model is used only for perception and explanation — turning pixels into structured
facts, and turning a rule violation into wording a person can act on. Whether a rule
passes is always computed in plain Python from the extracted data, so the same drawing
always produces the same verdicts. No rule outcome is ever read out of a model response.

## 3. Pipeline — PLANNED

```mermaid
flowchart TD
    A[Upload drawing<br/>PDF or image] --> B[Render to image<br/>pypdfium2]
    B --> C[Vision extraction<br/>LLM returns JSON]
    C --> D[User confirms<br/>or edits extraction]
    D -->|confirmed or edited| E[Rule engine<br/>deterministic checks]
    E --> F{Violations found?}
    F -->|no| H[Redlined PDF output]
    F -->|yes| G[Agent correction loop<br/>propose fix, re-check<br/>max 3 rounds]
    G --> E
    G -->|loop exhausted| H
```

Stage notes:

| Stage | What happens | Status |
| --- | --- | --- |
| Render | PDF page rasterized to an image the model can see | PLANNED |
| Extract | Model returns a JSON object; parsed by `llm_parsing.extract_json` | PLANNED (parser BUILT) |
| Confirm | User reviews and edits the extracted values before any checking | PLANNED |
| Check | Python rules produce pass/fail per rule, with no model involvement | PLANNED |
| Correct | Agent proposes a fix, rules re-run to verify it; **max 3 rounds**, then stop | PLANNED |
| Output | Annotated PDF with redline markup and a findings list | PLANNED |

The 3-round cap is a hard stop: if a proposed correction still fails, the finding is
reported unresolved rather than retried indefinitely.

## 4. Components

| Module | Role | Status |
| --- | --- | --- |
| `draftcheck/llm_parsing.py` | Pulls a single JSON object out of model output; strips `<think>` blocks and code fences; raises `JSONParseError` with the raw text attached | **BUILT** |
| `draftcheck/config.py` | Locates `.env` with `find_dotenv` and builds a frozen `Provider` (name, base_url, model, api_key); raises `ConfigError` naming any missing variable, and masks the key in `repr` | **BUILT** |
| `draftcheck/llm_client.py` | `LLMClient.chat(system, user)` over the OpenAI-compatible Baseten endpoint at `temperature=0`; returns reply text, latency, token usage, and model id | **BUILT** |
| `draftcheck/render.py` | `load_pages(path, dpi, max_side)` rasterizes a PDF with pypdfium2 or loads a PNG/JPG with pillow; returns `PageImage` (PNG bytes, width, height) per page, scaled down to `max_side` | **BUILT** |
| `draftcheck/prompts.py` | Prompt text for extraction and for correction wording | PLANNED |
| `draftcheck/schema.py` | pydantic models for the extracted drawing, findings, and corrections | PLANNED |
| `draftcheck/rules.py` | Deterministic rule functions; each carries a `source` field citing the reference | PLANNED |
| `draftcheck/agent.py` | Correction loop: propose, re-check, stop at 3 rounds | PLANNED |
| `draftcheck/redline.py` | Overlays markup on the original PDF; library to be chosen in Layer 2 | PLANNED |
| `draftcheck/api.py` | FastAPI app: upload, confirm, results endpoints | PLANNED |
| `frontend/` | Upload page, extraction review/edit screen, findings view | PLANNED |
| `scripts/make_sample_drawing.py` | Generates a synthetic drawing (reportlab) with its ground-truth JSON and a PNG preview; seeds a known error so the rule engine has something to catch | **BUILT** |
| `scripts/smoke_test.py` | One-shot check that the configured model answers over the network | **BUILT** |

Rule text is always written in our own words with a `source` citation — no standards text
is copied into the code.

## 5. Repo layout (what exists today)

```
draftCheck/
├── .github/
│   └── workflows/
│       └── ci.yml              # ruff + pytest on push to main and on PRs
├── docs/
│   └── ARCHITECTURE.md         # this file
├── draftcheck/
│   ├── __init__.py
│   ├── config.py               # BUILT
│   ├── llm_client.py           # BUILT
│   ├── llm_parsing.py          # BUILT
│   └── render.py               # BUILT
├── samples/
│   └── generated/              # synthetic drawings, regenerable from scripts/
│       ├── plate_001.pdf       # the drawing
│       ├── plate_001.gt.json   # ground truth, bboxes normalized top-left 0..1
│       └── plate_001.png       # rendered preview
├── scripts/
│   ├── __init__.py
│   ├── make_sample_drawing.py  # python -m scripts.make_sample_drawing
│   └── smoke_test.py           # python -m scripts.smoke_test
├── tests/
│   ├── test_config.py
│   ├── test_llm_parsing.py
│   ├── test_render.py
│   └── test_sample_drawing.py
├── CLAUDE.md                   # working rules for Claude Code
├── LICENSE                     # MIT
├── README.md
├── pyproject.toml
└── requirements.txt
```

## 6. Development workflow — BUILT

1. Claude Code works on a feature branch inside a git worktree, one step at a time.
2. Aakruti reviews the change and does all git operations — Claude Code never commits,
   pushes, merges, or rebases.
3. The branch goes up as a pull request against `main`.
4. CI runs `ruff check .` and `pytest`; both must pass.
5. Branch protection on `main` requires passing CI. No review is required, since this is
   a solo project.

## 7. Decisions log

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-10-27 | Muse Spark Open on Baseten as the primary model | Credits available, and it makes the project eligible for the open-weight bonus prize |
| 2026-10-27 | MIT license | Hackathon rules require a public repo; MIT makes it genuinely open source |
| 2026-10-28 | pypdfium2 to render PDFs to images; reportlab to generate synthetic test drawings | Permissive licenses; testable locally without external services |
| 2026-10-28 | Branch protection with required CI and no required review | Solo developer, so CI is the gate instead of a reviewer |
| 2026-10-28 | `.env` lives at the repo root; config uses `find_dotenv` to locate it from any worktree | One secret file, never committed, works from the main folder and Claude Code worktrees |

## 8. Status

| Layer | What it covers | State |
| --- | --- | --- |
| 1 | Extraction + JSON schema + rule engine + test generator | IN PROGRESS — `llm_parsing.py` BUILT, rest PLANNED |
| 2 | Agent correction loop + redlined PDF | PLANNED |
| 3 | Web UI + live deploy | PLANNED |
| 4 | After hackathon: SAM 3.1 precision, revisions, link to Research Paper Recommender | PLANNED (post-hackathon) |
