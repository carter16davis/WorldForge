# Coordinate conventions

Pure Python, no third-party dependencies, and no imports from `app`. This module
is where "anchor", "up axis", "heading" and "metres per model unit" are defined,
so those definitions can be read and tested without starting a web server.

Geocoding lives in `app/geocode.py` and packaging in `app/export.py`. There used
to be a second implementation of each here, plus a second web viewer and a second
HTTP server. One of each is the point.

## The two conventions

| | Origin | Axes |
| --- | --- | --- |
| `ground-center` + `Y` | footprint's horizontal centre, ground at the mesh minimum | glTF: +X east, +Y up, −Z north |
| `ground-origin` + `Z` | wherever the producing tool left it | ENU: +X east, +Y north, +Z up |

WorldForge writes the first. Photogrammetry and GIS tools emit the second.
`validate_transform` accepts both, in either combination: a producer that
declares its real frame is behaving correctly, and rejecting it only teaches
producers to claim Y-up and ship Z-up meshes.

Conversion happens where the geometry is — `reconstruction.anchor` for a scan,
`app.assets.import_mesh` for a mesh loaded into the viewer — and
`app.contracts.validate_placement` surfaces a non-canonical frame as a visible
warning until it has been converted.

## API

```python
validate_transform(flat_state)          # the exact shape the placement UI sends back
build_placement(asset_id, name, address, flat_state, *, has_lod=False)
model_to_local_enu(point, lo, hi, anchor, up_axis)   # what anchor/upAxis mean, in nine lines
local_to_enu(point, lo, hi, transform)  # the above, plus heading and scale
meters_per_degree(lat)                  # WGS84 series
enu_offset(point, origin)               # (lat, lon) -> (east, north) metres
latlon_from_enu(east, north, origin)    # and back
```

`validate_transform` rejects non-finite values, out-of-range coordinates,
non-positive scales and unrecognised anchor/axis names, and normalises heading
into `[0, 360)`. `build_placement` additionally enforces the assetId charset, so
an assetId can never become a path.

The JSON Schema is `placement.schema.json`. Runtime validation uses the same core
constraints without taking a schema dependency.

## Heading

Clockwise from true north, applied to the model to align it with the world.
Heading 0 leaves the model's own north pointing at true north.

```python
local_to_enu((0, -2, -1), (-1, -2, -1), (1, 4, 1),
             {..., "headingDegrees": 90, "metersPerModelUnit": 2})
# -> (2.0, 0.0, 0.0)   the model's north now points east
```
