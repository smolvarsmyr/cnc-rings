#!/usr/bin/env python3
"""Export a Hershey font to the JSON stroke-font format used by generate_sheets.py.

Needs the Hershey-Fonts package (pip install Hershey-Fonts), only for this
one-off conversion:

    python tools/export_hershey.py futural fonts/hershey_sans.json "Hershey Sans"
"""

import json
import sys

from HersheyFonts import HersheyFonts

NOTICE = ("Hershey fonts, originally created by Dr. A. V. Hershey while working at the U.S. "
          "National Bureau of Standards. Converted from the Hershey-Fonts Python package (MIT).")


def main(font_name: str, out_path: str, display_name: str) -> None:
    hf = HersheyFonts()
    hf.load_default_font(font_name)
    glyphs = {}
    cap_height = None
    for code in range(32, 127):
        ch = chr(code)
        glyph = next(iter(hf.glyphs_for_text(ch)), None)
        if glyph is None:
            continue
        # Hershey: Y down, baseline at base_line, x centred on 0.
        # JSON format: Y up, baseline at 0, glyph starting at x = 0.
        left, base = glyph.left_offset, glyph.base_line
        cap_height = base - glyph.cap_line
        strokes = [[[x - left, base - y] for x, y in stroke] for stroke in glyph.strokes]
        glyphs[ch] = {"advance": glyph.char_width, "strokes": strokes}

    rows = ",\n".join(f"    {json.dumps(ch)}: {json.dumps(g, separators=(',', ':'))}" for ch, g in glyphs.items())
    header = json.dumps({"name": display_name, "source": f"Hershey '{font_name}'", "license": NOTICE,
                         "cap_height": cap_height}, indent=2)[:-2]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f'{header},\n  "glyphs": {{\n{rows}\n  }}\n}}\n')


if __name__ == "__main__":
    main(*sys.argv[1:4])
