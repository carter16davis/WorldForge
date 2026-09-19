import io
import unittest
from unittest.mock import patch

from geospatial.server import Geocoder, preview
from tests.test_geospatial import state


class ViewerTests(unittest.TestCase):
    def payload(self, **updates):
        return {"assetId": "preview", "name": "Demo", "sourceAddress": "MetLife Stadium", "transform": state(**updates)}

    def test_footprint_rotation_and_export(self):
        north = preview(self.payload())
        east = preview(self.payload(headingDegrees=90, verticalOffsetMeters=12))
        self.assertGreater(north["front"][0], 40.8135)
        self.assertAlmostEqual(north["front"][1], -74.0745)
        self.assertGreater(east["front"][1], -74.0745)
        self.assertAlmostEqual(east["front"][0], 40.8135)
        self.assertEqual(east["placement"]["transform"]["verticalOffsetMeters"], 12)
        self.assertEqual(east["localCorners"][0][2], 12)

    def test_unsupported_map_latitude(self):
        with self.assertRaises(ValueError):
            preview(self.payload(latitude=89))

    @patch("geospatial.server.urlopen")
    def test_demo_needs_no_network(self, opener):
        self.assertEqual(Geocoder().search("MetLife Stadium")[0]["latitude"], 40.8135)
        opener.assert_not_called()

    @patch("geospatial.server.urlopen")
    def test_live_search_is_encoded_and_cached(self, opener):
        opener.return_value = io.BytesIO(b'[{"display_name":"A & B","lat":"38.0","lon":"-77.0"}]')
        geocoder = Geocoder()
        result = geocoder.search("A & B")
        self.assertEqual(result[0]["latitude"], 38)
        self.assertEqual(geocoder.search("a & b"), result)
        opener.assert_called_once()
        request = opener.call_args.args[0]
        self.assertIn("q=A+%26+B", request.full_url)
        self.assertIn("WorldForge", request.headers["User-agent"])

    @patch("geospatial.server.time.sleep")
    @patch("geospatial.server.urlopen")
    def test_uncached_requests_are_throttled(self, opener, sleep):
        opener.side_effect = [io.BytesIO(b'[]'), io.BytesIO(b'[]')]
        geocoder = Geocoder()
        geocoder.search("First address")
        geocoder.search("Second address")
        self.assertTrue(sleep.called)
        self.assertGreater(sleep.call_args.args[0], 0)


if __name__ == "__main__":
    unittest.main()
