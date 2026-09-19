"""Geohash encode/decode/neighbours — pure stdlib.

Person 2 owns the authoritative spatial index. This module exists so the
viewer and the export packager keep working before that module lands, and so
the World Cell overlay has something deterministic to draw. If Person 2 ships
`geospatial.geohash`, `app.pipeline` prefers it and this becomes dead weight.
"""

from __future__ import annotations

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DECODE = {c: i for i, c in enumerate(_BASE32)}

# neighbour lookup tables, indexed [direction][parity] where parity 0 = even length
_NEIGHBOUR = {
    "n": ("p0r21436x8zb9dcf5h7kjnmqesgutwvy", "bc01fg45238967deuvhjyznpkmstqrwx"),
    "s": ("14365h7k9dcfesgujnmqp0r2twvyx8zb", "238967debc01fg45kmstqrwxuvhjyznp"),
    "e": ("bc01fg45238967deuvhjyznpkmstqrwx", "p0r21436x8zb9dcf5h7kjnmqesgutwvy"),
    "w": ("238967debc01fg45kmstqrwxuvhjyznp", "14365h7k9dcfesgujnmqp0r2twvyx8zb"),
}
_BORDER = {
    "n": ("prxz", "bcfguvyz"),
    "s": ("028b", "0145hjnp"),
    "e": ("bcfguvyz", "prxz"),
    "w": ("0145hjnp", "028b"),
}


def encode(lat: float, lon: float, precision: int = 8) -> str:
    """Latitude/longitude -> geohash cell of the requested precision.

    Boundary coordinates round toward the higher cell (`>=`), matching the
    reference implementations. Person 2's index must use the same tie-break or
    the two halves of the pipeline will disagree on cells along a cell edge.
    """
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    out: list[str] = []
    bit = 0
    ch = 0
    even = True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch = (ch << 1) | 1
                lon_lo = mid
            else:
                ch <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch = (ch << 1) | 1
                lat_lo = mid
            else:
                ch <<= 1
                lat_hi = mid
        even = not even
        bit += 1
        if bit == 5:
            out.append(_BASE32[ch])
            bit = 0
            ch = 0
    return "".join(out)


def bounds(cell: str) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a geohash cell, in degrees."""
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    even = True
    for c in cell:
        try:
            val = _DECODE[c]
        except KeyError as exc:
            raise ValueError(f"{cell!r} is not a geohash: bad character {c!r}") from exc
        for mask in (16, 8, 4, 2, 1):
            if even:
                mid = (lon_lo + lon_hi) / 2
                if val & mask:
                    lon_lo = mid
                else:
                    lon_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2
                if val & mask:
                    lat_lo = mid
                else:
                    lat_hi = mid
            even = not even
    return lat_lo, lon_lo, lat_hi, lon_hi


def decode(cell: str) -> tuple[float, float]:
    """Centre of a geohash cell."""
    s, w, n, e = bounds(cell)
    return (s + n) / 2, (w + e) / 2


def adjacent(cell: str, direction: str) -> str:
    """The cell one step `direction` ('n'|'s'|'e'|'w') from `cell`."""
    cell = cell.lower()
    direction = direction.lower()
    if not cell:
        raise ValueError("cannot take a neighbour of the empty cell")
    if direction not in _NEIGHBOUR:
        raise ValueError(f"direction must be one of n/s/e/w, got {direction!r}")

    last, parent = cell[-1], cell[:-1]
    parity = len(cell) % 2  # 1 => odd length => longitude was split last
    if last in _BORDER[direction][parity] and parent:
        parent = adjacent(parent, direction)
    return parent + _BASE32[_NEIGHBOUR[direction][parity].index(last)]


def neighbours(cell: str) -> dict[str, str]:
    """The eight surrounding cells, keyed by compass direction."""
    n, s = adjacent(cell, "n"), adjacent(cell, "s")
    return {
        "n": n,
        "ne": adjacent(n, "e"),
        "e": adjacent(cell, "e"),
        "se": adjacent(s, "e"),
        "s": s,
        "sw": adjacent(s, "w"),
        "w": adjacent(cell, "w"),
        "nw": adjacent(n, "w"),
    }


def parents(cell: str, down_to: int = 4) -> list[str]:
    """Enclosing cells, coarsest last: ['dr5ru7k', 'dr5ru7', 'dr5ru']."""
    return [cell[:p] for p in range(len(cell) - 1, down_to - 1, -1)]


def index(lat: float, lon: float, precision: int = 8) -> dict:
    """The `spatialIndex` block of placement.json."""
    cell = encode(lat, lon, precision)
    return {
        "system": "geohash",
        "precision": precision,
        "cell": cell,
        "parentCells": parents(cell),
        "neighbors": list(neighbours(cell).values()),
    }


def cell_polygon(cell: str) -> list[list[float]]:
    """Closed [lon, lat] ring for the cell — GeoJSON winding, ready for the map."""
    s, w, n, e = bounds(cell)
    return [[w, s], [e, s], [e, n], [w, n], [w, s]]
