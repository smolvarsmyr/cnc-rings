#!/usr/bin/env python3
"""Web interface for generate_sheets.py.

    python webapp.py                  # then open http://127.0.0.1:5000
    python webapp.py --host 0.0.0.0   # reachable from other machines on the network
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

import generate_sheets as gs
import send_queue
import smb_share

WEB_DIR = Path(__file__).resolve().parent / "web"
# Set by the Docker build to the git commit; shown in the page header.
VERSION = os.environ.get("CNC_RINGS_VERSION") or "dev"
MIME = {"svg": "image/svg+xml", "dxf": "application/dxf", "dwg": "application/acad", "zip": "application/zip",
        "gcode": "text/plain"}

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
                   layers={name: colour for name, (_, colour) in gs.LAYERS.items()}, version=VERSION[:7])


@app.get("/api/presets")
def presets():
    return jsonify(gs.load_presets())


@app.get("/api/presets/export")
def export_presets():
    payload = json.dumps(gs.load_presets(), indent=2, ensure_ascii=False) + "\n"
    return Response(payload, mimetype="application/json",
                    headers={"Content-Disposition": 'attachment; filename="cnc-rings-presets.json"'})


@app.post("/api/presets/import")
def import_presets():
    """Merge presets from an exported file; presets with the same name are replaced."""
    data = request.get_json(force=True)
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise ValueError("Not a presets file: expected an object of preset name -> settings")
    presets = gs.load_presets()
    for name, raw in data.items():
        if not str(name).strip():
            raise ValueError("A preset needs a name")
        settings, _ = gs.normalize_settings(raw)
        presets[str(name).strip()] = vars(settings)
    gs.save_presets(presets)
    return jsonify(presets)


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


def name_from_request(job: gs.Job, data: dict) -> str:
    raw_name = str(data.get("name") or "").strip()
    return gs.safe_name(raw_name) if raw_name else gs.default_name(job.codes)


def sheet_files_from_request(job: gs.Job, data: dict, name: str, fmt: str) -> tuple[int, dict[str, bytes]]:
    number = int(data.get("sheet") or 1)
    if not 1 <= number <= len(job.sheets):
        raise ValueError(f"There is no sheet {number}")
    files = gs.sheet_files(job, job.sheets[number - 1], name, {fmt})
    if not files and fmt == "gcode":
        raise ValueError("There is no G-code to export: tick Export for at least one of the G-code operations")
    return number, files


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
    name = name_from_request(job, data)
    fmt = data.get("format", "zip")
    if fmt == "zip":
        formats = {"svg", "dxf", "gcode"} | ({"dwg"} if data.get("dwg") and gs.dwg_available() else set())
        payload, filename = gs.zip_files(gs.job_files(job, name, formats)), f"{name}.zip"
    elif fmt in ("svg", "dxf", "dwg", "gcode"):
        number, files = sheet_files_from_request(job, data, name, fmt)
        if len(files) == 1:
            filename, payload = next(iter(files.items()))
        else:  # one G-code file per tool
            payload, filename, fmt = gs.zip_files(files), f"{name}_sheet{number:02d}_gcode.zip", "zip"
    else:
        raise ValueError(f"Unknown format '{fmt}'")
    return Response(payload, mimetype=MIME[fmt], headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/smb")
def smb_settings():
    return jsonify(smb_share.public_config(smb_share.load_config()))


@app.put("/api/smb")
def save_smb_settings():
    config = smb_share.merge_config(request.get_json(force=True), smb_share.load_config())
    smb_share.save_config(config)
    send_queue.settings_changed()
    return jsonify(smb_share.public_config(config))


@app.post("/api/smb/test")
def test_smb():
    """Try the settings in the form (not yet saved) with the saved password unless a new one is typed."""
    config = smb_share.merge_config(request.get_json(force=True), smb_share.load_config())
    return jsonify(message=smb_share.test(config))


@app.post("/api/smb/send")
def send_smb():
    """Send the G-code of the sheet on screen to the CNC share, through the queue: when the share is offline
    the files wait there until it answers."""
    config = smb_share.load_config()
    if not smb_share.public_config(config)["configured"]:
        raise ValueError("Set up the CNC share first: it needs at least a server and a share name")
    job, data = job_from_request()
    name = name_from_request(job, data)
    number, files = sheet_files_from_request(job, data, name, "gcode")
    job_id = send_queue.enqueue(files, f"{name} sheet {number}")
    send_queue.deliver(manual=True)
    waiting = next((j for j in send_queue.jobs() if j["id"] == job_id), None)
    if waiting is None:
        return jsonify(sent=[smb_share.unc(config, filename) for filename in files])
    reason = waiting["error"] or f"{config['server']} does not answer on port {config['port']}"
    return jsonify(queued=list(files), reason=reason, queue=send_queue.status())


@app.get("/api/smb/queue")
def smb_queue():
    return jsonify(send_queue.status())


@app.post("/api/smb/queue/send")
def send_smb_queue():
    send_queue.deliver(manual=True)
    return jsonify(send_queue.status())


@app.delete("/api/smb/queue/<job_id>")
def remove_from_smb_queue(job_id: str):
    send_queue.remove(job_id)
    return jsonify(send_queue.status())


def main() -> None:
    """Serve with waitress, or with Flask's development server for --debug. Either way the thread that
    delivers the CNC queue runs alongside."""
    ap = argparse.ArgumentParser(description="Web interface for the ring sheet generator.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--open", action="store_true", help="open the page in a browser")
    ap.add_argument("--debug", action="store_true", help="restart when the code changes, show errors in the browser")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("smbprotocol").setLevel(logging.WARNING)  # logs every SMB message otherwise
    restarted = os.environ.get("WERKZEUG_RUN_MAIN")  # set in the process --debug restarts on code changes
    if args.open and not restarted:
        webbrowser.open(f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}")
    if not args.debug:
        import waitress

        send_queue.start()
        waitress.serve(app, host=args.host, port=args.port)
    else:
        if restarted:  # the process that serves, not the one watching the files
            send_queue.start()
        app.run(host=args.host, port=args.port, debug=True)


if __name__ == "__main__":
    main()
