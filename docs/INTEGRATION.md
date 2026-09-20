# Integration contract

What each part of WorldForge hands the next, and what the web app will call if it
finds it. `app/pipeline.py` refers to this file; before, the file did not exist.

## Layering

```
reconstruction/   media intake, RealityScan, georeferencing   depends on: geospatial
geospatial/       coordinate conventions and placement math   depends on: nothing
app/              models, geocoding, export, HTTP API         depends on: geospatial
web/              viewer, map, placement editor               depends on: app's HTTP API
```

`geospatial/` imports nothing from `app/`. That rule is what makes the
conventions readable and testable without starting a web server, and it is the
rule that a previous `geospatial.integration` module broke by reaching into
`app.geocode._parse_coordinates`.

## The one placement shape

`placement.json`, schema version 1, exactly as frozen in AGENTS.md. Two things
are worth stating because they used to differ between modules:

| Field | Meaning |
| --- | --- |
| `transform.anchor` | `ground-center` (origin at the footprint's horizontal centre, ground at the mesh minimum) or `ground-origin` (origin wherever the producer left it) |
| `transform.upAxis` | `Y` (glTF: +X east, +Y up, −Z north) or `Z` (ENU: +X east, +Y north, +Z up) |

Both values of each are **accepted**, not rejected. A producer declaring `Z` and
`ground-origin` truthfully is correct behaviour; the alternative is a producer
that learns to claim `Y` and ship a Z-up mesh. `validate_placement` raises the
non-canonical frame as a visible warning, `reconstruction.anchor` converts the
geometry, and the viewer applies a defensive rotation for anything that reaches
it unconverted.

Extension fields (`footprint`, `holes`, `dimensions`, `appearance`) are optional
everywhere. A consumer that ignores them still gets a valid map-ready placement.

### What the exported placement says

Inside the app the transform is live: `metersPerModelUnit` is whatever the user
has dialled in, and `upAxis` is whatever the producer declared. **The export
normalises both.** `app.export.bake_placement_scale` applies the scale and any
Z-up correction to every GLB in the package as a glTF node transform, and the
`placement.json` that ships beside them therefore reads:

```json
"transform": { "headingDegrees": 87, "metersPerModelUnit": 1,
               "verticalOffsetMeters": -2.5, "anchor": "ground-center",
               "upAxis": "Y" }
```

Three consequences for a consumer:

- Loading `building.glb` and ignoring the JSON entirely gives a building in
  metres, upright, at the size the editor showed.
- `dimensions` and `boundingBoxMeters` in the exported placement are measured
  off those bytes, not carried over from the reconstruction.
- Heading and vertical offset are still the consumer's job. They position the
  building; they do not describe it.

`manifest.json`'s `model` block names the factor that was baked
(`bakedTransform`), and `provenance.json` repeats it under `exportTransform`.
When a model cannot be rewritten, the package exports un-baked with the scale
left in `placement.json` and the reason in the manifest's `problems` — it never
ships a model whose size is a guess.

## What the web app probes for

`app.pipeline` imports the `reconstruction` package at call time and looks for
these names. Anything absent is reported as not wired in the capability strip,
and the app falls back to the prepared asset.

| Name | Signature | Present today |
| --- | --- | --- |
| `anchor_model` | see below | yes |
| `coverage_report` | `(asset_id: str) -> dict` | **no** |

`coverage_report` is deliberately undefined rather than stubbed: per-facade
coverage needs camera poses the reconstruction export does not include, and a
stub returning zeros makes the capability strip read green for something that
never works. A real scan's `coverage.json` therefore carries the counts that
*were* measured and an empty `facades` list.

Reconstruction itself is not a function call. It takes minutes, so it runs as a
background job.

## Reconstruction jobs

```
POST /api/upload       photos + address  ->  { batchId, files[], engines[], canReconstruct }
POST /api/reconstruct  { batchId, address | latitude+longitude, referenceMeters? }
                                         ->  { jobId, assetId, engine }
GET  /api/jobs/{id}    ->  { status, stage, progress, detail, log[], asset? }
GET  /api/engines      ->  { engines[], canReconstruct }
```

A job's `asset` is available only while the tab that started it is polling. What
outlives it is the directory under `web/assets`:

```
GET  /api/assets       ->  { assets[] }   cards, newest first
GET  /api/assets/{id}  ->  the same bundle shape as /api/session's `asset`
POST /api/place        ->  works for any published asset, not only the prepared one
```

An `assetId` reaching either of those is matched in full against
`app.assets.ASSET_ID` before it is joined to a path.

An upload batch is kept on disk under `uploads/batch-*/source/`, because the job
re-reads the originals. It used to be deleted at the end of the upload request,
which is why nothing could ever be reconstructed from it.

`POST /api/reconstruct` requires a location and refuses without one (422). A mesh
with nowhere to go is not map-ready, and discovering an unresolvable address
after ten minutes of photogrammetry is worse than discovering it immediately.
With no engine available it returns 503 rather than handing back the prepared
asset.

Job stages, and roughly what each costs:

| Stage | Share | What it does |
| --- | --- | --- |
| `extract` | 6% | video to frames, merged with any uploaded photos |
| `analyse` | 4% | blur, exposure, exact duplicates, EXIF, SHA-256 per image |
| `geocode` | 1% | address to coordinates |
| `reconstruct` | 79% | the engine |
| `anchor` | 7% | ground, isolation, scale, heading, re-origin |
| `publish` | 3% | write the asset where the viewer loads it, and a browser-sized copy beside it |

`publish` writes `web/assets/<asset-id>/` with `building.glb` (full resolution,
what the export packages), `building-lod.glb` (the same geometry, texture
resampled — `reconstruction/optimize.py`, declared as `models.low` and recorded
under `provenance.webModel`), `placement.json`, `provenance.json`,
`coverage.json` and `thumbnail.webp`. A failed resample is not a failed publish;
the asset then has no `models.low` and the viewer loads the full model.

The engine declares the frame its export is in — `RealityScanEngine.up_axis` is
`Z` — and the job passes it to `anchor_model`. Every measurement the anchor makes
is taken along that axis, so a wrong value there lays the building on its side
while every number in the report still looks reasonable. The report's `upAxis`
step records whether the geometry agrees with the declaration.

Job state lives in `uploads/jobs/<id>/job.json`, not in memory, so a restart
leaves a record. A job left `running` by a restart is marked failed when the
store reloads — a running job with no thread behind it would poll forever.

## Media assembly

`reconstruction/media.py` turns an upload into one folder of stills.

- `classify(dir)` splits photos, videos and unusable files.
- `probe(video)` returns duration, frame rate, resolution and codec, or an
  `error` explaining why the file cannot be read.
- `plan_interval(duration)` picks a sampling interval targeting
  `TARGET_FRAMES` (80), clamped to `MIN_INTERVAL_S`..`MAX_INTERVAL_S`
  (0.2–3.0 s).
- `assemble(source, work, ...)` returns a `MediaSet` and writes `work/media/`.
- `merge_provenance(provenance, media_set)` folds the derivation back in.

Sampling uses presentation timestamps via `pipeline.intake_video`, so a
variable-frame-rate recording samples evenly in *time*. Frames from different
clips are prefixed with the source stem so two videos whose internal frame names
both start at `frame_000001.png` cannot overwrite each other.

`assemble` raises only when nothing usable came out. One unreadable video is
recorded in `MediaSet.skipped` and the rest proceeds — losing one clip should not
discard a good set of photos.

After the merge, `provenance.json` carries:

```json
{
  "sourceMedia": [
    {"path": "walkaround.mp4", "kind": "video", "sha256": "...",
     "extraction": {"tool": "PyAV", "intervalSeconds": 0.2, "frameLimitReached": false},
     "framesExtracted": 40},
    {"path": "walkaround__frame_000001.png", "kind": "frame", "sha256": "...",
     "derivedFrom": {"sourceVideo": "walkaround.mp4", "sha256": "...",
                     "presentationTimestampSeconds": 0.0}},
    {"path": "corner.png", "kind": "photo", "sha256": "..."}
  ],
  "mediaSummary": {"photos": 1, "frames": 40, "total": 41, "videos": [...], "skipped": []}
}
```

## Engines

`reconstruction/engine.py` exposes `engine_status()` and `select_engine()`.

**RealityScan** is found via `RealityScan.exe` on PATH or under
`%ProgramFiles%/Epic Games/RealityScan*/`. From WSL it is launched through
interop with `wslpath` translation. The web app runs it unattended with
`-setReconstructionRegionAuto` and records `regionMode: "automatic"` in
provenance; for a hand-placed region use the interactive `prepare`/`build` CLI.

**External command** runs `WORLDFORGE_RECONSTRUCTION_CMD`, which must contain
`{photos}` and `{output}` and write `{output}/building.glb`. It is split with
`shlex` and run without a shell. This is the hook for COLMAP, Meshroom or a
cloud service, and it is what makes the job pipeline testable on a machine with
no RealityScan — see `tests/test_jobs.py`.

Environment:

| Variable | Effect |
| --- | --- |
| `WORLDFORGE_RECONSTRUCTION_CMD` | external engine command template |
| `WORLDFORGE_WIN_WORK_ROOT` | where to stage photos so Windows sees a drive path, not a UNC path |
| `WORLDFORGE_RECONSTRUCTION_STALL_S` | kill a run that reports no progress for this long (default 1800) |
| `WORLDFORGE_RECONSTRUCTION_DETAIL` | RealityScan meshing quality: `high` (default), `normal`, `preview` |
| `WORLDFORGE_RECONSTRUCTION_TRIANGLES` | triangles kept after meshing (default 1,000,000; `0` keeps every one) |

`POST /api/reconstruct` takes `detail` per job, and `engine.with_detail(engine,
detail)` is what applies it — a backend with no such knob, like the external
command, is returned unchanged rather than refused. The meshing commands
themselves live in `reconstruction/desktop.py` (`mesh_command`), so the
unattended path and the interactive CLI cannot drift apart.

### `anchor_model`

```python
anchor_model(model_path, latitude, longitude, *, asset_id,
             name="", address="", osm_footprint=None, reference_meters=None,
             elevation_meters=0.0, up_axis="Y", out_dir=None)
    -> {"placement": {...}, "report": {...}}
```

Returns a canonical placement (`ground-center`, `upAxis: "Y"`, metres) and an
anchor report. With `out_dir`, also writes `building.glb` and
`anchor-report.json`.

The report's `steps` each carry `solved`, `value`, `method`, `confidence` and
`note`. **An unsolved step is not a failure.** Scale and heading cannot be
recovered from a mesh alone — camera EXIF locates the photographer, not the
building — so without `osm_footprint` or `reference_meters` they are reported
unsolved and become controls the user moves in the placement editor. They never
become a confident guess.

`report.unresolved` lists those names, and `report.boundsMeters.measured` says
whether the dimensions are in metres or in arbitrary model units.

## Operator CLI

```sh
# intake -> orient -> align -> (isolate the subject by hand) -> mesh -> anchor -> package
.venv/bin/python -m reconstruction.pipeline intake  --photos IN --output work/intake --license-notes "..."
.venv/bin/python -m reconstruction.pipeline orient  --photos IN --output work/upright
.venv/bin/python -m reconstruction.pipeline prepare --photos work/upright
.venv/bin/python -m reconstruction.pipeline build   --project work/scans/<run>/aligned.rsproj --detail high
.venv/bin/python -m reconstruction.pipeline anchor  --model exports/<run>/building.glb \
    --output work/anchored --asset-id venue-001 \
    --latitude 40.8135 --longitude -74.0745 \
    --footprint app/data/metlife_footprint.json --reference-meters 250
.venv/bin/python -m reconstruction.pipeline package --model work/anchored/building.glb \
    --thumbnail t.webp --report work/anchored --output exports/venue-001 --asset-id venue-001
```

`package` reads its bounds from `anchor-report.json` when `--bounds` is omitted,
and refuses if that run did not solve a metric scale. Typing `--bounds 68 40 15`
by hand is how a 68-metre stadium ships as 68 model units.
