"""Stage exports before publishing them; never overwrite an existing asset."""

import json
import shutil
import struct
import tempfile
from pathlib import Path

from .placement import build_placement


def check_glb(path):
    path = Path(path)
    with path.open("rb") as stream:
        header = stream.read(12)
    if len(header) != 12:
        raise ValueError("GLB header is incomplete")
    magic, version, length = struct.unpack("<4sII", header)
    if magic != b"glTF" or version != 2 or length != path.stat().st_size or length < 20:
        raise ValueError("Expected a GLB v2 file with a matching declared length")


def export_package(root, placement, model_path, thumbnail_path, provenance_path, *, lod_path=None, extensions=None, documents=None):
    """Package a pre-anchored GLB. This does not rewrite vertices or validate full glTF."""
    canonical = build_placement(
        placement["assetId"], placement["name"], placement["sourceAddress"],
        {**placement["location"], **placement["transform"]},
        has_lod=lod_path is not None,
    )
    if placement != canonical:
        raise ValueError("Placement must match the supported contract and supplied model files")
    extensions = extensions or {}
    if set(extensions) & set(canonical):
        raise ValueError("Extensions cannot replace core placement fields")
    documents = documents or {}
    if any(name not in {"coverage.json", "manifest.json"} for name in documents):
        raise ValueError("Unsupported extra document")
    placement_json = json.dumps({**canonical, **extensions}, indent=2, allow_nan=False) + "\n"
    extra_json = {name: json.dumps(value, indent=2, allow_nan=False) + "\n" for name, value in documents.items()}
    check_glb(model_path)
    if lod_path is not None:
        check_glb(lod_path)
    thumbnail = Path(thumbnail_path)
    with thumbnail.open("rb") as stream:
        header = stream.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WEBP":
        raise ValueError("thumbnail must be a WebP file")
    provenance = json.loads(Path(provenance_path).read_text())
    if not isinstance(provenance, dict) or provenance.get("schemaVersion") != 1:
        raise ValueError("provenance must be a version 1 JSON object")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / canonical["assetId"]
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Refusing to overwrite {destination}")
    with tempfile.TemporaryDirectory(prefix=".worldforge-", dir=root) as temporary:
        stage = Path(temporary) / "package"
        stage.mkdir()
        shutil.copyfile(model_path, stage / "building.glb")
        shutil.copyfile(thumbnail_path, stage / "thumbnail.webp")
        shutil.copyfile(provenance_path, stage / "provenance.json")
        if lod_path is not None:
            shutil.copyfile(lod_path, stage / "building-lod.glb")
        (stage / "placement.json").write_text(placement_json)
        for name, content in extra_json.items():
            (stage / name).write_text(content)
        stage.rename(destination)
    return destination
