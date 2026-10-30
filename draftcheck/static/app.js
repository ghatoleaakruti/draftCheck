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

const findingsPanel = document.getElementById("findings-panel");
const findingsList = document.getElementById("findings-list");
const findingsNone = document.getElementById("findings-none");
const checkStatus = document.getElementById("check-status");
const checkErrorBox = document.getElementById("check-error");
const checkErrorText = document.getElementById("check-error-text");
const checkIssuesBox = document.getElementById("check-parse-issues");
const checkIssuesList = document.getElementById("check-parse-issues-list");

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

function updateCheckButton() {
  const blocked = invalidInputs.size > 0;
  checkButton.disabled = blocked || busy;
  invalidNote.hidden = !blocked;
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

/* ---- Building the tables ------------------------------------------------- */

/**
 * Replace a tbody's rows.
 *
 * `rows` is a list of `{ id, cells }`, where each cell is a `<td>` and `id` ties the row
 * to its box on the drawing. An empty list leaves one spanning row saying so, rather than
 * a table with a header and nothing under it — "the model found none" and "the page has
 * not loaded" should not look alike.
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

  for (const { id, cells } of rows) {
    const row = document.createElement("tr");
    for (const cell of cells) {
      row.appendChild(cell);
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

    return { id: datum.id, cells: [labelCell, removeCell] };
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

  // The boxes go up first: a row asks `linked` whether its item has one.
  buildOverlay([
    ...(state.working.datums || []).map((datum) => ({
      id: datum.id,
      bbox: datum.bbox,
      title: `Datum ${text(datum.label)}`,
    })),
    ...(state.working.dimensions || []).map((dimension) => ({
      id: dimension.id,
      bbox: dimension.bbox,
      title: `Dimension ${text(dimension.text_raw)}`,
    })),
    ...(state.working.fcfs || []).map((fcf) => ({
      id: fcf.id,
      bbox: fcf.bbox,
      title: `Frame ${text(fcf.characteristic)}`,
    })),
  ]);
  overlay.hidden = !overlayToggle.checked;

  renderHeader();
  renderDatums();
  renderDimensions();
  renderFcfs();
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
    setCheckStatus(true);
  }
}

function clearFindings() {
  state.checked = false;
  state.checkedAt = null;
  findingsPanel.hidden = true;
  findingsPanel.classList.remove("is-fresh");
  checkStatus.hidden = true;
  findingsList.replaceChildren();
  findingsNone.hidden = true;
  checkIssuesBox.hidden = true;
  checkErrorBox.hidden = true;
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
  setCheckStatus(false);
  findingsPanel.hidden = false;
  flashPanel();
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
