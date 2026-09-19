import unittest

from geospatial import (
    build_placement,
    enu_offset,
    latlon_from_enu,
    local_to_enu,
    meters_per_degree,
    validate_transform,
)


def state(**updates):
    return {
        "latitude": 40.8135, "longitude": -74.0745, "elevationMeters": 0,
        "headingDegrees": 0, "metersPerModelUnit": 1, "verticalOffsetMeters": 0,
        "anchor": "ground-center", "upAxis": "Y", **updates,
    }


class PlacementTests(unittest.TestCase):
    def test_invalid_values(self):
        for key, value in (
            ("latitude", 91), ("longitude", -181), ("latitude", True),
            ("elevationMeters", float("nan")), ("headingDegrees", float("inf")),
            ("metersPerModelUnit", 0), ("metersPerModelUnit", -1),
            ("verticalOffsetMeters", "2"), ("upAxis", "W"), ("anchor", "center"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_transform(state(**{key: value}))

    def test_both_conventions_are_accepted(self):
        """A producer must be able to declare its real frame, not the one we prefer.

        Rejecting Z-up only teaches a producer to claim Y-up and ship a Z-up
        mesh, which is the failure the field exists to catch.
        """
        for anchor in ("ground-center", "ground-origin"):
            for up in ("Y", "Z"):
                with self.subTest(anchor=anchor, upAxis=up):
                    result = validate_transform(state(anchor=anchor, upAxis=up))
                    self.assertEqual(result["anchor"], anchor)
                    self.assertEqual(result["upAxis"], up)

    def test_heading_and_ui_roundtrip(self):
        placement = build_placement("venue-1", "Venue", "Address", state(headingDegrees=-90))
        self.assertEqual(placement["transform"]["headingDegrees"], 270)
        self.assertNotIn("low", placement["models"])
        edited = {**placement["location"], **placement["transform"], "verticalOffsetMeters": 3}
        self.assertEqual(
            build_placement("venue-1", "Venue", "Address", edited)["transform"]["verticalOffsetMeters"], 3
        )

    def test_ground_anchor_scale_and_cardinal_headings(self):
        low, high = (-1, -2, -1), (1, 4, 1)
        self.assertEqual(local_to_enu((0, -2, 0), low, high, state(verticalOffsetMeters=3)), (0, 0, 3))
        for heading, expected in ((0, (0, 2)), (90, (2, 0)), (180, (0, -2)), (270, (-2, 0))):
            with self.subTest(heading=heading):
                result = local_to_enu((0, -2, -1), low, high,
                                      state(headingDegrees=heading, metersPerModelUnit=2))
                self.assertAlmostEqual(result[0], expected[0])
                self.assertAlmostEqual(result[1], expected[1])
        self.assertEqual(local_to_enu((0, 4, 0), low, high, state(metersPerModelUnit=2))[2], 12)

    def test_z_up_matches_y_up_for_the_same_building(self):
        """The two conventions must describe the same building, not two buildings.

        A point one unit north and two up, expressed in each frame, has to land
        on the same east/north/up triple — otherwise 'upAxis' is decoration.
        """
        y_low, y_high = (-1, 0, -1), (1, 4, 1)          # glTF: -Z north, +Y up
        z_low, z_high = (-1, -1, 0), (1, 1, 4)          # ENU:  +Y north, +Z up
        y = local_to_enu((0, 2, -1), y_low, y_high, state(upAxis="Y"))
        z = local_to_enu((0, 1, 2), z_low, z_high, state(upAxis="Z"))
        for a, b in zip(y, z):
            self.assertAlmostEqual(a, b)

    def test_ground_origin_does_not_recentre(self):
        low, high = (10, 5, 10), (20, 25, 20)
        centred = local_to_enu((10, 5, 10), low, high, state(anchor="ground-center"))
        raw = local_to_enu((10, 5, 10), low, high, state(anchor="ground-origin"))
        self.assertEqual(centred, (-5.0, 5.0, 0.0))
        self.assertEqual(raw, (10.0, -10.0, 5.0))

    def test_path_traversal_rejected(self):
        for asset_id in ("../escape", "/tmp/escape", "", ".", "a/b"):
            with self.subTest(asset_id=asset_id), self.assertRaises(ValueError):
                build_placement(asset_id, "Venue", "Address", state())


class GeodesyTests(unittest.TestCase):
    def test_meters_per_degree_is_plausible(self):
        m_lat, m_lon = meters_per_degree(0)
        self.assertAlmostEqual(m_lat, 110574, delta=60)
        self.assertAlmostEqual(m_lon, 111320, delta=60)
        # Longitude degrees shrink toward the poles; latitude degrees barely move.
        self.assertLess(meters_per_degree(60)[1], m_lon / 1.9)
        self.assertGreater(meters_per_degree(60)[0], m_lat)

    def test_enu_offset_round_trips(self):
        origin = (40.8135, -74.0745)
        point = (40.8142, -74.0731)
        east, north = enu_offset(point, origin)
        self.assertGreater(east, 0)      # further east
        self.assertGreater(north, 0)     # further north
        back = latlon_from_enu(east, north, origin)
        self.assertAlmostEqual(back[0], point[0], places=9)
        self.assertAlmostEqual(back[1], point[1], places=9)


if __name__ == "__main__":
    unittest.main()
