import io
import json
import zipfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import export, main
from app.geocode import Geocoder
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_ROOT", tmp_path)
    monkeypatch.setattr(main, "EXPORT_ROOT", tmp_path)
    with TestClient(app) as session:
        yield session


def test_capabilities_report_what_is_actually_wired(client):
    """The strip is the app's own honesty check, so it must track this machine
    rather than a value baked into a test. Whether RealityScan is installed
    differs between a developer's laptop and CI; what must not differ is that
    the strip agrees with `/api/engines`."""
    from reconstruction.engine import engine_status

    caps = {c["name"]: c for c in client.get("/api/session").json()["capabilities"]}
    assert caps["Geocoding"]["wired"] is True
    assert caps["Export packaging"]["wired"] is True

    usable = [e for e in engine_status() if e["available"]]
    assert caps["Reconstruction"]["wired"] is bool(usable)
    if usable:
        assert caps["Reconstruction"]["provider"] == usable[0]["name"]
        assert "Reconstruct" in caps["Reconstruction"]["detail"]
    else:
        # It must say plainly that nothing will be reconstructed here, and name
        # what is missing. It must *not* promise the prepared asset as a
        # fallback: `/api/reconstruct` returns 503 rather than quietly handing
        # back a footprint extrusion and calling it a reconstruction.
        detail = caps["Reconstruction"]["detail"].lower()
        assert "not reconstructed" in detail
        assert any(e["name"].lower().split()[0] in detail for e in engine_status())

    served = client.get("/api/engines").json()
    assert served["canReconstruct"] is bool(usable)


def test_place_then_export_writes_real_files(client):
    placed = client.post("/api/place", json={
        "address": "MetLife Stadium",
        "transform": {"headingDegrees": 90, "metersPerModelUnit": 1.2,
                      "verticalOffsetMeters": 3},
    })
    assert placed.status_code == 200, placed.text
    assert placed.json()["resolved"]["source"] == "venue-table"
    placement = placed.json()["asset"]["placement"]

    first = client.post("/api/export", json={"placement": placement, "era": "2026"})
    assert first.status_code == 200, first.text
    manifest = first.json()

    archive = client.get(manifest["downloadUrl"])
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
        prefix = placement["assetId"] + "/"
        doc = json.loads(package.read(prefix + "placement.json"))
        assert doc["transform"]["headingDegrees"] == 90
        assert doc["transform"]["verticalOffsetMeters"] == 3
        assert doc["spatialIndex"] == placement["spatialIndex"]
        assert doc["footprint"] == placement["footprint"]
        assert package.read(prefix + "building.glb")[:4] == b"glTF"
        assert json.loads(package.read(prefix + "manifest.json"))["exportId"] == manifest["exportId"]


def test_lod_is_generated_and_declared(client):
    """The low-detail model used to be dead code behind a delegation that always
    fired. If the manifest claims an LOD, the bytes have to be in the zip."""
    placement = client.get("/api/session").json()["asset"]["placement"]
    manifest = client.post("/api/export", json={"placement": placement}).json()

    assert "building-lod.glb" in manifest["files"], manifest["files"]
    archive = client.get(manifest["downloadUrl"])
    with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
        prefix = placement["assetId"] + "/"
        names = set(package.namelist())
        assert prefix + "building-lod.glb" in names
        lod = package.read(prefix + "building-lod.glb")
        assert lod[:4] == b"glTF"
        # An "LOD" that is not smaller than the model is a lie with a filename.
        assert len(lod) < len(package.read(prefix + "building.glb"))
        assert json.loads(package.read(prefix + "placement.json"))["models"]["low"] == "building-lod.glb"


def test_a_published_reconstruction_outlives_the_job_that_made_it(client):
    """The failure this guards against loses ten minutes of photogrammetry.

    A reconstruction is written to `web/assets` and the browser tab that started
    it gets the bundle when the job finishes. Reload the page and that tab is
    gone — so unless the asset can be listed and re-opened from disk, the model
    is stranded in a directory with no route to it.
    """
    from app.assets import ASSET_DIR
    from app.demo_data import DEMO_ASSET_ID

    written = {a["assetId"] for a in client.get("/api/assets").json()["assets"]}
    on_disk = {d.name for d in ASSET_DIR.iterdir()
               if (d / "placement.json").is_file()}
    assert written == on_disk
    assert DEMO_ASSET_ID in written

    # Every listed asset opens as a full bundle, with no prior session state.
    for asset_id in written:
        bundle = client.get(f"/api/assets/{asset_id}").json()
        assert bundle["placement"]["assetId"] == asset_id
        assert bundle["modelUrl"].endswith("/building.glb")
        assert bundle["previewUrl"].endswith(".glb")
        assert client.get(bundle["previewUrl"]).status_code == 200


def test_an_asset_id_cannot_escape_the_asset_directory(client):
    for hostile in ("../../etc/passwd", "..", ".ssh", "a/b", ""):
        response = client.get(f"/api/assets/{hostile}")
        assert response.status_code in (400, 404, 422), f"{hostile!r} -> {response.status_code}"
        assert "passwd" not in response.text


def test_any_published_asset_can_be_moved_not_only_the_prepared_one(client, tmp_path):
    """`/api/place` used to 404 on anything but the demo asset, which meant a
    reconstruction was stuck at whatever address was typed before it started —
    with a placement editor on screen offering to fix it."""
    import shutil

    from app.assets import ASSET_DIR
    from app.demo_data import DEMO_ASSET_ID

    scan = ASSET_DIR / "test-scan-placeable"
    shutil.copytree(ASSET_DIR / DEMO_ASSET_ID, scan, dirs_exist_ok=True)
    try:
        doc = json.loads((scan / "placement.json").read_text())
        doc["assetId"] = scan.name
        (scan / "placement.json").write_text(json.dumps(doc))

        moved = client.post("/api/place", json={
            "assetId": scan.name, "address": "MetLife Stadium",
            "transform": {"headingDegrees": 42},
        })
        assert moved.status_code == 200, moved.text
        placement = moved.json()["asset"]["placement"]
        assert placement["assetId"] == scan.name
        assert placement["transform"]["headingDegrees"] == 42
        # The cell follows the coordinates, as the AGENTS.md checklist requires.
        assert placement["spatialIndex"]["cell"]
    finally:
        shutil.rmtree(scan, ignore_errors=True)


def test_exports_are_immutable_snapshots(client):
    placement = client.get("/api/session").json()["asset"]["placement"]
    first = client.post("/api/export", json={"placement": placement, "era": "2026"}).json()
    original = client.get(first["downloadUrl"]).content

    placement["transform"]["headingDegrees"] = 180
    second = client.post("/api/export", json={"placement": placement, "era": "2426"}).json()

    assert second["exportId"] != first["exportId"]
    assert client.get(first["downloadUrl"]).content == original


def test_invalid_transform_does_not_fall_back(client):
    placement = client.get("/api/session").json()["asset"]["placement"]
    placement["transform"]["metersPerModelUnit"] = 0
    assert client.post("/api/export", json={"placement": placement}).status_code == 422
    assert client.post("/api/place", json={"latitude": 91, "longitude": 0}).status_code == 422
    assert client.post("/api/place", json={"latitude": 40}).status_code == 422
    placement["transform"]["metersPerModelUnit"] = 1
    placement["assetId"] = "../escape"
    assert client.post("/api/export", json={"placement": placement}).status_code == 422


def test_ambiguous_and_offline_geocoding(client):
    payload = (b'[{"display_name":"First match","lat":"37.2","lon":"-80.4"},'
               b'{"display_name":"Second match","lat":"37.3","lon":"-80.5"}]')

    with patch("app.geocode.GEOCODER", Geocoder()), \
         patch("app.geocode.urllib.request.urlopen") as opener:
        opener.return_value = io.BytesIO(payload)
        offline = client.post("/api/geocode",
                              json={"address": "Unlisted building", "allowNetwork": False}).json()
        assert offline["confidence"] == 0
        opener.assert_not_called()

        opener.return_value = io.BytesIO(payload)
        response = client.post("/api/place", json={"address": "Unlisted building"}).json()
        assert response["asset"] is None
        assert [c["label"] for c in response["resolved"]["candidates"]] == \
            ["First match", "Second match"]

    picked = client.post("/api/place",
                         json={"latitude": 37.2, "longitude": -80.4, "address": "First match"})
    assert picked.status_code == 200
    body = picked.json()
    assert body["asset"]["placement"]["location"]["latitude"] == 37.2
    assert body["asset"]["cells"][0]["cell"] == body["asset"]["placement"]["spatialIndex"]["cell"]


def test_server_errors_do_not_leak_internals(tmp_path, monkeypatch):
    """A stack trace on a projector leaks filesystem paths, and an exception
    string is not a sentence a judge can act on."""
    monkeypatch.setattr(export, "EXPORT_ROOT", tmp_path)
    monkeypatch.setattr(main, "EXPORT_ROOT", tmp_path)
    # The default TestClient re-raises server exceptions, which bypasses the very
    # handler under test; this asks for the response a browser would actually get.
    with TestClient(app, raise_server_exceptions=False) as client, \
            patch("app.main.pipeline.capabilities",
                  side_effect=RuntimeError("/srv/secret/path boom")):
        response = client.get("/api/session")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "/srv/secret/path" not in detail
    assert "RuntimeError" not in detail
    assert "server log" in detail


def test_blur_detection_actually_runs(tmp_path):
    """`_blur_score` used to `import cv2`, which is not a project dependency, so
    every score came back None and a blurry upload was never flagged. The check
    has to fire on the dependencies the project actually installs."""
    from PIL import Image, ImageDraw

    from app.pipeline import analyse_media

    sharp = tmp_path / "sharp.png"
    flat = tmp_path / "flat.png"

    detailed = Image.new("RGB", (256, 256), (120, 90, 70))
    draw = ImageDraw.Draw(detailed)
    for x in range(0, 256, 8):
        draw.line([(x, 0), (x, 256)], fill=(20, 20, 20), width=2)
    detailed.save(sharp, "PNG")
    Image.new("RGB", (256, 256), (120, 90, 70)).save(flat, "PNG")

    report = analyse_media([sharp, flat])
    rows = {r["name"]: r for r in report["files"]}

    assert rows["sharp.png"]["sharpness"] is not None
    assert rows["sharp.png"]["issues"] == []
    assert rows["flat.png"]["sharpness"] == pytest.approx(0.0, abs=1.0)
    assert any("focus" in issue for issue in rows["flat.png"]["issues"])
    assert report["usableCount"] == 1
