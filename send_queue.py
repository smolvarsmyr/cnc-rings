"""Queue for G-code on its way to the CNC share, which is often switched off.

Everything sent goes into the queue first, one folder per send in queue/ in the data folder, so it survives
a restart. It is delivered at once when the share answers. Otherwise a background thread checks every
CHECK_INTERVAL seconds whether the share accepts connections, and delivers the queue, oldest first, when it does.

When delivery fails for any other reason than the share being offline (wrong password, no write access, ...),
automatic retries pause until the share settings are saved or someone presses Send now: retrying a wrong
password every few minutes can lock the Windows account.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from typing import Any

import generate_sheets as gs
import smb_share

QUEUE_DIR = gs.DATA_DIR / "queue"
CHECK_INTERVAL = 300  # seconds

log = logging.getLogger(__name__)
_lock = threading.Lock()  # one delivery at a time, from requests and the background thread
_wake = threading.Event()
_state: dict[str, Any] = {"paused": "", "last_check": None, "next_check": None, "online": None}
_started = False


def jobs() -> list[dict[str, Any]]:
    """Queued sends, oldest first."""
    if not QUEUE_DIR.is_dir():
        return []
    out = []
    for folder in sorted(QUEUE_DIR.iterdir()):
        if folder.name.startswith("."):  # still being written
            continue
        try:
            out.append({"id": folder.name, **json.loads((folder / "job.json").read_text(encoding="utf-8"))})
        except (OSError, ValueError):
            continue  # half written, or removed meanwhile
    return out


def _write_job(job_id: str, job: dict[str, Any]) -> None:
    data = {k: v for k, v in job.items() if k != "id"}
    (QUEUE_DIR / job_id / "job.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def enqueue(files: dict[str, bytes], label: str) -> str:
    """Add a send to the queue. It replaces queued sends of the same files, which it is a newer version of."""
    job_id = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"  # sorts oldest first
    tmp = QUEUE_DIR / f".{job_id}"
    tmp.mkdir(parents=True)
    for name, data in files.items():
        (tmp / name).write_bytes(data)
    (tmp / "job.json").write_text(json.dumps({"label": label, "created": time.time(), "files": list(files),
                                              "error": ""}, indent=2) + "\n", encoding="utf-8")
    for job in jobs():
        if set(job["files"]) <= set(files):
            remove(job["id"])
    tmp.rename(QUEUE_DIR / job_id)  # appears in the queue only when complete
    return job_id


def remove(job_id: str) -> None:
    if job_id in {job["id"] for job in jobs()}:  # only ever a folder of the queue
        shutil.rmtree(QUEUE_DIR / job_id, ignore_errors=True)


def deliver(manual: bool = False) -> None:
    """Send the queue to the share, oldest first, if the share answers. A manual delivery also ends a pause."""
    with _lock:
        if manual:
            _state["paused"] = ""
        pending = jobs()
        if not pending or _state["paused"]:
            return
        config = smb_share.load_config()
        if not (config["server"] and config["share"]):
            return
        _state["last_check"] = time.time()
        _state["online"] = smb_share.reachable(config)
        if not _state["online"]:
            return
        for job in pending:
            try:
                files = {name: (QUEUE_DIR / job["id"] / name).read_bytes() for name in job["files"]}
                smb_share.send(config, files)
            except smb_share.Unreachable as exc:  # went away halfway: try again at the next check
                _state["online"] = False
                _write_job(job["id"], {**job, "error": str(exc)})
                return
            except (ValueError, OSError) as exc:
                _state["paused"] = str(exc)
                _write_job(job["id"], {**job, "error": str(exc)})
                return
            remove(job["id"])
            log.info("Sent %s to %s", job["label"], smb_share.unc(config))


def status() -> dict[str, Any]:
    return {"jobs": jobs(), "interval": CHECK_INTERVAL, **_state}


def settings_changed() -> None:
    """New share settings may fix what paused the queue, so try again soon."""
    _state["paused"] = ""
    _wake.set()


def _run() -> None:
    while True:
        _state["next_check"] = time.time() + CHECK_INTERVAL
        _wake.wait(CHECK_INTERVAL)
        _wake.clear()
        try:
            deliver()
        except Exception:
            log.exception("Delivering the CNC queue failed")


def start() -> None:
    """Start the background thread that delivers the queue (once per process)."""
    global _started
    if not _started:
        _started = True
        _wake.set()  # first check straight away: sends may be waiting from before a restart
        threading.Thread(target=_run, name="cnc-queue", daemon=True).start()
