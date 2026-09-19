"""
inspect_photos.py — what can we actually get out of a folder of building photos?

Run this on real photos BEFORE building anything that depends on metadata.
Half the time the answer is "nothing, the app stripped it," and it is much
better to learn that now than at 2am.

    python inspect_photos.py ./photos

Outputs a per-photo report and a verdict on which pipeline the set can support.
"""

from __future__ import annotations

import sys
import math
import json
from pathlib import Path
from datetime import datetime

from PIL import Image, ExifTags

# reverse lookups: tag name -> numeric id
TAGS = {v: k for k, v in ExifTags.TAGS.items()}
GPSTAGS = {v: k for k, v in ExifTags.GPSTAGS.items()}

IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}


def _rational(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _dms_to_deg(dms, ref: str | None) -> float | None:
    """EXIF stores coords as (degrees, minutes, seconds) plus N/S/E/W."""
    if not dms or len(dms) != 3:
        return None
    d, m, s = (_rational(v) for v in dms)
    if any(math.isnan(v) for v in (d, m, s)):
        return None
    val = d + m / 60.0 + s / 3600.0
    if ref in ("S", "W"):
        val = -val
    return val


def read_meta(path: Path) -> dict:
    out: dict = {"file": path.name, "errors": []}
    try:
        img = Image.open(path)
    except Exception as e:                      # HEIC needs pillow-heif
        out["errors"].append(f"cannot open: {e}")
        return out

    out["pixels"] = f"{img.width}x{img.height}"
    out["megapixels"] = round(img.width * img.height / 1e6, 1)

    exif = img.getexif()
    if not exif:
        out["errors"].append("no EXIF block at all")
        return out

    def tag(name):
        return exif.get(TAGS.get(name, -1))

    out["camera"] = " ".join(str(x).strip() for x in (tag("Make"), tag("Model")) if x) or None
    out["orientation"] = tag("Orientation")

    # --- lens geometry: needed to turn pixels into angles ------------------
    ifd = exif.get_ifd(0x8769)                  # ExifIFD
    def sub(name):
        return ifd.get(TAGS.get(name, -1))

    out["taken"] = str(sub("DateTimeOriginal") or tag("DateTime") or "") or None

    fl = sub("FocalLength")
    fl35 = sub("FocalLengthIn35mmFilm")
    out["focal_mm"] = round(_rational(fl), 2) if fl is not None else None
    out["focal_35mm_equiv"] = fl35
    if fl35:
        # horizontal FOV on a 36mm-wide 35mm frame
        out["fov_h_deg"] = round(2 * math.degrees(math.atan(36.0 / (2 * float(fl35)))), 1)
    else:
        out["fov_h_deg"] = None

    # --- GPS ---------------------------------------------------------------
    gps = exif.get_ifd(0x8825)
    if not gps:
        out["gps"] = None
        out["errors"].append("no GPS (location stripped or was off)")
        return out

    def g(name):
        return gps.get(GPSTAGS.get(name, -1))

    lat = _dms_to_deg(g("GPSLatitude"), g("GPSLatitudeRef"))
    lon = _dms_to_deg(g("GPSLongitude"), g("GPSLongitudeRef"))
    out["gps"] = {"lat": lat, "lon": lon} if lat is not None and lon is not None else None
    if out["gps"] is None:
        out["errors"].append("GPS block present but no usable coordinates")

    alt = g("GPSAltitude")
    if alt is not None:
        a = _rational(alt)
        if g("GPSAltitudeRef") in (1, b"\x01"):
            a = -a
        out["altitude_m"] = round(a, 1)

    # heading of the lens. Ref "T" = true north, "M" = magnetic.
    d = g("GPSImgDirection")
    if d is not None:
        out["heading_deg"] = round(_rational(d), 1)
        out["heading_ref"] = {"T": "true", "M": "magnetic"}.get(
            (g("GPSImgDirectionRef") or "T"), "unknown")

    # iPhones also write the bearing to the subject
    db = g("GPSDestBearing")
    if db is not None:
        out["dest_bearing_deg"] = round(_rational(db), 1)

    err = g("GPSHPositioningError")
    if err is not None:
        out["gps_accuracy_m"] = round(_rational(err), 1)

    return out


def haversine_m(a: dict, b: dict) -> float:
    R = 6371000.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp = p2 - p1
    dl = math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def verdict(reports: list[dict]) -> dict:
    n = len(reports)
    with_gps = [r for r in reports if r.get("gps")]
    with_heading = [r for r in reports if r.get("heading_deg") is not None]
    with_focal = [r for r in reports if r.get("focal_35mm_equiv")]

    # how spread out were the camera positions?
    spread = 0.0
    if len(with_gps) >= 2:
        pts = [r["gps"] for r in with_gps]
        spread = max(haversine_m(pts[i], pts[j])
                     for i in range(len(pts)) for j in range(i + 1, len(pts)))

    # how many distinct directions were shot?
    headings = sorted(r["heading_deg"] for r in with_heading)
    distinct = 0
    if headings:
        distinct = 1
        for prev, cur in zip(headings, headings[1:]):
            if cur - prev > 30:
                distinct += 1

    v = {
        "photos": n,
        "with_gps": len(with_gps),
        "with_heading": len(with_heading),
        "with_lens_info": len(with_focal),
        "camera_spread_m": round(spread, 1),
        "distinct_view_directions": distinct,
    }

    can, cannot = [], []
    (can if with_gps else cannot).append("place the building at a real address automatically")
    (can if with_heading else cannot).append("know which wall each photo shows")
    (can if with_focal else cannot).append("recover camera intrinsics for multi-view work")
    (can if spread > 5 else cannot).append("attempt real multi-view reconstruction (needs >5m spread)")
    (can if distinct >= 2 else cannot).append("see more than one side of the building")
    v["can"] = can
    v["cannot"] = cannot

    if not with_gps:
        v["likely_cause"] = ("Metadata looks stripped. Messaging apps and most "
                             "social uploads remove EXIF. Transfer originals by "
                             "cable, AirDrop, or a cloud drive set to 'original "
                             "quality' instead.")
    return v


def main(folder: str) -> None:
    root = Path(folder)
    files = sorted(p for p in root.iterdir() if p.suffix.lower() in IMG_EXT)
    if not files:
        print(f"no images found in {root.resolve()}")
        return

    reports = [read_meta(p) for p in files]

    for r in reports:
        print(f"\n--- {r['file']}  ({r.get('pixels','?')}, {r.get('megapixels','?')}MP)")
        print(f"    camera    {r.get('camera') or '-'}")
        print(f"    taken     {r.get('taken') or '-'}")
        if r.get("gps"):
            print(f"    gps       {r['gps']['lat']:.6f}, {r['gps']['lon']:.6f}"
                  f"  (+/- {r.get('gps_accuracy_m','?')}m)")
        else:
            print("    gps       MISSING")
        if r.get("heading_deg") is not None:
            print(f"    heading   {r['heading_deg']}deg {r.get('heading_ref','')}")
        else:
            print("    heading   MISSING")
        print(f"    lens      {r.get('focal_mm') or '-'}mm"
              f"  (35mm eq {r.get('focal_35mm_equiv') or '-'},"
              f" fov {r.get('fov_h_deg') or '-'}deg)")
        for e in r["errors"]:
            print(f"    ! {e}")

    v = verdict(reports)
    print("\n" + "=" * 62)
    print(f"{v['photos']} photos | gps {v['with_gps']} | heading {v['with_heading']} "
          f"| lens {v['with_lens_info']}")
    print(f"camera spread {v['camera_spread_m']}m | "
          f"{v['distinct_view_directions']} distinct view direction(s)")
    print("\nCAN:")
    for c in v["can"]:
        print(f"  + {c}")
    print("CANNOT:")
    for c in v["cannot"]:
        print(f"  - {c}")
    if "likely_cause" in v:
        print(f"\nNOTE: {v['likely_cause']}")

    with open("photo_report.json", "w") as f:
        json.dump({"photos": reports, "verdict": v}, f, indent=2, default=str)
    print("\nwrote photo_report.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "./photos")
