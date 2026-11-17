"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const STORAGE_KEY = "ring-sheets/v1";
const NUMERIC = new Set(["mm", "number", "int", "auto_mm"]);
const $ = (selector) => document.querySelector(selector);

const state = {
  schema: null,
  presets: {},
  preset: "",
  result: null, // last /api/generate response
  sheet: 0, // index of the sheet shown
  view: null, // viewBox [x, y, w, h] while zoomed or panned, null = fit
  requestId: 0,
};

// ---------------------------------------------------------------- helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

function svgEl(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.append(...children);
  return node;
}

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      message = (await response.json()).error || message;
    } catch {
      // not a JSON error response
    }
    throw new Error(message);
  }
  return response;
}

const getJSON = async (path, options) => (await api(path, options)).json();
const fmt = (value) => String(Number(Number(value).toFixed(2)));

function parseNumberList(text) {
  const out = [];
  for (const token of text.split(/[\s,;]+/)) {
    const range = /^(\d+)-(\d+)$/.exec(token);
    if (range) for (let n = Number(range[1]); n <= Number(range[2]); n++) out.push(n);
    else if (/^\d+$/.test(token)) out.push(Number(token));
  }
  return out;
}

function normalizeCells(cells) {
  const unique = new Map(cells.map(([r, c]) => [`${r}:${c}`, [Number(r), Number(c)]]));
  return [...unique.values()].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
}

/** "2:5, 2:10" -> [[2, 5], [2, 10]]; null when the text is not valid. */
function parseCells(text) {
  const cells = [];
  for (const token of text.split(/[\s,;]+/)) {
    if (!token) continue;
    const m = /^(\d+):(\d+)$/.exec(token);
    if (!m) return null;
    cells.push([Number(m[1]), Number(m[2])]);
  }
  return normalizeCells(cells);
}

const formatCells = (cells) => cells.map(([r, c]) => `${r}:${c}`).join(", ");
const input = (key) => document.getElementById(`f-${key}`);

// ---------------------------------------------------------------- form

function buildForm() {
  const groups = new Map();
  for (const f of state.schema.fields) {
    let container = $("#codes-fields");
    if (f.group !== "Codes") {
      if (!groups.has(f.group)) {
        // The G-code groups start folded away, as most of the time they are set once per machine.
        const details = el("details", { class: "group", open: !f.group.startsWith("G-code") }, el("summary", {}, f.group));
        $("#form").append(details);
        groups.set(f.group, details);
      }
      container = groups.get(f.group);
    }
    container.append(fieldRow(f));
    if (f.kind === "cells") container.append(patternHelper());
  }
}

function fieldRow(f) {
  const id = `f-${f.key}`;
  let control;
  if (f.kind === "bool") {
    control = el("input", { type: "checkbox", id, name: f.key });
  } else if (f.kind === "choice" || f.kind === "font") {
    const options = f.kind === "font" ? state.schema.fonts : f.choices;
    control = el("select", { id, name: f.key }, ...options.map((o) => el("option", { value: o }, o)));
  } else if (NUMERIC.has(f.kind)) {
    control = el("input", {
      type: "number", id, name: f.key, step: f.kind === "int" ? "1" : "any",
      min: f.minimum ?? undefined, placeholder: f.kind === "auto_mm" ? "auto" : undefined,
    });
  } else {
    control = el("input", {
      type: "text", id, name: f.key, spellcheck: "false",
      placeholder: f.kind === "cells" ? "row:column, …" : undefined,
    });
  }
  const unit = f.kind === "mm" || f.kind === "auto_mm" ? el("span", { class: "unit" }, "mm") : null;
  const classes = ["field", f.kind === "bool" && "check", f.kind === "cells" && "wide"].filter(Boolean);
  return el("div", { class: classes.join(" ") },
    el("label", { class: "name", for: id }, f.label),
    el("span", { class: "control" }, control, unit),
    f.help ? el("p", { class: "help" }, f.help) : null);
}

function patternHelper() {
  const rows = el("input", { type: "text", placeholder: "2, 4", spellcheck: "false" });
  const cols = el("input", { type: "text", placeholder: "5, 10", spellcheck: "false" });
  const apply = el("button", { type: "button" }, "Apply");
  const clear = el("button", { type: "button", class: "quiet" }, "Clear");
  apply.addEventListener("click", () => {
    const r = parseNumberList(rows.value);
    const c = parseNumberList(cols.value);
    if (r.length && c.length) setCells(r.flatMap((row) => c.map((col) => [row, col])));
  });
  clear.addEventListener("click", () => setCells([]));
  return el("div", {},
    el("div", { class: "pattern" },
      el("label", {}, "Rows", rows), el("label", {}, "Columns", cols), apply, clear),
    el("p", { class: "help" }, "Apply skips every combination of the listed rows and columns (ranges like 3-5 work). You can also click plates in the preview."));
}

function readSettings() {
  const out = {};
  for (const f of state.schema.fields) {
    const control = input(f.key);
    if (f.kind === "bool") {
      out[f.key] = control.checked;
    } else if (f.kind === "cells") {
      out[f.key] = parseCells(control.value) ?? control.value; // invalid text: let the server explain
    } else if (NUMERIC.has(f.kind)) {
      const raw = control.value.trim();
      out[f.key] = raw === "" ? (f.kind === "auto_mm" ? null : raw) : Number(raw);
    } else {
      out[f.key] = control.value;
    }
  }
  return out;
}

function writeSettings(settings) {
  for (const f of state.schema.fields) {
    const value = settings[f.key] ?? f.default;
    const control = input(f.key);
    if (f.kind === "bool") control.checked = Boolean(value);
    else if (f.kind === "cells") control.value = formatCells(Array.isArray(value) ? normalizeCells(value) : []);
    else control.value = value ?? "";
  }
}

/** Settings in a comparable form, to tell whether the form differs from the preset. */
function canonical(settings) {
  const out = {};
  for (const f of state.schema.fields) {
    let v = settings[f.key] === undefined ? f.default : settings[f.key];
    if (f.kind === "cells") v = Array.isArray(v) ? normalizeCells(v) : String(v);
    else if (f.kind === "auto_mm") v = v === null || v === "" ? null : Number(v);
    else if (NUMERIC.has(f.kind)) v = Number(v);
    else if (f.kind === "bool") v = Boolean(v);
    else v = String(v ?? "");
    out[f.key] = v;
  }
  return JSON.stringify(out);
}

function setCells(cells) {
  input("skip_cells").value = formatCells(normalizeCells(cells));
  onChange();
}

function toggleCell(row, col) {
  const cells = parseCells(input("skip_cells").value) ?? [];
  const i = cells.findIndex(([r, c]) => r === row && c === col);
  if (i >= 0) cells.splice(i, 1);
  else cells.push([row, col]);
  setCells(cells);
}

// ---------------------------------------------------------------- presets

function renderPresets() {
  if (!(state.preset in state.presets)) state.preset = "";
  $("#preset").replaceChildren(
    el("option", { value: "" }, "(no preset)"),
    ...Object.keys(state.presets).map((name) => el("option", { value: name }, name)));
  $("#preset").value = state.preset;
  updateDirty();
}

function updateDirty() {
  const preset = state.presets[state.preset];
  $("#preset-dirty").hidden = !preset || canonical(preset) === canonical(readSettings());
  $("#preset-delete").disabled = !preset;
}

async function savePreset() {
  const name = prompt("Save the current settings as preset:", state.preset)?.trim();
  if (!name) return;
  if (name !== state.preset && name in state.presets && !confirm(`Replace the preset "${name}"?`)) return;
  try {
    state.presets = await getJSON(`/api/presets/${encodeURIComponent(name)}`,
      { method: "PUT", body: { settings: readSettings() } });
    state.preset = name;
    renderPresets();
    saveLocal();
  } catch (err) {
    showMessages([], err.message);
  }
}

async function deletePreset() {
  if (!state.preset || !confirm(`Delete the preset "${state.preset}"?`)) return;
  try {
    state.presets = await getJSON(`/api/presets/${encodeURIComponent(state.preset)}`, { method: "DELETE" });
    renderPresets();
    saveLocal();
  } catch (err) {
    showMessages([], err.message);
  }
}

async function importPresets(file) {
  try {
    const data = JSON.parse(await file.text());
    const replaced = Object.keys(data).filter((name) => name in state.presets);
    if (replaced.length && !confirm(`Replace these presets?\n\n${replaced.join("\n")}`)) return;
    state.presets = await getJSON("/api/presets/import", { method: "POST", body: data });
    renderPresets();
  } catch (err) {
    showMessages([], `Could not import ${file.name}: ${err.message}`);
  }
}

// ---------------------------------------------------------------- generate + preview

let timer = null;

function onChange() {
  updateDirty();
  saveLocal();
  clearTimeout(timer);
  timer = setTimeout(generate, 250);
}

const payload = (extra = {}) => ({ settings: readSettings(), codes: $("#codes").value, name: $("#name").value, ...extra });

async function generate() {
  const id = ++state.requestId;
  $("#status").textContent = "Updating…";
  try {
    const result = await getJSON("/api/generate", { method: "POST", body: payload() });
    if (id !== state.requestId) return; // a newer request is on its way
    const old = state.result?.sheet;
    if (!old || old.width !== result.sheet.width || old.height !== result.sheet.height) state.view = null;
    state.result = result;
    state.sheet = Math.min(state.sheet, result.sheets.length - 1);
    $("#name").placeholder = result.name;
    renderTabs();
    renderSheet();
    renderStats();
    showMessages(result.warnings);
  } catch (err) {
    if (id === state.requestId) showMessages([], err.message);
  } finally {
    if (id === state.requestId) $("#status").textContent = "";
  }
}

function renderTabs() {
  const sheets = state.result.sheets;
  $("#tabs").replaceChildren(...(sheets.length < 2 ? [] : sheets.map((sheet, i) => {
    const button = el("button", { type: "button", class: i === state.sheet ? "active" : null, title: `${sheet.codes} codes` },
      `Sheet ${sheet.number}`);
    button.addEventListener("click", () => {
      state.sheet = i;
      renderTabs();
      renderSheet();
    });
    return button;
  })));
}

function renderSheet() {
  const { sheets, sheet: size } = state.result;
  const data = sheets[state.sheet];
  const holder = $("#preview");
  holder.innerHTML = data.svg;
  const svg = holder.querySelector("svg");
  svg.removeAttribute("width");
  svg.removeAttribute("height");

  const hatch = svgEl("pattern", { id: "skip-hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
    svgEl("rect", { width: 6, height: 6, fill: "rgba(255, 176, 32, 0.06)" }),
    svgEl("line", { x1: 0, y1: 0, x2: 0, y2: 6, stroke: "rgba(255, 176, 32, 0.35)", "stroke-width": 1.2 }));
  const background = svgEl("rect", { class: "sheet-bg", x: 0, y: 0, width: size.width, height: size.height });
  svg.prepend(svgEl("defs", {}, hatch), background);

  const overlay = svgEl("g", { class: "overlay" });
  for (const p of data.plates) {
    const rect = svgEl("rect", {
      class: ["plate", p.skipped && "skipped", !p.code && "empty", p.code && "has-code",
        selection.plates.has(`${p.row}:${p.col}`) && "selected"].filter(Boolean).join(" "),
      x: p.x, y: p.y, width: p.w, height: p.h, "data-row": p.row, "data-col": p.col,
    });
    const what = p.skipped ? "skipped for staples" : p.code ?? "empty";
    rect.append(svgEl("title", {}, `Row ${p.row}, column ${p.col}: ${what}`));
    overlay.append(rect);
  }
  svg.append(overlay);
  holder.append(measure.readout);
  applyView();
  renderPlateSelection();
}

function renderStats() {
  const s = state.result.stats;
  const g = s.grid;
  const parts = [
    `${s.codes} codes on ${s.sheets} sheet${s.sheets === 1 ? "" : "s"}`,
    `${g.cols} × ${g.rows} plates, ${s.skipped} skipped, ${s.plates_per_sheet} per sheet`,
    `grid ${fmt(g.width)} × ${fmt(g.height)} mm starting at (${fmt(g.x0)}, ${fmt(g.y0)})`,
    `text block ${fmt(s.text_block)} mm high, ${fmt(s.free_below_text)} mm free ${input("text_valign").value === "top" ? "below" : "above and below"}`,
    `widest code ${fmt(s.widest_text)} of ${fmt(s.room_for_text)} mm`,
  ];
  if (s.codes && s.empty_on_last_sheet) parts.push(`${s.empty_on_last_sheet} plates left empty on the last sheet`);
  $("#stats").textContent = parts.join(" · ");
}

function showMessages(warnings, error) {
  $("#messages").replaceChildren(
    ...(error ? [el("div", { class: "message error" }, error)] : []),
    ...warnings.map((w) => el("div", { class: "message warning" }, w)));
}

// ---------------------------------------------------------------- pan + zoom

function fullView() {
  const { width, height } = state.result.sheet;
  const pad = Math.max(width, height) * 0.02;
  return [-pad, -pad, width + 2 * pad, height + 2 * pad];
}

function applyView() {
  const svg = $("#preview svg");
  if (svg) svg.setAttribute("viewBox", (state.view ?? fullView()).join(" "));
  drawOrigin(); // these marks are sized in screen pixels
  drawMeasure();
}

/** A green square where G-code X0 Y0 is. */
function drawOrigin() {
  const svg = $("#preview svg");
  if (!svg) return;
  svg.querySelector(".origin-mark")?.remove();
  const { x, y, name } = state.result.origin;
  const size = 12 * unitsPerPixel();
  svg.append(svgEl("rect", { class: "origin-mark", x: x - size / 2, y: y - size / 2, width: size, height: size },
    svgEl("title", {}, `G-code X0 Y0: ${name}`)));
}

function setupPreview() {
  const preview = $("#preview");
  let drag = null;

  preview.addEventListener("wheel", (e) => {
    const svg = preview.querySelector("svg");
    if (!svg) return;
    e.preventDefault();
    const [x, y, w, h] = state.view ?? fullView();
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(svg.getScreenCTM().inverse());
    const full = fullView()[2];
    const k = Math.min(Math.max(w * Math.exp(e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015)), full / 60), full * 2) / w;
    state.view = [p.x - (p.x - x) * k, p.y - (p.y - y) * k, w * k, h * k];
    applyView();
  }, { passive: false });

  preview.addEventListener("pointerdown", (e) => {
    const svg = preview.querySelector("svg");
    // Left button; while selecting plates, where left-drag draws a box, the right and middle buttons pan.
    if (!svg || (e.button !== 0 && !(selection.on && (e.button === 1 || e.button === 2)))) return;
    if (e.button === 1) e.preventDefault(); // no autoscroll
    drag = { x: e.clientX, y: e.clientY, view: state.view ?? fullView(), moved: false,
      unit: 1 / svg.getScreenCTM().a, target: e.target, box: selection.on && e.button === 0 };
    preview.setPointerCapture(e.pointerId);
  });
  preview.addEventListener("contextmenu", (e) => {
    if (selection.on) e.preventDefault();
  });

  preview.addEventListener("pointermove", (e) => {
    if (measure.on && !drag?.moved) hoverMeasure(e);
    if (!drag) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    drag.moved = true;
    if (drag.box) {
      dragSelectBox(drag, e);
      return;
    }
    preview.classList.add("panning");
    const [x, y, w, h] = drag.view;
    state.view = [x - dx * drag.unit, y - dy * drag.unit, w, h];
    applyView();
  });

  const endDrag = (e) => {
    if (!drag) return;
    const { moved, target, box } = drag;
    drag = null;
    preview.classList.remove("panning");
    if (box && moved) {
      finishSelectBox(e.type === "pointerup");
      return;
    }
    if (e.type !== "pointerup" || moved) return;
    if (measure.on) placeMeasurePoint(e);
    else if (!target.classList?.contains("plate")) return;
    else if (selection.on) toggleSelected(target);
    else toggleCell(Number(target.dataset.row), Number(target.dataset.col));
  };
  preview.addEventListener("pointerup", endDrag);
  preview.addEventListener("pointercancel", endDrag);
  preview.addEventListener("pointerleave", () => {
    if (!measure.on) return;
    measure.hover = null;
    drawMeasure();
  });
  preview.addEventListener("dblclick", () => {
    if (measure.on) return; // two clicks are two measuring points
    state.view = null;
    applyView();
  });
}

// ---------------------------------------------------------------- selecting plates

// In this mode the G-code of the sheet on screen covers only the picked rings (row:column), without staple holes.
const selection = { on: false, plates: new Set(), box: null };

const platesWithCode = () => state.result?.sheets[state.sheet]?.plates.filter((p) => p.code) ?? [];

/** The request for G-code of the sheet on screen, with the picked rings while selecting. */
function gcodePayload(extra = {}) {
  const plates = selection.on
    ? platesWithCode().filter((p) => selection.plates.has(`${p.row}:${p.col}`)).map((p) => [p.row, p.col])
    : undefined;
  return payload({ sheet: state.sheet + 1, plates, ...extra });
}

function renderPlateSelection() {
  const total = platesWithCode().length;
  const picked = platesWithCode().filter((p) => selection.plates.has(`${p.row}:${p.col}`)).length;
  $("#selection-count").textContent = `${picked} of ${total} rings selected`;
  $("#preview").classList.toggle("selecting", selection.on);
  $(".downloads").classList.toggle("selection-active", selection.on);
  $("#selection-bar").hidden = !selection.on;
  $("#preview-hint").hidden = selection.on;
  $("#select-mode").setAttribute("aria-pressed", String(selection.on));
  document.querySelectorAll("#preview .plate").forEach((rect) => {
    rect.classList.toggle("selected", selection.plates.has(`${rect.dataset.row}:${rect.dataset.col}`));
  });
}

function setSelecting(on) {
  if (on && measure.on) setMeasuring(false);
  selection.on = on;
  renderPlateSelection();
}

function toggleSelected(rect) {
  if (!rect.classList.contains("has-code")) return; // no ring there
  const key = `${rect.dataset.row}:${rect.dataset.col}`;
  if (!selection.plates.delete(key)) selection.plates.add(key);
  renderPlateSelection();
}

/** Rings a dragged box touches, as row:column keys. */
function platesInBox(x0, y0, x1, y1) {
  const [left, right] = [Math.min(x0, x1), Math.max(x0, x1)];
  const [top, bottom] = [Math.min(y0, y1), Math.max(y0, y1)];
  return platesWithCode()
    .filter((p) => p.x < right && p.x + p.w > left && p.y < bottom && p.y + p.h > top)
    .map((p) => `${p.row}:${p.col}`);
}

/** While dragging in selection mode: draw the box and mark the rings it will change. It drops them when they
 * are all picked already (or Shift is held), and picks them otherwise. */
function dragSelectBox(drag, e) {
  const svg = $("#preview svg");
  const toSvg = (x, y) => new DOMPoint(x, y).matrixTransform(svg.getScreenCTM().inverse());
  drag.start ??= toSvg(drag.x, drag.y);
  const end = toSvg(e.clientX, e.clientY);
  const keys = platesInBox(drag.start.x, drag.start.y, end.x, end.y);
  drag.remove = e.shiftKey || (keys.length > 0 && keys.every((key) => selection.plates.has(key)));
  let rect = svg.querySelector(".select-box");
  if (!rect) {
    rect = svgEl("rect", { class: "select-box" });
    svg.append(rect);
  }
  rect.classList.toggle("remove", drag.remove);
  rect.setAttribute("x", Math.min(drag.start.x, end.x));
  rect.setAttribute("y", Math.min(drag.start.y, end.y));
  rect.setAttribute("width", Math.abs(end.x - drag.start.x));
  rect.setAttribute("height", Math.abs(end.y - drag.start.y));
  selection.box = { keys: new Set(keys), remove: drag.remove };
  document.querySelectorAll("#preview .plate").forEach((plate) => {
    const inside = selection.box.keys.has(`${plate.dataset.row}:${plate.dataset.col}`);
    plate.classList.toggle("box-add", inside && !drag.remove);
    plate.classList.toggle("box-remove", inside && drag.remove);
  });
}

function finishSelectBox(apply) {
  if (apply && selection.box) {
    for (const key of selection.box.keys) {
      if (selection.box.remove) selection.plates.delete(key);
      else selection.plates.add(key);
    }
  }
  selection.box = null;
  $("#preview .select-box")?.remove();
  document.querySelectorAll("#preview .plate").forEach((plate) => plate.classList.remove("box-add", "box-remove"));
  renderPlateSelection();
}

function setupSelection() {
  $("#select-mode").addEventListener("click", () => setSelecting(!selection.on));
  $("#select-all").addEventListener("click", () => {
    selection.plates = new Set(platesWithCode().map((p) => `${p.row}:${p.col}`));
    renderPlateSelection();
  });
  $("#select-none").addEventListener("click", () => {
    selection.plates.clear();
    renderPlateSelection();
  });
}

// ---------------------------------------------------------------- measuring

const SNAP_PX = 10; // how close, on screen, the cursor must be to snap

const measure = {
  on: false,
  a: null, // first point, { x, y, kind } in preview coordinates (Y down)
  b: null, // second point; while it is null, the line follows the cursor
  hover: null,
  last: null, // last pointer position, to snap again when Shift is pressed or released
  readout: el("div", { class: "measure-readout", hidden: true }),
};

const unitsPerPixel = () => 1 / ($("#preview svg")?.getScreenCTM()?.a || 1);

/** The point under the cursor, snapped to the nearest point (corner, hole, plate centre), else to the nearest
 * line, within SNAP_PX. With free set, exactly where the cursor is. */
function snapPoint(clientX, clientY, free) {
  const p = new DOMPoint(clientX, clientY).matrixTransform($("#preview svg").getScreenCTM().inverse());
  const targets = state.result?.sheets[state.sheet]?.snap;
  if (free || !targets) return { x: p.x, y: p.y, kind: "free" };
  let best = null;
  let bestDistance = SNAP_PX * unitsPerPixel();
  for (const [x, y, kind] of targets.points) {
    const d = Math.hypot(x - p.x, y - p.y);
    if (d <= bestDistance) [best, bestDistance] = [{ x, y, kind }, d];
  }
  if (best) return best;
  for (const [x1, y1, x2, y2] of targets.lines) {
    const dx = x2 - x1;
    const dy = y2 - y1;
    const t = Math.min(1, Math.max(0, ((p.x - x1) * dx + (p.y - y1) * dy) / (dx * dx + dy * dy || 1)));
    const x = x1 + t * dx;
    const y = y1 + t * dy;
    const d = Math.hypot(x - p.x, y - p.y);
    if (d <= bestDistance) [best, bestDistance] = [{ x, y, kind: "on a line" }, d];
  }
  return best ?? { x: p.x, y: p.y, kind: "free" };
}

function hoverMeasure(e) {
  if (!$("#preview svg")) return;
  measure.last = { x: e.clientX, y: e.clientY };
  measure.hover = snapPoint(e.clientX, e.clientY, e.shiftKey);
  drawMeasure();
}

function placeMeasurePoint(e) {
  const p = snapPoint(e.clientX, e.clientY, e.shiftKey);
  if (!measure.a || measure.b) [measure.a, measure.b] = [p, null];
  else measure.b = p;
  drawMeasure();
}

function setMeasuring(on) {
  if (on && selection.on) setSelecting(false);
  Object.assign(measure, { on, a: null, b: null, hover: null });
  $("#measure").setAttribute("aria-pressed", String(on));
  $("#preview").classList.toggle("measuring", on);
  $("#preview-hint").textContent = on
    ? "Click two points to measure · snaps to corners, holes and lines · hold Shift for a free point · Esc to clear, Esc again to stop"
    : "Click a plate to skip it for staples · scroll to zoom · drag to pan · double-click to fit";
  drawMeasure();
}

/** The mark for a point: a square on corners, a circle on holes and centres, a diamond on lines, a cross free. */
function measureMark(p, size, extra = "") {
  const r = size / 2;
  const cls = `mark ${extra}`;
  if (p.kind === "free") return svgEl("path", { class: cls, d: `M${p.x - r},${p.y}h${size}M${p.x},${p.y - r}v${size}` });
  if (p.kind === "on a line") return svgEl("path", { class: cls, d: `M${p.x},${p.y - r}l${r},${r}l${-r},${r}l${-r},${-r}z` });
  if (p.kind.includes("corner")) return svgEl("rect", { class: cls, x: p.x - r, y: p.y - r, width: size, height: size });
  return svgEl("circle", { class: cls, cx: p.x, cy: p.y, r });
}

function drawMeasure() {
  const svg = $("#preview svg");
  if (!svg) return;
  svg.querySelector(".measure")?.remove();
  measure.readout.hidden = !measure.on;
  if (!measure.on) return;

  const px = unitsPerPixel();
  const g = svgEl("g", { class: "measure" });
  const { a } = measure;
  const b = measure.b ?? (a ? measure.hover : null);
  if (a && b) {
    g.append(svgEl("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y }));
    const label = svgEl("text", { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 8 * px, "font-size": 13 * px,
      "stroke-width": 3 * px, "text-anchor": "middle" }, `${fmt(Math.hypot(b.x - a.x, b.y - a.y))} mm`);
    g.append(label);
  }
  for (const p of [a, measure.b]) if (p) g.append(measureMark(p, 9 * px, "fixed"));
  if (measure.hover) g.append(measureMark(measure.hover, 13 * px, "hover"));
  svg.append(g);
  renderMeasureReadout();
}

function renderMeasureReadout() {
  // Positions as the G-code has them: from X0 Y0 (the green square), with Y up.
  const origin = state.result.origin;
  const where = (p) => `X ${fmt(p.x - origin.x)}  Y ${fmt(origin.y - p.y)}`;
  const kind = (p) => (p.kind === "free" ? "" : ` (${p.kind})`);
  const { a, hover } = measure;
  const b = measure.b ?? (a ? hover : null);
  const lines = [];
  if (a && b) {
    lines.push(el("div", { class: "distance" }, `${fmt(Math.hypot(b.x - a.x, b.y - a.y))} mm`),
      el("div", {}, `Δx ${fmt(Math.abs(b.x - a.x))}  Δy ${fmt(Math.abs(b.y - a.y))} mm`),
      el("div", { class: "muted" }, `From ${where(a)}${kind(a)}`),
      el("div", { class: "muted" }, `to ${where(b)}${kind(b)}`));
    if (measure.b && hover) lines.push(el("div", { class: "muted" }, `Pointer ${where(hover)}${kind(hover)}`));
  } else if (hover) {
    lines.push(el("div", {}, `${where(hover)} mm${kind(hover)}`),
      el("div", { class: "muted" }, a ? "" : "Click to start measuring here"));
  } else {
    lines.push(el("div", { class: "muted" }, "Point at the sheet to measure"));
  }
  lines.push(el("div", { class: "muted origin-note" }, `X0 Y0 = G-code origin, ${origin.name} (green square)`));
  measure.readout.replaceChildren(...lines);
}

function setupMeasure() {
  $("#measure").addEventListener("click", () => setMeasuring(!measure.on));
  const typing = (e) => e.target.closest("input, textarea, select, [contenteditable]") || $("dialog[open]");
  document.addEventListener("keydown", (e) => {
    if (typing(e) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "m" || e.key === "M") {
      setMeasuring(!measure.on);
    } else if (e.key === "Escape" && selection.on && !measure.on) {
      setSelecting(false);
    } else if (e.key === "Escape" && measure.on) {
      if (measure.a) {
        [measure.a, measure.b] = [null, null];
        drawMeasure();
      } else {
        setMeasuring(false);
      }
    }
  });
  const reSnap = (e) => { // Shift pressed or released while the cursor stands still
    if (e.key === "Shift" && measure.on && measure.last) hoverMeasure({ clientX: measure.last.x, clientY: measure.last.y, shiftKey: e.type === "keydown" });
  };
  document.addEventListener("keydown", reSnap);
  document.addEventListener("keyup", reSnap);
}

// ---------------------------------------------------------------- CNC share

function showNotice(text) {
  $("#messages").prepend(el("div", { class: "message ok" }, text));
}

function smbResult(text, ok) {
  const box = $("#smb-result");
  box.hidden = false;
  box.className = `smb-result message ${ok ? "ok" : "error"}`;
  box.textContent = text;
}

const smbForm = () => {
  const form = Object.fromEntries(new FormData($("#smb-form")));
  return { ...form, port: form.port || 445 };
};

const clock = (seconds) => new Date(seconds * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });

function renderQueue(queue) {
  state.queue = queue;
  const n = queue.jobs.length;
  $("#queue-badge").hidden = n === 0;
  $("#queue-send-now").hidden = n === 0;
  $("#queue-badge").textContent = `${n} waiting`;
  $("#queue-list").replaceChildren(...queue.jobs.map((job) => {
    const remove = el("button", { type: "button", class: "quiet", title: "Remove from the queue" }, "Remove");
    remove.addEventListener("click", async () => {
      renderQueue(await getJSON(`/api/smb/queue/${encodeURIComponent(job.id)}`, { method: "DELETE" }));
    });
    const sources = job.files.map((name) => ({
      name, load: async () => (await api(`/api/smb/queue/${encodeURIComponent(job.id)}/${encodeURIComponent(name)}`)).text(),
    }));
    const fileItems = job.files.map((name, i) => {
      const view = el("button", { type: "button", class: "link" }, name);
      view.title = "Show in the G-code viewer";
      view.addEventListener("click", () => openGcodeViewer(sources, i));
      return el("li", {}, view);
    });
    return el("li", {},
      el("div", { class: "job" }, el("b", {}, job.label), el("span", { class: "when" }, ` · queued ${clock(job.created)}`), remove),
      el("ul", { class: "job-files" }, ...fileItems),
      job.error ? el("div", { class: "error" }, job.error) : null);
  }));
  if (!n) $("#queue-list").append(el("li", { class: "muted" }, "Nothing is waiting."));
  const info = [];
  if (queue.paused) info.push("Automatic retries are paused after the error above: fix the settings and save, or press Send now.");
  else if (n && queue.next_check) info.push(`Next check ${clock(queue.next_check)}, then every ${Math.round(queue.interval / 60)} minutes.`);
  if (queue.last_check) info.push(`Last checked ${clock(queue.last_check)}: ${queue.online ? "the share answered" : "no answer"}.`);
  $("#queue-info").textContent = info.join(" ");
}

async function refreshQueue() {
  try {
    renderQueue(await getJSON("/api/smb/queue"));
  } catch {
    // the server may be restarting; the next refresh will catch up
  }
}

async function openQueue() {
  $("#queue-result").hidden = true;
  await refreshQueue();
  $("#queue-dialog").showModal();
}

/** Try to send the queue now, from the toolbar or from the queue dialog. */
async function sendQueue(button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Sending…";
  const inDialog = $("#queue-dialog").open;
  const report = (text, ok) => {
    if (inDialog) {
      $("#queue-result").hidden = false;
      $("#queue-result").className = `message ${ok ? "ok" : "warning"}`;
      $("#queue-result").textContent = text;
    } else if (ok) {
      showNotice(text);
    } else {
      showMessages([text, ...(state.result?.warnings ?? [])]);
    }
  };
  try {
    const before = state.queue?.jobs.length ?? 0;
    renderQueue(await getJSON("/api/smb/queue/send", { method: "POST" }));
    const left = state.queue.jobs.length;
    if (!left) report(`Sent everything that was waiting (${before} send${before === 1 ? "" : "s"}).`, true);
    else if (!state.queue.online) report(`The CNC share does not answer yet; ${left} send${left === 1 ? " is" : "s are"} still waiting.`, false);
    else report(`${left} send${left === 1 ? " is" : "s are"} still waiting: ${state.queue.jobs[0].error || state.queue.paused}`, false);
  } catch (err) {
    report(err.message, false);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function viewSheetGcode() {
  try {
    const sheet = await getJSON("/api/gcode", { method: "POST", body: gcodePayload() });
    openGcodeViewer(Object.entries(sheet.files).map(([name, text]) => ({ name, load: async () => text })));
  } catch (err) {
    showMessages(state.result?.warnings ?? [], err.message);
  }
}

// ---------------------------------------------------------------- CNC files

const GCODE_EXTENSIONS = /\.(ngc|nc|gcode|gc|tap|cnc|txt)$/i;
const browse = { path: "/", entries: [], selected: new Set(), share: "" };
const childPath = (folder, name) => `${folder === "/" ? "" : folder}/${name}`;
const parentPath = (path) => path.replace(/\/[^/]*$/, "") || "/";

function filesMessage(text, kind = "error") {
  const box = $("#files-message");
  box.hidden = !text;
  box.className = `message ${kind}`;
  box.textContent = text ?? "";
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function openFiles() {
  $("#files-dialog").showModal();
  await loadFolder(null);
}

/** Show a folder of the share; null is the folder Send to CNC writes to. */
async function loadFolder(path) {
  filesMessage("Loading…", "muted");
  try {
    const data = await getJSON(`/api/smb/files${path === null ? "" : `?path=${encodeURIComponent(path)}`}`);
    Object.assign(browse, { path: data.path, entries: data.entries, share: data.share });
    browse.selected.clear();
    filesMessage(path === null && data.path !== data.folder
      ? `The folder Send to CNC uses (${data.folder}) does not exist yet; it is made on the first send.` : "", "muted");
  } catch (err) {
    browse.entries = [];
    filesMessage(err.message);
  }
  renderFiles();
}

/** Accept files dragged onto a folder (a row or a crumb) and move them there. */
function dropTarget(node, folder) {
  node.addEventListener("dragover", (e) => {
    if (!e.dataTransfer.types.includes("application/x-cnc-paths")) return;
    e.preventDefault();
    node.classList.add("drop");
  });
  node.addEventListener("dragleave", () => node.classList.remove("drop"));
  node.addEventListener("drop", (e) => {
    e.preventDefault();
    node.classList.remove("drop");
    const paths = JSON.parse(e.dataTransfer.getData("application/x-cnc-paths") || "[]").filter((p) => p !== folder);
    if (paths.length) moveFiles(paths, folder);
  });
}

function renderFiles() {
  const crumbs = [el("button", { type: "button", class: "link" }, browse.share || "share")];
  dropTarget(crumbs[0], "/");
  crumbs[0].addEventListener("click", () => loadFolder("/"));
  let path = "";
  for (const part of browse.path.split("/").filter(Boolean)) {
    path += `/${part}`;
    const target = path;
    const crumb = el("button", { type: "button", class: "link" }, part);
    crumb.addEventListener("click", () => loadFolder(target));
    dropTarget(crumb, target);
    crumbs.push(el("span", { class: "sep" }, "\\"), crumb);
  }
  $("#files-crumbs").replaceChildren(...crumbs);
  $("#files-up").disabled = browse.path === "/";

  $("#files-list").replaceChildren(...browse.entries.map((entry) => {
    const path = childPath(browse.path, entry.name);
    const check = el("input", { type: "checkbox", checked: browse.selected.has(path) });
    check.addEventListener("change", () => {
      if (check.checked) browse.selected.add(path);
      else browse.selected.delete(path);
      renderSelection();
    });
    let name;
    if (entry.dir) {
      name = el("button", { type: "button", class: "link folder" }, entry.name);
      name.addEventListener("click", () => loadFolder(path));
    } else {
      name = el("span", { class: "file" }, entry.name);
    }
    const actions = [];
    if (!entry.dir && GCODE_EXTENSIONS.test(entry.name)) {
      const view = el("button", { type: "button" }, "View");
      view.addEventListener("click", () => openGcodeViewer([{
        name: entry.name, load: async () => (await api(`/api/smb/file?path=${encodeURIComponent(path)}`)).text(),
      }]));
      actions.push(view);
    }
    const rename = el("button", { type: "button", class: "quiet" }, "Rename");
    rename.addEventListener("click", () => renameFile(path, entry.name));
    actions.push(rename);
    const row = el("tr", { draggable: "true", class: entry.dir ? "dir" : "" },
      el("td", { class: "check" }, check), el("td", {}, name),
      el("td", { class: "num" }, entry.dir ? "" : formatSize(entry.size)),
      el("td", { class: "muted" }, entry.modified ? clock(entry.modified) : ""),
      el("td", { class: "actions" }, ...actions));
    row.addEventListener("dragstart", (e) => {
      const paths = browse.selected.has(path) ? [...browse.selected] : [path];
      e.dataTransfer.setData("application/x-cnc-paths", JSON.stringify(paths));
      e.dataTransfer.effectAllowed = "move";
    });
    if (entry.dir) dropTarget(row, path);
    return row;
  }));
  if (!browse.entries.length && $("#files-message").hidden) {
    $("#files-list").append(el("tr", {}, el("td", { colspan: 5, class: "muted empty" }, "This folder is empty.")));
  }
  renderSelection();
}

function renderSelection() {
  const n = browse.selected.size;
  $("#files-selected").textContent = n ? `${n} selected` : "";
  $("#files-move").disabled = $("#files-delete").disabled = n === 0;
  $("#files-all").checked = n > 0 && n === browse.entries.length;
}

/** Run a change on the share, then show the folder again with a message. */
async function changeFiles(request, done) {
  try {
    await request();
    await loadFolder(browse.path);
    filesMessage(done, "ok");
  } catch (err) {
    filesMessage(err.message);
  }
}

function newFolder() {
  const name = prompt("Name of the new folder:")?.trim();
  if (!name) return;
  changeFiles(() => getJSON("/api/smb/folder", { method: "POST", body: { path: childPath(browse.path, name) } }),
    `Made the folder ${name}.`);
}

function renameFile(path, oldName) {
  const name = prompt(`New name for ${oldName}:`, oldName)?.trim();
  if (!name || name === oldName) return;
  changeFiles(() => getJSON("/api/smb/rename", { method: "POST", body: { path, name } }), `Renamed ${oldName} to ${name}.`);
}

function moveFiles(paths, folder) {
  const what = paths.length === 1 ? paths[0].split("/").pop() : `${paths.length} items`;
  changeFiles(() => getJSON("/api/smb/move", { method: "POST", body: { paths, to: folder } }),
    `Moved ${what} to ${folder === "/" ? "the top of the share" : folder}.`);
}

function moveSelected() {
  const folders = browse.entries.filter((e) => e.dir).map((e) => childPath(browse.path, e.name));
  const hint = folders.length ? `\nFolders here: ${folders.join(", ")}` : "";
  const folder = prompt(`Move ${browse.selected.size} item(s) to which folder? Use / between folders, / alone for the top of the share.${hint}`,
    folders[0] ?? parentPath(browse.path))?.trim();
  if (!folder) return;
  moveFiles([...browse.selected], folder.startsWith("/") ? folder : childPath(browse.path, folder));
}

function deleteSelected() {
  const paths = [...browse.selected];
  const dirs = browse.entries.filter((e) => e.dir && browse.selected.has(childPath(browse.path, e.name))).length;
  const names = paths.map((p) => p.split("/").pop()).join("\n");
  if (!confirm(`Delete ${paths.length} item(s) from the CNC share?${dirs ? " Folders are deleted with everything in them." : ""}\n\n${names}`)) return;
  changeFiles(() => getJSON("/api/smb/delete", { method: "POST", body: { paths } }), `Deleted ${paths.length} item(s).`);
}

function setupFiles() {
  $("#files-open").addEventListener("click", openFiles);
  $("#files-up").addEventListener("click", () => loadFolder(parentPath(browse.path)));
  $("#files-refresh").addEventListener("click", () => loadFolder(browse.path));
  $("#files-new-folder").addEventListener("click", newFolder);
  $("#files-move").addEventListener("click", moveSelected);
  $("#files-delete").addEventListener("click", deleteSelected);
  $("#files-all").addEventListener("change", (e) => {
    browse.selected = new Set(e.target.checked ? browse.entries.map((entry) => childPath(browse.path, entry.name)) : []);
    renderFiles();
  });
}

async function openSmb() {
  try {
    const config = await getJSON("/api/smb");
    for (const key of ["server", "share", "folder", "username", "port"]) $(`#smb-${key}`).value = config[key] ?? "";
    $("#smb-password").value = "";
    $("#smb-password").placeholder = config.password_set ? "saved" : "";
    $("#smb-result").hidden = true;
    $("#smb-dialog").showModal();
  } catch (err) {
    showMessages(state.result?.warnings ?? [], err.message);
  }
}

async function testSmb() {
  smbResult("Connecting…", true);
  try {
    smbResult((await getJSON("/api/smb/test", { method: "POST", body: smbForm() })).message, true);
  } catch (err) {
    smbResult(err.message, false);
  }
}

async function saveSmb(e) {
  e.preventDefault();
  if (!$("#smb-form").reportValidity()) return;
  try {
    await getJSON("/api/smb", { method: "PUT", body: smbForm() });
    $("#smb-dialog").close();
  } catch (err) {
    smbResult(err.message, false);
  }
}

async function sendToCnc() {
  const button = $("#smb-send");
  try {
    if (!(await getJSON("/api/smb")).configured) {
      openSmb();
      return;
    }
    button.disabled = true;
    button.textContent = "Sending…";
    const result = await getJSON("/api/smb/send", { method: "POST", body: gcodePayload() });
    if (result.sent) {
      showNotice(`Sent ${result.sent.length} file${result.sent.length === 1 ? "" : "s"}: ${result.sent.join(", ")}`);
    } else {
      const n = result.queued.length;
      showMessages([
        `Not sent yet, ${n} file${n === 1 ? " is" : "s are"} waiting in the queue: ${result.reason}. ` +
        `They are sent when the share answers (checked every ${Math.round(result.queue.interval / 60)} minutes).`,
        ...(state.result?.warnings ?? [])]);
    }
    await refreshQueue();
  } catch (err) {
    showMessages(state.result?.warnings ?? [], `Could not send to the CNC share: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "Send to CNC";
  }
}

// ---------------------------------------------------------------- downloads + storage

async function download(format) {
  try {
    const response = await api("/api/download", {
      method: "POST", body: gcodePayload({ format, dwg: state.schema.dwg }), // the server uses plates for G-code only
    });
    const blob = await response.blob();
    const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
    const link = el("a", { href: URL.createObjectURL(blob), download: match ? match[1] : `rings.${format}` });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  } catch (err) {
    showMessages(state.result?.warnings ?? [], err.message);
  }
}

function saveLocal() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      settings: readSettings(), codes: $("#codes").value, name: $("#name").value, preset: state.preset,
    }));
  } catch {
    // storage unavailable: the page still works, it just forgets on reload
  }
}

function loadLocal() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY));
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- start

async function init() {
  try {
    [state.schema, state.presets] = await Promise.all([getJSON("/api/schema"), getJSON("/api/presets")]);
  } catch (err) {
    showMessages([], `Could not reach the server: ${err.message}`);
    return;
  }
  buildForm();
  $('[data-download="dwg"]').hidden = !state.schema.dwg;
  $("#version").textContent = state.schema.version;

  const saved = loadLocal();
  if (saved?.settings) {
    writeSettings(saved.settings);
    $("#codes").value = saved.codes ?? "";
    $("#name").value = saved.name ?? "";
    state.preset = saved.preset ?? "";
  } else {
    state.preset = Object.keys(state.presets)[0] ?? "";
    writeSettings(state.presets[state.preset] ?? {});
    $("#codes").value = "PL37..PM02";
  }
  renderPresets();

  const changed = (e) => {
    if (!e.target.closest(".pattern")) onChange();
  };
  $("#form").addEventListener("input", changed);
  $("#form").addEventListener("submit", (e) => e.preventDefault());
  $("#codes-fields").addEventListener("input", changed);
  $("#codes").addEventListener("input", onChange);
  $("#name").addEventListener("input", saveLocal);
  $("#preset").addEventListener("change", (e) => {
    state.preset = e.target.value;
    if (state.presets[state.preset]) writeSettings(state.presets[state.preset]);
    state.view = null;
    onChange();
  });
  $("#preset-save").addEventListener("click", savePreset);
  $("#preset-delete").addEventListener("click", deletePreset);
  $("#preset-export").addEventListener("click", () => { location.href = "/api/presets/export"; });
  $("#preset-import").addEventListener("click", () => $("#preset-file").click());
  $("#preset-file").addEventListener("change", (e) => {
    if (e.target.files[0]) importPresets(e.target.files[0]);
    e.target.value = "";
  });
  $("#smb-send").addEventListener("click", sendToCnc);
  $("#smb-open").addEventListener("click", openSmb);
  $("#smb-test").addEventListener("click", testSmb);
  $("#smb-cancel").addEventListener("click", () => $("#smb-dialog").close());
  $("#smb-form").addEventListener("submit", saveSmb);
  $("#queue-badge").addEventListener("click", openQueue);
  $("#queue-send").addEventListener("click", (e) => sendQueue(e.currentTarget));
  $("#queue-send-now").addEventListener("click", (e) => sendQueue(e.currentTarget));
  $("#view-gcode").addEventListener("click", viewSheetGcode);
  document.querySelectorAll("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));
  setupFiles();
  setupGcodeViewer();
  refreshQueue();
  setInterval(refreshQueue, 30000);
  document.querySelectorAll("[data-download]").forEach((button) => {
    button.addEventListener("click", () => download(button.dataset.download));
  });
  setupPreview();
  setupMeasure();
  setupSelection();
  generate();
}

init();
