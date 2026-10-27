#!/usr/bin/env python3
"""Web interface for generate_sheets.py.

    python webapp.py                  # then open http://127.0.0.1:5000
    python webapp.py --host 0.0.0.0   # reachable from other machines on the network
"""

from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

import generate_sheets as gs

WEB_DIR = Path(__file__).resolve().parent / "web"
MIME = {"svg": "image/svg+xml", "dxf": "application/dxf", "dwg": "application/acad", "zip": "application/zip"}

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")


@app.errorhandler(ValueError)
def bad_request(exc: ValueError):
    return jsonify(error=str(exc)), 400


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/api/schema")
def schema():
    fields = [{"key": f.key, "default": f.default, "kind": f.kind, "group": f.group, "label": f.label,
               "help": f.help, "choices": list(f.choices), "minimum": f.minimum} for f in gs.FIELDS]
    return jsonify(fields=fields, fonts=gs.available_fonts(), dwg=gs.dwg_available(),
                   layers={name: colour for name, (_, colour) in gs.LAYERS.items()})


@app.get("/api/presets")
def presets():
    return jsonify(gs.load_presets())


@app.put("/api/presets/<path:name>")
def save_preset(name: str):
    name = name.strip()
    if not name:
        raise ValueError("A preset needs a name")
    settings, _ = gs.normalize_settings(request.get_json(force=True).get("settings"))
    presets = gs.load_presets()
    presets[name] = vars(settings)
    gs.save_presets(presets)
    return jsonify(presets)


@app.delete("/api/presets/<path:name>")
def delete_preset(name: str):
    presets = gs.load_presets()
    presets.pop(name, None)
    gs.save_presets(presets)
    return jsonify(presets)


def job_from_request() -> tuple[gs.Job, dict]:
    data = request.get_json(force=True)
    settings, warnings = gs.normalize_settings(data.get("settings"))
    if settings.font not in gs.available_fonts():
        raise ValueError(f"Unknown font '{settings.font}'")
    codes = gs.parse_codes(data.get("codes", ""), settings.range_skip_letters)
    job = gs.build_job(codes, settings)
    job.warnings[:0] = warnings
    return job, data


@app.post("/api/generate")
def generate():
    job, _ = job_from_request()
    height = job.settings.sheet_height
    sheets = []
    for sheet in job.sheets:
        plates = [{"row": p.row, "col": p.col, "code": p.code, "skipped": p.skipped,
                   "x": p.rect[0], "y": height - p.rect[1] - p.rect[3], "w": p.rect[2], "h": p.rect[3]}
                  for p in sheet.plates]
        sheets.append({"number": sheet.number, "svg": gs.sheet_svg(sheet, job.settings, xml_declaration=False),
                       "plates": plates, "codes": sum(p.code is not None for p in sheet.plates)})
    return jsonify(sheets=sheets, warnings=job.warnings, stats=job.stats,
                   sheet={"width": job.settings.sheet_width, "height": height},
                   name=gs.default_name(job.codes))


@app.post("/api/download")
def download():
    job, data = job_from_request()
    raw_name = str(data.get("name") or "").strip()
    name = gs.safe_name(raw_name) if raw_name else gs.default_name(job.codes)
    fmt = data.get("format", "zip")
    if fmt == "zip":
        formats = {"svg", "dxf"} | ({"dwg"} if data.get("dwg") and gs.dwg_available() else set())
        payload, filename = gs.zip_files(gs.job_files(job, name, formats)), f"{name}.zip"
    elif fmt in ("svg", "dxf", "dwg"):
        number = int(data.get("sheet") or 1)
        if not 1 <= number <= len(job.sheets):
            raise ValueError(f"There is no sheet {number}")
        filename, payload = next(iter(gs.sheet_files(job, job.sheets[number - 1], name, {fmt}).items()))
    else:
        raise ValueError(f"Unknown format '{fmt}'")
    return Response(payload, mimetype=MIME[fmt], headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def main() -> None:
    ap = argparse.ArgumentParser(description="Web interface for the ring sheet generator.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--open", action="store_true", help="open the page in a browser")
    args = ap.parse_args()
    if args.open:
        webbrowser.open(f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
