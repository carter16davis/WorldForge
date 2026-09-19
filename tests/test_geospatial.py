import json
import struct
import tempfile
import unittest
from pathlib import Path

from geospatial import build_placement, export_package, geocode_prepared, local_to_enu, validate_transform


def state(**updates):
    return {
        "latitude": 40.8135, "longitude": -74.0745, "elevationMeters": 0,
        "headingDegrees": 0, "metersPerModelUnit": 1, "verticalOffsetMeters": 0,
        "anchor": "ground-center", "upAxis": "Y", **updates,
    }


class PlacementTests(unittest.TestCase):
    def test_prepared_lookup_and_unknown_address(self):
        self.assertEqual(geocode_prepared("  MetLife Stadium ")["latitude"], 40.8135)
        with self.assertRaises(ValueError):
            geocode_prepared("Unknown building")

    def test_invalid_values(self):
        for key, value in (
            ("latitude", 91), ("longitude", -181), ("latitude", True),
            ("elevationMeters", float("nan")), ("headingDegrees", float("inf")),
            ("metersPerModelUnit", 0), ("metersPerModelUnit", -1),
            ("verticalOffsetMeters", "2"), ("upAxis", "Z"), ("anchor", "center"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_transform(state(**{key: value}))

    def test_heading_and_ui_roundtrip(self):
        placement = build_placement("venue-1", "Venue", "Address", state(headingDegrees=-90))
        self.assertEqual(placement["transform"]["headingDegrees"], 270)
        self.assertNotIn("low", placement["models"])
        edited = {**placement["location"], **placement["transform"], "verticalOffsetMeters": 3}
        self.assertEqual(build_placement("venue-1", "Venue", "Address", edited)["transform"]["verticalOffsetMeters"], 3)

    def test_ground_anchor_scale_and_cardinal_headings(self):
        low, high = (-1, -2, -1), (1, 4, 1)
        self.assertEqual(local_to_enu((0, -2, 0), low, high, state(verticalOffsetMeters=3)), (0, 0, 3))
        for heading, expected in ((0, (0, 2)), (90, (2, 0)), (180, (0, -2)), (270, (-2, 0))):
            result = local_to_enu((0, -2, -1), low, high, state(headingDegrees=heading, metersPerModelUnit=2))
            self.assertAlmostEqual(result[0], expected[0])
            self.assertAlmostEqual(result[1], expected[1])
        self.assertEqual(local_to_enu((0, 4, 0), low, high, state(metersPerModelUnit=2))[2], 12)

    def test_path_traversal_rejected(self):
        for asset_id in ("../escape", "/tmp/escape", "", ".", "a/b"):
            with self.assertRaises(ValueError):
                build_placement(asset_id, "Venue", "Address", state())

    def test_export_and_refusal_to_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Minimal glTF JSON chunk; packaging tests do not imply renderability.
            content = b'{"asset":{"version":"2.0"}}'
            content += b" " * (-len(content) % 4)
            model = root / "input.glb"
            model.write_bytes(struct.pack("<4sII", b"glTF", 2, 20 + len(content)) + struct.pack("<I4s", len(content), b"JSON") + content)
            thumb = root / "input.webp"
            thumb.write_bytes(b"RIFF\x04\x00\x00\x00WEBP")
            provenance = root / "source.json"
            provenance.write_text('{"schemaVersion": 1, "sourceMedia": []}')
            placement = build_placement("venue-1", "Venue", "Address", state())
            output = export_package(root / "exports", placement, model, thumb, provenance)
            self.assertEqual(json.loads((output / "placement.json").read_text()), placement)
            self.assertEqual((output / "building.glb").read_bytes(), model.read_bytes())
            self.assertEqual({p.name for p in output.iterdir()}, {"building.glb", "thumbnail.webp", "placement.json", "provenance.json"})
            with self.assertRaises(FileExistsError):
                export_package(root / "exports", placement, model, thumb, provenance)
            model.write_bytes(b"invalid GLB")
            placement["assetId"] = "invalid"
            with self.assertRaises(ValueError):
                export_package(root / "exports", placement, model, thumb, provenance)
            self.assertFalse((root / "exports" / "invalid").exists())


if __name__ == "__main__":
    unittest.main()
