"""Geohash correctness. Neighbour tables are transcription-prone, so the
adjacency tests check real cell geometry rather than just distinctness."""

import pytest

from app import geohash as gh

# Reference values from the canonical geohash implementations.
KNOWN = [
    (57.64911, 10.40744, 11, "u4pruydqqvj"),
    (-25.382708, -49.265506, 9, "6gkzwgjzn"),
    (40.8135, -74.0745, 8, "dr724mue"),
    (0.0, 0.0, 6, "s00000"),
]


@pytest.mark.parametrize("lat,lon,precision,expected", KNOWN)
def test_encode_matches_reference(lat, lon, precision, expected):
    assert gh.encode(lat, lon, precision) == expected


@pytest.mark.parametrize("lat,lon,precision,cell", KNOWN)
def test_decoded_centre_re_encodes_to_the_same_cell(lat, lon, precision, cell):
    clat, clon = gh.decode(cell)
    assert gh.encode(clat, clon, precision) == cell


@pytest.mark.parametrize("lat,lon,precision,cell", KNOWN)
def test_cell_contains_its_own_coordinate(lat, lon, precision, cell):
    s, w, n, e = gh.bounds(cell)
    assert s <= lat <= n
    assert w <= lon <= e


def _shares_edge(a: str, b: str, direction: str) -> bool:
    """`b` must sit immediately `direction` of `a`, flush along the shared edge."""
    as_, aw, an, ae = gh.bounds(a)
    bs, bw, bn, be = gh.bounds(b)
    tol = 1e-9
    if direction == "n":
        return abs(an - bs) < tol and abs(aw - bw) < tol and abs(ae - be) < tol
    if direction == "s":
        return abs(as_ - bn) < tol and abs(aw - bw) < tol and abs(ae - be) < tol
    if direction == "e":
        return abs(ae - bw) < tol and abs(as_ - bs) < tol and abs(an - bn) < tol
    return abs(aw - be) < tol and abs(as_ - bs) < tol and abs(an - bn) < tol


@pytest.mark.parametrize("cell", ["dr724mue", "u4pruyd", "6gkzwgjzn", "s0000", "ezs42"])
@pytest.mark.parametrize("direction", ["n", "s", "e", "w"])
def test_adjacent_cell_is_flush_against_the_shared_edge(cell, direction):
    assert _shares_edge(cell, gh.adjacent(cell, direction), direction)


@pytest.mark.parametrize("cell", ["dr724mue", "u4pruyd", "s0000"])
def test_adjacency_is_reversible(cell):
    opposite = {"n": "s", "s": "n", "e": "w", "w": "e"}
    for d, back in opposite.items():
        assert gh.adjacent(gh.adjacent(cell, d), back) == cell


def test_neighbours_returns_eight_distinct_cells_around_the_centre():
    cell = "dr724mue"
    ring = gh.neighbours(cell)
    assert len(set(ring.values())) == 8
    assert cell not in ring.values()
    # every neighbour must be the same size and touch the centre cell
    cs, cw, cn, ce = gh.bounds(cell)
    for name, other in ring.items():
        os_, ow, on, oe = gh.bounds(other)
        assert abs((on - os_) - (cn - cs)) < 1e-9, name
        assert os_ <= cn + 1e-9 and on >= cs - 1e-9, name
        assert ow <= ce + 1e-9 and oe >= cw - 1e-9, name


def test_parents_are_prefixes_ordered_finest_first():
    assert gh.parents("dr724mue") == ["dr724mu", "dr724m", "dr724", "dr72"]
    assert all("dr724mue".startswith(p) for p in gh.parents("dr724mue"))


def test_index_block_is_self_consistent():
    block = gh.index(40.8135, -74.0745, 8)
    assert block["cell"] == "dr724mue"
    assert block["precision"] == len(block["cell"])
    assert len(block["neighbors"]) == 8
    assert block["parentCells"][0] == block["cell"][:-1]


def test_cell_polygon_is_a_closed_ring_in_lon_lat_order():
    ring = gh.cell_polygon("dr724mue")
    assert ring[0] == ring[-1]
    assert len(ring) == 5
    s, w, n, e = gh.bounds("dr724mue")
    lons = {round(p[0], 12) for p in ring}
    lats = {round(p[1], 12) for p in ring}
    assert lons == {round(w, 12), round(e, 12)}
    assert lats == {round(s, 12), round(n, 12)}


def test_bad_input_is_rejected_clearly():
    with pytest.raises(ValueError, match="not a geohash"):
        gh.bounds("dr7a4m")          # 'a' is not in the geohash alphabet
    with pytest.raises(ValueError, match="n/s/e/w"):
        gh.adjacent("dr724", "up")
    with pytest.raises(ValueError, match="empty cell"):
        gh.adjacent("", "n")
