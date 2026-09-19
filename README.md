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

## What is real and what is prepared

The capability strip along the top of the app says so on screen, and it is not
decorative — it reflects what actually answered on this machine.

- **Geocoding** and **export packaging** are always wired.
- **Reconstruction** requires RealityScan, a Windows desktop application, and a
  human to isolate the subject mid-scan. It is not driven from the web app, so
  the strip reads "prepared asset" and uploads are analysed rather than
  reconstructed.
- The prepared demo GLB is **extruded from an OpenStreetMap footprint with an
  estimated height**, not reconstructed from photographs. `provenance.json` says
  exactly that.
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
