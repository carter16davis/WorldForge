"""Video in, reconstructable frames out — with the provenance chain intact.

Every video here is encoded for real by PyAV, not mocked, because the whole
point of sampling by presentation timestamp is behaviour that a mock cannot
exhibit.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image

from reconstruction import media

pytest.importorskip("av", reason="PyAV is needed to encode the test clips")


def write_clip(path, *, seconds=6.0, fps=30, size=(320, 240), seed=1):
    """A synthetic walkaround: moving stripe over per-frame noise."""
    import av

    container = av.open(str(path), "w")
    stream = container.add_stream("libx264", rate=fps)
    stream.width, stream.height = size
    stream.pix_fmt = "yuv420p"
    rng = np.random.default_rng(seed)
    for i in range(int(seconds * fps)):
        frame = rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
        frame[:, (i * 3) % (size[0] - 14):(i * 3) % (size[0] - 14) + 14] = 255
        for packet in stream.encode(av.VideoFrame.from_ndarray(frame, format="rgb24")):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


@pytest.fixture
def source(tmp_path):
    folder = tmp_path / "batch" / "source"
    folder.mkdir(parents=True)
    return folder


# ---------------------------------------------------------------------------
# Sampling plan
# ---------------------------------------------------------------------------

def test_interval_targets_a_frame_count_not_a_fixed_cadence():
    """A 20-second walkaround and a three-minute one need different intervals to
    yield a usable set, and asking the user to work that out is asking them to
    fail."""
    assert media.plan_interval(80) == pytest.approx(1.0, abs=0.01)
    # Short clip: sample fast, but never below the floor where consecutive
    # frames are near-identical and add cost without parallax.
    assert media.plan_interval(4) == media.MIN_INTERVAL_S
    # Long clip: capped, because past a few seconds a walking pace leaves gaps
    # alignment cannot bridge.
    assert media.plan_interval(3600) == media.MAX_INTERVAL_S
    assert media.plan_interval(None) == 1.0
    assert media.plan_interval(0) == 1.0


def test_probe_reads_real_video_metadata(source):
    info = media.probe(write_clip(source / "clip.mp4", seconds=5.0, fps=30))
    assert info["durationSeconds"] == pytest.approx(5.0, abs=0.2)
    assert info["frameRate"] == pytest.approx(30, abs=0.5)
    assert (info["width"], info["height"]) == (320, 240)
    assert "error" not in info


def test_probe_explains_a_file_it_cannot_read(source):
    (source / "broken.mp4").write_bytes(b"not a video")
    assert "error" in media.probe(source / "broken.mp4")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def test_video_becomes_frames(source, tmp_path):
    write_clip(source / "walkaround.mp4", seconds=6.0)
    result = media.assemble(source, tmp_path / "work")

    assert result.frames >= 20, "a 6s clip should yield a usable set at the floor interval"
    assert result.photos == 0
    assert result.total == len(list(result.directory.glob("*.png")))
    assert result.videos[0]["name"] == "walkaround.mp4"
    assert result.videos[0]["framesExtracted"] == result.frames
    assert len(result.videos[0]["sha256"]) == 64


def test_photos_and_video_are_merged_into_one_set(source, tmp_path):
    for i in range(5):
        Image.fromarray(
            np.random.default_rng(i).integers(0, 255, (240, 320, 3), dtype=np.uint8)
        ).save(source / f"photo_{i}.png")
    write_clip(source / "clip.mp4", seconds=6.0)

    result = media.assemble(source, tmp_path / "work")
    assert result.photos == 5
    assert result.frames > 0
    assert result.total == result.photos + result.frames
    names = {p.name for p in result.directory.iterdir()}
    assert {f"photo_{i}.png" for i in range(5)} <= names


def test_frames_from_two_videos_do_not_collide(source, tmp_path):
    write_clip(source / "north.mp4", seconds=4.0, seed=2)
    write_clip(source / "south.mp4", seconds=4.0, seed=3)
    result = media.assemble(source, tmp_path / "work")

    assert len(result.videos) == 2
    # Both clips produce frame_000001.png internally; neither may overwrite the
    # other in the merged folder.
    assert result.total == len(list(result.directory.iterdir()))
    assert any(n.name.startswith("north__") for n in result.directory.iterdir())
    assert any(n.name.startswith("south__") for n in result.directory.iterdir())


def test_a_photo_name_clashing_with_a_frame_is_kept(source, tmp_path):
    write_clip(source / "clip.mp4", seconds=4.0)
    Image.new("RGB", (32, 32)).save(source / "clip__frame_000001.png")
    result = media.assemble(source, tmp_path / "work")
    assert result.total == len(list(result.directory.iterdir()))


def test_one_bad_video_does_not_discard_good_photos(source, tmp_path):
    for i in range(6):
        Image.fromarray(
            np.random.default_rng(i).integers(0, 255, (240, 320, 3), dtype=np.uint8)
        ).save(source / f"photo_{i}.png")
    (source / "corrupt.mp4").write_bytes(b"not a video at all")

    result = media.assemble(source, tmp_path / "work")
    assert result.photos == 6
    assert result.frames == 0
    assert [s["name"] for s in result.skipped] == ["corrupt.mp4"]


def test_unsupported_files_are_reported_not_silently_dropped(source, tmp_path):
    Image.new("RGB", (64, 64)).save(source / "ok.png")
    (source / "notes.txt").write_text("hello")
    result = media.assemble(source, tmp_path / "work")
    assert [s["name"] for s in result.skipped] == ["notes.txt"]
    assert "Not a supported" in result.skipped[0]["reason"]


def test_nothing_usable_raises_with_the_reasons(source, tmp_path):
    (source / "corrupt.mp4").write_bytes(b"nope")
    with pytest.raises(ValueError, match="Nothing usable"):
        media.assemble(source, tmp_path / "work")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="No photos or video"):
        media.assemble(empty, tmp_path / "work2")


def test_explicit_interval_overrides_the_plan(source, tmp_path):
    write_clip(source / "clip.mp4", seconds=6.0)
    coarse = media.assemble(source, tmp_path / "coarse", frame_interval=2.0)
    assert coarse.frames == pytest.approx(3, abs=1)
    assert coarse.videos[0]["intervalSeconds"] == 2.0


def test_max_frames_is_respected(source, tmp_path):
    write_clip(source / "clip.mp4", seconds=6.0)
    capped = media.assemble(source, tmp_path / "work", frame_interval=0.2, max_frames=5)
    assert capped.frames == 5
    assert capped.videos[0]["extraction"]["frameLimitReached"] is True


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def test_every_frame_can_be_traced_to_a_second_of_a_file(source, tmp_path):
    """A frame is evidence only insofar as you can say which second of which
    file it came from."""
    from reconstruction.pipeline import intake

    write_clip(source / "walkaround.mp4", seconds=6.0)
    Image.fromarray(
        np.random.default_rng(9).integers(0, 255, (240, 320, 3), dtype=np.uint8)
    ).save(source / "corner.png")

    result = media.assemble(source, tmp_path / "work")
    intake(result.directory, tmp_path / "reports", "test", check_quality=False)
    merged = media.merge_provenance(
        json.loads((tmp_path / "reports" / "provenance.json").read_text()), result)

    rows = merged["sourceMedia"]
    videos = [r for r in rows if r["kind"] == "video"]
    frames = [r for r in rows if r["kind"] == "frame"]
    photos = [r for r in rows if r["kind"] == "photo"]

    assert len(videos) == 1
    assert len(frames) == result.frames
    assert [p["path"] for p in photos] == ["corner.png"]

    for frame in frames:
        origin = frame["derivedFrom"]
        assert origin["sourceVideo"] == "walkaround.mp4"
        assert origin["sha256"] == videos[0]["sha256"]
        assert origin["presentationTimestampSeconds"] >= 0

    # Timestamps advance, which is what makes them a real sampling record.
    stamps = [f["derivedFrom"]["presentationTimestampSeconds"] for f in frames]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)

    assert merged["mediaSummary"]["frames"] == result.frames
    assert merged["mediaSummary"]["photos"] == 1
