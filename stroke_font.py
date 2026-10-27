"""Single-line (stroke) fonts for engraving.

A stroke font describes every character as a set of open polylines instead of
filled outlines, so a CNC engraves each letter with a single pass of the tool.

Two file formats are supported:

* JSON (``.json``) - the simple native format, easy to write by hand or from
  a script::

      {
        "name": "My ring font",
        "cap_height": 100,
        "glyphs": {
          "L": {"advance": 60, "strokes": [[[10, 100], [10, 0], [55, 0]]]},
          ...
        }
      }

  Coordinates are in font units, Y up, baseline at y = 0, the glyph starting at
  x = 0. ``advance`` is the distance to the start of the next character and
  ``cap_height`` is the height of capital letters (it is scaled to the text
  height chosen in the settings).

* SVG fonts (``.svg``) - the format used by Inkscape's "Hershey Text"
  extension. A custom font can be drawn in Inkscape and saved as an SVG font
  (Extensions > Typography). Path commands M, L, H, V, C, S, Q, T and Z are
  supported; curves are flattened into short line segments.
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

Point = tuple[float, float]
Polyline = list[Point]


@dataclass
class Glyph:
    advance: float
    strokes: list[Polyline]


class StrokeFont:
    def __init__(self, name: str, cap_height: float, glyphs: dict[str, Glyph], fallback: str = "?"):
        if cap_height <= 0:
            raise ValueError(f"Font '{name}': cap_height must be positive")
        self.name = name
        self.cap_height = cap_height
        self.glyphs = glyphs
        self.fallback = fallback

    def render(self, text: str, cap_height: float, width_scale: float = 1.0,
               letter_spacing: float = 0.0) -> tuple[list[Polyline], set[str]]:
        """Lay out ``text`` on a baseline at y = 0, starting at x = 0.

        Sizes are in output units (mm). Returns the strokes and the set of
        characters the font has no glyph for (drawn as the fallback glyph).
        """
        scale = cap_height / self.cap_height
        sx = scale * width_scale
        strokes: list[Polyline] = []
        missing: set[str] = set()
        x = 0.0
        for ch in text:
            glyph = self.glyphs.get(ch)
            if glyph is None:
                missing.add(ch)
                glyph = self.glyphs.get(self.fallback) or Glyph(self.cap_height * 0.6, [])
            for stroke in glyph.strokes:
                strokes.append([(x + px * sx, py * scale) for px, py in stroke])
            x += glyph.advance * sx + letter_spacing
        return strokes, missing


def load_font(path: str | Path) -> StrokeFont:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        return load_json_font(path)
    if suffix == ".svg":
        return load_svg_font(path)
    raise ValueError(f"Unsupported font format '{suffix}' (use .json or .svg): {path}")


def load_json_font(path: Path) -> StrokeFont:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    glyphs = {
        ch: Glyph(float(g["advance"]), [[(float(x), float(y)) for x, y in stroke] for stroke in g.get("strokes", [])])
        for ch, g in data["glyphs"].items()
    }
    return StrokeFont(data.get("name", Path(path).stem), float(data["cap_height"]), glyphs)


def load_svg_font(path: Path) -> StrokeFont:
    root = ET.parse(path).getroot()
    font = next((el for el in root.iter() if _local(el.tag) == "font"), None)
    if font is None:
        raise ValueError(f"No <font> element found in {path}")
    face = next((el for el in font if _local(el.tag) == "font-face"), None)
    face_attrs = face.attrib if face is not None else {}
    default_adv = float(font.get("horiz-adv-x", face_attrs.get("units-per-em", 1000)))

    glyphs: dict[str, Glyph] = {}
    for el in font:
        tag = _local(el.tag)
        if tag == "missing-glyph":
            glyphs.setdefault("�", Glyph(float(el.get("horiz-adv-x", default_adv)), parse_path(el.get("d", ""))))
        elif tag == "glyph" and len(el.get("unicode", "")) == 1:
            glyphs[el.get("unicode")] = Glyph(float(el.get("horiz-adv-x", default_adv)), parse_path(el.get("d", "")))

    if "cap-height" in face_attrs:
        cap_height = float(face_attrs["cap-height"])
    elif glyphs.get("H") and glyphs["H"].strokes:
        cap_height = max(y for stroke in glyphs["H"].strokes for _, y in stroke)
    else:
        cap_height = float(face_attrs.get("ascent", 0.7 * float(face_attrs.get("units-per-em", 1000))))
    name = face_attrs.get("font-family") or font.get("id") or Path(path).stem
    return StrokeFont(name, cap_height, glyphs, fallback="?" if "?" in glyphs else "�")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


_PATH_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_PARAM_COUNT = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2}


def parse_path(d: str, curve_steps: int = 8) -> list[Polyline]:
    """Convert SVG path data into polylines, flattening curves."""
    tokens = _PATH_TOKEN.findall(d)
    lines: list[Polyline] = []
    current: Polyline | None = None
    x = y = 0.0
    start = (0.0, 0.0)
    ctrl: Point | None = None  # last control point, for the reflections in S and T
    prev: str | None = None
    cmd: str | None = None
    i = 0

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
        elif cmd is None:
            raise ValueError(f"Path data must start with a command: {d[:40]!r}")
        upper = cmd.upper()
        rel = cmd != upper

        if upper == "Z":
            if current and current[-1] != start:
                current.append(start)
            x, y = start
            current, cmd, prev = None, None, "Z"
            continue
        if upper not in _PARAM_COUNT:
            raise ValueError(f"Unsupported path command '{cmd}' (arcs are not supported in font glyphs)")

        n = _PARAM_COUNT[upper]
        if i + n > len(tokens) or any(t.isalpha() for t in tokens[i:i + n]):
            raise ValueError(f"Not enough parameters for path command '{cmd}'")
        v = [float(t) for t in tokens[i:i + n]]
        i += n
        if rel:
            if upper == "H":
                v[0] += x
            elif upper == "V":
                v[0] += y
            else:
                v = [val + (y if k % 2 else x) for k, val in enumerate(v)]

        if upper == "M":
            x, y = v
            start = (x, y)
            current = [start]
            lines.append(current)
            cmd = "l" if rel else "L"  # further coordinate pairs are line-tos
            prev = "M"
            continue
        if current is None:
            current = [(x, y)]
            lines.append(current)

        p0 = (x, y)
        if upper == "L":
            pts = [(v[0], v[1])]
        elif upper == "H":
            pts = [(v[0], y)]
        elif upper == "V":
            pts = [(x, v[0])]
        elif upper in "CS":
            if upper == "C":
                c1, c2, end = (v[0], v[1]), (v[2], v[3]), (v[4], v[5])
            else:
                c1 = _reflect(ctrl, p0) if prev in ("C", "S") and ctrl else p0
                c2, end = (v[0], v[1]), (v[2], v[3])
            pts = [_cubic(p0, c1, c2, end, k / curve_steps) for k in range(1, curve_steps + 1)]
            ctrl = c2
        else:  # Q, T
            if upper == "Q":
                c1, end = (v[0], v[1]), (v[2], v[3])
            else:
                c1 = _reflect(ctrl, p0) if prev in ("Q", "T") and ctrl else p0
                end = (v[0], v[1])
            pts = [_quad(p0, c1, end, k / curve_steps) for k in range(1, curve_steps + 1)]
            ctrl = c1
        current.extend(pts)
        x, y = pts[-1]
        prev = upper

    return [line for line in lines if len(line) >= 2]


def _reflect(p: Point, about: Point) -> Point:
    return (2 * about[0] - p[0], 2 * about[1] - p[1])


def _cubic(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    u = 1 - t
    a, b, c, d = u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def _quad(p0: Point, p1: Point, p2: Point, t: float) -> Point:
    u = 1 - t
    a, b, c = u * u, 2 * u * t, t * t
    return (a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1])


def rotate(points: Polyline, degrees: float) -> Polyline:
    if not degrees:
        return list(points)
    a = math.radians(degrees)
    ca, sa = math.cos(a), math.sin(a)
    return [(px * ca - py * sa, px * sa + py * ca) for px, py in points]
