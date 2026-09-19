#!/usr/bin/env python3
"""
assess_photos.py - opens the pixels, not just the metadata.

inspect_photos.py answers "did the phone record where I was standing?"
This answers "are these photos actually good enough to reconstruct from,
and if not, what exactly should the user go and re-shoot?"

Usage:
    python assess_photos.py ./photos
    python assess_photos.py ./photos --json quality_report.json
    python assess_photos.py ./photos --subject object     # tabletop, not building

Writes quality_report.json. No GPU, no model, no network.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC = True
except ImportError:
    HEIC = False

EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif"}

# --- Thresholds -------------------------------------------------------------
# These are starting points. Calibrate them on your first real building
# capture: run with --debug, look at the sharpness column for photos you
# judge by eye to be fine, and move SHARP_ABS_MIN below that.

WORK_SIDE = 1024  # analysis resolution (long side, px)
SHARP_ABS_MIN = 55.0  # laplacian variance floor
SHARP_REL_MIN = 0.35  # ...or this fraction of the set's median
CLIP_HI_MAX = 0.12  # max fraction of blown-out pixels
CLIP_LO_MAX = 0.20  # max fraction of crushed blacks
FOLIAGE_WARN = 0.28  # green fraction that suggests a tree in the way
DUP_HAMMING = 5  # dHash distance below which two shots are the same shot
DUP_HEADING_DEG = 20.0  # ...and the camera must have been pointing this close
DUP_DISTANCE_M = 4.0  # ...and standing this close
MIN_MEGAPIXELS = 1.5

# Coverage gate (buildings)
BLD_MIN_USABLE = 12
BLD_MIN_SECTORS = 3
BLD_MIN_SPREAD_M = 8.0

# Coverage gate (small objects on a table)
OBJ_MIN_USABLE = 8
OBJ_MIN_SECTORS = 3
OBJ_MIN_SPREAD_M = 0.0

SECTOR_NAMES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


# --- Per-photo record -------------------------------------------------------


@dataclass
class Photo:
    name: str
    ok: bool = False
    error: str | None = None
    width: int = 0
    height: int = 0
    megapixels: float = 0.0
    sharpness: float = 0.0  # laplacian variance
    sharp_rel: float = 0.0  # vs set median
    clip_hi: float = 0.0
    clip_lo: float = 0.0
    mean_luma: float = 0.0
    foliage: float = 0.0
    dhash: int = 0
    lat: float | None = None
    lon: float | None = None
    heading: float | None = None
    sector: str | None = None
    usable: bool = False
    flags: list[str] = field(default_factory=list)


# --- Pixel measurements -----------------------------------------------------


def laplacian_var(gray: np.ndarray) -> float:
    """Variance of the 3x3 Laplacian. High = lots of sharp edges = in focus.

    A blurred photo has soft transitions, so the second derivative stays near
    zero everywhere and the variance collapses. This is the cheapest blur
    detector that works, and it is the standard one.
    """
    g = gray.astype(np.float32)
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1]
        + g[2:, 1:-1]
        + g[1:-1, :-2]
        + g[1:-1, 2:]
    )
    return float(lap.var())


def dhash(gray: np.ndarray, size: int = 8) -> int:
    """64-bit difference hash. Two photos of the same thing from the same
    spot land within a few bits of each other."""
    img = Image.fromarray(gray.astype(np.uint8)).resize(
        (size + 1, size), Image.Resampling.LANCZOS
    )
    a = np.asarray(img, dtype=np.int16)
    bits = a[:, 1:] > a[:, :-1]
    out = 0
    for b in bits.flatten():
        out = (out << 1) | int(b)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def foliage_fraction(rgb: np.ndarray) -> float:
    """Fraction of frame that is saturated green. A proxy for 'there is a tree
    between you and the wall'. It will not catch parked cars or people -
    that needs the vision pass."""
    small = rgb[::4, ::4].astype(np.float32) / 255.0
    r, g, b = small[..., 0], small[..., 1], small[..., 2]
    mx = small.max(axis=-1)
    mn = small.min(axis=-1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    green = (g > r * 1.06) & (g > b * 1.06) & (sat > 0.18) & (mx > 0.12)
    return float(green.mean())


# --- EXIF -------------------------------------------------------------------


def _ratio(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _dms(vals, ref) -> float | None:
    try:
        d, m, s = (_ratio(x) for x in vals)
        deg = d + m / 60.0 + s / 3600.0
        if str(ref).upper() in ("S", "W"):
            deg = -deg
        return deg
    except (TypeError, ValueError):
        return None


def read_exif(img: Image.Image) -> tuple[float | None, float | None, float | None]:
    """Returns (lat, lon, heading). Tolerant - missing is normal, not an error."""
    try:
        exif = img.getexif()
        gps = exif.get_ifd(0x8825) or {}
    except Exception:
        return None, None, None
    lat = _dms(gps.get(2), gps.get(1)) if gps.get(2) else None
    lon = _dms(gps.get(4), gps.get(3)) if gps.get(4) else None
    hd = gps.get(17)
    heading = None
    if hd is not None:
        h = _ratio(hd)
        if not math.isnan(h):
            heading = h % 360.0
    return lat, lon, heading


def sector_of(heading: float) -> str:
    return SECTOR_NAMES[int((heading + 22.5) % 360 // 45)]


def metres_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Flat-earth is fine at building scale."""
    lat0 = math.radians((a[0] + b[0]) / 2)
    dx = (b[1] - a[1]) * 111_320.0 * math.cos(lat0)
    dy = (b[0] - a[0]) * 110_540.0
    return math.hypot(dx, dy)


# --- Main pass --------------------------------------------------------------


def measure(path: Path) -> Photo:
    p = Photo(name=path.name)
    try:
        img = Image.open(path)
        img.load()
    except Exception as e:
        if path.suffix.lower() in (".heic", ".heif") and not HEIC:
            p.error = "HEIC decoder missing - pip install pillow-heif"
        else:
            p.error = f"cannot open: {e}"
        return p

    p.width, p.height = img.size
    p.megapixels = round(p.width * p.height / 1e6, 1)
    p.lat, p.lon, p.heading = read_exif(img)
    if p.heading is not None:
        p.sector = sector_of(p.heading)

    rgb_full = img.convert("RGB")
    scale = WORK_SIDE / max(rgb_full.size)
    if scale < 1.0:
        rgb_full = rgb_full.resize(
            (max(1, int(rgb_full.width * scale)), max(1, int(rgb_full.height * scale))),
            Image.Resampling.LANCZOS,
        )
    rgb = np.asarray(rgb_full, dtype=np.uint8)
    gray = np.asarray(rgb_full.convert("L"), dtype=np.uint8)

    p.sharpness = round(laplacian_var(gray), 1)
    p.clip_hi = round(float((gray >= 250).mean()), 4)
    p.clip_lo = round(float((gray <= 5).mean()), 4)
    p.mean_luma = round(float(gray.mean()), 1)
    p.foliage = round(foliage_fraction(rgb), 3)
    p.dhash = dhash(gray)
    p.ok = True
    return p


def _same_viewpoint(a: Photo, b: Photo) -> bool:
    """Same spot, same direction. If either photo has no geotag we fall back
    to the hash alone, which is the old behaviour and the best we can do."""
    if a.heading is not None and b.heading is not None:
        diff = abs(a.heading - b.heading) % 360
        if min(diff, 360 - diff) > DUP_HEADING_DEG:
            return False
    if None not in (a.lat, a.lon, b.lat, b.lon):
        if metres_between((a.lat, a.lon), (b.lat, b.lon)) > DUP_DISTANCE_M:
            return False
    return True


def judge(photos: list[Photo]) -> None:
    """Second pass - needs the whole set to compute relative sharpness."""
    good = [p for p in photos if p.ok]
    if not good:
        return
    median_sharp = float(np.median([p.sharpness for p in good])) or 1.0

    for p in good:
        p.sharp_rel = round(p.sharpness / median_sharp, 2)
        if p.megapixels < MIN_MEGAPIXELS:
            p.flags.append(f"low resolution ({p.megapixels}MP)")

        low_detail = p.sharpness < SHARP_ABS_MIN or p.sharp_rel < SHARP_REL_MIN
        washed = p.clip_hi > CLIP_HI_MAX or (low_detail and p.mean_luma > 195)
        crushed = p.clip_lo > CLIP_LO_MAX or (low_detail and p.mean_luma < 45)

        # Order matters. Overexposure flattens local contrast, so a washed-out
        # photo scores like a blurred one. Calling it "blurry" sends the user
        # out to re-shoot a steadier photo when the real fix is exposure.
        if washed:
            p.flags.append(f"washed out / overexposed ({p.clip_hi:.0%} clipped)")
        elif crushed:
            p.flags.append(f"underexposed ({p.clip_lo:.0%} crushed)")
        elif low_detail:
            p.flags.append("blurry")

        if p.foliage > FOLIAGE_WARN:
            p.flags.append(f"heavy foliage ({p.foliage:.0%} of frame)")

        p.usable = not any(
            f.startswith(("blurry", "low resolution", "washed out", "underexposed"))
            for f in p.flags
        )

    # Near-duplicates. The hash alone is not enough: a brick wall shot from the
    # north and the same brick wall shot from the west hash almost identically,
    # because the texture is repetitive and dHash only sees coarse structure.
    # Two photos are only the same photo if the camera was also in the same
    # place, pointing the same way.
    for i, a in enumerate(good):
        if not a.usable:
            continue
        for b in good[i + 1 :]:
            if not b.usable or hamming(a.dhash, b.dhash) > DUP_HAMMING:
                continue
            if not _same_viewpoint(a, b):
                continue
            loser = a if a.sharpness < b.sharpness else b
            keeper = b if loser is a else a
            if not any("near-duplicate" in f for f in loser.flags):
                loser.flags.append(f"near-duplicate of {keeper.name}")


def gate(photos: list[Photo], subject: str) -> dict:
    usable = [p for p in photos if p.usable]
    distinct = [p for p in usable if not any("near-duplicate" in f for f in p.flags)]
    sectors = sorted({p.sector for p in distinct if p.sector})

    coords = [(p.lat, p.lon) for p in distinct if p.lat is not None]
    spread = 0.0
    for i, a in enumerate(coords):
        for b in coords[i + 1 :]:
            spread = max(spread, metres_between(a, b))

    if subject == "object":
        need_n, need_sec, need_spread = OBJ_MIN_USABLE, OBJ_MIN_SECTORS, OBJ_MIN_SPREAD_M
    else:
        need_n, need_sec, need_spread = BLD_MIN_USABLE, BLD_MIN_SECTORS, BLD_MIN_SPREAD_M

    enough_n = len(distinct) >= need_n
    enough_sec = len(sectors) >= need_sec
    enough_spread = spread >= need_spread

    asks: list[str] = []
    if not enough_n:
        asks.append(
            f"{need_n - len(distinct)} more usable photo(s) - you have {len(distinct)} distinct, need {need_n}"
        )
    if not enough_sec:
        have = ", ".join(sectors) if sectors else "none"
        missing = [s for s in ("N", "E", "S", "W") if s not in sectors]
        asks.append(
            f"more sides - camera faced {have}; walk round and shoot from the {', '.join(missing)} side(s)"
        )
    if not enough_spread and subject == "building":
        asks.append(
            f"walk further - cameras span {spread:.1f}m, need >{need_spread:.0f}m. "
            "Zooming does not count; take steps."
        )

    if enough_n and enough_sec and enough_spread:
        verdict = "RECONSTRUCT_MULTIVIEW"
        note = "Coverage and quality are sufficient. Send to the photogrammetry / multi-view path."
    elif len(distinct) >= 1 and (len(sectors) >= 2 or len(distinct) >= 4):
        verdict = "NEED_MORE_PHOTOS"
        note = "Partial coverage. Ask the user for the specific shots below before falling back."
    else:
        verdict = "GENERATE_SINGLE_VIEW"
        note = "Too thin to reconstruct. Route to the generative track and label the output as generated."

    return {
        "verdict": verdict,
        "note": note,
        "subject": subject,
        "counts": {
            "total": len(photos),
            "decoded": sum(1 for p in photos if p.ok),
            "usable": len(usable),
            "distinct": len(distinct),
        },
        "sectors_covered": sectors,
        "camera_spread_m": round(spread, 1),
        "asks": asks,
        "thresholds": {
            "min_distinct": need_n,
            "min_sectors": need_sec,
            "min_spread_m": need_spread,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--subject", choices=["building", "object"], default="building")
    ap.add_argument("--json", type=Path, default=Path("quality_report.json"))
    ap.add_argument("--debug", action="store_true", help="show raw metric columns")
    args = ap.parse_args()

    if not args.folder.is_dir():
        print(f"not a folder: {args.folder}", file=sys.stderr)
        return 2

    paths = sorted(p for p in args.folder.iterdir() if p.suffix.lower() in EXTS)
    if not paths:
        print(f"no images in {args.folder}", file=sys.stderr)
        return 2

    photos = [measure(p) for p in paths]
    judge(photos)
    result = gate(photos, args.subject)

    for p in photos:
        if not p.ok:
            print(f"--- {p.name}\n    ! {p.error}")
            continue
        mark = "ok  " if p.usable else "DROP"
        head = f"{p.heading:5.1f} {p.sector}" if p.heading is not None else "  -   -"
        line = f"{mark} {p.name:22} {p.megapixels:4.1f}MP  head {head}"
        if args.debug:
            line += (
                f"  sharp {p.sharpness:7.1f} (x{p.sharp_rel:.2f})"
                f"  hi {p.clip_hi:.2f} lo {p.clip_lo:.2f}  grn {p.foliage:.2f}"
            )
        print(line)
        for f in p.flags:
            print(f"       - {f}")

    c = result["counts"]
    print("\n" + "=" * 62)
    print(
        f"{c['total']} photos | {c['decoded']} decoded | {c['usable']} usable | "
        f"{c['distinct']} distinct viewpoints"
    )
    print(
        f"sides covered: {', '.join(result['sectors_covered']) or 'none'} | "
        f"camera spread {result['camera_spread_m']}m"
    )
    print(f"\nVERDICT: {result['verdict']}")
    print(f"  {result['note']}")
    if result["asks"]:
        print("\nASK THE USER FOR:")
        for a in result["asks"]:
            print(f"  - {a}")

    payload = {"gate": result, "photos": [asdict(p) for p in photos]}
    args.json.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
