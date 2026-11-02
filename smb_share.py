"""Send G-code to a Windows or Samba share (SMB), e.g. the folder the CNC controller PC loads its files from.

The share settings belong to the machine, not to a preset, so they are kept in smb.json in the data folder
next to the saved presets. The password is stored there in plain text and never sent back to the browser.
"""

from __future__ import annotations

import errno
import json
import os
import socket
from contextlib import contextmanager
from typing import Any

import generate_sheets as gs

CONFIG_FILE = gs.DATA_DIR / "smb.json"
DEFAULTS: dict[str, Any] = {"server": "", "port": 445, "share": "", "folder": "", "username": "", "password": ""}
TIMEOUT = 10  # seconds to wait for the server to answer


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
    username is cleared as well (guest access)."""
    config = dict(saved)
    for key in ("server", "share", "folder", "username"):
        config[key] = str(update.get(key) or "").strip()
    config["server"] = config["server"].strip("\\/")  # pasted as \\server
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


@contextmanager
def _session(config: dict[str, Any]):
    """A connection of its own for one operation, closed afterwards, so changed settings take effect at once.
    Errors come out as ValueError with a message for the page."""
    import smbclient

    if not (config["server"] and config["share"]):
        raise ValueError("Set up the CNC share first: it needs at least a server and a share name")
    cache: dict = {}
    try:
        smbclient.register_session(config["server"], username=config["username"] or None,
                                   password=config["password"] or None, port=config["port"],
                                   connection_timeout=TIMEOUT, connection_cache=cache, auth_protocol="ntlm")
        yield {"port": config["port"], "connection_cache": cache}
    except Exception as exc:  # smbprotocol raises many kinds, ValueError among them; the page only needs why
        raise _error(config, exc) from exc
    finally:
        try:
            smbclient.reset_connection_cache(fail_on_error=False, connection_cache=cache)
        except Exception:
            pass


def _error(config: dict[str, Any], exc: Exception) -> ValueError:
    """The exception as a ValueError (Unreachable when the server did not answer) with a readable reason."""
    text = str(exc).strip() or type(exc).__name__
    known = {
        "STATUS_LOGON_FAILURE": "wrong username or password",
        "STATUS_BAD_NETWORK_NAME": "there is no share with that name on the server",
        "STATUS_ACCESS_DENIED": "access denied; check that this user may write to the share",
        "STATUS_OBJECT_PATH_NOT_FOUND": "the folder does not exist",
        "STATUS_OBJECT_NAME_NOT_FOUND": "the folder does not exist",
        "STATUS_NO_SUCH_FILE": "the folder does not exist",
        "STATUS_DISK_FULL": "the disk is full",
    }
    for status, meaning in known.items():
        if status in text:
            return ValueError(f"{unc(config)}: {meaning}")
    if (isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror, Unreachable))
            or "failed to connect" in text.lower() or "timed out" in text.lower()):
        return Unreachable(f"{unc(config)}: cannot reach the server ({text}); check that the computer is on, "
                           "the address, and that file sharing is on")
    return ValueError(f"{unc(config)}: {text}")


def reachable(config: dict[str, Any], timeout: float = 3) -> bool:
    """Whether the server accepts connections on the SMB port. Quicker than logging in, and works where
    ping (ICMP) is blocked, as it is by default on Windows."""
    try:
        with socket.create_connection((config["server"], config["port"]), timeout=timeout):
            return True
    except OSError:
        return False


def _is_dir(path: str, conn: dict) -> bool:
    import smbclient

    try:
        return smbclient.path.isdir(path, **conn)
    except OSError as exc:  # servers differ in how they say a path is missing
        if "NOT_FOUND" in str(exc) or "NO_SUCH_FILE" in str(exc):
            return False
        raise


def _make_folder(config: dict[str, Any], conn: dict) -> None:
    """Create the folder one level at a time; not every server creates a folder inside one that is missing."""
    import smbclient

    parts = config["folder"].split("\\")
    for i in range(1, len(parts) + 1):
        try:
            smbclient.mkdir(unc({**config, "folder": "\\".join(parts[:i])}), **conn)
        except OSError as exc:
            if "OBJECT_NAME_COLLISION" not in str(exc) and exc.errno != errno.EEXIST:
                raise


def test(config: dict[str, Any]) -> str:
    """Log in and look at the folder. Returns a message for the page."""
    with _session(config) as conn:
        if not _is_dir(unc({**config, "folder": ""}), conn):
            raise ValueError("STATUS_BAD_NETWORK_NAME")  # turned into a readable error by _session
        if config["folder"] and not _is_dir(unc(config), conn):
            return f"Connected. {unc(config)} does not exist yet; it is created when you first send files."
    return f"Connected to {unc(config)}."


def send(config: dict[str, Any], files: dict[str, bytes]) -> list[str]:
    """Write the files into the share folder, replacing files with the same name. Returns their paths."""
    import smbclient

    with _session(config) as conn:
        if config["folder"]:
            _make_folder(config, conn)
        for filename, data in files.items():
            with smbclient.open_file(unc(config, filename), mode="wb", **conn) as f:
                f.write(data)
    return [unc(config, filename) for filename in files]
