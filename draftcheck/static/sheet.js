/* DraftCheck — what gets drawn on the sheet.
 *
 * Pure drawing helpers for the SVG overlay that sits on the page image: the reading scan,
 * revision clouds, delta triangles, handwritten pen notes, and the CHECKED stamp. Nothing
 * in here knows about a run or a request; app.js decides what to draw and passes in the
 * numbers, all of which come from the API's replies.
 *
 * Coordinates are image pixels (the overlay's viewBox is the image's natural size). Sizes
 * are in "sheet units": one unit is the image width / SHEET_UNITS, so a stroke or a label
 * is the same fraction of the drawing on a small sheet and a large one. The overlay
 * carries the unit as the CSS variable --u, which style.css multiplies stroke widths and
 * font sizes by.
 *
 * Every string is set with textContent: labels come from the server, which builds them
 * from what a model read off an uploaded file.
 */

"use strict";

const Sheet = (() => {
  const NS = "http://www.w3.org/2000/svg";

  /** The sheet is this many units wide, whatever its pixel size. */
  const SHEET_UNITS = 800;

  /** Whether the visitor asked for no motion. Read live, so a change applies at once. */
  const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  function reducedMotion() {
    return motionQuery.matches;
  }

  function el(name, attributes = {}, parent = null) {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attributes)) {
      node.setAttribute(key, String(value));
    }
    if (parent) parent.appendChild(node);
    return node;
  }

  /** Pixels per sheet unit for an image this wide. */
  function unitFor(imageWidth) {
    return imageWidth / SHEET_UNITS;
  }

  /** A normalized [x0, y0, x1, y1] box in image pixels, or null. */
  function toPixels(bbox, width, height) {
    if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(Number.isFinite)) {
      return null;
    }
    const [x0, y0, x1, y1] = bbox;
    return { x0: x0 * width, y0: y0 * height, x1: x1 * width, y1: y1 * height };
  }

  function clamp(value, low, high) {
    return Math.min(Math.max(value, low), Math.max(low, high));
  }

  /* ---- The hand-drawn filter -------------------------------------------- */

  /**
   * The wobble that makes a cloud or a strike line look drawn rather than plotted:
   * turbulence noise nudging each point of the stroke. Its frequency and strength are in
   * user units, so they are scaled by the sheet unit like everything else.
   */
  function ensureRoughFilter(svg, unit) {
    let defs = svg.querySelector("defs");
    if (!defs) {
      defs = el("defs");
      svg.insertBefore(defs, svg.firstChild);
    }
    let filter = defs.querySelector("#pen-rough");
    if (!filter) {
      filter = el("filter", { id: "pen-rough", x: "-10%", y: "-10%", width: "120%", height: "120%" }, defs);
      el("feTurbulence", { type: "fractalNoise", numOctaves: 2, seed: 7, result: "noise" }, filter);
      el(
        "feDisplacementMap",
        { in: "SourceGraphic", in2: "noise", xChannelSelector: "R", yChannelSelector: "G" },
        filter,
      );
    }
    filter.querySelector("feTurbulence").setAttribute("baseFrequency", String(0.045 / unit));
    filter.querySelector("feDisplacementMap").setAttribute("scale", String(2.4 * unit));
    return "url(#pen-rough)";
  }

  /* ---- Scan band ---------------------------------------------------------- */

  /**
   * A soft blue band with a solid leading line, which `sweep` moves down the sheet.
   * Returns the group; its `setY(y)` puts the leading line at image row y.
   */
  function scanBand(svg, width, height, unit) {
    let defs = svg.querySelector("defs");
    if (!defs) {
      defs = el("defs");
      svg.insertBefore(defs, svg.firstChild);
    }
    if (!defs.querySelector("#scan-fade")) {
      const gradient = el("linearGradient", { id: "scan-fade", x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
      el("stop", { offset: "0", class: "scan-stop-top" }, gradient);
      el("stop", { offset: "1", class: "scan-stop-bottom" }, gradient);
    }
    const bandHeight = height * 0.16;
    const group = el("g", { class: "scan" });
    const band = el("rect", { x: 0, width, height: bandHeight, fill: "url(#scan-fade)" }, group);
    const line = el("rect", { x: 0, width, height: 1.6 * unit, class: "scan-line" }, group);
    group.setY = (y) => {
      band.setAttribute("y", String(y - bandHeight));
      line.setAttribute("y", String(y - 0.8 * unit));
    };
    group.setY(-1);
    return group;
  }

  /**
   * Move a scan band from the top to the bottom of the sheet over `ms`, calling
   * `onRow(y)` every frame. Resolves at the bottom. `loop` repeats until `stop()`.
   */
  function sweep(band, height, ms, { onRow = null, loop = false } = {}) {
    let frame = 0;
    let stopped = false;
    let start = null;
    let finish;
    const done = new Promise((resolve) => {
      finish = resolve;
    });
    const span = height * 1.16; // runs on past the bottom until the band has left
    function step(now) {
      if (stopped) return;
      if (start === null) start = now;
      let t = (now - start) / ms;
      if (t >= 1 && loop) {
        start = now;
        t = 0;
      }
      const y = Math.min(t, 1) * span;
      band.setY(y);
      if (onRow) onRow(y);
      if (t >= 1) {
        finish();
        return;
      }
      frame = requestAnimationFrame(step);
    }
    frame = requestAnimationFrame(step);
    return {
      done,
      stop() {
        stopped = true;
        cancelAnimationFrame(frame);
        finish();
      },
    };
  }

  /* ---- Revision clouds ---------------------------------------------------- */

  /** About one arc per this many units of perimeter. */
  const ARC_UNITS = 4.5;

  /**
   * A closed path of outward arcs around a rectangle: the scalloped outline a checker
   * draws around something that has to change. The rectangle is walked clockwise and
   * cut into equal steps; each step is an arc that bulges away from the middle.
   */
  function cloudPath(box, unit) {
    const { x0, y0, x1, y1 } = box;
    const w = x1 - x0;
    const h = y1 - y0;
    const perimeter = 2 * (w + h);
    const count = Math.max(8, Math.round(perimeter / (ARC_UNITS * unit)));
    const step = perimeter / count;

    function pointAt(distance) {
      let d = distance % perimeter;
      if (d < w) return [x0 + d, y0];
      d -= w;
      if (d < h) return [x1, y0 + d];
      d -= h;
      if (d < w) return [x1 - d, y1];
      d -= w;
      return [x0, y1 - d];
    }

    const radius = step * 0.62;
    const [sx, sy] = pointAt(0);
    const parts = [`M${sx.toFixed(1)} ${sy.toFixed(1)}`];
    for (let i = 1; i <= count; i += 1) {
      const [x, y] = pointAt(i * step);
      // Clockwise on screen, sweep-flag 1 bulges each arc outward.
      parts.push(`A${radius.toFixed(1)} ${radius.toFixed(1)} 0 0 1 ${x.toFixed(1)} ${y.toFixed(1)}`);
    }
    parts.push("Z");
    return parts.join(" ");
  }

  /** Grow a box by `pad` on every side. */
  function padBox(box, pad) {
    return { x0: box.x0 - pad, y0: box.y0 - pad, x1: box.x1 + pad, y1: box.y1 + pad };
  }

  /**
   * One cloud with its delta, as a focusable button-like group. `order` staggers the
   * drawing animation. The returned group carries `bounds` (cloud and delta together),
   * which the stamp placement keeps clear of.
   */
  function cloud({ box, number, label, unit, width, height, order, filter }) {
    const ring = padBox(box, 5 * unit);
    const group = el("g", {
      class: "cloud",
      role: "button",
      tabindex: 0,
      "aria-label": label,
    });
    group.style.setProperty("--order", String(order));
    group.dataset.number = String(number);

    const d = cloudPath(ring, unit);
    // A wide invisible stroke on the same outline, so the cloud is easy to click.
    el("path", { d, class: "cloud-hit" }, group);
    el("path", { d, class: "cloud-line", pathLength: 100, filter }, group);

    // The delta sits just outside the cloud's top-left corner, kept on the sheet.
    const side = 20 * unit;
    const tall = side * 0.88;
    const right = clamp(ring.x0 + 2 * unit, side + 2 * unit, width - 2 * unit);
    const base = clamp(ring.y0 + 1 * unit, tall + 2 * unit, height - 2 * unit);
    const left = right - side;
    const delta = el("g", { class: "cloud-delta" }, group);
    el(
      "polygon",
      { points: `${left},${base} ${right},${base} ${left + side / 2},${base - tall}` },
      delta,
    );
    const digits = el(
      "text",
      { x: left + side / 2, y: base - tall * 0.2, "text-anchor": "middle" },
      delta,
    );
    digits.textContent = String(number);

    const title = el("title", {}, group);
    title.textContent = label;

    group.bounds = {
      x0: Math.min(ring.x0, left),
      y0: Math.min(ring.y0, base - tall),
      x1: ring.x1,
      y1: ring.y1,
    };
    group.ring = ring;
    return group;
  }

  /* ---- Pen notes ---------------------------------------------------------- */

  const PEN_FONT_URL = "/fonts/nanum-pen-script/NanumPenScript-Regular.ttf";
  let penFont = null;

  /**
   * Load the handwriting font, once, the first time a note is written. It is 3 MB, so it
   * is not in style.css (a stylesheet @font-face can be fetched early) and not preloaded:
   * the page that never reaches a fix never downloads it. Until it arrives the notes show
   * in the fallback cursive (font-display: swap); the promise lets them be re-measured.
   */
  function loadPenFont() {
    if (!penFont) {
      if (typeof FontFace === "undefined") {
        penFont = Promise.resolve(false);
      } else {
        const face = new FontFace("Nanum Pen Script", `url(${PEN_FONT_URL}) format("truetype")`, {
          display: "swap",
        });
        document.fonts.add(face);
        penFont = face.load().then(
          () => true,
          () => false,
        );
      }
    }
    return penFont;
  }

  /** A note's size, in units. */
  const PEN_UNITS = 22;

  /**
   * A short handwritten note. `status` is verified, held, or rejected; rejected notes
   * get a strike line drawn over them after they appear. Placed by `placeNote`.
   */
  function note({ text, status, order, filter }) {
    const group = el("g", { class: `pen-note st-${status}` });
    group.style.setProperty("--order", String(order));
    const words = el("text", { class: "pen-text" }, group);
    words.textContent = text;
    if (status === "rejected") {
      el("path", { class: "pen-strike", pathLength: 100, filter }, group);
    }
    return group;
  }

  /**
   * Put a note beside an anchor box (a cloud's ring): to its right, or to its left if
   * that runs off the sheet; `slot` stacks several notes for the same anchor. Kept inside
   * the sheet either way. Measures the text, so it is rerun once the pen font loads.
   */
  function placeNote(group, anchor, slot, unit, width, height) {
    const words = group.querySelector(".pen-text");
    const size = PEN_UNITS * unit;
    const textWidth = words.getComputedTextLength() || words.textContent.length * size * 0.42;
    const gap = 6 * unit;
    let x = anchor.x1 + gap;
    if (x + textWidth > width - 4 * unit) {
      x = anchor.x0 - gap - textWidth;
    }
    x = clamp(x, 4 * unit, width - 4 * unit - textWidth);
    const middle = (anchor.y0 + anchor.y1) / 2 + size * 0.32;
    const y = clamp(middle + slot * size * 1.05, size, height - 4 * unit);
    words.setAttribute("x", x.toFixed(1));
    words.setAttribute("y", y.toFixed(1));

    const strike = group.querySelector(".pen-strike");
    if (strike) {
      // A quick line through the middle of the word, rising a little as a hand does.
      const sy = y - size * 0.3;
      const ex = x + textWidth + 2 * unit;
      const mid = x + textWidth / 2;
      strike.setAttribute(
        "d",
        `M${(x - 2 * unit).toFixed(1)} ${(sy + 1.5 * unit).toFixed(1)} ` +
          `Q${mid.toFixed(1)} ${(sy - 1 * unit).toFixed(1)} ${ex.toFixed(1)} ${(sy - 1.5 * unit).toFixed(1)}`,
      );
    }
    group.bounds = { x0: x, y0: y - size * 0.8, x1: x + textWidth, y1: y + size * 0.2 };
  }

  /* ---- The CHECKED stamp -------------------------------------------------- */

  const STAMP_W = 200;
  const STAMP_H = 74;

  function overlaps(a, b) {
    return a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
  }

  /**
   * How much ink each candidate spot covers, read off a small copy of the page image.
   * Returns a function (box) -> fraction of dark pixels, or null if the image cannot be
   * read (it always can here: the page is served from the same origin).
   */
  function inkReader(image, width, height) {
    try {
      const scale = 240 / width;
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(width * scale));
      canvas.height = Math.max(1, Math.round(height * scale));
      const context = canvas.getContext("2d", { willReadFrequently: true });
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      return (box) => {
        const cx0 = Math.max(0, Math.floor(box.x0 * scale));
        const cy0 = Math.max(0, Math.floor(box.y0 * scale));
        const cx1 = Math.min(canvas.width, Math.ceil(box.x1 * scale));
        const cy1 = Math.min(canvas.height, Math.ceil(box.y1 * scale));
        let dark = 0;
        let total = 0;
        for (let y = cy0; y < cy1; y += 1) {
          for (let x = cx0; x < cx1; x += 1) {
            const i = (y * canvas.width + x) * 4;
            total += 1;
            if (pixels[i] + pixels[i + 1] + pixels[i + 2] < 600) dark += 1;
          }
        }
        return total ? dark / total : 1;
      };
    } catch {
      return null;
    }
  }

  /**
   * Where the stamp goes: the clearest spot on the sheet that touches no box, cloud, or
   * note, nearest the lower middle when several are equally clear. With no clear spot at
   * all it goes in the lower middle anyway.
   */
  function stampSpot({ width, height, unit, avoid, image }) {
    const w = STAMP_W * unit;
    const h = STAMP_H * unit;
    // The rotated stamp's footprint is a little larger than its rectangle.
    const reach = { w: w * 1.06, h: h * 1.25 };
    const margin = 10 * unit;
    const ink = image ? inkReader(image, width, height) : null;
    const fallback = { cx: width / 2, cy: height * 0.78 };
    let best = null;
    const columns = 9;
    const rows = 9;
    for (let row = 0; row < rows; row += 1) {
      for (let column = 0; column < columns; column += 1) {
        const cx = margin + reach.w / 2 + (column / (columns - 1)) * (width - 2 * margin - reach.w);
        const cy = margin + reach.h / 2 + (row / (rows - 1)) * (height - 2 * margin - reach.h);
        const box = { x0: cx - reach.w / 2, y0: cy - reach.h / 2, x1: cx + reach.w / 2, y1: cy + reach.h / 2 };
        if (avoid.some((other) => overlaps(box, padBox(other, 4 * unit)))) continue;
        const darkness = ink ? ink(box) : 0;
        if (darkness > 0.02) continue;
        const distance = Math.hypot((cx - fallback.cx) / width, (cy - fallback.cy) / height);
        const score = darkness * 20 + distance;
        if (!best || score < best.score) best = { cx, cy, score };
      }
    }
    return best || fallback;
  }

  /**
   * The CHECKED stamp: a double-bordered green rounded rectangle, rotated -7°, with the
   * engine's name, the date, and the counts. Its inner group carries the landing
   * animation; the base style is the landed state, so reduced motion simply shows it.
   */
  function stamp({ cx, cy, unit, date, verified, designer }) {
    const w = STAMP_W * unit;
    const h = STAMP_H * unit;
    const outer = el("g", {
      class: "stamp",
      transform: `translate(${cx.toFixed(1)} ${cy.toFixed(1)}) rotate(-7)`,
      role: "img",
      "aria-label": `Checked by the DraftCheck rule engine on ${date}: ${verified} verified, ${designer} for designer.`,
    });
    const inner = el("g", { class: "stamp-body" }, outer);
    el("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 7 * unit, class: "stamp-ring" }, inner);
    const inset = 4.5 * unit;
    el(
      "rect",
      {
        x: -w / 2 + inset,
        y: -h / 2 + inset,
        width: w - 2 * inset,
        height: h - 2 * inset,
        rx: 4 * unit,
        class: "stamp-ring-inner",
      },
      inner,
    );
    const title = el("text", { x: 0, y: -h * 0.06, "text-anchor": "middle", class: "stamp-title" }, inner);
    title.textContent = "CHECKED";
    const engine = el("text", { x: 0, y: h * 0.17, "text-anchor": "middle", class: "stamp-line" }, inner);
    engine.textContent = `DRAFTCHECK RULE ENGINE  ${date}`;
    const counts = el("text", { x: 0, y: h * 0.35, "text-anchor": "middle", class: "stamp-line" }, inner);
    counts.textContent = `${verified} VERIFIED, ${designer} FOR DESIGNER`;
    return outer;
  }

  return {
    SHEET_UNITS,
    reducedMotion,
    el,
    unitFor,
    toPixels,
    ensureRoughFilter,
    scanBand,
    sweep,
    cloudPath,
    cloud,
    loadPenFont,
    note,
    placeNote,
    stampSpot,
    stamp,
  };
})();
