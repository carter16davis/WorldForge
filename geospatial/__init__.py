"""WorldForge coordinate conventions: anchors, up axes, heading and metric scale.

Pure Python, no third-party dependencies, and no imports from `app`. The app
layer depends on this module; this module depends on nothing, so the conventions
can be read, tested and cited without starting a web server.

Geocoding lives in `app.geocode` and packaging in `app.export`. Both used to
have a second implementation here; one of each is the point.
"""

from .placement import (
    ANCHORS,
    UP_AXES,
    build_placement,
    enu_offset,
    latlon_from_enu,
    local_to_enu,
    meters_per_degree,
    model_to_local_enu,
    number,
    validate_transform,
)

__all__ = [
    "ANCHORS",
    "UP_AXES",
    "build_placement",
    "enu_offset",
    "latlon_from_enu",
    "local_to_enu",
    "meters_per_degree",
    "model_to_local_enu",
    "number",
    "validate_transform",
]
