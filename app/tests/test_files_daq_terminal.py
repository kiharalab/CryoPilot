"""Tests for project files, server path completion, DAQ results, and the terminal.

Run from the repository root with a throwaway app home:
    CRYOAGENT_APP_HOME=$(mktemp -d) .venv/bin/python -m pytest app/tests -q
"""

from __future__ import annotations

import importlib
import json
import os
import queue
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CRYOAGENT_APP_HOME", tempfile.mkdtemp(prefix="cryopilot-tests-"))


def _isolate_app_home() -> None:
    """Make sure the app database and projects live in the test home, never in ~/.cryoagent."""

    from app.server import config

    wanted = Path(os.environ["CRYOAGENT_APP_HOME"]).expanduser()
    if config.APP_HOME == wanted:
        return
    if "app.server.state" in sys.modules:
        pytest.skip("The app state was loaded with another CRYOAGENT_APP_HOME; run with it set.", allow_module_level=True)
    # Another test module imported the config first; nothing has used its paths yet.
    importlib.reload(config)


_isolate_app_home()

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from app.server import auth, config, daq, files, routes_terminal, terminal  # noqa: E402
from app.server.main import app  # noqa: E402
from app.server.state import store  # noqa: E402


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.TOKEN}"}


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    """The modeling agent's sandbox, moved out of the repository for the tests."""

    root = tmp_path_factory.mktemp("sandbox")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(files, "MODELING_SANDBOX_ROOT", root)
        patch.setattr(files, "MODELING_WORKSPACES_DIR", root / "workspaces")
        patch.setattr(config, "MODELING_SANDBOX_ROOT", root)
        yield root


@pytest.fixture(scope="module")
def client(sandbox):
    test_client = TestClient(app, headers=auth_headers())
    yield test_client
    routes_terminal.shutdown()


_project_counter = iter(range(1, 10_000))


@pytest.fixture
def project(client):
    response = client.post("/api/projects", json={"title": f"Files test {next(_project_counter)}"})
    assert response.status_code == 200, response.text
    return response.json()


def workspace_of(project) -> Path:
    return files.modeling_workspace(project["slug"])


# ------------------------------------------------------------------ files
def test_roots_and_one_level_tree(client, project, tmp_path):
    workspace = workspace_of(project)
    (workspace / "data" / "model.pdb").write_text("ATOM\n")
    (workspace / "data" / "map.mrc").write_bytes(b"\0" * 16)
    (workspace / "outputs" / "notes.md").write_text("# Notes\n")
    (workspace / ".hidden").write_text("secret")
    (workspace / "__pycache__").mkdir()
    (workspace / "zeta.txt").write_text("z")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (workspace / "escape.txt").symlink_to(outside)

    roots = client.get(f"/api/projects/{project['id']}/files/roots").json()
    assert [root["key"] for root in roots] == ["modeling", "files", "reconstruction"]
    assert roots[0]["label"] == "Modeling workspace" and all(root["writable"] for root in roots)

    top = client.get(f"/api/projects/{project['id']}/files/tree", params={"root": "modeling"}).json()
    assert top["path"] == "" and top["truncated"] is False
    names = [entry["name"] for entry in top["entries"]]
    assert names == ["data", "outputs", "zeta.txt"]  # folders first; hidden, caches, and escaping links skipped
    assert top["entries"][0]["type"] == "dir" and top["entries"][0]["size"] is None

    data = client.get(f"/api/projects/{project['id']}/files/tree", params={"root": "modeling", "path": "data"}).json()
    assert {entry["name"]: entry["category"] for entry in data["entries"]} == {"map.mrc": "volume", "model.pdb": "structure"}
    assert data["entries"][0]["path"] == "data/map.mrc"

    escaped = client.get(f"/api/projects/{project['id']}/files/tree", params={"root": "modeling", "path": "../.."})
    assert escaped.status_code == 400
    linked = client.get(f"/api/projects/{project['id']}/files/raw", params={"root": "modeling", "path": "escape.txt"})
    assert linked.status_code == 400
    unknown = client.get(f"/api/projects/{project['id']}/files/tree", params={"root": "nowhere"})
    assert unknown.status_code == 400

    # The old flat listing still works.
    flat = client.get(f"/api/projects/{project['id']}/files", params={"root": "modeling"}).json()
    assert {entry["path"] for entry in flat["files"]} >= {"data/model.pdb", "outputs/notes.md"}


def test_text_table_raw_upload_and_delete(client, project):
    workspace = workspace_of(project)
    base = f"/api/projects/{project['id']}/files"
    # A file just over the cap whose last character straddles the cut.
    big = "a" * (files.TEXT_PREVIEW_BYTES - 1) + "é" + "tail"
    (workspace / "big.log").write_text(big, encoding="utf-8")
    text = client.get(f"{base}/text", params={"root": "modeling", "path": "big.log"}).json()
    assert text["truncated"] is True and text["size"] == len(big.encode("utf-8"))
    assert text["text"] == "a" * (files.TEXT_PREVIEW_BYTES - 1) and text["binary"] is False

    (workspace / "scores.csv").write_text("chain,residue,score\nA,1,0.5\nA,2,-0.25\n\nB,3,1.0\n")
    table = client.get(f"{base}/table", params={"root": "modeling", "path": "scores.csv"}).json()
    assert table["columns"] == ["chain", "residue", "score"]
    assert table["rows"] == [["A", "1", "0.5"], ["A", "2", "-0.25"], ["B", "3", "1.0"]]
    assert table["total_rows"] == 3 and table["truncated"] is False

    (workspace / "wide.tsv").write_text("a\tb\n" + "".join(f"{i}\t{i * 2}\n" for i in range(files.TABLE_ROW_LIMIT + 5)))
    tsv = client.get(f"{base}/table", params={"root": "modeling", "path": "wide.tsv"}).json()
    assert tsv["delimiter"] == "\t" and len(tsv["rows"]) == files.TABLE_ROW_LIMIT
    assert tsv["total_rows"] == files.TABLE_ROW_LIMIT + 5 and tsv["truncated"] is True

    raw = client.get(f"{base}/raw", params={"root": "modeling", "path": "scores.csv"})
    assert raw.status_code == 200 and raw.text.startswith("chain,residue")
    assert raw.headers["content-disposition"].startswith("inline")
    download = client.get(f"{base}/raw", params={"root": "modeling", "path": "scores.csv", "download": 1})
    assert download.headers["content-disposition"].startswith("attachment")
    absolute = client.get(f"{base}/raw", params={"root": "modeling", "path": str((workspace / "scores.csv").resolve())})
    assert absolute.status_code == 200
    (workspace / "page.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    svg = client.get(f"{base}/raw", params={"root": "modeling", "path": "page.svg"})
    assert "sandbox" in svg.headers["content-security-policy"]

    uploaded = client.post(f"{base}/upload", data={"root": "modeling", "subdir": "data"}, files={"file": ("seq.fasta", b">A\nMKV\n")})
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["path"] == "data/seq.fasta" and uploaded.json()["category"] == "data"
    assert (workspace / "data" / "seq.fasta").read_text() == ">A\nMKV\n"
    assert not list((workspace / "data").glob(".*uploading"))

    assert client.delete(base, params={"root": "modeling", "path": "data/seq.fasta"}).json() == {"ok": True}
    assert not (workspace / "data" / "seq.fasta").exists()
    assert client.delete(base, params={"root": "modeling", "path": "data/seq.fasta"}).status_code == 404
    assert client.delete(base, params={"root": "modeling", "path": ""}).status_code == 400
    (workspace / "outputs" / "run1").mkdir()
    (workspace / "outputs" / "run1" / "x.txt").write_text("x")
    assert client.delete(base, params={"root": "modeling", "path": "outputs/run1"}).status_code == 200
    assert not (workspace / "outputs" / "run1").exists()


def test_deleting_a_symlink_keeps_its_target(client, project):
    workspace = workspace_of(project)
    target = workspace / "outputs" / "model.pdb"
    target.write_text("ATOM\n")
    (workspace / "data" / "link.pdb").symlink_to(target)
    response = client.delete(f"/api/projects/{project['id']}/files", params={"root": "modeling", "path": "data/link.pdb"})
    assert response.status_code == 200
    assert target.exists() and not (workspace / "data" / "link.pdb").exists()


@pytest.mark.parametrize(
    ("name", "sandboxed"),
    [
        ("page.html", True),
        ("feed.rss", True),
        ("graph.rdf", True),
        ("style.xslt", True),
        ("page.xhtml", True),
        ("plot.png", False),
        ("scores.csv", False),
        ("run.log", False),
        ("model.pdb", False),
        ("map.mrc", False),
    ],
)
def test_raw_files_are_sandboxed_unless_known_inert(client, project, name, sandboxed):
    (workspace_of(project) / name).write_bytes(b"<x/>")
    response = client.get(f"/api/projects/{project['id']}/files/raw", params={"root": "modeling", "path": name})
    assert response.status_code == 200
    assert ("sandbox" in response.headers.get("content-security-policy", "")) is sandboxed
    assert response.headers["x-content-type-options"] == "nosniff"


def test_upload_can_keep_an_existing_file(client, project):
    workspace = workspace_of(project)
    base = f"/api/projects/{project['id']}/files/upload"

    def upload(name, body, **form):
        response = client.post(base, data={"root": "modeling", "subdir": "data", **form}, files={"file": (name, body)})
        assert response.status_code == 200, response.text
        return response.json()

    assert upload("model.pdb", b"first", on_conflict="rename")["path"] == "data/model.pdb"
    second = upload("model.pdb", b"second", on_conflict="rename")
    assert (second["path"], second["name"]) == ("data/model-2.pdb", "model-2.pdb")
    assert upload("model.pdb", b"third", on_conflict="rename")["name"] == "model-3.pdb"
    assert (workspace / "data" / "model.pdb").read_bytes() == b"first"
    upload("emd.map.gz", b"a", on_conflict="rename")
    assert upload("emd.map.gz", b"b", on_conflict="rename")["name"] == "emd-2.map.gz"

    # Without it (the Files panel), an upload replaces the file of that name.
    assert upload("model.pdb", b"replaced")["path"] == "data/model.pdb"
    assert (workspace / "data" / "model.pdb").read_bytes() == b"replaced"
    bad = client.post(base, data={"root": "modeling", "on_conflict": "skip"}, files={"file": ("x.txt", b"x")})
    assert bad.status_code == 400


def test_cryosparc_root_is_listed_and_read_only(client, project, tmp_path, monkeypatch):
    cs_dir = tmp_path / "CS-test"
    (cs_dir / "J29").mkdir(parents=True)
    (cs_dir / "J29" / "job.json").write_text('{"uid": "J29"}')
    session = store.create_session(project_id=project["id"], kind="reconstruction", title="Env", config={"project_uid": "P61"})
    unbound = store.create_session(project_id=project["id"], kind="reconstruction", title="Not bound yet")

    fake = types.ModuleType("app.server.cryosparc_jobs")
    fake.project_dir_for_session = lambda s: cs_dir if s["id"] == session["id"] else None
    monkeypatch.setitem(sys.modules, "app.server.cryosparc_jobs", fake)

    base = f"/api/projects/{project['id']}/files"
    roots = client.get(f"{base}/roots").json()
    key = f"cryosparc:{session['id']}"
    assert [root["key"] for root in roots][-1] == key and len(roots) == 4
    assert roots[-1]["label"] == "CryoSPARC P61" and roots[-1]["writable"] is False
    assert f"cryosparc:{unbound['id']}" not in {root["key"] for root in roots}

    tree = client.get(f"{base}/tree", params={"root": key}).json()
    assert [entry["name"] for entry in tree["entries"]] == ["J29"]
    assert client.get(f"{base}/text", params={"root": key, "path": "J29/job.json"}).json()["text"] == '{"uid": "J29"}'
    upload = client.post(f"{base}/upload", data={"root": key, "subdir": ""}, files={"file": ("x.txt", b"x")})
    assert upload.status_code == 403
    assert client.delete(base, params={"root": key, "path": "J29/job.json"}).status_code == 403
    assert (cs_dir / "J29" / "job.json").exists()

    # A session of another project cannot be reached through this project.
    other = client.post("/api/projects", json={"title": "Other project"}).json()
    assert client.get(f"/api/projects/{other['id']}/files/tree", params={"root": key}).status_code == 400

    # A CryoSPARC lookup that fails does not break the roots.
    def broken(_session):
        raise RuntimeError("CryoSPARC is unreachable")

    fake.project_dir_for_session = broken
    assert [root["key"] for root in client.get(f"{base}/roots").json()] == ["modeling", "files", "reconstruction"]


# ------------------------------------------------------- server paths
def test_server_path_completion(client, tmp_path):
    (tmp_path / "movies").mkdir()
    (tmp_path / "metadata").mkdir()
    (tmp_path / ".cache").mkdir()
    for index in range(3):
        (tmp_path / "movies" / f"frame_{index}.tif").write_bytes(b"\0" * (index + 1))
    (tmp_path / "movies" / "gain.mrc").write_bytes(b"\0")
    (tmp_path / "readme.txt").write_text("r")

    listing = client.get("/api/fs/list", params={"path": str(tmp_path)}).json()
    assert [entry["name"] for entry in listing["entries"]] == ["metadata", "movies", "readme.txt"]
    assert listing["parent"] == str(tmp_path) and listing["type"] == "dir" and listing["glob"] is False

    partial = client.get("/api/fs/list", params={"path": str(tmp_path / "m")}).json()
    assert [entry["name"] for entry in partial["entries"]] == ["metadata", "movies"]
    assert partial["prefix"] == "m" and partial["exists"] is False
    assert partial["entries"][1]["path"] == str(tmp_path / "movies")

    dirs_only = client.get("/api/fs/list", params={"path": f"{tmp_path}/", "dirs_only": 1}).json()
    assert [entry["name"] for entry in dirs_only["entries"]] == ["metadata", "movies"]
    hidden = client.get("/api/fs/list", params={"path": str(tmp_path / ".c")}).json()
    assert [entry["name"] for entry in hidden["entries"]] == [".cache"]

    pattern = client.get("/api/fs/list", params={"path": str(tmp_path / "movies" / "*.tif")}).json()
    assert pattern["glob"] is True and pattern["match_count"] == 3
    assert [entry["name"] for entry in pattern["entries"]] == ["frame_0.tif", "frame_1.tif", "frame_2.tif"]
    assert pattern["entries"][2]["size"] == 3

    home = client.get("/api/fs/list", params={"path": "~"}).json()
    assert home["parent"] == str(Path.home())

    missing = client.get("/api/fs/list", params={"path": str(tmp_path / "nope" / "x")})
    assert missing.status_code == 400 and "No folder" in missing.json()["error"]
    with pytest.raises(ValueError):
        files._refuse_pseudo_filesystem("/proc/self")


def test_glob_completion_needs_a_top_folder_and_skips_pseudo_filesystems(client, tmp_path, monkeypatch):
    for pattern in ("/[p]roc/*", "/s?s/*", "/*/self/*"):
        response = client.get("/api/fs/list", params={"path": pattern})
        assert response.status_code == 400 and "Start the pattern with a folder" in response.json()["error"]

    # Stand-ins for /proc: one reached directly, one through a symlink.
    pseudo = tmp_path / "pseudo"
    (pseudo / "1").mkdir(parents=True)
    (pseudo / "1" / "status").write_text("x")
    (tmp_path / "data" / "1").mkdir(parents=True)
    (tmp_path / "data" / "1" / "status").write_text("x")
    (tmp_path / "linked").symlink_to(pseudo)
    monkeypatch.setattr(files, "PSEUDO_FILESYSTEMS", (os.path.realpath(pseudo),))
    found = client.get("/api/fs/list", params={"path": f"{tmp_path}/*/*/status"}).json()
    assert [entry["path"] for entry in found["entries"]] == [f"{tmp_path}/data/1/status"]
    assert found["match_count"] == 1 and found["count_capped"] is False

    folders = client.get("/api/fs/list", params={"path": f"{tmp_path}/d*/"}).json()
    assert [entry["name"] for entry in folders["entries"]] == ["data"]


def test_glob_completion_stops_in_time_without_matches(tmp_path, monkeypatch):
    for index in range(50):
        (tmp_path / f"d{index}" / "deeper").mkdir(parents=True)
    monkeypatch.setattr(files, "GLOB_TIME_LIMIT_S", 0.0)
    result = files.list_server_path(f"{tmp_path}/*/*/nothing*")
    assert result["match_count"] == 0 and result["count_capped"] is True
    monkeypatch.setattr(files, "GLOB_TIME_LIMIT_S", 3.0)
    assert files.list_server_path(f"{tmp_path}/*/*/nothing*")["count_capped"] is False


# -------------------------------------------------------------------- DAQ
CSV_HEADER = (
    "candidate_index,model_number,index,chain,residue_number,insertion_code,residue_name,scored,"
    "aa,ca,ss,total,aa_smoothed,ca_smoothed,ss_smoothed,total_smoothed\n"
)
# Chain A: residue 3 is unscored and residue 4A carries an insertion code.
RESIDUES = [
    ("A", 1, "", "MET", 0.6),
    ("A", 2, "", "LYS", -0.2),
    ("A", 3, "", "VAL", None),
    ("A", 4, "", "LEU", 0.4),
    ("A", 4, "A", "SER", 1.0),
    ("A", 6, "", "GLY", -1.6),
    ("B", 10, "", "ALA", -0.5),
    ("B", 11, "", "GLU", -0.1),
    ("B", 12, "", "ARG", 0.3),
]
# Centered 3-residue means along each chain over the scored residues in the window.
EXPECTED_MEANS_3 = [0.2, 0.2, None, 0.7, -0.2 / 3, -0.3, -0.3, -0.1, 0.1]


def daq_csv(candidate: int = 1) -> str:
    rows = []
    for index, (chain, resnum, icode, resname, aa) in enumerate(RESIDUES, start=1):
        if aa is None:
            values = ["", "", "", ""]
        else:
            values = [aa, aa / 2, -aa, aa + 1]
        rows.append(",".join(str(part) for part in [candidate, 1, index, chain, resnum, icode, resname, aa is not None, *values, "", "", "", ""]))
    return "\n".join(rows) + "\n"


def pdb_line(record, serial, name, resname, chain, resnum, icode, b=20.0, element="C"):
    return (
        f"{record:<6}{serial:>5} {name:<4} {resname:>3} {chain:1}{resnum:>4}{icode:1}   "
        f"{1.0:8.3f}{2.0:8.3f}{3.0:8.3f}{1.00:6.2f}{b:6.2f}          {element:>2}"
    )


def daq_pdb() -> str:
    lines = ["HEADER    TEST MODEL"]
    serial = 1
    for chain, resnum, icode, resname, _ in RESIDUES:
        for name in ("N", "CA"):
            lines.append(pdb_line("ATOM", serial, name, resname, chain, resnum, icode))
            serial += 1
    lines.append("ANISOU    1  N   MET A   1     1234   2345   3456    123    234    345       N")
    lines.append(pdb_line("HETATM", serial, "MG", "MG", "A", 501, "", b=33.0, element="MG"))
    lines.append("END")
    return "\n".join(lines) + "\n"


def write_daq_run(workspace: Path, folder: str = "outputs/check/daq/candidate_001", *, with_model: bool = True) -> Path:
    directory = workspace / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "daq_scores.csv").write_text(CSV_HEADER + daq_csv())
    (workspace / "data" / "model.pdb").write_text(daq_pdb())
    (workspace / "data" / "map.mrc").write_bytes(b"\0" * 64)
    metadata = {
        "metric_type": "daq",
        "map_path": str((workspace / "data" / "map.mrc").resolve()),
        "model_path": str((workspace / "data" / "model.pdb").resolve()),
        "candidate_index": 1,
        "settings": {"contour": 0.56},
    }
    (directory / "daq_scores.metadata.json").write_text(json.dumps(metadata))
    if with_model:
        (directory / "aa.pdb").write_text(daq_pdb())
    return directory


def test_window_means_match_the_agents_running_mean():
    sys.path.insert(0, str(REPO_ROOT / "modeling_agent"))
    try:
        from refinement import _running_mean
    except ImportError:
        pytest.skip("The modeling agent's dependencies are not installed")
    finally:
        sys.path.remove(str(REPO_ROOT / "modeling_agent"))
    values = [0.6, -0.2, None, 0.4, 1.0, -1.6, None, None, 0.9]
    for window in (1, 3, 5, 19):
        assert daq.window_means(values, window) == pytest.approx(_running_mean(values, window))


def test_daq_results_and_scores(client, project):
    workspace = workspace_of(project)
    write_daq_run(workspace)
    (workspace / ".venv" / "daq").mkdir(parents=True)
    (workspace / ".venv" / "daq" / "daq_scores.csv").write_text(CSV_HEADER)  # skipped folder

    results = client.get(f"/api/projects/{project['id']}/daq").json()["results"]
    assert len(results) == 1
    result = results[0]
    assert result["path"] == "outputs/check/daq/candidate_001/daq_scores.csv"
    assert result["folder"] == "outputs/check/daq/candidate_001"
    assert result["label"] == "model.pdb" and result["contour"] == 0.56 and result["candidate_index"] == 1
    assert result["map_relative"] == "data/map.mrc" and result["model_relative"] == "data/model.pdb"
    assert result["residue_count"] == 9 and result["scored_count"] == 8 and result["structure_available"] is True
    assert result["summary"]["window"] == 19

    scores = client.get(f"/api/projects/{project['id']}/daq/scores", params={"path": result["path"], "window": 3}).json()
    assert scores["chains"] == ["A", "B"] and scores["window"] == 3
    means = [residue["aa_mean"] for residue in scores["residues"]]
    assert means == pytest.approx(EXPECTED_MEANS_3)
    unscored = scores["residues"][2]
    assert unscored["scored"] is False and unscored["aa"] is None and unscored["aa_mean"] is None
    insertion = scores["residues"][4]
    assert (insertion["chain"], insertion["resnum"], insertion["icode"], insertion["resname"]) == ("A", 4, "A", "SER")
    assert insertion["ca"] == pytest.approx(0.5) and insertion["total"] == pytest.approx(2.0)

    summary = scores["summary"]
    assert summary["residues"] == 9 and summary["scored"] == 8
    assert summary["fraction_negative"] == pytest.approx(4 / 8)
    assert summary["metrics"]["aa"]["count"] == 8 and summary["metrics"]["aa"]["min"] == pytest.approx(-1.6)
    assert summary["metrics"]["aa_mean"]["mean"] == pytest.approx(sum(m for m in EXPECTED_MEANS_3 if m is not None) / 8)
    chain_b = scores["chain_summary"][1]
    assert chain_b == {"chain": "B", "residues": 3, "scored": 3, "first": 10, "last": 12,
                       "mean": pytest.approx(-0.1), "fraction_negative": pytest.approx(2 / 3)}

    single = client.get(f"/api/projects/{project['id']}/daq/scores", params={"path": result["path"], "window": 1}).json()
    assert [r["aa_mean"] for r in single["residues"]] == pytest.approx([r[4] for r in RESIDUES])

    bad = f"/api/projects/{project['id']}/daq/scores"
    assert client.get(bad, params={"path": "../../etc/passwd"}).status_code in {400, 404}
    assert client.get(bad, params={"path": result["path"], "window": 0}).status_code == 400
    assert client.get(bad, params={"path": "outputs/none.csv"}).status_code == 404
    (workspace / "data" / "other.csv").write_text("x,y\n1,2\n")
    assert client.get(bad, params={"path": "data/other.csv"}).status_code == 400


def test_daq_structure_rewrites_b_factors(client, project):
    workspace = workspace_of(project)
    write_daq_run(workspace)
    path = "outputs/check/daq/candidate_001/daq_scores.csv"
    response = client.get(f"/api/projects/{project['id']}/daq/structure", params={"path": path, "metric": "aa_mean", "window": 3})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    original = daq_pdb().splitlines()
    rewritten = response.text.splitlines()
    assert len(rewritten) == len(original)

    expected = {(chain, resnum, icode): mean for (chain, resnum, icode, _, _), mean in zip(RESIDUES, EXPECTED_MEANS_3)}
    atoms = 0
    for before, after in zip(original, rewritten):
        if before.startswith("ATOM  "):
            atoms += 1
            key = (before[21], int(before[22:26]), before[26].strip())
            mean = expected[key]
            assert after[60:66] == ("  0.00" if mean is None else f"{mean:6.2f}")
            assert after[:60] == before[:60] and after[66:] == before[66:]
        elif before.startswith("HETATM"):
            assert after[60:66] == "  0.00"  # ligands are not scored
        else:
            assert after == before  # header, ANISOU, END untouched
    assert atoms == 2 * len(RESIDUES)

    raw = client.get(f"/api/projects/{project['id']}/daq/structure", params={"path": path, "metric": "aa"}).text
    first = next(line for line in raw.splitlines() if line.startswith("ATOM"))
    assert first[60:66] == "  0.60"
    assert client.get(f"/api/projects/{project['id']}/daq/structure", params={"path": path, "metric": "bogus"}).status_code == 400


def test_daq_structure_falls_back_to_the_recorded_model(client, project, sandbox, tmp_path):
    workspace = workspace_of(project)
    write_daq_run(workspace, "outputs/nomodel", with_model=False)
    params = {"path": "outputs/nomodel/daq_scores.csv", "metric": "aa"}
    response = client.get(f"/api/projects/{project['id']}/daq/structure", params=params)
    assert response.status_code == 200
    assert next(line for line in response.text.splitlines() if line.startswith("ATOM"))[60:66] == "  0.60"

    # A recorded model outside this project's workspace is refused, even inside the agent's folder.
    metadata = workspace / "outputs" / "nomodel" / "daq_scores.metadata.json"
    for outside in (tmp_path / "model.pdb", sandbox / "workspaces" / "another-project" / "model.pdb"):
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text(daq_pdb())
        metadata.write_text(json.dumps({"model_path": str(outside), "candidate_index": 1}))
        assert client.get(f"/api/projects/{project['id']}/daq/structure", params=params).status_code == 400
        listed = client.get(f"/api/projects/{project['id']}/daq").json()["results"]
        assert next(result for result in listed if result["folder"] == "outputs/nomodel")["structure_available"] is False


def test_daq_list_skips_unreadable_tables(client, project):
    workspace = workspace_of(project)
    write_daq_run(workspace)
    broken = workspace / "outputs" / "broken"
    broken.mkdir(parents=True)
    (broken / "daq_scores.csv").write_bytes(CSV_HEADER.encode() + b"1,1,1,A,1,,MET,True,\xff\xfe,0,0,0,,,,\n")
    digits = workspace / "outputs" / "digits"
    digits.mkdir(parents=True)
    (digits / "daq_scores.csv").write_text(CSV_HEADER + "1,1,\u00b2,A,1,,MET,True,0.5,0,0,0,,,,\n", encoding="utf-8")
    response = client.get(f"/api/projects/{project['id']}/daq")
    assert response.status_code == 200
    folders = {result["folder"] for result in response.json()["results"]}
    assert folders == {"outputs/check/daq/candidate_001", "outputs/digits"}


def test_daq_keeps_only_the_recorded_candidate(tmp_path):
    table = tmp_path / "daq_scores.csv"
    table.write_text(CSV_HEADER + daq_csv(1) + daq_csv(2).replace(",MET,True,0.6,", ",MET,True,-9.0,"))
    rows = daq.load_rows(table, 2)
    assert len(rows) == len(RESIDUES) and rows[0]["aa"] == -9.0
    assert daq.load_rows(table, 1)[0]["aa"] == 0.6


# --------------------------------------------------------------- terminal
class FrameReader:
    """Collects WebSocket frames on a thread so a stuck shell fails the test instead of hanging it."""

    def __init__(self, socket) -> None:
        self.frames: queue.Queue = queue.Queue()
        self.output = ""
        self.exit_code = None
        self.closed = False
        thread = threading.Thread(target=self._run, args=(socket,), daemon=True)
        thread.start()

    def _run(self, socket) -> None:
        while True:
            try:
                self.frames.put(socket.receive_json())
            except Exception as exc:  # the socket closed
                self.frames.put(exc)
                return

    def wait_for(self, predicate, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"Timed out; terminal output so far: {self.output[-2000:]!r}")
            try:
                frame = self.frames.get(timeout=remaining)
            except queue.Empty:
                continue
            if isinstance(frame, Exception):
                self.closed = True
                if not predicate():
                    raise AssertionError(f"Socket closed ({frame!r}); output: {self.output[-2000:]!r}")
                return
            if frame["type"] == "output":
                self.output += frame["data"]
            elif frame["type"] == "exit":
                self.exit_code = frame["code"]


@pytest.fixture
def plain_shell(monkeypatch):
    # A shell without the user's startup files keeps the test independent of their setup.
    monkeypatch.setattr(terminal, "shell_command", lambda: ("/bin/sh", ["sh"]))


def test_terminal_round_trip(client, project, plain_shell):
    created = client.post("/api/terminals", json={"project_id": project["id"], "root": "modeling"})
    assert created.status_code == 200, created.text
    view = created.json()
    assert view["alive"] is True and view["title"] == "Modeling workspace"
    assert view["cwd"] == str(workspace_of(project))
    assert view["id"] in {entry["id"] for entry in client.get("/api/terminals").json()["terminals"]}
    url = f"/api/terminals/{view['id']}/ws"

    with client.websocket_connect(url, headers=auth_headers()) as socket:
        reader = FrameReader(socket)
        socket.send_json({"type": "resize", "cols": 101, "rows": 33})
        # The PTY echoes what is typed, so look for the shell's arithmetic, not the typed text.
        socket.send_json({"type": "input", "data": "echo cryopilot-$((20+3)); stty size; pwd\n"})
        reader.wait_for(lambda: "cryopilot-23" in reader.output and "33 101" in reader.output)
        reader.wait_for(lambda: project["slug"] in reader.output.split("cryopilot-23", 1)[1])

    # The shell outlives the connection; a new connection replays the scrollback.
    assert next(t for t in client.get("/api/terminals").json()["terminals"] if t["id"] == view["id"])["alive"] is True
    with client.websocket_connect(url, headers=auth_headers()) as socket:
        reader = FrameReader(socket)
        reader.wait_for(lambda: "cryopilot-23" in reader.output)
        socket.send_json({"type": "input", "data": "exit 7\n"})
        reader.wait_for(lambda: reader.exit_code is not None)
        assert reader.exit_code == 7

    listed = next(t for t in client.get("/api/terminals").json()["terminals"] if t["id"] == view["id"])
    assert listed["alive"] is False and listed["exit_code"] == 7
    # An exited terminal still shows its history, then says how it ended.
    with client.websocket_connect(url, headers=auth_headers()) as socket:
        reader = FrameReader(socket)
        reader.wait_for(lambda: reader.exit_code is not None)
        assert "cryopilot-23" in reader.output and reader.exit_code == 7
    assert client.delete(f"/api/terminals/{view['id']}").json() == {"ok": True}
    assert view["id"] not in {entry["id"] for entry in client.get("/api/terminals").json()["terminals"]}
    assert client.delete(f"/api/terminals/{view['id']}").status_code == 404


def test_terminal_socket_rejects_unknown_ids_and_missing_tokens(client, plain_shell):
    with client.websocket_connect("/api/terminals/doesnotexist/ws", headers=auth_headers()) as socket:
        with pytest.raises(WebSocketDisconnect) as info:
            socket.receive_json()
    assert info.value.code == routes_terminal.CLOSE_NOT_FOUND

    if auth.AUTH_ENABLED:
        created = client.post("/api/terminals", json={}).json()
        assert created["title"] == "Home" and created["cwd"] == str(Path.home())
        anonymous = TestClient(app)
        with pytest.raises(WebSocketDisconnect), anonymous.websocket_connect(f"/api/terminals/{created['id']}/ws") as socket:
            socket.receive_json()
        client.delete(f"/api/terminals/{created['id']}")


def test_terminal_limit_and_close_kills_the_shell(client, project, plain_shell, monkeypatch):
    monkeypatch.setattr(terminal, "MAX_TERMINALS", 2)
    first = client.post("/api/terminals", json={"project_id": project["id"]}).json()
    second = client.post("/api/terminals", json={"project_id": project["id"], "root": "files"}).json()
    assert second["title"] == "Uploads"
    third = client.post("/api/terminals", json={"project_id": project["id"]})
    assert third.status_code == 409
    shell = terminal.manager.get(first["id"]).process
    assert client.delete(f"/api/terminals/{first['id']}").status_code == 200
    assert shell.poll() is not None
    client.delete(f"/api/terminals/{second['id']}")
    bad_root = client.post("/api/terminals", json={"project_id": project["id"], "root": "nowhere"})
    assert bad_root.status_code == 400


def test_scrollback_is_bounded_and_slow_pages_are_dropped(monkeypatch):
    import asyncio

    monkeypatch.setattr(terminal, "SCROLLBACK_CHARS", 64)
    monkeypatch.setattr(terminal, "SUBSCRIBER_BACKLOG_CHARS", 100)
    shell = terminal.Terminal(cwd=Path.home(), title="Home")
    shell.alive = True
    loop = asyncio.new_event_loop()
    try:
        subscriber, backlog = shell.subscribe(loop)
        assert backlog == ""
        for index in range(10):
            shell._emit(f"line {index:02d} \x1b[1mbold\x1b[0m\r\n")
        _, replay = shell.subscribe(loop)
        assert len(replay) <= 64 and replay.startswith("line ") and replay.endswith("line 09 \x1b[1mbold\x1b[0m\r\n")
        loop.run_until_complete(asyncio.sleep(0))
        kinds = []
        while not subscriber.queue.empty():
            kinds.append(subscriber.queue.get_nowait()[0])
        assert kinds[-1] == "overflow" and kinds.count("overflow") == 1 and subscriber.closed
    finally:
        loop.close()



def test_terminal_input_never_reaches_a_closed_descriptor(client, project, plain_shell):
    shell = terminal.manager.create(cwd=workspace_of(project), title="Race")
    try:
        fd = shell._master
        assert fd is not None and os.get_blocking(fd) is False
        # A raw-mode PTY whose program does not read fills up, so a large paste waits for room.
        shell.write("stty raw -echo; sleep 30\n")
        time.sleep(0.5)
        writer = threading.Thread(target=shell.write, args=("x" * (1024 * 1024),), daemon=True)
        writer.start()
        writer.join(1.0)
        assert writer.is_alive()
    finally:
        terminal.manager.close(shell.id)
    # Closing the terminal ends the paste instead of leaving it blocked on the old descriptor.
    writer.join(5)
    assert not writer.is_alive()
    assert shell._master is None and shell.alive is False
    shell.write("more input\n")
    shell.resize(100, 40)
    assert (shell.cols, shell.rows) == (100, 40)
