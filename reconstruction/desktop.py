"""RealityScan CLI orchestration from Python (native Windows or WSL)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

WORKSPACE = Path(__file__).resolve().parents[1]


def desktop_path(path):
    path = str(Path(path).resolve())
    if os.name == 'nt':
        return path
    if not shutil.which('wslpath'):
        raise ValueError('Launching this desktop installation requires Windows or WSL interop.')
    result = subprocess.run(['wslpath', '-w', path], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f'Cannot translate path: {path}')
    return result.stdout.strip()


def executable_path(explicit=None):
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ValueError(f'RealityScan executable not found: {path}')
        return path.resolve()
    found = shutil.which('RealityScan.exe')
    if found:
        return Path(found)
    base = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) if os.name == 'nt' else Path('/mnt/c/Program Files')
    candidates = sorted((base / 'Epic Games').glob('RealityScan*/RealityScan.exe'))
    if len(candidates) != 1:
        raise ValueError('Specify --realityscan /path/to/RealityScan.exe; installation missing or ambiguous.')
    return candidates[0]


def new_run(root):
    return root / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])


def check_project(project):
    try:
        root = ET.parse(project).getroot()
    except ET.ParseError as error:
        raise ValueError(f'Invalid project XML: {project}') from error
    recon = root.find('reconstructions')
    if recon is None or not any(c.get('id') == recon.get('selectedComponentId')
                               for c in recon.findall('component')):
        raise ValueError('No selected aligned component. Inspect alignment, select the subject component, and save.')


def prepare_commands(photos, project):
    return ['-newScene', '-set', 'appIncSubdirs=true', '-addFolder', desktop_path(photos),
            '-align', '-selectMaximalComponent', '-setReconstructionRegionAuto',
            '-save', desktop_path(project)]


def build_commands(project, output, preset, triangles=None):
    commands = ['-load', desktop_path(project), '-calculateNormalModel']
    if triangles is not None:
        commands += ['-simplify', str(triangles)]
    return commands + ['-unwrap', '-calculateTexture', '-save', desktop_path(output / 'finished.rsproj'),
                       '-exportSelectedModel', desktop_path(output / 'building.glb'), desktop_path(preset), '-quit']


def check_export(output):
    from pipeline import validate_glb
    path = output / 'building.glb'
    document = validate_glb(path)
    if not document.get('images') or not document.get('textures') or not document.get('materials'):
        raise ValueError('GLB has no texture images, textures, or materials; inspect export settings.')
    return [path]


def execute(executable, commands, output, dry_run, verify, metadata):
    command = [str(executable), '-stdConsole', '-set', 'appQuitOnError=true',
               '-writeProgress', desktop_path(output / 'progress.log'), '5', *commands]
    if dry_run:
        print(json.dumps({'output': str(output), 'command': command}, indent=2))
        return
    output.mkdir(parents=True, exist_ok=False)
    record = {'status': 'running', 'command': command, 'startedAt': datetime.now(timezone.utc).isoformat(), **metadata}
    def save():
        (output / 'run.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    save()
    try:
        print(f'Running RealityScan. Logs: {output}', flush=True)
        with (output / 'realityscan.log').open('w', encoding='utf-8') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        record['exitCode'] = result.returncode
        if result.returncode:
            raise ValueError(f'RealityScan failed ({result.returncode}); inspect {output / "realityscan.log"}')
        verify()
        record['status'] = 'completed'
    except (OSError, ValueError, KeyboardInterrupt) as error:
        record['status'], record['error'] = 'failed', str(error)
        raise
    finally:
        record['finishedAt'] = datetime.now(timezone.utc).isoformat()
        save()


def prepare(photos, output=None, executable=None, dry_run=False):
    photos = Path(photos).resolve()
    if not photos.is_dir():
        raise ValueError('Supply a folder of photos or extracted frames.')
    images = [p for p in photos.rglob('*') if p.is_file() and p.suffix.lower() in
              {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp'}]
    if len(images) < 2:
        raise ValueError('At least two overlapping photos required; normally many more are needed.')
    output = Path(output).resolve() if output else new_run(WORKSPACE / 'reconstruction/work/scans')
    if output.exists() or output == photos or photos in output.parents:
        raise ValueError('Choose a new output folder outside the photo folder.')
    executable = executable_path(executable)
    project = output / 'aligned.rsproj'
    print(f'Preparing {len(images)} images. Project: {project}', flush=True)
    print('STOP before meshing: after alignment, adjust the region, save, and close this RealityScan window.\n'
          'Outside circle handles resize; quarter-circle handles rotate; arrows move.\n'
          'Inspect from several views. Background inside the box can still reconstruct.\n'
          'Then run build and type Continue. No automatic continuation.', flush=True)
    execute(executable, prepare_commands(photos, project), output, dry_run,
            lambda: check_project(project), {'stage': 'prepare', 'photos': str(photos), 'imageCount': len(images)})
    if not dry_run:
        print(f'Prepared project: {project}\nNext: build --project "{project}" --export-settings reconstruction/presets/glb.xml')


def build(project, preset, triangles=None, executable=None, dry_run=False):
    project, preset = Path(project).resolve(), Path(preset).resolve()
    check_project(project)
    if Path(str(project) + '.autosave').exists():
        raise ValueError('Resolve the autosave in RealityScan, save and close the project before building.')
    if triangles is not None and triangles < 1:
        raise ValueError('Triangle target must be positive.')
    try:
        root = ET.parse(preset).getroot()
    except ET.ParseError as error:
        raise ValueError('Use the ModelExport XML from a real RealityScan GLB export; see README.') from error
    if root.tag != 'ModelExport':
        raise ValueError('Preset must contain the ModelExport element from a RealityScan GLB export.')
    if not root.get('formatAndVersionUID', '').strip().startswith('glb ') or root.get('embedTextures') != '1':
        raise ValueError('Use a GLB preset with embedTextures=1 (reconstruction/presets/glb.xml).')
    executable = executable_path(executable)
    output = new_run(WORKSPACE / 'exports')
    if not dry_run:
        print(f'Using saved project: {project}\nConfirm you isolated the subject, SAVED, and CLOSED its RealityScan window.')
        try:
            answer = input('Type Continue to build; anything else cancels: ')
        except EOFError:
            answer = ''
        if answer != 'Continue':
            raise ValueError('Cancelled. No reconstruction started.')
    def verify():
        check_project(output / 'finished.rsproj')
        for path in check_export(output):
            print(f'Export: {path}')
    execute(executable, build_commands(project, output, preset, triangles), output, dry_run, verify,
            {'stage': 'build', 'sourceProject': str(project), 'exportPreset': str(preset)})
    if not dry_run:
        print(f'Finished project: {output / "finished.rsproj"}\nTextures are embedded in building.glb. VS Code does not automatically preview them.')
