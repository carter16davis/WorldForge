"""Make an anchored scan small enough to load in a browser.

RealityScan textures a model with a single 8192x8192 atlas. That is the right
thing for the export — it is the measured appearance of the building, and the
package handed to Scorched Nebraska should carry it — but it is 40 MB of PNG in
a 50 MB GLB, and a quarter of a gigabyte of video memory once decoded. Dropped
into a viewer alongside a map it is the difference between a model appearing and
a tab dying.

So the asset is published twice. `building.glb` is the full-resolution truth and
is what the export packages. `building-lod.glb` is the same geometry with the
atlas resampled to something a browser will accept, and is what the viewer
loads. Both are the same measurement; only the sampling of the texture differs,
and `provenance.json` records that it was done and by how much.

Geometry is not touched. Simplifying a mesh well needs a quadric decimator that
is not in this project's dependencies, and a bad decimation of a photogrammetric
surface looks like damage. RealityScan's own `-simplify` already caps the export
(a million triangles by default — see `reconstruction.engine`), which a browser
renders without complaint; the texture was always the problem.

    python -m reconstruction.optimize in.glb out.glb --max-texture 2048
"""

from __future__ import annotations

import io
import json
from pathlib import Path

try:
    from app.glb import NotGlb, read_glb, write_glb
except ImportError:      # running the module from inside reconstruction/
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from app.glb import NotGlb, read_glb, write_glb

# 2048 keeps facade detail legible at the distance the viewer frames a building
# from, and costs 16 MB of video memory against the atlas's 256 MB.
DEFAULT_MAX_TEXTURE = 2048

# Below this there is nothing worth rewriting the file for.
MIN_SAVING_RATIO = 0.9


class NotOptimizable(ValueError):
    """The GLB is structured in a way this rewriter will not touch safely."""


def _resample(raw: bytes, max_edge: int) -> tuple[bytes, str, tuple[int, int], tuple[int, int]] | None:
    """Resize one embedded image. None when it is already small enough."""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        before = image.size
        if max(before) <= max_edge:
            return None

        ratio = max_edge / max(before)
        after = (max(1, round(before[0] * ratio)), max(1, round(before[1] * ratio)))
        # LANCZOS: a photogrammetric atlas is packed with unrelated islands, and
        # a cheap filter smears colour across the seams between them.
        resized = image.resize(after, Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        if _has_transparency(resized):
            # Alpha carries cut-outs; JPEG would flatten them into black.
            resized.convert("RGBA").save(buffer, "PNG", optimize=True)
            mime = "image/png"
        else:
            # RealityScan writes an RGBA atlas whose alpha channel is solid 255.
            # Believing the channel rather than its contents costs ten times the
            # bytes to encode information that is not there.
            resized.convert("RGB").save(buffer, "JPEG", quality=88, optimize=True)
            mime = "image/jpeg"

    return buffer.getvalue(), mime, before, after


def _has_transparency(image) -> bool:
    """True only when the alpha channel actually carries something."""
    if image.mode not in ("RGBA", "LA", "PA", "P"):
        return False
    alpha = image.convert("RGBA").getchannel("A")
    return alpha.getextrema()[0] < 255


def web_model(source: Path | str, destination: Path | str, *,
              max_texture: int = DEFAULT_MAX_TEXTURE) -> dict:
    """Write a browser-sized copy of `source`. Returns what it did.

    Raises `NotOptimizable` rather than writing something subtly wrong: a GLB
    with external buffers, or one this rewriter does not understand, is better
    served whole than served broken.
    """
    source, destination = Path(source), Path(destination)
    try:
        document, binary = read_glb(source.read_bytes())
    except NotGlb as exc:
        raise NotOptimizable(str(exc)) from exc

    buffers = document.get("buffers") or []
    if any(b.get("uri") for b in buffers):
        raise NotOptimizable("GLB references an external buffer")
    if len(buffers) > 1:
        raise NotOptimizable(f"GLB has {len(buffers)} buffers; expected one")

    views = document.get("bufferViews") or []
    images = document.get("images") or []

    # Resample first, into a map of view index -> new bytes, so the rebuild
    # below is a single pass that cannot lose track of an offset.
    replacements: dict[int, bytes] = {}
    resampled = []
    for image in images:
        view = image.get("bufferView")
        if view is None:
            continue            # a URI-backed image; not ours to rewrite
        original = bytes(binary[views[view]["byteOffset"]:
                                views[view]["byteOffset"] + views[view]["byteLength"]])
        try:
            result = _resample(original, max_texture)
        except Exception as exc:
            raise NotOptimizable(f"could not read an embedded texture: {exc}") from exc
        if result is None:
            continue
        data, mime, before, after = result
        replacements[view] = data
        image["mimeType"] = mime
        image.pop("uri", None)
        resampled.append({
            "from": f"{before[0]}x{before[1]}", "to": f"{after[0]}x{after[1]}",
            "bytesBefore": len(original), "bytesAfter": len(data),
        })

    # Rebuild the binary chunk in view order. Offsets have to be recomputed even
    # for views that did not change, because everything after a shrunken view
    # moves. glTF requires 4-byte alignment for accessor-backed views.
    rebuilt = bytearray()
    for index, view in enumerate(views):
        payload = replacements.get(index)
        if payload is None:
            start = view.get("byteOffset", 0)
            payload = binary[start:start + view["byteLength"]]
        rebuilt += b"\0" * (-len(rebuilt) % 4)
        view["byteOffset"] = len(rebuilt)
        view["byteLength"] = len(payload)
        rebuilt += payload

    if buffers:
        buffers[0]["byteLength"] = len(rebuilt)

    optimized = write_glb(document, bytes(rebuilt))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(optimized)

    before_bytes = source.stat().st_size
    return {
        "sourceBytes": before_bytes,
        "bytes": len(optimized),
        "ratio": round(len(optimized) / before_bytes, 4) if before_bytes else 1.0,
        "maxTextureEdge": max_texture,
        "texturesResampled": resampled,
    }


def publish_web_model(source: Path | str, destination: Path | str, *,
                      max_texture: int = DEFAULT_MAX_TEXTURE) -> dict | None:
    """`web_model`, but never fails a publish.

    Returns the report, or None when no smaller model was produced — the caller
    then publishes only the full-resolution GLB and the viewer loads that. A
    model that is heavy is a worse demo than a model that is light; a model that
    is missing is not a demo at all.
    """
    import logging

    source, destination = Path(source), Path(destination)
    try:
        report = web_model(source, destination, max_texture=max_texture)
    except Exception as exc:
        logging.getLogger("worldforge.optimize").warning(
            "no web model for %s (%s); the viewer will load the full-resolution GLB",
            source.name, exc)
        destination.unlink(missing_ok=True)
        return None

    if report["ratio"] > MIN_SAVING_RATIO:
        # Shipping a second near-identical file and calling it an LOD is a lie
        # in the export manifest as much as a waste of disk.
        destination.unlink(missing_ok=True)
        return None
    return report


if __name__ == "__main__":       # pragma: no cover - operator convenience
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--max-texture", type=int, default=DEFAULT_MAX_TEXTURE)
    args = parser.parse_args()

    result = web_model(args.source, args.destination, max_texture=args.max_texture)
    print(json.dumps(result, indent=2))
