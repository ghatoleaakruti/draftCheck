/* DraftCheck — upload a drawing and view what the model read.
 *
 * One request: POST /api/runs with the file and the chosen provider. The reply carries
 * the run id, the extracted drawing, and the model/latency metadata; the rendered page
 * comes back separately from GET /api/runs/{id}/page.png.
 *
 * This file only displays. Nothing here decides whether the drawing passes — the rules
 * run server side, in a later step of the flow.
 *
 * Every cell is written with textContent, never innerHTML: the strings below come from
 * a language model reading an uploaded file, so they are treated as text, not markup.
 */

"use strict";

const EM_DASH = "—";

const form = document.getElementById("upload-form");
const fileInput = document.getElementById("file");
const providerSelect = document.getElementById("provider");
const submitButton = document.getElementById("submit");

const statusLine = document.getElementById("status");
const errorBox = document.getElementById("error");
const errorText = document.getElementById("error-text");
const resultSection = document.getElementById("result");

const parseIssuesBox = document.getElementById("parse-issues");
const parseIssuesList = document.getElementById("parse-issues-list");
const pageImage = document.getElementById("page-image");

/** Show one of the three states; the others are hidden. */
function setBusy(busy) {
  submitButton.disabled = busy;
  submitButton.textContent = busy ? "Reading…" : "Read drawing";
  statusLine.hidden = !busy;
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

/** "28.4 s". One decimal: this is how long the model took, not a measurement. */
function seconds(value) {
  if (typeof value !== "number" || !isFinite(value)) {
    return EM_DASH;
  }
  return `${value.toFixed(1)} s`;
}

/** "12.5 mm", falling back to the drawing's units when the dimension has none. */
function valueWithUnit(dimension, drawingUnits) {
  const unit = dimension.unit || drawingUnits;
  const shown = num(dimension.value);
  if (shown === EM_DASH) {
    return EM_DASH;
  }
  return unit ? `${shown} ${unit}` : shown;
}

/** "±0.1", "+0.2 / -0.1", or an em dash when neither limit was read. */
function tolerance(plus, minus) {
  const hasPlus = typeof plus === "number";
  const hasMinus = typeof minus === "number";
  if (!hasPlus && !hasMinus) {
    return EM_DASH;
  }
  if (hasPlus && hasMinus && plus === minus) {
    return `±${num(plus)}`;
  }
  const up = hasPlus ? `+${num(plus)}` : "+?";
  const down = hasMinus ? `-${num(minus)}` : "-?";
  return `${up} / ${down}`;
}

/** "left_edge → hole_1_center", or an em dash when the pair is incomplete. */
function fromTo(dimension) {
  if (!dimension.from_ref && !dimension.to_ref) {
    return EM_DASH;
  }
  return `${text(dimension.from_ref)} → ${text(dimension.to_ref)}`;
}

/** "A | B(MMC) | C", or an em dash for a frame with no datum references. */
function datumRefs(refs) {
  if (!Array.isArray(refs) || refs.length === 0) {
    return EM_DASH;
  }
  return refs
    .map((ref) => (ref.modifier ? `${text(ref.label)}(${ref.modifier})` : text(ref.label)))
    .join(" | ");
}

/* ---- Table building ------------------------------------------------------ */

/**
 * Replace a tbody's rows.
 *
 * `rows` is a list of cell lists; each cell is [value, monospaced]. An empty list
 * leaves one spanning row saying so, rather than a table with a header and nothing
 * under it — "the model found none" and "the page has not loaded" should not look alike.
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

  for (const cells of rows) {
    const row = document.createElement("tr");
    for (const [value, monospaced] of cells) {
      const cell = document.createElement("td");
      if (monospaced) {
        cell.className = "mono";
      }
      cell.textContent = value;
      row.appendChild(cell);
    }
    tbody.appendChild(row);
  }
}

function setCell(id, value) {
  document.getElementById(id).textContent = value;
}

function setCount(id, items) {
  document.getElementById(id).textContent = `(${items.length})`;
}

/* ---- Rendering one run --------------------------------------------------- */

function render(run) {
  const drawing = run.drawing || {};
  const datums = drawing.datums || [];
  const dimensions = drawing.dimensions || [];
  const fcfs = drawing.fcfs || [];
  const generalTolerance = drawing.general_tolerance || {};
  const issues = run.parse_issues || [];

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

  setCell("v-units", text(drawing.units));
  setCell(
    "v-gentol",
    typeof generalTolerance.linear_plus_minus === "number"
      ? num(generalTolerance.linear_plus_minus)
      : EM_DASH
  );
  setCell("v-gentol-raw", text(generalTolerance.text_raw));

  setCount("count-datums", datums);
  setCount("count-dimensions", dimensions);
  setCount("count-fcfs", fcfs);

  fillTable(
    "rows-datums",
    datums.map((datum) => [[text(datum.label), true]]),
    1,
    "No datum features were read."
  );

  fillTable(
    "rows-dimensions",
    dimensions.map((dimension) => [
      [text(dimension.text_raw), true],
      [valueWithUnit(dimension, drawing.units), true],
      [dimension.is_basic ? EM_DASH : tolerance(dimension.tol_plus, dimension.tol_minus), true],
      [dimension.is_basic ? "yes" : "no", false],
      [fromTo(dimension), true],
    ]),
    5,
    "No dimensions were read."
  );

  fillTable(
    "rows-fcfs",
    fcfs.map((fcf) => [
      [text(fcf.characteristic), false],
      [fcf.diameter_zone ? `ø${num(fcf.tolerance)}` : num(fcf.tolerance), true],
      [text(fcf.material_modifier), true],
      [datumRefs(fcf.datum_refs), true],
    ]),
    4,
    "No feature control frames were read."
  );

  resultSection.hidden = false;
}

/* ---- The request --------------------------------------------------------- */

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

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const file = fileInput.files && fileInput.files[0];
  if (!file) {
    clearOutput();
    showError("Choose a PDF, PNG, or JPG drawing first.");
    return;
  }

  clearOutput();
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
  }
});
