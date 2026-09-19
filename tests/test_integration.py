import io
import json
import zipfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import export, main
from geospatial import integration


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_ROOT", tmp_path)
    monkeypatch.setattr(main, "EXPORT_ROOT", tmp_path)
    with TestClient(app) as session:
        yield session


def test_frontend_uses_geospatial_and_exports_real_files(client):
    session = client.get("/api/session").json()
    caps = {c["name"]: c for c in session["capabilities"]}
    for name in ("Geocoding", "Export packaging"):
        assert caps[name]["wired"] is True
        assert caps[name]["provider"] == "geospatial"
    placement = session["asset"]["placement"]
    placed = client.post("/api/place", json={"address": "MetLife Stadium", "transform": {"headingDegrees": 90, "metersPerModelUnit": 1.2, "verticalOffsetMeters": 3}})
    assert placed.status_code == 200
    placement = placed.json()["asset"]["placement"]
    assert placed.json()["resolved"]["source"] == "prepared-venue"
    first = client.post("/api/export", json={"placement": placement, "era": "2026"})
    assert first.status_code == 200, first.text
    manifest = first.json()
    assert manifest["provider"] == "geospatial"
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
        assert "low" not in doc["models"]
    placement["transform"]["headingDegrees"] = 180
    second = client.post("/api/export", json={"placement": placement, "era": "2426"}).json()
    assert second["exportId"] != manifest["exportId"]
    assert client.get(manifest["downloadUrl"]).content == archive.content


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
    candidates = [{"label": "First match", "latitude": 37.2, "longitude": -80.4},
                  {"label": "Second match", "latitude": 37.3, "longitude": -80.5}]
    with patch.object(integration._geocoder, "search", return_value=candidates) as search:
        offline = client.post("/api/geocode", json={"address": "Unlisted building", "allowNetwork": False}).json()
        assert offline["confidence"] == 0
        search.assert_not_called()
        response = client.post("/api/place", json={"address": "Unlisted building"}).json()
        assert response["asset"] is None
        assert response["resolved"]["candidates"] == candidates
        picked = client.post("/api/place", json={"latitude": 37.2, "longitude": -80.4, "address": "First match"})
        assert picked.status_code == 200
        assert picked.json()["asset"]["placement"]["location"]["latitude"] == 37.2
        assert picked.json()["asset"]["cells"][0]["cell"] == picked.json()["asset"]["placement"]["spatialIndex"]["cell"]
