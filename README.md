# DraftCheck

**DraftCheck: an AI checker that catches GD&T and dimensioning errors in engineering drawings before they reach the shop floor.**

- **Live app:** https://draftcheck.onrender.com. It runs on a free host that sleeps when idle, so the first load can take up to a minute.
- **Demo video:** TODO: link

---

## The problem

Every engineering drawing is checked by hand before it is released. The checker looks for datums a tolerance refers to that are never defined, dimensions with no tolerance, and holes placed too close to an edge. Checking is slow, and errors still get through. I am a mechanical engineer and I have worked on CAD software (Siemens, Solid Edge), and I have seen what happens next: the shop builds the part as drawn, and the mistake turns into rework or scrap.

## What it does

1. **Upload** a drawing (PDF or image), or try the built-in sample.
2. **Muse Spark reads it.** It turns the sheet into structured data: dimensions, tolerances, feature control frames, datums, units, and the general tolerance.
3. **The engineer confirms.** Every value is shown in an editable table next to the drawing, with a box around the item on the sheet. Low-confidence readings and cross-check disagreements are marked.
4. **Six rules check it.** These are deterministic Python checks, and each one reports pass, fail, or not applicable with a sentence that uses the drawing's own values.
5. **The agent proposes fixes.** For each error, the model suggests the smallest edit, with a one-sentence engineering reason.
6. **The rules verify the fixes.** Each proposed edit is applied to a copy of the data and the same rules run again. Only that re-run decides whether a fix worked.
7. **Export** a redlined PDF (marked-up sheet plus a page listing each correction) and a CSV of corrections.
8. **Learn from it.** For students, "Explain this drawing" asks the model what the part likely is and why each dimension, frame, and datum is there. The notes are labelled as AI explanations, kept only where they name real items, and never used by the rules.

The screenshots below were captured locally with the **dev fake model** (`DRAFTCHECK_FAKE_MODEL=1`), which returns a saved real reading of the sample drawing so the page can be shown without calling a live model. The rules, the correction loop, and the redline all ran for real.

| Reading | Confirm |
| --- | --- |
| ![The drawing being read, with a scan band across the sheet](docs/images/01-reading.png) | ![Extracted tables next to the drawing, with item boxes and a cross-check disagreement marker on dim_5](docs/images/02-confirm.png) |
| **Checked** | **Done** |
| ![Six check results with one failure, and a red revision cloud around the feature control frame](docs/images/03-checked.png) | ![The verified fix shown with a pen label and a CHECKED stamp](docs/images/04-done.png) |

![Corrections tab with a before/after table and download buttons](docs/images/05-corrections.png)

![The "About this drawing" card with a part-type chip, summary, and features, and AI notes under each datum row](docs/images/06-explain.png)

## How it uses Meta's models

| Model | Where it runs | What DraftCheck uses it for |
| --- | --- | --- |
| **Muse Spark Open** (open-weight) | Baseten | The default. Reads the drawing and runs every round of the correction loop. |
| **Muse Spark** | Meta Model API | (1) An **independent cross-check reader**: it reads the same page at the same time, and any value the two models read differently is flagged for the engineer to review. (2) An **automatic fallback**: if Baseten does not answer a call in time, fails to connect, or returns a server error, that one call is sent to Muse Spark instead. |

**What the models do:**

- Vision extraction: read the drawing image and return structured JSON.
- Say what each dimension measures between (for example `left_edge` → `hole_1_center`), so the rules can reason about geometry.
- Give a confidence for each item they read.
- Propose edits to fix rule violations, each with an engineering reason.
- Revise their proposals after the code rejects an edit, using the rejection reason.
- Explain the drawing for students ("About this drawing"): what the part likely is and why each dimension, frame, and datum is there. A separate call, labelled as unchecked AI text, kept only where it names real items, and never used by the rules.

**What the models never do:** decide whether a rule passes or fails.

## Core principle

> **The model reads and reasons; deterministic Python decides.**

Safeguards that enforce it:

- **A fixed edit menu.** The agent can only propose four operations: set a field, add a datum, remove a datum reference from a frame, or add a dimension.
- **No deletions.** There is no operation that deletes a dimension, frame, or datum, and a backstop check rejects any edit that would make an existing item disappear. Deleting a callout is the cheapest way to make a finding go away, so it is not allowed.
- **Verdicts only come from re-running the rules.** The model's claim that its fix worked is recorded but never used.
- **A 3-round cap** on the correction loop.
- **Every rule explains itself** from real values: "The frames reference A, B, C, but nothing defines C."
- **Fixes are proposals.** They are marked for engineer review, and the CLI's exit code always describes the drawing as submitted.

## Results

Seventeen generated drawings, each run end to end (read → rules → correction loop) once per model, with known answers. They cover ten part types: eight plates, a bracket, a spur gear, a hex nut, a hex bolt, a flange, a stepped shaft, a gripper claw, a machined block, and a bolted-joint assembly. Together they have 29 views (including three sections, a detail, and an isometric), 19 GD&T frames, and 13 seeded issues. Scores are computed in Python against each drawing's ground truth. Totals from [docs/EVAL.md](docs/EVAL.md):

| | Muse Spark Open (Baseten) | Muse Spark (Meta Model API) |
| --- | --- | --- |
| Dimensions correct | 122/139 | 125/139 |
| Feature control frames correct | 16/19 | 16/19 |
| Datums found | 26/27 | 26/27 |
| Seeded issues caught | 11/13 | 11/13 |
| False alarms | 1 | 1 |
| Fixes verified by the rules | 10/10 | 10/10 |
| Part type recognized† | 15/17 | 15/17 |
| Views named correctly† | 27/29 | 22/29 |
| Dimensions placed in the right view† | 137/139 | 99/139 |
| Average latency per drawing* | 54.1 s | 24.4 s |
| Runs that used the fallback | 8 of 17 | 0 of 17 |

\* Total latency in EVAL.md divided by 17 runs (919.6 s and 414.3 s), for reading, rules, and the correction loop.
† From the "About this drawing" explanation (`python -m scripts.run_eval --explain`), which is scored on the ground-truth drawing, so reading errors don't affect it.

Browse every drawing, its scores, and which model caught each seeded issue on the live dashboard: **[draftcheck.onrender.com/eval.html](https://draftcheck.onrender.com/eval.html)**.

The drawings:

| Drawing | What it tests |
| --- | --- |
| plate_001 | The original sample: a position frame that references datum C, which is never defined |
| plate_clean | No errors at all, to check for false alarms |
| plate_dup_datum | Two datum symbols both labelled A |
| plate_flatness_datum | A flatness tolerance that references a datum |
| plate_no_basic | A position tolerance with no basic dimensions |
| plate_no_gentol | No general tolerance, and a dimension with no tolerance of its own |
| plate_edge_hole | A Ø6 hole centred 5 mm from an edge |
| bracket_01 | Front and side views, notes, full title block, with three seeded issues (undefined datum, flatness with a datum, hole near an edge) |
| plate_inch_multi | Inch units, four holes, two frames, datum B defined twice |
| gear_spur | A spur gear (24 teeth, module 2) in front and side views, with circular runout on the outside diameter to the bore. No errors |
| hex_nut | An M10 hex nut in top and front views, across flats and across corners (reference). No errors |
| hex_bolt | An M8 hex bolt in front and end views, with head height, thread length and length. No errors |
| flange | A 6-bolt flange with a hatched SECTION A-A, a basic bolt circle and perpendicularity of the bore; the hole position frame references datum C, which is never defined |
| stepped_shaft | A three-diameter shaft with a keyway, SECTION B-B and a 4:1 DETAIL C of the fillet; no general tolerance, and one diameter with no tolerance of its own |
| gripper_claw | An irregular claw in front and top views, with a 15° taper, a curved jaw on basic dimensions and a profile tolerance. No errors |
| block_3view | A machined block in three views and an isometric, with a counterbored hole, a hole table and parallelism; a flatness tolerance references a datum |
| bolted_joint | An assembly: two plates, bolt, washer and nut, in a front view and a hatched section, with balloons and a parts list. No errors |

Where the models missed:

- **Seeded issues (11/13 each).** Both models missed the MFG-001 hole-to-edge warning on bracket_01; both caught and fixed the two errors on that drawing. Each also missed one duplicate datum: Muse Spark Open on plate_dup_datum and Muse Spark on plate_inch_multi. In both cases the model read only one of the two symbols that share a letter, so the rules never saw the duplicate.
- **False alarms (1 each).** Both models' readings of gripper_claw, a clean drawing, triggered an MFG-001 hole-to-edge warning that the ground truth does not.
- **Frames (16/19 each).** Neither model read the circular runout frames (gear_spur, stepped_shaft) or the profile-of-a-surface frame (gripper_claw) correctly. Every other frame was read correctly by both.
- **Dimensions.** Both models matched only 10 of block_3view's 24 dimensions; 12 of those 24 are the hole table's entries (X, Y, and size for four holes). Muse Spark Open also missed one dimension each on plate_no_gentol, hex_nut, and hex_bolt. Every dimension either model found also had the right tolerances and basic flag.
- **Part type (15/17 each).** Muse Spark Open called plate_001 a bracket and plate_inch_multi a cover; Muse Spark called plate_inch_multi a cover and gripper_claw a plate.
- **Views.** Muse Spark's 22/29 comes from six of the single-view plates, where it named the one view something other than "front", so none of their dimensions counted as placed in the right view (99/139). On bolted_joint both models found one of the two views and named the other something else; Muse Spark Open did the same with one of block_3view's four views. Muse Spark Open's two dimension-view misses are one on each of those drawings.
- **Fallback.** Muse Spark Open needed the Meta fallback on 8 of its 17 runs; Muse Spark never did.

**Be aware:** these are drawings I generated with a script, not real production drawings, so they are cleaner than what a shop sees.

## Architecture

```mermaid
flowchart LR
    A[Upload PDF or image] --> B[Render page 1<br/>pypdfium2]
    B --> C[Muse Spark Open reads<br/>JSON extraction]
    B -.parallel.-> X[Muse Spark cross-check<br/>compared in Python]
    X -.flags cells.-> D
    C --> D[Engineer confirms<br/>or edits tables]
    D --> E[6 deterministic rules]
    E -->|errors| F[Agent proposes edits<br/>fixed menu]
    F --> G[Apply to a copy<br/>reject unsafe edits]
    G --> E
    E -->|clean or 3 rounds| H[Redline PDF + CSV]
    C -. Baseten timeout .-> M[Fallback to Muse Spark]
```

Full design, data flow, and decision log: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## The rules

| Id | What it checks | Severity |
| --- | --- | --- |
| GDT-001 | A feature control frame refers to a datum letter that no datum symbol on the drawing defines | error |
| GDT-002 | The same datum letter is defined more than once | error |
| GDT-003 | A form tolerance (flatness, straightness, circularity, cylindricity) references a datum, which has no meaning | error |
| GDT-004 | The drawing has a position tolerance but no basic dimensions anywhere | error |
| DIM-001 | Units are missing, or there is no general tolerance while some dimension has no tolerance of its own | error |
| MFG-001 | A hole leaves less material between its wall and an edge than one hole diameter | warning |

Each rule is written in my own words and has a `source` field that cites its reference by topic (for example "ASME Y14.5-2018, datum reference frames"). MFG-001 cites general design-for-manufacturing practice, and its threshold is a named constant you can tune. No standards text is copied into the code.

## Engineering

- **595 tests** (`pytest`), plus `ruff` linting.
- **CI on every pull request** (GitHub Actions runs ruff and pytest), and **branch protection** on `main` requires CI to pass.
- **Usage limits** for the free demo: 5 uploads and 10 fix requests per IP per hour, and 100 model calls per day across everyone. Uploaded drawings are deleted after 24 hours.
- **Cancel** buttons for reading and fixing. Cancel reaches the server and aborts the model call.
- **Time limits:** each model call has a 90 s limit on the deployed server.
- **Fallback:** a Baseten call with no answer after 45 s is sent once to the Meta Model API. Who answered is recorded on the page, in the files, and on the redline stamp.
- **Cross-check:** the second reading gets at most 20 s after the first one returns, then the upload continues without it.
- **Production fixes:**
  - *Server freezing during uploads.* The model call blocked the thread that answers health checks, so the host could decide the app had died and restart it mid-upload. Slow work now runs on worker threads, and a test checks that `/healthz` still answers while a model call is in progress.
  - *Render memory.* Large multi-page PDFs pushed the free plan past its memory limit. Now only page 1 is rendered, once, at a capped size, and each stage logs memory use.

## Run it locally

Requires Python 3.12.

```bash
python3.12 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root with these variables (values come from your own Baseten and Meta accounts):

```
LLM_PROVIDER=baseten
BASETEN_BASE_URL=
BASETEN_MODEL=
BASETEN_API_KEY=
META_BASE_URL=
META_MODEL=
META_API_KEY=
```

Start the web app, then open http://127.0.0.1:8000:

```bash
uvicorn draftcheck.api:app --reload
```

Check a drawing from the command line (fix loop and redline included):

```bash
python -m draftcheck.cli check samples/generated/plate_001.pdf --fix --redline
```

Run the evaluation (calls both models and rewrites `docs/EVAL.md`):

```bash
python -m scripts.run_eval
```

Add `--explain` to also run and score the "About this drawing" explanation for each drawing (one more model call per drawing and model):

```bash
python -m scripts.run_eval --explain
```

For UI work with no model calls, start the app with the fake model. It only answers requests from your own machine:

```bash
DRAFTCHECK_FAKE_MODEL=1 uvicorn draftcheck.api:app --reload
```

Run the tests and linter:

```bash
pytest
```

```bash
ruff check .
```

## Limitations

- Only page 1 of a drawing is read.
- Only 6 rules so far.
- The bounding boxes the model returns vary in accuracy, so a highlight box may be slightly off its callout.
- Rules that depend on what a dimension measures between (MFG-001) rely on the model's endpoint labels.
- MFG-001 checks every hole against the largest hole diameter on the drawing, which is conservative but not exact.
- Circular runout and profile-of-a-surface frames are not yet read reliably (0 of 3 in the eval).
- Hole tables are not yet extracted as dimensions (block_3view: 10 of 24 dimensions read).
- Baseten calls occasionally hang. The fallback handles it, but those runs are slower.
- The free host sleeps when idle, so the first visit can take up to a minute.
- Fixes are proposals for an engineer to review, not edits to the CAD model.

## Roadmap

- Check the notes on the drawing.
- Check BOM tables.
- Assembly checks: parts lists, balloons, BOM consistency, fits between parts.
- More rules.
- Per-hole diameters for MFG-001.
- SAM 3.1 for precise regions around each callout.
- Redlines written back into CAD-native formats.
- Accounts, saved history, and revision comparison (Rev A vs Rev B).

## Built during the hackathon

Everything in this repo was written from Oct 27 to Nov 4, 2026, during the Meta Global AI Developer Hackathon. The code was written with Claude Code under my direction. The architecture, rule design, and engineering review are mine.

## License

[MIT](LICENSE)
