"""Turn whatever was uploaded into one folder of stills a reconstructor can use.

Photogrammetry consumes still images. People arrive with a phone video, or a
handful of photos, or both, so something has to reconcile that before the engine
runs. That is this module.

Video frames are sampled by presentation timestamp, not by decoded frame index,
because a variable-frame-rate phone recording will otherwise sample unevenly —
and uneven sampling around a building means dense coverage of whichever wall you
walked past slowly. `pipeline.intake_video` does the sampling; this adds the
choice of interval, name collision handling across several sources, and a
provenance chain that survives the merge.

Every extracted frame keeps a `derivedFrom` record naming the source video, its
SHA-256, and the timestamp the frame came from. A frame is evidence only insofar
as you can say which second of which file it came from.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

try:
    from .pipeline import IMAGE_TYPES, intake_video
except ImportError:      # running as a loose script
    from pipeline import IMAGE_TYPES, intake_video

VIDEO_TYPES = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm'}

# Sampling aims for a frame count rather than a fixed interval: a 20-second
# walkaround and a three-minute one need very different intervals to yield a
# usable set, and asking the user to work that out is asking them to fail.
TARGET_FRAMES = 80
MIN_INTERVAL_S = 0.2
MAX_INTERVAL_S = 3.0


@dataclass
class MediaSet:
    """Everything the reconstruction stage needs, plus how it was derived."""

    directory: Path
    photos: int = 0
    frames: int = 0
    videos: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    derived: dict = field(default_factory=dict)      # image name -> derivedFrom

    @property
    def total(self) -> int:
        return self.photos + self.frames

    def as_dict(self) -> dict:
        return {'photos': self.photos, 'frames': self.frames, 'total': self.total,
                'videos': self.videos, 'skipped': self.skipped}


def classify(source):
    """Split an upload into (photos, videos, unusable)."""
    photos, videos, other = [], [], []
    for path in sorted(Path(source).iterdir()):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in IMAGE_TYPES:
            photos.append(path)
        elif suffix in VIDEO_TYPES:
            videos.append(path)
        else:
            other.append(path)
    return photos, videos, other


def probe(path):
    """Duration, frame rate and size of a video, or an explanation of why not.

    Used by the upload screen so the sampling plan can be shown *before* anyone
    waits on a reconstruction.
    """
    try:
        import av
    except ImportError:
        return {'error': 'PyAV is not installed, so this video cannot be read.'}
    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                return {'error': 'No video stream in this file.'}
            stream = container.streams.video[0]
            duration = None
            if stream.duration is not None and stream.time_base:
                duration = float(stream.duration * stream.time_base)
            elif container.duration is not None:
                duration = container.duration / 1_000_000
            rate = float(stream.average_rate) if stream.average_rate else None
            return {'durationSeconds': round(duration, 2) if duration else None,
                    'frameRate': round(rate, 2) if rate else None,
                    'width': stream.codec_context.width,
                    'height': stream.codec_context.height,
                    'codec': stream.codec_context.name}
    except Exception as error:
        return {'error': f'Could not read this video ({type(error).__name__}): {error}'}


def plan_interval(duration_seconds, target=TARGET_FRAMES):
    """Seconds between sampled frames, aiming for `target` frames.

    Clamped at both ends: below `MIN_INTERVAL_S` consecutive frames are nearly
    identical and add processing time without adding parallax, and above
    `MAX_INTERVAL_S` a normal walking pace leaves gaps alignment cannot bridge.
    """
    if not duration_seconds or duration_seconds <= 0:
        return 1.0
    return round(min(MAX_INTERVAL_S, max(MIN_INTERVAL_S, duration_seconds / target)), 3)


def _unique(directory, name):
    """A free filename in `directory`, preserving the original where possible."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for n in range(2, 10_000):
        candidate = directory / f'{stem}-{n}{suffix}'
        if not candidate.exists():
            return candidate
    raise ValueError(f'Cannot find a free filename for {name}.')


def assemble(source, work, *, license_notes='', frame_interval=None,
             max_frames=300, progress=lambda detail: None):
    """Photos and videos in `source` -> one folder of stills in `work/media`.

    Returns a `MediaSet`. Raises only when nothing usable could be produced;
    a single unreadable video is recorded in `skipped` and the rest proceeds,
    because losing one clip should not discard a good set of photos.
    """
    source, work = Path(source), Path(work)
    photos, videos, other = classify(source)
    if not photos and not videos:
        raise ValueError(
            'No photos or video found in the upload. Supported: '
            + ', '.join(sorted(IMAGE_TYPES | VIDEO_TYPES)) + '.'
        )

    media = work / 'media'
    shutil.rmtree(media, ignore_errors=True)
    media.mkdir(parents=True)

    result = MediaSet(directory=media)
    result.skipped = [{'name': p.name, 'reason': 'Not a supported photo or video format.'}
                      for p in other]

    for photo in photos:
        shutil.copy2(photo, _unique(media, photo.name))
    result.photos = len(photos)
    if photos:
        progress(f'{len(photos)} photo(s) taken as they are.')

    for index, video in enumerate(videos, start=1):
        info = probe(video)
        if 'error' in info:
            result.skipped.append({'name': video.name, 'reason': info['error']})
            progress(f'Skipping {video.name}: {info["error"]}')
            continue

        interval = frame_interval or plan_interval(info.get('durationSeconds'))
        progress(f'Extracting frames from {video.name} '
                 f'({info.get("durationSeconds") or "?"}s) every {interval}s.')

        extracted = work / 'extracted' / f'{index:02d}'
        shutil.rmtree(extracted, ignore_errors=True)
        try:
            intake_video(video, extracted, license_notes or f'Uploaded video {video.name}',
                         check_quality=False, interval=interval, max_frames=max_frames)
        except (ValueError, OSError) as error:
            result.skipped.append({'name': video.name, 'reason': str(error)})
            progress(f'Skipping {video.name}: {error}')
            continue

        provenance = json.loads((extracted / 'provenance.json').read_text())
        by_path = {row['path']: row for row in provenance['sourceMedia']}
        stem = ''.join(c if c.isalnum() or c in '-_' else '_' for c in video.stem)[:40]

        count = 0
        for frame in sorted((extracted / 'frames').glob('*.png')):
            target = _unique(media, f'{stem}__{frame.name}')
            shutil.copy2(frame, target)
            row = by_path.get(f'frames/{frame.name}') or {}
            if row.get('derivedFrom'):
                result.derived[target.name] = row['derivedFrom']
            count += 1

        result.frames += count
        video_row = provenance['sourceMedia'][0]
        result.videos.append({
            'name': video.name,
            'sha256': video_row.get('sha256', ''),
            'sizeBytes': video_row.get('sizeBytes', 0),
            'framesExtracted': count,
            'intervalSeconds': interval,
            'extraction': video_row.get('extraction', {}),
            **{k: v for k, v in info.items() if k != 'error'},
        })
        progress(f'{video.name}: {count} frames.')

    if result.total == 0:
        reasons = '; '.join(f'{s["name"]}: {s["reason"]}' for s in result.skipped)
        raise ValueError(f'Nothing usable came out of the upload. {reasons}')
    return result


def merge_provenance(provenance, media_set):
    """Fold the video derivation back into the intake provenance.

    `pipeline.intake` hashes the assembled stills and knows nothing about where
    they came from. This restores the chain: each extracted frame regains its
    `derivedFrom`, and each source video is listed in its own right, so the
    package can answer "which second of which file is this surface from?".
    """
    merged = dict(provenance)
    rows = []
    for row in merged.get('sourceMedia', []):
        derived = media_set.derived.get(Path(row.get('path', '')).name)
        rows.append({**row, 'kind': 'frame', 'derivedFrom': derived} if derived
                    else {**row, 'kind': 'photo'})

    videos = [{'path': v['name'], 'kind': 'video', 'sha256': v['sha256'],
               'sizeBytes': v['sizeBytes'], 'extraction': v['extraction'],
               'framesExtracted': v['framesExtracted']}
              for v in media_set.videos]

    merged['sourceMedia'] = videos + rows
    merged['mediaSummary'] = media_set.as_dict()
    return merged
