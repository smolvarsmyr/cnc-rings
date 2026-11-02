"""CNC share settings and sending, against a fake pysmb connection that keeps the files in memory."""

import shutil
import stat
import types

import pytest
from smb.base import OperationFailure

import generate_sheets as gs
import send_queue
import smb_share
import webapp

FORM = {"server": "\\\\cnc-pc", "share": "gcode", "folder": "rings/2026", "username": "cnc", "password": "s3cret",
        "port": ""}


class FakeShare:
    """Just enough of pysmb's SMBConnection: one server with one share, and a password to check."""

    def __init__(self):
        self.files: dict[str, bytes] = {}  # by UNC path
        self.dirs = {"/"}  # paths inside the share
        self.logins = []
        self.online = True

    def connection(self, username, password, my_name, remote_name, domain="", is_direct_tcp=False):
        share = self

        class Connection:
            isUsingSMB2 = True

            def connect(self, ip, port, timeout=60):
                share.logins.append((ip, username, password, port))
                if not share.online:
                    raise ConnectionRefusedError(111, "Connection refused")
                return password == "s3cret"

            def listShares(self, timeout=30):
                return [types.SimpleNamespace(name=n, isSpecial=False) for n in ("gcode", "IPC$")]

            def getAttributes(self, service, path, timeout=30):
                if path not in share.dirs:
                    raise OperationFailure("not found", [])
                return types.SimpleNamespace(isDirectory=True)

            def createDirectory(self, service, path, timeout=30):
                assert path.rsplit("/", 1)[0] or "/" in share.dirs, "parent folder missing"
                share.dirs.add(path)

            def storeFile(self, service, path, file_obj, timeout=30):
                assert path.rsplit("/", 1)[0] in share.dirs, "folder missing"
                share.files["\\\\cnc-pc\\" + service + path.replace("/", "\\")] = file_obj.read()

            def close(self):
                pass

        return Connection()


@pytest.fixture
def share(monkeypatch):
    fake = FakeShare()
    monkeypatch.setattr(smb_share, "SMBConnection", fake.connection)
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
    error = client.post("/api/smb/test", json={**FORM, "share": "nope"}).get_json()["error"]
    assert "there is no share called nope; the server has: gcode" in error
    share.dirs |= {"/rings", "/rings/2026"}
    assert client.post("/api/smb/test", json=FORM).get_json() == {
        "message": "Connected to \\\\cnc-pc\\gcode\\rings\\2026 (SMB2)."}


@pytest.mark.parametrize("server, share_name, folder", [
    ("192.168.1.201/cncdisk", "cncdisk", "from the folder field"),  # no folder in the address: keep the field
    ("\\\\192.168.1.201\\CNCDISK\\rings", "CNCDISK", "rings"),
    ("smb://192.168.1.201/CNCDISK/rings/2026", "CNCDISK", "rings\\2026"),
])
def test_whole_address_in_server_field(server, share_name, folder):
    form = {"server": server, "share": "ignored", "folder": "from the folder field"}
    config = smb_share.merge_config(form, smb_share.DEFAULTS)
    assert (config["server"], config["share"], config["folder"]) == ("192.168.1.201", share_name, folder)


def test_guest_and_domain_logins(share):
    smb_share.test(smb_share.merge_config({**FORM, "username": "", "password": "s3cret"}, smb_share.DEFAULTS))
    assert share.logins[-1][1:3] == ("", "s3cret")
    smb_share.test(smb_share.merge_config({**FORM, "username": "WORKSHOP\\cnc"}, smb_share.DEFAULTS))
    assert share.logins[-1][1] == "cnc"


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
