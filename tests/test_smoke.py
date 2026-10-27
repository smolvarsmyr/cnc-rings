"""Smoke tests: these gate every release, so a commit that breaks generation never reaches the machines."""

import io
import json
import xml.etree.ElementTree as ET

import ezdxf
import pytest

import generate_sheets as gs
import webapp


@pytest.fixture(autouse=True)
def fresh_presets():
    gs.PRESETS_FILE.unlink(missing_ok=True)
    yield
    gs.PRESETS_FILE.unlink(missing_ok=True)


@pytest.fixture
def client():
    return webapp.app.test_client()


@pytest.mark.parametrize("preset", list(gs.load_presets(gs.DEFAULT_PRESETS_FILE)))
def test_default_presets_generate_valid_files(preset):
    settings, _ = gs.normalize_settings(gs.load_presets()[preset])
    job = gs.build_job(gs.parse_codes("PL37..PM02", settings.range_skip_letters), settings)
    files = gs.job_files(job, "test", {"svg", "dxf"})
    assert any(name.endswith(".svg") for name in files) and any(name.endswith(".dxf") for name in files)
    for name, data in files.items():
        if name.endswith(".svg"):
            ET.fromstring(data)
        elif name.endswith(".dxf"):
            doc = ezdxf.read(io.StringIO(data.decode()))
            assert {"TEXT", "GRID", "STAPLES"} <= {layer.dxf.name for layer in doc.layers}


def test_presets_fall_back_to_defaults_and_save_to_data_dir():
    assert gs.load_presets() == json.loads(gs.DEFAULT_PRESETS_FILE.read_text(encoding="utf-8"))
    gs.save_presets({"mine": gs.default_settings()})
    assert gs.PRESETS_FILE.parent == gs.DATA_DIR
    assert list(gs.load_presets()) == ["mine"]


def test_cli(tmp_path):
    preset = next(iter(gs.load_presets()))
    assert gs.main(["-p", preset, "-o", str(tmp_path), "PL37..PL40"]) == 0
    assert list(tmp_path.glob("*.svg")) and list(tmp_path.glob("*.dxf"))


def test_web_generate_and_download(client):
    schema = client.get("/api/schema").get_json()
    assert schema["version"]
    settings = next(iter(client.get("/api/presets").get_json().values()))
    result = client.post("/api/generate", json={"settings": settings, "codes": "PL37..PM02"}).get_json()
    assert result["sheets"] and result["sheets"][0]["svg"].startswith("<svg")
    response = client.post("/api/download", json={"settings": settings, "codes": "PL37..PM02", "format": "zip"})
    assert response.status_code == 200 and response.data[:2] == b"PK"


def test_web_preset_export_import(client):
    exported = json.loads(client.get("/api/presets/export").data)
    client.delete(f"/api/presets/{next(iter(exported))}")
    client.put("/api/presets/export", json={"settings": gs.default_settings()})  # a preset may be named "export"
    merged = client.post("/api/presets/import", json=exported).get_json()
    assert set(merged) == set(exported) | {"export"}
    assert client.delete("/api/presets/export").status_code == 200
    assert client.post("/api/presets/import", json=["not", "presets"]).status_code == 400
