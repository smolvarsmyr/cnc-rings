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
PRESETS_FILE = HERE / "presets.json"
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
    files = {}
    if "svg" in formats:
        title = f"{name} - sheet {sheet.number} of {len(job.sheets)}"
        files[f"{stem}.svg"] = sheet_svg(sheet, job.settings, title).encode("utf-8")
    if formats & {"dxf", "dwg"}:
        doc = sheet_dxf(sheet, job.settings)
        if "dxf" in formats:
            files[f"{stem}.dxf"] = dxf_bytes(doc)
        if "dwg" in formats:
            files[f"{stem}.dwg"] = dwg_bytes(doc)
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
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_presets(presets: dict[str, dict[str, Any]], path: Path = PRESETS_FILE) -> None:
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
    ap.add_argument("--formats", default="svg,dxf", help="comma separated: svg, dxf, dwg (default: svg,dxf)")
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
        if formats - {"svg", "dxf", "dwg"}:
            raise ValueError(f"Unknown format(s): {', '.join(sorted(formats - {'svg', 'dxf', 'dwg'}))}")

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
