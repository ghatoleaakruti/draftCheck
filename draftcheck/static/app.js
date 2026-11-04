/* DraftCheck — the checker's desk.
 *
 * Four requests, the same ones the page has always made:
 *   POST /api/runs               the file and the chosen provider -> run id, extraction, model
 *   GET  /api/runs/{id}/page.png the rendered page, shown as paper on the desk
 *   POST /api/runs/{id}/check    the drawing the user confirmed -> findings
 *   POST /api/runs/{id}/fix      run the correction loop -> rounds, verdicts, redline URLs
 * plus GET /samples/plate_001.pdf, which "Try the sample drawing" fetches and then uploads
 * through POST /api/runs exactly like a picked file.
 *
 * Nothing here decides whether the drawing passes. The tables are an editor and a
 * display; the rules run server side, on exactly the JSON this file posts.
 *
 * The screen is one state machine. `currentStage()` reads the run's real state — is a
 * request out, has the check run, have the tables moved on since — and `updateUI()`
 * paints the stepper, the model pill, the status line, and the one primary button from
 * it. Nothing sets those directly, so they cannot disagree with each other.
 *
 * Two copies of the drawing are kept. `original` is what the model read and is never
 * touched — it is what the "edited" marks compare against and what Reset restores.
 * `working` starts as a copy of it, the inputs write into it, and it is the request body.
 *
 * Every string from the server is written with textContent or as an input `value`, never
 * innerHTML: it comes from a language model reading an uploaded file, so it is text.
 */

"use strict";

const EM_DASH = "—";
const SVG_NS = "http://www.w3.org/2000/svg";

const $ = (id) => document.getElementById(id);

const fileInput = $("file");
const providerSelect = $("provider");
const uploadButton = $("upload-button");
const sampleButtons = Array.from(document.querySelectorAll(".js-sample"));

const desk = $("desk");
const stageEl = $("stage");
const emptyState = $("empty");
const dropzone = $("dropzone");
const dropMain = $("drop-main");
const paper = $("paper");
const pageImage = $("page-image");
const overlay = $("overlay");
const boxLayer = $("boxes");
const overlayToggle = $("show-overlay");

const stepper = $("stepper");
const modelPill = $("model-pill");
const modelPillText = $("model-pill-text");
const fileName = $("file-name");
const fileSub = $("file-sub");
const phase = $("phase");

const panelBody = $("panel-body");
const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
const issueCount = $("issue-count");

const extractedEmpty = $("extracted-empty");
const extracted = $("extracted");
const parseIssuesBox = $("parse-issues");
const parseIssuesList = $("parse-issues-list");
const metaFallback = $("meta-fallback");
const confidenceSummary = $("confidence-summary");
const resetButton = $("reset");
const addDatumButton = $("add-datum");

const issuesEmpty = $("issues-empty");
const findingsList = $("findings-list");
const findingsNone = $("findings-none");
const checkStatus = $("check-status");
const checkIssuesBox = $("check-parse-issues");
const checkIssuesList = $("check-parse-issues-list");

const correctionsEmpty = $("corrections-empty");
const agentPanel = $("agent-panel");
const agentStatus = $("agent-status");
const agentRounds = $("agent-rounds");
const agentVerdicts = $("agent-verdicts");
const redlineImage = $("redline-image");
const redlinePdf = $("redline-pdf");

const actionHint = $("action-hint");
const primaryButton = $("primary");
const cancelButton = $("cancel");
const toasts = $("toasts");
const fakeBadge = $("fake-badge");
const actionBar = document.querySelector(".actionbar");
const statusBar = document.querySelector(".statusbar");

/* ---- Constants ----------------------------------------------------------- */

/** What the server will open, checked here so an obvious mistake costs no request. */
const ALLOWED_SUFFIXES = [".pdf", ".png", ".jpg", ".jpeg"];
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

/** The sample drawing, served by the API as one static file. */
const SAMPLE_URL = "/samples/plate_001.pdf";
const SAMPLE_NAME = "plate_001.pdf";

/** The correction loop's hard cap (draftcheck/agent.py MAX_ROUNDS). Only used in words. */
const MAX_ROUNDS = 3;

/** Short names for the provider options, for the model pill. */
const PROVIDER_LABELS = {
  baseten: "Muse Spark Open",
  meta: "Muse Spark",
};

/**
 * Said beside the model name when the server's fallback stepped in: Muse Spark Open on
 * Baseten did not answer, so the same request went to Muse Spark on the Meta Model API.
 */
const FALLBACK_NOTE =
  "Muse Spark Open didn't respond in time, so Muse Spark (Meta Model API) read this drawing.";

// Closing the connection stops our server, but the provider keeps working on the
// abandoned request and the next one waits behind it (docs/ARCHITECTURE.md §4.13).
const CANCELLED_MESSAGE =
  "Cancelled. Muse Spark may still be finishing the previous request, so your next read can take up to a minute.";

/**
 * How long a check may take before it is worth telling the user it is running. Under
 * this, a "Checking…" label would appear and vanish too fast to read.
 */
const SLOW_CHECK_MS = 300;

/* ---- State --------------------------------------------------------------- */

const state = {
  runId: null,
  original: null,
  working: null,
  /** Run metadata from the upload reply: model, latency, attempts, fell_back. */
  run: null,
  /** Which provider the run was read with, for the pill while fixing. */
  provider: null,
  /** The file on the desk, and where it came from: "uploaded", "sample", or "demo". */
  file: null,
  /** The file being read right now, shown in place of `file` until the read lands. */
  pendingFile: null,
  /** True once a check has returned, so a later edit can mark its findings out of date. */
  checked: false,
  checkedAt: null,
  /** True once the tables have moved on from the last check. */
  stale: false,
  errorCount: 0,
  warningCount: 0,
  /** When the correction loop last returned, or null. */
  fixedAt: null,
  /** True once the tables have moved on from the last fix. */
  agentStale: false,
  /** The last fix's numbers, as the server reported them. */
  fix: null,
  /** True once the redline has been downloaded from the primary button. */
  exported: false,
  /** Whether the page image has loaded, and its size in image pixels. */
  imageReady: false,
  imageW: 0,
  imageH: 0,
  /** The pill's last "answered" text, restored when a busy spell ends. */
  answered: null,
  /** True when the server answered with the development fake model (DRAFTCHECK_FAKE_MODEL). */
  fake: false,
};

let reading = false;
let checking = false;
let checkLabelShown = false;
let fixing = false;

/** The controller for the request Cancel would stop, or `null` when none is out. */
let readController = null;
let fixController = null;

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
 * The same, but as something to put in an input's `value`. Unrounded, so a box nobody
 * touched reads back as exactly the number the model gave.
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

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

/** "left_edge → hole_1_center", or an em dash when the pair is incomplete. */
function fromTo(dimension) {
  if (!dimension.from_ref && !dimension.to_ref) {
    return EM_DASH;
  }
  return `${text(dimension.from_ref)} → ${text(dimension.to_ref)}`;
}

/**
 * Datum references as one editable string: "A|B(MMC)|C". One compartment per datum, most
 * important first. `parseDatumRefs` is the exact opposite of this.
 */
function datumRefsText(refs) {
  if (!Array.isArray(refs) || refs.length === 0) {
    return "";
  }
  return refs
    .map((ref) => (ref.modifier ? `${text(ref.label)}(${ref.modifier})` : text(ref.label)))
    .join("|");
}

/** "14:32:07" — padded and 24-hour, so the line never changes width. */
function clockTime(when) {
  const pad = (number) => String(number).padStart(2, "0");
  return `${pad(when.getHours())}:${pad(when.getMinutes())}:${pad(when.getSeconds())}`;
}

function isBBox(bbox) {
  return Array.isArray(bbox) && bbox.length === 4 && bbox.every((v) => typeof v === "number");
}

/**
 * The drawing zone an item sits in, e.g. "C3", from the centre of its bbox.
 *
 * Rows A–D run top to bottom and columns 1–6 left to right, the way a sheet's border is
 * usually lettered. `bbox` is [x0, y0, x1, y1] in 0..1 from the top-left of the page.
 */
const ZONE_ROWS = "ABCD";
const ZONE_COLUMNS = 6;

function zoneOf(bbox) {
  if (!isBBox(bbox)) {
    return null;
  }
  const clamp = (value, top) => Math.min(top, Math.max(0, value));
  const cx = (bbox[0] + bbox[2]) / 2;
  const cy = (bbox[1] + bbox[3]) / 2;
  const row = clamp(Math.floor(cy * ZONE_ROWS.length), ZONE_ROWS.length - 1);
  const column = clamp(Math.floor(cx * ZONE_COLUMNS), ZONE_COLUMNS - 1);
  return `${ZONE_ROWS[row]}${column + 1}`;
}

/* ---- Toasts -------------------------------------------------------------- */

/** How long every toast stays, errors included. */
const TOAST_MS = 5000;

/** Gap kept between the toasts and whichever bar they would otherwise sit on. */
const TOAST_GAP_PX = 12;

/**
 * Lift the toasts clear of the action bar and the status bar (with its legend).
 *
 * Both bars sit along the bottom of the window on a wide screen; on a narrow one they are
 * in the page flow, so whether one is under the toasts depends on the scroll. Each bar
 * that reaches into the bottom strip where the toasts would go pushes them up above it.
 */
function placeToasts() {
  const height = toasts.offsetHeight;
  let bottom = 16;
  for (const bar of [actionBar, statusBar]) {
    const rect = bar.getBoundingClientRect();
    const reachesStrip = rect.bottom > window.innerHeight - bottom - height && rect.top < window.innerHeight;
    if (reachesStrip) {
      bottom = Math.max(bottom, window.innerHeight - rect.top + TOAST_GAP_PX);
    }
  }
  toasts.style.bottom = `${bottom}px`;
}

window.addEventListener("resize", placeToasts);
window.addEventListener("scroll", placeToasts, { passive: true });

/**
 * A small dark pill above the bottom bars, gone after five seconds.
 *
 * A message already on screen is not shown twice: its timer starts again instead, so a
 * repeated error reads as "still true" rather than stacking up copies. Errors are
 * announced as alerts and stay readable for the same five seconds.
 */
function toast(message, { error = false } = {}) {
  const existing = Array.from(toasts.children).find((item) => item.dataset.message === message);
  if (existing) {
    clearTimeout(Number(existing.dataset.timer));
    existing.dataset.timer = String(setTimeout(() => existing.remove(), TOAST_MS));
    return;
  }

  const item = document.createElement("div");
  item.className = error ? "toast is-error" : "toast";
  item.setAttribute("role", error ? "alert" : "status");
  item.dataset.message = message;

  const body = document.createElement("span");
  body.textContent = message;
  item.appendChild(body);

  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";
  close.addEventListener("click", () => item.remove());
  item.appendChild(close);

  toasts.appendChild(item);
  // Three at most; the oldest goes first.
  while (toasts.children.length > 3) {
    toasts.firstElementChild.remove();
  }
  item.dataset.timer = String(setTimeout(() => item.remove(), TOAST_MS));
  placeToasts();
}

/** Drop the errors still on screen; called as a new request starts, so they cannot be
 *  mistaken for news about it. */
function clearErrorToasts() {
  for (const item of toasts.querySelectorAll(".toast.is-error")) {
    item.remove();
  }
}

/* ---- Tabs ---------------------------------------------------------------- */

function selectTab(name, { focus = false } = {}) {
  for (const tab of tabs) {
    const on = tab.id === `tab-${name}`;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !on;
    if (on && focus) {
      tab.focus();
    }
  }
  panelBody.scrollTop = 0;
}

for (const tab of tabs) {
  tab.addEventListener("click", () => selectTab(tab.id.replace("tab-", "")));
  // Arrow keys move between tabs, the usual tablist pattern.
  tab.addEventListener("keydown", (event) => {
    const index = tabs.indexOf(tab);
    let next = null;
    if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
    if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
    if (event.key === "Home") next = tabs[0];
    if (event.key === "End") next = tabs[tabs.length - 1];
    if (next) {
      event.preventDefault();
      selectTab(next.id.replace("tab-", ""), { focus: true });
    }
  });
}

/* ---- The stage machine --------------------------------------------------- */

/** Where the run is, read from its real state. */
function currentStage() {
  if (reading) return "reading";
  if (!state.runId) return "empty";
  if (fixing) return "fixing";
  if (checking) return "checking";
  if (!state.checked || state.stale) return "confirm";
  if (state.fixedAt && !state.agentStale) return "done";
  return state.errorCount > 0 ? "checked" : "clean";
}

/** Stepper position for each stage: which step is current, and how many are done. */
const STEP_OF = {
  empty: [0, 0],
  reading: [0, 0],
  confirm: [1, 1],
  checking: [2, 2],
  checked: [3, 3],
  clean: [3, 3],
  fixing: [3, 3],
  done: [4, 4],
};

function paintStepper(stage) {
  let [current, done] = STEP_OF[stage];
  if (stage === "done" && state.exported) {
    current = -1;
    done = 5;
  }
  for (const step of stepper.children) {
    const index = Number(step.dataset.step);
    step.classList.toggle("is-done", index < done);
    step.classList.toggle("is-current", index === current);
    if (index === current) {
      step.setAttribute("aria-current", "step");
    } else {
      step.removeAttribute("aria-current");
    }
    const name = step.querySelector(".step-name").textContent;
    step.setAttribute("aria-label", index < done ? `${name}, done` : name);
  }
}

function providerLabel(value) {
  return PROVIDER_LABELS[value] || value || "the model";
}

/** "Read by <code>muse-spark-1.2</code> (fallback)", built as nodes. */
function setPill(prefix, model, { busy = false, fellBack = false } = {}) {
  const parts = [prefix];
  if (model) {
    parts.push(" ");
    const code = document.createElement("code");
    code.textContent = model;
    parts.push(code);
  }
  if (fellBack) {
    parts.push(" (fallback)");
  }
  modelPillText.replaceChildren(...parts);
  modelPill.classList.toggle("is-busy", busy);
  modelPill.classList.toggle("is-fallback", fellBack);
}

function paintPill(stage) {
  // Said whenever the server has told us it is answering with the development fake, so a
  // screenshot or a demo can never pass the fake off as a real model's reading.
  fakeBadge.hidden = !state.fake;
  // In fake mode no real provider is called, so the busy line must not name one.
  const reader = state.fake ? "the fake model" : providerLabel(providerSelect.value);
  const proposer = state.fake ? "the fake model" : providerLabel(state.provider);
  if (stage === "reading") {
    setPill(`Reading with ${reader}`, null, { busy: true });
  } else if (stage === "fixing") {
    setPill(`Proposing with ${proposer}`, null, { busy: true });
  } else if (state.answered) {
    setPill(state.answered.prefix, state.answered.model, { fellBack: state.answered.fellBack });
  } else {
    setPill(`Ready · ${providerLabel(providerSelect.value)}`, null);
  }
}

function itemCount() {
  const w = state.working || {};
  return (w.datums || []).length + (w.dimensions || []).length + (w.fcfs || []).length;
}

function phaseText(stage) {
  switch (stage) {
    case "empty":
      return "Drop or choose a drawing to begin.";
    case "reading":
      return `Reading ${state.pendingFile ? state.pendingFile.name : "the drawing"}… about 20–30 seconds.`;
    case "confirm":
      if (state.stale) {
        return "The tables changed since the last check. Confirm them to check again.";
      }
      return (
        `Read ${plural(itemCount(), "item")} in ${seconds(state.run && state.run.latency_s)}. ` +
        "Check the tables against the drawing, then confirm."
      );
    case "checking":
      return "Running DraftCheck's rules on the confirmed tables…";
    case "checked":
      return `Checked: ${plural(state.errorCount, "error")}, ${plural(state.warningCount, "warning")}.`;
    case "clean":
      return `Checked: no errors, ${plural(state.warningCount, "warning")}.`;
    case "fixing":
      return "The agent is proposing fixes; the rules re-check every round.";
    case "done": {
      const fix = state.fix;
      return (
        `Done after ${plural(fix.rounds, "round")}: ${fix.resolved} of ${fix.total} ` +
        "findings verified resolved by the rules."
      );
    }
    default:
      return "";
  }
}

/** The primary button and its hint, for each stage. */
function primaryFor(stage) {
  const blocked = invalidInputs.size > 0;
  switch (stage) {
    case "empty":
      return {
        label: "Read drawing",
        hint: "Choose a PDF, PNG, or JPG; the model reads its dimensions, datums, and frames.",
      };
    case "reading":
      return {
        label: "Reading…",
        disabled: true,
        hint: "The model is reading the drawing. This usually takes 20–30 seconds.",
      };
    case "confirm":
      return {
        label: "Confirm and run checks",
        disabled: blocked,
        warn: blocked,
        hint: blocked
          ? "Some cells still need fixing — they are outlined in red."
          : "Runs DraftCheck's rules on the tables exactly as they are now.",
      };
    case "checking":
      return {
        label: checkLabelShown ? "Checking…" : "Confirm and run checks",
        disabled: true,
        hint: "Runs DraftCheck's rules on the tables exactly as they are now.",
      };
    case "checked":
      return {
        label: "Let the agent fix them",
        disabled: blocked,
        warn: blocked,
        hint: blocked
          ? "Some cells still need fixing — they are outlined in red."
          : `The model proposes edits for the ${plural(state.errorCount, "error")}; ` +
            "the rules re-check every round.",
      };
    case "clean":
      return {
        label: "Nothing to fix",
        disabled: true,
        hint: "The rules found no errors, so there is nothing for the agent to fix.",
      };
    case "fixing":
      return {
        label: `Running rounds (up to ${MAX_ROUNDS})…`,
        disabled: true,
        hint: "Each round's edits are re-checked by the rule engine before the next.",
      };
    case "done":
      return {
        label: "Download redlined PDF",
        hint: "The drawing with every finding marked, plus a page listing each correction.",
      };
    default:
      return { label: "", hint: "" };
  }
}

/** The count on the Issues tab: error findings still open. */
function openErrors() {
  if (state.fixedAt && !state.agentStale && state.fix) {
    return state.fix.finalErrors;
  }
  return state.checked ? state.errorCount : 0;
}

/** Repaint everything that follows from the stage. Called after every change of state. */
function updateUI() {
  const stage = currentStage();
  const busy = stage === "reading" || stage === "fixing";

  paintStepper(stage);
  paintPill(stage);
  phase.textContent = phaseText(stage);

  const action = primaryFor(stage);
  primaryButton.textContent = action.label;
  primaryButton.disabled = Boolean(action.disabled);
  primaryButton.dataset.stage = stage;
  actionHint.textContent = action.hint;
  actionHint.classList.toggle("is-warn", Boolean(action.warn));
  cancelButton.hidden = !busy;

  // Desk: the file, and the drop zone or the paper.
  const file = reading ? state.pendingFile : state.file;
  fileName.textContent = file ? file.name : "No drawing yet";
  fileSub.textContent = file
    ? `Sheet 1, ${file.source === "sample" ? "sample drawing" : file.source === "demo" ? "demo extraction" : "uploaded"}`
    : "Upload a drawing to begin";

  const showPaper = Boolean(state.runId) && !reading && state.imageReady;
  emptyState.hidden = Boolean(state.runId) && !reading;
  paper.hidden = !showPaper;
  if (showPaper) {
    fitPaper();
  }

  dropzone.classList.toggle("is-busy", reading);
  dropzone.setAttribute("aria-disabled", String(reading));
  dropzone.tabIndex = reading ? -1 : 0;
  dropMain.textContent = reading
    ? `Reading ${state.pendingFile ? state.pendingFile.name : "the drawing"}…`
    : DROP_PROMPT;

  uploadButton.disabled = busy;
  for (const button of sampleButtons) {
    button.disabled = busy;
  }
  providerSelect.disabled = busy;

  // Panel: what each tab has to show.
  const hasRun = Boolean(state.runId) && !reading;
  extracted.hidden = !hasRun;
  extractedEmpty.hidden = hasRun;
  resetButton.disabled = fixing || checking;
  addDatumButton.disabled = fixing || checking;

  issuesEmpty.hidden = state.checked;
  const errors = openErrors();
  issueCount.textContent = String(errors);
  issueCount.hidden = errors === 0;
  issueCount.setAttribute("aria-label", `${plural(errors, "open error")}`);

  const fixed = Boolean(state.fixedAt);
  correctionsEmpty.hidden = fixed;
  agentPanel.hidden = !fixed;
}

/* ---- Sizing the paper to the stage --------------------------------------- */

/** Scale the page to fit the stage, keeping its aspect; the SVG follows the paper. */
function fitPaper() {
  if (!state.imageReady || paper.hidden) {
    return;
  }
  const style = getComputedStyle(stageEl);
  const width = stageEl.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const height = stageEl.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
  if (width <= 0 || height <= 0) {
    return;
  }
  const scale = Math.min(width / state.imageW, height / state.imageH);
  paper.style.width = `${Math.floor(state.imageW * scale)}px`;
  paper.style.height = `${Math.floor(state.imageH * scale)}px`;
}

new ResizeObserver(fitPaper).observe(stageEl);

pageImage.addEventListener("load", () => {
  state.imageW = pageImage.naturalWidth;
  state.imageH = pageImage.naturalHeight;
  state.imageReady = true;
  // The overlay is in image pixels, so step 19b can draw at the drawing's own scale.
  overlay.setAttribute("viewBox", `0 0 ${state.imageW} ${state.imageH}`);
  overlay.setAttribute("preserveAspectRatio", "none");
  layoutBoxes();
  updateUI();
});

pageImage.addEventListener("error", () => {
  if (pageImage.getAttribute("src")) {
    toast("The rendered page could not be loaded.", { error: true });
  }
});

/* ---- Linking the tables to the drawing ----------------------------------- */

/*
 * Every datum, dimension, and frame has an id (DT1, D1, F1…) and, when the model said
 * where it is, a bbox. The id is the thread — one box on the image, one row in a table,
 * and any finding about the item all share it.
 */

/** item id -> { box, row, bbox }. `box` is null for an item with no bbox. Rebuilt per run. */
const linked = new Map();

/** The ids lit by the last click. */
let pickedIds = [];

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

/** Light one or more items on both sides, and bring `scrollInto` into view. */
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

/** Register every item, and draw an SVG box for the ones the model placed. */
function buildOverlay(items) {
  linked.clear();
  pickedIds = [];
  boxLayer.replaceChildren();

  for (const item of items) {
    if (!item.id) {
      continue;
    }
    if (!isBBox(item.bbox)) {
      linked.set(item.id, { box: null, row: null, bbox: null });
      continue;
    }
    const box = document.createElementNS(SVG_NS, "rect");
    box.setAttribute("class", item.unsure ? "obox is-unsure" : "obox");
    box.dataset.itemId = item.id;
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `${item.id}: ${item.title}`;
    box.appendChild(title);
    boxLayer.appendChild(box);
    linked.set(item.id, { box, row: null, bbox: item.bbox });
  }
  layoutBoxes();
}

/** Place the boxes in image pixels. Needs the image's size, so it reruns on load. */
function layoutBoxes() {
  if (!state.imageReady) {
    return;
  }
  for (const pair of linked.values()) {
    if (!pair.box) {
      continue;
    }
    const [x0, y0, x1, y1] = pair.bbox;
    pair.box.setAttribute("x", String(x0 * state.imageW));
    pair.box.setAttribute("y", String(y0 * state.imageH));
    pair.box.setAttribute("width", String(Math.max(0, x1 - x0) * state.imageW));
    pair.box.setAttribute("height", String(Math.max(0, y1 - y0) * state.imageH));
  }
}

/** Tie a freshly built row to its box, or mark it as unplaceable. */
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

/* Hovering either side lights both. Delegated, so the handlers survive every rebuild. */
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
hoverHandlers(extracted);

// Clicking a box goes to its row in the Extracted tab; clicking a row lights its box.
overlay.addEventListener("click", (event) => {
  const box = event.target.closest(".obox");
  if (!box) {
    return;
  }
  const id = box.dataset.itemId;
  const pair = linked.get(id);
  selectTab("extracted");
  pick([id], pair && pair.row);
});

extracted.addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-item-id]");
  if (row) {
    pick([row.dataset.itemId], null);
  }
});

overlayToggle.addEventListener("change", () => {
  boxLayer.style.display = overlayToggle.checked ? "" : "none";
});

/* ---- The two copies of the drawing --------------------------------------- */

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function byId(list, id) {
  return (list || []).find((item) => item.id === id);
}

/** An id for a datum the user adds, chosen here so it never collides with another. */
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
 * An invalid cell is outlined, the reason sits in its tooltip, and the primary button
 * stays disabled until the set is empty. `working` keeps the last good value, so nothing
 * half-typed is ever posted.
 */

/** The inputs currently holding something unusable. Emptied by `renderTables`. */
const invalidInputs = new Set();

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
}

/**
 * The number in a numeric input. A `type="number"` input empties its own value when what
 * was typed is not a number and raises `badInput` instead, so that is checked first —
 * before an empty box could be mistaken for a field left blank on purpose.
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

/** "A|B(MMC)|C" as a list of datum references, or `null` when it is not readable. */
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

/** The control's padding and border, in characters, with a little room to type into. */
const INPUT_PAD_CH = 3;
const MIN_INPUT_CH = 5;

/**
 * Set a control's minimum width from the value it holds, so a narrow panel makes the
 * column grow (and `.table-scroll` scroll) instead of hiding digits — "100" never reads
 * as "10".
 */
function fitControl(control) {
  if (control.tagName !== "INPUT" || control.type === "checkbox") {
    return;
  }
  const characters = Math.max(control.value.length, 0) + INPUT_PAD_CH;
  control.style.minWidth = `${Math.max(MIN_INPUT_CH, characters)}ch`;
}

function markEdited(cell, edited, originalText) {
  cell.classList.toggle("is-edited", edited);
  if (edited) {
    cell.title = `Edited. Muse Spark read: ${originalText}`;
  } else {
    cell.removeAttribute("title");
  }
}

/**
 * One editable cell: `read` turns the control into `{ ok, value }`, `apply` writes an
 * accepted value into `working`, and `was` is what the model read.
 */
function editCell(control, { read, apply, was, shown, same, mono = true, cell }) {
  const target = cell || document.createElement("td");
  if (cell) {
    target.replaceChildren();
  }
  target.classList.toggle("mono", mono);
  target.appendChild(control);

  const matches = same || ((a, b) => a === b);

  function commit(first) {
    fitControl(control);
    const result = read();
    setValid(control, result.ok, result.message || "This value cannot be used.");
    if (!result.ok) {
      if (!first) updateUI();
      return;
    }
    apply(result.value);
    markEdited(target, !matches(result.value, was), shown);
    if (!first) {
      afterEdit();
    }
  }

  control.addEventListener("input", () => commit(false));
  commit(true);
  return target;
}

function textCell(value, className) {
  const cell = document.createElement("td");
  if (className) {
    cell.className = className;
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

/** A number cell wired to one field of one item. */
function numberCell(item, field, was, { required, min, cell, label }) {
  const control = numberControl(numInput(item[field]), { min });
  if (label) {
    control.setAttribute("aria-label", label);
  }
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
 * The model returns a confidence with every item. Below the threshold the row gets a
 * warn tint and a note, and the box on the drawing goes amber. It changes nothing about
 * the check: the rules run on the values, not on how sure the model was.
 */

/** Below this, an item is marked for a second look. The only place this number lives. */
const CONFIDENCE_THRESHOLD = 0.8;

/** A missing confidence is unknown, not low — the model said nothing either way. */
function isUnsure(item) {
  const value = item && item.confidence;
  return typeof value === "number" && isFinite(value) && value < CONFIDENCE_THRESHOLD;
}

/** "92%", or an em dash when the model gave no confidence. */
function confidenceText(item) {
  const value = item && item.confidence;
  return typeof value === "number" && isFinite(value) ? `${Math.round(value * 100)}%` : EM_DASH;
}

let unsureIds = [];

function unsureMarker(item) {
  if (!isUnsure(item)) {
    return null;
  }
  const mark = document.createElement("span");
  mark.className = "unsure";
  mark.textContent = "Low confidence, check this";
  mark.title =
    `Muse Spark is unsure about this one (confidence ${item.confidence.toFixed(2)}). ` +
    `Please check it.`;
  return mark;
}

function goToFirstUnsure() {
  const id = unsureIds[0];
  const pair = id ? linked.get(id) : undefined;
  if (pair && pair.row) {
    pick([id], pair.row);
  }
}

function renderConfidenceSummary() {
  if (unsureIds.length === 0) {
    confidenceSummary.className = "confidence-summary is-clear";
    confidenceSummary.replaceChildren("Muse Spark is confident about every item");
    return;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "confidence-link";
  button.textContent = `Muse Spark flagged ${plural(unsureIds.length, "item")} to double-check`;
  button.addEventListener("click", goToFirstUnsure);
  confidenceSummary.className = "confidence-summary is-flagged";
  confidenceSummary.replaceChildren(button);
}

/* ---- Column headers, each with a plain-language tip ---------------------- */

/*
 * The tips are DraftCheck's own explanations, written for someone checking a drawing
 * for the first time. They are not taken from any standard.
 */
const TIPS = {
  id: "DraftCheck's name for this item. The same id links the row, its box on the drawing, and any issue about it.",
  label: "The single letter that names this datum feature on the drawing.",
  textRaw: "The dimension exactly as the model read it off the sheet, before it was split into numbers.",
  value: "The size or distance the dimension calls for, in the drawing's units.",
  tolPlus: "How far above the value the part may measure. Empty means the dimension has no tolerance of its own.",
  tolMinus: "How far below the value the part may measure. Empty means the dimension has no tolerance of its own.",
  basic: "Ticked for an exact, untoleranced dimension (usually drawn in a box). A feature control frame controls it instead.",
  fromTo: "The two features the dimension measures between, as the model named them.",
  zone: "Where the item sits on the sheet: rows A to D from top to bottom, columns 1 to 6 from left to right.",
  confidence: "How sure the model said it was about this reading. Under 80% the row is flagged for a second look; it does not change the check.",
  characteristic: "The kind of geometric control the frame applies, such as position or flatness.",
  tolerance: "The size of the zone the feature must stay within, in the drawing's units.",
  modifier: "Whether the tolerance is tied to the feature's largest or smallest material size (MMC or LMC), or to neither.",
  datumRefs: "The datums the frame is measured from, most important first, separated by bars, for example A|B|C.",
};

const COLUMNS = {
  datums: [
    { label: "ID", tip: TIPS.id },
    { label: "Label", tip: TIPS.label },
    { label: "Zone", tip: TIPS.zone },
    { label: "Conf.", tip: TIPS.confidence, num: true },
    { label: "Remove", hidden: true },
  ],
  dimensions: [
    { label: "ID", tip: TIPS.id },
    { label: "Text as printed", tip: TIPS.textRaw },
    { label: "Value", tip: TIPS.value },
    { label: "Tol +", tip: TIPS.tolPlus },
    { label: "Tol −", tip: TIPS.tolMinus },
    { label: "Basic", tip: TIPS.basic },
    { label: "From → to", tip: TIPS.fromTo },
    { label: "Zone", tip: TIPS.zone },
    { label: "Conf.", tip: TIPS.confidence, num: true },
  ],
  fcfs: [
    { label: "ID", tip: TIPS.id },
    { label: "Characteristic", tip: TIPS.characteristic },
    { label: "Tolerance", tip: TIPS.tolerance },
    { label: "Modifier", tip: TIPS.modifier },
    { label: "Datum refs", tip: TIPS.datumRefs },
    { label: "Zone", tip: TIPS.zone },
    { label: "Conf.", tip: TIPS.confidence, num: true },
  ],
};

let tipCounter = 0;

/** Show a tip bubble beside its button. Fixed-positioned so a table scroller cannot clip it. */
function openTip(button, bubble) {
  const rect = button.getBoundingClientRect();
  bubble.classList.add("is-open");
  const width = bubble.offsetWidth;
  const left = Math.min(window.innerWidth - width - 8, Math.max(8, rect.left + rect.width / 2 - width / 2));
  const below = rect.bottom + 6;
  const top = below + bubble.offsetHeight > window.innerHeight ? rect.top - bubble.offsetHeight - 6 : below;
  bubble.style.left = `${left}px`;
  bubble.style.top = `${top}px`;
}

function buildHeader(rowId, columns) {
  const row = $(rowId);
  row.replaceChildren();
  for (const column of columns) {
    const th = document.createElement("th");
    th.scope = "col";
    if (column.num) {
      th.className = "num";
    }
    if (column.hidden) {
      const span = document.createElement("span");
      span.className = "visually-hidden";
      span.textContent = column.label;
      th.appendChild(span);
      row.appendChild(th);
      continue;
    }

    const inner = document.createElement("span");
    inner.className = "th-inner";
    inner.append(column.label);

    const wrap = document.createElement("span");
    wrap.className = "tip-wrap";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tip";
    button.textContent = "i";
    button.setAttribute("aria-label", `About ${column.label}`);
    const bubble = document.createElement("span");
    bubble.className = "tip-text";
    bubble.setAttribute("role", "tooltip");
    bubble.id = `tip-${(tipCounter += 1)}`;
    bubble.textContent = column.tip;
    button.setAttribute("aria-describedby", bubble.id);

    const open = () => openTip(button, bubble);
    const close = () => bubble.classList.remove("is-open");
    button.addEventListener("mouseenter", open);
    button.addEventListener("focus", open);
    button.addEventListener("mouseleave", close);
    button.addEventListener("blur", close);
    button.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close();
    });
    // A click on the tip is not a click on the table.
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      open();
    });

    wrap.append(button, bubble);
    inner.appendChild(wrap);
    th.appendChild(inner);
    row.appendChild(th);
  }
}

// The bubbles are fixed-positioned, so close them all when the panel scrolls under them.
panelBody.addEventListener("scroll", () => {
  for (const bubble of document.querySelectorAll(".tip-text.is-open")) {
    bubble.classList.remove("is-open");
  }
});

buildHeader("head-datums", COLUMNS.datums);
buildHeader("head-dimensions", COLUMNS.dimensions);
buildHeader("head-fcfs", COLUMNS.fcfs);

/* ---- Building the tables ------------------------------------------------- */

/** The cells every item row starts and ends with: its id, and its zone and confidence. */
function idCell(id) {
  return textCell(text(id), "item-id");
}

function zoneCell(item) {
  return textCell(zoneOf(item.bbox) || EM_DASH, "zone");
}

function confidenceCell(item) {
  return textCell(confidenceText(item), "conf num");
}

/** Replace a tbody's rows; an empty list leaves one row saying so. */
function fillTable(tbodyId, rows, columns, emptyMessage) {
  const tbody = $(tbodyId);
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
}

function setCount(id, items) {
  $(id).textContent = `(${items.length})`;
}

const UNIT_OPTIONS = [
  ["", "none"],
  ["mm", "mm"],
  ["in", "in"],
];

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
    cell: $("v-units"),
  });

  if (!working.general_tolerance) {
    working.general_tolerance = { linear_plus_minus: null, text_raw: null };
  }
  const originalTolerance = original.general_tolerance || {};
  numberCell(working.general_tolerance, "linear_plus_minus", originalTolerance.linear_plus_minus ?? null, {
    required: false,
    min: 0,
    cell: $("v-gentol"),
    label: "General tolerance",
  });

  $("v-gentol-raw").textContent = text(originalTolerance.text_raw);
}

function renderDatums() {
  const datums = state.working.datums || [];
  const originals = state.original.datums || [];

  const rows = datums.map((datum) => {
    const was = byId(originals, datum.id);
    const control = textControl(datum.label || "", { width: 3 });
    control.classList.add("cell-label");
    control.setAttribute("aria-label", `Datum label for ${datum.id}`);

    const labelCell = editCell(control, {
      read: () => {
        const value = control.value.trim().toUpperCase();
        return { ok: /^[A-Z]$/.test(value), value, message: "A datum label is a single letter, A to Z." };
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
    remove.setAttribute("aria-label", `Remove datum ${datum.label || datum.id}`);
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      state.working.datums = state.working.datums.filter((item) => item !== datum);
      renderTables();
      afterEdit();
    });
    removeCell.appendChild(remove);

    return {
      id: datum.id,
      cells: [idCell(datum.id), labelCell, zoneCell(datum), confidenceCell(datum), removeCell],
      marker: unsureMarker(datum),
    };
  });

  setCount("count-datums", datums);
  fillTable("rows-datums", rows, COLUMNS.datums.length, "No datum features were read.");
}

function renderDimensions() {
  const dimensions = state.working.dimensions || [];
  const originals = state.original.dimensions || [];

  const rows = dimensions.map((dimension) => {
    const was = byId(originals, dimension.id) || {};
    const id = dimension.id;

    const basic = document.createElement("input");
    basic.type = "checkbox";
    basic.className = "cell-check";
    basic.checked = Boolean(dimension.is_basic);
    basic.setAttribute("aria-label", `Basic dimension, ${id}`);

    return {
      id,
      marker: unsureMarker(dimension),
      cells: [
        idCell(id),
        textCell(text(dimension.text_raw), "mono"),
        numberCell(dimension, "value", was.value ?? null, { required: true, label: `Value, ${id}` }),
        numberCell(dimension, "tol_plus", was.tol_plus ?? null, {
          required: false,
          min: 0,
          label: `Plus tolerance, ${id}`,
        }),
        numberCell(dimension, "tol_minus", was.tol_minus ?? null, {
          required: false,
          min: 0,
          label: `Minus tolerance, ${id}`,
        }),
        editCell(basic, {
          read: () => ({ ok: true, value: basic.checked }),
          apply: (value) => {
            dimension.is_basic = value;
          },
          was: Boolean(was.is_basic),
          shown: was.is_basic ? "yes" : "no",
          mono: false,
        }),
        textCell(fromTo(dimension), "mono"),
        zoneCell(dimension),
        confidenceCell(dimension),
      ],
    };
  });

  setCount("count-dimensions", dimensions);
  fillTable("rows-dimensions", rows, COLUMNS.dimensions.length, "No dimensions were read.");
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
    const id = fcf.id;

    const modifier = selectControl(MODIFIER_OPTIONS, fcf.material_modifier || "");
    modifier.setAttribute("aria-label", `Material modifier, ${id}`);
    const refs = textControl(datumRefsText(fcf.datum_refs), { width: 10 });
    refs.setAttribute("aria-label", `Datum references, ${id}`);

    return {
      id,
      marker: unsureMarker(fcf),
      cells: [
        idCell(id),
        textCell(text(fcf.characteristic)),
        numberCell(fcf, "tolerance", was.tolerance ?? null, {
          required: true,
          min: 0,
          label: `Tolerance, ${id}`,
        }),
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
          same: (a, b) => datumRefsText(a) === datumRefsText(b),
        }),
        zoneCell(fcf),
        confidenceCell(fcf),
      ],
    };
  });

  setCount("count-fcfs", fcfs);
  fillTable("rows-fcfs", rows, COLUMNS.fcfs.length, "No feature control frames were read.");
}

/** Everything the user can edit, built from `working` and compared against `original`. */
function renderTables() {
  invalidInputs.clear();
  unsureIds = [];

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

  renderHeader();
  renderDatums();
  renderDimensions();
  renderFcfs();
  renderConfidenceSummary();
}

/* ---- Findings ------------------------------------------------------------ */

function setCheckStatus() {
  if (!state.checkedAt) {
    checkStatus.hidden = true;
    return;
  }
  const at = `Checked at ${clockTime(state.checkedAt)}`;
  checkStatus.textContent = state.stale ? `${at} — edited since; confirm again to re-check` : at;
  checkStatus.classList.toggle("is-stale", state.stale);
  checkStatus.hidden = false;
}

function setAgentStatus() {
  if (!state.fixedAt) {
    agentStatus.hidden = true;
    return;
  }
  const at = `Fixes proposed at ${clockTime(state.fixedAt)}`;
  agentStatus.textContent = state.agentStale ? `${at} — edited since; check and fix again` : at;
  agentStatus.classList.toggle("is-stale", state.agentStale);
  agentStatus.hidden = false;
}

/** After every accepted edit: whatever was computed from the old tables is now dated. */
function afterEdit() {
  if (state.checked) {
    state.stale = true;
    setCheckStatus();
  }
  if (state.fixedAt) {
    state.agentStale = true;
    setAgentStatus();
  }
  updateUI();
}

function clearFindings() {
  state.checked = false;
  state.checkedAt = null;
  state.stale = false;
  state.errorCount = 0;
  state.warningCount = 0;
  checkStatus.hidden = true;
  findingsList.replaceChildren();
  findingsNone.hidden = true;
  checkIssuesBox.hidden = true;
  clearAgent();
}

/** Where a finding is: its own bbox, or the first of its items that has one. */
function findingZone(finding) {
  const own = zoneOf(finding.bbox);
  if (own) {
    return own;
  }
  for (const id of finding.item_ids || []) {
    const pair = linked.get(id);
    const zone = pair && zoneOf(pair.bbox);
    if (zone) {
      return zone;
    }
  }
  return null;
}

/** One finding card: delta, title, rule and zone, severity, why, and source. */
function findingCard(finding, number) {
  const severity = finding.severity === "error" ? "error" : "warning";
  const item = document.createElement("li");
  item.className = `finding sev-${severity}`;
  item.tabIndex = 0;

  const delta = document.createElement("span");
  delta.className = "delta";
  delta.setAttribute("aria-hidden", "true");
  delta.textContent = String(number);

  const title = document.createElement("p");
  title.className = "finding-title";
  title.textContent = finding.title || finding.rule_id;

  const ids = Array.isArray(finding.item_ids) ? finding.item_ids : [];
  const zone = findingZone(finding);
  const where = zone ? `zone ${zone}` : ids.length ? "no location" : "whole drawing";
  const meta = document.createElement("p");
  meta.className = "finding-meta";
  meta.textContent = `${finding.rule_id}, ${where}`;

  const chip = document.createElement("span");
  chip.className = "chip";
  chip.textContent = severity;

  const why = document.createElement("p");
  why.className = "finding-why";
  why.textContent = finding.message;

  const source = document.createElement("p");
  source.className = "finding-source";
  source.textContent = `Source: ${text(finding.source)}`;

  item.append(delta, title, chip, meta, why, source);
  item.setAttribute("aria-label", `Issue ${number}, ${severity}: ${title.textContent}. ${meta.textContent}`);

  function show() {
    for (const other of findingsList.children) {
      other.classList.toggle("is-active", other === item);
      other.removeAttribute("aria-current");
    }
    item.setAttribute("aria-current", "true");
    pick(ids, null);
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

function renderFindings(result) {
  const findings = result.findings || [];
  const issues = result.parse_issues || [];

  const cards = document.createDocumentFragment();
  findings.forEach((finding, index) => cards.appendChild(findingCard(finding, index + 1)));

  const lines = document.createDocumentFragment();
  for (const issue of issues) {
    const line = document.createElement("li");
    line.textContent = issue;
    lines.appendChild(line);
  }

  findingsList.replaceChildren(cards);
  checkIssuesList.replaceChildren(lines);
  findingsNone.hidden = findings.length > 0;
  checkIssuesBox.hidden = issues.length === 0;

  // A new check supersedes the last fix: its rounds were about different findings.
  clearAgent();
  state.checked = true;
  state.checkedAt = new Date();
  state.stale = false;
  state.errorCount = result.error_count || 0;
  state.warningCount = result.warning_count || 0;
  setCheckStatus();
}

/* ---- Corrections --------------------------------------------------------- */

function clearAgent() {
  state.fixedAt = null;
  state.agentStale = false;
  state.fix = null;
  state.exported = false;
  agentStatus.hidden = true;
  agentRounds.replaceChildren();
  agentVerdicts.replaceChildren();
  redlineImage.removeAttribute("src");
}

function paragraph(className, value) {
  const line = document.createElement("p");
  line.className = className;
  line.textContent = value;
  return line;
}

/** One proposed edit. `applied` is the rule engine's word, not the model's. */
function editItem(edit, applied) {
  const item = document.createElement("li");
  item.className = applied ? "edit is-applied" : "edit is-rejected";

  const head = document.createElement("p");
  head.className = "edit-head";
  const tag = document.createElement("span");
  tag.className = "edit-tag";
  tag.textContent = applied ? "applied" : "rejected";
  const sentence = document.createElement("span");
  sentence.className = "edit-text";
  sentence.textContent = edit.edit;
  head.append(tag, sentence);
  item.appendChild(head);

  if (edit.rationale) {
    item.appendChild(paragraph("edit-why", `Why: ${edit.rationale}`));
  }
  if (!applied && edit.reason) {
    item.appendChild(paragraph("edit-reason", `Rejected: ${edit.reason}`));
  }
  return item;
}

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

/** "Answered by muse-spark-1.2 (fallback: timeout)", or null when no model answered. */
function roundModel(round) {
  if (!round.model) {
    return null;
  }
  const fallback = round.fell_back
    ? ` (fallback${round.fallback_reason ? `: ${round.fallback_reason}` : ""})`
    : "";
  return `Answered by ${round.model}${fallback}`;
}

function roundCard(round) {
  const item = document.createElement("li");
  item.className = "round";

  const head = document.createElement("p");
  head.className = "round-head";
  const number = document.createElement("span");
  number.textContent = `Round ${round.round}`;
  const counts = document.createElement("span");
  counts.className = "round-counts";
  counts.textContent = `Findings: ${round.findings_before} → ${round.findings_after}`;
  head.append(number, counts);
  item.appendChild(head);

  const answered = roundModel(round);
  if (answered) {
    const model = paragraph("round-model", answered);
    model.classList.toggle("is-fallback", Boolean(round.fell_back));
    item.appendChild(model);
  }
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
    for (const edit of applied) edits.appendChild(editItem(edit, true));
    for (const edit of rejected) edits.appendChild(editItem(edit, false));
    item.appendChild(edits);
  }

  const cleared = changedFindings("Cleared:", round.resolved, "is-cleared");
  const introduced = changedFindings("Introduced:", round.introduced, "is-introduced");
  if (cleared) item.appendChild(cleared);
  if (introduced) item.appendChild(introduced);
  if (!cleared && !introduced && !round.error) {
    item.appendChild(paragraph("round-empty", "No finding changed in this round."));
  }
  return item;
}

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
 * Show one correction run. The redline URLs are fixed per run and rewritten on every fix,
 * so both carry a timestamp to keep the browser from showing a cached one.
 */
function renderAgent(result) {
  const rounds = result.rounds || [];
  const statuses = result.status || [];

  const roundNodes = document.createDocumentFragment();
  for (const round of rounds) roundNodes.appendChild(roundCard(round));
  if (rounds.length === 0) {
    const item = document.createElement("li");
    item.className = "round";
    item.appendChild(paragraph("round-empty", "The agent was not asked for any edits."));
    roundNodes.appendChild(item);
  }
  const verdictNodes = document.createDocumentFragment();
  for (const entry of statuses) verdictNodes.appendChild(verdictItem(entry));

  agentRounds.replaceChildren(roundNodes);
  agentVerdicts.replaceChildren(verdictNodes);

  const stamp = Date.now();
  redlineImage.src = `${result.redline_png_url}?t=${stamp}`;
  redlinePdf.href = `${result.redline_pdf_url}?t=${stamp}`;

  const finalFindings = result.final_findings || [];
  state.fix = {
    rounds: rounds.length,
    resolved: result.resolved_count || 0,
    total: statuses.length,
    finalErrors: finalFindings.filter((finding) => finding.severity === "error").length,
  };
  state.fixedAt = new Date();
  state.agentStale = false;
  state.exported = false;
  if (result.model) {
    state.answered = { prefix: "Fixed by", model: result.model, fellBack: Boolean(result.fell_back) };
  }
  if (typeof result.fake_model === "boolean") {
    state.fake = result.fake_model;
  }
  setAgentStatus();
}

/* ---- Rendering one run --------------------------------------------------- */

function render(run, file) {
  const drawing = run.drawing || {};
  const issues = run.parse_issues || [];

  state.runId = run.run_id;
  state.run = run;
  state.file = file;
  state.original = clone(drawing);
  state.working = clone(drawing);
  state.answered = { prefix: "Read by", model: run.model, fellBack: Boolean(run.fell_back) };
  state.fake = Boolean(run.fake_model);
  clearFindings();

  $("meta-model").textContent = text(run.model);
  $("meta-latency").textContent = seconds(run.latency_s);
  $("meta-attempts").textContent = text(run.attempts);
  metaFallback.textContent = run.fell_back ? FALLBACK_NOTE : "";
  metaFallback.hidden = !run.fell_back;

  parseIssuesList.replaceChildren();
  for (const issue of issues) {
    const item = document.createElement("li");
    item.textContent = issue;
    parseIssuesList.appendChild(item);
  }
  parseIssuesBox.hidden = issues.length === 0;

  state.imageReady = false;
  pageImage.src = run.page_url;

  renderTables();
  selectTab("extracted");
}

/* ---- The requests -------------------------------------------------------- */

/** The message in a failed response: FastAPI's `detail`, string or list. */
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

/*
 * Reading and fixing each wait on the model, and Cancel aborts the browser's request.
 * The server notices the browser left and aborts its own model call. Every `await` is
 * followed by a look at `signal.aborted`, so a reply that lands in the same instant as
 * the click is dropped rather than drawn.
 */

cancelButton.addEventListener("click", () => {
  if (readController) readController.abort();
  if (fixController) fixController.abort();
});

/**
 * Send one file and show what comes back. Cancel or a failure leaves a run already on
 * screen exactly as it was, because only `render` replaces it.
 */
async function startRun(file, source) {
  clearErrorToasts();
  state.pendingFile = { name: file.name, source };
  reading = true;
  updateUI();

  const body = new FormData();
  body.append("file", file);
  body.append("provider", providerSelect.value);
  const provider = providerSelect.value;

  const controller = new AbortController();
  readController = controller;
  const { signal } = controller;

  try {
    const response = await fetch("/api/runs", { method: "POST", body, signal });
    if (!response.ok) {
      const message = await errorMessage(response);
      if (!signal.aborted) {
        toast(`Could not read the drawing. ${message}`, { error: true });
      }
      return;
    }
    const run = await response.json();
    if (!signal.aborted) {
      state.provider = provider;
      render(run, { name: file.name, source });
    }
  } catch (problem) {
    if (!signal.aborted) {
      toast(`Could not reach the server: ${problem.message}`, { error: true });
    }
  } finally {
    readController = null;
    reading = false;
    state.pendingFile = null;
    if (signal.aborted) {
      toast(CANCELLED_MESSAGE);
    }
    // The same file can be picked again after a failure.
    fileInput.value = "";
    updateUI();
  }
}

/** Send the edited drawing to the rules. What passes is decided server side. */
async function runCheck() {
  if (!state.runId || invalidInputs.size > 0) {
    return;
  }
  clearErrorToasts();
  checking = true;
  checkLabelShown = false;
  updateUI();
  const labelTimer = setTimeout(() => {
    checkLabelShown = true;
    updateUI();
  }, SLOW_CHECK_MS);

  try {
    const response = await fetch(`/api/runs/${state.runId}/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.working),
    });
    if (!response.ok) {
      toast(`Could not check the drawing. ${await errorMessage(response)}`, { error: true });
      return;
    }
    renderFindings(await response.json());
    selectTab("issues");
  } catch (problem) {
    toast(`Could not reach the server: ${problem.message}`, { error: true });
  } finally {
    clearTimeout(labelTimer);
    checking = false;
    checkLabelShown = false;
    updateUI();
  }
}

/**
 * Ask the agent for corrections. Nothing is posted: the server works from the drawing
 * the last check saved, which is why this is only offered while that check is current.
 */
async function runFix() {
  if (!state.runId || !state.checked || state.stale || state.errorCount === 0) {
    return;
  }
  clearErrorToasts();
  fixing = true;
  updateUI();

  const controller = new AbortController();
  fixController = controller;
  const { signal } = controller;

  try {
    const response = await fetch(`/api/runs/${state.runId}/fix`, { method: "POST", signal });
    if (!response.ok) {
      const message = await errorMessage(response);
      if (!signal.aborted) {
        toast(`Could not propose fixes. ${message}`, { error: true });
      }
      return;
    }
    const result = await response.json();
    if (!signal.aborted) {
      renderAgent(result);
      selectTab("corrections");
    }
  } catch (problem) {
    if (!signal.aborted) {
      toast(`Could not reach the server: ${problem.message}`, { error: true });
    }
  } finally {
    fixController = null;
    fixing = false;
    if (signal.aborted) {
      toast(CANCELLED_MESSAGE);
    }
    updateUI();
  }
}

function downloadRedline() {
  if (!state.fixedAt || !redlinePdf.getAttribute("href") || redlinePdf.getAttribute("href") === "#") {
    return;
  }
  redlinePdf.click();
}

redlinePdf.addEventListener("click", () => {
  state.exported = true;
  toast("Downloading the redlined PDF.");
  updateUI();
});

primaryButton.addEventListener("click", () => {
  switch (currentStage()) {
    case "empty":
      fileInput.click();
      break;
    case "confirm":
      runCheck();
      break;
    case "checked":
      runFix();
      break;
    case "done":
      downloadRedline();
      break;
    default:
      break;
  }
});

resetButton.addEventListener("click", () => {
  if (!state.original) {
    return;
  }
  state.working = clone(state.original);
  renderTables();
  afterEdit();
  toast("Tables reset to what the model read.");
});

addDatumButton.addEventListener("click", () => {
  if (!state.working) {
    return;
  }
  // No label yet: the cell opens invalid, which is honest — a datum needs a letter.
  state.working.datums = state.working.datums || [];
  state.working.datums.push({ id: newDatumId(), label: "", location: null, bbox: null });
  renderTables();
  afterEdit();
});

/* ---- Ways in: drop, click, keyboard, the buttons, and the sample ---------- */

const DROP_PROMPT = "Drop a drawing here, or click to choose a file";

/** Why this file cannot be sent, or `null` when it can. The server checks again. */
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

function isBusy() {
  return reading || fixing;
}

function offerFile(file, source = "uploaded") {
  if (isBusy()) {
    return;
  }
  const reason = rejectionReason(file);
  if (reason) {
    toast(reason, { error: true });
    return;
  }
  startRun(file, source);
}

/** Fetch the sample PDF and upload it exactly as a picked file would be. */
async function trySample() {
  if (isBusy()) {
    return;
  }
  try {
    const response = await fetch(SAMPLE_URL);
    if (!response.ok) {
      toast(`Could not load the sample drawing. ${await errorMessage(response)}`, { error: true });
      return;
    }
    const blob = await response.blob();
    offerFile(new File([blob], SAMPLE_NAME, { type: "application/pdf" }), "sample");
  } catch (problem) {
    toast(`Could not reach the server: ${problem.message}`, { error: true });
  }
}

for (const button of sampleButtons) {
  button.addEventListener("click", trySample);
}

uploadButton.addEventListener("click", () => {
  if (!isBusy()) fileInput.click();
});

dropzone.addEventListener("click", () => {
  if (!isBusy()) fileInput.click();
});

dropzone.addEventListener("keydown", (event) => {
  if (isBusy() || (event.key !== "Enter" && event.key !== " ")) {
    return;
  }
  event.preventDefault(); // Space would scroll the page.
  fileInput.click();
});

fileInput.addEventListener("change", () => {
  const file = fileInput.files && fileInput.files[0];
  if (file) {
    offerFile(file);
  }
});

providerSelect.addEventListener("change", updateUI);

/* The whole desk takes a drop, so a second drawing can be dropped on the first. Both
   dragover and drop must preventDefault, or the browser opens the file in the tab. */
desk.addEventListener("dragover", (event) => {
  if (isBusy()) {
    return;
  }
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  desk.classList.add("is-dragging");
});

desk.addEventListener("dragleave", (event) => {
  if (!desk.contains(event.relatedTarget)) {
    desk.classList.remove("is-dragging");
  }
});

desk.addEventListener("drop", (event) => {
  event.preventDefault();
  desk.classList.remove("is-dragging");
  if (isBusy()) {
    return;
  }
  const file = event.dataTransfer.files && event.dataTransfer.files[0];
  if (!file) {
    toast("That drop carried no file. Drag a PDF, PNG, or JPG drawing onto the desk.", { error: true });
    return;
  }
  offerFile(file);
});

/* ---- The demo run -------------------------------------------------------- */

/*
 * `?demo=low-confidence` loads a saved extraction with two items marked unsure, so the
 * markings can be looked at without a model call. The server only answers this for a
 * browser on the same machine with DRAFTCHECK_ENABLE_DEMO=1.
 */
const LOW_CONFIDENCE_DEMO = "low-confidence";

async function loadDemo() {
  state.pendingFile = { name: "plate_001.png", source: "demo" };
  reading = true;
  updateUI();
  try {
    const response = await fetch(`/api/demo/${LOW_CONFIDENCE_DEMO}`, { method: "POST" });
    if (!response.ok) {
      toast(`Could not load the demo. ${await errorMessage(response)}`, { error: true });
      return;
    }
    render(await response.json(), { name: "plate_001.png", source: "demo" });
  } catch (problem) {
    toast(`Could not reach the server: ${problem.message}`, { error: true });
  } finally {
    reading = false;
    state.pendingFile = null;
    updateUI();
  }
}

updateUI();

/** Ask the server whether the development fake model is answering, for the pill's badge. */
async function loadModelMode() {
  try {
    const response = await fetch("/api/model-mode");
    if (response.ok) {
      state.fake = Boolean((await response.json()).fake_model);
      updateUI();
    }
  } catch {
    // No answer means no badge; the upload reply will say so if the fake is in use.
  }
}

loadModelMode();

if (new URLSearchParams(window.location.search).get("demo") === LOW_CONFIDENCE_DEMO) {
  loadDemo();
}
