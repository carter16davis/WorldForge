# WorldForge / MegFault

Turn building photos and an address into a geographically placed 3D asset.

## Integrated frontend

Frontend imported from `origin/frontend` commit `0bf6c2c`, with geospatial adapters for address search, placement validation, and export. This is the combined app with the stadium GLB viewer, map, World Cells, and era controls.

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8002
```

Open http://127.0.0.1:8002. Geocoding and export packaging should be green in the capability strip; hover over them to see the `geospatial` provider. Search a place, choose a match if needed, adjust the transform, and export a ZIP. Each export has an immutable snapshot URL; previous downloads remain available after further edits. The standalone placement playground below is still available.

The demo GLB is generated from a footprint with estimated height, not reconstructed from photos. Coverage colors are demonstration data. The 2426 export preserves the same geometry and records that its visual treatment is applied in the viewer. Only Y-up, ground-center assets are currently supported by the integrated geospatial export; other conventions require conversion before export.

```sh
.venv/bin/python -m pytest -q
```

Start the visual placement playground:

```sh
python3 -m geospatial.server
```

Open http://127.0.0.1:8000. Search for a place, select a result, then drag the anchor and adjust heading, scale, or height. Download the resulting placement JSON. The building block is an illustrative placeholder; real GLB loading comes later. Internet access is needed for the street map and live address search; the prepared example and local block preview also work without those services.

- Project plan and teammate contracts: [AGENTS.md](AGENTS.md)
- Placement module, export commands, and coordinate conventions: [geospatial/README.md](geospatial/README.md)

Generate demo placement metadata (Python 3.10+, no dependencies):

```sh
python3 -m geospatial --asset-id venue-metlife-001 --name "New York New Jersey Stadium" --address "MetLife Stadium" --elevation 0
```

The demo uses approximate prepared coordinates and an explicit relative elevation of zero. See the module documentation before exporting a real asset.

```sh
python3 -m unittest discover -s tests -v
```
