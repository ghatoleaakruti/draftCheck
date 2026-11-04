/* DraftCheck — the checker's desk.
 *
 * Four requests, the same ones the page has always made:
 *   POST /api/runs               the file and the chosen provider -> run id, extraction, model
 *   GET  /api/runs/{id}/page.png the rendered page, shown as paper on the desk
 *   POST /api/runs/{id}/check    the drawing the user confirmed -> findings
 *   POST /api/runs/{id}/fix      run the correction loop -> rounds, verdicts, redline URLs
 *                                ?stream=1 sends each round as it happens (NDJSON)
 *   POST /api/runs/{id}/explain  an AI explanation for learners (never read by the rules)
 * plus GET /samples/plate_001.pdf, which "Try the sample drawing" fetches and then uploads,
 * and GET /eval/pdfs/<stem>.pdf, which "Try it live" on the Test results page asks for
 * with /?try=<stem>; it is uploaded the same way
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
const crossCheckBox = $("cross-check");
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
const cloudLayer = $("clouds");
const penLayer = $("pen-notes");
const stampLayer = $("stamp-layer");
const scanLayer = $("scan-layer");
const overlayToggle = $("show-overlay");
const readingSheet = $("reading-sheet");
const readingPreview = $("reading-preview");
const readingLabel = $("reading-label");
const readingOverlay = $("reading-overlay");

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
const crossCheckSummary = $("crosscheck-summary");
const resetButton = $("reset");
const addDatumButton = $("add-datum");

const issuesEmpty = $("issues-empty");
const findingsList = $("findings-list");
const roundsStrip = $("rounds-strip");
const findingsNone = $("findings-none");
const checksBox = $("checks");
const checksSummary = $("checks-summary");
const checksSummaryAfter = $("checks-summary-after");
const checksToggle = $("checks-toggle");
const checksList = $("checks-list");
const checkStatus = $("check-status");
const checkIssuesBox = $("check-parse-issues");
const checkIssuesList = $("check-parse-issues-list");

const correctionsEmpty = $("corrections-empty");
const agentPanel = $("agent-panel");
const agentStatus = $("agent-status");
const correctionsIntro = $("corrections-intro");
const correctionsRows = $("corrections-rows");
const redlinePdf = $("redline-pdf");
const exportCsv = $("export-csv");

const explainButton = $("explain-button");
const aboutIntro = $("about-intro");
const aboutResult = $("about-result");
const aboutIssuesBox = $("about-issues");
const aboutIssuesList = $("about-issues-list");

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

/** How the desk names where its file came from. */
const SOURCE_LABELS = {
  sample: "sample drawing",
  demo: "demo extraction",
  suite: "test drawing",
};

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
  /** The cross-check's outcome from the upload reply: status, summary, disagreements. */
  crossCheck: null,
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
  /** The last check's findings, in run_rules order: finding N is cloud N and card N. */
  findings: [],
  /** How many rules the last check ran, as the server said. */
  rulesRun: null,
  /** The last /fix reply, as it came back. */
  fixResult: null,
  /** Pixels per sheet unit on this page image (see sheet.js). */
  unit: 1,
  /** True from a new run until its boxes have been revealed by the final sweep. */
  revealPending: false,
  /** The last /explain reply's `explanation`, or null. Shown only; never sent to the rules. */
  explanation: null,
  /** True once the user asked for an explanation of this run, so a later check refreshes it. */
  explainWanted: false,
};

let reading = false;
let checking = false;
let checkLabelShown = false;
let fixing = false;
/** The round in progress while a streamed fix runs, for the pill. Null otherwise. */
let live = null;
let explaining = false;
/** Set when a check lands while an explanation is out, so it is asked again afterwards. */
let explainQueued = false;

/** The controller for the request Cancel would stop, or `null` when none is out. */
let readController = null;
let fixController = null;
let explainController = null;

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
  if (fixing || replaying) return "fixing";
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
  } else if (stage === "fixing" && live && live.round) {
    // The real current round, from the stream, and the model that answered it.
    const prefix = `Round ${live.round} · proposing with`;
    if (live.model) {
      setPill(prefix, live.model, { busy: true, fellBack: live.fellBack });
    } else {
      setPill(`${prefix} ${proposer}`, null, { busy: true });
    }
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
  // The replay of a fix that has already come back is not something to cancel.
  cancelButton.hidden = !(busy || explaining) || replaying;
  explainButton.disabled = busy || explaining || checking || !state.runId;
  explainButton.textContent = explaining
    ? "Explaining…"
    : state.explanation
      ? "Explain again"
      : "Explain this drawing";

  // Desk: the file, and the drop zone or the paper.
  const file = reading ? state.pendingFile : state.file;
  fileName.textContent = file ? file.name : "No drawing yet";
  fileSub.textContent = file
    ? `Sheet 1, ${SOURCE_LABELS[file.source] || "uploaded"}`
    : "Upload a drawing to begin";

  const showPaper = Boolean(state.runId) && !reading && state.imageReady;
  // While reading, the scanning sheet stands in for both the drop zone and the old page;
  // the old page comes back untouched if the read is cancelled or fails.
  emptyState.hidden = reading || Boolean(state.runId);
  readingSheet.hidden = !reading;
  paper.hidden = !showPaper;
  if (showPaper) {
    fitPaper();
  }
  if (reading) {
    fitReadingSheet();
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
  crossCheckBox.disabled = busy;

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

/** Scale a sheet of `w` x `h` to fit the stage, keeping its aspect. */
function fitSheet(sheet, w, h) {
  const style = getComputedStyle(stageEl);
  const width = stageEl.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const height = stageEl.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
  if (width <= 0 || height <= 0 || !w || !h) {
    return;
  }
  const scale = Math.min(width / w, height / h);
  sheet.style.width = `${Math.floor(w * scale)}px`;
  sheet.style.height = `${Math.floor(h * scale)}px`;
}

/** Scale the page to fit the stage, keeping its aspect; the SVG follows the paper. */
function fitPaper() {
  if (!state.imageReady || paper.hidden) {
    return;
  }
  fitSheet(paper, state.imageW, state.imageH);
}

new ResizeObserver(() => {
  fitPaper();
  fitReadingSheet();
}).observe(stageEl);

/** Give an overlay its image-pixel viewBox and its sheet unit (--u, used by style.css). */
function sizeOverlay(svg, width, height) {
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const unit = Sheet.unitFor(width);
  svg.style.setProperty("--u", String(unit));
  return unit;
}

pageImage.addEventListener("load", () => {
  state.imageW = pageImage.naturalWidth;
  state.imageH = pageImage.naturalHeight;
  state.imageReady = true;
  // The overlay is in image pixels, so everything on it is drawn at the drawing's scale.
  state.unit = sizeOverlay(overlay, state.imageW, state.imageH);
  Sheet.ensureRoughFilter(overlay, state.unit);
  layoutBoxes();
  if (state.checked) {
    drawClouds();
  }
  updateUI();
  if (state.revealPending) {
    revealBoxes();
  }
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

/** Register every item, and draw an SVG box and id tag for the ones the model placed. */
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
    const box = Sheet.el("g", { class: item.unsure ? "obox is-unsure" : "obox" });
    box.classList.toggle("is-disagree", Boolean(item.disagree));
    // Hidden until the final sweep passes it, on a run that has just been read.
    box.classList.toggle("is-pending", state.revealPending);
    box.dataset.itemId = item.id;
    Sheet.el("rect", { class: "obox-frame", rx: 0, ry: 0 }, box);
    Sheet.el("rect", { class: "obox-tag" }, box);
    const tag = Sheet.el("text", { class: "obox-id" }, box);
    tag.textContent = item.id;
    const title = Sheet.el("title", {}, box);
    title.textContent = `${item.id}: ${item.title}`;
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
  const u = state.unit;
  for (const pair of linked.values()) {
    if (!pair.box) {
      continue;
    }
    const box = Sheet.toPixels(pair.bbox, state.imageW, state.imageH);
    const frame = pair.box.querySelector(".obox-frame");
    frame.setAttribute("x", String(box.x0));
    frame.setAttribute("y", String(box.y0));
    frame.setAttribute("width", String(Math.max(0, box.x1 - box.x0)));
    frame.setAttribute("height", String(Math.max(0, box.y1 - box.y0)));
    frame.setAttribute("rx", String(2 * u));

    // The id tag sits on top of the box's top-left corner, kept on the sheet.
    const label = pair.box.querySelector(".obox-id");
    const tagH = 11.5 * u;
    const tagW = label.textContent.length * 8.5 * u * 0.62 + 6 * u;
    const tagX = Math.min(box.x0, state.imageW - tagW);
    const tagY = Math.max(0, box.y0 - tagH);
    const tag = pair.box.querySelector(".obox-tag");
    tag.setAttribute("x", String(tagX));
    tag.setAttribute("y", String(tagY));
    tag.setAttribute("width", String(tagW));
    tag.setAttribute("height", String(tagH));
    tag.setAttribute("rx", String(2 * u));
    label.setAttribute("x", String(tagX + 3 * u));
    label.setAttribute("y", String(tagY + tagH - 3 * u));
    pair.centerY = (box.y0 + box.y1) / 2;
  }
}

/* ---- The reading scan ---------------------------------------------------- */

/*
 * While the model reads, a scan band loops down a stand-in sheet: the picked image when
 * it is one, a blank sheet for a PDF (the server renders the page; until it answers there
 * is no picture to show). When the reply lands, one last sweep runs over the real page and
 * each box appears as the line passes its centre.
 */

/** The stand-in sheet's size in pixels: the picked image's, or A-size landscape. */
const BLANK_SHEET = { w: 1414, h: 1000 };
let readingSize = BLANK_SHEET;
let readingScan = null;
let readingPreviewUrl = null;

function fitReadingSheet() {
  if (!readingSheet.hidden) {
    fitSheet(readingSheet, readingSize.w, readingSize.h);
  }
}

function startReadingScan(file) {
  stopReadingScan();
  readingSize = BLANK_SHEET;
  readingLabel.textContent = `Reading ${file.name}…`;
  readingPreview.hidden = true;
  readingPreview.removeAttribute("src");

  const draw = () => {
    const unit = sizeOverlay(readingOverlay, readingSize.w, readingSize.h);
    readingOverlay.replaceChildren();
    fitReadingSheet();
    if (Sheet.reducedMotion()) {
      return;
    }
    const band = Sheet.scanBand(readingOverlay, readingSize.w, readingSize.h, unit);
    readingOverlay.appendChild(band);
    if (readingScan) readingScan.stop();
    readingScan = Sheet.sweep(band, readingSize.h, 2400, { loop: true });
  };

  if (file.type && file.type.startsWith("image/")) {
    readingPreviewUrl = URL.createObjectURL(file);
    readingPreview.onload = () => {
      readingSize = { w: readingPreview.naturalWidth, h: readingPreview.naturalHeight };
      readingPreview.hidden = false;
      readingLabel.textContent = "";
      draw();
    };
    readingPreview.src = readingPreviewUrl;
  }
  draw();
}

function stopReadingScan() {
  if (readingScan) {
    readingScan.stop();
    readingScan = null;
  }
  readingOverlay.replaceChildren();
  if (readingPreviewUrl) {
    URL.revokeObjectURL(readingPreviewUrl);
    readingPreviewUrl = null;
  }
}

/** The final sweep over the real page, revealing each box as the line passes it. */
let revealScan = null;

function revealBoxes() {
  state.revealPending = false;
  const pending = Array.from(boxLayer.querySelectorAll(".obox.is-pending"));
  if (revealScan) revealScan.stop();
  scanLayer.replaceChildren();
  if (Sheet.reducedMotion()) {
    for (const box of pending) box.classList.remove("is-pending");
    return;
  }
  const band = Sheet.scanBand(overlay, state.imageW, state.imageH, state.unit);
  scanLayer.appendChild(band);
  const scan = Sheet.sweep(band, state.imageH, 1100, {
    onRow(y) {
      for (const box of pending) {
        const pair = linked.get(box.dataset.itemId);
        if (pair && pair.centerY <= y) box.classList.remove("is-pending");
      }
    },
  });
  revealScan = scan;
  scan.done.then(() => {
    for (const box of pending) box.classList.remove("is-pending");
    if (revealScan === scan) {
      scanLayer.replaceChildren();
      revealScan = null;
    }
  });
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
// A cloud goes to its issue card instead (see the Revision clouds section).
overlay.addEventListener("click", (event) => {
  const cloud = event.target.closest(".cloud");
  if (cloud) {
    openIssue(Number(cloud.dataset.number), { fromCloud: true });
    return;
  }
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

/** Show or fade the model's boxes. After a check they fade, and this brings them back. */
function setBoxesShown(shown) {
  overlayToggle.checked = shown;
  boxLayer.classList.toggle("is-faded", !shown);
}

overlayToggle.addEventListener("change", () => setBoxesShown(overlayToggle.checked));

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

/* ---- Cross-check --------------------------------------------------------- */

/*
 * With cross-check on, a second Meta model read the same page and the server compared
 * the two readings in Python. A cell the models read differently gets a "⇄" marker whose
 * tooltip gives both readings; a row only the first model read says so; the item's box
 * on the drawing is dashed. Like low confidence, this is a hint for the reviewer — the
 * rules run on the table values and never on the cross-check.
 */

/** The marker on a cell the two models read differently. Not the amber of low confidence. */
const DISAGREE_MARK = "⇄";
const ONE_SIDED_TEXT = "only one model read this";

/** Disagreement notes by "item:field", and the ids with any; rebuilt by `renderTables`. */
let crossNotes = new Map();
let disagreeIds = new Set();
let oneSidedIds = new Set();

function crossCheckResult() {
  return state.crossCheck && state.crossCheck.status === "done" ? state.crossCheck : null;
}

function indexCrossCheck() {
  crossNotes = new Map();
  disagreeIds = new Set();
  oneSidedIds = new Set();
  const result = crossCheckResult();
  if (!result) {
    return;
  }
  for (const item of result.disagreements || []) {
    crossNotes.set(`${item.item_id}:${item.field}`, item.note);
    disagreeIds.add(item.item_id);
  }
  for (const item of result.only_primary || []) {
    oneSidedIds.add(item.item_id);
  }
}

/** Mark `cell` if the models disagree on any of `fields` of item `id`. Returns the cell. */
function markCrossCheck(cell, id, fields) {
  const notes = fields.map((field) => crossNotes.get(`${id}:${field}`)).filter(Boolean);
  cell.classList.toggle("is-disagree", notes.length > 0);
  if (notes.length > 0) {
    const mark = document.createElement("span");
    mark.className = "xcheck";
    mark.textContent = DISAGREE_MARK;
    mark.title = notes.join(" ");
    mark.setAttribute("aria-label", `The two models disagree. ${notes.join(" ")}`);
    cell.appendChild(mark);
  }
  return cell;
}

function oneSidedMarker(id) {
  if (!id || !oneSidedIds.has(id)) {
    return null;
  }
  const mark = document.createElement("span");
  mark.className = "xcheck-only";
  mark.textContent = ONE_SIDED_TEXT;
  mark.title = "Muse Spark Open read this item; the cross-check model did not find it.";
  return mark;
}

/** Where a disagreement about the whole drawing is shown, by field. */
const DRAWING_CELLS = { units: "v-units", general_tolerance: "v-gentol" };

function goToFirstDisagreement() {
  const result = crossCheckResult();
  const first = result && (result.disagreements || [])[0];
  if (!first) {
    return;
  }
  const pair = linked.get(first.item_id);
  if (pair && pair.row) {
    pick([first.item_id], pair.row);
    return;
  }
  const cell = $(DRAWING_CELLS[first.field] || "v-units");
  if (cell) {
    cell.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

function renderCrossCheckSummary() {
  const outcome = state.crossCheck;
  if (!outcome) {
    crossCheckSummary.hidden = true;
    crossCheckSummary.replaceChildren();
    return;
  }
  crossCheckSummary.hidden = false;
  const flagged = outcome.status === "done" && (outcome.disagreements || []).length > 0;
  if (flagged) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "crosscheck-link";
    button.textContent = `${DISAGREE_MARK} ${outcome.summary}`;
    button.title = "Show the first value the two models read differently";
    button.addEventListener("click", goToFirstDisagreement);
    crossCheckSummary.className = "crosscheck-summary is-flagged";
    crossCheckSummary.replaceChildren(button);
    return;
  }
  const quiet = outcome.status !== "done";
  crossCheckSummary.className = `crosscheck-summary ${quiet ? "is-quiet" : "is-clear"}`;
  crossCheckSummary.replaceChildren(outcome.summary);
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
    const oneSided = oneSidedMarker(id);
    if (oneSided) {
      row.classList.add("is-one-sided");
      cells[0].appendChild(oneSided);
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
  markCrossCheck($("v-units"), "drawing", ["units"]);

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
  markCrossCheck($("v-gentol"), "drawing", ["general_tolerance"]);

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

    const datumNote = explainNote("datums", datum.label);
    addExplainLine(labelCell, datumNote && `${capitalize(datumNote.role)}: ${datumNote.why}`);

    return {
      id: datum.id,
      cells: [
        idCell(datum.id),
        markCrossCheck(labelCell, datum.id, ["label"]),
        zoneCell(datum),
        confidenceCell(datum),
        removeCell,
      ],
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
        printedWithNote(dimension.text_raw, explainNote("dimensions", id), (note) =>
          `${capitalize(note.kind)}: ${note.purpose}${whereSeen(note)}`),
        markCrossCheck(
          numberCell(dimension, "value", was.value ?? null, { required: true, label: `Value, ${id}` }),
          id,
          ["value"],
        ),
        markCrossCheck(
          numberCell(dimension, "tol_plus", was.tol_plus ?? null, {
            required: false,
            min: 0,
            label: `Plus tolerance, ${id}`,
          }),
          id,
          ["tol_plus"],
        ),
        markCrossCheck(
          numberCell(dimension, "tol_minus", was.tol_minus ?? null, {
            required: false,
            min: 0,
            label: `Minus tolerance, ${id}`,
          }),
          id,
          ["tol_minus"],
        ),
        markCrossCheck(editCell(basic, {
          read: () => ({ ok: true, value: basic.checked }),
          apply: (value) => {
            dimension.is_basic = value;
          },
          was: Boolean(was.is_basic),
          shown: was.is_basic ? "yes" : "no",
          mono: false,
        }), id, ["is_basic"]),
        markCrossCheck(textCell(fromTo(dimension), "mono"), id, ["from_ref", "to_ref"]),
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
        markCrossCheck(
          printedWithNote(fcf.characteristic, explainNote("fcfs", id), (note) => note.meaning, ""),
          id,
          ["characteristic"],
        ),
        markCrossCheck(
          numberCell(fcf, "tolerance", was.tolerance ?? null, {
            required: true,
            min: 0,
            label: `Tolerance, ${id}`,
          }),
          id,
          ["tolerance"],
        ),
        markCrossCheck(editCell(modifier, {
          read: () => ({ ok: true, value: modifier.value || null }),
          apply: (value) => {
            fcf.material_modifier = value;
          },
          was: was.material_modifier || null,
          shown: text(was.material_modifier),
        }), id, ["material_modifier"]),
        markCrossCheck(editCell(refs, {
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
        }), id, ["datum_refs"]),
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
  indexCrossCheck();

  buildOverlay([
    ...(state.working.datums || []).map((datum) => ({
      id: datum.id,
      bbox: datum.bbox,
      title: `Datum ${text(datum.label)}`,
      unsure: isUnsure(datum),
      disagree: disagreeIds.has(datum.id),
    })),
    ...(state.working.dimensions || []).map((dimension) => ({
      id: dimension.id,
      bbox: dimension.bbox,
      title: `Dimension ${text(dimension.text_raw)}`,
      unsure: isUnsure(dimension),
      disagree: disagreeIds.has(dimension.id),
    })),
    ...(state.working.fcfs || []).map((fcf) => ({
      id: fcf.id,
      bbox: fcf.bbox,
      title: `Frame ${text(fcf.characteristic)}`,
      unsure: isUnsure(fcf),
      disagree: disagreeIds.has(fcf.id),
    })),
  ]);

  renderHeader();
  renderDatums();
  renderDimensions();
  renderFcfs();
  renderConfidenceSummary();
  renderCrossCheckSummary();
  paintAiLabels();
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
  state.findings = [];
  issueCards.clear();
  numberByKey.clear();
  selectedIssue = null;
  clearAgent();
  cloudLayer.replaceChildren();
  clouds.clear();
  roundsStrip.hidden = true;
  checksList.replaceChildren();
  checksBox.hidden = true;
  checks.submitted = null;
  checks.after = null;
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

/* ---- Issues: cards and revision clouds ----------------------------------- */

/*
 * Finding N is card N, cloud N, delta N, and marker N on the redline PDF: all numbered in
 * the order the server's rules returned them. A cloud and its card select each other.
 */

/** number -> card <li>, and number -> cloud <g>. Rebuilt per check. */
const issueCards = new Map();
const clouds = new Map();
/** finding key ("GDT-001@fcf_1") -> its number. */
const numberByKey = new Map();

let selectedIssue = null;

function cloudLabel(finding, number) {
  return `Issue ${number}: ${finding.title || finding.rule_id}. Open its card.`;
}

/** Select issue `number`: its card, its cloud, and the items it is about. */
function selectIssue(number) {
  const card = issueCards.get(number);
  if (!card) {
    return;
  }
  for (const [other, otherCard] of issueCards) {
    const on = other === number;
    otherCard.classList.toggle("is-active", on);
    if (on) {
      otherCard.setAttribute("aria-current", "true");
    } else {
      otherCard.removeAttribute("aria-current");
    }
  }
  for (const [other, cloud] of clouds) {
    cloud.classList.toggle("is-selected", other === number);
  }
  selectedIssue = number;
  const finding = state.findings[number - 1];
  pick(finding && Array.isArray(finding.item_ids) ? finding.item_ids : [], null);
}

/** Open the Issues tab on issue `number`, as clicking its cloud does. */
function openIssue(number, { fromCloud = false } = {}) {
  if (!issueCards.has(number)) {
    return;
  }
  selectTab("issues");
  selectIssue(number);
  if (fromCloud) {
    issueCards.get(number).scrollIntoView({ block: "nearest", behavior: Sheet.reducedMotion() ? "auto" : "smooth" });
  }
}

overlay.addEventListener("keydown", (event) => {
  const cloud = event.target.closest(".cloud");
  if (cloud && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    openIssue(Number(cloud.dataset.number), { fromCloud: true });
  }
});

/** One finding card: delta, title, rule and zone, severity, why, source, attempts. */
function findingCard(finding, number) {
  const severity = finding.severity === "error" ? "error" : "warning";
  const item = document.createElement("li");
  item.className = `finding sev-${severity}`;
  item.tabIndex = 0;
  item.dataset.number = String(number);

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

  // Filled round by round as the correction loop's reply is replayed.
  const attempts = document.createElement("ol");
  attempts.className = "attempts";
  attempts.hidden = true;

  const notes = document.createElement("div");
  notes.className = "finding-ai";
  fillFindingNotes(notes, finding);

  item.append(delta, title, chip, meta, why, source, notes, attempts);
  item.setAttribute("aria-label", `Issue ${number}, ${severity}: ${title.textContent}. ${meta.textContent}`);

  item.addEventListener("click", () => selectIssue(number));
  item.addEventListener("keydown", (event) => {
    if (event.target === item && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      selectIssue(number);
    }
  });
  return item;
}

/** Draw a revision cloud around every finding the rules could place. */
function drawClouds() {
  cloudLayer.replaceChildren();
  clouds.clear();
  if (!state.imageReady) {
    return;
  }
  const filter = Sheet.ensureRoughFilter(overlay, state.unit);
  let order = 0;
  state.findings.forEach((finding, index) => {
    const box = Sheet.toPixels(finding.bbox, state.imageW, state.imageH);
    if (!box) {
      return;
    }
    const number = index + 1;
    const cloud = Sheet.cloud({
      box,
      number,
      label: cloudLabel(finding, number),
      unit: state.unit,
      width: state.imageW,
      height: state.imageH,
      order,
      filter,
    });
    cloud.classList.add("st-open");
    cloud.classList.toggle("is-selected", number === selectedIssue);
    cloudLayer.appendChild(cloud);
    clouds.set(number, cloud);
    order += 1;
  });
}

/** Colour each cloud by its finding's outcome: open, verified, held, or unresolved. */
function colourClouds(outcomes) {
  for (const [number, cloud] of clouds) {
    const finding = state.findings[number - 1];
    const outcome = (finding && outcomes.get(finding.key)) || "open";
    for (const name of ["st-open", "st-verified", "st-held", "st-unresolved"]) {
      cloud.classList.toggle(name, name === `st-${outcome}`);
    }
  }
}

function renderFindings(result) {
  const findings = result.findings || [];
  const issues = result.parse_issues || [];

  state.findings = findings;
  state.rulesRun = typeof result.rules_run === "number" ? result.rules_run : null;
  issueCards.clear();
  numberByKey.clear();
  selectedIssue = null;

  const cards = document.createDocumentFragment();
  findings.forEach((finding, index) => {
    const card = findingCard(finding, index + 1);
    issueCards.set(index + 1, card);
    if (finding.key) numberByKey.set(finding.key, index + 1);
    cards.appendChild(card);
  });

  const lines = document.createDocumentFragment();
  for (const issue of issues) {
    const line = document.createElement("li");
    line.textContent = issue;
    lines.appendChild(line);
  }

  findingsList.replaceChildren(cards);
  checkIssuesList.replaceChildren(lines);
  paintAiLabels();
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

  // The model's boxes give way to the checker's clouds; "Show boxes" brings them back.
  setBoxesShown(false);
  drawClouds();
  resetRoundsStrip();
  renderChecks(result);
}

/* ---- Checks: every rule's verdict on the whole drawing -------------------- */

/*
 * The server decides each status and writes each sentence; this only lays them out.
 * A failed check is a button that opens its first issue, card and cloud together.
 */

const CHECK_ICONS = {
  pass: { icon: "✓", word: "passed", className: "is-pass" },
  fail: { icon: "✗", word: "failed", className: "is-fail" },
  not_applicable: { icon: "–", word: "not applicable", className: "is-na" },
};

function checkRow(result) {
  const look = CHECK_ICONS[result.status] || CHECK_ICONS.not_applicable;
  const issues = Array.isArray(result.issues) ? result.issues : [];
  const first = result.status === "fail" ? issues.find((number) => issueCards.has(number)) : undefined;

  const row = document.createElement(first ? "button" : "div");
  row.className = `check ${look.className}`;
  if (first) {
    row.type = "button";
    row.addEventListener("click", () => {
      openIssue(first);
      issueCards.get(first).scrollIntoView({ block: "nearest", behavior: Sheet.reducedMotion() ? "auto" : "smooth" });
    });
  }

  const icon = document.createElement("span");
  icon.className = "check-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = look.icon;

  const name = document.createElement("p");
  name.className = "check-name";
  const id = document.createElement("span");
  id.className = "check-id";
  id.textContent = text(result.rule_id);
  name.append(id, document.createTextNode(text(result.title)));

  const why = document.createElement("p");
  why.className = "check-why";
  why.textContent = text(result.explanation);

  row.append(icon, name, why);
  const label = `${text(result.rule_id)} ${text(result.title)}: ${look.word}. ${text(result.explanation)}`;
  row.setAttribute("aria-label", first ? `${label} Open issue ${first}.` : label);

  const item = document.createElement("li");
  item.appendChild(row);
  return item;
}

/*
 * Two sets of results once a fix has run: the drawing as submitted (from /check) and the
 * corrected drawing (from /fix, where the server re-ran the rules on it). The toggle picks
 * which one the list shows; both summary lines stay visible.
 */
const SUBMITTED_LEAD = "On the submitted drawing";
const AFTER_FIXES_LEAD = "After the agent's verified fixes";

const checks = { submitted: null, after: null, view: "submitted" };

function checkSet(results, summary) {
  return { results: Array.isArray(results) ? results : [], summary: text(summary) };
}

function paintChecks() {
  const shown = checks.view === "after" && checks.after ? checks.after : checks.submitted;
  if (!shown) {
    checksBox.hidden = true;
    return;
  }
  const rows = document.createDocumentFragment();
  for (const ruleResult of shown.results) {
    rows.appendChild(checkRow(ruleResult));
  }
  checksList.replaceChildren(rows);
  checksList.dataset.view = checks.after ? checks.view : "submitted";

  const both = Boolean(checks.after);
  checksSummary.textContent = both ? `${SUBMITTED_LEAD}: ${checks.submitted.summary}` : checks.submitted.summary;
  checksSummaryAfter.textContent = both ? `${AFTER_FIXES_LEAD}: ${checks.after.summary}` : "";
  checksSummaryAfter.hidden = !both;
  checksSummary.classList.toggle("is-shown", both && checks.view === "submitted");
  checksSummaryAfter.classList.toggle("is-shown", both && checks.view === "after");
  checksToggle.hidden = !both;
  for (const button of checksToggle.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === checks.view));
  }
  checksBox.hidden = shown.results.length === 0;
}

/** After /check: the submitted drawing's results, and no after-fixes set yet. */
function renderChecks(result) {
  checks.submitted = checkSet(result.rule_results, result.checks_summary);
  checks.after = null;
  checks.view = "submitted";
  paintChecks();
}

/** After /fix: add the corrected drawing's results and show them by default. */
function renderFixChecks(result) {
  if (!checks.submitted || !Array.isArray(result.final_rule_results)) {
    return;
  }
  checks.after = checkSet(result.final_rule_results, result.final_checks_summary);
  checks.view = "after";
  paintChecks();
}

checksToggle.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (!button || !checks.after) {
    return;
  }
  checks.view = button.dataset.view;
  paintChecks();
});

/* ---- The rounds strip ----------------------------------------------------- */

const roundCells = Array.from(roundsStrip.querySelectorAll(".round-cell"));

/** Set one cell: `phase` is pending, running, done, or skipped. */
function setRoundCell(index, { number = "–", sub = "Waiting", phase = "pending", title = "" } = {}) {
  const cell = roundCells[index];
  if (!cell) {
    return;
  }
  cell.dataset.phase = phase;
  cell.querySelector(".strip-number").textContent = String(number);
  cell.querySelector(".strip-sub").textContent = sub;
  cell.title = title;
}

/** After a check: what was found, and every round still to come. */
function resetRoundsStrip() {
  roundsStrip.hidden = !state.checked;
  const ran = state.rulesRun === null ? "Rules ran" : `${plural(state.rulesRun, "rule")} ran`;
  setRoundCell(0, {
    number: state.findings.length,
    sub: ran,
    phase: "done",
    title: `${plural(state.findings.length, "finding")} on the confirmed tables`,
  });
  for (let index = 1; index <= MAX_ROUNDS; index += 1) {
    setRoundCell(index);
  }
}

/** Every edit of one round: kept, rolled back by the rules, and refused by the guards. */
function roundEdits(round) {
  return [...(round.applied || []), ...(round.rolled_back || []), ...(round.rejected || [])];
}

/** "1 verified, 1 rejected": what the rule engine made of one round's edits. */
function roundSummaryText(round) {
  if (round.error) {
    return "Could not run";
  }
  const edits = roundEdits(round);
  if (edits.length === 0) {
    return "No edits proposed";
  }
  const count = (status) => edits.filter((edit) => edit.status === status).length;
  const parts = [`${count("verified")} verified`];
  for (const [status, word] of [["held", "held"], ["failed", "rolled back"], ["rejected", "rejected"]]) {
    if (count(status) > 0) parts.push(`${count(status)} ${word}`);
  }
  return parts.join(", ");
}

/* ---- Attempts, in each issue card ----------------------------------------- */

const VERDICT_CHIPS = {
  verified: "✓ Rule engine: pass",
  held: "! pass, held for designer",
  failed: "✕ Rule engine: fail · rolled back",
  rejected: "✕ Rule engine: fail",
  pending: "… Rule engine: re-checking",
};

function paragraph(className, value) {
  const line = document.createElement("p");
  line.className = className;
  line.textContent = value;
  return line;
}

/** One round's proposal for one issue: the diff, the verdict, and Python's note. */
function attemptItem(round, edit) {
  const item = document.createElement("li");
  item.className = `attempt st-${edit.status}`;

  const head = document.createElement("p");
  head.className = "attempt-head";
  const label = document.createElement("span");
  label.className = "attempt-round";
  label.textContent = `Round ${round.round} proposal`;
  const sentence = document.createElement("span");
  sentence.className = "attempt-edit";
  sentence.textContent = edit.edit;
  head.append(label, sentence);
  if (round.fell_back) {
    const fallback = document.createElement("span");
    fallback.className = "attempt-fallback";
    fallback.textContent = `answered by ${round.model || "the fallback model"} (fallback)`;
    head.append(fallback);
  }
  item.appendChild(head);

  if (edit.before !== null && edit.after !== null && edit.before !== undefined) {
    const diff = document.createElement("div");
    diff.className = "diff";
    const before = document.createElement("p");
    before.className = "diff-line diff-before";
    const was = document.createElement("del");
    was.textContent = edit.before;
    before.append(was);
    const after = document.createElement("p");
    after.className = "diff-line diff-after";
    // Refused, or rolled back after the re-run: either way it is not on the drawing.
    const offSheet = edit.status === "rejected" || edit.status === "failed";
    const now = document.createElement(offSheet ? "del" : "ins");
    now.textContent = edit.after;
    after.append(now);
    diff.append(before, after);
    const afterWord = edit.status === "rejected" ? "Proposed, refused" : edit.status === "failed" ? "Tried, rolled back" : "After";
    diff.setAttribute("aria-label", `Before: ${edit.before}. ${afterWord}: ${edit.after}.`);
    item.appendChild(diff);
  }

  const verdict = document.createElement("p");
  verdict.className = "attempt-verdict";
  const chip = document.createElement("span");
  chip.className = "verdict-chip";
  chip.textContent = VERDICT_CHIPS[edit.status] || VERDICT_CHIPS.failed;
  verdict.append(chip);
  if (edit.note) {
    const note = document.createElement("span");
    note.className = "verdict-note";
    note.textContent = edit.note;
    verdict.append(note);
  }
  item.appendChild(verdict);
  return item;
}

/** Proposals that cite no issue on the list still get shown, in a card of their own. */
function otherProposalsCard() {
  let card = findingsList.querySelector(".finding.is-other");
  if (!card) {
    card = document.createElement("li");
    card.className = "finding is-other";
    card.appendChild(paragraph("finding-title", "Proposals not tied to an issue"));
    const attempts = document.createElement("ol");
    attempts.className = "attempts";
    card.appendChild(attempts);
    findingsList.appendChild(card);
  }
  return card;
}

/** Add one round's edits to the cards of the issues they cite. */
function showRoundAttempts(round) {
  const edits = roundEdits(round);
  for (const edit of edits) {
    const numbers = (edit.finding_ids || []).map((key) => numberByKey.get(key)).filter(Boolean);
    const cards = numbers.length ? numbers.map((number) => issueCards.get(number)) : [otherProposalsCard()];
    for (const card of cards) {
      const list = card.querySelector(".attempts");
      list.hidden = false;
      list.appendChild(attemptItem(round, edit));
    }
  }
  if (round.error) {
    const card = otherProposalsCard();
    card.querySelector(".finding-title").textContent = "Rounds that could not run";
    const list = card.querySelector(".attempts");
    list.appendChild(paragraph("round-error", `Round ${round.round} could not run: ${round.error}`));
  }
}

/* ---- Corrections ---------------------------------------------------------- */

/** Bumped whenever a replay should stop: a new check, a new run, or a new fix. */
let replayToken = 0;
let replaying = false;
const ROUND_REPLAY_MS = 600;

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function clearSheetMarkup() {
  penLayer.replaceChildren();
  stampLayer.replaceChildren();
  penNotes = [];
  penSlots = new Map();
}

function clearAgent() {
  replayToken += 1;
  replaying = false;
  state.fixedAt = null;
  state.agentStale = false;
  state.fix = null;
  state.fixResult = null;
  state.exported = false;
  agentStatus.hidden = true;
  correctionsRows.replaceChildren();
  correctionsIntro.textContent = "";
  clearSheetMarkup();
  colourClouds(new Map());
  for (const list of findingsList.querySelectorAll(".attempts")) {
    list.replaceChildren();
    list.hidden = true;
  }
  const other = findingsList.querySelector(".finding.is-other");
  if (other) other.remove();
  if (state.checked) resetRoundsStrip();
  // The after-fixes checks belonged to that fix; back to the submitted drawing's.
  if (checks.after) {
    checks.after = null;
    checks.view = "submitted";
    paintChecks();
  }
}

/** Every edit the loop proposed that has something to write on the sheet, in order. */
function sheetEdits(result) {
  const edits = [];
  for (const round of result.rounds || []) {
    for (const edit of roundEdits(round)) {
      if (edit.pen) edits.push(edit);
    }
  }
  return edits;
}

/* Pen notes: one per edit, beside the cloud of the issue it cites. */
let penNotes = [];
/** How many notes already sit beside each anchor, so the next one goes below them. */
let penSlots = new Map();

function noteAnchor(edit) {
  for (const key of edit.finding_ids || []) {
    const cloud = clouds.get(numberByKey.get(key));
    if (cloud) return cloud.ring;
  }
  const pair = edit.target_id ? linked.get(edit.target_id) : null;
  return pair && pair.bbox ? Sheet.toPixels(pair.bbox, state.imageW, state.imageH) : null;
}

/** Rolled-back edits are written like refused ones: red, struck through — not on the drawing. */
const NOTE_STATUS = { verified: "verified", held: "held", failed: "rejected", rejected: "rejected" };

function drawPenNotes(result) {
  clearSheetMarkup();
  return addPenNotes(sheetEdits(result));
}

/**
 * Write notes for `edits` beside the ones already on the sheet. A live round adds its own
 * this way when its verdict arrives, so earlier rounds' notes are not written again.
 */
function addPenNotes(edits) {
  if (edits.length === 0 || !state.imageReady) {
    return 0;
  }
  const font = Sheet.loadPenFont();
  const filter = Sheet.ensureRoughFilter(overlay, state.unit);
  let order = 0;
  for (const edit of edits) {
    const anchor = noteAnchor(edit);
    if (!anchor) continue;
    const key = `${anchor.x0},${anchor.y0}`;
    const slot = penSlots.get(key) || 0;
    penSlots.set(key, slot + 1);
    const group = Sheet.note({ text: edit.pen, status: NOTE_STATUS[edit.status] || "held", order, filter });
    penLayer.appendChild(group);
    penNotes.push({ group, anchor, slot });
    order += 1;
  }
  layoutPenNotes();
  // The handwriting font arrives after the first note is drawn; measure again when it does.
  font.then(layoutPenNotes);
  return order;
}

function layoutPenNotes() {
  for (const { group, anchor, slot } of penNotes) {
    Sheet.placeNote(group, anchor, slot, state.unit, state.imageW, state.imageH);
  }
}

/** A local date, as the stamp prints it: 2026-11-03. */
function stampDate(when) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
}

function outcomeCounts(result) {
  const statuses = result.status || [];
  const verified = statuses.filter((entry) => entry.outcome === "verified").length;
  return { verified, designer: statuses.length - verified };
}

function drawStamp(result) {
  stampLayer.replaceChildren();
  if (!state.imageReady) {
    return;
  }
  const avoid = [
    ...Array.from(linked.values())
      .map((pair) => (pair.bbox ? Sheet.toPixels(pair.bbox, state.imageW, state.imageH) : null))
      .filter(Boolean),
    ...Array.from(clouds.values()).map((cloud) => cloud.bounds),
    ...penNotes.map((entry) => entry.group.bounds).filter(Boolean),
  ];
  const spot = Sheet.stampSpot({
    width: state.imageW,
    height: state.imageH,
    unit: state.unit,
    avoid,
    image: pageImage,
  });
  const { verified, designer } = outcomeCounts(result);
  stampLayer.appendChild(
    Sheet.stamp({
      cx: spot.cx,
      cy: spot.cy,
      unit: state.unit,
      date: stampDate(state.fixedAt || new Date()),
      verified,
      designer,
    }),
  );
}

const OUTCOME_WORDS = {
  verified: "Verified",
  held: "Held for designer",
  unresolved: "Unresolved",
  failed: "Failed, rolled back",
};

/** The last applied edit that cites `key`, or null. */
function lastAppliedFor(result, key) {
  let found = null;
  for (const round of result.rounds || []) {
    for (const edit of round.applied || []) {
      if ((edit.finding_ids || []).includes(key)) found = edit;
    }
  }
  return found;
}

/** One row per original finding, plus any applied edit that cites none. */
function correctionRows(result) {
  const rows = [];
  for (const entry of result.status || []) {
    const number = numberByKey.get(entry.finding) || null;
    const edit = lastAppliedFor(result, entry.finding);
    rows.push({
      number,
      finding: number ? state.findings[number - 1] : null,
      before: edit && edit.before !== null ? edit.before : EM_DASH,
      after: edit && edit.after !== null ? edit.after : EM_DASH,
      outcome: entry.outcome || (entry.status === "resolved" ? "verified" : "unresolved"),
    });
  }
  // Every edit the rules rolled back gets its own row, under the issue it tried to fix.
  for (const round of result.rounds || []) {
    for (const edit of round.rolled_back || []) {
      const number = (edit.finding_ids || []).map((key) => numberByKey.get(key)).find(Boolean) || null;
      rows.push({
        number,
        finding: number ? state.findings[number - 1] : null,
        before: edit.before ?? EM_DASH,
        after: edit.after ?? EM_DASH,
        outcome: "failed",
      });
    }
  }
  for (const round of result.rounds || []) {
    for (const edit of round.applied || []) {
      const cited = (edit.finding_ids || []).some((key) => numberByKey.has(key));
      if (!cited) {
        rows.push({
          number: null,
          finding: null,
          before: edit.before ?? EM_DASH,
          after: edit.after ?? EM_DASH,
          outcome: edit.status,
        });
      }
    }
  }
  return rows;
}

function renderCorrections(result) {
  const { verified, designer } = outcomeCounts(result);
  const passed = `${plural(verified, "correction")} passed the rule engine.`;
  const need = designer === 1 ? "1 needs a designer." : `${designer} need a designer.`;
  correctionsIntro.textContent = `${passed} ${need}`;

  const body = document.createDocumentFragment();
  for (const row of correctionRows(result)) {
    const line = document.createElement("tr");
    const delta = document.createElement("td");
    if (row.number) {
      const mark = document.createElement("span");
      mark.className = `delta delta-small st-${row.outcome}`;
      mark.textContent = String(row.number);
      delta.appendChild(mark);
    } else {
      delta.textContent = EM_DASH;
    }
    const before = document.createElement("td");
    before.className = "mono";
    before.textContent = row.before;
    const after = document.createElement("td");
    after.className = "mono";
    after.textContent = row.after;
    const status = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = `status-chip st-${row.outcome}`;
    chip.textContent = OUTCOME_WORDS[row.outcome] || row.outcome;
    status.appendChild(chip);
    line.append(delta, before, after, status);
    body.appendChild(line);
  }
  correctionsRows.replaceChildren(body);
}

/* The correction list as CSV, built here from the replies already on the page. */

function csvCell(value) {
  let cell = value === null || value === undefined ? "" : String(value);
  // A cell that a spreadsheet would read as a formula is written as text instead: these
  // strings come from a model reading an uploaded file.
  if (/^[=+\-@\t\r]/.test(cell)) cell = `'${cell}`;
  return /[",\n\r]/.test(cell) ? `"${cell.replace(/"/g, '""')}"` : cell;
}

function correctionsCsv(result) {
  const lines = [["issue", "rule", "zone", "severity", "before", "after", "status", "source"]];
  for (const row of correctionRows(result)) {
    const finding = row.finding;
    lines.push([
      row.number || "",
      finding ? finding.rule_id : "",
      finding ? findingZone(finding) || "" : "",
      finding ? finding.severity : "",
      row.before,
      row.after,
      OUTCOME_WORDS[row.outcome] || row.outcome,
      finding ? finding.source : "",
    ]);
  }
  return `${lines.map((line) => line.map(csvCell).join(",")).join("\r\n")}\r\n`;
}

exportCsv.addEventListener("click", () => {
  if (!state.fixResult) {
    return;
  }
  // The byte-order mark tells Excel the file is UTF-8, so ⌖ and Ø survive the trip.
  const blob = new Blob(["﻿", correctionsCsv(state.fixResult)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "corrections.csv";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast("Downloading the correction list.");
});

/**
 * Show one correction run, from /fix's final reply.
 *
 * Streamed (`live`), the rounds have already been drawn as they happened, so this only
 * settles the page on the final reply — the same rounds, drawn again at once — gives the
 * clouds their final colours, and puts the stamp down. Not streamed (the fallback, when
 * the stream failed), every round arrives at once and is replayed on screen one by one.
 * With reduced motion all of it lands at once.
 */
async function renderAgent(result, { live = false } = {}) {
  const token = ++replayToken;
  const rounds = result.rounds || [];
  const still = live || Sheet.reducedMotion();
  replaying = !still;

  const stamp = Date.now();
  redlinePdf.href = `${result.redline_pdf_url}?t=${stamp}`;

  resetRoundsStrip();
  for (const list of findingsList.querySelectorAll(".attempts")) {
    list.replaceChildren();
    list.hidden = true;
  }
  for (let index = 0; index < rounds.length; index += 1) {
    const round = rounds[index];
    if (!still) {
      setRoundCell(index + 1, { number: "–", sub: "Running", phase: "running" });
      await wait(ROUND_REPLAY_MS);
      if (token !== replayToken) return;
    }
    setRoundCell(index + 1, {
      number: round.findings_after,
      sub: roundSummaryText(round),
      phase: "done",
      title: round.error ? round.error : `${plural(round.findings_after, "finding")} left after round ${round.round}`,
    });
    showRoundAttempts(round);
  }
  for (let index = rounds.length + 1; index <= MAX_ROUNDS; index += 1) {
    setRoundCell(index, { sub: "Not needed", phase: "skipped" });
  }

  const finalFindings = result.final_findings || [];
  const statuses = result.status || [];
  state.fixResult = result;
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
  replaying = false;
  setAgentStatus();
  renderCorrections(result);
  renderFixChecks(result);

  colourClouds(new Map(statuses.map((entry) => [entry.finding, entry.outcome || "unresolved"])));
  // Live, each round wrote its own notes as its verdict came in; they stay as they are.
  const notes = live && penNotes.length > 0 ? 0 : drawPenNotes(result);
  updateUI();

  // The stamp lands once the last note has been written (reveal 0.55 s, 260 ms apart).
  if (!still && notes > 0) {
    await wait((notes - 1) * 260 + 700);
    if (token !== replayToken) return;
  }
  drawStamp(result);
}

/* ---- AI explanations for learners ---------------------------------------- */

/*
 * A teaching aid, written by the model and validated on the server against the drawing's
 * real ids. It is display only: nothing here is posted to the rules, and every section
 * that shows one is labelled "AI explanation · not checked by the rules".
 */

function capitalize(value) {
  const word = String(value || "");
  return word ? word[0].toUpperCase() + word.slice(1) : word;
}

/** The explanation's entry for one item, by id (or datum label, or finding key). */
function explainNote(section, name) {
  const explanation = state.explanation;
  if (!explanation || !name) return null;
  const field = section === "datums" ? "label" : section === "findings" ? "key" : "id";
  return (explanation[section] || []).find((entry) => entry[field] === name) || null;
}

/** Add a muted second line to a cell, under what is printed there. */
function addExplainLine(cell, line) {
  if (!line) return;
  const note = document.createElement("span");
  note.className = "ai-line";
  note.textContent = line;
  cell.appendChild(note);
}

/** A cell with the printed text and, when there is an explanation, its line beneath. */
function printedWithNote(printed, note, lineOf, className = "mono") {
  const cell = document.createElement("td");
  const main = document.createElement("span");
  main.className = className;
  main.textContent = text(printed);
  cell.appendChild(main);
  addExplainLine(cell, note && lineOf(note));
  return cell;
}

/** " · in front view, also seen in top view" for a dimension note, or "" with no view. */
function whereSeen(note) {
  if (!note.view) return "";
  const named = (name) => `${name} view`;
  const also = (note.also_seen_in || []).map(named);
  const tail = also.length ? `, also seen in ${joinWords(also)}` : "";
  return ` · in ${named(note.view)}${tail}`;
}

/** ["a", "b", "c"] -> "a, b and c". */
function joinWords(words) {
  if (words.length < 2) return words.join("");
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

/** The chip for each part type the explanation can give (prompts.PART_TYPES). */
const PART_TYPE_LABELS = {
  plate: "Plate",
  bracket: "Bracket",
  flange: "Flange",
  shaft: "Shaft",
  gear: "Gear",
  housing: "Housing",
  cover: "Cover",
  spacer: "Spacer",
  block: "Block",
  fastener: "Fastener",
  assembly: "Assembly",
  other: "Other",
};

/** How the page names each table kind and projection the explanation can give. */
const TABLE_LABELS = {
  parts_list: "Parts list",
  hole_table: "Hole table",
  revision: "Revision block",
  notes: "Notes",
  other: "Other table",
};
const PROJECTION_LABELS = {
  "first-angle": "First-angle",
  "third-angle": "Third-angle",
  unclear: "Projection unclear",
};

/** One "<strong>Lead:</strong> text" list item. */
function leadItem(lead, rest) {
  const item = document.createElement("li");
  const strong = document.createElement("strong");
  strong.textContent = `${lead}: `;
  item.append(strong, rest);
  return item;
}

/** The Views, Tables, and Balloons rows of the About card. */
function renderAboutRows(explanation) {
  const views = (explanation && explanation.views) || [];
  const viewItems = document.createDocumentFragment();
  for (const view of views) {
    viewItems.appendChild(leadItem(view.label || `${capitalize(view.name)} view`, view.shows));
  }
  $("about-views").replaceChildren(viewItems);
  const projection = (explanation && explanation.projection) || "unclear";
  const note = explanation && explanation.projection_note;
  $("about-projection-chip").textContent = PROJECTION_LABELS[projection] || PROJECTION_LABELS.unclear;
  $("about-projection-note").textContent = note || "";
  $("about-projection").hidden = !note && projection === "unclear";
  $("about-views-row").hidden = views.length === 0 && !note;

  const tables = (explanation && explanation.tables) || [];
  const tableItems = document.createDocumentFragment();
  for (const table of tables) {
    tableItems.appendChild(leadItem(TABLE_LABELS[table.kind] || TABLE_LABELS.other, table.summary));
  }
  $("about-tables").replaceChildren(tableItems);
  $("about-tables-row").hidden = tables.length === 0;

  const balloons = explanation && explanation.balloons;
  $("about-balloons").textContent = balloons || "";
  $("about-balloons-row").hidden = !balloons;
}

/** "Why it matters" and "How to think about fixing" on an issue card, when explained. */
function fillFindingNotes(container, finding) {
  const note = explainNote("findings", finding.key);
  container.replaceChildren();
  container.hidden = !note;
  if (!note) return;
  for (const [label, value] of [
    ["Why it matters", note.why_it_matters],
    ["How to think about fixing", note.how_to_think_about_fixing],
  ]) {
    const line = document.createElement("p");
    line.className = "finding-ai-line";
    const strong = document.createElement("strong");
    strong.textContent = `${label}: `;
    line.append(strong, value);
    container.appendChild(line);
  }
}

/** Show each section's one label only when that section has an explanation in it. */
function paintAiLabels() {
  const explanation = state.explanation;
  const has = (section, names) =>
    Boolean(explanation) && names.some((name) => explainNote(section, name));
  const working = state.working || {};
  $("ai-label-datums").hidden = !has("datums", (working.datums || []).map((d) => d.label));
  $("ai-label-dimensions").hidden = !has("dimensions", (working.dimensions || []).map((d) => d.id));
  $("ai-label-fcfs").hidden = !has("fcfs", (working.fcfs || []).map((f) => f.id));
  $("ai-label-issues").hidden = !has("findings", state.findings.map((f) => f.key));
}

/** The "About this drawing" card: chip, name guess, summary, features. */
function renderAbout(result) {
  const explanation = result && result.explanation;
  const part = explanation && explanation.part;
  aboutIntro.hidden = Boolean(part);
  aboutResult.hidden = !part;
  if (part) {
    $("about-type").textContent = PART_TYPE_LABELS[part.type] || PART_TYPE_LABELS.other;
    $("about-name").textContent = part.name_guess || "Unnamed part";
    $("about-summary").textContent = part.summary || "";
    const features = document.createDocumentFragment();
    for (const feature of part.features || []) {
      const item = document.createElement("li");
      item.textContent = feature;
      features.appendChild(item);
    }
    $("about-features").replaceChildren(features);
  }
  renderAboutRows(explanation);
  const issues = (explanation && explanation.parse_issues) || [];
  const lines = document.createDocumentFragment();
  for (const issue of issues) {
    const item = document.createElement("li");
    item.textContent = issue;
    lines.appendChild(item);
  }
  aboutIssuesList.replaceChildren(lines);
  aboutIssuesBox.hidden = issues.length === 0;
}

/** Put a new explanation everywhere it shows: the card, the table rows, the issue cards. */
function showExplanation(result) {
  state.explanation = result.explanation || null;
  renderAbout(result);
  renderTables();
  for (const [number, card] of issueCards) {
    const finding = state.findings[number - 1];
    const notes = card.querySelector(".finding-ai");
    if (finding && notes) fillFindingNotes(notes, finding);
  }
  paintAiLabels();
}

/** Ask the server to explain the drawing. Cached there, so asking twice is free. */
async function runExplain() {
  if (explaining) {
    explainQueued = true;
    return;
  }
  if (!state.runId || isBusy()) {
    return;
  }
  clearErrorToasts();
  state.explainWanted = true;
  explaining = true;
  updateUI();

  const controller = new AbortController();
  explainController = controller;
  const { signal } = controller;
  const runId = state.runId;

  try {
    const response = await fetch(`/api/runs/${runId}/explain`, { method: "POST", signal });
    if (!response.ok) {
      const message = await errorMessage(response);
      if (!signal.aborted) {
        toast(`Could not explain the drawing. ${message}`, { error: true });
      }
      return;
    }
    const result = await response.json();
    // A reply for a run that is no longer on screen is dropped.
    if (!signal.aborted && state.runId === runId) {
      if (result.fake_model) state.fake = true;
      showExplanation(result);
    }
  } catch (problem) {
    if (!signal.aborted) {
      toast(`Could not reach the server: ${problem.message}`, { error: true });
    }
  } finally {
    explainController = null;
    explaining = false;
    if (signal.aborted) {
      explainQueued = false;
      toast("Explanation cancelled.");
    }
    if (explainQueued && state.runId === runId) {
      explainQueued = false;
      setTimeout(runExplain, 0);
    }
    updateUI();
  }
}

explainButton.addEventListener("click", runExplain);

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
  // The second model's reading, compared on the server. Absent on the demo run.
  state.crossCheck = run.cross_check || null;
  clearFindings();
  state.explanation = null;
  state.explainWanted = false;
  renderAbout(null);
  // A fresh reading: its boxes wait for the final sweep, and they start out shown.
  state.revealPending = true;
  setBoxesShown(true);

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
  if (explainController) explainController.abort();
});

/**
 * Send one file and show what comes back. Cancel or a failure leaves a run already on
 * screen exactly as it was, because only `render` replaces it.
 */
async function startRun(file, source) {
  clearErrorToasts();
  state.pendingFile = { name: file.name, source };
  reading = true;
  startReadingScan(file);
  updateUI();

  const body = new FormData();
  body.append("file", file);
  body.append("provider", providerSelect.value);
  body.append("cross_check", crossCheckBox.checked ? "1" : "0");
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
    stopReadingScan();
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
    // The user asked for an explanation of this run: the confirmed data has changed, so
    // ask again, which adds notes for the new findings. One more model call, opted into.
    if (state.explainWanted) {
      setTimeout(runExplain, 0);
    }
  } catch (problem) {
    toast(`Could not reach the server: ${problem.message}`, { error: true });
  } finally {
    clearTimeout(labelTimer);
    checking = false;
    checkLabelShown = false;
    updateUI();
  }
}

/* ---- Live rounds: /fix?stream=1 ------------------------------------------- */

/*
 * The streamed fix sends one JSON object per line as the loop runs: round_started,
 * proposal, and verdict for each round, then done (the same reply as without streaming)
 * or error. Every verdict on it is the rule engine's re-run; the page only draws it.
 */

/** The stream broke before done or error: worth one retry without streaming. */
class StreamBroken extends Error {}

/** POST /fix, streamed or not. One fetch for both, so Cancel's signal reaches either. */
function postFix(stream, signal) {
  const query = stream ? "?stream=1" : "";
  return fetch(`/api/runs/${state.runId}/fix${query}`, { method: "POST", signal });
}

/**
 * Read newline-delimited JSON off `response` as it arrives, calling `onEvent` per line.
 * A line can be split across chunks, so text is buffered until its newline comes.
 */
async function readEvents(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let newline = buffer.indexOf("\n");
    while (newline !== -1) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) onEvent(JSON.parse(line));
      newline = buffer.indexOf("\n");
    }
    if (done) break;
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer));
}

/** Take out what a live round put on the issue cards before its verdict. */
function removeLiveAttempts(round) {
  for (const item of findingsList.querySelectorAll(`.attempt[data-live-round="${round}"]`)) {
    const list = item.parentElement;
    item.remove();
    if (list && list.children.length === 0) list.hidden = true;
  }
}

/** Put `item` on the cards of the issues `keys` names, or on the "other" card. */
function addLiveAttempt(keys, build) {
  const numbers = keys.map((key) => numberByKey.get(key)).filter(Boolean);
  const cards = numbers.length ? numbers.map((number) => issueCards.get(number)) : [otherProposalsCard()];
  for (const card of cards) {
    const list = card.querySelector(".attempts");
    list.hidden = false;
    list.appendChild(build());
  }
}

/** "Round 2: proposing…" on each open error the round is working on. */
function onRoundStarted(event) {
  live = { round: event.round, model: event.model || (live && live.model), fellBack: false };
  setRoundCell(event.round, { number: "–", sub: "Proposing…", phase: "running" });
  const errors = new Set(state.findings.filter((f) => f.severity === "error").map((f) => f.key));
  for (const key of (event.findings || []).filter((k) => errors.has(k) && numberByKey.has(k))) {
    addLiveAttempt([key], () => {
      const item = document.createElement("li");
      item.className = "attempt is-proposing";
      item.dataset.liveRound = String(event.round);
      const head = document.createElement("p");
      head.className = "attempt-head";
      const label = document.createElement("span");
      label.className = "attempt-round";
      label.textContent = `Round ${event.round}:`;
      const what = document.createElement("span");
      what.className = "attempt-edit";
      what.textContent = "proposing…";
      head.append(label, what);
      item.appendChild(head);
      return item;
    });
  }
  updateUI();
}

/** The model's proposal and rationale, before the rules have had their say. */
function onProposal(event) {
  live = { round: event.round, model: event.model || (live && live.model), fellBack: Boolean(event.fell_back) };
  removeLiveAttempts(event.round);
  setRoundCell(event.round, { number: "–", sub: "Re-checking…", phase: "running" });
  for (const edit of event.edits || []) {
    const pending = { ...edit, status: "pending", note: "The rules are re-run on this edit before it counts." };
    addLiveAttempt(edit.finding_ids || [], () => {
      const item = attemptItem(event, pending);
      item.dataset.liveRound = String(event.round);
      return item;
    });
  }
  updateUI();
}

/**
 * Clouds as the rounds so far leave them: cleared by a verified (or held) edit, or still
 * open after an edit that tried. Only keys the re-run cleared are ever coloured as passed.
 */
function liveOutcomes(rounds) {
  const outcomes = new Map();
  for (const round of rounds) {
    const cleared = new Set((round.cleared || []).map((entry) => entry.key));
    for (const edit of round.edits || []) {
      for (const key of edit.finding_ids || []) {
        if (cleared.has(key)) {
          outcomes.set(key, edit.status === "held" ? "held" : "verified");
        } else if (outcomes.get(key) !== "verified" && outcomes.get(key) !== "held") {
          outcomes.set(key, "unresolved");
        }
      }
    }
    for (const key of cleared) {
      if (!outcomes.has(key) || outcomes.get(key) === "unresolved") outcomes.set(key, "verified");
    }
  }
  return outcomes;
}

/** The rule engine's verdict on one round: chips on the cards, colours and notes on the sheet. */
function onVerdict(event, rounds) {
  rounds.push(event);
  removeLiveAttempts(event.round);
  showRoundAttempts(event);
  setRoundCell(event.round, {
    number: event.findings_after,
    sub: roundSummaryText(event),
    phase: "done",
    title: event.error ? event.error : `${plural(event.findings_after, "finding")} left after round ${event.round}`,
  });
  colourClouds(liveOutcomes(rounds));
  addPenNotes((event.edits || []).filter((edit) => edit.pen));
  updateUI();
}

/**
 * Run a streamed fix. Resolves with the final reply on done; throws an Error carrying the
 * server's message on an error event, or StreamBroken if the stream ended without either.
 */
async function streamFix(response, signal) {
  const type = response.headers.get("content-type") || "";
  if (!type.includes("application/x-ndjson")) {
    throw new StreamBroken("not a stream");
  }
  const rounds = [];
  let final = null;
  let failure = null;
  selectTab("issues");
  try {
    await readEvents(response, (event) => {
      if (signal.aborted || final || failure) return;
      if (event.type === "round_started") onRoundStarted(event);
      else if (event.type === "proposal") onProposal(event);
      else if (event.type === "verdict") onVerdict(event, rounds);
      else if (event.type === "done") final = event;
      else if (event.type === "error") failure = event;
    });
  } catch (problem) {
    if (signal.aborted) throw problem;
    throw new StreamBroken(problem.message);
  }
  if (failure) {
    throw new Error(failure.detail || `The server returned ${failure.status}.`);
  }
  if (!final) {
    throw new StreamBroken("the stream ended early");
  }
  return final;
}

/** The fix without streaming: one reply, replayed round by round (ROUND_REPLAY_MS). */
async function replayFix(signal) {
  clearAgent();
  live = null;
  setRoundCell(1, { number: "–", sub: "Proposing…", phase: "running" });
  updateUI();
  const response = await postFix(false, signal);
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  const result = await response.json();
  if (signal.aborted) return;
  selectTab("issues");
  fixController = null;
  fixing = false;
  const replay = renderAgent(result);
  updateUI();
  await replay;
}

/**
 * Ask the agent for corrections. Nothing is posted: the server works from the drawing
 * the last check saved, which is why this is only offered while that check is current.
 *
 * The rounds are streamed and drawn as they happen. If the stream itself fails, the fix
 * is asked for once more without streaming and replayed instead.
 */
async function runFix() {
  if (!state.runId || !state.checked || state.stale || state.errorCount === 0) {
    return;
  }
  clearErrorToasts();
  fixing = true;
  live = null;
  clearAgent();
  setRoundCell(1, { number: "–", sub: "Proposing…", phase: "running" });
  updateUI();

  const controller = new AbortController();
  fixController = controller;
  const { signal } = controller;

  try {
    let result = null;
    try {
      const response = await postFix(true, signal);
      if (!response.ok) {
        const message = await errorMessage(response);
        if (!signal.aborted) {
          toast(`Could not propose fixes. ${message}`, { error: true });
        }
        return;
      }
      result = await streamFix(response, signal);
    } catch (problem) {
      if (signal.aborted || !(problem instanceof StreamBroken || problem instanceof TypeError)) {
        throw problem;
      }
      // The stream broke (a proxy, a dropped connection): ask once more, all at once.
      await replayFix(signal);
      return;
    }
    if (!signal.aborted) {
      fixController = null;
      fixing = false;
      live = null;
      const settle = renderAgent(result, { live: true });
      updateUI();
      await settle;
    }
  } catch (problem) {
    if (!signal.aborted) {
      const message = problem instanceof TypeError ? `Could not reach the server: ${problem.message}` : `Could not propose fixes. ${problem.message}`;
      toast(message, { error: true });
    }
  } finally {
    fixController = null;
    fixing = false;
    live = null;
    if (signal.aborted) {
      toast(CANCELLED_MESSAGE);
    }
    if (!state.fixResult && state.checked) {
      // Cancelled or failed: the rounds are still to come, and nothing was saved.
      clearAgent();
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

/** A test-drawing name from the Test results page: lowercase letters, digits, underscores. */
const SUITE_STEM = /^[a-z0-9_]{1,64}$/;

/** "Try it live": fetch one test drawing's PDF and upload it as a picked file would be. */
async function trySuiteDrawing(stem) {
  if (!SUITE_STEM.test(stem) || isBusy()) {
    return;
  }
  try {
    const response = await fetch(`/eval/pdfs/${stem}.pdf`);
    if (!response.ok) {
      toast(`Could not load the test drawing ${stem}.`, { error: true });
      return;
    }
    const blob = await response.blob();
    offerFile(new File([blob], `${stem}.pdf`, { type: "application/pdf" }), "suite");
  } catch (problem) {
    toast(`Could not reach the server: ${problem.message}`, { error: true });
  }
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
  startReadingScan({ name: "plate_001.png", type: "" });
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
    stopReadingScan();
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

{
  // Arriving from "Try it live": upload that test drawing once, then drop the parameter so
  // a reload does not upload it again.
  const params = new URLSearchParams(window.location.search);
  const tryStem = params.get("try");
  if (tryStem !== null) {
    params.delete("try");
    const rest = params.toString();
    history.replaceState(null, "", `${window.location.pathname}${rest ? `?${rest}` : ""}`);
    trySuiteDrawing(tryStem);
  }
}

if (new URLSearchParams(window.location.search).get("demo") === LOW_CONFIDENCE_DEMO) {
  loadDemo();
}
