"""Address -> coordinates. The only geocoder in the project.

Resolution order is deliberate: coordinates typed by hand, then the built-in
venue table, then OpenStreetMap's Nominatim. The table comes before the network
so the prepared demo path never waits on a third-party service, and so the same
address gives the same answer in every rehearsal.

Every result carries its `source` and `confidence` so the UI can say where the
coordinate came from instead of presenting a guess as a measurement. An
ambiguous address resolves to nothing and returns `candidates` for the user to
choose from — silently picking the first of five matches is how a building ends
up on the wrong continent.

Live requests are serialized, rate-limited to one per 1.1 s and cached for the
process lifetime, per the Nominatim usage policy:
https://operations.osmfoundation.org/policies/nominatim/
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

from app import geohash
from geospatial import number

USER_AGENT = "WorldForge/0.1 (VTHacks 14 project; contact: team@worldforge.invalid)"
NOMINATIM = os.environ.get("WORLDFORGE_GEOCODER_URL",
                           "https://nominatim.openstreetmap.org/search")
TIMEOUT_S = float(os.environ.get("WORLDFORGE_GEOCODE_TIMEOUT", "6"))
MIN_REQUEST_INTERVAL_S = 1.1
CACHE_LIMIT = 256


@dataclass
class GeocodeResult:
    latitude: float
    longitude: float
    displayName: str
    source: str            # "nominatim" | "venue-table" | "coordinates" | "none"
    confidence: float      # 0..1, how much the placement can lean on this
    cell: str = ""
    note: str = ""
    candidates: list[dict] = field(default_factory=list)

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


class Geocoder:
    """One upstream request at a time, at most one per 1.1 s, cached per process."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_request = 0.0
        self.cache: dict[str, list[dict]] = {}

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Candidate matches, best first. Raises on a transport or decode failure."""
        query = " ".join(query.split())
        if not 3 <= len(query) <= 300:
            raise ValueError("Enter a place or address between 3 and 300 characters.")
        with self.lock:
            key = query.casefold()
            if key in self.cache:
                return self.cache[key]

            delay = MIN_REQUEST_INTERVAL_S - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)

            params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": limit})
            req = urllib.request.Request(
                f"{NOMINATIM}?{params}",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            self.last_request = time.monotonic()
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                hits = json.loads(resp.read(1_000_000).decode("utf-8"))

            matches = [{
                "label": hit.get("display_name", query),
                "latitude": number(float(hit["lat"]), "latitude", -90, 90),
                "longitude": number(float(hit["lon"]), "longitude", -180, 180),
                "source": "nominatim",
                # Nominatim's `importance` is a relevance score, not positional
                # accuracy; treat it as a soft prior, never a confident one.
                "confidence": round(min(0.95, 0.5 + float(hit.get("importance") or 0.4) / 2), 3),
                "note": f"OpenStreetMap match ({hit.get('type', 'place')}).",
            } for hit in hits[:limit]]

            if len(self.cache) >= CACHE_LIMIT:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = matches
            return matches


GEOCODER = Geocoder()


def geocode(query: str, *, allow_network: bool = True) -> GeocodeResult:
    """Resolve an address. Never raises for an unresolvable address — returns a
    zero-confidence result the UI can flag and the user can correct by hand."""
    query = " ".join((query or "").strip().split())
    if not query:
        return GeocodeResult(0.0, 0.0, "", "none", 0.0, note="No address entered.")

    if (direct := _parse_coordinates(query)) is not None:
        return direct

    if (venue := _match_venue(query)) is not None:
        return GeocodeResult(
            latitude=venue["lat"],
            longitude=venue["lon"],
            displayName=f'{venue["venue"]} — {venue["address"]}',
            source="venue-table",
            confidence=0.6,
            note="Prepared venue table; coordinates are the venue centre, not a surveyed point.",
        )

    if not allow_network:
        return GeocodeResult(
            0.0, 0.0, query, "none", 0.0,
            note="Offline: enter 'latitude, longitude' directly, or pick a prepared venue.",
        )

    try:
        candidates = GEOCODER.search(query)
    except Exception:
        # Offline, rate-limited, or DNS-blocked. The demo must never die because
        # a third-party service hiccuped.
        return GeocodeResult(
            0.0, 0.0, query, "none", 0.0,
            note="Address search is unavailable. Enter coordinates or pick a prepared venue.",
        )

    if len(candidates) == 1:
        hit = candidates[0]
        return GeocodeResult(
            latitude=hit["latitude"], longitude=hit["longitude"],
            displayName=hit["label"], source="nominatim",
            confidence=hit["confidence"], note=hit["note"],
        )

    if candidates:
        return GeocodeResult(
            0.0, 0.0, query, "none", 0.0, candidates=candidates,
            note=f"{len(candidates)} possible matches. Choose the intended address.",
        )

    return GeocodeResult(
        0.0, 0.0, query, "none", 0.0,
        note="Address did not resolve. Enter 'latitude, longitude' directly, or pick a venue.",
    )
