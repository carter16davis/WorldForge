# Person 1: RealityScan reconstruction

This directory owns offline source intake and reconstruction handoff only. It does
not modify the shared contracts, frontend, placement module, or root dependencies.
Requires Python 3.11+. Basic intake and packaging use the standard library;
optional image-quality checks use Pillow and NumPy; video uses PyAV in the same
dedicated environment.
Use a separate clone/worktree if teammates run agents on the same machine.

## 1. Record the input

Keep original photos and videos outside Git. From the repository root:

```bash
.venv/bin/python -m reconstruction.pipeline intake --photos /path/to/photos --output reconstruction/work/intake --license-notes "Captured by NAME on DATE; permission and attribution details"
```

For image-quality checks:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m reconstruction.pipeline intake --photos /path/to/photos --output reconstruction/work/reviewed-intake --license-notes "Captured by NAME on DATE; permission and attribution details" --check-quality
```

On Windows use `py` to create the environment and
`reconstruction\.venv\Scripts\python.exe` for subsequent commands.

Quality checks resize grayscale images to a maximum dimension of 1024. Laplacian
variance below 100 flags possible blur or low texture. Mean luminance below 45 or
above 210, or over half of pixels near black/white, flags possible exposure issues.
These advisory heuristics need calibration on real photos; smooth walls, shadows,
and skies can trigger false positives. Unreadable images are reported individually.

This hashes supported photo files, records relative paths and sizes, and reports
exact duplicates. It never removes originals. Without `--check-quality`, decoding,
blur and lighting are marked unperformed. Near-duplicate detection and geometric
coverage analysis remain pending. File extensions are used for discovery, not
proof that images are valid. Coverage stays unknown rather than being invented.
Use this initial path only for original, non-synthetic source photographs.

### Video input

Install/update dependencies using the command above, then pass `--video` instead
of `--photos`:

```bash
.venv/bin/python -m reconstruction.pipeline intake --video /path/to/capture.mp4 --output reconstruction/work/video-intake --license-notes "Recorded by NAME on DATE; permission and attribution details" --frame-interval 1 --max-frames 300 --check-quality
```

This samples the first video stream, saving full-resolution PNG frames in
`reconstruction/work/video-intake/frames/`, with the two JSON reports alongside.
Import those PNGs into RealityScan. Start with one frame per second and review
overlap; use a smaller interval if your camera moved too far between frames.
The default limit is 300 frames. If more sampled frames remain, the report records
the limit and recommends increasing it. No interpolated frames are generated.

Each extracted image records its hash, source-video hash, decoded frame index,
actual presentation timestamp, and elapsed time since the first decoded frame.
Sampling uses decoded timestamps rather than assuming a constant frame rate.
Retain the original video separately: it is recorded by name/hash but is not copied.
PNG paths resolve relative to the intake directory. Packaged provenance remains
an evidence inventory; source media is not copied into the final GLB package.

PyAV decodes the video in Python; a standalone FFmpeg executable is not required
when using its prebuilt wheels. Supported containers/codecs depend on that build.
Corrupt, unsupported, or timestamp-less input produces an error and cleans up staged
output. Quarter-turn video display rotations are automatically applied and recorded
per frame; unsupported angles fail explicitly. Mirrored display matrices are not
handled. Inspect extracted frames for unusual metadata. HDR color is not explicitly tone-mapped;
use a standard SDR recording for the initial capture.

### Correct existing photos

HEIC/HEIF input is supported through `pillow-heif` in the project's `requirements.txt`. Run
`orient` first to create PNG copies before RealityScan `prepare`; keep its
`orientation.json` with the original evidence. The intake quality checks can also
read HEIC directly. Original files are preserved.

```bash
.venv/bin/python -m reconstruction.pipeline orient --photos reconstruction/work/photos --output reconstruction/work/upright-photos
```

This applies EXIF orientation (including mirrored photo orientations) and writes
PNG copies with the orientation tag removed, preserving other EXIF metadata.
`orientation.json` records original and corrected hashes and transformations. Keep
this manifest with the evidence; the intake command does not automatically merge
it into provenance. Originals are never edited. Unreadable photos abort the command
without publishing partial output. Output must be outside the source directory.

For previously extracted sideways frames without EXIF, explicitly add `--rotate 270`
for a clockwise quarter-turn (`--rotate` is counterclockwise). For new video intake,
rotation is automatic; do not rotate those corrected frames a second time.

Do not automatically delete blur-flagged images. Review them at full size, compare
nearby frames, and exclude truly blurred images from the reconstruction selection
only when sharper overlapping views exist. Preserve original evidence. Smooth
surfaces can trigger the blur heuristic even when in focus.

## 2. Reconstruct in RealityScan Desktop

Run from the repository root in your existing Python environment. The Python
script launches the installed RealityScan desktop application using its CLI;
it automatically translates paths when running in this WSL workspace.

### Stage 1: prepare and align

```bash
.venv/bin/python -m reconstruction.pipeline prepare --photos reconstruction/work/IMG_0132-upright/frames
```

Or supply any photo directory. A unique run folder is created under
`reconstruction/work/scans/`; optionally choose a new folder with `--output`.
RealityScan imports, aligns, selects the largest component, sets an initial region,
and saves `aligned.rsproj`. It stays visible and the script waits for you to close
that instance. No mesh commands are sent in this stage.

### Mandatory manual pause

Inspect alignment and select the component representing your subject. Activate
**MESH & COLOR > Reconstruction Region** or click a region edge. Drag the outside
circle handles to resize, quarter-circle handles to rotate, and arrows to move.
Check the box from several viewpoints, keeping the subject/base inside and unwanted
surroundings outside. Unwanted geometry **inside the box** may still reconstruct.

**Save the adjusted project and close that RealityScan window.** Keep its sidecar
folder and source photos in place. The script cannot judge whether your region is
correct: you must inspect it. Alignment failure should be resolved before meshing.

### GLB export settings

`reconstruction/presets/glb.xml` is the default preset, derived from this machine's
actual RealityScan 2.2 GLB export info. Its `embedTextures` setting is enabled so
textures are stored inside `building.glb`. Build rejects non-GLB or non-embedded
presets and checks the result for external resources and missing texture data.

To replace the preset for another RealityScan version, manually export a textured
GLB with **Embed textures** and **Export an info file** enabled. Copy the complete
`ModelExport` element from its `.rsInfo` into an XML file and supply
`--export-settings /path/to/glb.xml`. Do not use an OBJ preset.
See [official export documentation](https://rshelp.capturingreality.com/en-US/tools/export.htm).

### Stage 2: build and export

Substitute the actual prepared project path printed by Stage 1:

```bash
.venv/bin/python -m reconstruction.pipeline build --project reconstruction/work/scans/YOUR-RUN/aligned.rsproj
```

Type exactly **Continue** to confirm you isolated the subject, saved, and closed
its window. Anything else cancels. There is no timer or unattended confirmation
flag. You can close the Python script and resume later with this same command.
Resolve any project autosave in RealityScan before resuming.

Build loads your saved selected component and region without realigning or resetting
it, creates a Normal-quality mesh, unwraps, textures, and exports a self-contained textured GLB.
Optional `--triangles 100000` simplifies **before** unwrap and texture; no target is
imposed by default. A new `exports/<run>/` inside this workspace contains the model,
textures, finished project and sidecars, `run.json`, `realityscan.log`, and
`progress.log`. The model and texture images are embedded in `building.glb`. Paths print on completion.
Exporting into VS Code does not automatically provide a 3D preview.

Both stages accept `--realityscan /path/to/RealityScan.exe` and `--dry-run`.
Dry runs only print commands and do not launch the app. Existing output folders are
never overwritten. Inputs and the saved adjusted project are preserved. The script
stops on a nonzero desktop exit and checks project/export files before reporting
success. Failed runs retain logs and partial output for diagnosis. Basic file checks
cannot establish mesh quality. Native Windows Python also works, but no PowerShell
scripts or VS Code tasks are required.

CLI commands verified against the [official command list](https://rshelp.capturingreality.com/en-US/appbasics/allcommands.htm).
The resulting `building.glb` can be passed directly to the packaging command below. Coordinate final units,
origin and format with Person 2 and the sponsor.

## 3. Anchor the scan to the Earth

This is the step that turns a scan into an asset. RealityScan's mesh is in
arbitrary local space: it does not know where it is, which way north is, how
large it is, or where its ground plane sits, and it still contains whatever
shared the reconstruction region.

```bash
.venv/bin/python -m reconstruction.pipeline anchor \
    --model exports/YOUR-RUN/building.glb --output reconstruction/work/anchored \
    --asset-id venue-example-001 --latitude 40.8135 --longitude -74.0745 \
    --address "1 MetLife Stadium Dr, East Rutherford, NJ" \
    --footprint app/data/metlife_footprint.json --reference-meters 250
```

It writes an anchored `building.glb` (ground at y=0, footprint centre at the
origin, Y-up, metres), a `placement.json`, and an `anchor-report.json` recording
each step's method and confidence. The command prints that report:

```text
  OK   groundPlane = 3.668  (lowest-dense-band, confidence 0.95)
  OK   isolation = 0.0402   (largest-vertical-body, confidence 0.6)
  OK   scale = 10.0         (reference-measurement, confidence 0.95)
  OK   heading = 205.0      (footprint-match, confidence 0.9)
```

`--footprint` is a JSON file of `[[lat, lon], ...]` for the real building, used
to solve both scale and heading. `--reference-meters` is one measured real-world
length along the building's longest horizontal axis and overrides the footprint
match for scale, because a tape measure beats a polygon.

Neither is required. Without them the geometry is still cleaned, grounded and
re-origined, and scale and heading are reported **unsolved** — they become
controls the user moves in the placement editor rather than numbers this tool
invented. Photogrammetry genuinely cannot recover them: camera EXIF GPS locates
the photographer, not the building.

A near-symmetric footprint cannot resolve the 180-degree flip from overlap alone.
The report says so, with both orientations' scores, instead of picking one and
looking certain. Check a known facade in the editor.

## 4. Package the handoff

Bounds are read from `anchor-report.json` in metres. Pass `--bounds W L H`
only to override them; packaging refuses measured bounds from an anchor run that
did not solve a metric scale, because those are in arbitrary model units.

```bash
.venv/bin/python -m reconstruction.pipeline package --model /path/to/building.glb --thumbnail /path/to/thumbnail.webp --report reconstruction/work/intake --asset-id venue-example-001 --output reconstruction/work/venue-example-001
```

Output contains `building.glb`, `thumbnail.webp`, `provenance.json`, `confidence.json`,
and `reconstruction.json` matching the existing reconstruction handoff shape. Paths
in the handoff resolve relative to its directory; agree on this interpretation with
teammates before integration. Both commands refuse to overwrite an existing output
directory. The GLB check verifies its container and rejects external resource URIs;
it is not a full glTF validator or proof of correct rendering. The thumbnail check
only identifies its container.

Preserve a known-good venue package for judging. The next coding task is
alignment-based coverage, which needs camera poses the current export does not
include.

## Verification

```bash
.venv/bin/python -m pytest reconstruction/tests -v
```

## Deferred follow-up

- Keep existing metadata-based orientation behavior for now. Revisit user-facing
  Auto / Rotate left / Rotate right / Reset preview controls after the first GLB.
- Add a local HTML frame-review gallery showing thumbnails, filenames, possible
  blur/exposure badges and full-size inspection. Reuse confidence report data;
  coordinate application UI integration with Person 3.
- Keep blur flags advisory. Preserve originals and record any manual exclusions.
