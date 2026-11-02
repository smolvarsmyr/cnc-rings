"""Send G-code to a Windows or Samba share (SMB), e.g. the folder the CNC controller loads its files from.

Speaks SMB1 as well as SMB2 (pysmb), because the Samba servers built into CNC controllers often only do SMB1.

The share settings belong to the machine, not to a preset, so they are kept in smb.json in the data folder
next to the saved presets. The password is stored there in plain text and never sent back to the browser.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
from contextlib import contextmanager
from typing import Any

from smb.base import NotConnectedError, NotReadyError, OperationFailure
from smb.SMBConnection import SMBConnection

import generate_sheets as gs

CONFIG_FILE = gs.DATA_DIR / "smb.json"
DEFAULTS: dict[str, Any] = {"server": "", "port": 445, "share": "", "folder": "", "username": "", "password": ""}
TIMEOUT = 10  # seconds to wait for the server to answer
CLIENT_NAME = "cnc-rings"  # how this app introduces itself to the server

# NT status codes worth explaining (the rest are shown as they come).
STATUS = {
    0xC000006D: "wrong username or password",
    0xC0000022: "access denied; check that this user may write to the share",
    0xC00000CC: "there is no share with that name on the server",
    0xC0000034: "the folder does not exist",
    0xC000003A: "the folder does not exist",
    0xC000007F: "the disk is full",
    0xC0000043: "the file is in use on the CNC; close it in the controller and send again",
}


class Unreachable(ValueError):
    """The server did not answer: switched off, asleep, or not on the network."""


def load_config() -> dict[str, Any]:
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    """The settings as the browser may see them: without the password."""
    public = {k: v for k, v in config.items() if k != "password"}
    return {**public, "password_set": bool(config["password"]), "configured": bool(config["server"] and config["share"])}


def merge_config(update: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    """Settings from the form on top of the saved ones. An empty password keeps the saved password, unless the
    username is cleared as well (guest access).

    A whole address typed into Server, like 192.168.1.201/cncdisk/rings, \\\\192.168.1.201\\cncdisk or
    smb://192.168.1.201/cncdisk, is split into server, share and folder."""
    config = dict(saved)
    for key in ("server", "share", "folder", "username"):
        config[key] = str(update.get(key) or "").strip()
    server, *path = [p for p in re.split(r"[\\/]+", re.sub(r"^smb:", "", config["server"], flags=re.I)) if p] or [""]
    config["server"] = server
    if path:
        config["share"], config["folder"] = path[0], "\\".join(path[1:]) or config["folder"]
    config["share"] = config["share"].strip("\\/")
    config["folder"] = config["folder"].replace("/", "\\").strip("\\")
    try:
        config["port"] = int(update.get("port") or 445)
    except (TypeError, ValueError):
        raise ValueError("Port: must be a whole number") from None
    if not 1 <= config["port"] <= 65535:
        raise ValueError("Port: must be between 1 and 65535")
    if update.get("password"):
        config["password"] = str(update["password"])
    elif not config["username"]:
        config["password"] = ""
    return config


def save_config(config: dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)  # it holds the password
    tmp.replace(CONFIG_FILE)


def unc(config: dict[str, Any], filename: str = "") -> str:
    """\\\\server\\share\\folder\\filename"""
    return "\\\\" + "\\".join(p for p in (config["server"], config["share"], config["folder"], filename) if p)


def _path(config: dict[str, Any], *names: str) -> str:
    """A path inside the share, as pysmb takes it."""
    return "/" + "/".join(p for p in (*config["folder"].split("\\"), *names) if p)


@contextmanager
def _session(config: dict[str, Any]):
    """A logged-in connection for one operation, closed afterwards, so changed settings take effect at once.
    Errors come out as ValueError (Unreachable when the server does not answer) with a message for the page."""
    if not (config["server"] and config["share"]):
        raise ValueError("Set up the CNC share first: it needs at least a server and a share name")
    domain, _, user = config["username"].rpartition("\\")
    conn = SMBConnection(user, config["password"], CLIENT_NAME, config["server"], domain=domain,
                         is_direct_tcp=config["port"] != 139)  # 139 is SMB over NetBIOS
    try:
        try:
            logged_in = conn.connect(config["server"], config["port"], timeout=TIMEOUT)
        except (OSError, NotConnectedError, NotReadyError) as exc:
            raise Unreachable(f"{unc(config)}: cannot reach the server ({exc or type(exc).__name__}); check that "
                              "the computer is on, the address, and that file sharing is on") from exc
        if not logged_in:
            raise ValueError(f"{unc(config)}: {STATUS[0xC000006D]}")
        yield conn
    except OperationFailure as exc:
        status = exc.smb_messages[-1].status if exc.smb_messages else None
        raise ValueError(f"{unc(config)}: {STATUS.get(status, exc.message)}") from exc
    except (OSError, NotConnectedError) as exc:  # dropped halfway
        raise Unreachable(f"{unc(config)}: the connection was lost ({exc or type(exc).__name__})") from exc
    finally:
        conn.close()


def reachable(config: dict[str, Any], timeout: float = 3) -> bool:
    """Whether the server accepts connections on the SMB port. Quicker than logging in, and works where
    ping (ICMP) is blocked, as it is by default on Windows."""
    try:
        with socket.create_connection((config["server"], config["port"]), timeout=timeout):
            return True
    except OSError:
        return False


def _is_dir(conn: SMBConnection, config: dict[str, Any], path: str) -> bool:
    try:
        return conn.getAttributes(config["share"], path, timeout=TIMEOUT).isDirectory
    except OperationFailure:
        return False


def _check_share(conn: SMBConnection, config: dict[str, Any]) -> None:
    shares = [s.name for s in conn.listShares(timeout=TIMEOUT) if not s.isSpecial and not s.name.endswith("$")]
    if config["share"].lower() not in (s.lower() for s in shares):
        raise ValueError(f"{unc(config)}: there is no share called {config['share']}; the server has: "
                         + (", ".join(shares) or "no shares you can see"))


def test(config: dict[str, Any]) -> str:
    """Log in and look at the share and the folder. Returns a message for the page."""
    with _session(config) as conn:
        _check_share(conn, config)
        version = "SMB2" if conn.isUsingSMB2 else "SMB1"
        if config["folder"] and not _is_dir(conn, config, _path(config)):
            return f"Connected ({version}). {unc(config)} does not exist yet; it is created when you first send files."
        return f"Connected to {unc(config)} ({version})."


def send(config: dict[str, Any], files: dict[str, bytes]) -> list[str]:
    """Write the files into the share folder, replacing files with the same name. Returns their paths."""
    with _session(config) as conn:
        parts = [p for p in config["folder"].split("\\") if p]
        for i in range(1, len(parts) + 1):  # create the folder one level at a time
            path = "/" + "/".join(parts[:i])
            if not _is_dir(conn, config, path):
                conn.createDirectory(config["share"], path, timeout=TIMEOUT)
        for filename, data in files.items():
            conn.storeFile(config["share"], _path(config, filename), io.BytesIO(data), timeout=TIMEOUT)
    return [unc(config, filename) for filename in files]
