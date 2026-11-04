/* DraftCheck — "Test results".
 *
 * Reads one file, /eval/results.json, the public snapshot scripts/run_eval.py writes, and
 * draws the headline numbers, a filterable gallery of the test drawings, and a detail
 * panel per drawing. No model calls and no API calls, so it is instant. Every element is
 * built with DOM calls (textContent, never innerHTML), so nothing in the snapshot can
 * become markup.
 */

"use strict";

const SNAPSHOT_URL = "/eval/results.json";

const $ = (id) => document.getElementById(id);

const page = {
  /** The snapshot, once loaded. */
  data: null,
  /** Drawing stem shown in the detail panel. */
  open: null,
};

/* ---- Small helpers ------------------------------------------------------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** "spur_gear" -> "Spur gear". */
function humanize(value) {
  const words = String(value || "other").replace(/_/g, " ");
  return words[0].toUpperCase() + words.slice(1);
}

function plural(count, word, many = `${word}s`) {
  return `${count} ${count === 1 ? word : many}`;
}

function frac(num, den) {
  return `${num}/${den}`;
}

/** The short name of a model, for badges: "Muse Spark Open (Baseten)" -> "Muse Spark Open". */
function shortLabel(provider) {
  return provider.label.replace(/\s*\(.*\)\s*$/, "");
}

function gdtCount(features) {
  return Object.values(features.gdt_frames || {}).reduce((sum, n) => sum + n, 0);
}

function hasSeeded(drawing) {
  return drawing.seeded_errors.length > 0;
}

/** The providers the model filter leaves on screen. */
function shownProviders() {
  const chosen = $("filter-model").value;
  return page.data.providers.filter((p) => !chosen || p.id === chosen);
}

/* ---- Loading ------------------------------------------------------------- */

async function load() {
  let data = null;
  try {
    const response = await fetch(SNAPSHOT_URL, { cache: "no-store" });
    if (response.ok) data = await response.json();
  } catch {
    data = null;
  }
  if (!data || !Array.isArray(data.drawings) || data.drawings.length === 0) {
    showEmpty();
    return;
  }
  page.data = data;
  $("eval-method").textContent = data.method || $("eval-method").textContent;
  const when = data.generated_at ? new Date(data.generated_at) : null;
  $("eval-stamp-text").textContent =
    when && !Number.isNaN(when.getTime())
      ? `Snapshot ${when.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`
      : "Snapshot";
  $("eval-content").hidden = false;
  renderHeadline();
  fillFilters();
  renderGrid();
  openFromHash();
}

function showEmpty() {
  $("eval-empty").hidden = false;
  $("eval-content").hidden = true;
  $("eval-stamp-text").textContent = "No snapshot";
}

/* ---- Headline ------------------------------------------------------------ */

function stat(value, label, note) {
  const tile = el("div", "stat");
  tile.append(el("p", "stat-value", value), el("p", "stat-label", label));
  if (note) tile.append(el("p", "stat-note", note));
  return tile;
}

function renderHeadline() {
  const { suite, totals, providers } = page.data;
  const runsOk = providers.reduce((sum, p) => sum + (totals[p.id]?.runs_ok || 0), 0);
  const runsPossible = suite.drawings * providers.length;
  $("eval-suite").replaceChildren(
    stat(suite.drawings, "Drawings"),
    stat(suite.part_types, "Part types"),
    stat(suite.views, "Views"),
    stat(suite.gdt_frames, "GD&T frames"),
    stat(suite.seeded_errors, "Seeded issues"),
    stat(frac(runsOk, runsPossible), "Runs finished", "drawing × model"),
  );

  const rows = [
    ["Runs finished", (t) => frac(t.runs_ok, suite.drawings)],
    ["Seeded issues caught", (t) => frac(t.errors_caught, t.errors_expected)],
    ["False alarms", (t) => String(t.false_alarms)],
    ["Fixes verified by the rules", (t) => (t.errors_before ? frac(t.fixes_verified, t.errors_before) : "n/a")],
    ["Part types recognized", (t) => (t.part_types_scored ? frac(t.part_types_recognized, t.part_types_scored) : "not run")],
    ["Runs that used the fallback", (t) => (t.runs_ok ? `${t.fallbacks} of ${t.runs_ok}` : "—")],
    ["Average latency per drawing", (t) => (t.runs_ok ? `${(t.latency_s / t.runs_ok).toFixed(1)} s` : "—")],
  ];
  const head = el("tr");
  head.append(el("th", "", ""));
  for (const provider of providers) head.append(el("th", "", provider.label));
  const body = document.createDocumentFragment();
  for (const [label, value] of rows) {
    const row = el("tr");
    row.append(el("th", "", label));
    for (const provider of providers) row.append(el("td", "mono", value(totals[provider.id] || {})));
    body.append(row);
  }
  const table = $("eval-compare");
  table.tHead.replaceChildren(head);
  table.tBodies[0].replaceChildren(body);
}

/* ---- Filters ------------------------------------------------------------- */

function fillFilters() {
  const types = [...new Set(page.data.drawings.map((d) => d.part_type))].sort();
  const partSelect = $("filter-part");
  for (const type of types) {
    const option = el("option", "", humanize(type));
    option.value = type;
    partSelect.append(option);
  }
  const modelSelect = $("filter-model");
  for (const provider of page.data.providers) {
    const option = el("option", "", provider.label);
    option.value = provider.id;
    modelSelect.append(option);
  }
  for (const id of ["filter-part", "filter-model", "filter-seeded", "filter-multiview"]) {
    $(id).addEventListener("change", renderGrid);
  }
}

function matches(drawing) {
  const part = $("filter-part").value;
  const seeded = $("filter-seeded").value;
  if (part && drawing.part_type !== part) return false;
  if (seeded === "seeded" && !hasSeeded(drawing)) return false;
  if (seeded === "clean" && hasSeeded(drawing)) return false;
  if ($("filter-multiview").checked && drawing.features.views < 2) return false;
  return true;
}

/* ---- Gallery ------------------------------------------------------------- */

function featureChips(features) {
  const chips = [plural(features.views, "view")];
  if (features.sections) chips.push(plural(features.sections, "section"));
  if (features.details) chips.push(plural(features.details, "detail"));
  const gdt = gdtCount(features);
  if (gdt) chips.push(`${gdt} GD&T`);
  for (const table of features.tables || []) chips.push(humanize(table));
  if (features.balloons) chips.push(plural(features.balloons, "balloon"));
  const list = el("ul", "chips");
  for (const text of chips) list.append(el("li", "chip", text));
  return list;
}

/** One model's badges on a card: issues caught, false alarms, fixes, part type, fallback. */
function modelBadges(drawing, provider) {
  const run = drawing.runs[provider.id] || { status: "not run" };
  const explain = drawing.explain[provider.id] || { status: "not run" };
  const row = el("div", "badges");
  row.append(el("span", "badge-model", shortLabel(provider)));
  if (run.status !== "ok") {
    row.append(el("span", "badge is-muted", run.status));
    return row;
  }
  const { errors_caught: caught, errors_expected: expected } = run.errors;
  row.append(
    el("span", `badge ${caught === expected ? "is-ok" : "is-bad"}`, `Issues ${frac(caught, expected)}`),
  );
  const alarms = run.errors.false_alarms.length;
  row.append(el("span", `badge ${alarms ? "is-bad" : "is-ok"}`, `False alarms ${alarms}`));
  if (run.fixes.errors_before) {
    const done = run.fixes.fixes_verified === run.fixes.errors_before;
    row.append(
      el("span", `badge ${done ? "is-ok" : "is-bad"}`,
        `Fixes ${frac(run.fixes.fixes_verified, run.fixes.errors_before)}`),
    );
  }
  if (explain.status === "ok" && explain.part_type_correct !== null) {
    row.append(
      el("span", `badge ${explain.part_type_correct ? "is-ok" : "is-bad"}`,
        `Part type ${explain.part_type_correct ? "✓" : "✗"}`),
    );
  }
  if (run.fell_back) row.append(el("span", "badge is-warn", "Fallback"));
  return row;
}

function card(drawing) {
  const button = el("button", "card");
  button.type = "button";
  button.dataset.stem = drawing.stem;
  button.setAttribute("aria-label", `${drawing.stem}, ${humanize(drawing.part_type)}. Open details`);

  const paper = el("div", "card-paper");
  const image = el("img");
  image.src = `/${drawing.thumb}`;
  image.alt = "";
  image.loading = "lazy";
  paper.append(image);

  const head = el("div", "card-head");
  head.append(el("span", "card-name mono", drawing.stem), el("span", "part-chip", humanize(drawing.part_type)));

  const seeded = el(
    "p",
    `card-seeded ${hasSeeded(drawing) ? "is-seeded" : "is-clean"}`,
    hasSeeded(drawing)
      ? `Seeded: ${drawing.seeded_errors.map((e) => e.rule_id).join(", ")}`
      : "Clean: nothing seeded",
  );

  const badges = el("div", "card-badges");
  for (const provider of shownProviders()) badges.append(modelBadges(drawing, provider));

  button.append(paper, head, featureChips(drawing.features), seeded, badges);
  button.addEventListener("click", () => openDetail(drawing.stem));
  return button;
}

function renderGrid() {
  const shown = page.data.drawings.filter(matches);
  $("eval-grid").replaceChildren(...shown.map(card));
  $("eval-none").hidden = shown.length > 0;
  $("eval-count").textContent = `Showing ${shown.length} of ${page.data.drawings.length} drawings`;
}

/* ---- Detail panel -------------------------------------------------------- */

function table(headers, rows) {
  const wrap = el("table", "facts");
  const head = el("tr");
  for (const text of headers) head.append(el("th", "", text));
  const thead = el("thead");
  thead.append(head);
  const tbody = el("tbody");
  for (const cells of rows) {
    const row = el("tr");
    cells.forEach((cell, index) => row.append(el(index ? "td" : "th", index ? "mono" : "", cell)));
    tbody.append(row);
  }
  wrap.append(thead, tbody);
  return wrap;
}

/** ✓ caught / ✗ missed / — not run, for one seeded error and one model. */
function caughtMark(value) {
  if (value === true) return "✓ caught";
  if (value === false) return "✗ missed";
  return "— not run";
}

function seededSection(drawing, providers) {
  if (!hasSeeded(drawing)) {
    return el("p", "detail-note", "Clean drawing: no errors were seeded, so any finding would be a false alarm.");
  }
  const list = el("ul", "seeded-list");
  for (const error of drawing.seeded_errors) {
    const item = el("li");
    item.append(el("p", "seeded-rule mono", error.rule_id), el("p", "seeded-text", error.description));
    const marks = el("p", "seeded-marks");
    for (const provider of providers) {
      const value = error.caught_by[provider.id];
      const mark = el("span", `badge ${value === true ? "is-ok" : value === false ? "is-bad" : "is-muted"}`,
        `${shortLabel(provider)}: ${caughtMark(value)}`);
      marks.append(mark);
    }
    item.append(marks);
    list.append(item);
  }
  return list;
}

function perModel(providers, drawing, kind, rows) {
  const done = providers.filter((p) => drawing[kind][p.id]?.status === "ok");
  if (done.length === 0) {
    const status = providers.map((p) => `${shortLabel(p)}: ${drawing[kind][p.id]?.status || "not run"}`);
    return el("p", "detail-note", `No scores yet (${status.join("; ")}).`);
  }
  return table(
    ["", ...done.map(shortLabel)],
    rows.map(([label, value]) => [label, ...done.map((p) => value(drawing[kind][p.id]))]),
  );
}

function openDetail(stem) {
  const drawing = page.data.drawings.find((d) => d.stem === stem);
  if (!drawing) return;
  page.open = stem;
  const providers = shownProviders();

  $("detail-part").textContent = humanize(drawing.part_type);
  $("detail-name").textContent = drawing.stem;
  $("detail-title").textContent = drawing.title || "";
  const image = $("detail-image");
  image.src = `/${drawing.sheet}`;
  image.alt = `${drawing.stem} drawing`;
  $("detail-try").href = `/?try=${encodeURIComponent(drawing.stem)}`;

  $("detail-seeded").replaceChildren(seededSection(drawing, providers));
  $("detail-extraction").replaceChildren(
    perModel(providers, drawing, "runs", [
      ["Dimensions found", (r) => frac(r.extraction.dims_found, r.extraction.dims_expected)],
      ["Dimensions correct", (r) => frac(r.extraction.dims_correct, r.extraction.dims_expected)],
      ["Frames correct", (r) => frac(r.extraction.fcfs_correct, r.extraction.fcfs_expected)],
      ["Datums found", (r) => frac(r.extraction.datums_found, r.extraction.datums_expected)],
      ["Units right", (r) => (r.extraction.units_correct ? "yes" : "no")],
      ["General tolerance right", (r) => (r.extraction.general_tolerance_correct ? "yes" : "no")],
    ]),
  );
  $("detail-explain").replaceChildren(
    perModel(providers, drawing, "explain", [
      ["Part type", (e) => `${humanize(e.part_type)} ${e.part_type_correct === null ? "" : e.part_type_correct ? "✓" : "✗"}`],
      ["Views found", (e) => `${frac(e.views_found, e.views_expected)}${e.views_extra ? ` (+${e.views_extra})` : ""}`],
      ["Dimensions in the right view", (e) => frac(e.dims_view_correct, e.dims_with_view)],
      ["Projection", (e) => e.projection || "—"],
    ]),
  );
  $("detail-runs").replaceChildren(
    perModel(providers, drawing, "runs", [
      ["Answered by", (r) => r.model || "—"],
      ["Latency", (r) => `${r.latency_s.toFixed(1)} s`],
      ["Fallback used", (r) => (r.fell_back ? "yes" : "no")],
      ["Fix rounds", (r) => String(r.fixes.rounds)],
    ]),
  );

  const dialog = $("eval-detail");
  if (!dialog.open) dialog.showModal();
  history.replaceState(null, "", `#${encodeURIComponent(stem)}`);
}

function closeDetail() {
  $("eval-detail").close();
}

$("detail-close").addEventListener("click", closeDetail);
$("eval-detail").addEventListener("close", () => {
  page.open = null;
  history.replaceState(null, "", window.location.pathname);
  const card = document.querySelector(`.card[data-stem="${CSS.escape(page.lastOpened || "")}"]`);
  if (card) card.focus();
});
$("eval-detail").addEventListener("click", (event) => {
  // A click on the backdrop (the dialog itself, outside its content) closes it.
  if (event.target === $("eval-detail")) closeDetail();
});
document.addEventListener("click", (event) => {
  const card = event.target.closest && event.target.closest(".card");
  if (card) page.lastOpened = card.dataset.stem;
});

/** A link like /eval.html#flange opens that drawing straight away. */
function openFromHash() {
  const stem = decodeURIComponent(window.location.hash.slice(1));
  if (stem) openDetail(stem);
}

load();
