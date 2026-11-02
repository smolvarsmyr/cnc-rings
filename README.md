# cnc-rings

Generates CNC engraving sheets for colour rings. Each code gets its own plate, a strip that is later cut out and rolled into a ring, with the code engraved 3 or 4 times. Straight lines separate the plates. Some plates are left empty so the sheet can be stapled down, and there are more staple holes in the margin around the sheet.

The output is one SVG and one DXF file per sheet, plus ready-to-run [G-code](#g-code). The SVG and DXF have four layers, so CAM software (VCarve, FreeCAD, …) can give each one its own toolpath:

| Layer     | Contents                                                     | Typical toolpath             |
|-----------|--------------------------------------------------------------|------------------------------|
| `TEXT`    | Codes in a single-line font (open strokes, not outlines)     | Engrave on the line          |
| `GRID`    | Straight lines between the plates, including the outer frame | Engrave or V-groove on line  |
| `STAPLES` | Staple holes around the edge and in the skipped plates       | Drill (circles) / pocket     |
| `SHEET`   | Sheet outline, used only as a reference                      | none                         |

Units are mm. The origin is the bottom-left corner of the sheet, with X to the right and Y up. Rows count from the bottom and columns from the left, both starting at 1.

## Setup (Python 3.10+)

To run it from the code, see [Development](#development). To run it on a Raspberry Pi or another computer that should update by itself, see [Running it on a Raspberry Pi or any other computer](#running-it-on-a-raspberry-pi-or-any-other-computer). The short version:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
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
- Download the SVG, DXF or G-code of the sheet on screen, or a zip of all sheets. The zip also contains `*_plates.csv`, which lists the sheet, row and column of each code, and `*_settings.json`.

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

Day-to-day work happens on the `dev` branch, running the app straight from the code. Nothing reaches the machines until `dev` is merged into `main`.

### Running from the code

You need Python 3.10 or newer and git. The commands are for Linux, macOS and a Raspberry Pi. On Windows, use `.venv\Scripts\python` wherever it says `.venv/bin/python`, and `.venv\Scripts\pytest` for `.venv/bin/pytest`.

```sh
git clone https://github.com/smolvarsmyr/cnc-rings.git
cd cnc-rings
git switch dev

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt   # the app's packages plus pytest

.venv/bin/python webapp.py --debug --open
```

This opens the web interface at http://127.0.0.1:5000.

- **Restarts:** with `--debug`, the server restarts by itself when you save a Python file, and errors are shown in the browser. Changes to `web/` (HTML, CSS, JS) only need a page reload.
- **Other devices:** add `--host 0.0.0.0` to open the app from another device on the network, at `http://<this-computer>:5000`. Leave out `--debug` when you do that, because the debug page lets anyone who can reach it run code.
- **Command line:** `.venv/bin/python generate_sheets.py ...` works the same way (see [Command line](#command-line)), and writes to `output/`.
- **Requirements:** if `requirements.txt` changes after a pull, run the `pip install` line again.

**Presets while developing:**
- **Where they go:** saved presets go to `presets.json` in the repo folder, which git ignores. Until you save the first preset, the app shows `presets.default.json`.
- **Changing the defaults:** to make your presets the defaults for new installations, copy `presets.json` over `presets.default.json` and commit it.
- **Another folder:** set `CNC_RINGS_DATA=/some/folder` to keep presets somewhere else.

### Tests

```sh
.venv/bin/pytest
```

The tests generate sheets from every default preset and check the SVG, DXF and web API. They also run every G-code file through a simulator that checks the safety rules: no sideways rapid moves below the safe height, no rapid moves into uncut material, and never going down faster than the plunge rate. Run them before merging. They are also what stops a broken version from being released.

### From `dev` to the machines

1. Commit to `dev` and push it with `git push -u origin dev` (later pushes just `git push`). Nothing is built or deployed.
2. When it is ready, open a pull request from `dev` to `main` on GitHub. The tests run on the pull request.
3. Merge it. The push to `main` builds and publishes a new image, and every machine picks it up within `UPDATE_INTERVAL` (15 minutes by default).

## The photo preset

`PL series 40x115 (photo)` is a copy of the sheet in the reference photo: 600 × 600 mm sheet, 14 × 5 plates of 40 × 115 mm, 4 copies per plate, plates 2:5, 2:10, 4:5 and 4:10 skipped, and 4 pairs of staple holes on each edge. I estimated the sizes from a photo taken at an angle, so check them against a real sheet before cutting and save the corrected values with **Save preset…**.

## G-code

The app can also write G-code directly, so you don't need the CAM step. There is one file per tool and sheet, numbered in the order to run them:

```
PL37-PM02_sheet01_1_staples_T1.ngc        staple holes, 4 mm end mill
PL37-PM02_sheet01_2_text+grid_T2.ngc      codes and grid lines, 60° V-bit
```

The settings are in the folded *G-code* groups at the bottom of the form, and they are saved with presets. The defaults are for cast acrylic:

| Operation | Tool | Speed | Feed / plunge | Depth |
|---|---|---|---|---|
| Staple holes | T1, 4 mm 2-flute end mill | 16000 rpm | 1500 / 300 mm/min | 2.5 mm, 1 mm per pass |
| Text | T2, 60° V-bit, 0.7 mm tip | 18000 rpm | 1000 / 300 mm/min | 0.2 mm |
| Grid lines | T2, the same V-bit | 18000 rpm | 1000 / 300 mm/min | 0.7 mm |

The origin (top-left), the 5 mm safe height, the 22.5° ramps and the depths come from the MillMage setup the sheets were cut with before. Text and grid share tool 2, so they end up in one file. The 4 mm end mill is wider than the 3.5 mm slots in the skipped plates of the photo preset, so the app warns that those come out too big. Make the slots at least 4 mm wide, or use a smaller end mill for them.

In acrylic, melted chips are a sign that the feed is too low for the spindle speed (too little chip load). Raise the feed or lower the speed rather than the other way round.

- **G-code:** settings for the whole machine. These are the safe height for all moves between cuts, the height the tool rapids down to before it slows to the plunge rate, where X0 Y0 is (top-left, bottom-left or centre of the sheet; Z0 is always the top of the material), the ramp angle, the milling direction, the spindle spin-up wait, an optional park position, and the file extension.
- **One group per operation** (staple holes, text, grid lines) with *Export*, a tool number and description, spindle speed, feed and plunge rate, depth and depth per pass. Operations with the same tool number go into the same file. Untick *Export* to leave an operation out. A spindle speed of 0 leaves out the spindle commands, for a router you switch on by hand.

How each operation is cut:

- **Text and grid lines** are followed on the line. Each line ramps in along its first segment, and with several passes it goes back and forth without lifting.
- **Staple holes** as wide as the tool are drilled with pecks. Slots as wide as the tool are cut along their length. Wider holes are milled out from the middle to the edge, with a helix into round holes and a ramp into slots. The *Tool diameter* setting decides which method is used. A warning appears if the tool is wider than a hole.
- The tool never goes down faster than the plunge rate, also on ramps and helixes. All moves between cuts are at the safe height.

The files use only common G-code (G0/G1/G2/G3, M3/M5, G4, M30, comments in parentheses), which runs on GRBL, LinuxCNC, Mach3/4 and UCCNC. The header gives the estimated cutting time. **Check new settings in a simulator such as [NC Viewer](https://ncviewer.com) or with an air cut before cutting a sheet.**

### Sending G-code to the CNC computer

**Send to CNC** copies the G-code of the sheet on screen straight to a shared folder on the computer that runs the machine. It replaces files with the same name. Set up the share with **CNC share…** next to it:

| Setting | Example | |
|---|---|---|
| Server | `192.168.1.20` | IP address or name of the computer with the share. When the app runs in Docker, use the IP address, because Windows computer names often don't resolve inside a container. |
| Share | `gcode` | The share name, as in `\\192.168.1.20\gcode`. |
| Folder in the share | `rings` | Optional. The folder is created on the first send. |
| Username / Password | `cnc` | A Windows (or Samba) account that may write to the share. For a domain account, use `DOMAIN\user`. |
| Port | `445` | Only change it if the server uses a different port. |

**Test connection** logs in and checks the share and the folder without saving anything. Error messages say what is wrong: wrong password, no such share, server unreachable, or no write access.

**When the CNC computer is off,** the files wait in a queue:
- **Every send is queued first.** If the share answers, the files are delivered straight away. If not, they stay in the queue, which is kept in `queue/` in the data folder, so it survives a restart.
- **The queue is checked every 5 minutes** while anything is waiting, and once when the app starts. The check opens a connection to the share's port (445) rather than sending a ping, because Windows blocks ping by default and the open port is what matters.
- **Delivery is oldest first,** as soon as the share answers. Sending the same sheet again replaces its waiting copy.
- **The *n waiting* button** next to **Send to CNC** opens the queue. There you can see when it was last checked, remove a send, or press **Send now**.
- **Retries pause on other errors.** If delivery fails with a wrong password or missing write access, automatic retries stop, because Windows can lock an account after repeated failed logins. They resume when you save the share settings or press **Send now**.
- **Docker logs:** `docker compose logs app` shows a line for every send the queue delivers.

On the Windows computer, share a folder as follows:
1. Right-click the folder → *Properties* → *Sharing* → *Advanced Sharing*.
2. Tick *Share this folder*.
3. Under *Permissions*, give the user *Change* access.

It's best to use a separate local user that can only reach this share.

**Where the settings are kept:**
- **Per installation:** the share settings belong to the installation, not to a preset, so exporting presets never includes them.
- **Location:** they are saved in `smb.json` in the data folder. That's the Docker volume, or the repo folder when you run from the code, where git ignores it.
- **Password:** it is stored there **in plain text** and is never sent back to the browser. To keep it, leave the password field empty when you change other settings.
- **Who can use it:** the web interface has no login, so anyone who can open it can send files to the share or change these settings.

## DWG

DXF is the format VCarve and FreeCAD import most reliably. DWG export works only when the [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter) is installed. Once it is, a DWG button appears in the web interface and `--formats svg,dxf,gcode,dwg` works on the command line.

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

- `generate_sheets.py`: layout, SVG/DXF/G-code output, CLI. The settings list (`FIELDS`) also builds the web form.
- `stroke_font.py`: loads single-line fonts and lays out text.
- `webapp.py` and `web/`: the web interface (Flask).
- `smb_share.py`: sending G-code to a Windows or Samba share.
- `send_queue.py`: the queue that holds G-code while the share is offline, and the thread that delivers it.
- `presets.default.json`: the presets a new installation starts with. Saved presets go to `presets.json`.
- `tests/`: smoke tests that gate every release.
- `Dockerfile`, `compose.yaml`, `.github/workflows/release.yml`: building, publishing and running the image.
