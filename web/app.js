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
        const details = el("details", { class: "group", open: true }, el("summary", {}, f.group));
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
      class: ["plate", p.skipped && "skipped", !p.code && "empty"].filter(Boolean).join(" "),
      x: p.x, y: p.y, width: p.w, height: p.h, "data-row": p.row, "data-col": p.col,
    });
    const what = p.skipped ? "skipped for staples" : p.code ?? "empty";
    rect.append(svgEl("title", {}, `Row ${p.row}, column ${p.col}: ${what}`));
    overlay.append(rect);
  }
  svg.append(overlay);
  applyView();
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
    if (!svg || e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, view: state.view ?? fullView(), moved: false,
      unit: 1 / svg.getScreenCTM().a, target: e.target };
    preview.setPointerCapture(e.pointerId);
  });

  preview.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    drag.moved = true;
    preview.classList.add("panning");
    const [x, y, w, h] = drag.view;
    state.view = [x - dx * drag.unit, y - dy * drag.unit, w, h];
    applyView();
  });

  const endDrag = (e) => {
    if (!drag) return;
    const { moved, target } = drag;
    drag = null;
    preview.classList.remove("panning");
    if (e.type === "pointerup" && !moved && target.classList?.contains("plate")) {
      toggleCell(Number(target.dataset.row), Number(target.dataset.col));
    }
  };
  preview.addEventListener("pointerup", endDrag);
  preview.addEventListener("pointercancel", endDrag);
  preview.addEventListener("dblclick", () => {
    state.view = null;
    applyView();
  });
}

// ---------------------------------------------------------------- downloads + storage

async function download(format) {
  try {
    const response = await api("/api/download", {
      method: "POST", body: payload({ format, sheet: state.sheet + 1, dwg: state.schema.dwg }),
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
  document.querySelectorAll("[data-download]").forEach((button) => {
    button.addEventListener("click", () => download(button.dataset.download));
  });
  setupPreview();
  generate();
}

init();
