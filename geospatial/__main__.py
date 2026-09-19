import argparse
import json
import sys

from .placement import build_placement, geocode_prepared
from .export import export_package


def main():
    parser = argparse.ArgumentParser(description="Create WorldForge placement metadata or an export package")
    parser.add_argument("--address", required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--latitude", type=float)
    parser.add_argument("--longitude", type=float)
    parser.add_argument("--elevation", type=float, required=True, help="Explicit ground elevation in meters; use 0 for a documented relative datum")
    parser.add_argument("--heading", type=float, default=0)
    parser.add_argument("--scale", type=float, default=1)
    parser.add_argument("--offset", type=float, default=0)
    parser.add_argument("--model")
    parser.add_argument("--thumbnail")
    parser.add_argument("--provenance")
    parser.add_argument("--lod")
    parser.add_argument("--output", help="Export root; requires model, thumbnail, and provenance")
    args = parser.parse_args()
    if (args.latitude is None) != (args.longitude is None):
        parser.error("Supply both latitude and longitude")
    if args.output and not all((args.model, args.thumbnail, args.provenance)):
        parser.error("Export requires --model, --thumbnail, and --provenance")
    if not args.output and any((args.model, args.thumbnail, args.provenance, args.lod)):
        parser.error("Asset file arguments require --output")
    try:
        location = geocode_prepared(args.address) if args.latitude is None else {
            "latitude": args.latitude, "longitude": args.longitude,
        }
        if "source" in location:
            print(location["source"], file=sys.stderr)
        placement = build_placement(args.asset_id, args.name, args.address, {
            **location, "elevationMeters": args.elevation, "headingDegrees": args.heading,
            "metersPerModelUnit": args.scale, "verticalOffsetMeters": args.offset,
            "anchor": "ground-center", "upAxis": "Y",
        }, has_lod=bool(args.lod))
        if args.output:
            print(export_package(args.output, placement, args.model, args.thumbnail, args.provenance, lod_path=args.lod))
        else:
            print(json.dumps(placement, indent=2, allow_nan=False))
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
