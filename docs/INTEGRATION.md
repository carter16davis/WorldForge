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

## What the web app probes for

`app.pipeline` imports the `reconstruction` package at call time and looks for
these names. Anything absent is reported as not wired in the capability strip,
and the app falls back to the prepared asset.

| Name | Signature | Present today |
| --- | --- | --- |
| `analyse_media` | `(paths: list[str]) -> dict` | yes |
| `anchor_model` | see below | yes |
| `reconstruct` | `(paths: list[str], address: str) -> dict \| None` | **no** |
| `coverage_report` | `(asset_id: str) -> dict` | **no** |

The last two are deliberately undefined rather than stubbed. `reconstruct` would
have to drive RealityScan, a Windows desktop application that requires a human to
isolate the subject between alignment and meshing; `coverage_report` would need
camera poses the current export does not include. A stub returning `None` or
zeros makes the capability strip read green for something that never works, which
is worse than an honest red.

### `analyse_media` result

```json
{
  "files": [{"name": "", "kind": "photo|video|other", "issues": [], "sizeBytes": 0}],
  "usableCount": 0, "totalCount": 0, "geotaggedCount": 0,
  "notes": [], "provider": ""
}
```

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
.venv/bin/python -m reconstruction.pipeline build   --project work/scans/<run>/aligned.rsproj
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
