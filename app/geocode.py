"""Address -> coordinates, with a fallback that survives a dead conference network.

Person 2 owns geocoding. Until their module lands, this resolves addresses via
Nominatim and falls back to a built-in venue table so the live demo never
depends on a network call succeeding in front of judges.

Every result carries its `source` and `confidence` so the UI can say where the
coordinate came from instead of presenting a guess as a measurement.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

from app import geohash

USER_AGENT = "WorldForge/0.1 (VTHacks 14 project; contact: team@worldforge.invalid)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
TIMEOUT_S = float(os.environ.get("WORLDFORGE_GEOCODE_TIMEOUT", "6"))


@dataclass
class GeocodeResult:
    latitude: float
    longitude: float
    displayName: str
    source: str            # "nominatim" | "venue-table" | "coordinates"
    confidence: float      # 0..1, how much the placement can lean on this
    cell: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["cell"] = self.cell or geohash.encode(self.latitude, self.longitude, 8)
        return d


# The 2026 tournament venues. Coordinates are venue centres, good to roughly a
# stadium's width — enough to frame the map, not a survey. The live geocoder is
# always preferred; this table is what keeps the demo alive offline.
VENUES: list[dict] = [
    {"key": "metlife", "name": "New York New Jersey Stadium", "venue": "MetLife Stadium",
     "address": "1 MetLife Stadium Dr, East Rutherford, NJ 07073", "lat": 40.8135, "lon": -74.0745,
     "country": "USA", "capacity": 82500},
    {"key": "att", "name": "Dallas Stadium", "venue": "AT&T Stadium",
     "address": "1 AT&T Way, Arlington, TX 76011", "lat": 32.7473, "lon": -97.0945,
     "country": "USA", "capacity": 80000},
    {"key": "nrg", "name": "Houston Stadium", "venue": "NRG Stadium",
     "address": "1 NRG Pkwy, Houston, TX 77054", "lat": 29.6847, "lon": -95.4107,
     "country": "USA", "capacity": 72220},
    {"key": "arrowhead", "name": "Kansas City Stadium", "venue": "Arrowhead Stadium",
     "address": "1 Arrowhead Dr, Kansas City, MO 64129", "lat": 39.0489, "lon": -94.4839,
     "country": "USA", "capacity": 76416},
    {"key": "mbs", "name": "Atlanta Stadium", "venue": "Mercedes-Benz Stadium",
     "address": "1 AMB Dr NW, Atlanta, GA 30313", "lat": 33.7554, "lon": -84.4008,
     "country": "USA", "capacity": 71000},
    {"key": "hardrock", "name": "Miami Stadium", "venue": "Hard Rock Stadium",
     "address": "347 Don Shula Dr, Miami Gardens, FL 33056", "lat": 25.9580, "lon": -80.2389,
     "country": "USA", "capacity": 65326},
    {"key": "levis", "name": "San Francisco Bay Area Stadium", "venue": "Levi's Stadium",
     "address": "4900 Marie P DeBartolo Way, Santa Clara, CA 95054", "lat": 37.4033, "lon": -121.9694,
     "country": "USA", "capacity": 68500},
    {"key": "sofi", "name": "Los Angeles Stadium", "venue": "SoFi Stadium",
     "address": "1001 S Stadium Dr, Inglewood, CA 90301", "lat": 33.9535, "lon": -118.3392,
     "country": "USA", "capacity": 70240},
    {"key": "lumen", "name": "Seattle Stadium", "venue": "Lumen Field",
     "address": "800 Occidental Ave S, Seattle, WA 98134", "lat": 47.5952, "lon": -122.3316,
     "country": "USA", "capacity": 68740},
    {"key": "gillette", "name": "Boston Stadium", "venue": "Gillette Stadium",
     "address": "1 Patriot Pl, Foxborough, MA 02035", "lat": 42.0909, "lon": -71.2643,
     "country": "USA", "capacity": 65878},
    {"key": "linc", "name": "Philadelphia Stadium", "venue": "Lincoln Financial Field",
     "address": "1 Lincoln Financial Field Way, Philadelphia, PA 19148", "lat": 39.9008, "lon": -75.1675,
     "country": "USA", "capacity": 69796},
    {"key": "azteca", "name": "Estadio Ciudad de Mexico", "venue": "Estadio Azteca",
     "address": "Calz. de Tlalpan 3465, Mexico City, Mexico", "lat": 19.3029, "lon": -99.1505,
     "country": "Mexico", "capacity": 87523},
    {"key": "akron", "name": "Estadio Guadalajara", "venue": "Estadio Akron",
     "address": "Cto. JVC 2800, Zapopan, Jalisco, Mexico", "lat": 20.6819, "lon": -103.4625,
     "country": "Mexico", "capacity": 49850},
    {"key": "bbva", "name": "Estadio Monterrey", "venue": "Estadio BBVA",
     "address": "Av. Pablo Livas 2011, Guadalupe, Nuevo Leon, Mexico", "lat": 25.6693, "lon": -100.2444,
     "country": "Mexico", "capacity": 53500},
    {"key": "bmo", "name": "Toronto Stadium", "venue": "BMO Field",
     "address": "170 Princes' Blvd, Toronto, ON M6K 3C3, Canada", "lat": 43.6332, "lon": -79.4186,
     "country": "Canada", "capacity": 45736},
    {"key": "bcplace", "name": "Vancouver Stadium", "venue": "BC Place",
     "address": "777 Pacific Blvd, Vancouver, BC V6B 4Y8, Canada", "lat": 49.2768, "lon": -123.1119,
     "country": "Canada", "capacity": 54500},
]


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum() or c == " ").strip()


def _parse_coordinates(query: str) -> GeocodeResult | None:
    """Accept a raw 'lat, lon' pair — the escape hatch when an address will not resolve."""
    parts = [p.strip() for p in query.replace(";", ",").split(",")]
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return GeocodeResult(lat, lon, f"{lat:.5f}, {lon:.5f}", "coordinates", 1.0,
                         note="Entered directly as coordinates.")


def _match_venue(query: str) -> dict | None:
    q = _norm(query)
    if not q:
        return None
    for v in VENUES:
        haystacks = (_norm(v["venue"]), _norm(v["name"]), _norm(v["address"]), v["key"])
        if any(q == h for h in haystacks):
            return v
    for v in VENUES:
        haystacks = (_norm(v["venue"]), _norm(v["name"]), _norm(v["address"]), v["key"])
        if any(q in h or h in q for h in haystacks if h):
            return v
    return None


def _nominatim(query: str) -> GeocodeResult | None:
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 1})
    req = urllib.request.Request(f"{NOMINATIM}?{params}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            hits = json.loads(resp.read().decode("utf-8"))
    except Exception:
        # Offline, rate-limited, or DNS-blocked. The caller falls back; the demo
        # must never die because a third-party service hiccuped.
        return None
    if not hits:
        return None
    hit = hits[0]
    # Nominatim's `importance` is a relevance score, not positional accuracy;
    # treat it as a soft prior and keep it out of the confident range.
    importance = float(hit.get("importance") or 0.4)
    return GeocodeResult(
        latitude=float(hit["lat"]),
        longitude=float(hit["lon"]),
        displayName=hit.get("display_name", query),
        source="nominatim",
        confidence=round(min(0.95, 0.5 + importance / 2), 3),
        note=f"OpenStreetMap match ({hit.get('type', 'place')}).",
    )


def geocode(query: str, *, allow_network: bool = True) -> GeocodeResult:
    """Resolve an address. Never raises for an unresolvable address — returns a
    zero-confidence result the UI can flag and the user can correct by hand."""
    query = (query or "").strip()
    if not query:
        return GeocodeResult(0.0, 0.0, "", "none", 0.0, note="No address entered.")

    if (direct := _parse_coordinates(query)) is not None:
        return direct

    if allow_network and (hit := _nominatim(query)) is not None:
        return hit

    if (venue := _match_venue(query)) is not None:
        return GeocodeResult(
            latitude=venue["lat"],
            longitude=venue["lon"],
            displayName=f'{venue["venue"]} — {venue["address"]}',
            source="venue-table",
            confidence=0.6,
            note="Offline venue table; coordinates are the venue centre, not a surveyed point.",
        )

    return GeocodeResult(
        0.0, 0.0, query, "none", 0.0,
        note="Address did not resolve. Enter 'latitude, longitude' directly, or pick a venue.",
    )
