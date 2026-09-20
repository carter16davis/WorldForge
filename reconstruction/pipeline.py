"""Offline intake and handoff for manually reconstructed RealityScan assets."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import tempfile
from datetime import datetime, timezone


IMAGE_TYPES = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp', '.heic', '.heif'}


def enable_heif(path):
    if Path(path).suffix.lower() in {'.heic', '.heif'}:
        try:
            from pillow_heif import register_heif_opener
        except ImportError as error:
            raise ValueError('Install reconstruction/requirements.txt for HEIC photos.') from error
        register_heif_opener()


def image_quality(path):
    """Advisory pixel heuristics, not geometric coverage or confidence scores."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    import numpy as np
    enable_heif(path)

    try:
        with Image.open(path) as original:
            original.load()
            width, height = original.size
            image = ImageOps.exif_transpose(original).convert('L')
            image.thumbnail((1024, 1024))
            pixels = np.asarray(image, dtype=np.float64)
        if min(pixels.shape) < 3:
            return {'width': width, 'height': height, 'flags': ['image-too-small']}
        center = pixels[1:-1, 1:-1]
        laplacian = (pixels[:-2, 1:-1] + pixels[2:, 1:-1]
                     + pixels[1:-1, :-2] + pixels[1:-1, 2:] - 4 * center)
        sharpness = float(laplacian.var())
        mean = float(pixels.mean())
        dark = float((pixels <= 10).mean())
        bright = float((pixels >= 245).mean())
        flags = []
        if sharpness < 100:
            flags.append('possible-blur-or-low-texture')
        if mean < 45 or dark > 0.5:
            flags.append('possibly-underexposed')
        if mean > 210 or bright > 0.5:
            flags.append('possibly-overexposed')
        return {'width': width, 'height': height,
                'laplacianVariance': round(sharpness, 3),
                'meanLuminance': round(mean, 3),
                'darkPixelFraction': round(dark, 4),
                'brightPixelFraction': round(bright, 4), 'flags': flags}
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        return {'flags': ['unreadable-image'], 'error': str(error)}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def intake(source, output, license_notes, check_quality=False):
    source, output = Path(source), Path(output)
    if not source.is_dir():
        raise ValueError('Source must be a photo directory.')
    files = sorted(p for p in source.rglob('*') if p.suffix.lower() in IMAGE_TYPES and p.is_file())
    if not files:
        raise ValueError('No supported photos found. Extract video frames before intake.')
    if check_quality:
        try:
            import PIL
            import numpy
        except ImportError as error:
            raise ValueError('Install reconstruction/requirements.txt to use --check-quality.') from error
    records, duplicates, seen, quality = [], [], {}, []
    for path in files:
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        relative = path.relative_to(source).as_posix()
        records.append({'path': relative, 'sha256': digest, 'sizeBytes': path.stat().st_size})
        if check_quality:
            quality.append({'path': relative, **image_quality(path)})
        if digest in seen:
            duplicates.append({'path': relative, 'duplicateOf': seen[digest]})
        else:
            seen[digest] = relative
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'provenance.json', {
        'schemaVersion': 1, 'sourceMedia': records,
        'reconstructionTool': 'RealityScan Desktop',
        'observedCoveragePercent': None, 'syntheticCoveragePercent': 0,
        'syntheticUse': 'none', 'licenseNotes': license_notes,
        'generatedAt': datetime.now(timezone.utc).isoformat(),
    })
    write_json(output / 'confidence.json', {
        'schemaVersion': 1, 'status': 'requires-review',
        'photoCount': len(records), 'uniquePhotoCount': len(seen),
        'exactDuplicates': duplicates,
        'checksNotPerformed': (['near-duplicates', 'surface-coverage'] if check_quality else
                               ['image-decode', 'blur', 'lighting', 'near-duplicates', 'surface-coverage']),
        'imageQuality': quality,
        'qualityMethod': ({'analysisMaxDimension': 1024, 'blurVarianceThreshold': 100,
                           'darkMeanThreshold': 45, 'brightMeanThreshold': 210,
                           'clippedFractionThreshold': 0.5,
                           'note': 'Advisory heuristics only; review flags, do not auto-delete photos.'}
                          if check_quality else None),
        'observedCoveragePercent': None,
        'recommendations': ['Review photos and alignment in RealityScan before reconstruction.'],
    })


def rotate_image(image, degrees):
    """Apply quarter-turn counterclockwise rotation without resampling."""
    from PIL import Image
    angle = degrees % 360
    if angle not in (0, 90, 180, 270):
        raise ValueError(f'Unsupported rotation {degrees}; expected a multiple of 90 degrees.')
    methods = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180,
               270: Image.Transpose.ROTATE_270}
    return image.transpose(methods[angle]) if angle else image.copy()


def orient_photos(source, output, degrees=0):
    """Write EXIF-normalized PNG copies and a source hash manifest."""
    try:
        from PIL import Image, ImageOps
    except ImportError as error:
        raise ValueError('Install reconstruction/requirements.txt for photo orientation.') from error
    source, output = Path(source), Path(output)
    if not source.is_dir():
        raise ValueError('Source must be a photo directory.')
    if output.exists():
        raise FileExistsError(f'Output already exists: {output}')
    if source.resolve() in output.resolve().parents:
        raise ValueError('Place corrected output outside the source photo directory.')
    files = sorted(p for p in source.rglob('*') if p.is_file() and p.suffix.lower() in IMAGE_TYPES)
    if not files:
        raise ValueError('No supported photos found.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.orient-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'result'
        staged.mkdir()
        records = []
        for path in files:
            enable_heif(path)
            relative = path.relative_to(source)
            target = staged / (str(relative) + '.png')
            target.parent.mkdir(parents=True, exist_ok=True)
            with path.open('rb') as stream:
                source_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
            with Image.open(path) as original:
                orientation = original.getexif().get(274, 1)
                corrected = rotate_image(ImageOps.exif_transpose(original), degrees)
                corrected.save(target, exif=corrected.getexif().tobytes())
            with target.open('rb') as stream:
                result_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
            records.append({'sourcePath': relative.as_posix(), 'sourceSha256': source_hash,
                            'path': target.relative_to(staged).as_posix(), 'sha256': result_hash,
                            'sourceExifOrientation': orientation,
                            'additionalRotationCounterclockwise': degrees})
        write_json(staged / 'orientation.json', {'schemaVersion': 1, 'images': records})
        if output.exists():
            raise FileExistsError(f'Output already exists: {output}')
        staged.rename(output)


def intake_video(source, output, license_notes, check_quality=False, interval=1.0, max_frames=300):
    """Sample real decoded frames; preserve actual presentation timestamps."""
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError('Frame interval must be positive and finite.')
    if max_frames < 1:
        raise ValueError('Maximum frames must be at least 1.')
    source, output = Path(source), Path(output)
    if not source.is_file():
        raise ValueError('Video must be an existing local file.')
    if output.exists():
        raise FileExistsError(f'Output already exists: {output}')
    try:
        import av
        from PIL import Image
    except ImportError as error:
        raise ValueError('Install reconstruction/requirements.txt for video intake.') from error
    with source.open('rb') as stream:
        source_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.video-intake-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'result'
        frames_dir = staged / 'frames'
        frames_dir.mkdir(parents=True)
        timestamps, first_time, last_time, next_sample = [], None, None, 0.0
        capped = False
        try:
            with av.open(str(source)) as container:
                if not container.streams.video:
                    raise ValueError('Input contains no video stream.')
                for index, frame in enumerate(container.decode(video=0)):
                    timestamp = frame.time
                    if timestamp is None or not math.isfinite(timestamp):
                        raise ValueError('Video has missing frame timestamps; re-export it with timestamps.')
                    if last_time is not None and timestamp < last_time:
                        raise ValueError('Video frame timestamps are not monotonic.')
                    last_time = timestamp
                    if first_time is None:
                        first_time = timestamp
                    elapsed = timestamp - first_time
                    if elapsed + 1e-9 < next_sample:
                        continue
                    if len(timestamps) >= max_frames:
                        capped = True
                        break
                    name = f'frame_{len(timestamps) + 1:06d}.png'
                    rotation = frame.rotation
                    rotate_image(frame.to_image(), rotation).save(frames_dir / name)
                    timestamps.append({'path': name, 'decodedFrameIndex': index,
                                       'presentationTimestampSeconds': timestamp,
                                       'secondsFromFirstFrame': elapsed,
                                       'rotationCounterclockwise': rotation})
                    next_sample = (math.floor((elapsed + 1e-9) / interval) + 1) * interval
        except av.error.FFmpegError as error:
            raise ValueError(f'Could not decode video: {error}') from error
        if not timestamps:
            raise ValueError('Video contains no decodable frames.')
        reports = staged / 'reports'
        intake(frames_dir, reports, license_notes, check_quality)
        provenance = json.loads((reports / 'provenance.json').read_text())
        by_name = {item['path']: item for item in timestamps}
        for record in provenance['sourceMedia']:
            record['derivedFrom'] = {'sourceVideo': source.name, 'sha256': source_hash,
                                     **by_name[record['path']]}
            record['path'] = 'frames/' + record['path']
        provenance['sourceMedia'].insert(0, {
            'path': source.name, 'kind': 'video', 'sha256': source_hash,
            'sizeBytes': source.stat().st_size,
            'extraction': {'tool': 'PyAV', 'version': av.__version__,
                           'intervalSeconds': interval, 'maxFrames': max_frames,
                           'frameLimitReached': capped, 'stream': 'first-video',
                           'format': 'PNG', 'rotationApplied': any(t['rotationCounterclockwise'] % 360 for t in timestamps),
                           'orientationPolicy': 'apply-frame-display-rotation'},
        })
        write_json(reports / 'provenance.json', provenance)
        confidence = json.loads((reports / 'confidence.json').read_text())
        for record in confidence['imageQuality']:
            record['path'] = 'frames/' + record['path']
        for record in confidence['exactDuplicates']:
            for key in ('path', 'duplicateOf'):
                record[key] = 'frames/' + record[key]
        if capped:
            confidence['recommendations'].append('Frame limit reached; increase --max-frames to cover the remaining video.')
        confidence['recommendations'].append('Display rotation metadata applied; visually review frame orientation.')
        write_json(reports / 'confidence.json', confidence)
        for filename in ('provenance.json', 'confidence.json'):
            (reports / filename).rename(staged / filename)
        reports.rmdir()
        if output.exists():
            raise FileExistsError(f'Output already exists: {output}')
        staged.rename(output)


def validate_glb(path):
    """Check the GLB container and require self-contained resources."""
    data = Path(path).read_bytes()
    if len(data) < 20:
        raise ValueError('GLB is truncated.')
    magic, version, length = struct.unpack_from('<4sII', data)
    if magic != b'glTF' or version != 2 or length != len(data):
        raise ValueError('Expected a valid GLB 2.0 header and file length.')
    offset, chunks = 12, []
    while offset < length:
        if offset + 8 > length:
            raise ValueError('Truncated GLB chunk header.')
        size, kind = struct.unpack_from('<II', data, offset)
        offset += 8
        if size % 4 or offset + size > length:
            raise ValueError('Invalid GLB chunk length.')
        chunks.append((kind, data[offset:offset + size]))
        offset += size
    if not chunks or chunks[0][0] != 0x4E4F534A:
        raise ValueError('GLB must begin with JSON.')
    document = json.loads(chunks[0][1])
    if not document.get('meshes'):
        raise ValueError('GLB contains no meshes.')
    for resource in document.get('buffers', []) + document.get('images', []):
        uri = resource.get('uri', '')
        if uri and not uri.startswith('data:'):
            raise ValueError('Export embedded textures and buffers; external resources are not portable.')
    return document


def measured_bounds(report):
    """Read width/length/height in meters from an anchor run, or explain why not.

    Typing bounds by hand is how a 68-meter stadium ships as 68 model units. The
    anchor command measures them off the mesh, so packaging prefers that record
    and only falls back to a typed value when it is given one.
    """
    path = Path(report) / 'anchor-report.json'
    if not path.is_file():
        raise ValueError('Supply --bounds, or run anchor first so bounds can be measured.')
    measured = json.loads(path.read_text(encoding='utf-8')).get('boundsMeters') or {}
    if not measured.get('measured'):
        raise ValueError('The anchor run did not solve a metric scale, so its bounds '
                         'are in arbitrary model units. Re-run anchor with '
                         '--reference-meters or --footprint, or pass --bounds.')
    try:
        return [float(measured[key]) for key in ('widthMeters', 'lengthMeters', 'heightMeters')]
    except KeyError as error:
        raise ValueError(f'anchor-report.json is missing {error.args[0]}.') from error


def package(model, thumbnail, report, output, asset_id, bounds):
    model, thumbnail, report, output = map(Path, (model, thumbnail, report, output))
    if not asset_id.strip():
        raise ValueError('Asset ID must not be empty.')
    if bounds is None:
        bounds = measured_bounds(report)
    if any(not math.isfinite(v) or v <= 0 for v in bounds):
        raise ValueError('Bounds must be positive, finite dimensions in meters.')
    validate_glb(model)
    preview = thumbnail.read_bytes()
    if len(preview) < 12 or preview[:4] != b'RIFF' or preview[8:12] != b'WEBP':
        raise ValueError('Thumbnail must be a WebP image.')
    for filename in ('provenance.json', 'confidence.json'):
        json.loads((report / filename).read_text(encoding='utf-8'))
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(model, output / 'building.glb')
    shutil.copyfile(thumbnail, output / 'thumbnail.webp')
    for filename in ('provenance.json', 'confidence.json'):
        shutil.copyfile(report / filename, output / filename)
    write_json(output / 'reconstruction.json', {
        'assetId': asset_id, 'modelPath': 'building.glb',
        'thumbnailPath': 'thumbnail.webp',
        'bounds': dict(zip(('width', 'length', 'height'), bounds)),
        'boundsUnit': 'meters',
        'confidenceReportPath': 'confidence.json', 'provenancePath': 'provenance.json',
    })


def read_footprint(path):
    """Accept either a bare [[lat, lon], ...] list or an object with an "outer" ring.

    The project's own footprint files (app/data/*.json) use the second shape, so
    the one command that wants a footprint has to read the one file that has one.
    """
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    ring = document.get('outer') if isinstance(document, dict) else document
    if not isinstance(ring, list) or len(ring) < 3:
        raise ValueError('Footprint must be at least three [latitude, longitude] pairs.')
    try:
        return [(float(lat), float(lon)) for lat, lon in ring]
    except (TypeError, ValueError) as error:
        raise ValueError(f'Footprint must be [latitude, longitude] pairs: {error}') from error


def anchor_scan(args):
    """Georeference a scan and write the anchored GLB plus its report."""
    from . import anchor as anchoring

    footprint = read_footprint(args.footprint) if args.footprint else None
    result = anchoring.anchor_model(
        args.model, args.latitude, args.longitude,
        asset_id=args.asset_id, name=args.name, address=args.address,
        osm_footprint=footprint, reference_meters=args.reference_meters,
        elevation_meters=args.elevation, up_axis=args.up_axis,
        out_dir=args.output,
    )
    output = Path(args.output)
    write_json(output / 'placement.json', result['placement'])
    report = result['report']
    print(f"Anchored {args.asset_id} -> {output}")
    for step in report['steps']:
        mark = 'OK  ' if step['solved'] else 'TODO'
        value = '' if step['value'] is None else f" = {step['value']}"
        print(f"  {mark} {step['name']}{value}  ({step['method'] or 'n/a'}, "
              f"confidence {step['confidence']})")
        if step['note']:
            print(f"       {step['note']}")
    if report['unresolved']:
        print("\nUnresolved, set these in the placement editor: "
              + ', '.join(report['unresolved']))


def _desktop():
    """Import the RealityScan driver as a package submodule or a sibling script."""
    try:
        from . import desktop
    except ImportError:
        import desktop
    return desktop


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='Align in RealityScan, then stop for manual region isolation')
    prepare.add_argument('--photos', required=True)
    prepare.add_argument('--output', help='New run folder; defaults to a unique folder under reconstruction/work/scans')
    build = commands.add_parser('build', help='Resume saved region and export textured GLB after explicit Continue')
    build.add_argument('--project', required=True)
    build.add_argument('--export-settings', default=str(Path(__file__).parent / 'presets/glb.xml'))
    build.add_argument('--triangles', type=int, help='Optional simplification target before unwrap and texturing')
    build.add_argument('--detail', choices=('preview', 'normal', 'high'), default='high',
                       help='RealityScan meshing quality (default: high)')
    for stage in (prepare, build):
        stage.add_argument('--realityscan', help='Override RealityScan executable path')
        stage.add_argument('--dry-run', action='store_true')
    orient = commands.add_parser('orient', help='Write corrected photo copies using EXIF orientation')
    orient.add_argument('--photos', required=True)
    orient.add_argument('--output', required=True)
    orient.add_argument('--rotate', type=int, choices=(0, 90, 180, 270), default=0,
                        help='Additional counterclockwise rotation after EXIF normalization')
    capture = commands.add_parser('intake')
    inputs = capture.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--photos')
    inputs.add_argument('--video')
    capture.add_argument('--frame-interval', type=float, default=1.0, help='Video sampling interval in seconds (default: 1)')
    capture.add_argument('--max-frames', type=int, default=300, help='Maximum extracted video frames (default: 300)')
    capture.add_argument('--output', required=True)
    capture.add_argument('--license-notes', required=True)
    capture.add_argument('--check-quality', action='store_true', help='Decode images and flag possible blur/exposure issues')
    place = commands.add_parser('anchor', help='Georeference a scan: ground, isolate, scale, heading, re-origin')
    place.add_argument('--model', required=True, help='GLB or OBJ exported from RealityScan')
    place.add_argument('--output', required=True, help='New folder for building.glb and anchor-report.json')
    place.add_argument('--asset-id', required=True)
    place.add_argument('--latitude', type=float, required=True)
    place.add_argument('--longitude', type=float, required=True)
    place.add_argument('--name', default='')
    place.add_argument('--address', default='')
    place.add_argument('--elevation', type=float, default=0.0,
                       help='Ground elevation in meters, or 0 for a documented relative datum')
    place.add_argument('--footprint', help='JSON file holding [[lat, lon], ...] for the real building')
    place.add_argument('--reference-meters', type=float,
                       help='One measured real-world length along the building\'s longest horizontal axis')
    place.add_argument('--up-axis', choices=('Y', 'Z'), default='Y',
                       help="The exported mesh's up axis (default: Y, the glTF convention)")

    export = commands.add_parser('package')
    for name in ('model', 'thumbnail', 'report', 'output', 'asset-id'):
        export.add_argument('--' + name, required=True)
    export.add_argument('--bounds', nargs=3, type=float,
                        metavar=('WIDTH', 'LENGTH', 'HEIGHT'),
                        help='Dimensions in meters. Omit to read them from the '
                             'anchor-report.json written by the anchor command.')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            desktop = _desktop()
            desktop.prepare(args.photos, args.output, args.realityscan, args.dry_run)
        elif args.command == 'build':
            desktop = _desktop()
            desktop.build(args.project, args.export_settings, args.triangles, args.realityscan,
                          args.dry_run, args.detail)
        elif args.command == 'anchor':
            anchor_scan(args)
        elif args.command == 'orient':
            orient_photos(args.photos, args.output, args.rotate)
        elif args.command == 'intake':
            if args.video:
                intake_video(args.video, args.output, args.license_notes, args.check_quality,
                             args.frame_interval, args.max_frames)
            else:
                intake(args.photos, args.output, args.license_notes, args.check_quality)
        else:
            package(args.model, args.thumbnail, args.report, args.output, args.asset_id, args.bounds)
    except (OSError, ValueError) as error:
        parser.exit(2, f'Error: {error}\n')


if __name__ == '__main__':
    main()
