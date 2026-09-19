"""Reconstruction backends: photos in, a textured mesh out.

Two are available.

**RealityScan** drives the Epic desktop application through its command line.
It is the good one, and it is Windows-only; under WSL the calls are translated
with `wslpath`. The interactive CLI in `pipeline.py` deliberately stops between
alignment and meshing so a human can place the reconstruction region by hand.
The web app cannot stop for that, so it runs unattended with
`-setReconstructionRegionAuto` and records `regionMode: "automatic"` in
provenance. Automatic region selection keeps whatever shared the box with the
building; `anchor.isolate_building` is what removes it afterwards, and the
confidence reported for an unattended run is correspondingly lower.

**External** runs any command you give it in `WORLDFORGE_RECONSTRUCTION_CMD`.
That is the hook for COLMAP, Meshroom, or a cloud service — anything that can
take a folder of photos and write a GLB. WorldForge does not ship a driver for
those, because an untested driver for a tool the author never ran is worse than
a documented hole.

    export WORLDFORGE_RECONSTRUCTION_CMD='my-photogrammetry --in {photos} --out {output}'

The command must write `{output}/building.glb`. `{photos}` and `{output}` are
substituted as POSIX paths; the command is split with `shlex`, not run through a
shell, so shell metacharacters in it do nothing.

Neither backend is a hidden fallback for the other. `available()` reports why a
backend cannot run, the UI shows that sentence, and a job fails rather than
quietly producing the prepared asset and calling it a reconstruction.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from . import desktop
except ImportError:      # running as a loose script
    import desktop

PHOTO_TYPES = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp'}

# RealityScan needs at least this many aligned photos to have any chance. The
# application will happily try with fewer and produce nothing usable.
MIN_PHOTOS = 8

# A reconstruction that has not written its progress file in this long is
# assumed wedged. Meshing a large capture is slow, so this is generous.
STALL_TIMEOUT_S = float(os.environ.get('WORLDFORGE_RECONSTRUCTION_STALL_S', '1800'))


@dataclass
class EngineStatus:
    name: str
    available: bool
    detail: str
    executable: str = ''

    def as_dict(self) -> dict:
        return {'name': self.name, 'available': self.available,
                'detail': self.detail, 'executable': self.executable}


def count_photos(photos):
    return sum(1 for p in Path(photos).rglob('*')
               if p.is_file() and p.suffix.lower() in PHOTO_TYPES)


# ---------------------------------------------------------------------------
# Windows path staging
# ---------------------------------------------------------------------------

def windows_work_root():
    """A directory RealityScan can open as a real drive path, or None.

    `wslpath -w` turns a WSL path into `\\\\wsl.localhost\\...`, a UNC path.
    RealityScan handles those inconsistently and some Windows tooling refuses
    them outright, so a job whose photos live in the Linux filesystem is staged
    onto the Windows side first. `WORLDFORGE_WIN_WORK_ROOT` overrides the guess.
    """
    explicit = os.environ.get('WORLDFORGE_WIN_WORK_ROOT')
    if explicit:
        return Path(explicit)
    if os.name == 'nt':
        return None                      # already native; nothing to stage
    candidates = sorted(Path('/mnt/c/Users').glob('*/AppData/Local/Temp')) \
        if Path('/mnt/c/Users').is_dir() else []
    for candidate in candidates:
        if candidate.is_dir() and os.access(candidate, os.W_OK):
            return candidate / 'worldforge'
    return None


def needs_staging(path):
    """True when this path would reach Windows as a UNC path."""
    if os.name == 'nt':
        return False
    try:
        return desktop.desktop_path(path).startswith('\\\\')
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# RealityScan
# ---------------------------------------------------------------------------

def oneshot_commands(photos, output, preset, triangles=None):
    """Align, auto-region, mesh, unwrap, texture and export without stopping.

    The interactive path in `pipeline.py` is still the one to use when quality
    matters; this exists so the web app can run at all.
    """
    commands = [
        '-newScene',
        '-set', 'appIncSubdirs=true',
        '-addFolder', desktop.desktop_path(photos),
        '-align',
        '-selectMaximalComponent',
        '-setReconstructionRegionAuto',
        '-calculateNormalModel',
    ]
    if triangles is not None:
        commands += ['-simplify', str(triangles)]
    return commands + [
        '-unwrap',
        '-calculateTexture',
        '-save', desktop.desktop_path(output / 'scene.rsproj'),
        '-exportSelectedModel',
        desktop.desktop_path(output / 'building.glb'),
        desktop.desktop_path(preset),
        '-quit',
    ]


class RealityScanEngine:
    name = 'RealityScan'

    def __init__(self, executable=None, preset=None, triangles=400_000):
        self.executable = executable
        self.preset = Path(preset) if preset else Path(__file__).parent / 'presets/glb.xml'
        self.triangles = triangles

    def status(self):
        try:
            found = desktop.executable_path(self.executable)
        except ValueError as error:
            return EngineStatus(self.name, False, str(error))
        if not self.preset.is_file():
            return EngineStatus(self.name, False,
                                f'Export preset missing: {self.preset}', str(found))
        if os.name != 'nt' and not shutil.which('wslpath'):
            return EngineStatus(self.name, False,
                                'RealityScan is a Windows application and this machine '
                                'has no WSL interop to launch it with.', str(found))
        return EngineStatus(
            self.name, True,
            'Runs unattended with an automatic reconstruction region. For a '
            'hand-placed region, use the prepare/build commands in the CLI.',
            str(found),
        )

    def run(self, photos, output, progress=lambda stage, detail: None):
        photos, output = Path(photos), Path(output)
        found = count_photos(photos)
        if found < MIN_PHOTOS:
            raise ValueError(
                f'Only {found} usable photo{"" if found == 1 else "s"}. '
                f'Photogrammetry needs at least {MIN_PHOTOS}, and realistically '
                f'20 or more taken while walking around the building with '
                f'60-80% overlap between consecutive frames.'
            )
        executable = desktop.executable_path(self.executable)
        output.mkdir(parents=True, exist_ok=True)

        staged = None
        source = photos
        if needs_staging(photos):
            root = windows_work_root()
            if root is None:
                progress('stage', 'Photos are on the WSL filesystem and no Windows '
                                  'work directory was found; passing a UNC path, which '
                                  'RealityScan may refuse. Set WORLDFORGE_WIN_WORK_ROOT.')
            else:
                staged = root / f'job-{os.getpid()}-{int(time.time())}'
                progress('stage', f'Copying {found} photos to {staged} so RealityScan '
                                  f'sees a drive path rather than a UNC path.')
                shutil.copytree(photos, staged / 'photos')
                source = staged / 'photos'

        try:
            commands = oneshot_commands(source, output, self.preset, self.triangles)
            _execute(executable, commands, output, progress)
        finally:
            if staged is not None:
                shutil.rmtree(staged, ignore_errors=True)

        glb = output / 'building.glb'
        if not glb.is_file():
            raise ValueError(
                'RealityScan exited without writing building.glb. The usual cause '
                'is that alignment failed, which means the photos do not overlap '
                f'enough. See {output / "realityscan.log"}.'
            )
        return glb


def _execute(executable, commands, output, progress):
    """Run RealityScan, streaming its progress file back to the caller."""
    progress_file = output / 'progress.log'
    command = [str(executable), '-stdConsole', '-set', 'appQuitOnError=true',
               '-writeProgress', desktop.desktop_path(progress_file), '5', *commands]

    stop = threading.Event()
    watcher = threading.Thread(target=_watch_progress,
                               args=(progress_file, progress, stop), daemon=True)
    watcher.start()
    try:
        with (output / 'realityscan.log').open('w', encoding='utf-8') as log:
            log.write(' '.join(command) + '\n\n')
            log.flush()
            proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            code = _wait_with_stall_timeout(proc, progress_file)
    finally:
        stop.set()
        watcher.join(timeout=2)

    if code != 0:
        raise ValueError(
            f'RealityScan exited with code {code}. Inspect '
            f'{output / "realityscan.log"} for the reason.'
        )


def _wait_with_stall_timeout(proc, progress_file):
    """Wait for RealityScan, killing it if it stops reporting progress.

    A wedged reconstruction that never exits would hold a job open forever, and
    the web app has no way to tell the difference from a slow one except that a
    slow one keeps writing progress.
    """
    last_change = time.monotonic()
    last_size = -1
    while True:
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        size = progress_file.stat().st_size if progress_file.exists() else -1
        if size != last_size:
            last_size, last_change = size, time.monotonic()
        elif time.monotonic() - last_change > STALL_TIMEOUT_S:
            proc.kill()
            proc.wait(timeout=30)
            raise ValueError(
                f'RealityScan reported no progress for '
                f'{STALL_TIMEOUT_S / 60:.0f} minutes and was stopped.'
            )


def _watch_progress(progress_file, progress, stop):
    seen = ''
    while not stop.wait(2):
        try:
            text = progress_file.read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            continue
        if text and text != seen:
            seen = text
            progress('reconstruct', text.splitlines()[-1][:200])


# ---------------------------------------------------------------------------
# External command
# ---------------------------------------------------------------------------

class ExternalEngine:
    name = 'External command'

    def __init__(self, template=None):
        self.template = template if template is not None else os.environ.get(
            'WORLDFORGE_RECONSTRUCTION_CMD', '')

    def status(self):
        if not self.template.strip():
            return EngineStatus(
                self.name, False,
                'Not configured. Set WORLDFORGE_RECONSTRUCTION_CMD to a command '
                'that reads {photos} and writes {output}/building.glb.')
        try:
            parts = shlex.split(self.template)
        except ValueError as error:
            return EngineStatus(self.name, False, f'Command will not parse: {error}')
        if not parts:
            return EngineStatus(self.name, False, 'Command is empty.')
        binary = shutil.which(parts[0])
        if binary is None:
            return EngineStatus(self.name, False, f'{parts[0]!r} is not on PATH.')
        if '{photos}' not in self.template or '{output}' not in self.template:
            return EngineStatus(self.name, False,
                                'Command must contain both {photos} and {output}.',
                                binary)
        return EngineStatus(self.name, True, f'Runs {self.template}', binary)

    def run(self, photos, output, progress=lambda stage, detail: None):
        state = self.status()
        if not state.available:
            raise ValueError(state.detail)
        photos, output = Path(photos), Path(output)
        output.mkdir(parents=True, exist_ok=True)
        command = [part.format(photos=photos.as_posix(), output=output.as_posix())
                   for part in shlex.split(self.template)]
        progress('reconstruct', f'Running {command[0]}')
        with (output / 'engine.log').open('w', encoding='utf-8') as log:
            log.write(' '.join(command) + '\n\n')
            log.flush()
            code = subprocess.call(command, stdout=log, stderr=subprocess.STDOUT)
        if code != 0:
            raise ValueError(f'{command[0]} exited with code {code}; see '
                             f'{output / "engine.log"}.')
        glb = output / 'building.glb'
        if not glb.is_file():
            raise ValueError(f'{command[0]} finished but wrote no {glb.name}.')
        return glb


# ---------------------------------------------------------------------------

def engines():
    """Every backend, best first, whether or not it can run here."""
    return [RealityScanEngine(), ExternalEngine()]


def engine_status():
    return [e.status().as_dict() for e in engines()]


def select_engine():
    """The first backend that can actually run, or None."""
    for engine in engines():
        if engine.status().available:
            return engine
    return None
