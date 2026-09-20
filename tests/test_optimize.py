"""Can a RealityScan export actually reach a browser?

A textured reconstruction leaves RealityScan as roughly 10 MB of geometry
wrapped around 40 MB of 8192x8192 PNG. The export should keep that — it is the
measured appearance of the building — and the viewer must never be asked to
download it. `reconstruction.optimize` is the seam, and the thing it must never
do is change the measurement on its way through.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh
from PIL import Image

from reconstruction import optimize


def _textured_glb(path, size=(1024, 1024), mode="RGB"):
    """A box carrying an atlas that does not compress, like a real one."""
    mesh = trimesh.creation.box(extents=[4.0, 3.0, 2.0]).subdivide()
    rng = np.random.default_rng(7)
    channels = 4 if mode == "RGBA" else 3
    noise = rng.integers(0, 255, (*size, channels), dtype=np.uint8)
    if mode == "RGBA":
        noise[:, :, 3] = 128                       # genuinely translucent

    mesh.visual = trimesh.visual.TextureVisuals(
        uv=rng.random((len(mesh.vertices), 2)),
        material=trimesh.visual.material.PBRMaterial(
            baseColorTexture=Image.fromarray(noise, mode)),
    )
    path.write_bytes(trimesh.Scene(mesh).export(file_type="glb"))
    return mesh


def test_the_geometry_is_untouched(tmp_path):
    """The whole contract. A viewer model that is not the same measurement as
    the exported one is not a preview of anything."""
    source, destination = tmp_path / "building.glb", tmp_path / "building-lod.glb"
    original = _textured_glb(source)

    report = optimize.web_model(source, destination, max_texture=256)

    small = trimesh.load(destination, force="mesh")
    assert np.allclose(small.vertices, original.vertices)
    assert np.array_equal(small.faces, original.faces)
    assert np.allclose(small.visual.uv, original.visual.uv)
    assert report["bytes"] < report["sourceBytes"] / 2


def test_the_texture_is_resampled_not_dropped(tmp_path):
    source, destination = tmp_path / "building.glb", tmp_path / "lod.glb"
    _textured_glb(source, size=(1024, 1024))

    optimize.web_model(source, destination, max_texture=256)

    texture = trimesh.load(destination, force="mesh").visual.material.baseColorTexture
    assert texture is not None, "the model came back untextured"
    assert max(texture.size) == 256


def test_an_opaque_alpha_channel_does_not_force_png(tmp_path):
    """RealityScan writes RGBA whose alpha is solid 255. Believing the channel
    rather than its contents costs about ten times the bytes."""
    source = tmp_path / "building.glb"
    mesh = trimesh.creation.box().subdivide()
    rng = np.random.default_rng(3)
    noise = rng.integers(0, 255, (512, 512, 4), dtype=np.uint8)
    noise[:, :, 3] = 255
    mesh.visual = trimesh.visual.TextureVisuals(
        uv=rng.random((len(mesh.vertices), 2)),
        material=trimesh.visual.material.PBRMaterial(
            baseColorTexture=Image.fromarray(noise, "RGBA")))
    source.write_bytes(trimesh.Scene(mesh).export(file_type="glb"))

    as_png = tmp_path / "png.glb"
    optimize.web_model(source, as_png, max_texture=128)
    assert trimesh.load(as_png, force="mesh").visual.material.baseColorTexture.mode == "RGB"


def test_real_transparency_survives(tmp_path):
    source, destination = tmp_path / "building.glb", tmp_path / "lod.glb"
    _textured_glb(source, size=(512, 512), mode="RGBA")

    optimize.web_model(source, destination, max_texture=128)

    texture = trimesh.load(destination, force="mesh").visual.material.baseColorTexture
    assert texture.mode in ("RGBA", "LA", "P")
    assert min(texture.convert("RGBA").getchannel("A").getextrema()) < 255


def test_a_model_with_nothing_to_gain_is_not_published(tmp_path):
    """Shipping a second near-identical file and calling it an LOD is a lie in
    the export manifest as much as a waste of disk."""
    source, destination = tmp_path / "building.glb", tmp_path / "lod.glb"
    _textured_glb(source, size=(64, 64))

    assert optimize.publish_web_model(source, destination, max_texture=2048) is None
    assert not destination.exists()


def test_a_file_it_cannot_rewrite_is_refused_not_mangled(tmp_path):
    source, destination = tmp_path / "notes.txt", tmp_path / "lod.glb"
    source.write_text("this is not a GLB")

    with pytest.raises(optimize.NotOptimizable):
        optimize.web_model(source, destination)
    # And the forgiving wrapper leaves nothing half-written behind.
    assert optimize.publish_web_model(source, destination) is None
    assert not destination.exists()


def test_buffer_views_stay_aligned(tmp_path):
    """glTF requires accessor-backed views on a four-byte boundary. Rebuilding
    the binary chunk after shrinking an image is exactly where that is lost, and
    a misaligned view is a model that loads in trimesh and not in a browser."""
    import json
    import struct

    source, destination = tmp_path / "building.glb", tmp_path / "lod.glb"
    _textured_glb(source, size=(512, 512))
    optimize.web_model(source, destination, max_texture=128)

    data = destination.read_bytes()
    length = struct.unpack("<I", data[12:16])[0]
    document = json.loads(data[20:20 + length])

    assert struct.unpack("<I", data[8:12])[0] == len(data)      # declared length
    assert all(view.get("byteOffset", 0) % 4 == 0 for view in document["bufferViews"])
    assert document["buffers"][0]["byteLength"] >= max(
        view["byteOffset"] + view["byteLength"] for view in document["bufferViews"])
