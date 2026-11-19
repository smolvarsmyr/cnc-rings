"""G-code checks. A small simulator follows every move and asserts the rules that keep the machine safe."""

import io
import math
import re
import zipfile

import pytest

import generate_sheets as gs
import webapp

PRESET = "PL series 40x115 (photo)"


def make_job(codes="PL37..PM02", **overrides):
    raw = {**gs.load_presets(gs.DEFAULT_PRESETS_FILE)[PRESET], **overrides}
    settings, _ = gs.normalize_settings(raw)
    return gs.build_job(gs.parse_codes(codes), settings)


def gcode_files(job):
    return {name: data.decode("ascii") for name, data in gs.sheet_gcode(job, job.sheets[0], "t", "test").items()}


def simulate(program, s, plunge):
    """Run a program and check it. Returns the moves as (code, start, end) in work coordinates."""
    lines = program.splitlines()
    assert lines[-1] == "M30"
    assert "G21" in next(line for line in lines if not line.startswith("("))
    pos = {"X": None, "Y": None, "Z": None}
    feed = None
    deepest = {}  # (x, y) -> lowest Z cut there, so rapids may only go back down into holes already cut
    moves = []
    for line in lines:
        if line.startswith("("):
            continue
        words = re.findall(r"([A-Z])(-?[\d.]+)", line)
        letters = dict(words)
        if "F" in letters:
            feed = float(letters["F"])
        if not (letters.get("G") in ("0", "1", "2", "3") and line.startswith("G")):
            continue
        code = int(letters["G"])
        start = dict(pos)
        for axis in "XYZ":
            if axis in letters:
                pos[axis] = float(letters[axis])
        if code == 0:
            if pos["X"] != start["X"] or pos["Y"] != start["Y"]:
                assert start["Z"] is not None and start["Z"] >= s.gcode_safe_z - 1e-9, f"rapid sideways while low: {line}"
            if pos["Z"] < s.gcode_plunge_z - 1e-9:
                assert pos["Z"] > deepest.get((pos["X"], pos["Y"]), 0.0), f"rapid into uncut material: {line}"
        else:
            assert start["X"] is not None and start["Z"] is not None and feed, f"cut from an unknown position: {line}"
            if code in (2, 3):
                cx, cy = start["X"] + float(letters.get("I", 0)), start["Y"] + float(letters.get("J", 0))
                r0 = math.hypot(start["X"] - cx, start["Y"] - cy)
                assert abs(r0 - math.hypot(pos["X"] - cx, pos["Y"] - cy)) < 0.002, f"arc radius mismatch: {line}"
                length = math.tau * r0 if (start["X"], start["Y"]) == (pos["X"], pos["Y"]) else math.pi * r0
            else:
                length = math.dist((start["X"], start["Y"]), (pos["X"], pos["Y"]))
            drop = start["Z"] - pos["Z"]
            if drop > 1e-9:
                # 0.5 % for coordinates rounded to 4 decimals on very short ramps
                assert drop / math.hypot(length, drop) * feed <= plunge * 1.005, f"goes down faster than plunge: {line}"
            key = (pos["X"], pos["Y"])
            deepest[key] = min(deepest.get(key, 0.0), pos["Z"])
        moves.append((code, start, dict(pos)))
    return moves


def check_file(program, s, ops):
    """ops: the operations in the file, as in its name, e.g. "text+grid"."""
    settings = [gs._op_settings(s, op) for op in ops.split("+")]
    moves = simulate(program, s, max(o.plunge for o in settings))
    assert min(end["Z"] for _, _, end in moves) == pytest.approx(-max(o.depth for o in settings))
    xs = [end["X"] for _, _, end in moves if end["X"] is not None]
    ys = [end["Y"] for _, _, end in moves if end["Y"] is not None]
    origin = {"bottom-left": (0, 0), "bottom-right": (s.sheet_width, 0), "top-left": (0, s.sheet_height),
              "top-right": (s.sheet_width, s.sheet_height), "center": (s.sheet_width / 2, s.sheet_height / 2)}
    ox, oy = origin[s.gcode_origin]
    assert -ox - 1e-6 <= min(xs) and max(xs) <= s.sheet_width - ox + 1e-6
    assert -oy - 1e-6 <= min(ys) and max(ys) <= s.sheet_height - oy + 1e-6
    return moves


def test_one_file_per_tool_in_order():
    job = make_job()
    files = gcode_files(job)
    assert list(files) == ["t_1_staples_T1.ngc", "t_2_text+grid_T2.ngc"]  # text and grid share the V-bit
    for name, program in files.items():
        check_file(program, job.settings, name.split("_")[2])


@pytest.mark.parametrize("origin", ["top-left", "top-right", "bottom-left", "bottom-right", "center"])
def test_origin(origin):
    job = make_job(gcode_origin=origin)
    for name, program in gcode_files(job).items():
        check_file(program, job.settings, name.split("_")[2])


def test_tools_decide_the_files():
    files = gcode_files(make_job())
    assert "(Text:" in files["t_2_text+grid_T2.ngc"] and "(Grid lines:" in files["t_2_text+grid_T2.ngc"]
    job = make_job(gcode_grid_tool=3)
    assert list(gcode_files(job)) == ["t_1_staples_T1.ngc", "t_2_text_T2.ngc", "t_3_grid_T3.ngc"]
    for name, program in gcode_files(job).items():
        check_file(program, job.settings, name.split("_")[2])


def test_disabled_operations_are_left_out():
    assert list(gcode_files(make_job(gcode_text_enabled=False))) == ["t_1_staples_T1.ngc", "t_2_grid_T2.ngc"]
    off = {f"gcode_{op}_enabled": False for op, _, _ in gs.GCODE_OPS}
    assert gcode_files(make_job(**off)) == {}


@pytest.mark.parametrize("direction, arc", [("climb", "G3"), ("conventional", "G2")])
def test_holes_wider_than_the_tool_are_milled_out(direction, arc):
    job = make_job(gcode_staples_tool_diameter=1.0, gcode_direction=direction)
    program = gcode_files(job)["t_1_staples_T1.ngc"]
    moves = check_file(program, job.settings, "staples")
    assert arc in program and ({"G2", "G3"} - {arc}).isdisjoint(re.findall(r"^G[23](?= )", program, re.M))
    # A 5 mm hole with a 1 mm tool needs several rings to clear the middle: the cuts reach the centre.
    cuts = [(end["X"], end["Y"]) for code, _, end in moves if code and end["Z"] < 0]
    assert any(math.dist(p, (10, -50)) < 0.5 for p in cuts)


def test_tool_as_wide_as_the_holes_plunges():
    job = make_job(gcode_staples_tool_diameter=3.5, edge_hole_diameter=3.5, edge_slot_length=0, cell_slot_length=0)
    program = gcode_files(job)["t_1_staples_T1.ngc"]
    check_file(program, job.settings, "staples")
    assert not re.search(r"^G[23] ", program, re.M)
    assert not any("staple tool" in w for w in job.warnings)


def test_tool_wider_than_holes_warns():
    assert any("staple tool" in w for w in make_job(gcode_staples_tool_diameter=4.0).warnings)


def test_ramp_and_plunge_entries():
    ramped = gcode_files(make_job(gcode_text_enabled=False))["t_2_grid_T2.ngc"]
    assert re.search(r"^G1 X[\d.]+ Z-0.35", ramped, re.M)  # down half way along the line, then back
    straight = gcode_files(make_job(gcode_text_enabled=False, gcode_ramp_angle=0))["t_2_grid_T2.ngc"]
    assert not re.search(r"^G1 (?=.*[XY])(?=.*Z)", straight, re.M)


def test_passes_park_and_spindle():
    job = make_job(gcode_text_step=0.1, gcode_text_rpm=0, gcode_grid_enabled=False, gcode_park_x=0, gcode_park_y=0)
    files = gcode_files(job)
    text = files["t_2_text_T2.ngc"]
    check_file(text, job.settings, "text")
    assert "Z-0.1 " in text or "Z-0.1\n" in text
    assert not re.search(r"^M[35]\b", text, re.M)
    assert text.splitlines()[-2] == "G0 X0 Y0"
    assert "M3 S16000" in files["t_1_staples_T1.ngc"] and "M5" in files["t_1_staples_T1.ngc"]


def test_web_download():
    client = webapp.app.test_client()
    settings = gs.load_presets(gs.DEFAULT_PRESETS_FILE)[PRESET]
    body = {"settings": settings, "codes": "PL37..PL40", "format": "gcode"}
    response = client.post("/api/download", json=body)
    assert response.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(response.data)).namelist() == [
        "PL37-PL40_sheet01_1_staples_T1.ngc", "PL37-PL40_sheet01_2_text+grid_T2.ngc"]

    only_grid = {**settings, "gcode_staples_enabled": False, "gcode_text_enabled": False}
    response = client.post("/api/download", json={**body, "settings": only_grid})
    assert response.mimetype == "text/plain" and response.data.startswith(b"(PL37-PL40")

    none = {**only_grid, "gcode_grid_enabled": False}
    assert client.post("/api/download", json={**body, "settings": none}).status_code == 400

    zipped = client.post("/api/download", json={**body, "format": "zip"})
    assert any(name.endswith(".ngc") for name in zipfile.ZipFile(io.BytesIO(zipped.data)).namelist())


def text_plates(order):
    """The plates the text cuts visit, in order, as (row, column) with row 1 at the bottom."""
    job = make_job(gcode_text_order=order, gcode_grid_enabled=False, gcode_origin="bottom-left")
    program = gcode_files(job)["t_2_text_T2.ngc"]
    g = job.grid
    visits = []
    for code, start, end in check_file(program, job.settings, "text"):
        if code and end["Z"] < 0:
            plate = (int((end["Y"] - g.y0) // g.plate_h) + 1, int((end["X"] - g.x0) // g.plate_w) + 1)
            if not visits or visits[-1] != plate:
                visits.append(plate)
    return job, visits


def test_text_plate_by_plate_from_the_bottom():
    job, visits = text_plates("plate by plate")
    plates_with_text = {(p.row, p.col) for p in job.sheets[0].plates if p.code}
    assert len(visits) == len(plates_with_text) and set(visits) == plates_with_text  # each plate once, finished
    assert [row for row, _ in visits] == sorted(row for row, _ in visits)  # bottom row first, then up
    first_row = [col for row, col in visits if row == 1]
    second_row = [col for row, col in visits if row == 2]
    assert first_row == sorted(first_row) and second_row == sorted(second_row, reverse=True)  # back and forth


def test_text_row_by_row_from_the_bottom():
    _, visits = text_plates("row by row")
    rows = [row for row, _ in visits]
    assert rows == sorted(rows)  # never back to a lower row


def test_text_free_order_is_the_shortest_way():
    _, visits = text_plates("free")
    rows = [row for row, _ in visits]
    assert rows != sorted(rows)


def test_selected_plates_only():
    job = make_job(gcode_origin="bottom-left")
    picked = {(1, 1), (1, 2), (3, 5)}
    files = gs.sheet_gcode(job, job.sheets[0], "t", "test", picked)
    assert list(files) == ["t_2_text+grid_T2_selected.ngc"]  # no staple holes; numbered as in the full export
    program = files["t_2_text+grid_T2_selected.ngc"].decode()
    codes = {(p.row, p.col): p.code for p in job.sheets[0].plates}
    assert f"(Plates: {codes[1, 1]} [1:1], {codes[1, 2]} [1:2], {codes[3, 5]} [3:5])" in program
    rects = [job.grid.plate_rect(row, col) for row, col in picked]
    cuts = [(end["X"], end["Y"]) for code, _, end in check_file(program, job.settings, "text+grid") if code and end["Z"] < 0]
    assert all(any(x0 - 1e-6 <= x <= x0 + w + 1e-6 and y0 - 1e-6 <= y <= y0 + h + 1e-6 for x0, y0, w, h in rects)
               for x, y in cuts)  # nothing outside the picked plates
    for x0, y0, w, h in rects:  # and the text of each of them
        assert any(x0 + 1 < x < x0 + w - 1 and y0 + 1 < y < y0 + h - 1 for x, y in cuts)


def test_plate_edges_are_cut_once():
    grid = make_job().grid
    edges = gs.plate_edges(grid, {(1, 1), (1, 2)})  # two plates side by side
    assert len(edges) == 5  # bottom and top joined across both, three uprights


def test_selected_needs_plates_and_keeps_numbering():
    job = make_job(gcode_staples_tool=2)  # staples, text and grid all in one file
    assert list(gs.sheet_gcode(job, job.sheets[0], "t", "test", {(1, 1)})) == ["t_1_text+grid_T2_selected.ngc"]
    with pytest.raises(ValueError, match="Select at least one plate"):
        gs.sheet_gcode(job, job.sheets[0], "t", "test", set())


def test_web_selected_download():
    client = webapp.app.test_client()
    settings = gs.load_presets(gs.DEFAULT_PRESETS_FILE)[PRESET]
    body = {"settings": settings, "codes": "PL37..PL40", "format": "gcode", "plates": [[1, 1], [1, 2]]}
    response = client.post("/api/download", json=body)
    assert response.headers["Content-Disposition"] == 'attachment; filename="PL37-PL40_sheet01_2_text+grid_T2_selected.ngc"'
    assert client.post("/api/download", json={**body, "plates": "nonsense"}).status_code == 400
    assert client.post("/api/download", json={**body, "plates": []}).status_code == 400
    full = client.post("/api/download", json={**body, "plates": None})
    assert full.data[:2] == b"PK"  # without plates: the whole sheet, staple holes too


@pytest.mark.parametrize("style, line", [("G4 P seconds", "G4 P2.5"), ("G4 P milliseconds", "G4 P2500"),
                                         ("G4 X seconds", "G4 X2.5")])
def test_spindle_wait_in_the_controllers_units(style, line):
    for program in gcode_files(make_job(gcode_spinup=2.5, gcode_dwell=style)).values():
        lines = program.splitlines()
        assert lines[lines.index(next(l for l in lines if l.startswith("M3 "))) + 1] == line
