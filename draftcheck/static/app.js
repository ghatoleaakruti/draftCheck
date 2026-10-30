/* DraftCheck — upload a drawing, correct what the model read, and check it.
 *
 * Two requests. POST /api/runs sends the file and the chosen provider, and the reply
 * carries the run id, the extracted drawing, and the model/latency metadata; the rendered
 * page comes back separately from GET /api/runs/{id}/page.png. POST /api/runs/{id}/check
 * sends the drawing the user confirmed and returns the findings.
 *
 * Nothing here decides whether the drawing passes. The tables are an editor and a
 * display: the rules run server side, on exactly the JSON this file posts.
 *
 * Two copies of the drawing are kept. `original` is what the model read and is never
 * touched — it is what the "edited" marks compare against and what Reset restores.
 * `working` starts as a copy of it, the inputs write into it, and it is the request body,
 * so every field nobody edited is passed through exactly as extracted.
 *
 * Every cell is written with textContent or as an input `value`, never innerHTML: the
 * strings below come from a language model reading an uploaded file, so they are treated
 * as text, not markup.
 */

"use strict";

const EM_DASH = "—";

const form = document.getElementById("upload-form");
const fileInput = document.getElementById("file");
const providerSelect = document.getElementById("provider");
const submitButton = document.getElementById("submit");

const dropzone = document.getElementById("dropzone");
const dropMain = document.getElementById("drop-main");

const statusLine = document.getElementById("status");
const errorBox = document.getElementById("error");
const errorText = document.getElementById("error-text");
const resultSection = document.getElementById("result");

const parseIssuesBox = document.getElementById("parse-issues");
const parseIssuesList = document.getElementById("parse-issues-list");
const pageImage = document.getElementById("page-image");
const overlay = document.getElementById("overlay");
const overlayToggle = document.getElementById("show-overlay");
const dataPanel = document.querySelector(".data-panel");

const checkButton = document.getElementById("check");
const resetButton = document.getElementById("reset");
const addDatumButton = document.getElementById("add-datum");
const invalidNote = document.getElementById("invalid-note");

const confidenceSummary = document.getElementById("confidence-summary");

const findingsPanel = document.getElementById("findings-panel");
const findingsList = document.getElementById("findings-list");
const findingsNone = document.getElementById("findings-none");
const checkStatus = document.getElementById("check-status");
const checkErrorBox = document.getElementById("check-error");
const checkErrorText = document.getElementById("check-error-text");
const checkIssuesBox = document.getElementById("check-parse-issues");
const checkIssuesList = document.getElementById("check-parse-issues-list");

const proposeButton = document.getElementById("propose-fix");
const fixHint = document.getElementById("fix-hint");
const fixStatus = document.getElementById("fix-status");

const agentPanel = document.getElementById("agent-panel");
const agentStatus = document.getElementById("agent-status");
const agentErrorBox = document.getElementById("agent-error");
const agentErrorText = document.getElementById("agent-error-text");
const agentRounds = document.getElementById("agent-rounds");
const agentVerdicts = document.getElementById("agent-verdicts");
const redlineImage = document.getElementById("redline-image");
const redlinePdf = document.getElementById("redline-pdf");

/* ---- The drop zone ------------------------------------------------------- */

const DROP_PROMPT = "Drop a drawing here, or click to choose a file";

/** What the server will open, checked here so an obvious mistake costs no request. */
const ALLOWED_SUFFIXES = [".pdf", ".png", ".jpg", ".jpeg"];
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

/** True while a run is in flight; the drop zone ignores clicks and drops then. */
let busy = false;

/**
 * Why this file cannot be sent, or `null` when it can.
 *
 * Checked on the file name, not on the browser's `type`, which is a guess the client
 * makes from the extension anyway. The server checks both again — this is only here so
 * the wrong file gives an answer immediately instead of after a round trip.
 */
function rejectionReason(file) {
  const name = file.name || "";
  const dot = name.lastIndexOf(".");
  const suffix = dot === -1 ? "" : name.slice(dot).toLowerCase();

  if (!ALLOWED_SUFFIXES.includes(suffix)) {
    return `“${name}” is not a drawing DraftCheck can read. Use a PDF, PNG, or JPG file.`;
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    const megabytes = (file.size / (1024 * 1024)).toFixed(1);
    return `“${name}” is ${megabytes} MB. The limit is 10 MB.`;
  }
  return null;
}

/** Show one of the three states; the others are hidden. */
function setBusy(isBusy) {
  busy = isBusy;
  submitButton.disabled = isBusy;
  submitButton.textContent = isBusy ? "Reading…" : "Read drawing";
  statusLine.hidden = !isBusy;

  dropzone.classList.toggle("is-busy", isBusy);
  dropzone.classList.remove("is-dragging");
  dropzone.setAttribute("aria-disabled", String(isBusy));
  dropzone.tabIndex = isBusy ? -1 : 0;
}

/** The file name while reading, the invitation the rest of the time. */
function setDropLabel(fileName) {
  dropMain.textContent = fileName || DROP_PROMPT;
}

function showError(message) {
  errorText.textContent = message;
  errorBox.hidden = false;
}

function clearOutput() {
  errorBox.hidden = true;
  resultSection.hidden = true;
}

/* ---- Formatting helpers -------------------------------------------------- */

/** A value for a cell, or an em dash when the model left it out. */
function text(value) {
  if (value === null || value === undefined || value === "") {
    return EM_DASH;
  }
  return String(value);
}

/** A number trimmed of trailing zeros, e.g. 12.50 -> "12.5". */
function num(value) {
  if (typeof value !== "number" || !isFinite(value)) {
    return EM_DASH;
  }
  return String(Number(value.toFixed(4)));
}

/**
 * The same, but as something to put in an input's `value`.
 *
 * Unrounded, unlike `num`: a box the user has not touched must read back as the exact
 * number the model gave, or the cell would mark itself edited on sight.
 */
function numInput(value) {
  return typeof value === "number" && isFinite(value) ? String(value) : "";
}

/** "28.4 s". One decimal: this is how long the model took, not a measurement. */
function seconds(value) {
  if (typeof value !== "number" || !isFinite(value)) {
    return EM_DASH;
  }
  return `${value.toFixed(1)} s`;
}

/** "left_edge → hole_1_center", or an em dash when the pair is incomplete. */
function fromTo(dimension) {
  if (!dimension.from_ref && !dimension.to_ref) {
    return EM_DASH;
  }
  return `${text(dimension.from_ref)} → ${text(dimension.to_ref)}`;
}

/**
 * Datum references as one editable string: "A|B(MMC)|C".
 *
 * The bars are what a drawing's frame does — one compartment per datum, most important
 * first — and the bracketed modifier keeps a frame that carries one from losing it the
 * moment somebody edits the cell. `parseDatumRefs` is the exact opposite of this.
 */
function datumRefsText(refs) {
  if (!Array.isArray(refs) || refs.length === 0) {
    return "";
  }
  return refs
    .map((ref) => (ref.modifier ? `${text(ref.label)}(${ref.modifier})` : text(ref.label)))
    .join("|");
}

/* ---- Linking the tables to the drawing ----------------------------------- */

/*
 * Every datum, dimension, and frame has an id (DT1, D1, F1…) and, when the model said
 * where it is, a bbox: [x0, y0, x1, y1] in 0..1 from the top-left of the page. The id is
 * the thread — one box on the image, one row in a table, and any finding about the item
 * all share it, so pointing at any of them can light up the others.
 */

/** item id -> { box, row }. `box` is null for an item with no bbox. Rebuilt per run. */
const linked = new Map();

/** The ids lit by the last click — one for a row or box, possibly several for a finding. */
let pickedIds = [];

/** Put a class on both halves of a pair, or do nothing for an id we cannot locate. */
function paint(id, className, on) {
  const pair = linked.get(id);
  if (!pair) {
    return;
  }
  if (pair.box) {
    pair.box.classList.toggle(className, on);
  }
  if (pair.row) {
    pair.row.classList.toggle(className, on);
  }
}

/**
 * Choose one or more items: light them on both sides and bring `scrollInto` into view.
 *
 * `block: "nearest"` scrolls the least that will do the job — the page image is sticky,
 * so a bigger jump would shove the drawing around for no reason.
 */
function pick(ids, scrollInto) {
  for (const previous of pickedIds) {
    paint(previous, "is-picked", false);
  }
  pickedIds = ids;
  for (const id of pickedIds) {
    paint(id, "is-picked", true);
  }
  if (scrollInto) {
    scrollInto.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

/**
 * Register every item and draw a box for the ones the model placed.
 *
 * Items without a bbox are registered too, with `box: null`: a datum the user adds has no
 * place on the drawing, but a finding about it must still be able to light its row.
 */
function buildOverlay(items) {
  linked.clear();
  pickedIds = [];
  overlay.replaceChildren();

  for (const item of items) {
    if (!item.id) {
      continue;
    }
    const bbox = item.bbox;
    if (!Array.isArray(bbox) || bbox.length !== 4) {
      linked.set(item.id, { box: null, row: null });
      continue;
    }
    const [x0, y0, x1, y1] = bbox;

    const box = document.createElement("div");
    box.className = "obox";
    box.dataset.itemId = item.id;
    // Percentages, not pixels: the image is `width: 100%`, so its size changes with the
    // window and a pixel offset would drift off the drawing.
    box.style.left = `${x0 * 100}%`;
    box.style.top = `${y0 * 100}%`;
    box.style.width = `${(x1 - x0) * 100}%`;
    box.style.height = `${(y1 - y0) * 100}%`;
    box.title = item.title;
    // Amber instead of the faint blue, so the items worth a second look are findable on
    // the drawing without reading the tables at all.
    box.classList.toggle("is-unsure", Boolean(item.unsure));

    overlay.appendChild(box);
    linked.set(item.id, { box, row: null });
  }
}

/**
 * Tie a freshly built row to its box, or mark it as unplaceable.
 *
 * A row is always interactive — `data-item-id` is what the hover and click handlers look
 * for — but a row whose item has no box says so, since pointing at it lights nothing on
 * the drawing.
 */
function linkRow(id, row, firstCell) {
  const pair = id ? linked.get(id) : undefined;
  if (!pair) {
    return;
  }
  pair.row = row;
  row.dataset.itemId = id;
  if (!pair.box && firstCell) {
    const note = document.createElement("span");
    note.className = "noloc";
    note.textContent = "no location";
    firstCell.appendChild(note);
  }
}

/* Hovering either side lights both. Delegated, so the handlers survive every rebuild of
   the tables and the overlay. */
function hoverHandlers(container) {
  container.addEventListener("mouseover", (event) => {
    const target = event.target.closest("[data-item-id]");
    if (target) {
      paint(target.dataset.itemId, "is-hot", true);
    }
  });
  container.addEventListener("mouseout", (event) => {
    const target = event.target.closest("[data-item-id]");
    if (target) {
      paint(target.dataset.itemId, "is-hot", false);
    }
  });
}

hoverHandlers(overlay);
hoverHandlers(dataPanel);

// Clicking a box goes to the row; clicking a row goes to the box.
overlay.addEventListener("click", (event) => {
  const box = event.target.closest(".obox");
  if (box) {
    const pair = linked.get(box.dataset.itemId);
    pick([box.dataset.itemId], pair && pair.row);
  }
});

dataPanel.addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-item-id]");
  if (row) {
    const pair = linked.get(row.dataset.itemId);
    pick([row.dataset.itemId], pair && pair.box);
  }
});

// The toggle only hides the drawing's boxes. The table rows stay as they are, so a
// reader who switches the boxes off keeps everything the model read.
overlayToggle.addEventListener("change", () => {
  overlay.hidden = !overlayToggle.checked;
});

/* ---- The two copies of the drawing --------------------------------------- */

/** The run being reviewed. `original` is the model's reading; `working` is the user's. */
const state = {
  runId: null,
  original: null,
  working: null,
  /** True once a check has returned, so a later edit can mark its findings out of date. */
  checked: false,
  /** True once the tables have moved on from the last check. */
  stale: false,
  /** Error-severity findings in the last check. The agent is only offered when there are some. */
  errorCount: 0,
  /** When the correction loop last returned, or null. The agent results go stale too. */
  fixedAt: null,
};

/** A deep copy, so editing one copy of the drawing can never reach the other. */
function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

/** The item in a list with this id, or undefined. Ids are unique within a list. */
function byId(list, id) {
  return (list || []).find((item) => item.id === id);
}

/**
 * An id for a datum the user adds.
 *
 * The server gives ids to items that arrive without one, numbering them by position — so
 * an added datum could be handed an id another datum already has. Choosing the id here
 * avoids that entirely, and keeps the row linkable the moment it appears.
 */
function newDatumId() {
  const taken = new Set((state.working.datums || []).map((datum) => datum.id));
  let n = 1;
  while (taken.has(`DT${n}`)) {
    n += 1;
  }
  return `DT${n}`;
}

/* ---- Validation ---------------------------------------------------------- */

/*
 * An invalid cell is one the server could only guess at: a tolerance that is not a
 * number, a datum called "AB". Rather than send it and get a surprise back, the input is
 * outlined, the reason sits in its tooltip, and Check stays disabled until the set is
 * empty. `working` is left holding the last good value, so nothing half-typed is posted.
 */

/**
 * The inputs currently holding something unusable.
 *
 * Emptied by `renderTables`, which is the only thing that throws the controls away: an
 * input that has been replaced cannot be fixed, and would otherwise keep Check disabled
 * forever. Every cell re-registers itself as it is rebuilt.
 */
const invalidInputs = new Set();

/** True while the correction loop is running; both buttons are dead for its duration. */
let fixing = false;

function updateCheckButton() {
  const blocked = invalidInputs.size > 0;
  checkButton.disabled = blocked || busy || fixing;
  invalidNote.hidden = !blocked;
  updateFixButton();
}

/** Mark an input good or bad, and say why in its tooltip when it is bad. */
function setValid(input, ok, message) {
  input.classList.toggle("is-invalid", !ok);
  input.setAttribute("aria-invalid", String(!ok));
  if (ok) {
    invalidInputs.delete(input);
    input.removeAttribute("title");
  } else {
    invalidInputs.add(input);
    input.title = message;
  }
  updateCheckButton();
}

/**
 * The number in a numeric input.
 *
 * A `type="number"` input empties its own value when what was typed is not a number, and
 * raises `badInput` instead — so that is checked first, before an empty box could be
 * mistaken for a field left blank on purpose.
 */
function readNumber(input, { required, min }) {
  if (input.validity && input.validity.badInput) {
    return { ok: false, value: null };
  }
  const raw = input.value.trim();
  if (raw === "") {
    return { ok: !required, value: null };
  }
  const parsed = Number(raw);
  if (!isFinite(parsed)) {
    return { ok: false, value: null };
  }
  if (min !== undefined && parsed < min) {
    return { ok: false, value: null };
  }
  return { ok: true, value: parsed };
}

/**
 * "A|B(MMC)|C" as a list of datum references, or `null` when it is not readable.
 *
 * Empty compartments are skipped rather than rejected, so a half-typed "A|" is not
 * flagged while the next letter is still on its way.
 */
function parseDatumRefs(raw) {
  const refs = [];
  for (const piece of raw.split("|")) {
    const part = piece.trim().toUpperCase();
    if (part === "") {
      continue;
    }
    const match = /^([A-Z])(?:\((MMC|LMC)\))?$/.exec(part);
    if (!match) {
      return null;
    }
    refs.push({ label: match[1], modifier: match[2] || null });
  }
  return refs;
}

/* ---- Editable cells ------------------------------------------------------ */

/*
 * Every editable cell is built the same way: a control, a rule for reading it, and
 * somewhere in `working` to put the result. `commit` runs on each keystroke and does
 * three things — validate, write the value through, and repaint the cell's "edited" mark
 * against `original`. Anything that does not validate is not written through.
 */

/*
 * Keeping a cell wide enough for what is in it.
 *
 * The controls are `width: 100%` of their column, and a table squeezes its columns to fit
 * the panel — so on a narrow window "100" came out as "10" and "0.2" as "0". A cell cannot
 * be narrower than the control's `min-width`, so setting that per control from the value
 * it holds makes the column grow instead of the digits disappearing, and the table carries
 * the overflow to `.table-scroll` when the panel runs out of room.
 */

/** The cell's padding and border, plus the spinner a number input draws over its value. */
const INPUT_PAD_CH = 3.5;

/** No control is narrower than this, so an empty box is still worth clicking. */
const MIN_INPUT_CH = 5;

/** Set a text or number control's minimum width from the value it is holding. */
function fitControl(control) {
  if (control.tagName !== "INPUT" || control.type === "checkbox") {
    return;
  }
  const characters = Math.max(control.value.length, 0) + INPUT_PAD_CH;
  control.style.minWidth = `${Math.max(MIN_INPUT_CH, characters)}ch`;
}

/** Add or remove a cell's accent, with the model's own value in the tooltip. */
function markEdited(cell, edited, originalText) {
  cell.classList.toggle("is-edited", edited);
  if (edited) {
    cell.title = `Edited. Muse Spark read: ${originalText}`;
  } else {
    cell.removeAttribute("title");
  }
}

/**
 * One editable cell.
 *
 * `read` turns the control into `{ ok, value }`; `apply` writes an accepted value into
 * `working`; `was` is what the model read, shown when the two differ. `same` compares the
 * two, defaulting to `===` — the datum-references cell compares strings instead. `cell`
 * reuses a `<td>` already on the page, which is how the units table is filled.
 */
function editCell(control, { read, apply, was, shown, same, mono = true, cell }) {
  const target = cell || document.createElement("td");
  if (cell) {
    target.replaceChildren();
  }
  target.classList.toggle("mono", mono);
  target.appendChild(control);

  const matches = same || ((a, b) => a === b);

  /** Validate, write through, and repaint the mark. `first` is the paint on build. */
  function commit(first) {
    // Before anything else, and whatever the value turns out to be: a cell holding
    // something unusable is exactly one the user needs to be able to read.
    fitControl(control);
    const result = read();
    setValid(control, result.ok, result.message || "This value cannot be used.");
    if (!result.ok) {
      return;
    }
    apply(result.value);
    markEdited(target, !matches(result.value, was), shown);
    if (!first) {
      afterEdit();
    }
  }

  // `input` covers all three controls used here — text and number boxes as they are
  // typed in, selects and checkboxes as they are switched.
  control.addEventListener("input", () => commit(false));
  // Built, so paint the mark and flag a value that is unusable from the start — an added
  // datum opens with no label, and that is exactly a cell that needs fixing.
  commit(true);
  return target;
}

/** A plain read-only cell. */
function textCell(value, mono) {
  const cell = document.createElement("td");
  if (mono) {
    cell.className = "mono";
  }
  cell.textContent = value;
  return cell;
}

function numberControl(value, { min } = {}) {
  const input = document.createElement("input");
  input.type = "number";
  input.step = "any";
  if (min !== undefined) {
    input.min = String(min);
  }
  input.value = value;
  input.className = "cell-input cell-number";
  return input;
}

function selectControl(options, value) {
  const select = document.createElement("select");
  select.className = "cell-input cell-select";
  for (const [optionValue, label] of options) {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = label;
    select.appendChild(option);
  }
  select.value = value;
  return select;
}

function textControl(value, { width } = {}) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value;
  input.className = "cell-input";
  if (width) {
    input.size = width;
  }
  return input;
}

/**
 * A number cell wired to one field of one item.
 *
 * `min` is left out for a dimension's value — a coordinate dimension can be negative —
 * and is 0 for every tolerance, where a negative number is never a real reading.
 */
function numberCell(item, field, was, { required, min, cell }) {
  const control = numberControl(numInput(item[field]), { min });
  const wants = min === undefined ? "a number" : `a number of ${min} or more`;
  const message = required ? `Type ${wants}.` : `Type ${wants}, or leave this empty.`;

  return editCell(control, {
    read: () => ({ ...readNumber(control, { required, min }), message }),
    apply: (value) => {
      item[field] = value;
    },
    was,
    shown: num(was),
    cell,
  });
}

/* ---- Low confidence ------------------------------------------------------ */

/*
 * The model returns a confidence with every datum, dimension, and frame. Below the
 * threshold the item is marked on both sides — an amber tag on its row, an amber outline
 * on its box — and counted in one line above the tables, so a reviewer with no time to
 * read every row still knows which ones the model was least sure of.
 *
 * It changes nothing about the check. Confidence is the model's own opinion of its
 * reading; the rules run on the values, not on how sure the model was about them.
 */

/** Below this, an item is marked for a second look. The only place this number lives. */
const CONFIDENCE_THRESHOLD = 0.8;

/**
 * True when the model said it was unsure about this item.
 *
 * A missing or unusable confidence is *unknown*, not *low*: the model said nothing about
 * how sure it was, and marking that as doubt would invent a verdict it never gave.
 */
function isUnsure(item) {
  const value = item && item.confidence;
  return typeof value === "number" && isFinite(value) && value < CONFIDENCE_THRESHOLD;
}

/** The ids flagged in the current tables, in the order the tables show them. */
let unsureIds = [];

/** The amber tag for a row, or null when the model was sure enough about the item. */
function unsureMarker(item) {
  if (!isUnsure(item)) {
    return null;
  }
  const mark = document.createElement("span");
  mark.className = "unsure";
  mark.textContent = "unsure";
  // Two decimals always, so the number is in the same place in every tooltip.
  mark.title =
    `Muse Spark is unsure about this one (confidence ${item.confidence.toFixed(2)}). ` +
    `Please check it.`;
  // The tooltip is the whole message, and a title alone is not reliably announced.
  mark.setAttribute("aria-label", mark.title);
  return mark;
}

/** Scroll to the first flagged row and light it, the way clicking the row would. */
function goToFirstUnsure() {
  const id = unsureIds[0];
  const pair = id ? linked.get(id) : undefined;
  if (!pair || !pair.row) {
    return;
  }
  pick([id], pair.row);
}

/**
 * The line above the tables: how many items to look at, or that there are none.
 *
 * The count is a button only when there is somewhere to go. "Confident about every item"
 * is a statement, and a statement that looks clickable and does nothing is worse than
 * plain text.
 */
function renderConfidenceSummary() {
  if (unsureIds.length === 0) {
    confidenceSummary.className = "confidence-summary is-clear";
    confidenceSummary.replaceChildren("Muse Spark is confident about every item");
    return;
  }

  const button = document.createElement("button");
  button.type = "button";
  button.className = "confidence-link";
  button.textContent =
    `Muse Spark flagged ${unsureIds.length} item${unsureIds.length === 1 ? "" : "s"} ` +
    `to double-check`;
  button.addEventListener("click", goToFirstUnsure);

  confidenceSummary.className = "confidence-summary is-flagged";
  confidenceSummary.replaceChildren(button);
}

/* ---- Building the tables ------------------------------------------------- */

/**
 * Replace a tbody's rows.
 *
 * `rows` is a list of `{ id, cells, marker }`, where each cell is a `<td>`, `id` ties the
 * row to its box on the drawing, and `marker` is the low-confidence tag when there is one.
 * An empty list leaves one spanning row saying so, rather than a table with a header and
 * nothing under it — "the model found none" and "the page has not loaded" should not look
 * alike.
 */
function fillTable(tbodyId, rows, columns, emptyMessage) {
  const tbody = document.getElementById(tbodyId);
  tbody.replaceChildren();

  if (rows.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = columns;
    cell.className = "empty";
    cell.textContent = emptyMessage;
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }

  for (const { id, cells, marker } of rows) {
    const row = document.createElement("tr");
    for (const cell of cells) {
      row.appendChild(cell);
    }
    // The tag rides in the first cell, next to "no location", so a row carries both of
    // the things that are true about it rather than growing a column for each.
    if (marker) {
      row.classList.add("is-unsure");
      cells[0].appendChild(marker);
      if (id) {
        unsureIds.push(id);
      }
    }
    linkRow(id, row, cells[0]);
    tbody.appendChild(row);
  }
  updateCheckButton();
}

function setCell(id, value) {
  document.getElementById(id).textContent = value;
}

function setCount(id, items) {
  document.getElementById(id).textContent = `(${items.length})`;
}

const UNIT_OPTIONS = [
  ["", "none"],
  ["mm", "mm"],
  ["in", "in"],
];

/** Units and the general tolerance: the two header fields the rules read. */
function renderHeader() {
  const working = state.working;
  const original = state.original;

  const unitsControl = selectControl(UNIT_OPTIONS, working.units || "");
  unitsControl.setAttribute("aria-label", "Units");
  editCell(unitsControl, {
    read: () => ({ ok: true, value: unitsControl.value || null }),
    apply: (value) => {
      working.units = value;
    },
    was: original.units || null,
    shown: text(original.units),
    cell: document.getElementById("v-units"),
  });

  // `general_tolerance` can come back missing altogether; the cell still needs somewhere
  // to write, and an object with nothing in it parses to the same thing.
  if (!working.general_tolerance) {
    working.general_tolerance = { linear_plus_minus: null, text_raw: null };
  }
  const originalTolerance = original.general_tolerance || {};
  numberCell(
    working.general_tolerance,
    "linear_plus_minus",
    originalTolerance.linear_plus_minus ?? null,
    { required: false, min: 0, cell: document.getElementById("v-gentol") }
  );

  setCell("v-gentol-raw", text(originalTolerance.text_raw));
}

function renderDatums() {
  const datums = state.working.datums || [];
  const originals = state.original.datums || [];

  const rows = datums.map((datum) => {
    // Matched by id, which the server assigned to the model's datums and `newDatumId`
    // assigns to added ones — so an added datum has no original and is always "edited".
    const was = byId(originals, datum.id);
    const control = textControl(datum.label || "", { width: 3 });
    control.classList.add("cell-label");
    control.setAttribute("aria-label", "Datum label");

    const labelCell = editCell(control, {
      read: () => {
        const value = control.value.trim().toUpperCase();
        return {
          ok: /^[A-Z]$/.test(value),
          value,
          message: "A datum label is a single letter, A to Z.",
        };
      },
      apply: (value) => {
        datum.label = value;
      },
      was: was ? was.label : null,
      shown: was ? text(was.label) : "nothing — you added this datum",
    });

    const removeCell = document.createElement("td");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn-row";
    remove.textContent = "Remove";
    remove.title = `Remove datum ${datum.label || ""}`.trim();
    remove.addEventListener("click", () => {
      state.working.datums = state.working.datums.filter((item) => item !== datum);
      // Everything, not just this table: the removed datum's box has to come off the
      // drawing too, and that is `buildOverlay`'s doing.
      renderTables();
      afterEdit();
    });
    removeCell.appendChild(remove);

    return { id: datum.id, cells: [labelCell, removeCell], marker: unsureMarker(datum) };
  });

  setCount("count-datums", datums);
  fillTable("rows-datums", rows, 2, "No datum features were read.");
}

function renderDimensions() {
  const dimensions = state.working.dimensions || [];
  const originals = state.original.dimensions || [];

  const rows = dimensions.map((dimension) => {
    const was = byId(originals, dimension.id) || {};

    const basic = document.createElement("input");
    basic.type = "checkbox";
    basic.className = "cell-check";
    basic.checked = Boolean(dimension.is_basic);
    basic.setAttribute("aria-label", "Basic dimension");

    return {
      id: dimension.id,
      marker: unsureMarker(dimension),
      cells: [
        textCell(text(dimension.text_raw), true),
        numberCell(dimension, "value", was.value ?? null, { required: true }),
        numberCell(dimension, "tol_plus", was.tol_plus ?? null, { required: false, min: 0 }),
        numberCell(dimension, "tol_minus", was.tol_minus ?? null, { required: false, min: 0 }),
        editCell(basic, {
          read: () => ({ ok: true, value: basic.checked }),
          apply: (value) => {
            dimension.is_basic = value;
          },
          was: Boolean(was.is_basic),
          shown: was.is_basic ? "yes" : "no",
          mono: false,
        }),
        textCell(fromTo(dimension), true),
      ],
    };
  });

  setCount("count-dimensions", dimensions);
  fillTable("rows-dimensions", rows, 6, "No dimensions were read.");
}

const MODIFIER_OPTIONS = [
  ["", "none"],
  ["MMC", "MMC"],
  ["LMC", "LMC"],
];

function renderFcfs() {
  const fcfs = state.working.fcfs || [];
  const originals = state.original.fcfs || [];

  const rows = fcfs.map((fcf) => {
    const was = byId(originals, fcf.id) || {};

    const modifier = selectControl(MODIFIER_OPTIONS, fcf.material_modifier || "");
    const refs = textControl(datumRefsText(fcf.datum_refs), { width: 12 });
    refs.setAttribute("aria-label", "Datum references");

    return {
      id: fcf.id,
      marker: unsureMarker(fcf),
      cells: [
        textCell(text(fcf.characteristic), false),
        numberCell(fcf, "tolerance", was.tolerance ?? null, { required: true, min: 0 }),
        editCell(modifier, {
          read: () => ({ ok: true, value: modifier.value || null }),
          apply: (value) => {
            fcf.material_modifier = value;
          },
          was: was.material_modifier || null,
          shown: text(was.material_modifier),
        }),
        editCell(refs, {
          read: () => {
            const parsed = parseDatumRefs(refs.value);
            return {
              ok: parsed !== null,
              value: parsed,
              message: "Datums separated by bars, e.g. A|B|C — one letter each.",
            };
          },
          apply: (value) => {
            fcf.datum_refs = value;
          },
          was: was.datum_refs || [],
          shown: datumRefsText(was.datum_refs) || EM_DASH,
          // Two lists are the same when they read the same in the cell.
          same: (a, b) => datumRefsText(a) === datumRefsText(b),
        }),
      ],
    };
  });

  setCount("count-fcfs", fcfs);
  fillTable("rows-fcfs", rows, 4, "No feature control frames were read.");
}

/** Everything the user can edit, built from `working` and compared against `original`. */
function renderTables() {
  // Every control on screen is about to be replaced, so no complaint about one of them
  // outlives this rebuild; the new cells re-register whatever is still wrong.
  invalidInputs.clear();
  // Refilled by `fillTable` as the three tables are built below, in their order.
  unsureIds = [];

  // The boxes go up first: a row asks `linked` whether its item has one.
  buildOverlay([
    ...(state.working.datums || []).map((datum) => ({
      id: datum.id,
      bbox: datum.bbox,
      title: `Datum ${text(datum.label)}`,
      unsure: isUnsure(datum),
    })),
    ...(state.working.dimensions || []).map((dimension) => ({
      id: dimension.id,
      bbox: dimension.bbox,
      title: `Dimension ${text(dimension.text_raw)}`,
      unsure: isUnsure(dimension),
    })),
    ...(state.working.fcfs || []).map((fcf) => ({
      id: fcf.id,
      bbox: fcf.bbox,
      title: `Frame ${text(fcf.characteristic)}`,
      unsure: isUnsure(fcf),
    })),
  ]);
  overlay.hidden = !overlayToggle.checked;

  renderHeader();
  renderDatums();
  renderDimensions();
  renderFcfs();
  renderConfidenceSummary();
}

/* ---- Findings ------------------------------------------------------------ */

/** "14:32:07" — padded and 24-hour, so the line never changes width. */
function clockTime(when) {
  const pad = (number) => String(number).padStart(2, "0");
  return `${pad(when.getHours())}:${pad(when.getMinutes())}:${pad(when.getSeconds())}`;
}

/**
 * The line under the panel title: when the check ran, and whether it still holds.
 *
 * Both states are one line in the same place. An earlier version put the stale warning in
 * a box of its own above the findings, which meant a check that landed deleted a 114-pixel
 * block and shunted everything under it upwards — the "flicker" this replaced.
 */
function setCheckStatus(stale) {
  if (!state.checkedAt) {
    checkStatus.hidden = true;
    return;
  }
  const at = `Checked at ${clockTime(state.checkedAt)}`;
  checkStatus.textContent = stale ? `${at} — edited since; check again` : at;
  checkStatus.classList.toggle("is-stale", stale);
  checkStatus.hidden = false;
}

/**
 * Called after every accepted edit.
 *
 * Findings are only true of the data they were computed from, so the first edit after a
 * check says so rather than leaving a stale verdict sitting there looking current.
 */
function afterEdit() {
  if (state.checked) {
    state.stale = true;
    setCheckStatus(true);
  }
  // The agent worked from the data the check ran on, so an edit dates its rounds and its
  // redline exactly as it dates the findings.
  setAgentStatus(true);
  updateFixButton();
}

function clearFindings() {
  state.checked = false;
  state.checkedAt = null;
  state.stale = false;
  state.errorCount = 0;
  findingsPanel.hidden = true;
  findingsPanel.classList.remove("is-fresh");
  checkStatus.hidden = true;
  findingsList.replaceChildren();
  findingsNone.hidden = true;
  checkIssuesBox.hidden = true;
  checkErrorBox.hidden = true;
  clearAgent();
}

/**
 * Tint the panel for half a second, so an update that changes nothing visible is still
 * visibly an update.
 *
 * The class has to come off and the reflow has to be forced, or a second check in the
 * same second would re-add a class that is already there and play no animation at all.
 */
function flashPanel() {
  findingsPanel.classList.remove("is-fresh");
  void findingsPanel.offsetWidth;
  findingsPanel.classList.add("is-fresh");
}

/** One finding: its rule, its severity, its message, and the items it is about. */
function findingItem(finding) {
  const item = document.createElement("li");
  item.className = `finding sev-${finding.severity === "error" ? "error" : "warning"}`;
  item.tabIndex = 0;

  const head = document.createElement("p");
  head.className = "finding-head";

  const severity = document.createElement("span");
  severity.className = "sev-tag";
  severity.textContent = finding.severity;
  head.appendChild(severity);

  const rule = document.createElement("code");
  rule.className = "rule-id";
  rule.textContent = finding.rule_id;
  head.appendChild(rule);

  const message = document.createElement("p");
  message.className = "finding-message";
  message.textContent = finding.message;

  item.append(head, message);

  const ids = Array.isArray(finding.item_ids) ? finding.item_ids : [];
  // Light every item the finding names, and scroll to the first one we can actually
  // point at — its box if it has one, otherwise its row.
  function show() {
    pick(ids, null);
    for (const id of ids) {
      const pair = linked.get(id);
      const target = (pair && (pair.box || pair.row)) || null;
      if (target) {
        target.scrollIntoView({ block: "nearest", behavior: "smooth" });
        break;
      }
    }
  }
  item.addEventListener("click", show);
  item.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      show();
    }
  });

  return item;
}

/**
 * Replace the findings with a new set, in one update.
 *
 * Both lists are built in a fragment and swapped in with a single `replaceChildren`, so
 * the panel goes straight from the old findings to the new ones. Emptying a list and
 * refilling it would reach the same place, but it leaves a moment where the panel holds
 * nothing — and on a slow enough machine that moment is a frame the user sees.
 */
function renderFindings(result) {
  const findings = result.findings || [];
  const issues = result.parse_issues || [];

  const newFindings = document.createDocumentFragment();
  for (const finding of findings) {
    newFindings.appendChild(findingItem(finding));
  }

  const newIssues = document.createDocumentFragment();
  for (const issue of issues) {
    const line = document.createElement("li");
    line.textContent = issue;
    newIssues.appendChild(line);
  }

  findingsList.replaceChildren(newFindings);
  checkIssuesList.replaceChildren(newIssues);
  findingsNone.hidden = findings.length > 0;
  checkIssuesBox.hidden = issues.length === 0;
  checkErrorBox.hidden = true;

  state.checked = true;
  state.checkedAt = new Date();
  state.stale = false;
  state.errorCount = result.error_count || 0;
  setCheckStatus(false);
  findingsPanel.hidden = false;
  flashPanel();
  updateFixButton();
}

/* ---- Proposing fixes ----------------------------------------------------- */

/*
 * The agent is offered only when there is something for it to work on and the findings
 * it would work from are current: a fix proposed against data the user has since edited
 * would be answering a question nobody asked any more. The button says nothing about why
 * it is dead, so `#fix-hint` says it instead.
 */

/** Why the agent cannot run right now, or "" when it can. */
function fixBlockedBecause() {
  if (!state.checked) {
    return "Check the drawing first — the agent works from the findings.";
  }
  if (state.stale) {
    return "The tables have changed since the last check. Check the drawing again first.";
  }
  if (state.errorCount === 0) {
    return "Nothing to propose: the last check found no errors.";
  }
  return "";
}

function updateFixButton() {
  const reason = fixBlockedBecause();
  proposeButton.disabled = reason !== "" || busy || fixing || invalidInputs.size > 0;
  fixHint.textContent = reason;
  fixHint.hidden = reason === "";
}

/** The agent's line: when it ran, and whether the tables have moved on since. */
function setAgentStatus(stale) {
  if (!state.fixedAt) {
    agentStatus.hidden = true;
    return;
  }
  const at = `Fixes proposed at ${clockTime(state.fixedAt)}`;
  agentStatus.textContent = stale ? `${at} — edited since; check and fix again` : at;
  agentStatus.classList.toggle("is-stale", stale);
  agentStatus.hidden = false;
}

function clearAgent() {
  state.fixedAt = null;
  agentPanel.hidden = true;
  agentStatus.hidden = true;
  agentErrorBox.hidden = true;
  agentRounds.replaceChildren();
  agentVerdicts.replaceChildren();
  redlineImage.removeAttribute("src");
}

/** A paragraph with a class. The page builds a lot of these. */
function paragraph(className, value) {
  const line = document.createElement("p");
  line.className = className;
  line.textContent = value;
  return line;
}

/**
 * One proposed edit: what it would do, whether it stuck, and the reasoning either side.
 *
 * `applied` is the rule engine's word, not the model's — a rejected edit is one
 * `edits.apply_edits` refused, and `reason` is the refusal it wrote.
 */
function editItem(edit, applied) {
  const item = document.createElement("li");
  item.className = applied ? "edit is-applied" : "edit is-rejected";

  const head = document.createElement("p");
  head.className = "edit-head";

  const tag = document.createElement("span");
  tag.className = "edit-tag";
  tag.textContent = applied ? "applied" : "rejected";
  head.appendChild(tag);

  const sentence = document.createElement("span");
  sentence.className = "edit-text";
  sentence.textContent = edit.edit;
  head.appendChild(sentence);

  item.appendChild(head);
  if (edit.rationale) {
    item.appendChild(paragraph("edit-why", `Why: ${edit.rationale}`));
  }
  if (!applied && edit.reason) {
    item.appendChild(paragraph("edit-reason", `Rejected: ${edit.reason}`));
  }
  return item;
}

/** "Cleared" or "Introduced", with one line per finding, or nothing when the list is empty. */
function changedFindings(label, findings, className) {
  if (!findings || findings.length === 0) {
    return null;
  }
  const block = document.createElement("div");
  block.className = `changed ${className}`;
  block.appendChild(paragraph("changed-label", label));

  const list = document.createElement("ul");
  list.className = "changed-list";
  for (const finding of findings) {
    const line = document.createElement("li");
    line.textContent = finding.message || finding.key;
    list.appendChild(line);
  }
  block.appendChild(list);
  return block;
}

/** One round: the edits it proposed, and what the rules said about the result. */
function roundCard(round) {
  const item = document.createElement("li");
  item.className = "round";

  const head = document.createElement("p");
  head.className = "round-head";

  const number = document.createElement("span");
  number.className = "round-number";
  number.textContent = `Round ${round.round}`;
  head.appendChild(number);

  const counts = document.createElement("span");
  counts.className = "round-counts";
  counts.textContent = `Findings: ${round.findings_before} before → ${round.findings_after} after`;
  head.appendChild(counts);

  item.appendChild(head);

  if (round.error) {
    item.appendChild(paragraph("round-error", `This round could not run: ${round.error}`));
  }

  const applied = round.applied || [];
  const rejected = round.rejected || [];
  if (applied.length === 0 && rejected.length === 0) {
    item.appendChild(paragraph("round-empty", "The model proposed no edits in this round."));
  } else {
    const edits = document.createElement("ul");
    edits.className = "edits";
    for (const edit of applied) {
      edits.appendChild(editItem(edit, true));
    }
    for (const edit of rejected) {
      edits.appendChild(editItem(edit, false));
    }
    item.appendChild(edits);
  }

  const cleared = changedFindings("Cleared:", round.resolved, "is-cleared");
  const introduced = changedFindings("Introduced:", round.introduced, "is-introduced");
  if (cleared) {
    item.appendChild(cleared);
  }
  if (introduced) {
    item.appendChild(introduced);
  }
  if (!cleared && !introduced && !round.error) {
    item.appendChild(paragraph("round-empty", "No finding changed in this round."));
  }

  return item;
}

/** One original finding and what became of it. The verdict came from re-running the rules. */
function verdictItem(entry) {
  const item = document.createElement("li");
  const resolved = entry.status === "resolved";
  item.className = resolved ? "verdict is-resolved" : "verdict is-unresolved";

  const tag = document.createElement("span");
  tag.className = "verdict-tag";
  tag.textContent = resolved ? "verified resolved" : "unresolved";

  const key = document.createElement("code");
  key.className = "rule-id";
  key.textContent = entry.finding;

  const message = document.createElement("span");
  message.className = "verdict-message";
  message.textContent = entry.message;

  item.append(tag, key, message);
  return item;
}

/**
 * Show one correction run: the rounds, the verdicts, and the redline it produced.
 *
 * The redline URLs are fixed per run and the files are rewritten on every fix, so both
 * carry a timestamp — without it the browser would show the previous run's redline from
 * its cache and nothing on screen would say so.
 */
function renderAgent(result) {
  const rounds = result.rounds || [];
  const statuses = result.status || [];

  const newRounds = document.createDocumentFragment();
  for (const round of rounds) {
    newRounds.appendChild(roundCard(round));
  }
  if (rounds.length === 0) {
    const item = document.createElement("li");
    item.className = "round";
    item.appendChild(paragraph("round-empty", "The agent was not asked for any edits."));
    newRounds.appendChild(item);
  }

  const newVerdicts = document.createDocumentFragment();
  for (const entry of statuses) {
    newVerdicts.appendChild(verdictItem(entry));
  }

  agentRounds.replaceChildren(newRounds);
  agentVerdicts.replaceChildren(newVerdicts);
  agentErrorBox.hidden = true;

  const stamp = Date.now();
  redlineImage.src = `${result.redline_png_url}?t=${stamp}`;
  redlinePdf.href = `${result.redline_pdf_url}?t=${stamp}`;

  state.fixedAt = new Date();
  setAgentStatus(false);
  agentPanel.hidden = false;
}

/* ---- Rendering one run --------------------------------------------------- */

function render(run) {
  const drawing = run.drawing || {};
  const issues = run.parse_issues || [];

  state.runId = run.run_id;
  state.original = clone(drawing);
  state.working = clone(drawing);
  clearFindings();

  setCell("meta-model", text(run.model));
  setCell("meta-latency", seconds(run.latency_s));
  setCell("meta-attempts", text(run.attempts));

  parseIssuesList.replaceChildren();
  for (const issue of issues) {
    const item = document.createElement("li");
    item.textContent = issue;
    parseIssuesList.appendChild(item);
  }
  parseIssuesBox.hidden = issues.length === 0;

  pageImage.src = run.page_url;

  renderTables();
  updateCheckButton();
  resultSection.hidden = false;
}

/* ---- The requests -------------------------------------------------------- */

/**
 * The message to show for a failed response.
 *
 * FastAPI puts its own text in `detail`, which is a string for the errors this app
 * raises and a list of objects for a request its schema rejected. Both are handled,
 * and a reply that is not JSON at all falls back to the status line.
 */
async function errorMessage(response) {
  let payload;
  try {
    payload = await response.json();
  } catch {
    return `The server returned ${response.status} ${response.statusText}.`;
  }

  const detail = payload && payload.detail;
  if (typeof detail === "string" && detail) {
    return detail;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    return detail.map((item) => item.msg || JSON.stringify(item)).join("\n");
  }
  return `The server returned ${response.status} ${response.statusText}.`;
}

/**
 * Send one file and show what comes back.
 *
 * The one path for both ways in: dropping a file, and the Read drawing button. A file
 * that fails `rejectionReason` never gets here, so nothing unsendable reaches the wire.
 */
async function startRun(file) {
  clearOutput();
  setDropLabel(file.name);
  setBusy(true);

  const body = new FormData();
  body.append("file", file);
  body.append("provider", providerSelect.value);

  try {
    const response = await fetch("/api/runs", { method: "POST", body });
    if (!response.ok) {
      showError(await errorMessage(response));
      return;
    }
    render(await response.json());
  } catch (problem) {
    // A fetch only rejects when the request never completed — the server is down, or
    // the connection dropped. An HTTP error status is handled above.
    showError(`Could not reach the server: ${problem.message}`);
  } finally {
    setBusy(false);
    setDropLabel(null);
    updateCheckButton();
  }
}

/**
 * How long a check may take before it is worth telling the user it is running.
 *
 * Under this, the answer arrives before anyone could read a progress label, so the button
 * says nothing and simply goes dead for an instant.
 */
const SLOW_CHECK_MS = 300;

/**
 * Send the edited drawing to the rules and show what they say.
 *
 * `working` is the body: the extraction with the user's edits written into it, every
 * other field untouched. What passes is decided server side, from this JSON.
 */
async function runCheck() {
  if (!state.runId || invalidInputs.size > 0) {
    return;
  }
  // Nothing else on screen changes while the request is out. The panel keeps the previous
  // findings, the error box keeps whatever it was showing, and the status line still says
  // when that answer was computed — all of it still true until a new answer arrives to
  // replace it in one go.
  //
  // Disabling is immediate, because its whole job is to swallow a second click.
  checkButton.disabled = true;

  // The label is not. A check against a local server comes back in about 100 ms, and a
  // word that appears and vanishes inside a fifth of a second reads as a glitch rather
  // than as progress. So "Checking…" is only promised, and only kept if the wait turns
  // out to be long enough to be worth reporting.
  let labelChanged = false;
  const labelTimer = setTimeout(() => {
    labelChanged = true;
    checkButton.textContent = "Checking…";
  }, SLOW_CHECK_MS);

  /** Keep the findings, put the reason above them. */
  function failed(message) {
    checkErrorText.textContent = message;
    checkErrorBox.hidden = false;
    findingsPanel.hidden = false;
  }

  try {
    const response = await fetch(`/api/runs/${state.runId}/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.working),
    });
    if (!response.ok) {
      failed(await errorMessage(response));
      return;
    }
    renderFindings(await response.json());
  } catch (problem) {
    failed(`Could not reach the server: ${problem.message}`);
  } finally {
    // Either the timer has not fired, and must not fire now, or it has and the label
    // needs putting back. Restored only when it actually changed, so a fast check leaves
    // the button untouched from first click to last.
    clearTimeout(labelTimer);
    if (labelChanged) {
      checkButton.textContent = "Check drawing";
    }
    updateCheckButton();
  }
}

checkButton.addEventListener("click", runCheck);

/**
 * Ask the agent for corrections, and show the rounds and the redline it produced.
 *
 * The loop is three model calls at worst and takes 20–60 seconds, so unlike Check this one
 * always ends up saying it is working. It still goes through the same 300 ms timer: the
 * request could fail at the server before the model is ever called, and a spinner that
 * appears for a tenth of a second on the way to an error message is noise.
 *
 * Nothing is posted. The server works from `confirmed.json` — the drawing the last check
 * saved — which is why the button is dead whenever the tables have moved on from it.
 */
async function runFix() {
  if (!state.runId || fixBlockedBecause() !== "") {
    return;
  }
  fixing = true;
  updateCheckButton();

  let labelChanged = false;
  const labelTimer = setTimeout(() => {
    labelChanged = true;
    proposeButton.textContent = "Proposing…";
    fixStatus.hidden = false;
  }, SLOW_CHECK_MS);

  /** Keep whatever the panel is showing, and put the reason above it. */
  function failed(message) {
    agentErrorText.textContent = message;
    agentErrorBox.hidden = false;
    agentPanel.hidden = false;
  }

  try {
    const response = await fetch(`/api/runs/${state.runId}/fix`, { method: "POST" });
    if (!response.ok) {
      failed(await errorMessage(response));
      return;
    }
    renderAgent(await response.json());
  } catch (problem) {
    failed(`Could not reach the server: ${problem.message}`);
  } finally {
    clearTimeout(labelTimer);
    if (labelChanged) {
      proposeButton.textContent = "Propose fixes";
      fixStatus.hidden = true;
    }
    fixing = false;
    updateCheckButton();
  }
}

proposeButton.addEventListener("click", runFix);

resetButton.addEventListener("click", () => {
  if (!state.original) {
    return;
  }
  state.working = clone(state.original);
  renderTables();
  afterEdit();
});

addDatumButton.addEventListener("click", () => {
  if (!state.working) {
    return;
  }
  // No label and no place on the drawing yet: the label cell opens invalid, which is the
  // honest state — a datum without a letter is not something the rules can use.
  state.working.datums = state.working.datums || [];
  state.working.datums.push({ id: newDatumId(), label: "", location: null, bbox: null });
  renderTables();
  afterEdit();
});

/** Check a file and either start reading it or say why not. No request for a bad file. */
function offerFile(file) {
  const reason = rejectionReason(file);
  if (reason) {
    clearOutput();
    showError(reason);
    return;
  }
  startRun(file);
}

/* ---- Ways in: drop, click, keyboard, and the button ---------------------- */

dropzone.addEventListener("click", (event) => {
  // The hidden input lives inside the zone, so its own click would bounce back here and
  // reopen the dialog forever. Only a click on the zone itself opens it.
  if (busy || event.target === fileInput) {
    return;
  }
  fileInput.click();
});

dropzone.addEventListener("keydown", (event) => {
  if (busy || (event.key !== "Enter" && event.key !== " ")) {
    return;
  }
  event.preventDefault(); // Space would scroll the page.
  fileInput.click();
});

// Picking a file from the dialog reads it straight away, exactly as a drop does.
fileInput.addEventListener("change", () => {
  const file = fileInput.files && fileInput.files[0];
  if (file) {
    offerFile(file);
  }
});

/* The browser opens a dropped file in the tab unless both dragover and drop call
   preventDefault — dragover says "this element accepts the drop", drop keeps the
   navigation from happening. */
dropzone.addEventListener("dragover", (event) => {
  if (busy) {
    return;
  }
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  dropzone.classList.add("is-dragging");
});

// dragleave fires when crossing onto a child element too; `relatedTarget` outside the
// zone is what actually means the pointer left it.
dropzone.addEventListener("dragleave", (event) => {
  if (!dropzone.contains(event.relatedTarget)) {
    dropzone.classList.remove("is-dragging");
  }
});

dropzone.addEventListener("drop", (event) => {
  if (busy) {
    return;
  }
  event.preventDefault();
  dropzone.classList.remove("is-dragging");

  const file = event.dataTransfer.files && event.dataTransfer.files[0];
  if (!file) {
    clearOutput();
    showError("That drop carried no file. Drag a PDF, PNG, or JPG drawing onto the box.");
    return;
  }

  // Hand the drop to the file input as well, so the form holds the same file the drop
  // started and the Read drawing button can run it again. Assigning `files` fires no
  // change event, so this does not start a second run.
  fileInput.files = event.dataTransfer.files;
  offerFile(file);
});

/* ---- The demo run -------------------------------------------------------- */

/*
 * `?demo=low-confidence` loads a saved extraction with two items marked unsure, so the
 * markings above can be looked at without spending a model call on it. The server only
 * answers this for a browser on the same machine, so a deployed DraftCheck has no demo.
 */

const DEMO_PARAM = "demo";
const LOW_CONFIDENCE_DEMO = "low-confidence";

/** Ask for the demo run and show it, exactly as an upload's reply is shown. */
async function loadDemo() {
  clearOutput();
  setBusy(true);
  try {
    const response = await fetch(`/api/demo/${LOW_CONFIDENCE_DEMO}`, { method: "POST" });
    if (!response.ok) {
      showError(await errorMessage(response));
      return;
    }
    render(await response.json());
  } catch (problem) {
    showError(`Could not reach the server: ${problem.message}`);
  } finally {
    setBusy(false);
    setDropLabel(null);
    updateCheckButton();
  }
}

if (new URLSearchParams(window.location.search).get(DEMO_PARAM) === LOW_CONFIDENCE_DEMO) {
  loadDemo();
}

form.addEventListener("submit", (event) => {
  event.preventDefault();

  const file = fileInput.files && fileInput.files[0];
  if (!file) {
    clearOutput();
    showError("Drop a PDF, PNG, or JPG drawing on the box, or click it to choose one.");
    return;
  }
  offerFile(file);
});
