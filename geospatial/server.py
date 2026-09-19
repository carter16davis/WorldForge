"""Local placement playground: python3 -m geospatial.server."""

import argparse
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from .placement import build_placement, geocode_prepared, local_to_enu, number

WEB = Path(__file__).parent / "web"


class Geocoder:
    """One upstream request at a time, at most once per second, with session caching."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last_request = 0
        self.cache = {}

    def search(self, query):
        query = " ".join(query.split())
        if not 3 <= len(query) <= 300:
            raise ValueError("Enter a place or address between 3 and 300 characters.")
        try:
            prepared = geocode_prepared(query)
            return [{**prepared, "label": "MetLife Stadium · prepared demo location"}]
        except ValueError:
            pass
        with self.lock:
            key = query.casefold()
            if key in self.cache:
                return self.cache[key]
            delay = 1.1 - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            endpoint = os.environ.get("WORLDFORGE_GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
            request = Request(endpoint + "?" + urlencode({"q": query, "format": "jsonv2", "limit": 5}), headers={
                "User-Agent": "WorldForge-LocalPlacementDemo/0.1",
                "Accept": "application/json",
            })
            self.last_request = time.monotonic()
            with urlopen(request, timeout=12) as response:
                data = json.loads(response.read(1_000_000))
            matches = [{
                "label": item["display_name"],
                "latitude": number(float(item["lat"]), "latitude", -90, 90),
                "longitude": number(float(item["lon"]), "longitude", -180, 180),
                "source": "OpenStreetMap / Nominatim · review the selected location",
            } for item in data[:5]]
            if len(self.cache) >= 256:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = matches
            return matches


def preview(payload):
    placement = build_placement(payload["assetId"], payload["name"], payload["sourceAddress"], payload["transform"])
    state = {**placement["location"], **placement["transform"]}
    # The preview is a made-up 60 x 90 x 30 model-unit block, never surveyed geometry.
    points = [(-30, 0, -45), (30, 0, -45), (30, 0, 45), (-30, 0, 45)]
    enu = [local_to_enu(p, (-30, 0, -45), (30, 30, 45), state) for p in points]
    lat, lon = state["latitude"], state["longitude"]
    if abs(lat) > 85:
        raise ValueError("This map preview supports latitudes between -85 and 85 degrees.")
    def to_latlon(point):
        # Short-distance spherical approximation for the demonstration footprint.
        east, north, _ = point
        return [lat + math.degrees(north / 6378137), lon + math.degrees(east / (6378137 * math.cos(math.radians(lat))))]
    return {"placement": placement, "footprint": [to_latlon(p) for p in enu],
            "front": to_latlon(local_to_enu((0, 0, -45), (-30, 0, -45), (30, 30, 45), state)),
            "localCorners": enu}


class Handler(BaseHTTPRequestHandler):
    geocoder = Geocoder()

    def send_json(self, status, data):
        body = json.dumps(data, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/api/search":
            try:
                self.send_json(200, {"results": self.geocoder.search(parse_qs(url.query).get("q", [""])[0])})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except Exception:
                self.send_json(502, {"error": "Address search is unavailable. Try the prepared venue or enter coordinates below."})
            return
        files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
        if url.path not in files:
            self.send_error(404)
            return
        filename, content_type = files[url.path]
        data = (WEB / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/api/preview":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("Invalid request size")
            self.send_json(200, preview(json.loads(self.rfile.read(length))))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            self.send_json(400, {"error": str(error)})


def main():
    parser = argparse.ArgumentParser(description="Start the local WorldForge placement viewer")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"WorldForge: http://127.0.0.1:{server.server_port} (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
