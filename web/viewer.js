"use strict";

// G-code viewer: draws the toolpath of a G-code file in a top or 3D view and simulates it over time.
// Used by app.js through openGcodeViewer(files), where files is a list of { name, load: async () => text }.

const RAPID_FEED = 3000; // mm/min assumed for G0 moves, which the file does not give a speed for
const ARC_STEP = Math.PI / 36; // arcs are drawn as straight pieces of at most 5°
const DEPTH_COLOURS = ["#7dd3fc", "#38bdf8", "#818cf8", "#a78bfa", "#c084fc", "#e879f9"]; // shallow to deep, clear of the red rapids

/** Moves of a G-code program as straight segments, with the time each one takes. */
function parseGcode(text) {
  const lines = text.split(/\r?\n/);
  const seg = { x0: [], y0: [], z0: [], x1: [], y1: [], z1: [], rapid: [], line: [], end: [] };
  let x = 0, y = 0, z = 0;
  let motion = 0, absolute = true, scale = 1, feed = 0, time = 0;
  const add = (nx, ny, nz, rapid, line) => {
    const length = Math.hypot(nx - x, ny - y, nz - z);
    if (length < 1e-9) return;
    time += (length / (rapid ? RAPID_FEED : feed || RAPID_FEED)) * 60;
    seg.x0.push(x); seg.y0.push(y); seg.z0.push(z);
    seg.x1.push(nx); seg.y1.push(ny); seg.z1.push(nz);
    seg.rapid.push(rapid ? 1 : 0); seg.line.push(line); seg.end.push(time);
    x = nx; y = ny; z = nz;
  };

  for (let i = 0; i < lines.length; i++) {
    const code = lines[i].replace(/\([^)]*\)/g, "").replace(/;.*/, "").toUpperCase();
    const words = {};
    const gs = [];
    for (const [, letter, value] of code.matchAll(/([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))/g)) {
      if (letter === "G") gs.push(Number(value));
      else words[letter] = Number(value);
    }
    for (const g of gs) {
      if (g >= 0 && g <= 3 && Number.isInteger(g)) motion = g;
      else if (g === 20) scale = 25.4;
      else if (g === 21) scale = 1;
      else if (g === 90) absolute = true;
      else if (g === 91) absolute = false;
    }
    if ("F" in words) feed = words.F * scale;
    if (!("X" in words || "Y" in words || "Z" in words) || gs.some((g) => g === 4 || g === 92 || g === 28 || g === 53)) continue;
    const target = (letter, current) => (letter in words ? words[letter] * scale + (absolute ? 0 : current) : current);
    const nx = target("X", x), ny = target("Y", y), nz = target("Z", z);
    if (motion <= 1) {
      add(nx, ny, nz, motion === 0, i);
      continue;
    }
    // Arcs in the XY plane, around the centre given by I J (relative to the start) or by the radius R.
    let cx, cy;
    if ("R" in words) {
      const r = words.R * scale;
      const mx = (x + nx) / 2, my = (y + ny) / 2;
      const half = Math.hypot(nx - x, ny - y) / 2;
      // Positive R is the shorter arc: its centre is right of the chord for G2, left of it for G3.
      const h = Math.sqrt(Math.max(r * r - half * half, 0)) * ((motion === 2) === (r > 0) ? 1 : -1);
      cx = mx + (h * (ny - y)) / (2 * half || 1);
      cy = my - (h * (nx - x)) / (2 * half || 1);
    } else {
      cx = x + (words.I ?? 0) * scale;
      cy = y + (words.J ?? 0) * scale;
    }
    const r = Math.hypot(x - cx, y - cy);
    const a0 = Math.atan2(y - cy, x - cx);
    let sweep = Math.atan2(ny - cy, nx - cx) - a0;
    if (motion === 3 && sweep <= 1e-9) sweep += 2 * Math.PI; // counter-clockwise
    if (motion === 2 && sweep >= -1e-9) sweep -= 2 * Math.PI; // clockwise
    const steps = Math.max(2, Math.ceil(Math.abs(sweep) / ARC_STEP));
    const z0 = z;
    for (let k = 1; k <= steps; k++) {
      const a = a0 + (sweep * k) / steps;
      add(k === steps ? nx : cx + r * Math.cos(a), k === steps ? ny : cy + r * Math.sin(a), z0 + ((nz - z0) * k) / steps, false, i);
    }
  }

  const n = seg.x0.length;
  const f32 = (a) => Float32Array.from(a);
  const bounds = { minX: Infinity, minY: Infinity, minZ: Infinity, maxX: -Infinity, maxY: -Infinity, maxZ: -Infinity };
  let cut = 0;
  for (let k = 0; k < n; k++) {
    for (const [px, py, pz] of [[seg.x0[k], seg.y0[k], seg.z0[k]], [seg.x1[k], seg.y1[k], seg.z1[k]]]) {
      bounds.minX = Math.min(bounds.minX, px); bounds.maxX = Math.max(bounds.maxX, px);
      bounds.minY = Math.min(bounds.minY, py); bounds.maxY = Math.max(bounds.maxY, py);
      bounds.minZ = Math.min(bounds.minZ, pz); bounds.maxZ = Math.max(bounds.maxZ, pz);
    }
    if (!seg.rapid[k]) cut += Math.hypot(seg.x1[k] - seg.x0[k], seg.y1[k] - seg.y0[k], seg.z1[k] - seg.z0[k]);
  }
  return {
    lines, n, bounds, cut, total: time,
    x0: f32(seg.x0), y0: f32(seg.y0), z0: f32(seg.z0), x1: f32(seg.x1), y1: f32(seg.y1), z1: f32(seg.z1),
    rapid: Uint8Array.from(seg.rapid), line: Uint32Array.from(seg.line), end: Float64Array.from(seg.end),
  };
}

const viewer = {
  files: [], // { name, load }
  program: null,
  view: { yaw: 0, pitch: 0, zoom: 1, panX: 0, panY: 0, cx: 0, cy: 0, cz: 0 },
  t: 0, // simulated time, seconds
  playing: false,
  speed: 10,
  lastFrame: 0,
  canvas: null,
  ctx: null,
};

const vq = (selector) => document.querySelector(`#viewer ${selector}`);

async function openGcodeViewer(files, index = 0) {
  viewer.files = files;
  const dialog = document.getElementById("viewer");
  if (!dialog.open) dialog.showModal();
  vq(".viewer-tabs").replaceChildren(...files.map((f, i) => {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.textContent = f.name;
    tab.addEventListener("click", () => loadViewerFile(i));
    return tab;
  }));
  await loadViewerFile(index);
}

async function loadViewerFile(index) {
  vq(".viewer-tabs").querySelectorAll("button").forEach((b, i) => b.classList.toggle("active", i === index));
  pauseViewer();
  viewer.program = null;
  setViewerStatus("Loading…");
  try {
    viewer.program = parseGcode(await viewer.files[index].load());
  } catch (err) {
    setViewerStatus(`Could not open ${viewer.files[index].name}: ${err.message}`);
    drawViewer();
    return;
  }
  if (!viewer.program.n) setViewerStatus("This file has no moves to show.");
  else setViewerStatus("");
  viewer.t = viewer.program.total; // start with the whole path drawn
  resizeViewer();
  fitViewer();
  renderViewerInfo();
}

function setViewerStatus(text) {
  vq(".viewer-status").textContent = text;
  vq(".viewer-status").hidden = !text;
}

// ---------------------------------------------------------------- view

function setViewerAngle(yaw, pitch) {
  Object.assign(viewer.view, { yaw, pitch });
  vq('[data-view="top"]').classList.toggle("active", pitch === 0 && yaw === 0);
  vq('[data-view="3d"]').classList.toggle("active", pitch !== 0 || yaw !== 0);
  fitViewer();
}

function project(x, y, z) {
  const v = viewer.view;
  const dx = x - v.cx, dy = y - v.cy, dz = z - v.cz;
  const cy = Math.cos(v.yaw), sy = Math.sin(v.yaw);
  const x1 = dx * cy - dy * sy;
  const y1 = dx * sy + dy * cy;
  const up = y1 * Math.cos(v.pitch) + dz * Math.sin(v.pitch);
  return [viewer.canvas.width / 2 + v.panX + x1 * v.zoom, viewer.canvas.height / 2 + v.panY - up * v.zoom];
}

function fitViewer() {
  const p = viewer.program;
  if (!p?.n) return drawViewer();
  const b = p.bounds;
  Object.assign(viewer.view, { cx: (b.minX + b.maxX) / 2, cy: (b.minY + b.maxY) / 2, cz: (b.minZ + b.maxZ) / 2, zoom: 1, panX: 0, panY: 0 });
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const x of [b.minX, b.maxX]) for (const y of [b.minY, b.maxY]) for (const z of [b.minZ, b.maxZ]) {
    const [sx, sy] = project(x, y, z);
    minX = Math.min(minX, sx); maxX = Math.max(maxX, sx); minY = Math.min(minY, sy); maxY = Math.max(maxY, sy);
  }
  const margin = 30 * devicePixelRatio;
  const k = Math.min((viewer.canvas.width - 2 * margin) / Math.max(maxX - minX, 1e-6),
    (viewer.canvas.height - 2 * margin) / Math.max(maxY - minY, 1e-6));
  viewer.view.zoom = k;
  drawViewer();
}

function resizeViewer() {
  const canvas = viewer.canvas;
  const box = canvas.parentElement.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(box.width * devicePixelRatio));
  canvas.height = Math.max(1, Math.round(box.height * devicePixelRatio));
  drawViewer();
}

/** Index of the segment being cut at time t (the last one when t is past the end). */
function segmentAt(t) {
  const end = viewer.program.end;
  let lo = 0, hi = end.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (end[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Where the tool is at time t. */
function toolAt(t) {
  const p = viewer.program;
  const k = segmentAt(t);
  const start = k ? p.end[k - 1] : 0;
  const f = Math.min(1, Math.max(0, (t - start) / (p.end[k] - start || 1)));
  return { k, x: p.x0[k] + (p.x1[k] - p.x0[k]) * f, y: p.y0[k] + (p.y1[k] - p.y0[k]) * f, z: p.z0[k] + (p.z1[k] - p.z0[k]) * f };
}

function drawViewer() {
  const { canvas, ctx } = viewer;
  if (!ctx) return;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#101114";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const p = viewer.program;
  if (!p?.n) return;

  const dpr = devicePixelRatio;
  const tool = toolAt(viewer.t);
  const done = viewer.t >= p.total ? p.n : tool.k; // segments before the tool are cut
  const { minZ } = p.bounds;
  const bucket = (z) => (minZ >= 0 ? 0 : Math.min(DEPTH_COLOURS.length - 1, Math.floor((Math.min(z, 0) / minZ) * DEPTH_COLOURS.length)));

  const paths = { rapidDone: new Path2D(), rapidLeft: new Path2D(), cutDone: DEPTH_COLOURS.map(() => new Path2D()), cutLeft: new Path2D() };
  for (let k = 0; k < p.n; k++) {
    const isDone = k < done;
    const [ax, ay] = project(p.x0[k], p.y0[k], p.z0[k]);
    let [bx, by] = project(p.x1[k], p.y1[k], p.z1[k]);
    if (k === done && done < p.n) [bx, by] = project(tool.x, tool.y, tool.z); // the part already cut
    let path;
    if (p.rapid[k]) path = isDone || k === done ? paths.rapidDone : paths.rapidLeft;
    else path = isDone || k === done ? paths.cutDone[bucket(Math.min(p.z0[k], p.z1[k]))] : paths.cutLeft;
    path.moveTo(ax, ay);
    path.lineTo(bx, by);
    if (k === done && !p.rapid[k]) { // and the rest of it, still to cut
      const [ex, ey] = project(p.x1[k], p.y1[k], p.z1[k]);
      paths.cutLeft.moveTo(bx, by);
      paths.cutLeft.lineTo(ex, ey);
    }
  }
  ctx.lineWidth = 1 * dpr;
  ctx.lineCap = "round";
  ctx.strokeStyle = "rgba(160, 170, 190, 0.18)";
  ctx.stroke(paths.cutLeft);
  ctx.setLineDash([4 * dpr, 4 * dpr]);
  ctx.strokeStyle = "rgba(248, 113, 113, 0.18)";
  ctx.stroke(paths.rapidLeft);
  ctx.strokeStyle = "rgba(248, 113, 113, 0.75)";
  ctx.stroke(paths.rapidDone);
  ctx.setLineDash([]);
  ctx.lineWidth = 1.4 * dpr;
  paths.cutDone.forEach((path, i) => {
    ctx.strokeStyle = DEPTH_COLOURS[i];
    ctx.stroke(path);
  });

  // X0 Y0 Z0: axes and the green square used in the sheet preview.
  const [ox, oy] = project(0, 0, 0);
  const axis = (dx, dy, dz, colour) => {
    const len = 40 * dpr / viewer.view.zoom;
    const [ex, ey] = project(dx * len, dy * len, dz * len);
    ctx.strokeStyle = colour;
    ctx.lineWidth = 2 * dpr;
    ctx.beginPath(); ctx.moveTo(ox, oy); ctx.lineTo(ex, ey); ctx.stroke();
  };
  axis(1, 0, 0, "#ef4444");
  axis(0, 1, 0, "#22c55e");
  if (viewer.view.pitch) axis(0, 0, 1, "#3b82f6");
  ctx.fillStyle = "#22c55e";
  ctx.fillRect(ox - 5 * dpr, oy - 5 * dpr, 10 * dpr, 10 * dpr);

  // The tool: a yellow mark, with a line up to show its height in the 3D view.
  const [tx, ty] = project(tool.x, tool.y, tool.z);
  ctx.strokeStyle = "#facc15";
  ctx.lineWidth = 2 * dpr;
  if (viewer.view.pitch) {
    const [hx, hy] = project(tool.x, tool.y, tool.z + 20 * dpr / viewer.view.zoom);
    ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(hx, hy); ctx.stroke();
  }
  ctx.beginPath(); ctx.arc(tx, ty, 5 * dpr, 0, 2 * Math.PI); ctx.stroke();
}

// ---------------------------------------------------------------- simulation

const clockText = (s) => {
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${h ? `${h}:` : ""}${String(m).padStart(h ? 2 : 1, "0")}:${String(sec).padStart(2, "0")}`;
};
const mm = (v) => String(Number(v.toFixed(3)));

function renderViewerInfo() {
  const p = viewer.program;
  const info = vq(".viewer-info");
  const code = vq(".viewer-code");
  if (!p?.n) {
    info.replaceChildren();
    code.replaceChildren();
    return;
  }
  const tool = toolAt(viewer.t);
  const current = p.line[tool.k];
  const b = p.bounds;
  vq(".viewer-slider").value = String(Math.round((viewer.t / p.total) * 10000));
  vq(".viewer-time").textContent = `${clockText(viewer.t)} / ${clockText(p.total)}`;
  const row = (label, value) => {
    const div = document.createElement("div");
    div.innerHTML = "<span></span><b></b>";
    div.firstChild.textContent = label;
    div.lastChild.textContent = value;
    return div;
  };
  info.replaceChildren(
    row("Tool", `X ${mm(tool.x)}  Y ${mm(tool.y)}  Z ${mm(tool.z)}`),
    row("Line", `${current + 1} of ${p.lines.length}`),
    row("Size", `${mm(b.maxX - b.minX)} × ${mm(b.maxY - b.minY)} mm`),
    row("Depth", `Z ${mm(b.minZ)} to ${mm(b.maxZ)}`),
    row("Cutting", `${(p.cut / 1000).toFixed(2)} m`));

  const from = Math.max(0, current - 6);
  code.replaceChildren(...p.lines.slice(from, current + 9).map((text, i) => {
    const n = from + i;
    const line = document.createElement("div");
    line.className = n === current ? "current" : "";
    line.textContent = `${String(n + 1).padStart(6)}  ${text}`;
    line.addEventListener("click", () => jumpToLine(n));
    return line;
  }));
}

function jumpToLine(n) {
  const p = viewer.program;
  let k = p.line.findIndex((line) => line >= n);
  if (k < 0) k = p.n - 1;
  viewer.t = p.end[k];
  drawViewer();
  renderViewerInfo();
}

function stepLine(direction) {
  const p = viewer.program;
  if (!p?.n) return;
  pauseViewer();
  const current = p.line[toolAt(viewer.t).k];
  if (direction > 0) {
    const k = p.line.findIndex((line) => line > current);
    viewer.t = k < 0 ? p.total : p.end[k];
  } else {
    let k = toolAt(viewer.t).k;
    while (k > 0 && p.line[k] >= current) k--;
    viewer.t = p.line[k] < current ? p.end[k] : 0;
  }
  drawViewer();
  renderViewerInfo();
}

function playViewer() {
  const p = viewer.program;
  if (!p?.n) return;
  if (viewer.t >= p.total) viewer.t = 0;
  viewer.playing = true;
  viewer.lastFrame = performance.now();
  vq(".viewer-play").textContent = "Pause";
  requestAnimationFrame(frameViewer);
}

function pauseViewer() {
  viewer.playing = false;
  vq(".viewer-play").textContent = "Play";
}

function frameViewer(now) {
  if (!viewer.playing) return;
  const p = viewer.program;
  viewer.t = Math.min(p.total, viewer.t + ((now - viewer.lastFrame) / 1000) * viewer.speed);
  viewer.lastFrame = now;
  drawViewer();
  renderViewerInfo();
  if (viewer.t >= p.total) pauseViewer();
  else requestAnimationFrame(frameViewer);
}

// ---------------------------------------------------------------- setup

function setupGcodeViewer() {
  const canvas = vq("canvas");
  viewer.canvas = canvas;
  viewer.ctx = canvas.getContext("2d");
  new ResizeObserver(() => resizeViewer()).observe(canvas.parentElement);

  vq('[data-view="top"]').addEventListener("click", () => setViewerAngle(0, 0));
  vq('[data-view="3d"]').addEventListener("click", () => setViewerAngle(-Math.PI / 6, Math.PI / 3));
  vq('[data-view="fit"]').addEventListener("click", fitViewer);
  vq(".viewer-play").addEventListener("click", () => (viewer.playing ? pauseViewer() : playViewer()));
  vq(".viewer-prev").addEventListener("click", () => stepLine(-1));
  vq(".viewer-next").addEventListener("click", () => stepLine(1));
  vq(".viewer-speed").addEventListener("change", (e) => { viewer.speed = Number(e.target.value); });
  viewer.speed = Number(vq(".viewer-speed").value);
  vq(".viewer-slider").addEventListener("input", (e) => {
    if (!viewer.program?.n) return;
    pauseViewer();
    viewer.t = (Number(e.target.value) / 10000) * viewer.program.total;
    drawViewer();
    renderViewerInfo();
  });
  vq(".viewer-close").addEventListener("click", () => document.getElementById("viewer").close());
  document.getElementById("viewer").addEventListener("close", pauseViewer);
  document.getElementById("viewer").addEventListener("keydown", (e) => {
    if (e.target.closest("select, input")) return;
    if (e.key === " ") { e.preventDefault(); viewer.playing ? pauseViewer() : playViewer(); }
    else if (e.key === "ArrowRight") stepLine(1);
    else if (e.key === "ArrowLeft") stepLine(-1);
  });

  // Mouse: wheel zooms at the cursor; drag pans in the top view and turns the 3D view (Shift-drag always pans).
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const v = viewer.view;
    const r = canvas.getBoundingClientRect();
    const mx = (e.clientX - r.left) * devicePixelRatio - canvas.width / 2;
    const my = (e.clientY - r.top) * devicePixelRatio - canvas.height / 2;
    const k = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015));
    v.zoom *= k;
    v.panX = mx - (mx - v.panX) * k;
    v.panY = my - (my - v.panY) * k;
    drawViewer();
  }, { passive: false });
  let drag = null;
  canvas.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX, y: e.clientY, pan: e.shiftKey || e.button !== 0 || viewer.view.pitch === 0 };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    drag.x = e.clientX;
    drag.y = e.clientY;
    const v = viewer.view;
    if (drag.pan) {
      v.panX += dx * devicePixelRatio;
      v.panY += dy * devicePixelRatio;
    } else {
      v.yaw -= dx * 0.01;
      v.pitch = Math.min(Math.PI / 2, Math.max(0.05, v.pitch - dy * 0.01));
    }
    drawViewer();
  });
  canvas.addEventListener("pointerup", () => { drag = null; });
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener("dblclick", fitViewer);
}
