# Person 1 reconstruction progress

Updated: 2026-09-19

## Current state and immediate next action

The 40-photo mock-building capture has completed import and alignment in
RealityScan. The log reports one component, an initial reconstruction region,
and a saved project. **No mesh has been generated for this capture.** The desktop
application is intentionally left open for manual subject isolation.

1. In RealityScan, inspect the subject and camera alignment. Select the component
   you intend to reconstruct if needed.
2. Activate **MESH & COLOR > Reconstruction Region**, or click an edge of the box
   in the 3D view.
3. Drag the outside circle handles to resize the box, quarter-circle handles to
   rotate it, and arrows to move it. Inspect from the top and several sides.
4. Keep the entire subject and its base inside, while excluding surrounding floor,
   furniture, and unrelated scene areas as much as possible. Anything unwanted
   **inside the box can still reconstruct**; the box is not a subject mask.
5. Save with **Ctrl+S** to the same `aligned.rsproj`, then close this RealityScan
   window. Keep the project sidecar folder and source PNGs in place. The Python
   prepare process waits for the window to close; that is expected.
6. From the repository root in WSL, run:

```bash
reconstruction/.venv/bin/python reconstruction/pipeline.py build \
  --project reconstruction/work/mock-building-aligned/aligned.rsproj
```

7. Type exactly `Continue` after confirming you saved and closed the adjusted
   project. There is no automatic timed continuation. Any other response cancels.

Build loads the saved component and region without resetting the region or
realigning. It creates a Normal-quality mesh, optionally simplifies, unwraps,
textures, saves a finished project and exports `building.glb` into a fresh
`exports/<run>/` folder. Review geometry and textures afterward; completion alone
is not evidence of a good reconstruction.

## Scope and collaboration

- Read the project AGENTS.md and focused on Person 1: media intake, reconstruction,
  confidence/provenance and asset handoff.
- Created branch `person1/realityscan-pipeline`.
- Current changes remain uncommitted. Shared placement/viewer code and JSON
  integration contracts have not been edited.
- Tooling lives under `reconstruction/`. Root `.gitignore` excludes `/exports/`;
  reconstruction ignores `input/`, `work/`, `.venv/`, and bytecode caches.
- RealityScan 2.2 was located at
  `C:\Program Files\Epic Games\RealityScan_2.2\RealityScan.exe`.
  This workspace runs under WSL; Python translates paths for the Windows desktop
  CLI. The automation itself is Python, not PowerShell.

## Implemented files and behavior

- `pipeline.py`: CLI entry point for `intake`, `orient`, `prepare`, `build`, and
  `package`.
- `desktop.py`: executable discovery, WSL path translation, staged desktop CLI
  commands, explicit Continue gate, logs/status and GLB export checks.
- `presets/glb.xml`: derived from this installation's real GLB export settings,
  with texture embedding enabled. It is the default for Build.
- `requirements.txt`: Pillow, NumPy, PyAV, and pillow-heif. Dependencies installed
  in the isolated `reconstruction/.venv` environment.
- `tests/`: Python tests for quality heuristics, provenance/duplicates, orientation,
  video extraction, failure cleanup, stage boundaries, Continue cancellation,
  GLB portability and launch failure reporting.
- `README.md`: commands, manual region instructions, preset setup and limitations.

### Photo intake and quality

Intake records relative filenames, sizes, SHA-256 hashes and source/license notes.
It reports exact duplicates and optionally checks decoding, possible blur/low
texture, and exposure. Checks are advisory, not automatic deletion criteria.
Geometric coverage remains unknown (`null`); no measured coverage is invented.
Near-duplicate detection and alignment-based coverage are not implemented.

### Video and orientation

Video intake samples actual decoded frames using presentation timestamps. Defaults
are a one-second interval and a 300-frame limit, both configurable. It saves PNGs,
source-video hashes, frame indices/timestamps and quality/provenance reports.
Quarter-turn rotation metadata is applied automatically; an upright video needs
no pixel rotation. Mirrored video display matrices and HDR tone mapping are not
handled. The photo `orient` command applies EXIF orientation and writes new PNG
copies with a transformation/hash manifest; originals stay unchanged.

HEIC/HEIF decoding was added for the latest photo capture. Convert these to PNG
with `orient` before passing them to RealityScan `prepare`. Intake can also
analyze HEIC directly. Keep `orientation.json` with the evidence: intake does not
automatically merge that conversion manifest into provenance.

### Reconstruction and output

Prepare imports a folder recursively, aligns, selects the largest component,
creates an initial region, saves, and leaves RealityScan visible. It sends no
mesh commands. Users may select another component and adjust the region before
saving. Build requires an explicit `Continue` after loading parameters; it uses
the saved selection/region. Existing runs and original photographs are preserved.
Logs and run metadata live in each run directory. Failed runs retain diagnostics.

Optional `--triangles N` simplifies before unwrapping/texturing so the final mesh
is textured. The default output is a self-contained GLB, not OBJ. Basic validation
rejects external resources and checks for images, textures and materials; it is
not a full glTF validator or a visual quality guarantee.

`package` copies a GLB, WebP thumbnail and intake reports into a reconstruction
handoff with the existing JSON shape. Bounds are still supplied manually. It does
not generate placement, establish metric scale, or create the thumbnail for you.

## Tests and experiments performed

### Original can video: IMG_0132.MOV

- Extracted 18 unique frames at one-second intervals.
- Five frames flagged for possible blur or low texture; no exposure flags.
- Applied the video's 90-degree clockwise metadata rotation and verified an
  upright sample. Corrected frames are 1080 by 1920.
- The former one-shot pipeline aligned two components and produced a mesh of
  661,582 triangles. The user inspected it and reported very poor geometry.
- Likely contributors included a small shiny subject, extensive background, and
  the can being moved near the end. These are plausible causes, not a confirmed
  quantitative diagnosis. Cropping alone does not repair bad alignment.
- Initial GLB export had a separate PNG and failed the portability check.
- Re-export with an embedded preset was blocked because the saved project was
  open in another instance. That attempt is retained under
  `exports/IMG_0132-embedded/` with its failure log.
- Created `exports/IMG_0132-portable/building.glb` by embedding the existing PNG
  without changing mesh or texture bytes. Embedded texture hashes were verified.
  This fixes packaging, not geometry quality.

### Latest mock-building photo capture

- Moved 40 HEIC photos from `reconstruction/input/Mock Building/` into
  `reconstruction/input/`, then removed the empty nested folder. No collisions.
- Converted all 40 to PNG with original-hash/orientation records.
- Intake found 40 unique images and no blur/exposure flags. This does not guarantee
  adequate viewpoint coverage or reconstruction quality.
- Launched the real Prepare stage: RealityScan imported 40 photos, aligned one
  component and saved `aligned.rsproj`. It is now at the mandatory manual pause.
- The existing nine-test suite passed before this alignment. A separate generated
  HEIC smoke test verified decoding, PNG conversion and original preservation.
- Full build from the manually adjusted new capture, embedded preset export in
  RealityScan, and visual mesh review are still pending.

## Current artifact locations

| Path | Purpose |
| --- | --- |
| `input/` | Original can video and 40 original HEIC photographs |
| `work/IMG_0132-upright/` | Corrected can frames and reports |
| `work/IMG_0132-mesh/` | Old can RealityScan project, GLB and separate PNG |
| `work/mock-building-photos/` | 40 normalized PNGs and orientation manifest |
| `work/mock-building-intake/` | Current confidence/provenance reports |
| `work/mock-building-aligned/aligned.rsproj` | Current project awaiting manual region adjustment |
| `../exports/IMG_0132-portable/building.glb` | Standalone can export; poor geometry unchanged |
| `../exports/IMG_0132-embedded/` | Failed desktop re-export diagnostics |

Paths above are relative to `reconstruction/`. Do not remove sidecar folders or
source images referenced by saved projects.

## Changes that were superseded or cleaned up

- Briefly implemented PowerShell automation and VS Code tasks, then removed them
  at the user's request and returned to Python.
- Removed the unsafe one-shot Python reconstruction command in favor of
  `prepare` / manual isolation / `build`.
- Briefly targeted OBJ per the detailed algorithm, then switched back to the
  project's intended GLB format with embedded textures.
- Deleted the redundant sideways-frame `work/IMG_0132-test/` folder and disposable
  Python bytecode caches. An IDE tab pointing there may now be stale.
- Original inputs, upright frames, existing mesh projects and the environment
  were retained.

## Deferred work and remaining limits

- Local image review gallery with possible-blur/exposure badges and full-size view.
- User-facing orientation preview and rotate/reset controls.
- Never automatically delete blur-flagged originals; review and record exclusions.
- Improved coverage assessment, measured bounds, thumbnails and optional LODs.
- Verify a suitable World Cup venue capture and preserve a known-good demo asset.
- Confirm sponsor format/axis/unit/origin/texture/polygon requirements and coordinate
  with Person 2 before finalizing delivery conventions.
- Validate the full adjusted-region build and embedded GLB export on real data.
- Final application integration and map placement remain outside Person 1's scope.

## References

Commands and region controls were checked against official documentation:
- https://rshelp.capturingreality.com/en-US/appbasics/allcommands.htm
- https://rshelp.capturingreality.com/en-US/tutorials/commandline_5.htm
- https://rshelp.capturingreality.com/en-US/tools/export.htm
- https://rshelp.capturingreality.com/en-US/tutorials/quickstart_5_reconstructionRegion.htm
