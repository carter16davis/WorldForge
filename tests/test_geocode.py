import io
import unittest
from unittest.mock import patch

from app.geocode import Geocoder, geocode


class GeocoderTests(unittest.TestCase):
    """The single geocoder. Two of these used to exist; the point is that one does."""

    def test_prepared_venue_needs_no_network(self):
        with patch("app.geocode.urllib.request.urlopen") as opener:
            result = geocode("MetLife Stadium")
            self.assertEqual(result.latitude, 40.8135)
            self.assertEqual(result.source, "venue-table")
            opener.assert_not_called()

    def test_coordinates_are_accepted_directly(self):
        result = geocode("37.2296, -80.4139")
        self.assertEqual(result.source, "coordinates")
        self.assertAlmostEqual(result.latitude, 37.2296)
        self.assertAlmostEqual(result.longitude, -80.4139)
        self.assertEqual(result.confidence, 1.0)

    def test_offline_never_reaches_the_network(self):
        with patch("app.geocode.urllib.request.urlopen") as opener:
            result = geocode("Somewhere unlisted", allow_network=False)
            opener.assert_not_called()
        self.assertEqual(result.confidence, 0)
        self.assertIn("Offline", result.note)

    def test_ambiguous_match_resolves_to_nothing_and_offers_candidates(self):
        """Silently taking the first of several matches is how a building ends up
        on the wrong continent."""
        payload = (b'[{"display_name":"Springfield, IL","lat":"39.8","lon":"-89.6"},'
                   b'{"display_name":"Springfield, MA","lat":"42.1","lon":"-72.6"}]')
        with patch("app.geocode.GEOCODER", Geocoder()), \
             patch("app.geocode.urllib.request.urlopen", return_value=io.BytesIO(payload)):
            result = geocode("Springfield")
        self.assertEqual(result.confidence, 0)
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.candidates[0]["label"], "Springfield, IL")

    def test_single_match_resolves(self):
        payload = b'[{"display_name":"A & B","lat":"38.0","lon":"-77.0","importance":0.6}]'
        with patch("app.geocode.GEOCODER", Geocoder()), \
             patch("app.geocode.urllib.request.urlopen", return_value=io.BytesIO(payload)):
            result = geocode("A & B")
        self.assertEqual(result.latitude, 38.0)
        self.assertEqual(result.source, "nominatim")
        self.assertGreater(result.confidence, 0.5)

    def test_transport_failure_falls_back_instead_of_raising(self):
        with patch("app.geocode.GEOCODER", Geocoder()), \
             patch("app.geocode.urllib.request.urlopen", side_effect=OSError("no route")):
            result = geocode("Somewhere unreachable")
        self.assertEqual(result.confidence, 0)
        self.assertIn("unavailable", result.note)

    @patch("app.geocode.urllib.request.urlopen")
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

    @patch("app.geocode.time.sleep")
    @patch("app.geocode.urllib.request.urlopen")
    def test_uncached_requests_are_throttled(self, opener, sleep):
        """The Nominatim usage policy is one request per second, and honouring it
        is the difference between a demo and an abusive client."""
        opener.side_effect = [io.BytesIO(b"[]"), io.BytesIO(b"[]")]
        geocoder = Geocoder()
        geocoder.search("First address")
        geocoder.search("Second address")
        self.assertTrue(sleep.called)
        self.assertGreater(sleep.call_args.args[0], 0)

    def test_query_length_is_bounded(self):
        for query in ("ab", "x" * 301):
            with self.subTest(query=len(query)), self.assertRaises(ValueError):
                Geocoder().search(query)


if __name__ == "__main__":
    unittest.main()
