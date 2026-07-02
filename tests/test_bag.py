import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

# Add the src directory to the path to import our module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fundatracker import bag, funda  # noqa: E402


class TestBagPureFunctions(unittest.TestCase):
    """Unit tests for the pure geometry/parsing helpers (no network)."""

    def test_addr_key_normalises(self):
        k1 = bag.BagEnricher._addr_key("Baetostraat", "3", "-I", "1055 EP", "Amsterdam")
        k2 = bag.BagEnricher._addr_key("Baetostraat", 3, "-I", "1055ep", "Amsterdam")
        # postal code whitespace stripped + uppercased, house number stringified
        self.assertEqual(k1, k2)
        self.assertEqual(k1, "Baetostraat|3|-I|1055EP|Amsterdam")

    def test_clean_year_rejects_sentinels(self):
        self.assertIsNone(bag.BagEnricher._clean_year(1005))  # BAG monument sentinel
        self.assertIsNone(bag.BagEnricher._clean_year(0))
        self.assertIsNone(bag.BagEnricher._clean_year(None))
        self.assertIsNone(bag.BagEnricher._clean_year("not-a-year"))
        self.assertEqual(bag.BagEnricher._clean_year(1998), 1998)
        self.assertEqual(bag.BagEnricher._clean_year("2015"), 2015)

    def test_point_in_ring(self):
        # unit square [0,0]-[1,1] as [lon, lat] pairs
        square = [[0, 0], [1, 0], [1, 1], [0, 1]]
        self.assertTrue(bag.BagEnricher._point_in_ring(0.5, 0.5, square))
        self.assertFalse(bag.BagEnricher._point_in_ring(1.5, 0.5, square))
        self.assertFalse(bag.BagEnricher._point_in_ring(-0.1, 0.5, square))


class TestBagEnricherCache(unittest.TestCase):
    def test_cache_hit_skips_network(self):
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "bag.json"
            enricher = bag.BagEnricher(cache_path=cache_path)
            key = enricher._addr_key("Straat", "1", "", "1000AA", "Amsterdam")
            enricher._cache[key] = 1997

            with patch.object(enricher, "_ls_lookup") as mock_ls:
                year = enricher.lookup_bouwjaar(
                    "Straat", "1", "", "1000AA", "Amsterdam"
                )

            self.assertEqual(year, 1997)
            mock_ls.assert_not_called()

    def test_failure_returns_none_and_is_cached(self):
        with TemporaryDirectory() as tmp:
            enricher = bag.BagEnricher(cache_path=Path(tmp) / "bag.json")
            with patch.object(
                enricher, "_ls_lookup", side_effect=RuntimeError("boom")
            ) as mock_ls:
                year = enricher.lookup_bouwjaar("X", "1", "", "1000AA", "Amsterdam")
                # second call should hit the cache, not the network again
                year2 = enricher.lookup_bouwjaar("X", "1", "", "1000AA", "Amsterdam")

            self.assertIsNone(year)
            self.assertIsNone(year2)
            mock_ls.assert_called_once()  # cached the None result

    def test_save_and_reload_cache(self):
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "bag.json"
            enricher = bag.BagEnricher(cache_path=cache_path)
            enricher._cache["Straat|1||1000AA|Amsterdam"] = 2001
            enricher.save_cache()

            self.assertEqual(
                json.loads(cache_path.read_text())["Straat|1||1000AA|Amsterdam"], 2001
            )
            reloaded = bag.BagEnricher(cache_path=cache_path)
            self.assertEqual(reloaded._cache["Straat|1||1000AA|Amsterdam"], 2001)

    def test_lookup_uses_bbox_and_swaps_coords(self):
        """The WFS query must use bbox (cql_filter is silently ignored) and the
        returned lat,lon coords must be swapped so point-in-polygon succeeds."""
        with TemporaryDirectory() as tmp:
            enricher = bag.BagEnricher(cache_path=Path(tmp) / "bag.json")

            # A pand polygon around lat=52.5, lon=4.9, expressed lat,lon (urn CRS)
            wfs_payload = {
                "features": [
                    {
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [52.4999, 4.8999],
                                    [52.4999, 4.9001],
                                    [52.5001, 4.9001],
                                    [52.5001, 4.8999],
                                    [52.4999, 4.8999],
                                ]
                            ],
                        },
                        "properties": {"bouwjaar": 1999},
                    }
                ]
            }

            captured = {}

            def fake_get(url, params=None, timeout=None):
                resp = Mock()
                if url == bag.LS_URL:
                    resp.json.return_value = {
                        "response": {"docs": [{"centroide_ll": "POINT(4.9 52.5)"}]}
                    }
                else:  # BAG WFS
                    captured["params"] = params
                    resp.json.return_value = wfs_payload
                return resp

            with patch.object(enricher._session, "get", side_effect=fake_get):
                year = enricher.lookup_bouwjaar(
                    "Straat", "1", "", "1000AA", "Amsterdam"
                )

            self.assertEqual(year, 1999)
            # must use the native bbox parameter, never cql_filter
            self.assertIn("bbox", captured["params"])
            self.assertNotIn("cql_filter", captured["params"])


class TestSchemaAndEnrichment(unittest.TestCase):
    def test_schema_has_bag_bouwjaar(self):
        schema = funda.get_funda_schema()
        self.assertIn("bag_bouwjaar", schema)
        self.assertEqual(schema["bag_bouwjaar"], "INTEGER")

    def test_enrich_bag_bouwjaar_sets_field(self):
        rows = [
            {
                "listing_id": "1",
                "address_street_name": "Baetostraat",
                "address_house_number": "3",
                "address_house_number_suffix": "-I",
                "address_postal_code": "1055EP",
                "address_city": "Amsterdam",
            }
        ]
        fake_enricher = Mock()
        fake_enricher.lookup_bouwjaar.return_value = 1998

        funda.enrich_bag_bouwjaar(rows, enricher=fake_enricher)

        self.assertEqual(rows[0]["bag_bouwjaar"], 1998)
        fake_enricher.lookup_bouwjaar.assert_called_once_with(
            street="Baetostraat",
            housenumber="3",
            suffix="-I",
            postal_code="1055EP",
            city="Amsterdam",
        )

    def test_enrich_bag_bouwjaar_resilient_to_failure(self):
        rows = [{"listing_id": "1", "address_postal_code": "1055EP"}]
        fake_enricher = Mock()
        fake_enricher.lookup_bouwjaar.side_effect = RuntimeError("BAG down")

        # Must not raise, and must store None.
        funda.enrich_bag_bouwjaar(rows, enricher=fake_enricher)
        self.assertIsNone(rows[0]["bag_bouwjaar"])


@unittest.skipUnless(
    os.environ.get("FUNDA_BAG_LIVE") == "1",
    "set FUNDA_BAG_LIVE=1 to run the live PDOK network test",
)
class TestBagLive(unittest.TestCase):
    """Real PDOK calls to validate the end-to-end BAG lookup. Network-gated."""

    def test_known_address_bouwjaar(self):
        with TemporaryDirectory() as tmp:
            enricher = bag.BagEnricher(cache_path=Path(tmp) / "bag.json")
            # Amsterdam Centraal station building; BAG bouwjaar is 1889.
            year = enricher.lookup_bouwjaar(
                "Stationsplein", "1", "", "1012AB", "Amsterdam"
            )
            self.assertIsNotNone(year)
            self.assertTrue(1850 <= year <= 1900, f"unexpected year {year}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
