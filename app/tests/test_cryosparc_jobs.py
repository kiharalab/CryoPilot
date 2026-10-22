"""Tests for CryoSPARC jobs, maps, map previews, project maps, and modeling attachments.

The job documents in ``fixtures/cryosparc`` are trimmed copies of a real
CryoSPARC v5.0.2 project (P61, HIV-1 Env). Tests build a fake project folder
from them with tiny MRC files, and stand in for the CryoSPARC API with a fake
cryosparc-tools client.

Run from the repository root:
    .venv/bin/python -m pytest app/tests/test_cryosparc_jobs.py -q
"""

from __future__ import annotations

import atexit
import copy
import importlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cryosparc"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Keep the user's ~/.cryoagent untouched: point the app at a scratch home
# before the server modules create their database.
_SCRATCH = Path(tempfile.mkdtemp(prefix="cryopilot-tests-"))
atexit.register(shutil.rmtree, _SCRATCH, ignore_errors=True)
os.environ.setdefault("CRYOAGENT_APP_HOME", str(_SCRATCH / "app"))
os.environ.setdefault("CRYOAGENT_SESSION_DB", str(_SCRATCH / "sessions.sqlite3"))
if "app.server.config" in sys.modules and "app.server.state" not in sys.modules:
    importlib.reload(sys.modules["app.server.config"])

import mrcfile  # noqa: E402

from app.server import config  # noqa: E402

if config.APP_HOME == Path("~/.cryoagent/app").expanduser():
    pytest.skip("the app home is the user's real one; run with CRYOAGENT_APP_HOME set", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.server import attachments, auth, cryosparc_bridge, cryosparc_jobs, files as file_roots, volumes  # noqa: E402
from app.server.main import app  # noqa: E402
from app.server.state import store  # noqa: E402

PROJECT_UID = "P61"
CREDENTIALS = ("http://localhost:61000", "agent@example.org", "test-password")
HEADERS = {"Authorization": f"Bearer {auth.TOKEN}"}


# ----------------------------------------------------------------- helpers
def fixture_doc(uid: str) -> dict:
    return json.loads((FIXTURES / f"{uid}.json").read_text())


def api_doc(doc: dict) -> dict:
    """What ``job.model.model_dump(mode="json")`` returns: plain ISO timestamps."""

    def convert(value):
        if isinstance(value, dict):
            if set(value) == {"$date"}:
                return value["$date"].replace("Z", "+00:00")
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return convert(copy.deepcopy(doc))


def map_data(shape=(18, 20, 22)) -> np.ndarray:
    return np.arange(np.prod(shape), dtype=np.float32).reshape(shape)


def write_map(path: Path, shape=(18, 20, 22), voxel=1.245) -> np.ndarray:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = map_data(shape)
    with mrcfile.new(str(path), overwrite=True) as mrc:
        mrc.set_data(data)
        mrc.voxel_size = voxel
    return data


def cs_array(result: str, mrc_path: str, shape=(22, 20, 18), psize=1.245) -> np.ndarray:
    dtype = [("uid", "<u8"), (f"{result}/path", "S128"), (f"{result}/shape", "<u4", (3,)), (f"{result}/psize_A", "<f4")]
    array = np.zeros(1, dtype=dtype)
    array[0] = (11254673506506741426, mrc_path.encode(), shape, psize)
    return array


def write_cs(path: Path, result: str, mrc_path: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        np.save(handle, cs_array(result, mrc_path))


# The maps written into the fake project folder: (metafile, result, map file).
PROJECT_MAPS = [
    ("J29/J29_009_volume_map_sharp.cs", "map_sharp", "J29/J29_009_volume_map_sharp.mrc"),
    ("J29/J29_009_volume_map.cs", "map", "J29/J29_009_volume_map.mrc"),
    ("J31/J31_map_half_A.cs", "map_half_A", "J31/J31_map_half_A.mrc"),
    ("J32/J32_map_sharp.cs", "map_sharp", "J32/J32_map_sharp.mrc"),
    ("J34/J34_localfilter.cs", "map_filtered", "J34/J34_localfilter.mrc"),
]


def build_project(root: Path, harness_id: str) -> Path:
    """A CryoSPARC project folder like P61, plus a decoy copy with the same uid."""

    parent = root / "cryosparc"
    project_dir = parent / "CS-revitrified-hiv-1-env"
    for fixture in FIXTURES.glob("J*.json"):
        job_dir = project_dir / fixture.stem
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text(fixture.read_text())
    info = json.loads((FIXTURES / "project.json").read_text())
    info.update({"project_dir": str(project_dir), "description": f"cryoagent-session:{harness_id}"})
    (project_dir / "project.json").write_text(json.dumps(info))
    decoy = parent / "CS-older-copy"
    decoy.mkdir(parents=True)
    (decoy / "project.json").write_text(json.dumps({**info, "project_dir": str(decoy), "description": "cryoagent-session:other"}))
    for metafile, result, mrc_path in PROJECT_MAPS:
        write_cs(project_dir / metafile, result, mrc_path)
        write_map(project_dir / mrc_path)
    return project_dir


class NotFoundError(Exception):
    code = 404


class FakeClient:
    """The parts of ``cryosparc.tools.CryoSPARC`` the bridge uses."""

    def __init__(self, project_dir: str, files: dict[str, bytes] | None = None, assets=None):
        self.project_dir = project_dir
        self.docs = {fixture.stem: api_doc(fixture_doc(fixture.stem)) for fixture in FIXTURES.glob("J*.json")}
        self.files = files or {}
        self.datasets = {metafile: cs_array(result, mrc_path) for metafile, result, mrc_path in PROJECT_MAPS}
        self.assets = assets or {}
        self.calls: list[tuple] = []
        self.fail_listing: Exception | None = None

    def find_project(self, project_uid):
        self.calls.append(("find_project", project_uid))
        return SimpleNamespace(dir=PurePosixPath(self.project_dir))

    def find_jobs(self, project_uid=None, workspace_uid=None, *, order=1):
        self.calls.append(("find_jobs", project_uid, workspace_uid, order))
        if self.fail_listing is not None:
            raise self.fail_listing
        return [SimpleNamespace(model=SimpleNamespace(model_dump=lambda mode="python", doc=doc: copy.deepcopy(doc))) for doc in self.docs.values()]

    def find_job(self, project_uid, job_uid):
        self.calls.append(("find_job", project_uid, job_uid))
        if job_uid not in self.docs:
            raise NotFoundError(f"Job {job_uid} not found")
        doc = self.docs[job_uid]
        return SimpleNamespace(model=SimpleNamespace(model_dump=lambda mode="python": copy.deepcopy(doc)))

    def download_dataset(self, project_uid, path):
        self.calls.append(("download_dataset", project_uid, str(path)))
        return self.datasets[str(path)]

    def download_file(self, project_uid, path, target):
        self.calls.append(("download_file", project_uid, str(path)))
        Path(target).write_bytes(self.files[str(path)])
        return target

    def list_assets(self, project_uid, job_uid):
        self.calls.append(("list_assets", project_uid, job_uid))
        return [SimpleNamespace(**asset) for asset in self.assets.get(job_uid, [])]

    def download_asset(self, fileid, target):
        self.calls.append(("download_asset", fileid))
        Path(target).write_bytes(f"image {fileid}".encode())
        return target


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A project with one reconstruction session bound to the fake P61 folder, signed out."""

    for cache in (
        cryosparc_jobs._dir_cache,
        cryosparc_jobs._parsed_jobs,
        cryosparc_jobs._cs_paths,
        cryosparc_jobs._remote_paths,
        cryosparc_jobs._asset_index,
        cryosparc_bridge._clients,
        cryosparc_bridge._failures,
        cryosparc_bridge._sign_in_locks,
    ):
        cache.clear()
    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(file_roots, "MODELING_SANDBOX_ROOT", sandbox)
    monkeypatch.setattr(file_roots, "MODELING_WORKSPACES_DIR", sandbox / "workspaces")
    monkeypatch.setattr(cryosparc_jobs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(volumes, "PREVIEW_DIR", tmp_path / "previews")
    monkeypatch.setattr(cryosparc_jobs, "cryosparc_credentials", lambda required=True: None)

    harness_id = uuid.uuid4().hex
    project_dir = build_project(tmp_path, harness_id)
    project = store.create_project(title=f"Env {uuid.uuid4().hex[:6]}", parent_directory=str(project_dir.parent))
    file_roots.ensure_project_dirs(project["slug"])
    session = store.create_session(
        project_id=project["id"],
        kind="reconstruction",
        title="HIV-1 Env reconstruction",
        harness_session_id=harness_id,
        config={
            "project_uid": PROJECT_UID,
            "workspace_uid": "W1",
            "agent": {"cryosparc_url": "http://localhost:61000"},
            "manifest": {"project": {"title": "Revitrified HIV-1 Env", "parent_directory": str(project_dir.parent)}},
        },
        status="stopped",
    )
    return SimpleNamespace(
        tmp=tmp_path,
        project=project,
        session=session,
        project_dir=project_dir,
        workspace=sandbox / "workspaces" / project["slug"],
        client=TestClient(app),
    )


def sign_in(monkeypatch, client: FakeClient) -> None:
    monkeypatch.setattr(cryosparc_jobs, "cryosparc_credentials", lambda required=True: CREDENTIALS)
    monkeypatch.setattr(cryosparc_bridge, "_client", lambda url, email, password: client)


def get(env, path, **params):
    return env.client.get(path, params=params, headers=HEADERS)


# ----------------------------------------------------------------- parsing
def test_parse_refinement_job_lists_its_maps_sharpened_first():
    job = cryosparc_jobs.parse_job(fixture_doc("J29"))
    assert job["uid"] == "J29"
    assert job["type"] == "nonuniform_refine_new"
    assert job["type_title"] == "Non-uniform Refinement"
    assert job["status"] == "completed"
    assert job["completed_at"] == "2026-10-14T11:18:35.881000+00:00"
    assert job["resolution_A"] == pytest.approx(3.0895, abs=1e-4)
    names = [volume["name"] for volume in job["volumes"]]
    assert names == ["map_sharp", "map", "map_half_A", "map_half_B", "mask_refine", "mask_fsc", "mask_fsc_auto", "precision"]
    by_name = {volume["name"]: volume for volume in job["volumes"]}
    assert by_name["map"]["label"] == "Unsharpened map"
    assert by_name["map_sharp"]["label"] == "Sharpened map"
    assert by_name["map_half_B"]["kind"] == "half_map"
    assert by_name["mask_fsc"]["label"] == "Mask (FSC)"
    assert by_name["precision"]["kind"] == "other"
    sharp = by_name["map_sharp"]
    assert sharp["metafile"] == "J29/J29_009_volume_map_sharp.cs"
    assert sharp["path"] == "J29/J29_009_volume_map_sharp.mrc"
    assert sharp["version"] == 9
    assert sharp["psize_A"] == 1.245
    assert sharp["shape"] == [320, 320, 320]
    assert sharp["size_bytes"] == 131073024
    assert by_name["precision"]["size_bytes"] == 65946624


def test_parse_class_volumes_get_unique_names_and_per_class_resolution():
    hetero = cryosparc_jobs.parse_job(fixture_doc("J11"))
    names = [volume["name"] for volume in hetero["volumes"]]
    assert "volume_class_0.map_sharp" in names and "volume_class_3.map" in names
    assert len(names) == len(set(names)) == 8
    first = next(volume for volume in hetero["volumes"] if volume["name"] == "volume_class_0.map_sharp")
    assert first["label"] == "Class 0 sharpened map"
    assert first["resolution_A"] == pytest.approx(10.143, abs=1e-3)
    assert hetero["resolution_A"] == min(volume["resolution_A"] for volume in hetero["volumes"])

    abinit = cryosparc_jobs.parse_job(fixture_doc("J9"))
    # The multi-volume series output is skipped; each class has its own map.
    assert [volume["name"] for volume in abinit["volumes"]] == [f"volume_class_{n}.map" for n in range(4)]
    assert abinit["volumes"][2]["label"] == "Class 2 map"
    assert abinit["resolution_A"] is None


def test_parse_post_processing_jobs():
    validation = cryosparc_jobs.parse_job(fixture_doc("J31"))
    assert validation["type_title"] == "Validation (FSC)"
    assert validation["resolution_A"] == pytest.approx(3.0983, abs=1e-4)
    assert {volume["kind"] for volume in validation["volumes"]} == {"half_map"}

    sharpen = cryosparc_jobs.parse_job(fixture_doc("J32"))
    assert sharpen["volumes"][0]["b_factor"] == pytest.approx(-117.765, abs=1e-3)
    assert sharpen["resolution_A"] is None

    local_filter = cryosparc_jobs.parse_job(fixture_doc("J34"))
    filtered = local_filter["volumes"][0]
    assert (filtered["name"], filtered["kind"], filtered["label"]) == ("map_filtered", "map_sharp", "Locally filtered map")
    assert filtered["path"] == "J34/J34_localfilter.mrc"

    diagnostics = cryosparc_jobs.parse_job(fixture_doc("J19"))
    assert diagnostics["volumes"][0]["label"] == "3D FSC map"
    assert diagnostics["volumes"][0]["kind"] == "other"
    assert cryosparc_jobs.parse_job(fixture_doc("J7"))["volumes"] == []


def test_parse_api_model_dump_matches_job_json():
    from_disk = cryosparc_jobs.parse_job(fixture_doc("J29"))
    from_api = cryosparc_jobs.parse_job(api_doc(fixture_doc("J29")))
    assert from_api == from_disk
    model = api_doc(fixture_doc("J29"))
    model["completed_at"] = datetime(2026, 10, 14, 11, 18, 35, 881000, tzinfo=timezone.utc)
    assert cryosparc_jobs.parse_job(model)["completed_at"] == from_disk["completed_at"]


def test_parse_cryosparc_v4_layout():
    doc = {
        "uid": "J12",
        "project_uid": "P3",
        "job_type": "homo_refine_new",
        "title": "Homogeneous refinement",
        "status": "completed",
        "completed_at": {"$date": {"$numberLong": "1760440000000"}},
        "workspace_uids": ["W1"],
        "output_result_groups": [
            {
                "type": "volume",
                "name": "volume",
                "title": "Refined volume",
                "summary": {"map/psize_A": 0.83, "map/shape": [256, 256, 256]},
                "latest_summary_stats": {"fsc_info_best": {"radwn_final_A": 2.41}},
            },
            {"type": "particle", "name": "particles", "title": "Particles"},
        ],
        "output_results": [
            {"group_name": "volume", "name": "map", "type": "volume.blob", "metafiles": ["J12/J12_004_volume_map.cs"], "versions": [4], "passthrough": False},
            {"group_name": "volume", "name": "map_sharp", "type": "volume.blob", "metafiles": ["J12/J12_004_volume_map_sharp.cs"], "versions": [4], "passthrough": False},
            {"group_name": "volume", "name": "mask_refine", "type": "volume.blob", "metafiles": ["J10/J10_mask.cs"], "versions": [0], "passthrough": True},
            {"group_name": "particles", "name": "blob", "type": "particle.blob", "metafiles": ["J12/J12_004_particles.cs"], "versions": [4]},
        ],
    }
    job = cryosparc_jobs.parse_job(doc)
    assert job["type_title"] == "Homogeneous Refinement"
    assert job["completed_at"] == "2025-10-14T11:06:40+00:00"
    assert [volume["name"] for volume in job["volumes"]] == ["map_sharp", "map"]
    assert job["volumes"][0]["resolution_A"] == 2.41
    assert job["volumes"][0]["psize_A"] == 0.83


# ------------------------------------------------------------- job listing
def test_jobs_from_the_project_folder(env):
    response = get(env, f"/api/sessions/{env.session['id']}/cryosparc/jobs")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["source"] == "filesystem"
    assert data["project_uid"] == "P61" and data["workspace_uid"] == "W1"
    # The decoy folder has the same uid; the session id in the description picks the right one.
    assert data["project_dir"] == str(env.project_dir)
    assert [job["uid"] for job in data["jobs"]] == ["J34", "J33", "J32", "J31", "J29", "J19", "J11", "J9", "J7"]
    jobs = {job["uid"]: job for job in data["jobs"]}
    sharp = jobs["J29"]["volumes"][0]
    assert sharp["name"] == "map_sharp" and sharp["available"] is True
    assert sharp["size_bytes"] == (env.project_dir / "J29/J29_009_volume_map_sharp.mrc").stat().st_size
    assert {volume["name"]: volume["available"] for volume in jobs["J29"]["volumes"]}["precision"] is False
    # Post-processing jobs show the resolution of the refinement they came from.
    assert jobs["J32"]["resolution_A"] == pytest.approx(3.0895, abs=1e-4)
    assert jobs["J32"]["resolution_from"] == "J29"
    assert jobs["J34"]["volumes"][0]["resolution_from"] == "J29"
    assert jobs["J31"]["resolution_from"] is None
    assert cryosparc_jobs.project_dir_for_session(env.session) == env.project_dir


def test_cs_metafile_decides_the_map_path(env):
    # Point J34's metafile at another file: the listing must follow it, not the name.
    write_map(env.project_dir / "J34/renamed_output.mrc", shape=(4, 4, 4))
    write_cs(env.project_dir / "J34/J34_localfilter.cs", "map_filtered", "J34/renamed_output.mrc")
    data = get(env, f"/api/sessions/{env.session['id']}/cryosparc/jobs").json()
    filtered = next(job for job in data["jobs"] if job["uid"] == "J34")["volumes"][0]
    assert filtered["path"] == "J34/renamed_output.mrc"
    assert filtered["size_bytes"] == (env.project_dir / "J34/renamed_output.mrc").stat().st_size
    # A metafile that points outside the project folder is refused.
    write_cs(env.project_dir / "J34/J34_localfilter.cs", "map_filtered", "../../outside.mrc")
    cryosparc_jobs._cs_paths.clear()
    response = get(env, f"/api/sessions/{env.session['id']}/cryosparc/volume", job="J34", name="map_filtered", preview=0)
    assert response.status_code == 404


def test_unbound_and_unreadable_sessions(env):
    unbound = store.create_session(project_id=env.project["id"], kind="reconstruction", title="Starting", config={"manifest": {}}, status="starting")
    data = get(env, f"/api/sessions/{unbound['id']}/cryosparc/jobs").json()
    assert data["project_uid"] is None and data["jobs"] == [] and data["reason"]
    assert get(env, f"/api/sessions/{unbound['id']}/cryosparc/volume", job="J29", name="map").status_code == 409

    elsewhere = store.create_session(
        project_id=env.project["id"],
        kind="reconstruction",
        title="Remote",
        config={"project_uid": "P99", "workspace_uid": "W1", "manifest": {"project": {"parent_directory": str(env.tmp / "missing")}}},
        status="stopped",
    )
    response = get(env, f"/api/sessions/{elsewhere['id']}/cryosparc/jobs")
    assert response.status_code == 400
    assert response.json()["setup"] == "cryosparc"

    modeling = store.create_session(project_id=env.project["id"], kind="modeling", title="Model", config={}, status="stopped")
    assert get(env, f"/api/sessions/{modeling['id']}/cryosparc/jobs").status_code == 400
    assert get(env, "/api/sessions/nope/cryosparc/jobs").status_code == 404


# -------------------------------------------------------------------- maps
def test_volume_full_resolution_and_preview(env, monkeypatch):
    monkeypatch.setattr(volumes, "MAX_PREVIEW_SIZE", 8)
    url = f"/api/sessions/{env.session['id']}/cryosparc/volume"
    source = env.project_dir / "J29/J29_009_volume_map_sharp.mrc"

    full = get(env, url, job="J29", name="map_sharp", preview=0)
    assert full.status_code == 200
    assert full.content == source.read_bytes()
    assert 'filename="P61_J29_map_sharp.mrc"' in full.headers["content-disposition"]

    response = get(env, url, job="J29", name="map_sharp", preview=1)
    assert response.status_code == 200, response.text
    assert 'filename="P61_J29_map_sharp_preview.mrc"' in response.headers["content-disposition"]
    preview_path = env.tmp / "downloaded_preview.mrc"
    preview_path.write_bytes(response.content)
    data = map_data()
    with mrcfile.open(str(preview_path)) as mrc:
        # Longest side 22 -> factor 3 -> (18, 20, 22) // 3 sections, rows, columns.
        assert mrc.data.shape == (6, 6, 7)
        assert mrc.data[1, 2, 3] == pytest.approx(data[3:6, 6:9, 9:12].mean())
        assert mrc.voxel_size.x == pytest.approx(1.245 * 3, abs=1e-5)
        assert float(mrc.header.cella.x) == pytest.approx(7 * 1.245 * 3, abs=1e-4)
        assert float(mrc.header.origin.x) == pytest.approx(1.245, abs=1e-5)
    stats = json.loads(response.headers["x-map-stats"])
    assert stats["bin_factor"] == 3
    assert stats["min"] == 0 and stats["max"] == pytest.approx(data.max())
    assert stats["mean"] == pytest.approx(data.mean(), rel=1e-6)
    assert stats["rms"] == pytest.approx(data.std(), rel=1e-6)

    # A second request is served from the cache, and the listing now carries the stats.
    previews = sorted(volumes.PREVIEW_DIR.glob("*.mrc"))
    assert get(env, url, job="J29", name="map_sharp").content == response.content
    assert sorted(volumes.PREVIEW_DIR.glob("*.mrc")) == previews
    jobs = get(env, f"/api/sessions/{env.session['id']}/cryosparc/jobs").json()["jobs"]
    listed = next(job for job in jobs if job["uid"] == "J29")["volumes"][0]
    assert listed["stats"]["max"] == pytest.approx(data.max())


def test_small_maps_are_served_as_they_are(env):
    source = env.project_dir / "J32/J32_map_sharp.mrc"
    response = get(env, f"/api/sessions/{env.session['id']}/cryosparc/volume", job="J32", name="map_sharp")
    assert response.status_code == 200
    assert response.content == source.read_bytes()
    assert json.loads(response.headers["x-map-stats"])["bin_factor"] == 1


def test_volume_errors(env):
    url = f"/api/sessions/{env.session['id']}/cryosparc/volume"
    assert get(env, url, job="J29", name="nonexistent").status_code == 404
    assert get(env, url, job="J999", name="map").status_code == 404
    assert get(env, url, job="../J29", name="map").status_code == 400
    missing = get(env, url, job="J29", name="precision")
    assert missing.status_code == 404
    assert "no longer in the project folder" in missing.json()["error"]


def test_bin_factor():
    assert volumes.bin_factor([320, 320, 320]) == 2
    assert volumes.bin_factor([256, 256, 256]) == 1
    assert volumes.bin_factor([600, 600, 600]) == 3
    assert volumes.bin_factor([22, 20, 18], max_size=8) == 3


# ------------------------------------------------------------ project maps
def test_project_maps_newest_first_and_all_outputs(env):
    unbound = store.create_session(project_id=env.project["id"], kind="reconstruction", title="Starting", config={}, status="starting")
    broken = store.create_session(
        project_id=env.project["id"],
        kind="reconstruction",
        title="Moved project",
        config={"project_uid": "P99", "workspace_uid": "W1", "manifest": {"project": {"parent_directory": str(env.tmp / "gone")}}},
        status="stopped",
    )
    response = get(env, f"/api/projects/{env.project['id']}/maps")
    assert response.status_code == 200
    data = response.json()
    assert [(item["job_uid"], item["name"]) for item in data["maps"][:4]] == [
        ("J34", "map_filtered"),
        ("J32", "map_sharp"),
        ("J29", "map_sharp"),
        ("J29", "map"),
    ]
    assert {item["kind"] for item in data["maps"]} <= {"map", "map_sharp"}
    first = data["maps"][0]
    assert first["session_id"] == env.session["id"]
    assert first["session_title"] == "HIV-1 Env reconstruction"
    assert first["job_type"] == "local_filter" and first["job_title"].startswith("Local Filtering")
    assert first["completed_at"] == "2026-10-14T11:27:33.818000+00:00"
    assert first["resolution_A"] == pytest.approx(3.0895, abs=1e-4)
    assert [error["session_id"] for error in data["errors"]] == [broken["id"]]
    assert data["errors"][0]["setup"] == "cryosparc"
    assert unbound["id"] not in {item["session_id"] for item in data["maps"]}

    everything = get(env, f"/api/projects/{env.project['id']}/maps", all=1).json()["maps"]
    assert {"half_map", "mask", "other"} <= {item["kind"] for item in everything}
    assert len(everything) > len(data["maps"])


# ----------------------------------------------------------------- the API
def test_api_source_with_readable_project_folder(env, monkeypatch):
    fake = FakeClient(str(env.project_dir))
    sign_in(monkeypatch, fake)
    data = get(env, f"/api/sessions/{env.session['id']}/cryosparc/jobs").json()
    assert data["source"] == "api"
    assert data["cryosparc_url"] == CREDENTIALS[0]
    assert ("find_jobs", "P61", "W1", -1) in fake.calls
    sharp = next(job for job in data["jobs"] if job["uid"] == "J29")["volumes"][0]
    assert sharp["size_bytes"] == (env.project_dir / "J29/J29_009_volume_map_sharp.mrc").stat().st_size

    response = get(env, f"/api/sessions/{env.session['id']}/cryosparc/volume", job="J29", name="map_sharp", preview=0)
    assert response.content == (env.project_dir / "J29/J29_009_volume_map_sharp.mrc").read_bytes()
    assert not [call for call in fake.calls if call[0] == "download_file"]


def test_api_source_downloads_maps_into_the_cache(env, monkeypatch):
    remote = env.tmp / "remote.mrc"
    write_map(remote, shape=(6, 6, 6))
    fake = FakeClient("/net/elsewhere/CS-revitrified-hiv-1-env", files={"J29/J29_009_volume_map_sharp.mrc": remote.read_bytes()})
    sign_in(monkeypatch, fake)
    session = store.create_session(
        project_id=env.project["id"],
        kind="reconstruction",
        title="On another computer",
        config={"project_uid": "P61", "workspace_uid": "W1", "manifest": {"project": {"parent_directory": "/net/elsewhere"}}},
        status="stopped",
    )
    data = get(env, f"/api/sessions/{session['id']}/cryosparc/jobs").json()
    assert data["source"] == "api" and data["project_dir_readable"] is False
    assert data["project_dir"] == "/net/elsewhere/CS-revitrified-hiv-1-env"
    sharp = next(job for job in data["jobs"] if job["uid"] == "J29")["volumes"][0]
    assert sharp["path"] == "J29/J29_009_volume_map_sharp.mrc" and sharp["size_bytes"] == 131073024

    url = f"/api/sessions/{session['id']}/cryosparc/volume"
    for _ in range(2):
        response = get(env, url, job="J29", name="map_sharp", preview=0)
        assert response.status_code == 200, response.text
        assert response.content == remote.read_bytes()
    assert [call for call in fake.calls if call[0] == "download_file"] == [("download_file", "P61", "J29/J29_009_volume_map_sharp.mrc")]
    assert ("download_dataset", "P61", "J29/J29_009_volume_map_sharp.cs") in fake.calls
    cached = list((cryosparc_jobs.CACHE_DIR / "maps").rglob("*.mrc"))
    assert [path.name for path in cached] == ["J29_009_volume_map_sharp.mrc"]


def test_api_failure_falls_back_to_the_project_folder(env, monkeypatch):
    fake = FakeClient(str(env.project_dir))
    fake.fail_listing = RuntimeError("Could not connect to CryoSPARC at http://localhost:61000\nconnection refused")
    sign_in(monkeypatch, fake)
    data = get(env, f"/api/sessions/{env.session['id']}/cryosparc/jobs").json()
    assert data["source"] == "filesystem"
    assert "Could not connect to CryoSPARC" in data["warning"]


def test_credentials_for_another_cryosparc_are_not_used(env, monkeypatch):
    fake = FakeClient(str(env.project_dir))
    sign_in(monkeypatch, fake)
    config = dict(env.session["config"], agent={"cryosparc_url": "http://other-host:39000"})
    session = store.update_session(env.session["id"], config=config)
    data = get(env, f"/api/sessions/{session['id']}/cryosparc/jobs").json()
    assert data["source"] == "filesystem"
    reason = get(env, f"/api/sessions/{session['id']}/cryosparc/jobs/J29/assets").json()["reason"]
    assert reason.startswith("This session used the CryoSPARC at http://other-host:39000.")
    assert fake.calls == []


def test_bridge_reconnects_once_when_the_session_expired(monkeypatch):
    import httpx
    from cryosparc.errors import APIError

    expired = APIError("token expired", res=httpx.Response(401, request=httpx.Request("GET", "http://localhost:61000/api")))
    created = []

    def factory(url, email, password):
        client = FakeClient("/p")
        if not created:
            def fail(*args, **kwargs):
                raise expired
            client.find_project = fail
        created.append(client)
        return client

    monkeypatch.setattr(cryosparc_bridge, "_client", factory)
    assert cryosparc_bridge.project_directory(CREDENTIALS, "P61") == "/p"
    assert len(created) == 2


@pytest.fixture
def bridge(monkeypatch):
    """The bridge's client cache, empty, with sign-ins handled by ``bridge.connect``."""

    for name in ("_clients", "_failures", "_sign_in_locks"):
        monkeypatch.setattr(cryosparc_bridge, name, {})
    state = SimpleNamespace(signed_in=[], connect=lambda url, email, password: FakeClient("/p"))

    def connect(url, email, password):
        state.signed_in.append((email, password))
        return state.connect(url, email, password)

    monkeypatch.setattr(cryosparc_bridge, "_connect", connect)
    return state


def test_bridge_signs_in_once_and_never_under_the_shared_lock(bridge):
    import threading

    release = threading.Event()
    slow_started = threading.Event()

    def connect(url, email, password):
        if email == "slow@example.org":
            slow_started.set()
            assert release.wait(10)
        return FakeClient("/p")

    bridge.connect = connect
    slow = ("http://localhost:61000", "slow@example.org", "pw")
    results = []
    threads = [threading.Thread(target=lambda: results.append(cryosparc_bridge._client(*slow))) for _ in range(4)]
    for thread in threads:
        thread.start()
    assert slow_started.wait(10)
    # Another account signs in while the first sign-in is still waiting on the network.
    other = cryosparc_bridge._client("http://localhost:61000", "fast@example.org", "pw")
    assert isinstance(other, FakeClient)
    release.set()
    for thread in threads:
        thread.join(10)
    assert len(results) == 4 and all(client is results[0] for client in results)
    assert bridge.signed_in.count(("slow@example.org", "pw")) == 1


def test_bridge_remembers_a_failed_sign_in_for_a_while(bridge, monkeypatch):
    def unreachable(url, email, password):
        raise RuntimeError("Could not connect to CryoSPARC at http://localhost:61000")

    bridge.connect = unreachable
    with pytest.raises(RuntimeError, match="Could not connect"):
        cryosparc_bridge.project_directory(CREDENTIALS, "P61")
    with pytest.raises(cryosparc_bridge.SignInError, match="Could not connect"):
        cryosparc_bridge.project_directory(CREDENTIALS, "P61")
    assert len(bridge.signed_in) == 1

    # Testing the connection in Settings always tries again.
    bridge.connect = lambda url, email, password: SimpleNamespace(find_projects=lambda: ["P61"])
    assert cryosparc_bridge.test_connection(*CREDENTIALS) == {"ok": True, "project_count": 1}
    assert len(bridge.signed_in) == 2

    # Once the wait is over, a request signs in again by itself.
    cryosparc_bridge.forget(*CREDENTIALS)
    bridge.connect = unreachable
    with pytest.raises(RuntimeError):
        cryosparc_bridge._client(*CREDENTIALS)
    monkeypatch.setattr(cryosparc_bridge, "FAILED_SIGN_IN_SECONDS", 0)
    bridge.connect = lambda url, email, password: FakeClient("/p")
    assert cryosparc_bridge.project_directory(CREDENTIALS, "P61") == "/p"


def test_bridge_drops_clients_of_an_old_password(bridge):
    old = cryosparc_bridge._client("http://localhost:61000/", "agent@example.org", "old-password")
    new = cryosparc_bridge._client(*CREDENTIALS)
    assert old is not new
    assert list(cryosparc_bridge._clients.values()) == [new]
    assert cryosparc_bridge._client(*CREDENTIALS) is new and len(bridge.signed_in) == 2


def test_bridge_client_has_a_short_connect_timeout(monkeypatch):
    import cryosparc.tools

    seen = {}

    class Recorder:
        def __init__(self, base_url, **kwargs):
            seen.update(kwargs, base_url=base_url)

    monkeypatch.setattr(cryosparc.tools, "CryoSPARC", Recorder)
    cryosparc_bridge._connect("http://localhost:61000/", "agent@example.org", "pw")
    assert seen["base_url"] == "http://localhost:61000"
    assert seen["timeout"].connect == cryosparc_bridge.CONNECT_TIMEOUT_S and seen["timeout"].read == cryosparc_bridge.READ_TIMEOUT_S


# ------------------------------------------------------------------ assets
def test_assets_need_cryosparc_and_only_images_are_listed(env, monkeypatch):
    url = f"/api/sessions/{env.session['id']}/cryosparc/jobs/J29/assets"
    signed_out = get(env, url).json()
    assert signed_out["assets"] == [] and "Sign in to CryoSPARC" in signed_out["reason"]

    uploaded = datetime(2026, 10, 14, 11, 18, tzinfo=timezone.utc)
    fake = FakeClient(
        str(env.project_dir),
        assets={
            "J29": [
                {"id": "6acf6502a5b3d25f8643566f", "filename": "J29_fsc.png", "contentType": "image/png", "length": 2048, "uploadDate": uploaded},
                {"id": "6acf6502a5b3d25f86435670", "filename": "J29_fsc_iteration_009.json", "contentType": "application/json", "length": 512, "uploadDate": uploaded},
                {"id": "6acf6502a5b3d25f86435671", "filename": "J29_viewing_direction.svg", "contentType": "image/svg+xml", "length": 900, "uploadDate": uploaded},
            ],
            "J7": [{"id": "6ace83f290eb283471482821", "filename": "J7_classes.png", "contentType": "image/png", "length": 10, "uploadDate": uploaded}],
        },
    )
    sign_in(monkeypatch, fake)
    assets = get(env, url).json()["assets"]
    assert [asset["filename"] for asset in assets] == ["J29_viewing_direction.svg", "J29_fsc.png"]
    assert assets[1] == {
        "id": "6acf6502a5b3d25f8643566f",
        "filename": "J29_fsc.png",
        "kind": "image",
        "format": "png",
        "content_type": "image/png",
        "size": 2048,
        "uploaded_at": "2026-10-14T11:18:00+00:00",
    }

    image = get(env, f"{url}/6acf6502a5b3d25f8643566f")
    assert image.status_code == 200
    assert image.content == b"image 6acf6502a5b3d25f8643566f"
    assert image.headers["content-type"] == "image/png"
    assert "default-src 'none'" in image.headers["content-security-policy"]
    get(env, f"{url}/6acf6502a5b3d25f8643566f")
    assert len([call for call in fake.calls if call[0] == "download_asset"]) == 1

    # Ids that are not images of this job are refused.
    assert get(env, f"{url}/6acf6502a5b3d25f86435670").status_code == 404
    assert get(env, f"{url}/6ace83f290eb283471482821").status_code == 404
    assert get(env, f"{url}/not-an-id").status_code == 400


# ------------------------------------------------------------- attachments
def test_attaching_a_cryosparc_map_copies_it_into_the_workspace(env):
    slug = env.project["slug"]
    attachment = {"type": "cryosparc_map", "session_id": env.session["id"], "job_uid": "J29", "name": "map_sharp", "label": "J29 Sharpened map"}
    lines = attachments.materialize(env.project, [attachment])
    assert lines == [
        f"Density map: workspaces/{slug}/data/P61_J29_map_sharp.mrc "
        "(CryoSPARC P61 J29, Non-uniform Refinement, sharpened map, 3.09 Å GSFSC, 1.245 Å/pixel)"
    ]
    copy_path = env.workspace / "data" / "P61_J29_map_sharp.mrc"
    assert copy_path.is_file() and not copy_path.is_symlink()
    assert copy_path.stat().st_ino != (env.project_dir / "J29/J29_009_volume_map_sharp.mrc").stat().st_ino
    assert copy_path.read_bytes() == (env.project_dir / "J29/J29_009_volume_map_sharp.mrc").read_bytes()

    # An identical copy is not made twice.
    before = copy_path.stat().st_mtime_ns
    attachments.materialize(env.project, [attachment])
    assert copy_path.stat().st_mtime_ns == before

    sharpened = attachments.materialize(env.project, [{"type": "cryosparc_map", "session_id": env.session["id"], "job_uid": "J32", "name": "map_sharp"}])
    assert sharpened == [
        f"Density map: workspaces/{slug}/data/P61_J32_map_sharp.mrc "
        "(CryoSPARC P61 J32, Sharpening Tools, sharpened map, 3.09 Å GSFSC from J29, B-factor -117.8 Å², 1.245 Å/pixel)"
    ]


def test_attaching_project_files(env):
    slug = env.project["slug"]
    model = env.workspace / "outputs" / "model.pdb"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_text("ATOM\n")
    uploads = Path(file_roots.project_files_dir(slug))
    (uploads / "env.fasta").write_text(">A\nMRVKEK\n")
    (uploads / "notes.txt").write_text("notes")
    (env.workspace / "data" / "notes.txt").write_text("a different file with the same name")

    lines = attachments.materialize(
        env.project,
        [
            {"type": "file", "root": "modeling", "path": "outputs/model.pdb"},
            {"type": "file", "root": "files", "path": "env.fasta"},
            {"type": "file", "root": "files", "path": "notes.txt"},
            {"type": "file", "root": f"cryosparc:{env.session['id']}", "path": "J34/J34_localfilter.mrc"},
        ],
    )
    assert lines == [
        f"Model: workspaces/{slug}/outputs/model.pdb",
        f"Sequence (FASTA): workspaces/{slug}/data/env.fasta",
        f"File: workspaces/{slug}/data/notes-2.txt",
        f"Density map: workspaces/{slug}/data/P61_J34_localfilter.mrc",
    ]
    assert (env.workspace / "data" / "env.fasta").read_text() == ">A\nMRVKEK\n"
    assert (env.workspace / "data" / "notes.txt").read_text() == "a different file with the same name"
    assert (env.workspace / "data" / "notes-2.txt").read_text() == "notes"
    assert not (env.workspace / "outputs" / "model.pdb").is_symlink()


@pytest.mark.parametrize(
    ("attachment", "message"),
    [
        ({"type": "cryosparc_map", "session_id": "missing", "job_uid": "J29", "name": "map"}, "no longer exists"),
        ({"type": "cryosparc_map", "session_id": "SESSION", "job_uid": "J29", "name": "nonexistent"}, "has no map named"),
        ({"type": "cryosparc_map", "session_id": "SESSION", "job_uid": "J29"}, "needs session_id, job_uid, and name"),
        ({"type": "file", "root": "files", "path": "../../../etc/passwd"}, "escapes its root"),
        ({"type": "file", "root": "files", "path": "missing.pdb"}, "File not found"),
        ({"type": "file", "root": "SESSION_ROOT", "path": "../../../etc/passwd"}, "outside the CryoSPARC project folder"),
        ({"type": "link", "url": "https://example.org"}, "Unknown attachment type"),
    ],
)
def test_attachment_errors(env, attachment, message):
    attachment = {
        key: value.replace("SESSION_ROOT", f"cryosparc:{env.session['id']}").replace("SESSION", env.session["id"]) if isinstance(value, str) else value
        for key, value in attachment.items()
    }
    with pytest.raises(attachments.AttachmentError, match=message):
        attachments.materialize(env.project, [attachment])


def test_attaching_sequences_and_compressed_files_with_taken_names(env):
    slug = env.project["slug"]
    uploads = Path(file_roots.project_files_dir(slug))
    (uploads / "env.fna").write_text(">A\nATGC\n")
    (uploads / "emd.map.gz").write_bytes(b"new map")
    (env.workspace / "data" / "emd.map.gz").write_bytes(b"an older, different map")
    lines = attachments.materialize(
        env.project,
        [{"type": "file", "root": "files", "path": "env.fna"}, {"type": "file", "root": "files", "path": "emd.map.gz"}],
    )
    assert lines == [
        f"Sequence (FASTA): workspaces/{slug}/data/env.fna",
        f"Density map: workspaces/{slug}/data/emd-2.map.gz",
    ]
    assert (env.workspace / "data" / "emd-2.map.gz").read_bytes() == b"new map"


def test_attachments_from_another_project_are_refused(env):
    other = store.create_project(title=f"Other {uuid.uuid4().hex[:6]}")
    with pytest.raises(attachments.AttachmentError, match="does not belong"):
        attachments.materialize(other, [{"type": "cryosparc_map", "session_id": env.session["id"], "job_uid": "J29", "name": "map"}])
