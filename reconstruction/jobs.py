"""Background reconstruction jobs: photos and an address in, a placed asset out.

Reconstruction takes minutes, so it cannot happen inside the HTTP request that
starts it. A job runs on a worker thread, writes its state to disk after every
change, and the browser polls `/api/jobs/{id}`.

The stages are the whole product in order:

    extract      video to frames, merged with any uploaded photos
    analyse      per-image intake — blur, exposure, duplicates, EXIF GPS
    geocode      the address, because the mesh will need somewhere to be
    reconstruct  the engine (RealityScan, or a configured external command)
    anchor       ground plane, isolation, scale, heading, re-origin
    publish      write the asset where the viewer can load it

State lives in `uploads/<jobId>/job.json` rather than in memory, so a server
restart during a long scan leaves a record of what happened instead of a job
that silently never existed. Jobs interrupted by a restart are marked failed on
load: a `running` job with no thread behind it would poll forever.
"""

from __future__ import annotations

import json
import shutil
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STAGES = ('extract', 'analyse', 'geocode', 'reconstruct', 'anchor', 'publish')

# Rough share of wall-clock each stage takes, so the progress bar is not a lie.
# Reconstruction dominates by an order of magnitude.
STAGE_WEIGHT = {'extract': 0.06, 'analyse': 0.04, 'geocode': 0.01,
                'reconstruct': 0.79, 'anchor': 0.07, 'publish': 0.03}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


@dataclass
class Job:
    id: str
    status: str = 'queued'            # queued | running | done | failed
    stage: str = ''
    detail: str = ''
    progress: float = 0.0             # 0..1
    assetId: str = ''
    address: str = ''
    photoCount: int = 0
    engine: str = ''
    error: str = ''
    log: list[str] = field(default_factory=list)
    createdAt: str = field(default_factory=_now)
    finishedAt: str = ''

    def as_dict(self):
        return asdict(self)


class JobStore:
    """Thread-safe job registry backed by one JSON file per job."""

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    def _path(self, job_id):
        return self.root / job_id / 'job.json'

    def _load_existing(self):
        for path in sorted(self.root.glob('*/job.json')):
            try:
                data = json.loads(path.read_text())
                job = Job(**{k: v for k, v in data.items() if k in Job.__annotations__})
            except (OSError, ValueError, TypeError):
                continue
            if job.status in ('queued', 'running'):
                # No thread survived the restart, so nothing will ever finish it.
                job.status, job.error = 'failed', 'Server restarted while this job was running.'
                job.finishedAt = _now()
            self.jobs[job.id] = job
        self._flush_all()

    def _flush_all(self):
        for job in self.jobs.values():
            self._write(job)

    def _write(self, job):
        path = self._path(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job.as_dict(), indent=2) + '\n')

    def create(self, **fields):
        job = Job(id=uuid.uuid4().hex, **fields)
        with self.lock:
            self.jobs[job.id] = job
            self._write(job)
        return job

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return Job(**job.as_dict()) if job else None

    def list(self, limit=20):
        with self.lock:
            ordered = sorted(self.jobs.values(), key=lambda j: j.createdAt, reverse=True)
            return [Job(**j.as_dict()).as_dict() for j in ordered[:limit]]

    def update(self, job_id, **fields):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            self._write(job)
            return Job(**job.as_dict())

    def note(self, job_id, stage, detail, fraction=0.0):
        """Record progress within a stage. `fraction` is progress inside it."""
        done = sum(STAGE_WEIGHT[s] for s in STAGES[:STAGES.index(stage)]) if stage in STAGES else 0.0
        overall = done + STAGE_WEIGHT.get(stage, 0.0) * max(0.0, min(1.0, fraction))
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            job.stage, job.detail = stage, detail
            job.progress = round(overall, 4)
            line = f'{_now()}  {stage}: {detail}'
            job.log.append(line)
            del job.log[:-200]           # a wedged scan must not grow without bound
            self._write(job)


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def run_job(store, job_id, source_dir, address, *, asset_root, asset_id,
            engine=None, latitude=None, longitude=None, osm_footprint=None,
            reference_meters=None, license_notes='', frame_interval=None,
            max_frames=300):
    """Uploaded media to a placed asset. Raises on failure; the caller records it."""
    from . import anchor as anchoring
    from . import engine as engines
    from . import media as media_module
    from .pipeline import intake

    source_dir = Path(source_dir)
    work = source_dir.parent
    note = lambda stage, detail, f=0.0: store.note(job_id, stage, detail, f)

    # --- extract -----------------------------------------------------------
    # Whatever was uploaded becomes one folder of stills. A video is sampled by
    # presentation timestamp so a variable-frame-rate phone recording does not
    # end up densely covering whichever wall the person walked past slowly.
    note('extract', 'Sorting the upload; extracting frames from any video.')
    media = media_module.assemble(
        source_dir, work, license_notes=license_notes,
        frame_interval=frame_interval, max_frames=max_frames,
        progress=lambda detail: note('extract', detail, 0.5),
    )
    store.update(job_id, photoCount=media.total)
    note('extract',
         f'{media.total} image(s) to reconstruct from '
         f'({media.photos} uploaded, {media.frames} from video).', 1.0)

    # --- analyse -----------------------------------------------------------
    note('analyse', 'Checking the images for blur, exposure and duplicates.')
    reports = work / 'intake'
    shutil.rmtree(reports, ignore_errors=True)
    intake(media.directory, reports,
           license_notes or 'Uploaded through the WorldForge web app.',
           check_quality=True)
    confidence = json.loads((reports / 'confidence.json').read_text())
    provenance = media_module.merge_provenance(
        json.loads((reports / 'provenance.json').read_text()), media)
    flagged = sum(1 for row in confidence.get('imageQuality') or [] if row.get('flags'))
    note('analyse', f'{confidence["uniquePhotoCount"]} unique images, {flagged} flagged.', 1.0)

    # --- geocode -----------------------------------------------------------
    if latitude is None or longitude is None:
        from app.geocode import geocode

        note('geocode', f'Resolving {address!r}.')
        resolved = geocode(address)
        if resolved.confidence <= 0:
            raise ValueError(
                f'The address did not resolve, so the building has nowhere to go. '
                f'{resolved.note} You can enter "latitude, longitude" directly.'
            )
        latitude, longitude = resolved.latitude, resolved.longitude
        note('geocode', f'{resolved.displayName} ({resolved.source}).', 1.0)
    else:
        note('geocode', f'Using supplied coordinates {latitude:.5f}, {longitude:.5f}.', 1.0)

    # --- reconstruct -------------------------------------------------------
    engine = engine or engines.select_engine()
    if engine is None:
        raise ValueError(
            'No reconstruction engine is available on this machine. '
            + ' '.join(e['detail'] for e in engines.engine_status() if not e['available'])
        )
    store.update(job_id, engine=engine.name)
    note('reconstruct', f'Starting {engine.name}. This is the slow part.')
    raw = engine.run(media.directory, work / 'scan',
                     progress=lambda stage, detail: note('reconstruct', detail, 0.5))
    note('reconstruct', f'{engine.name} exported {raw.name}.', 1.0)

    # --- anchor ------------------------------------------------------------
    note('anchor', 'Finding the ground plane, isolating the building, solving scale.')
    anchored = work / 'anchored'
    result = anchoring.anchor_model(
        raw, latitude, longitude, asset_id=asset_id, name=address or asset_id,
        address=address, osm_footprint=osm_footprint,
        reference_meters=reference_meters, out_dir=anchored,
    )
    report = result['report']
    unresolved = report.get('unresolved') or []
    note('anchor',
         'Anchored. ' + (f'Unsolved: {", ".join(unresolved)} — set these in the editor.'
                         if unresolved else 'Scale and heading solved from the footprint.'),
         1.0)

    # --- publish -----------------------------------------------------------
    note('publish', 'Writing the asset for the viewer.')
    destination = Path(asset_root) / asset_id
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(anchored / 'building.glb', destination / 'building.glb')

    placement = result['placement']
    (destination / 'placement.json').write_text(json.dumps(placement, indent=2) + '\n')

    provenance.update({
        'reconstructionTool': engine.name,
        'mediaSummary': media.as_dict(),
        'regionMode': 'automatic',
        'regionNote': (
            'The reconstruction region was placed automatically because this run '
            'was unattended. A hand-placed region excludes more of the surroundings; '
            'use the prepare/build commands in reconstruction/pipeline.py for that.'
        ),
        'anchorReport': report,
        'observedCoveragePercent': None,
        'syntheticCoveragePercent': 0.0,
        'syntheticUse': 'none',
        'generatedAt': _now(),
    })
    (destination / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    (destination / 'coverage.json').write_text(
        json.dumps(_coverage_from_intake(asset_id, confidence, report, media), indent=2) + '\n')

    _write_thumbnail(placement, destination / 'thumbnail.webp')
    note('publish', 'Done.', 1.0)
    return placement


def _coverage_from_intake(asset_id, confidence, anchor_report, media=None):
    """A coverage report built only from what was actually measured.

    Deliberately not per-facade. Per-facade coverage needs the camera poses from
    the reconstruction, which the RealityScan export does not include, and
    inventing four facade rows from a photo count would be exactly the kind of
    fabricated measurement this project exists to avoid. So the facade list is
    empty and the recommendations say what is actually known.
    """
    photos = confidence.get('uniquePhotoCount', 0)
    flagged = [row for row in confidence.get('imageQuality') or [] if row.get('flags')]
    unresolved = anchor_report.get('unresolved') or []

    recommendations = []
    if photos < 20:
        recommendations.append(
            f'Only {photos} unique images. Walk right around the building and '
            f'capture 20 or more with 60-80% overlap between consecutive frames.')
    if media is not None and media.frames and not media.photos:
        recommendations.append(
            'Every image came from video. Video frames carry motion blur and '
            'heavier compression than stills, so a set of photographs of the '
            'same building will reconstruct better.')
    for skipped in (media.skipped if media is not None else []):
        recommendations.append(f'{skipped["name"]} was not used: {skipped["reason"]}')
    if flagged:
        recommendations.append(
            f'{len(flagged)} photo(s) flagged for blur or exposure. Re-shoot those '
            f'angles rather than deleting them blind.')
    for name in unresolved:
        recommendations.append(
            f'{name} could not be solved from the evidence — set it in the '
            f'placement editor, or supply a reference footprint or measurement.')
    recommendations.append(
        'Per-facade coverage is not reported: it needs camera poses that the '
        'reconstruction export does not currently include.')

    return {
        'schemaVersion': 1,
        'assetId': asset_id,
        'overallConfidence': 0.0,
        'facades': [],
        'measured': {'uniqueImages': photos, 'flaggedImages': len(flagged),
                     'fromVideo': media.frames if media is not None else 0,
                     'fromPhotos': media.photos if media is not None else photos,
                     'perFacadeCoverage': None},
        'recommendations': recommendations,
    }


def _write_thumbnail(placement, path):
    """Plan view of the anchored footprint. Best-effort; never fails a job."""
    try:
        from app.assets import thumbnail

        ring = [tuple(p) for p in placement.get('footprint') or []]
        if len(ring) >= 3:
            thumbnail(ring, [], path)
    except Exception:
        pass


def start(store, job_id, **kwargs):
    """Run `run_job` on a worker thread, recording success or failure."""

    def worker():
        store.update(job_id, status='running')
        try:
            placement = run_job(store, job_id, **kwargs)
            store.update(job_id, status='done', assetId=placement['assetId'],
                         finishedAt=_now(), progress=1.0)
        except Exception as exc:
            store.note(job_id, store.get(job_id).stage or 'reconstruct', f'Failed: {exc}')
            store.update(job_id, status='failed', error=str(exc), finishedAt=_now(),
                         log=(store.get(job_id).log + traceback.format_exc().splitlines()[-12:]))

    thread = threading.Thread(target=worker, name=f'reconstruct-{job_id[:8]}', daemon=True)
    thread.start()
    return thread
