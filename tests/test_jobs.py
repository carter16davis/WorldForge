"""The upload-to-placed-asset path, end to end, without RealityScan.

RealityScan is a Windows desktop application, so it cannot run in CI or on a
Linux dev machine. The `ExternalEngine` hook is what makes the rest of the
pipeline testable: these tests point it at a real script that consumes a photo
folder and writes a GLB, then assert the job around it does its job.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import trimesh
from PIL import Image, ImageDraw

from reconstruction import engine as engines
from reconstruction import jobs

BUILDING_W, BUILDING_H, BUILDING_L = 30.0, 12.0, 18.0   # glTF X, Y, Z


@pytest.fixture
def fake_engine_cmd(tmp_path):
    """A script that behaves like a photogrammetry tool: photos in, GLB out."""
    script = tmp_path / "fake_engine.py"
    script.write_text(textwrap.dedent(f"""
        import sys, pathlib, trimesh
        args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
        photos = pathlib.Path(args["--photos"])
        out = pathlib.Path(args["--output"])
        count = len([p for p in photos.iterdir() if p.is_file()])
        if count < 3:
            sys.exit("not enough photos")
        out.mkdir(parents=True, exist_ok=True)
        mesh = trimesh.creation.box(extents=[{BUILDING_W}, {BUILDING_H}, {BUILDING_L}])
        mesh.apply_translation([0, {BUILDING_H} / 2, 0])
        (out / "building.glb").write_bytes(trimesh.Scene(mesh).export(file_type="glb"))
    """))
    return f'{sys.executable} {script} --photos {{photos}} --output {{output}}'


@pytest.fixture
def photos(tmp_path):
    folder = tmp_path / "batch" / "source"
    folder.mkdir(parents=True)
    rng = np.random.default_rng(7)
    for i in range(12):
        # Distinct noise per frame: identical photos are exact duplicates, and
        # intake is right to collapse them, which would make this fixture lie.
        noise = rng.integers(0, 255, size=(240, 320, 3), dtype=np.uint8)
        img = Image.fromarray(noise)
        draw = ImageDraw.Draw(img)
        for x in range(i % 5, 320, 9):
            draw.line([(x, 0), (x, 240)], fill=(20, 20, 30), width=2)
        img.save(folder / f"photo_{i:03d}.png")
    return folder


def _store(tmp_path):
    return jobs.JobStore(tmp_path / "jobstore")


# ---------------------------------------------------------------------------
# Engine availability
# ---------------------------------------------------------------------------

def test_external_engine_reports_why_it_cannot_run(monkeypatch):
    monkeypatch.delenv("WORLDFORGE_RECONSTRUCTION_CMD", raising=False)
    status = engines.ExternalEngine().status()
    assert status.available is False
    assert "WORLDFORGE_RECONSTRUCTION_CMD" in status.detail

    assert engines.ExternalEngine("definitely-not-a-real-binary {photos} {output}").status().available is False
    missing = engines.ExternalEngine(f"{sys.executable} --version").status()
    assert missing.available is False
    assert "{photos}" in missing.detail


def test_external_engine_runs(fake_engine_cmd, photos, tmp_path):
    engine = engines.ExternalEngine(fake_engine_cmd)
    assert engine.status().available is True
    glb = engine.run(photos, tmp_path / "scan")
    assert glb.is_file()
    assert glb.read_bytes()[:4] == b"glTF"


def test_engine_failure_is_reported_not_swallowed(photos, tmp_path):
    """A reconstruction that fails must fail. Quietly showing the prepared asset
    and calling it a reconstruction is the bug this whole path exists to fix."""
    engine = engines.ExternalEngine(f"{sys.executable} -c pass {{photos}} {{output}}")
    with pytest.raises(ValueError, match="wrote no building.glb"):
        engine.run(photos, tmp_path / "scan")


def test_realityscan_status_is_honest_about_this_machine():
    status = engines.RealityScanEngine(executable="/nonexistent/RealityScan.exe").status()
    assert status.available is False
    assert "not found" in status.detail.lower()


def test_realityscan_meshes_at_full_detail_by_default(monkeypatch):
    """A Normal-detail mesh halves the images before it builds depth maps, and
    a 400k cap threw away most of what survived. Detail is the entire reason to
    run photogrammetry rather than extrude a footprint, so the defaults ask for
    it and the environment is what dials it back."""
    monkeypatch.delenv("WORLDFORGE_RECONSTRUCTION_TRIANGLES", raising=False)
    engine = engines.RealityScanEngine()
    assert engine.detail == "high"
    assert engine.triangles == engines.DEFAULT_TRIANGLES

    monkeypatch.setattr(engines.desktop, "desktop_path", str)
    commands = engines.oneshot_commands(
        Path("photos"), Path("out"), Path("preset.xml"), engine.triangles, engine.detail)

    assert "-calculateHighModel" in commands
    assert "-calculateNormalModel" not in commands
    # Decimation has to happen before the unwrap or the atlas is laid out for
    # a mesh that is not the one being shipped.
    assert commands.index("-simplify") < commands.index("-unwrap") < commands.index("-calculateTexture")
    assert commands[commands.index("-simplify") + 1] == str(engines.DEFAULT_TRIANGLES)


def test_the_triangle_budget_and_detail_are_configurable(monkeypatch):
    monkeypatch.setenv("WORLDFORGE_RECONSTRUCTION_TRIANGLES", "0")
    assert engines.default_triangles() is None          # keep every triangle

    monkeypatch.setattr(engines.desktop, "desktop_path", str)
    engine = engines.RealityScanEngine()
    commands = engines.oneshot_commands(
        Path("p"), Path("o"), Path("s"), engine.triangles, engine.detail)
    assert "-simplify" not in commands

    monkeypatch.setenv("WORLDFORGE_RECONSTRUCTION_TRIANGLES", "250000")
    assert engines.default_triangles() == 250_000

    assert engines.with_detail(engines.RealityScanEngine(), "normal").detail == "normal"
    with pytest.raises(ValueError, match="Unknown reconstruction detail"):
        engines.with_detail(engines.RealityScanEngine(), "ultra")
    # A backend with no such knob is asked and simply does not have one.
    external = engines.ExternalEngine("tool {photos} {output}")
    assert engines.with_detail(external, "high") is external


def test_too_few_photos_is_refused_before_launching_anything(tmp_path):
    thin = tmp_path / "thin"
    thin.mkdir()
    for i in range(3):
        Image.new("RGB", (64, 64)).save(thin / f"p{i}.png")
    with pytest.raises(ValueError, match="at least"):
        engines.RealityScanEngine(executable=sys.executable).run(thin, tmp_path / "out")


# ---------------------------------------------------------------------------
# The job pipeline
# ---------------------------------------------------------------------------

def test_job_produces_a_placed_anchored_asset(fake_engine_cmd, photos, tmp_path):
    store = _store(tmp_path)
    job = store.create(address="MetLife Stadium")
    assets = tmp_path / "assets"

    placement = jobs.run_job(
        store, job.id, source_dir=photos, address="MetLife Stadium",
        asset_root=assets, asset_id="scan-test",
        engine=engines.ExternalEngine(fake_engine_cmd),
        reference_meters=BUILDING_W,
    )

    # Located: the address was resolved and the asset carries real coordinates.
    assert placement["location"]["latitude"] == pytest.approx(40.8135, abs=0.01)
    assert placement["transform"]["upAxis"] == "Y"
    assert placement["transform"]["anchor"] == "ground-center"
    assert placement["transform"]["metersPerModelUnit"] == pytest.approx(1.0, rel=0.05)

    # Measured: dimensions come off the mesh, in metres.
    dims = placement["dimensions"]
    assert dims["widthMeters"] == pytest.approx(BUILDING_W, rel=0.05)
    assert dims["heightMeters"] == pytest.approx(BUILDING_H, rel=0.05)
    assert dims["lengthMeters"] == pytest.approx(BUILDING_L, rel=0.05)

    # Published where the viewer loads assets from.
    out = assets / "scan-test"
    assert (out / "building.glb").read_bytes()[:4] == b"glTF"
    for name in ("placement.json", "provenance.json", "coverage.json"):
        assert json.loads((out / name).read_text())

    # Grounded and centred.
    mesh = trimesh.load(out / "building.glb", force="mesh")
    low, high = mesh.bounds
    assert low[1] == pytest.approx(0.0, abs=0.05)
    assert (low[0] + high[0]) / 2 == pytest.approx(0.0, abs=0.2)

    finished = store.get(job.id)
    assert finished.progress == pytest.approx(1.0, abs=0.01)
    assert finished.stage == "publish"


def test_provenance_records_the_unattended_region_and_the_real_photos(
        fake_engine_cmd, photos, tmp_path):
    """An unattended run places the reconstruction region automatically, which is
    worse than a human doing it. The package has to say so."""
    store = _store(tmp_path)
    job = store.create()
    assets = tmp_path / "assets"
    jobs.run_job(store, job.id, source_dir=photos, address="MetLife Stadium",
                 asset_root=assets, asset_id="scan-test",
                 engine=engines.ExternalEngine(fake_engine_cmd))

    provenance = json.loads((assets / "scan-test" / "provenance.json").read_text())
    assert provenance["regionMode"] == "automatic"
    assert "hand-placed" in provenance["regionNote"]
    assert provenance["reconstructionTool"] == "External command"
    assert provenance["syntheticUse"] == "none"

    # Every uploaded photo is recorded with its hash, not just counted.
    media = provenance["sourceMedia"]
    assert len(media) == 12
    assert all(len(row["sha256"]) == 64 for row in media)
    assert provenance["anchorReport"]["steps"]


def test_coverage_reports_only_what_was_measured(fake_engine_cmd, photos, tmp_path):
    """Per-facade coverage needs camera poses the export does not include.
    Inventing four facade rows from a photo count is exactly the fabricated
    measurement this project exists to avoid."""
    store = _store(tmp_path)
    job = store.create()
    assets = tmp_path / "assets"
    jobs.run_job(store, job.id, source_dir=photos, address="MetLife Stadium",
                 asset_root=assets, asset_id="scan-test",
                 engine=engines.ExternalEngine(fake_engine_cmd))

    coverage = json.loads((assets / "scan-test" / "coverage.json").read_text())
    assert coverage["facades"] == []
    assert coverage["measured"]["perFacadeCoverage"] is None
    assert coverage["measured"]["uniqueImages"] == 12
    assert coverage["measured"]["fromPhotos"] == 12
    assert coverage["measured"]["fromVideo"] == 0
    assert any("20 or more" in r for r in coverage["recommendations"])
    assert any("camera poses" in r for r in coverage["recommendations"])


def test_unresolvable_address_fails_the_job_before_reconstructing(photos, tmp_path):
    """A mesh with nowhere to go is not map-ready, and burning ten minutes of
    photogrammetry to find that out afterwards is worse."""
    store = _store(tmp_path)
    job = store.create()

    class Exploding:
        name = "should not run"

        def run(self, *a, **k):
            raise AssertionError("reconstruction started despite no location")

    with patch("app.geocode.urllib.request.urlopen", side_effect=OSError("offline")), \
         pytest.raises(ValueError, match="did not resolve"):
        jobs.run_job(store, job.id, source_dir=photos,
                     address="zzz nowhere at all zzz",
                     asset_root=tmp_path / "assets", asset_id="scan-test",
                     engine=Exploding())


def test_store_survives_a_restart_and_does_not_leave_zombies(tmp_path):
    """A `running` job with no thread behind it would poll forever."""
    store = _store(tmp_path)
    job = store.create(address="somewhere")
    store.update(job.id, status="running")

    reopened = jobs.JobStore(tmp_path / "jobstore")
    revived = reopened.get(job.id)
    assert revived.status == "failed"
    assert "restarted" in revived.error


def test_progress_is_monotonic_across_stages(tmp_path):
    store = _store(tmp_path)
    job = store.create()
    seen = []
    for stage in jobs.STAGES:
        store.note(job.id, stage, "working", 0.0)
        seen.append(store.get(job.id).progress)
        store.note(job.id, stage, "done", 1.0)
        seen.append(store.get(job.id).progress)
    assert seen == sorted(seen)
    assert seen[-1] == pytest.approx(1.0, abs=0.001)


def test_log_does_not_grow_without_bound(tmp_path):
    store = _store(tmp_path)
    job = store.create()
    for i in range(500):
        store.note(job.id, "reconstruct", f"line {i}")
    log = store.get(job.id).log
    assert len(log) == 200
    assert "line 499" in log[-1]


# ---------------------------------------------------------------------------
# The whole path over HTTP
# ---------------------------------------------------------------------------

def test_upload_then_reconstruct_over_http(fake_engine_cmd, tmp_path, monkeypatch):
    """Upload photos, start a job, poll it, and get a placed asset back — the
    exact sequence the browser performs."""
    import io
    import time

    from fastapi.testclient import TestClient

    from app import main as app_main

    monkeypatch.setattr(app_main, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_main, "JOBS", jobs.JobStore(tmp_path / "uploads" / "jobs"))
    monkeypatch.setattr(app_main, "ASSET_DIR", tmp_path / "assets")
    monkeypatch.setattr(app_main, "select_engine",
                        lambda: engines.ExternalEngine(fake_engine_cmd))

    rng = np.random.default_rng(3)
    payload = []
    for i in range(12):
        img = Image.fromarray(rng.integers(0, 255, (240, 320, 3), dtype=np.uint8))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        payload.append(("files", (f"p{i}.png", buf.getvalue(), "image/png")))

    with TestClient(app_main.app) as client:
        report = client.post("/api/upload", files=payload,
                             data={"address": "MetLife Stadium"}).json()
        assert report["batchId"].startswith("batch-")
        assert report["totalCount"] == 12

        # The batch must still be on disk; it used to be deleted in a finally,
        # which is why nothing could ever be reconstructed from it.
        assert (tmp_path / "uploads" / report["batchId"] / "source").is_dir()

        started = client.post("/api/reconstruct", json={
            "batchId": report["batchId"], "address": "MetLife Stadium",
        })
        assert started.status_code == 200, started.text
        job_id = started.json()["jobId"]

        for _ in range(200):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

        assert job["status"] == "done", job.get("error") or job
        assert job["progress"] == 1.0
        bundle = job["asset"]
        assert bundle["placement"]["assetId"] == job["assetId"]
        assert bundle["modelUrl"].endswith("building.glb")
        assert bundle["placement"]["transform"]["upAxis"] == "Y"

        # And the reconstructed asset exports like any other.
        exported = client.post("/api/export", json={"placement": bundle["placement"]})
        assert exported.status_code == 200, exported.text
        assert "building.glb" in exported.json()["files"]


def test_reconstruct_refuses_without_a_location(fake_engine_cmd, tmp_path, monkeypatch):
    import io

    from fastapi.testclient import TestClient

    from app import main as app_main

    monkeypatch.setattr(app_main, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_main, "JOBS", jobs.JobStore(tmp_path / "uploads" / "jobs"))
    monkeypatch.setattr(app_main, "select_engine",
                        lambda: engines.ExternalEngine(fake_engine_cmd))

    buf = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buf, "PNG")
    with TestClient(app_main.app) as client:
        report = client.post("/api/upload",
                             files=[("files", ("a.png", buf.getvalue(), "image/png"))],
                             data={"address": ""}).json()
        refused = client.post("/api/reconstruct", json={"batchId": report["batchId"]})
        assert refused.status_code == 422
        assert "not map-ready" in refused.json()["detail"]
        assert client.post("/api/reconstruct", json={
            "batchId": report["batchId"], "latitude": 40.0,
        }).status_code == 422


def test_no_engine_gives_503_not_a_fake_asset(tmp_path, monkeypatch):
    """The old behaviour was to return the prepared placeholder and call it a
    reconstruction. It must refuse instead."""
    import io

    from fastapi.testclient import TestClient

    from app import main as app_main

    monkeypatch.setattr(app_main, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_main, "JOBS", jobs.JobStore(tmp_path / "uploads" / "jobs"))
    monkeypatch.setattr(app_main, "select_engine", lambda: None)

    buf = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buf, "PNG")
    with TestClient(app_main.app) as client:
        report = client.post("/api/upload",
                             files=[("files", ("a.png", buf.getvalue(), "image/png"))],
                             data={"address": "MetLife Stadium"}).json()
        assert report["asset"] is None
        refused = client.post("/api/reconstruct", json={
            "batchId": report["batchId"], "address": "MetLife Stadium"})
        assert refused.status_code == 503
        assert "No reconstruction engine" in refused.json()["detail"]


def test_a_video_upload_alone_produces_a_placed_building(fake_engine_cmd, tmp_path):
    """The headline: someone uploads one clip of a building and gets an asset."""
    from tests.test_media import write_clip

    source = tmp_path / "batch" / "source"
    source.mkdir(parents=True)
    write_clip(source / "walkaround.mp4", seconds=6.0)

    store = _store(tmp_path)
    job = store.create(address="MetLife Stadium")
    assets = tmp_path / "assets"

    placement = jobs.run_job(
        store, job.id, source_dir=source, address="MetLife Stadium",
        asset_root=assets, asset_id="scan-video",
        engine=engines.ExternalEngine(fake_engine_cmd), reference_meters=BUILDING_W,
    )

    assert placement["location"]["latitude"] == pytest.approx(40.8135, abs=0.01)
    assert (assets / "scan-video" / "building.glb").read_bytes()[:4] == b"glTF"

    provenance = json.loads((assets / "scan-video" / "provenance.json").read_text())
    summary = provenance["mediaSummary"]
    assert summary["photos"] == 0
    assert summary["frames"] >= 20
    assert summary["videos"][0]["name"] == "walkaround.mp4"

    # The frames are traceable back to the clip, not just counted.
    frames = [r for r in provenance["sourceMedia"] if r["kind"] == "frame"]
    assert len(frames) == summary["frames"]
    assert all(f["derivedFrom"]["sourceVideo"] == "walkaround.mp4" for f in frames)

    coverage = json.loads((assets / "scan-video" / "coverage.json").read_text())
    assert coverage["measured"]["fromVideo"] == summary["frames"]
    assert coverage["measured"]["fromPhotos"] == 0
    assert any("motion blur" in r for r in coverage["recommendations"])

    log = "\n".join(store.get(job.id).log)
    assert "extract" in log and "walkaround.mp4" in log


def test_upload_reports_the_sampling_plan_for_a_video(tmp_path):
    """The plan has to be visible before anyone commits to a slow reconstruction."""
    from app.pipeline import analyse_media
    from tests.test_media import write_clip

    clip = write_clip(tmp_path / "clip.mp4", seconds=10.0)
    report = analyse_media([clip])
    row = report["files"][0]

    assert row["kind"] == "video"
    assert row["durationSeconds"] == pytest.approx(10.0, abs=0.3)
    assert row["plannedIntervalSeconds"] == pytest.approx(0.2, abs=0.05)
    assert row["estimatedFrames"] >= 20
    assert report["videoFrameEstimate"] == row["estimatedFrames"]
    assert any("sampled on reconstruct" in n for n in report["notes"])


def test_a_clip_too_short_to_walk_around_is_flagged(tmp_path):
    from app.pipeline import analyse_media
    from tests.test_media import write_clip

    row = analyse_media([write_clip(tmp_path / "quick.mp4", seconds=3.0)])["files"][0]
    assert any("too short" in issue for issue in row["issues"])
