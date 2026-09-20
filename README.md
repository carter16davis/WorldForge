# WorldForge

Turn building photos and a street address into a 3D asset that can be placed
correctly in the world of Scorched Nebraska.

Reconstruction is a solved commodity — RealityScan does it well. What it hands
back is a mesh floating in arbitrary local space, containing the sidewalk and a
parked car, that does not know where on Earth it is, which way north is, or how
many metres tall it is. The distance between that and a map-ready asset is this
project.

## Run it

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The prepared venue loads on startup, so there is
something on screen before you click anything. Enter an address or drop photos,
adjust heading, scale and vertical offset, then export a ZIP.

```sh
.venv/bin/python -m pytest -q
```

## Reconstructing a building from photos

1. Drop **photos, a video walkaround, or both**. For photos, 20+ taken while
   walking right around the building with 60–80% overlap. For video, one
   continuous clip of the same walk — 30 seconds or more.
2. Enter the address. This is required before reconstruction starts, not after:
   a mesh with nowhere to go is not map-ready, and finding that out *after* ten
   minutes of photogrammetry is worse than finding out immediately.
3. Press **Reconstruct this building**, and watch the stages:
   `extract → analyse → geocode → reconstruct → anchor → publish`.

The result is a model measured from your own media, anchored to the geocoded
coordinate, grounded, re-origined and ready to export.

### Where it goes

A finished reconstruction is written to `web/assets/<asset-id>/` and stays
there. **Saved models** at the top of the rail lists everything published on
this machine, newest first, and opening one loads its model, placement and
provenance — so a reconstruction survives a page reload, a server restart and
the browser tab that started it. `GET /api/assets` is the same list.

Each asset is published twice. `building.glb` is the full-resolution model and
is what the export packages. `building-lod.glb` is the same geometry with the
texture atlas resampled for a browser, and is what the viewer loads; RealityScan
textures a model with a single 8192×8192 atlas, which is 40 MB of PNG and a
quarter of a gigabyte of video memory. `provenance.json` records the resampling.
Geometry is identical in both — see `reconstruction/optimize.py`.

### Video

An uploaded video is sampled into stills before reconstruction. Sampling is by
*presentation timestamp*, not decoded frame index: a phone records at a variable
frame rate, and sampling by index would cover whichever wall you walked past
slowly far more densely than the rest.

The interval is chosen from the clip's own duration to land near 80 frames,
clamped to 0.2–3.0 s. Below the floor consecutive frames are near-identical and
cost processing time without adding parallax; above the ceiling a walking pace
leaves gaps alignment cannot bridge. The upload screen shows the plan
(`~40 frames @ 0.2s`) before you commit to a slow run. Override per job with
`frameInterval` and `maxFrames` on `POST /api/reconstruct`.

Photos and video in the same upload are merged into one set. Every extracted
frame keeps a `derivedFrom` record in `provenance.json` naming the source video,
its SHA-256 and the timestamp the frame came from — a frame is evidence only
insofar as you can say which second of which file it came from.

Frames are not as good as photographs: they carry motion blur and heavier
compression. When a reconstruction came only from video, `coverage.json` says so
in its recommendations rather than leaving you to wonder why the mesh is soft.

### Engines

| Engine | When it runs | Export frame |
| --- | --- | --- |
| **RealityScan** | Found automatically on Windows, or under WSL via `wslpath`. Runs unattended with an automatic reconstruction region. | Z-up |
| **External command** | Whatever you put in `WORLDFORGE_RECONSTRUCTION_CMD`. | Y-up, or `WORLDFORGE_RECONSTRUCTION_UP_AXIS=Z` |

The export frame is not cosmetic. Every measurement `reconstruction/anchor.py`
makes is taken along the up axis — the ground plane is the lowest dense band
along it, bodies are ranked by how high they reach, the footprint is the
silhouette cast down it. RealityScan writes its own Z-up survey frame whatever
the container convention says, and anchoring one of its exports as Y-up turns a
stadium into a 79 × 12 m slab standing 38 m tall. Each engine declares its
frame; the anchor report's `upAxis` step says whether the geometry agrees.

```sh
export WORLDFORGE_RECONSTRUCTION_CMD='mytool --in {photos} --out {output}'
```

The command must write `{output}/building.glb`. That hook is the answer for
COLMAP, Meshroom or a cloud service: WorldForge does not ship drivers for those,
because an untested driver for a tool the author never ran is worse than a
documented hole.

`GET /api/engines` says which are available and why the others are not, and the
app shows that sentence rather than failing silently. If nothing is available,
`POST /api/reconstruct` returns 503 — it does not quietly hand back the prepared
asset and call it a reconstruction.

### The unattended trade-off

The CLI's `prepare` / `build` commands stop between alignment and meshing so you
can place the reconstruction region by hand, which excludes far more of the
surroundings. The web app cannot stop for that, so it runs with
`-setReconstructionRegionAuto` and records `regionMode: "automatic"` in
`provenance.json`. Cleaning up what the automatic region kept is what
`reconstruction/anchor.py`'s isolation step is for.

That step drops *sheets* unconditionally — the ground plane under the capture
and the backdrop stitched behind it, a few enormous triangles each. Beyond
that, what it does depends on how the scan split. A clean capture yields a
handful of bodies and the tallest substantial one is the building. A scan in
hundreds of pieces has no body that is the building, because photogrammetry
fragmented the subject itself, so every solid piece is kept and the report says
that nothing but the sheets was removed. Isolation never guesses which fragment
is "really" the subject: keeping a neighbour's wall is a visible mistake the
placement editor can work around, and deleting the building is not.

Under WSL, `wslpath -w` produces a `\\wsl.localhost\...` UNC path, which
RealityScan handles inconsistently. Jobs are therefore staged onto the Windows
filesystem first; set `WORLDFORGE_WIN_WORK_ROOT` if the guess is wrong.

## What is real and what is prepared

The capability strip along the top of the app says so on screen, and it is not
decorative — it reflects what actually answered on this machine.

- **Geocoding** and **export packaging** are always wired.
- **Reconstruction** is wired when an engine is available; the strip names it.
  With none, uploads are analysed but not reconstructed, and the strip says so.
- The prepared demo GLB is **extruded from an OpenStreetMap footprint with an
  estimated height**, not reconstructed from photographs. `provenance.json` says
  exactly that. Anything you reconstruct yourself is a separate asset and is
  *not* labelled that way.
- **Per-facade coverage is not reported for a real scan.** It needs the camera
  poses, which the reconstruction export does not include, so `coverage.json`
  carries the photo counts that were measured and an empty `facades` list rather
  than four invented rows.
- Coverage colours in the demo are illustrative, and the legend labels them.
- The model the viewer shows for a scan is the **resampled** one. Its geometry
  is identical to the export's; only the texture is sampled more coarsely, and
  `provenance.json` gives both sizes and the resampling factor.
- The 2426 export carries identical geometry, coordinates and scale to the 2026
  export; the treatment is applied in the viewer and `provenance.json` records it.

## Layout

| Path | What it is |
| --- | --- |
| `app/` | FastAPI service, placement models, geocoding, export packaging |
| `web/` | Viewer, map and placement editor. Plain ES modules, no build step |
| `geospatial/` | Coordinate conventions and placement math. No dependencies, no imports from `app` |
| `reconstruction/` | Media intake, RealityScan orchestration, georeferencing, and the browser-sized copy of the result |
| `docs/INTEGRATION.md` | What each layer hands the next |

## Reconstruction to placement

The full operator path, and the `anchor` step that closes the gap between a scan
and an asset:

```sh
.venv/bin/python -m reconstruction.pipeline anchor \
    --model exports/<run>/building.glb --output work/anchored \
    --asset-id venue-001 --latitude 40.8135 --longitude -74.0745 \
    --footprint app/data/metlife_footprint.json --reference-meters 250
```

It detects the ground plane, drops everything that shared the reconstruction
region, measures the mesh's silhouette, solves metric scale and heading against a
surveyed footprint, and re-origins the mesh to ground-centre Y-up. Every step
records its method and confidence, and a step it cannot solve from evidence is
reported unsolved rather than guessed — an unsolved heading becomes a control in
the placement editor, not a confident number in a file.

See [docs/INTEGRATION.md](docs/INTEGRATION.md) for the commands before and after
it, and [AGENTS.md](AGENTS.md) for the project plan and export contract.

## Notes on sources

Address search uses OpenStreetMap Nominatim, serialized and rate-limited to one
request per 1.1 s with a per-process cache, per the
[Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/).
The prepared venue table is consulted first, so the demo path needs no network at
all. Map tiles follow the
[OSM tile policy](https://operations.osmfoundation.org/policies/tiles/) with
visible attribution and no prefetching. Avoid entering confidential addresses
into live search.
