# cnc-rings

Generates CNC engraving sheets for colour rings. Each code gets its own plate, a strip that is later cut out and rolled into a ring, with the code engraved 3 or 4 times. Straight lines separate the plates. Some plates are left empty so the sheet can be stapled down, and there are more staple holes in the margin around the sheet.

The output is one SVG and one DXF file per sheet. Each file has four layers, so CAM software (VCarve, FreeCAD, …) can give each one its own toolpath:

| Layer     | Contents                                                     | Typical toolpath             |
|-----------|--------------------------------------------------------------|------------------------------|
| `TEXT`    | Codes in a single-line font (open strokes, not outlines)     | Engrave on the line          |
| `GRID`    | Straight lines between the plates, including the outer frame | Engrave or V-groove on line  |
| `STAPLES` | Staple holes around the edge and in the skipped plates       | Drill (circles) / pocket     |
| `SHEET`   | Sheet outline, used only as a reference                      | none                         |

Units are mm. The origin is the bottom-left corner of the sheet, with X to the right and Y up. Rows count from the bottom and columns from the left, both starting at 1.

## Setup (Python 3.10+)

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Web interface

```sh
.venv/bin/python webapp.py --open        # http://127.0.0.1:5000
.venv/bin/python webapp.py --host 0.0.0.0  # reachable from the workshop PC
```

- Paste codes or ranges (`PL37..PM02`). The preview updates as you type.
- Pick a preset, adjust settings, and use **Save preset…** to store the result in `presets.json`, e.g. one preset per species or ring size.
- Click a plate in the preview to skip it for staples, or fill in rows × columns under *Skipped plates*.
- Scroll to zoom, drag to pan, double-click to fit.
- Download the SVG or DXF of the sheet on screen, or a zip of all sheets. The zip also contains `*_plates.csv`, which lists the sheet, row and column of each code, and `*_settings.json`.

## Command line

```sh
.venv/bin/python generate_sheets.py -p "PL series 40x115 (photo)" PL37..PM02
.venv/bin/python generate_sheets.py -p "PL series 40x115 (photo)" -f codes.txt -s copies=3 -s skip_cells=2:5,2:10
.venv/bin/python generate_sheets.py -c output/PL37-PM02_settings.json   # repeat an earlier run exactly
.venv/bin/python generate_sheets.py --list-settings                      # every setting with its default
```

Settings are applied in this order: defaults, then the preset (`-p`), then the JSON file (`-c`), then each `-s KEY=VALUE`. Files go to `output/` unless you pass `-o`.

**Codes** are separated by new lines, commas or spaces. A range counts like an odometer, so `PL98..PM01` gives PL98, PL99, PM00, PM01. `PL98-PM01` also works when both ends have the same pattern of letters and digits. The *Letters left out of ranges* setting (e.g. `IOQ`) skips letters that are easy to confuse.

When there are more codes than free plates, extra sheets are created.

## The photo preset

`PL series 40x115 (photo)` is a copy of the sheet in the reference photo: 600 × 600 mm sheet, 14 × 5 plates of 40 × 115 mm, 4 copies per plate, plates 2:5, 2:10, 4:5 and 4:10 skipped, and 4 pairs of staple holes on each edge. I estimated the sizes from a photo taken at an angle, so check them against a real sheet before cutting and save the corrected values with **Save preset…**.

## DWG

DXF is the format VCarve and FreeCAD import most reliably. DWG export works only when the [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter) is installed. Once it is, a DWG button appears in the web interface and `--formats svg,dxf,dwg` works on the command line.

## Fonts

Fonts live in `fonts/`, and every `.json` or `.svg` file there shows up in the font list. The default `hershey_sans.json` is the Hershey Simplex single-line font. *Width scale* condenses it to fit narrow plates.

To make your own font, either:

- **JSON:** each glyph is a list of polylines in font units, with Y up and the baseline at 0. `cap_height` is the height of the capital letters, and the *Text height* setting scales it.

  ```json
  {
    "name": "My ring font",
    "cap_height": 100,
    "glyphs": {
      "L": {"advance": 60, "strokes": [[[10, 100], [10, 0], [55, 0]]]}
    }
  }
  ```

- **SVG font:** this is the format of Inkscape's *Hershey Text* extension, so you can draw the glyphs in Inkscape and save them as an SVG font. Curves are flattened into short line segments.

`tools/export_hershey.py` converts other Hershey fonts to the JSON format. It needs `pip install Hershey-Fonts`.

## Files

- `generate_sheets.py`: layout, SVG/DXF output, CLI. The settings list (`FIELDS`) also builds the web form.
- `stroke_font.py`: loads single-line fonts and lays out text.
- `webapp.py` and `web/`: the web interface (Flask).
- `presets.json`: saved presets.
