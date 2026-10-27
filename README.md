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
- Pick a preset, adjust settings, and use **Save preset…** to store the result in `presets.json`, e.g. one preset per species or ring size. **Export** downloads all presets as a file, and **Import…** adds the presets from such a file, for example to copy them to another machine.
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

## Running it on a Raspberry Pi or any other computer

Each push to `main` runs the tests in GitHub Actions. If they pass, the workflow builds a Docker image for amd64 (ordinary PCs), arm64 (Pi 3/4/5 and Apple Silicon on a 64-bit OS) and arm/v7 (Pi 2/3/4 on a 32-bit OS), and publishes it as `ghcr.io/smolvarsmyr/cnc-rings`. Every machine running `compose.yaml` checks for a new image every 15 minutes and switches to it by itself. The Pi 1 and Pi Zero (first model) are not supported.

**Setting up a machine:**

1. Install Docker.
   - Raspberry Pi / Linux: `curl -fsSL https://get.docker.com | sh`, then `sudo usermod -aG docker $USER` and log in again.
   - Windows / macOS: install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and turn on *Start Docker Desktop when you sign in*.
2. Make a folder, put [`compose.yaml`](compose.yaml) in it, and run `docker compose up -d` in that folder.
3. Open `http://<machine-name-or-ip>/`. The version (git commit) is shown next to the title.

The app and the updater start again whenever Docker starts, so after a reboot everything comes back by itself.

**Settings:** copy [`.env.example`](.env.example) to `.env` next to `compose.yaml` to change the port (`PORT`, default 80), how often the updater checks for a new version (`UPDATE_INTERVAL`), or to pin a version (`TAG=sha-<commit>`). Then run `docker compose up -d` again.

**Presets** are kept in a Docker volume, outside the image, so updates never touch them. To move them to another machine, use **Export** and **Import…** in the web interface.

**Useful commands** (run them in the folder with `compose.yaml`):

```sh
docker compose logs -f app       # what the app is doing
docker compose logs -f updater   # when it last updated
docker compose pull && docker compose up -d   # update now instead of waiting
docker compose down              # stop (presets are kept)
```

**Rolling back:** every build is also tagged with its commit, e.g. `sha-dc74554`. To run an older build, set `TAG=sha-dc74554` in `.env` and run `docker compose up -d`. To follow `main` again, remove the line.

## Development

Work in the venv as described above. Saved presets go to `presets.json`, which git ignores. `presets.default.json` is used until you save the first preset, so to ship your presets as the defaults for new machines, copy `presets.json` over `presets.default.json`. Set `CNC_RINGS_DATA=/some/folder` to keep presets somewhere else.

Before pushing, run the tests:

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Pull requests only run the tests. Pushes to `main` are released. If the tests fail, nothing is published and the machines keep running the previous version.

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
- `presets.default.json`: the presets a new installation starts with. Saved presets go to `presets.json`.
- `tests/`: smoke tests that gate every release.
- `Dockerfile`, `compose.yaml`, `.github/workflows/release.yml`: building, publishing and running the image.
