"""CNC share settings and sending, against a fake smbclient that keeps the files in memory."""

import errno
import shutil
import stat
import sys
import types

import pytest

import generate_sheets as gs
import send_queue
import smb_share
import webapp

FORM = {"server": "\\\\cnc-pc", "share": "gcode", "folder": "rings/2026", "username": "cnc", "password": "s3cret",
        "port": ""}


class FakeShare:
    """Just enough of smbclient: one server with one share, and a password to check."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs = {"\\\\cnc-pc\\gcode"}
        self.logins = []
        self.online = True

    def module(self):
        share = self
        mod = types.ModuleType("smbclient")

        def register_session(server, username=None, password=None, **kwargs):
            share.logins.append((server, username, password, kwargs["port"]))
            if password != "s3cret":
                raise ValueError("STATUS_LOGON_FAILURE")

        def mkdir(path, **kwargs):
            if path in share.dirs:
                raise OSError(errno.EEXIST, "STATUS_OBJECT_NAME_COLLISION")
            share.dirs.add(path)

        class Writer:
            def __init__(self, path):
                self.path = path

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def write(self, data):
                assert self.path.rsplit("\\", 1)[0] in share.dirs, "folder missing"
                share.files[self.path] = data

        mod.register_session = register_session
        mod.reset_connection_cache = lambda **kwargs: None
        mod.mkdir = mkdir
        mod.open_file = lambda path, mode, **kwargs: Writer(path)
        mod.path = types.SimpleNamespace(isdir=lambda path, **kwargs: path in share.dirs)
        return mod


@pytest.fixture
def share(monkeypatch):
    fake = FakeShare()
    monkeypatch.setitem(sys.modules, "smbclient", fake.module())
    monkeypatch.setattr(smb_share, "reachable", lambda config, timeout=3: fake.online)
    for path in (smb_share.CONFIG_FILE, send_queue.QUEUE_DIR):
        shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
    send_queue._state["paused"] = ""
    yield fake
    smb_share.CONFIG_FILE.unlink(missing_ok=True)
    shutil.rmtree(send_queue.QUEUE_DIR, ignore_errors=True)


@pytest.fixture
def client():
    return webapp.app.test_client()


def test_settings_are_saved_without_showing_the_password(share, client):
    assert client.get("/api/smb").get_json()["configured"] is False
    saved = client.put("/api/smb", json=FORM).get_json()
    assert saved == {"server": "cnc-pc", "share": "gcode", "folder": "rings\\2026", "username": "cnc", "port": 445,
                     "password_set": True, "configured": True}
    assert "s3cret" not in str(client.get("/api/smb").get_json())
    assert stat.S_IMODE(smb_share.CONFIG_FILE.stat().st_mode) == 0o600
    # An empty password keeps the saved one; clearing the username clears it.
    client.put("/api/smb", json={**FORM, "password": ""})
    assert smb_share.load_config()["password"] == "s3cret"
    client.put("/api/smb", json={**FORM, "password": "", "username": ""})
    assert smb_share.load_config()["password"] == ""


def test_bad_settings(share, client):
    assert client.put("/api/smb", json={**FORM, "port": "x"}).status_code == 400
    assert client.put("/api/smb", json={**FORM, "port": 70000}).status_code == 400
    error = client.post("/api/smb/send", json={"codes": "PL37"}).get_json()["error"]
    assert "Set up the CNC share first" in error


def test_connection_test(share, client):
    assert "does not exist yet" in client.post("/api/smb/test", json=FORM).get_json()["message"]
    assert "wrong username or password" in client.post("/api/smb/test", json={**FORM, "password": "x"}).get_json()["error"]
    assert "no share" in client.post("/api/smb/test", json={**FORM, "share": "nope"}).get_json()["error"]
    share.dirs |= {"\\\\cnc-pc\\gcode\\rings", "\\\\cnc-pc\\gcode\\rings\\2026"}
    assert client.post("/api/smb/test", json=FORM).get_json() == {"message": "Connected to \\\\cnc-pc\\gcode\\rings\\2026."}


SETTINGS = gs.load_presets(gs.DEFAULT_PRESETS_FILE)["PL series 40x115 (photo)"]
SEND = {"settings": SETTINGS, "codes": "PL37..PM02", "sheet": 1}
FOLDER = "\\\\cnc-pc\\gcode\\rings\\2026\\"
FILES = ["PL37-PM02_sheet01_1_staples_T1.ngc", "PL37-PM02_sheet01_2_text+grid_T2.ngc"]


def test_send_this_sheet(share, client):
    client.put("/api/smb", json=FORM)
    sent = client.post("/api/smb/send", json=SEND).get_json()["sent"]
    assert sent == [FOLDER + name for name in FILES]
    assert share.files[sent[0]].startswith(b"(PL37-PM02 - sheet 1 of 1: staple holes)")
    assert client.post("/api/smb/send", json=SEND).status_code == 200  # again: replaces, folder already there
    assert share.logins[-1] == ("cnc-pc", "cnc", "s3cret", 445)
    assert send_queue.jobs() == []


def test_offline_share_queues_until_it_answers(share, client):
    client.put("/api/smb", json=FORM)
    share.online = False
    result = client.post("/api/smb/send", json=SEND).get_json()
    assert result["queued"] == FILES and "does not answer" in result["reason"]
    other = client.post("/api/smb/send", json={**SEND, "codes": "AB10..AB20"}).get_json()
    assert len(other["queue"]["jobs"]) == 2 and share.files == {}
    # Sending the same sheet again replaces its queued copy instead of queueing it twice.
    client.post("/api/smb/send", json=SEND)
    assert [job["label"] for job in send_queue.jobs()] == ["AB10-AB20 sheet 1", "PL37-PM02 sheet 1"]

    send_queue.deliver()  # the background check: still offline
    assert len(send_queue.jobs()) == 2 and send_queue.status()["online"] is False
    share.online = True
    send_queue.deliver()
    assert send_queue.jobs() == [] and len(share.files) == 4


def test_queue_survives_a_restart(share, client):
    client.put("/api/smb", json=FORM)
    share.online = False
    client.post("/api/smb/send", json=SEND)
    files = sorted(p.name for p in (send_queue.QUEUE_DIR / send_queue.jobs()[0]["id"]).iterdir())
    assert files == sorted(["job.json", *FILES])  # on disk, in the data folder


def test_errors_pause_automatic_retries(share, client):
    client.put("/api/smb", json=FORM)
    share.online = False
    client.post("/api/smb/send", json=SEND)
    smb_share.save_config({**smb_share.load_config(), "password": "wrong"})
    share.online = True
    send_queue.deliver()
    status = client.get("/api/smb/queue").get_json()
    assert "wrong username or password" in status["paused"] and "wrong username" in status["jobs"][0]["error"]
    logins = len(share.logins)
    send_queue.deliver()  # paused: does not log in again, so the account is not locked
    assert len(share.logins) == logins
    client.put("/api/smb", json=FORM)  # the right password again
    assert client.get("/api/smb/queue").get_json()["paused"] == ""
    assert client.post("/api/smb/queue/send").get_json()["jobs"] == []


def test_remove_from_queue(share, client):
    client.put("/api/smb", json=FORM)
    share.online = False
    job_id = client.post("/api/smb/send", json=SEND).get_json()["queue"]["jobs"][0]["id"]
    send_queue.remove("..")  # only ever removes a folder of the queue
    assert send_queue.QUEUE_DIR.is_dir() and len(send_queue.jobs()) == 1
    assert client.delete(f"/api/smb/queue/{job_id}").get_json()["jobs"] == []
