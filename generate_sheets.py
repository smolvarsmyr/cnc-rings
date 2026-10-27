#!/usr/bin/env python3
"""Generate CNC engraving sheets for colour-ring plates.

Every code gets its own plate (a strip that is later cut out and rolled into
a ring) on which the code is engraved several times. The plates are laid out
in a grid on the sheet and separated by straight lines. Some plates can be
left empty to make room for staples that hold the sheet down, and staple
holes are also placed in the margin around the sheet.

One SVG and/or DXF file is written per sheet, with a separate layer for each
kind of toolpath so CAM software (VCarve, FreeCAD, ...) can pick them up:

    SHEET    outline of the sheet (reference only)
    GRID     straight lines separating the plates
    STAPLES  staple holes (sheet margin and skipped plates)
    TEXT     single-line engraving of the codes

Units are millimetres with the origin in the bottom-left corner of the sheet
(X right, Y up). Rows are numbered from the bottom and columns from the left,
both starting at 1.
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import math
import os
import re
import string
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.sax.saxutils import escape

from stroke_font import Point, Polyline, StrokeFont, load_font, rotate

HERE = Path(__file__).resolve().parent
FONTS_DIR = HERE / "fonts"
# User data (saved presets) lives in CNC_RINGS_DATA, or next to the code when that is not set.
# presets.default.json ships with the code and is used until presets are first saved.
DATA_DIR = Path(os.environ.get("CNC_RINGS_DATA") or HERE)
PRESETS_FILE = DATA_DIR / "presets.json"
DEFAULT_PRESETS_FILE = HERE / "presets.default.json"
EPS = 1e-6
MAX_RANGE = 100_000

# Layer name -> (DXF colour index, SVG stroke colour), in drawing order.
LAYERS = {
    "SHEET": (8, "#808080"),
    "GRID": (5, "#0057b8"),
    "STAPLES": (3, "#1a9e3a"),
    "TEXT": (1, "#d00000"),
}


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    key: str
    default: Any
    kind: str  # mm | number | int | bool | choice | font | text | auto_mm | cells
    group: str
    label: str
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None


def _gcode_fields(op: str, group: str, tool: int, name: str, rpm: int, feed: float, plunge: float, depth: float,
                  step: float, diameter: float | None = None) -> list[Field]:
    """Settings for one G-code operation, all named gcode_<op>_..."""
    p = f"gcode_{op}_"
    fields = [
        Field(p + "enabled", True, "bool", group, "Export", "Write G-code for this operation."),
        Field(p + "tool", tool, "int", group, "Tool number",
              "Operations with the same tool number share a file, so tools are only changed between files.",
              minimum=0),
        Field(p + "tool_name", name, "text", group, "Tool", "Written into the file as a reminder of what to fit."),
    ]
    if diameter is not None:
        fields.append(Field(p + "tool_diameter", diameter, "mm", group, "Tool diameter",
                            "Holes wider than the tool are milled out from the middle; otherwise the tool plunges "
                            "straight in.", minimum=0.1))
    return fields + [
        Field(p + "rpm", rpm, "int", group, "Spindle speed (rpm)",
              "0 = no spindle commands, e.g. for a router switched on by hand.", minimum=0),
        Field(p + "feed", feed, "number", group, "Feed rate (mm/min)", minimum=1),
        Field(p + "plunge", plunge, "number", group, "Plunge rate (mm/min)", minimum=1),
        Field(p + "depth", depth, "mm", group, "Cut depth", "Below the top of the material.", minimum=0.01),
        Field(p + "step", step, "mm", group, "Depth per pass", "0 = the full depth in one pass.", minimum=0),
    ]


FIELDS = (
    Field("sheet_width", 600.0, "mm", "Sheet", "Sheet width", minimum=1),
    Field("sheet_height", 600.0, "mm", "Sheet", "Sheet height", minimum=1),
    Field("margin", 12.0, "mm", "Sheet", "Minimum margin",
          "Kept free along every edge. The plate grid is centred on the sheet.", minimum=0),
    Field("sheet_outline", True, "bool", "Sheet", "Draw sheet outline", "Reference rectangle on the SHEET layer."),

    Field("plate_width", 40.0, "mm", "Plates", "Plate width", minimum=1),
    Field("plate_height", 115.0, "mm", "Plates", "Plate height", minimum=1),
    Field("columns", 0, "int", "Plates", "Columns", "0 = as many as fit.", minimum=0),
    Field("rows", 0, "int", "Plates", "Rows", "0 = as many as fit.", minimum=0),
    Field("fill_from", "bottom", "choice", "Plates", "Start filling from",
          "Codes fill each row left to right, starting with the bottom or the top row.", ("bottom", "top")),

    Field("font", "hershey_sans.json", "font", "Text", "Font"),
    Field("copies", 4, "int", "Text", "Copies per plate", minimum=1),
    Field("text_height", 15.5, "mm", "Text", "Text height", "Height of the capital letters.", minimum=0.1),
    Field("line_pitch", 21.5, "mm", "Text", "Distance between text rows",
          "Centre to centre (baseline to baseline).", minimum=0),
    Field("padding_top", 5.5, "mm", "Text", "Top padding",
          "From the top edge of the plate to the top of the first row.", minimum=0),
    Field("padding_side", 3.0, "mm", "Text", "Side padding",
          "Minimum free space left and right of the text (warns when a code is wider).", minimum=0),
    Field("text_valign", "top", "choice", "Text", "Vertical position",
          "Top uses the top padding; centre puts the text block in the middle of the plate.", ("top", "center")),
    Field("width_scale", 0.6, "number", "Text", "Width scale", "Horizontal stretch of the font; below 1 condenses.",
          minimum=0.05),
    Field("letter_spacing", 0.0, "mm", "Text", "Extra letter spacing"),
    Field("text_angle", 0.0, "number", "Text", "Text rotation (°)",
          "Counter-clockwise. 90 runs the code along the length of the plate."),

    Field("range_skip_letters", "", "text", "Codes", "Letters left out of ranges",
          "For example IOQ: ranges like AB..AZ then never produce codes containing these letters."),

    Field("skip_cells", [], "cells", "Skipped plates", "Skipped plates",
          "row:column pairs, e.g. 2:5, 2:10. Rows count from the bottom, columns from the left."),

    Field("edge_staples_x", 4, "int", "Edge staples", "Staples along bottom and top", minimum=0),
    Field("edge_staples_y", 4, "int", "Edge staples", "Staples along left and right", minimum=0),
    Field("edge_corner_offset", 55.0, "mm", "Edge staples", "Distance from corner",
          "From the sheet corner to the centre of the first and last staple on each edge.", minimum=0),
    Field("edge_inset", None, "auto_mm", "Edge staples", "Distance from edge",
          "From the sheet edge to the hole centres. Empty = centred in the margin.", minimum=0),
    Field("edge_leg_spacing", 10.0, "mm", "Edge staples", "Leg spacing",
          "Centre distance between the two holes of one staple. 0 = a single hole.", minimum=0),
    Field("edge_hole_diameter", 5.0, "mm", "Edge staples", "Hole diameter / slot width", minimum=0.1),
    Field("edge_slot_length", 0.0, "mm", "Edge staples", "Slot length",
          "Overall length of slotted holes, across the staple. 0 = round holes.", minimum=0),

    Field("cell_staples", 1, "int", "Staples in skipped plates", "Staples per plate", minimum=0),
    Field("cell_staple_pitch", 30.0, "mm", "Staples in skipped plates", "Distance between staples",
          "Used when there is more than one staple per plate; they are stacked vertically.", minimum=0),
    Field("cell_leg_axis", "vertical", "choice", "Staples in skipped plates", "Leg direction",
          choices=("vertical", "horizontal")),
    Field("cell_leg_spacing", 10.0, "mm", "Staples in skipped plates", "Leg spacing", minimum=0),
    Field("cell_hole_diameter", 3.5, "mm", "Staples in skipped plates", "Hole diameter / slot width", minimum=0.1),
    Field("cell_slot_length", 8.5, "mm", "Staples in skipped plates", "Slot length",
          "Overall length of slotted holes, across the staple. 0 = round holes.", minimum=0),

    Field("dxf_version", "R2000", "choice", "Output", "DXF version",
          "R2000 works in VCarve and FreeCAD; R12 for very old software.", ("R12", "R2000", "R2010", "R2018")),

    Field("gcode_safe_z", 5.0, "mm", "G-code", "Safe height",
          "For all moves between cuts. Z0 is the top of the material.", minimum=0.5),
    Field("gcode_plunge_z", 1.0, "mm", "G-code", "Rapid down to",
          "Height above the material where the tool slows down to the plunge rate.", minimum=0),
    Field("gcode_origin", "top-left", "choice", "G-code", "X0 Y0 is at",
          "The point of the sheet the machine is zeroed on.", ("bottom-left", "top-left", "center")),
    Field("gcode_ramp_angle", 22.5, "number", "G-code", "Ramp angle (°)",
          "Lines and slots are entered by ramping down along the first bit of the path and back. "
          "0 = plunge straight down.", minimum=0),
    Field("gcode_direction", "climb", "choice", "G-code", "Milling direction",
          "For staple holes wider than the tool, with the spindle turning clockwise (M3).",
          ("climb", "conventional")),
    Field("gcode_spinup", 3.0, "number", "G-code", "Spindle spin-up wait (s)", minimum=0),
    Field("gcode_park_x", None, "auto_mm", "G-code", "Park at X",
          "Where the tool goes when a file ends, in G-code coordinates. Empty = stay above the last cut."),
    Field("gcode_park_y", None, "auto_mm", "G-code", "Park at Y"),
    Field("gcode_extension", "ngc", "choice", "G-code", "File extension", choices=("ngc", "nc", "gcode", "tap", "cnc")),

    *_gcode_fields("staples", "G-code: staple holes", 1, "3 mm 2 flute end mill", 18000, feed=1500.0, plunge=400.0,
                   depth=2.5, step=1.0, diameter=3.0),
    *_gcode_fields("text", "G-code: text", 2, "engraving bit", 1000, feed=600.0, plunge=120.0, depth=0.2, step=0.0),
    *_gcode_fields("grid", "G-code: grid lines", 3, "45° 2 flute V-bit", 18000, feed=1143.0, plunge=400.0,
                   depth=0.7, step=0.0),
)
FIELD_BY_KEY = {f.key: f for f in FIELDS}


def default_settings() -> dict[str, Any]:
    return {f.key: copy.deepcopy(f.default) for f in FIELDS}


def normalize_settings(raw: dict[str, Any] | None = None) -> tuple[SimpleNamespace, list[str]]:
    """Merge ``raw`` onto the defaults and validate it. Raises ValueError on bad values."""
    values = default_settings()
    warnings = []
    for key, value in (raw or {}).items():
        f = FIELD_BY_KEY.get(key)
        if f is None:
            warnings.append(f"Unknown setting '{key}' ignored.")
            continue
        values[key] = _coerce(f, value)
    return SimpleNamespace(**values), warnings


def _coerce(f: Field, value: Any) -> Any:
    if f.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if f.kind == "auto_mm" and (value is None or str(value).strip().lower() in ("", "auto", "none")):
        return None
    if f.kind in ("mm", "number", "int", "auto_mm"):
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{f.label}: '{value}' is not a number") from None
        if not math.isfinite(num):
            raise ValueError(f"{f.label}: '{value}' is not a number")
        if f.kind == "int":
            if num != int(num):
                raise ValueError(f"{f.label}: must be a whole number")
            num = int(num)
        if f.minimum is not None and num < f.minimum:
            raise ValueError(f"{f.label}: must be at least {f.minimum:g}")
        return num
    if f.kind == "choice":
        match = {c.lower(): c for c in f.choices}.get(str(value).strip().lower())
        if match is None:
            raise ValueError(f"{f.label}: must be one of {', '.join(f.choices)}")
        return match
    if f.kind == "cells":
        return parse_cells(value)
    return str(value).strip()


def parse_cells(value: Any) -> list[list[int]]:
    """Parse skipped plates given as "2:5, 2:10" or [[2, 5], [2, 10]]."""
    if isinstance(value, str):
        pairs = []
        for token in re.split(r"[\s,;]+", value.strip()):
            if not token:
                continue
            m = re.fullmatch(r"(\d+):(\d+)", token)
            if not m:
                raise ValueError(f"Skipped plates: '{token}' is not row:column")
            pairs.append((int(m[1]), int(m[2])))
    else:
        try:
            pairs = [(int(r), int(c)) for r, c in value or []]
        except (TypeError, ValueError):
            raise ValueError("Skipped plates: expected a list of [row, column] pairs") from None
    if any(r < 1 or c < 1 for r, c in pairs):
        raise ValueError("Skipped plates: rows and columns start at 1")
    return [list(p) for p in sorted(set(pairs))]


def format_cells(cells: list[list[int]]) -> str:
    return ", ".join(f"{r}:{c}" for r, c in cells)


# --------------------------------------------------------------------------
# Codes
# --------------------------------------------------------------------------

def parse_codes(text: str, skip_letters: str = "") -> list[str]:
    """Split a list of codes and expand ranges.

    Codes are separated by new lines, commas, semicolons or spaces. A range is
    written FIRST..LAST (or FIRST-LAST when both ends have the same pattern of
    letters and digits) and counts like an odometer: PL98..PM01 gives PL98,
    PL99, PM00, PM01.
    """
    codes: list[str] = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        if ".." in token:
            first, _, last = token.partition("..")
            codes.extend(expand_range(first, last, skip_letters))
        elif token.count("-") == 1 and _same_pattern(*token.split("-")):
            codes.extend(expand_range(*token.split("-"), skip_letters))
        else:
            codes.append(token)
    return codes


def _same_pattern(a: str, b: str) -> bool:
    return (len(a) == len(b) >= 2
            and all(x.isdigit() == y.isdigit() and x.isalpha() == y.isalpha() for x, y in zip(a, b)))


def expand_range(first: str, last: str, skip_letters: str = "") -> list[str]:
    label = f"Range {first}..{last}"
    if not first or len(first) != len(last):
        raise ValueError(f"{label}: both ends must have the same number of characters")
    skip = set(skip_letters.upper())
    prefix = next((i for i, (a, b) in enumerate(zip(first, last)) if a != b), len(first))

    alphabets = []
    for i, (a, b) in enumerate(zip(first, last)):
        if i < prefix:
            alphabets.append(a)  # common prefix never changes
        elif a.isdigit() and b.isdigit():
            alphabets.append(string.digits)
        elif a.isalpha() and b.isalpha() and a.isupper() == b.isupper():
            letters = string.ascii_uppercase if a.isupper() else string.ascii_lowercase
            alphabets.append("".join(ch for ch in letters if ch.upper() not in skip))
        else:
            raise ValueError(f"{label}: '{a}' and '{b}' are not both digits or both letters of the same case")

    def indices(code: str) -> list[int]:
        out = []
        for ch, alphabet in zip(code, alphabets):
            if ch not in alphabet:
                raise ValueError(f"{label}: '{ch}' is one of the letters left out of ranges")
            out.append(alphabet.index(ch))
        return out

    pos, end = indices(first), indices(last)
    if end < pos:
        raise ValueError(f"{label}: the last code comes before the first")
    codes = []
    while True:
        codes.append("".join(alphabet[i] for alphabet, i in zip(alphabets, pos)))
        if pos == end:
            return codes
        if len(codes) >= MAX_RANGE:
            raise ValueError(f"{label}: more than {MAX_RANGE} codes")
        k = len(pos) - 1
        while True:  # odometer increment; cannot overflow because end > pos
            pos[k] += 1
            if pos[k] < len(alphabets[k]):
                break
            pos[k] = 0
            k -= 1


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

@dataclass
class Polylines:
    lines: list[Polyline]
    closed: bool = False


@dataclass
class Circle:
    center: Point
    radius: float


@dataclass
class Slot:
    """Rounded slot: a stadium between the centres ``a`` and ``b`` of its end arcs."""
    a: Point
    b: Point
    radius: float


Shape = Polylines | Circle | Slot


@dataclass
class Grid:
    x0: float
    y0: float
    cols: int
    rows: int
    plate_w: float
    plate_h: float

    @property
    def width(self) -> float:
        return self.cols * self.plate_w

    @property
    def height(self) -> float:
        return self.rows * self.plate_h

    def plate_rect(self, row: int, col: int) -> tuple[float, float, float, float]:
        return (self.x0 + (col - 1) * self.plate_w, self.y0 + (row - 1) * self.plate_h, self.plate_w, self.plate_h)


@dataclass
class Plate:
    row: int
    col: int
    rect: tuple[float, float, float, float]
    code: str | None = None
    skipped: bool = False


@dataclass
class Sheet:
    number: int
    plates: list[Plate]
    layers: dict[str, list[Shape]]


@dataclass
class Job:
    settings: SimpleNamespace
    codes: list[str]
    grid: Grid
    sheets: list[Sheet]
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeShape:
    strokes: list[Polyline]  # centred on (0, 0), already rotated
    width: float  # size of the rotated text box along X
    height: float  # and along Y


def compute_grid(s: SimpleNamespace) -> Grid:
    avail_w = s.sheet_width - 2 * s.margin
    avail_h = s.sheet_height - 2 * s.margin
    cols = s.columns or int((avail_w + EPS) // s.plate_width)
    rows = s.rows or int((avail_h + EPS) // s.plate_height)
    if cols < 1 or rows < 1:
        raise ValueError(f"No plates fit: {s.plate_width:g} x {s.plate_height:g} mm plates, "
                         f"but only {avail_w:g} x {avail_h:g} mm inside the margins")
    w, h = cols * s.plate_width, rows * s.plate_height
    return Grid((s.sheet_width - w) / 2, (s.sheet_height - h) / 2, cols, rows, s.plate_width, s.plate_height)


def shape_code(code: str, s: SimpleNamespace, font: StrokeFont) -> tuple[CodeShape, set[str]]:
    strokes, missing = font.render(code, s.text_height, s.width_scale, s.letter_spacing)
    xs = [x for stroke in strokes for x, _ in stroke]
    xmin, xmax = (min(xs), max(xs)) if xs else (0.0, 0.0)
    cx, cy = (xmin + xmax) / 2, s.text_height / 2
    strokes = [rotate([(x - cx, y - cy) for x, y in stroke], s.text_angle) for stroke in strokes]
    w, h = xmax - xmin, s.text_height
    a = math.radians(s.text_angle)
    return CodeShape(strokes, abs(w * math.cos(a)) + abs(h * math.sin(a)),
                     abs(w * math.sin(a)) + abs(h * math.cos(a))), missing


def text_block_height(s: SimpleNamespace, row_height: float) -> float:
    return (s.copies - 1) * s.line_pitch + row_height


def text_centres(rect: tuple[float, float, float, float], row_height: float, s: SimpleNamespace) -> list[Point]:
    x, y, w, h = rect
    if s.text_valign == "top":
        first = y + h - s.padding_top - row_height / 2
    else:
        first = y + h / 2 + text_block_height(s, row_height) / 2 - row_height / 2
    return [(x + w / 2, first - i * s.line_pitch) for i in range(s.copies)]


def grid_lines(grid: Grid) -> list[Shape]:
    x1, y1 = grid.x0 + grid.width, grid.y0 + grid.height
    lines: list[Shape] = []
    for r in range(grid.rows + 1):
        y = grid.y0 + r * grid.plate_h
        lines.append(Polylines([[(grid.x0, y), (x1, y)]]))
    for c in range(grid.cols + 1):
        x = grid.x0 + c * grid.plate_w
        lines.append(Polylines([[(x, grid.y0), (x, y1)]]))
    return lines


def staple(center: Point, vertical: bool, spacing: float, hole_d: float, slot_len: float) -> list[Shape]:
    """Holes for one staple: two legs ``spacing`` apart along the leg axis."""
    cx, cy = center
    ux, uy = (0.0, 1.0) if vertical else (1.0, 0.0)  # leg axis
    px, py = uy, ux  # across the staple, the direction slots are stretched in
    half = spacing / 2
    legs = [(cx - ux * half, cy - uy * half), (cx + ux * half, cy + uy * half)] if spacing > 0 else [center]
    r = hole_d / 2
    stretch = max(0.0, (slot_len - hole_d) / 2)
    if stretch <= 0:
        return [Circle(leg, r) for leg in legs]
    return [Slot((lx - px * stretch, ly - py * stretch), (lx + px * stretch, ly + py * stretch), r) for lx, ly in legs]


def edge_staples(s: SimpleNamespace, grid: Grid) -> list[Shape]:
    W, H = s.sheet_width, s.sheet_height
    margin = {"bottom": grid.y0, "top": H - grid.y0 - grid.height,
              "left": grid.x0, "right": W - grid.x0 - grid.width}

    def inset(side: str) -> float:
        return s.edge_inset if s.edge_inset is not None else margin[side] / 2

    def spots(length: float, n: int) -> list[float]:
        if n <= 0:
            return []
        if n == 1:
            return [length / 2]
        a, b = s.edge_corner_offset, length - s.edge_corner_offset
        return [a + (b - a) * k / (n - 1) for k in range(n)]

    hole = (s.edge_leg_spacing, s.edge_hole_diameter, s.edge_slot_length)
    shapes: list[Shape] = []
    for x in spots(W, s.edge_staples_x):
        shapes += staple((x, inset("bottom")), False, *hole)
    for y in spots(H, s.edge_staples_y):
        shapes += staple((W - inset("right"), y), True, *hole)
    for x in reversed(spots(W, s.edge_staples_x)):
        shapes += staple((x, H - inset("top")), False, *hole)
    for y in reversed(spots(H, s.edge_staples_y)):
        shapes += staple((inset("left"), y), True, *hole)
    return shapes


def plate_staples(s: SimpleNamespace, grid: Grid, cells: list[tuple[int, int]]) -> list[Shape]:
    shapes: list[Shape] = []
    n = s.cell_staples
    for row, col in cells:
        x, y, w, h = grid.plate_rect(row, col)
        for k in range(n):
            cy = y + h / 2 + ((n - 1) / 2 - k) * s.cell_staple_pitch
            shapes += staple((x + w / 2, cy), s.cell_leg_axis == "vertical",
                             s.cell_leg_spacing, s.cell_hole_diameter, s.cell_slot_length)
    return shapes


def shape_bbox(shape: Shape) -> tuple[float, float, float, float]:
    if isinstance(shape, Circle):
        (x, y), r = shape.center, shape.radius
        return x - r, y - r, x + r, y + r
    if isinstance(shape, Slot):
        r = shape.radius
        return (min(shape.a[0], shape.b[0]) - r, min(shape.a[1], shape.b[1]) - r,
                max(shape.a[0], shape.b[0]) + r, max(shape.a[1], shape.b[1]) + r)
    xs = [x for line in shape.lines for x, _ in line]
    ys = [y for line in shape.lines for _, y in line]
    return min(xs), min(ys), max(xs), max(ys)


def _overlaps(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] < b[2] - EPS and b[0] < a[2] - EPS and a[1] < b[3] - EPS and b[1] < a[3] - EPS


def _inside(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] >= b[0] - EPS and a[1] >= b[1] - EPS and a[2] <= b[2] + EPS and a[3] <= b[3] + EPS


def _list(items: list[str], limit: int = 8) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def build_job(codes: list[str], s: SimpleNamespace, font: StrokeFont | None = None) -> Job:
    font = font or get_font(s.font)
    grid = compute_grid(s)
    warnings: list[str] = []

    avail_w, avail_h = s.sheet_width - 2 * s.margin, s.sheet_height - 2 * s.margin
    if grid.width > avail_w + EPS or grid.height > avail_h + EPS:
        warnings.append(f"The plate grid ({grid.width:g} x {grid.height:g} mm) is larger than the space inside "
                        f"the margins ({avail_w:g} x {avail_h:g} mm).")

    skip_all = {tuple(c) for c in s.skip_cells}
    skipped = sorted(c for c in skip_all if c[0] <= grid.rows and c[1] <= grid.cols)
    if len(skipped) < len(skip_all):
        outside = [f"{r}:{c}" for r, c in sorted(skip_all - set(skipped))]
        warnings.append(f"Skipped plates outside the {grid.cols} x {grid.rows} grid are ignored: {_list(outside)}.")
    row_order = range(1, grid.rows + 1) if s.fill_from == "bottom" else range(grid.rows, 0, -1)
    free = [(r, c) for r in row_order for c in range(1, grid.cols + 1) if (r, c) not in skip_all]
    if not free:
        raise ValueError("Every plate on the sheet is skipped")

    dupes = sorted(code for code, n in Counter(codes).items() if n > 1)
    if dupes:
        warnings.append(f"Codes listed more than once: {_list(dupes)}.")

    # Text: shape every distinct code once and check that it fits its plate.
    shapes: dict[str, CodeShape] = {}
    missing: set[str] = set()
    for code in dict.fromkeys(codes):
        shapes[code], miss = shape_code(code, s, font)
        missing |= miss
    if missing:
        warnings.append(f"The font '{font.name}' has no glyph for: {' '.join(sorted(missing))}")

    room_w = s.plate_width - 2 * s.padding_side
    too_wide = [c for c, sh in shapes.items() if sh.width > room_w + EPS]
    if too_wide:
        widest = max(shapes[c].width for c in too_wide)
        warnings.append(f"Text wider than the plate allows ({room_w:g} mm between the side paddings, "
                        f"widest {widest:.1f} mm): {_list(too_wide)}.")
    row_h = max((sh.height for sh in shapes.values()), default=s.text_height)
    block = text_block_height(s, row_h)
    used = (s.padding_top + block) if s.text_valign == "top" else block
    if used > s.plate_height + EPS:
        warnings.append(f"The text block needs {used:.1f} mm but the plate is only {s.plate_height:g} mm high.")
    if s.copies > 1 and s.line_pitch < row_h - EPS:
        warnings.append(f"Text rows overlap: the distance between rows ({s.line_pitch:g} mm) is less than "
                        f"the height of a row ({row_h:.1f} mm).")

    # Staples and the drawing layers shared by every sheet.
    edge = edge_staples(s, grid)
    cell = plate_staples(s, grid, skipped)
    sheet_box = (0.0, 0.0, s.sheet_width, s.sheet_height)
    grid_box = (grid.x0, grid.y0, grid.x0 + grid.width, grid.y0 + grid.height)
    if any(_overlaps(shape_bbox(h), grid_box) for h in edge):
        warnings.append("Edge staple holes reach into the plate grid; increase the margin, "
                        "the distance from the corner or reduce the hole size.")
    if any(not _inside(shape_bbox(h), sheet_box) for h in edge):
        warnings.append("Edge staple holes extend past the edge of the sheet.")
    for row, col in skipped:
        x, y, w, h = grid.plate_rect(row, col)
        if any(not _inside(shape_bbox(hole), (x, y, x + w, y + h)) for hole in plate_staples(s, grid, [(row, col)])):
            warnings.append("Staple holes in skipped plates extend past the plate edges.")
            break

    if s.gcode_staples_enabled and edge + cell:
        smallest = 2 * min(hole.radius for hole in edge + cell)
        if s.gcode_staples_tool_diameter > smallest + EPS:
            warnings.append(f"G-code: the staple tool ({s.gcode_staples_tool_diameter:g} mm) is wider than the "
                            f"smallest staple hole ({smallest:g} mm), so those holes come out too big.")
    if any(getattr(s, f"gcode_{op}_enabled") for op, _, _ in GCODE_OPS) and s.gcode_plunge_z > s.gcode_safe_z:
        warnings.append("G-code: 'Rapid down to' is above the safe height, so the safe height is used for both.")

    static: dict[str, list[Shape]] = {
        "SHEET": [Polylines([[(0, 0), (s.sheet_width, 0), (s.sheet_width, s.sheet_height), (0, s.sheet_height)]],
                            closed=True)] if s.sheet_outline else [],
        "GRID": grid_lines(grid),
        "STAPLES": edge + cell,
    }

    sheets = []
    chunks = [codes[i:i + len(free)] for i in range(0, len(codes), len(free))] or [[]]
    for number, chunk in enumerate(chunks, 1):
        assigned = dict(zip(free, chunk))  # keeps the fill order
        text: list[Shape] = []
        for (row, col), code in assigned.items():
            sh = shapes[code]
            for cx, cy in text_centres(grid.plate_rect(row, col), sh.height, s):
                text.append(Polylines([[(x + cx, y + cy) for x, y in stroke] for stroke in sh.strokes]))
        plates = [Plate(r, c, grid.plate_rect(r, c), assigned.get((r, c)), (r, c) in skip_all)
                  for r in range(1, grid.rows + 1) for c in range(1, grid.cols + 1)]
        sheets.append(Sheet(number, plates, {**static, "TEXT": text}))

    free_below = s.plate_height - used if s.text_valign == "top" else (s.plate_height - block) / 2
    stats = {
        "codes": len(codes),
        "sheets": len(sheets),
        "plates_per_sheet": len(free),
        "skipped": len(skipped),
        "empty_on_last_sheet": len(free) - len(chunks[-1]),
        "grid": {"cols": grid.cols, "rows": grid.rows, "width": grid.width, "height": grid.height,
                 "x0": grid.x0, "y0": grid.y0},
        "text_block": round(block, 3),
        "free_below_text": round(free_below, 3),
        "widest_text": round(max((sh.width for sh in shapes.values()), default=0.0), 3),
        "room_for_text": room_w,
    }
    return Job(s, codes, grid, sheets, warnings, stats)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def slot_outline(slot: Slot) -> tuple[Point, Point, Point, Point]:
    """Corners of a slot, counter-clockwise; the arcs run v1->v2 and v3->v0."""
    (ax, ay), (bx, by), r = slot.a, slot.b, slot.radius
    length = math.hypot(bx - ax, by - ay)
    nx, ny = -(by - ay) / length, (bx - ax) / length
    return (ax - nx * r, ay - ny * r), (bx - nx * r, by - ny * r), (bx + nx * r, by + ny * r), (ax + nx * r, ay + ny * r)


def _num(v: float) -> str:
    text = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def sheet_svg(sheet: Sheet, s: SimpleNamespace, title: str = "", xml_declaration: bool = True) -> str:
    W, H = s.sheet_width, s.sheet_height

    def pt(p: Point) -> str:
        return f"{_num(p[0])},{_num(H - p[1])}"  # SVG has Y pointing down

    def element(shape: Shape) -> str:
        if isinstance(shape, Circle):
            return f'<circle cx="{_num(shape.center[0])}" cy="{_num(H - shape.center[1])}" r="{_num(shape.radius)}"/>'
        if isinstance(shape, Slot):
            v0, v1, v2, v3 = slot_outline(shape)
            r = _num(shape.radius)
            # Counter-clockwise on paper, which is sweep-flag 0 in SVG's Y-down coordinates.
            return f'<path d="M{pt(v0)} L{pt(v1)} A{r},{r} 0 0 0 {pt(v2)} L{pt(v3)} A{r},{r} 0 0 0 {pt(v0)} Z"/>'
        close = " Z" if shape.closed else ""
        return '<path d="' + " ".join("M" + " L".join(pt(p) for p in line) + close for line in shape.lines) + '"/>'

    out = ['<?xml version="1.0" encoding="UTF-8"?>'] if xml_declaration else []
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
               f'width="{_num(W)}mm" height="{_num(H)}mm" viewBox="0 0 {_num(W)} {_num(H)}">')
    if title:
        out.append(f"  <title>{escape(title)}</title>")
    for name, (_, colour) in LAYERS.items():
        shapes = sheet.layers.get(name)
        if not shapes:
            continue
        out.append(f'  <g id="{name}" class="layer-{name}" inkscape:groupmode="layer" inkscape:label="{name}" '
                   f'fill="none" stroke="{colour}" stroke-width="0.25" stroke-linecap="round" stroke-linejoin="round">')
        out.extend("    " + element(shape) for shape in shapes)
        out.append("  </g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


def sheet_dxf(sheet: Sheet, s: SimpleNamespace):
    import ezdxf
    from ezdxf import units

    doc = ezdxf.new(s.dxf_version, setup=False)
    r12 = s.dxf_version == "R12"
    if not r12:  # R12 has no unit header
        doc.units = units.MM
    doc.header["$MEASUREMENT"] = 1
    for name, (colour, _) in LAYERS.items():
        doc.layers.add(name, color=colour)
    msp = doc.modelspace()

    def polyline(points, closed: bool, attribs: dict, fmt: str = "xy") -> None:
        if r12:
            msp.add_polyline2d(points, format=fmt, close=closed, dxfattribs=attribs)
        else:
            msp.add_lwpolyline(points, format=fmt, close=closed, dxfattribs=attribs)

    for name in LAYERS:
        attribs = {"layer": name}
        for shape in sheet.layers.get(name, []):
            if isinstance(shape, Circle):
                msp.add_circle(shape.center, shape.radius, dxfattribs=attribs)
            elif isinstance(shape, Slot):
                v0, v1, v2, v3 = slot_outline(shape)
                polyline([(*v0, 0), (*v1, 1), (*v2, 0), (*v3, 1)], True, attribs, "xyb")  # bulge 1 = half circle
            else:
                for line in shape.lines:
                    if len(line) == 2 and not shape.closed:
                        msp.add_line(line[0], line[1], dxfattribs=attribs)
                    else:
                        polyline(line, shape.closed, attribs)
    doc.header["$EXTMIN"] = (0, 0, 0)
    doc.header["$EXTMAX"] = (s.sheet_width, s.sheet_height, 0)
    return doc


def dxf_bytes(doc) -> bytes:
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode(doc.output_encoding, errors="replace")


def dwg_available() -> bool:
    try:
        from ezdxf.addons import odafc
        return bool(odafc.is_installed())
    except Exception:
        return False


def dwg_bytes(doc) -> bytes:
    from ezdxf.addons import odafc

    if not odafc.is_installed():
        raise ValueError("DWG export needs the ODA File Converter (https://www.opendesign.com/guestfiles/oda_file_converter)")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sheet.dwg"
        odafc.export_dwg(doc, str(path), version="R2018", replace=True)
        return path.read_bytes()


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "rings"


def default_name(codes: list[str]) -> str:
    if not codes:
        return "rings"
    return safe_name(codes[0] if len(codes) == 1 else f"{codes[0]}-{codes[-1]}")


def sheet_files(job: Job, sheet: Sheet, name: str, formats: set[str]) -> dict[str, bytes]:
    stem = f"{name}_sheet{sheet.number:02d}"
    title = f"{name} - sheet {sheet.number} of {len(job.sheets)}"
    files = {}
    if "svg" in formats:
        files[f"{stem}.svg"] = sheet_svg(sheet, job.settings, title).encode("utf-8")
    if formats & {"dxf", "dwg"}:
        doc = sheet_dxf(sheet, job.settings)
        if "dxf" in formats:
            files[f"{stem}.dxf"] = dxf_bytes(doc)
        if "dwg" in formats:
            files[f"{stem}.dwg"] = dwg_bytes(doc)
    if "gcode" in formats:
        files.update(sheet_gcode(job, sheet, stem, title))
    return files


def job_files(job: Job, name: str, formats: set[str]) -> dict[str, bytes]:
    """All output files: every sheet, a plate list and the settings used."""
    files: dict[str, bytes] = {}
    for sheet in job.sheets:
        files.update(sheet_files(job, sheet, name, formats))
    table = io.StringIO()
    writer = csv.writer(table)
    writer.writerow(["sheet", "row", "column", "code"])
    for sheet in job.sheets:
        for p in sheet.plates:
            if p.code is not None:
                writer.writerow([sheet.number, p.row, p.col, p.code])
    files[f"{name}_plates.csv"] = table.getvalue().encode("utf-8")
    record = {"codes": job.codes, "settings": vars(job.settings)}
    files[f"{name}_settings.json"] = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    return files


def zip_files(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# G-code
# --------------------------------------------------------------------------

# (operation, layer, description), in the order the files are meant to be run: the staple holes first, so the
# sheet can be stapled down before the text and the grid are engraved.
GCODE_OPS = (("staples", "STAPLES", "staple holes"), ("text", "TEXT", "text"), ("grid", "GRID", "grid lines"))
STEPOVER = 0.4  # of the tool diameter, between the rings that mill out a staple hole wider than the tool
PECK_CLEARANCE = 0.2  # mm above the bottom of a hole to rapid back down to between pecks
ORIGIN_NAMES = {"bottom-left": "bottom-left corner", "top-left": "top-left corner", "center": "centre"}


def _op_settings(s: SimpleNamespace, op: str) -> SimpleNamespace:
    prefix = f"gcode_{op}_"
    return SimpleNamespace(**{k[len(prefix):]: v for k, v in vars(s).items() if k.startswith(prefix)})


def _pass_depths(depth: float, step: float) -> list[float]:
    """Equally spaced pass depths down to the full depth."""
    n = max(1, math.ceil(depth / step - EPS)) if step > 0 else 1
    return [depth * k / n for k in range(1, n + 1)]


# Comments are kept to plain ASCII, which every controller accepts.
_COMMENT_CHARS = str.maketrans({"(": "[", ")": "]", "°": " deg", "æ": "ae", "ø": "o", "å": "a", "Æ": "AE", "Ø": "O",
                                "Å": "A", "×": "x"})


def _gcode_comment(text: str) -> str:
    return "(" + text.translate(_COMMENT_CHARS).encode("ascii", "replace").decode() + ")"


class _Program:
    """The moves of one G-code file.

    Takes sheet coordinates, with Z0 at the top of the material and negative Z into it, and writes them
    relative to the chosen origin. Travel between cuts only ever happens at the safe height."""

    def __init__(self, s: SimpleNamespace):
        self.s = s
        self.origin = {"bottom-left": (0.0, 0.0), "top-left": (0.0, s.sheet_height),
                       "center": (s.sheet_width / 2, s.sheet_height / 2)}[s.gcode_origin]
        self.safe_z = s.gcode_safe_z
        self.plunge_z = min(s.gcode_plunge_z, s.gcode_safe_z)
        self.lines: list[str] = []
        self.x: float | None = None  # unknown until the first move
        self.y: float | None = None
        self.z: float | None = None
        self.feed: float | None = None
        self.rpm = 0
        self.minutes = 0.0

    @property
    def xy(self) -> Point:
        """Where the tool is, or the origin before the first move (where the machine was most likely zeroed)."""
        return self.origin if self.x is None else (self.x, self.y)

    def comment(self, text: str) -> None:
        self.lines.append(_gcode_comment(text))

    def _emit(self, code: str, words: list[str], feed: float | None) -> None:
        if feed is not None and feed != self.feed:
            words.append(f"F{round(feed)}")
            self.feed = feed
        self.lines.append(" ".join([code, *words]))

    def _go(self, code: str, x: float | None, y: float | None, z: float | None, feed: float | None) -> float:
        """A straight move, leaving out the axes that do not change. Returns its length."""
        old = (self.x, self.y, self.z)
        new = tuple(o if v is None else v for v, o in zip((x, y, z), old))
        offsets = (*self.origin, 0.0)
        words = [f"{axis}{_num(v - off)}" for axis, v, o, off in zip("XYZ", new, old, offsets)
                 if v is not None and (o is None or abs(v - o) > EPS)]
        if not words:
            return 0.0
        self._emit(code, words, feed)
        length = math.dist(old, new) if None not in old else 0.0
        self.x, self.y, self.z = new
        return length

    def rapid(self, x: float | None = None, y: float | None = None, z: float | None = None) -> None:
        self._go("G0", x, y, z, None)

    def cut(self, x: float | None = None, y: float | None = None, z: float | None = None, *, feed: float) -> None:
        self.minutes += self._go("G1", x, y, z, feed) / feed

    def arc(self, ccw: bool, x: float, y: float, centre: Point, *, feed: float, z: float | None = None) -> None:
        """G3 (counter-clockwise) or G2 to (x, y) around centre; a full circle when (x, y) is where it starts."""
        cx, cy = centre
        a0 = math.atan2(self.y - cy, self.x - cx)
        a1 = math.atan2(y - cy, x - cx)
        sweep = ((a1 - a0) if ccw else (a0 - a1)) % math.tau
        if sweep < 1e-9:
            sweep = math.tau
        dz = 0.0 if z is None else z - self.z
        self.minutes += math.hypot(math.hypot(self.x - cx, self.y - cy) * sweep, dz) / feed
        words = [f"X{_num(x - self.origin[0])}", f"Y{_num(y - self.origin[1])}"]
        if z is not None:
            words.append(f"Z{_num(z)}")
        words += [f"I{_num(cx - self.x)}", f"J{_num(cy - self.y)}"]
        self._emit("G3" if ccw else "G2", words, feed)
        self.x, self.y, self.z = x, y, (self.z if z is None else z)

    def lift(self) -> None:
        self.rapid(z=self.safe_z)

    def travel(self, x: float, y: float) -> None:
        """Lift to the safe height, move above (x, y) and drop to where plunging starts."""
        self.lift()
        self.rapid(x, y)
        self.rapid(z=self.plunge_z)

    def spindle(self, rpm: int) -> None:
        if rpm == self.rpm:
            return
        self.lift()
        if rpm:
            self.lines.append(f"M3 S{rpm}")
            if self.s.gcode_spinup > 0:
                self.lines.append(f"G4 P{_num(self.s.gcode_spinup)}")
                self.minutes += self.s.gcode_spinup / 60
        else:
            self.lines.append("M5")
        self.rpm = rpm


def _nearest_first(shapes: list[Shape], g: _Program):
    """Shapes in a short travel order: always the one whose bounding box is closest to the tool next."""
    boxes = [shape_bbox(shape) for shape in shapes]
    left = list(range(len(shapes)))
    while left:
        x, y = g.xy

        def distance(i: int) -> float:
            x0, y0, x1, y1 = boxes[i]
            dx, dy = max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1)
            return dx * dx + dy * dy

        i = min(left, key=distance)
        left.remove(i)
        yield shapes[i]


def _strokes_nearest_first(shape: Polylines, g: _Program):
    """The strokes of a shape, each started from whichever end is closer to the tool."""
    strokes = [line + line[:1] if shape.closed else list(line) for line in shape.lines]
    while strokes:
        here = g.xy
        i = min(range(len(strokes)), key=lambda i: min(math.dist(here, strokes[i][0]), math.dist(here, strokes[i][-1])))
        stroke = strokes.pop(i)
        yield stroke if math.dist(here, stroke[0]) <= math.dist(here, stroke[-1]) else stroke[::-1]


def _enter(g: _Program, path: list[Point], z: float, o: SimpleNamespace) -> None:
    """Go down to z at the start of path, ramping out along its first segment and back at the ramp angle.

    Plunges straight down through the air to the top of the material, and wherever there is no segment to ramp
    along. When the segment is too short for the ramp angle, the ramp is steeper. Ramps never go down faster
    than the plunge rate."""
    if g.z > 0:
        g.cut(z=0.0, feed=o.plunge)
    angle = g.s.gcode_ramp_angle
    if angle <= 0 or len(path) < 2 or z >= g.z or math.dist(path[0], path[1]) < EPS:
        g.cut(z=z, feed=o.plunge)
        return
    (x0, y0), (x1, y1) = path[0], path[1]
    half = (g.z - z) / 2
    run = min(half / math.tan(math.radians(min(angle, 89.0))), math.dist(path[0], path[1]))
    feed = _ramp_feed(o, math.hypot(run, half), half)
    t = run / math.dist(path[0], path[1])
    g.cut(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, g.z - half, feed=feed)
    g.cut(x0, y0, z, feed=feed)


def _ramp_feed(o: SimpleNamespace, length: float, drop: float) -> float:
    """Feed rate for a move of this length that goes down by drop, so the tool goes down at most at the plunge
    rate. Rounded down, as feed rates are written as whole numbers."""
    return min(o.feed, math.floor(o.plunge * length / drop))


def _engrave(g: _Program, points: list[Point], depths: list[float], o: SimpleNamespace) -> None:
    """Cut along a line. Open lines go back and forth, one pass per depth, without lifting in between."""
    closed = len(points) > 2 and math.dist(points[0], points[-1]) < EPS
    g.travel(*points[0])
    for k, depth in enumerate(depths):
        path = points if closed or k % 2 == 0 else points[::-1]
        _enter(g, path, -depth, o)
        for x, y in path[1:]:
            g.cut(x, y, feed=o.feed)
    g.lift()


def _drill(g: _Program, point: Point, depths: list[float], o: SimpleNamespace) -> None:
    g.travel(*point)
    for k, depth in enumerate(depths):
        if k:  # peck: lift out to clear the chips, then rapid back down to just above the bottom
            g.rapid(z=g.plunge_z)
            g.rapid(z=min(-depths[k - 1] + PECK_CLEARANCE, g.plunge_z))
        g.cut(z=-depth, feed=o.plunge)
    g.lift()


def _mill_hole(g: _Program, shape: Circle | Slot, depths: list[float], o: SimpleNamespace, ccw: bool) -> None:
    """A staple hole. When the tool is as wide as the hole it plunges (or cuts along the slot); otherwise each pass
    mills rings from the middle out, the last one finishing the edge. Round holes are entered with one turn of a
    helix per pass, slots with a ramp."""
    slot = isinstance(shape, Slot)
    a, b = (shape.a, shape.b) if slot else (shape.center, shape.center)
    tool_r = o.tool_diameter / 2
    edge = shape.radius - tool_r
    if edge < 0.01:
        if slot:
            _engrave(g, [a, b], depths, o)
        else:
            _drill(g, a, depths, o)
        return

    rings = [edge]
    while rings[-1] > tool_r:  # the middle is not cut yet
        rings.append(max(rings[-1] - STEPOVER * o.tool_diameter, 0.0))
    rings.reverse()

    def start(r: float) -> Point:
        if not slot:
            return (a[0] + r, a[1])
        return slot_outline(Slot(a, b, r))[0] if r > 0 else a

    def ring(r: float) -> None:
        if not slot:
            if r > 0:
                g.arc(ccw, *start(r), a, feed=o.feed)
        elif r <= 0:
            g.cut(*b, feed=o.feed)
            g.cut(*a, feed=o.feed)
        else:
            v0, v1, v2, v3 = slot_outline(Slot(a, b, r))  # counter-clockwise from v0
            if ccw:
                g.cut(*v1, feed=o.feed)
                g.arc(True, *v2, b, feed=o.feed)
                g.cut(*v3, feed=o.feed)
                g.arc(True, *v0, a, feed=o.feed)
            else:
                g.arc(False, *v3, a, feed=o.feed)
                g.cut(*v2, feed=o.feed)
                g.arc(False, *v1, b, feed=o.feed)
                g.cut(*v0, feed=o.feed)

    for k, depth in enumerate(depths):
        for i, r in enumerate(rings):
            if i == 0 and k == 0:
                g.travel(*start(r))
            else:  # out to the next ring, or back to the first one through what is already cut
                g.cut(*start(r), feed=o.feed)
            if i == 0:
                if slot:  # ramp along the straight side
                    _enter(g, list(slot_outline(Slot(a, b, r))[:2]) if r > 0 else [a, b], -depth, o)
                elif r <= 0:
                    g.cut(z=-depth, feed=o.plunge)
                else:
                    top = -depths[k - 1] if k else 0.0
                    g.cut(z=top, feed=o.plunge)
                    drop = top + depth
                    g.arc(ccw, *start(r), a, z=-depth, feed=_ramp_feed(o, math.hypot(math.tau * r, drop), drop))
            ring(r)
    g.lift()


def _gcode_program(job: Job, sheet: Sheet, title: str, tool: int, ops: list[tuple[str, str, str]]) -> str:
    s = job.settings
    g = _Program(s)
    names = []
    for op, layer, label in ops:
        o = _op_settings(s, op)
        names.append(o.tool_name)
        depths = _pass_depths(o.depth, o.step)
        g.comment(f"{label.capitalize()}: {_num(o.depth)} mm deep in {len(depths)} pass{'es' if len(depths) > 1 else ''}, "
                  f"feed {_num(o.feed)} mm/min, plunge {_num(o.plunge)} mm/min")
        g.spindle(o.rpm)
        if layer == "STAPLES":
            for shape in _nearest_first(sheet.layers[layer], g):
                _mill_hole(g, shape, depths, o, s.gcode_direction == "climb")
        else:
            for shape in _nearest_first(sheet.layers[layer], g):
                for stroke in _strokes_nearest_first(shape, g):
                    _engrave(g, stroke, depths, o)
    g.lift()
    g.spindle(0)
    park_x, park_y = s.gcode_park_x, s.gcode_park_y
    g.rapid(None if park_x is None else park_x + g.origin[0], None if park_y is None else park_y + g.origin[1])
    g.lines.append("M30")

    tool_names = " / ".join(n for n in dict.fromkeys(names) if n)
    head = [
        _gcode_comment(title),
        _gcode_comment(f"Tool {tool}" + (f": {tool_names}" if tool_names else "")),
        _gcode_comment(f"X0 Y0 = {ORIGIN_NAMES[s.gcode_origin]} of the sheet, Z0 = top of the material, units mm"),
        _gcode_comment(f"About {max(1, round(g.minutes))} min at the set feeds, plus rapid moves"),
        "G21 G90 G17 G94",  # millimetres, absolute coordinates, XY plane for arcs, feed in mm/min
    ]
    return "\n".join(head + g.lines) + "\n"


def sheet_gcode(job: Job, sheet: Sheet, stem: str, title: str) -> dict[str, bytes]:
    """One file per tool, in the order of GCODE_OPS; operations sharing a tool number share a file."""
    s = job.settings
    groups: dict[int, list[tuple[str, str, str]]] = {}
    for op, layer, label in GCODE_OPS:
        if getattr(s, f"gcode_{op}_enabled") and sheet.layers.get(layer):
            groups.setdefault(getattr(s, f"gcode_{op}_tool"), []).append((op, layer, label))
    files = {}
    for number, (tool, ops) in enumerate(groups.items(), 1):
        what = "+".join(op for op, _, _ in ops)
        label = " and ".join(label for _, _, label in ops)
        program = _gcode_program(job, sheet, f"{title}: {label}", tool, ops)
        files[f"{stem}_{number}_{what}_T{tool}.{s.gcode_extension}"] = program.encode("ascii")
    return files


# --------------------------------------------------------------------------
# Fonts and presets
# --------------------------------------------------------------------------

def available_fonts() -> list[str]:
    return sorted(p.name for p in FONTS_DIR.iterdir() if p.suffix.lower() in (".json", ".svg"))


_font_cache: dict[Path, tuple[float, StrokeFont]] = {}


def get_font(name: str) -> StrokeFont:
    """Load a font by file name (from fonts/) or path; reloaded when the file changes."""
    path = Path(name).expanduser()
    if not path.is_file():
        path = FONTS_DIR / name
    if not path.is_file():
        raise ValueError(f"Font not found: {name} (available: {', '.join(available_fonts())})")
    mtime = path.stat().st_mtime
    cached = _font_cache.get(path)
    if cached is None or cached[0] != mtime:
        cached = _font_cache[path] = (mtime, load_font(path))
    return cached[1]


def load_presets(path: Path = PRESETS_FILE) -> dict[str, dict[str, Any]]:
    if not path.exists():
        path = DEFAULT_PRESETS_FILE
        if not path.exists():
            return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_presets(presets: dict[str, dict[str, Any]], path: Path = PRESETS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(presets, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

EXAMPLES = """\
examples:
  %(prog)s -p "PL series 40x115 (photo)" PL37..PM02
  %(prog)s -p "PL series 40x115 (photo)" -f codes.txt -s copies=3 -s skip_cells=2:5,2:10
  %(prog)s -c output/PL37-PM02_settings.json        # repeat an earlier run
  %(prog)s --list-settings
"""


FORMATS = {"svg", "dxf", "dwg", "gcode"}


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate SVG/DXF engraving sheets for colour-ring plates.",
                                 epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="*", help="codes and ranges, e.g. PL37..PM02 AB12")
    ap.add_argument("-f", "--codes-file", help="text file with codes (one per line, or separated by commas/spaces)")
    ap.add_argument("-p", "--preset", help="preset name from presets.json")
    ap.add_argument("-c", "--config", help="JSON settings file, e.g. a *_settings.json written by an earlier run")
    ap.add_argument("-s", "--set", action="append", default=[], metavar="KEY=VALUE", help="override a setting")
    ap.add_argument("-o", "--out-dir", default="output", help="output directory (default: output)")
    ap.add_argument("-n", "--name", help="file name prefix (default: first-last code)")
    ap.add_argument("--formats", default="svg,dxf,gcode",
                    help="comma separated: svg, dxf, dwg, gcode (default: svg,dxf,gcode)")
    ap.add_argument("--list-presets", action="store_true", help="list presets and exit")
    ap.add_argument("--list-settings", action="store_true", help="list all settings with defaults and exit")
    args = ap.parse_args(argv)

    if args.list_presets:
        for name in load_presets():
            print(name)
        return 0
    if args.list_settings:
        for f in FIELDS:
            default = format_cells(f.default) if f.kind == "cells" else f.default
            print(f"{f.key:22} {str(default):18} {f.label}" + (f" - {f.help}" if f.help else ""))
        return 0

    try:
        raw: dict[str, Any] = {}
        config_codes: list[str] = []
        if args.preset:
            presets = load_presets()
            if args.preset not in presets:
                raise ValueError(f"Unknown preset '{args.preset}'. Available: {', '.join(presets) or 'none'}")
            raw.update(presets[args.preset])
        if args.config:
            data = json.loads(Path(args.config).read_text(encoding="utf-8"))
            if "settings" in data:
                config_codes = data.get("codes", [])
                data = data["settings"]
            raw.update(data)
        for item in args.set:
            key, sep, value = item.partition("=")
            if not sep:
                raise ValueError(f"--set expects KEY=VALUE, got '{item}'")
            raw[key.strip()] = _parse_value(value.strip())

        settings, warnings = normalize_settings(raw)
        text = " ".join(args.codes)
        if args.codes_file:
            text += "\n" + Path(args.codes_file).read_text(encoding="utf-8")
        codes = parse_codes(text, settings.range_skip_letters) if text.strip() else list(config_codes)
        if not codes:
            raise ValueError("No codes given (pass them as arguments or with --codes-file)")
        formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
        if formats - FORMATS:
            raise ValueError(f"Unknown format(s): {', '.join(sorted(formats - FORMATS))}")

        job = build_job(codes, settings)
        name = safe_name(args.name) if args.name else default_name(codes)
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for filename, data in job_files(job, name, formats).items():
            (out_dir / filename).write_bytes(data)
            print(out_dir / filename)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    st = job.stats
    print(f"{st['codes']} codes on {st['sheets']} sheet(s): {st['grid']['cols']} x {st['grid']['rows']} plates, "
          f"{st['skipped']} skipped, {st['plates_per_sheet']} per sheet", file=sys.stderr)
    for warning in warnings + job.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
