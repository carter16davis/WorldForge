# Geospatial placement

Standalone Python 3.10+ module with no third-party dependencies. The frontend can use the flat placement object in `AGENTS.md`; reconstruction can supply a prepared GLB independently.

## Run

### Visual playground

```sh
python3 -m geospatial.server
```

Visit http://127.0.0.1:8000 (or set `--port 8001`). The server binds only to localhost. Stop it with Ctrl+C.

1. The prepared stadium location opens automatically with an illustrative building footprint.
2. Search an address or public building name and explicitly choose a match. Live queries go to OpenStreetMap's Nominatim service; avoid entering confidential addresses. The prepared stadium lookup does not need a network request.
3. Drag the anchor, click the map, or enter coordinates to correct the position.
4. Adjust heading, scale, and height. The map shows a footprint and forward line; the local block preview also visualizes the vertical offset.
5. Download `placement.json`, validated by the Python placement module. This downloads metadata only, not a model or a full package.

The footprint is a made-up 60 × 90 model-unit rectangle, 30 units tall. It is neither a surveyed footprint nor a reconstructed stadium. Projection uses a short-distance spherical approximation and supports latitudes from -85 to 85 degrees. Ground elevation remains explicitly zero in a relative demo datum. Manual position edits preserve the selected place label; they do not perform reverse geocoding.

Leaflet is loaded from a pinned CDN release and OpenStreetMap provides map tiles. Fonts are optional and have local fallbacks. If the map library or tiles cannot load, the local SVG building preview, prepared lookup, coordinates, and export remain available. Live geocoder failures show a fallback message without replacing the current selection.

Live requests are user-triggered, serialized, rate-limited, and cached in memory for the server session, with attribution shown in the interface, following the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/). This is a single-process local demo, not a deployed public geocoding service. Set `WORLDFORGE_GEOCODER_URL` to switch to another compatible search endpoint. Tiles follow the [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/) with visible attribution and normal browser caching; there is no prefetch or offline tile download.

API routes for frontend integration:

- `GET /api/search?q=...` returns candidate `label`, `latitude`, `longitude`, and `source` values.
- `POST /api/preview` accepts `assetId`, `name`, `sourceAddress`, and the existing flat placement shape under `transform`. It returns validated `placement`, geographic `footprint` corners, a geographic `front` point, and `localCorners` in east/north/up meters. These preview fields do not change the shared export contract.

### Command line

From the repository root, generate placement JSON using the prepared venue lookup:

```sh
python3 -m geospatial --asset-id venue-metlife-001 --name "New York New Jersey Stadium" --address "MetLife Stadium" --elevation 0
```

The prepared coordinates are copied from the project plan, approximate, and not independently verified. Zero elevation here is a relative demo datum, not a measured height above sea level. Record the chosen datum and coordinate source in provenance license/source notes. Unknown addresses produce an error; the module never substitutes the demo venue for an unsuccessful lookup.

For any other address, provide reviewed coordinates:

```sh
python3 -m geospatial --asset-id building-001 --name "My building" --address "Reviewed source address" --latitude 40.8135 --longitude -74.0745 --elevation 0 --heading 90 --scale 1 --offset 0
```

To export, append these arguments to either command (supply existing input files):

```sh
--model path/to/building.glb --thumbnail path/to/thumbnail.webp --provenance path/to/provenance.json --output exports
```

Optional `--lod path/to/building-lod.glb` adds a lower-detail asset. The package is written to `exports/<asset-id>/`; existing packages are never intentionally overwritten. Export checks file headers, placement values, and provenance version. It does not perform full GLB/WebP validation, mesh conversion, texture inspection, or rendering checks.

## Teammate interfaces and conventions

`geocode_prepared(address)` returns latitude, longitude, and a source description. A future live provider should return candidates for user review rather than choose an ambiguous address automatically. No paid provider or API credentials are required for the prepared path.

`validate_transform(flat_state)` accepts the exact placement UI shape in `AGENTS.md`, rejects invalid coordinates/nonfinite values/nonpositive scales, and normalizes headings to [0, 360). `build_placement(asset_id, name, address, flat_state)` generates the nested export shape. The JSON Schema is `placement.schema.json`; runtime validation uses the same core constraints without a schema dependency.

- Latitude/longitude use WGS84 decimal degrees. Elevation is an explicitly supplied ground height; the caller must document its datum. Vertical offset is added above that ground.
- Units are meters per model unit. Bounds from reconstruction should be measured in model units before applying scale.
- Models are Y-up. At heading zero, model -Z faces north and +X faces east; heading increases clockwise from north. A model's visually meaningful front must be established by reconstruction or corrected manually.
- Ground-center is the center of the model's X/Z bounds at its minimum Y. `local_to_enu(point, bounds_min, bounds_max, flat_state)` recenters a point, scales it, rotates it, and returns east/north/up meters relative to the geographic anchor. It does not project onto a map or convert local offsets into latitude/longitude.
- Export copies the GLB unchanged. Reconstruction must bake the ground-center origin into its GLB before export; the function for local points is a viewer/processing helper, not an automatic mesh rewrite. Avoid applying that recentering twice.
- Sponsor coordinate, texture, and polygon requirements still need confirmation before finalizing these conventions.

## Scope and next steps

The local viewer connects address search and a map to the core placement/export slice. Actual GLB rendering, terrain elevation, measured footprints, and verified venue alignment remain to be connected. Geohash indexing/heatmaps stay in the planned differentiation phase; the illustrative hashes in `AGENTS.md` must not be treated as computed values for its example coordinates. The core schema deliberately omits that proposed extension until implementation and team agreement.

Run the tests:

```sh
python3 -m unittest discover -s tests -v
```
