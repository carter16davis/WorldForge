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

1. Drop 20+ photos taken while walking right around the building, with 60–80%
   overlap between consecutive frames.
2. Enter the address. This is required before reconstruction starts, not after:
   a mesh with nowhere to go is not map-ready, and finding that out *after* ten
   minutes of photogrammetry is worse than finding out immediately.
3. Press **Reconstruct this building**, and watch the stages:
   `analyse → geocode → reconstruct → anchor → publish`.

The result is a model measured from your photographs, anchored to the geocoded
coordinate, grounded, re-origined and ready to export.

### Engines

| Engine | When it runs |
| --- | --- |
| **RealityScan** | Found automatically on Windows, or under WSL via `wslpath`. Runs unattended with an automatic reconstruction region. |
| **External command** | Whatever you put in `WORLDFORGE_RECONSTRUCTION_CMD`. |

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
- The 2426 export carries identical geometry, coordinates and scale to the 2026
  export; the treatment is applied in the viewer and `provenance.json` records it.

## Layout

| Path | What it is |
| --- | --- |
| `app/` | FastAPI service, placement models, geocoding, export packaging |
| `web/` | Viewer, map and placement editor. Plain ES modules, no build step |
| `geospatial/` | Coordinate conventions and placement math. No dependencies, no imports from `app` |
| `reconstruction/` | Media intake, RealityScan orchestration, and georeferencing |
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
