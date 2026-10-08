import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

from fundatracker import funda
from tests.fixtures import (
    EMPTY_RESPONSE,
    MINIMAL_RESPONSE,
    SAMPLE_RESPONSE,
)

# Add the src directory to the path to import our module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestFundaFunctions(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures before each test method."""

        self.sample_response = SAMPLE_RESPONSE
        self.minimal_response = MINIMAL_RESPONSE
        self.empty_response = EMPTY_RESPONSE

        funda.get_listing_insights.cache_clear()

        # Skip the homepage warm-up so tests count only search requests.
        warm_up_patcher = patch("fundatracker.funda._warm_up_search_session")
        self.mock_warm_up = warm_up_patcher.start()
        self.addCleanup(warm_up_patcher.stop)

    def test_get_funda_schema(self):
        """Test that get_funda_schema returns expected schema structure."""
        schema = funda.get_funda_schema()

        # Test that it returns a dictionary
        self.assertIsInstance(schema, dict)

        # Test some key fields exist
        expected_fields = [
            "id",
            "agent_id",
            "listing_id",
            "address_country",
            "address_city",
            "price",
            "number_of_bedrooms",
            "number_of_rooms",
        ]

        for field in expected_fields:
            self.assertIn(field, schema)

        # Test that id field is primary key
        self.assertIn("PRIMARY KEY", schema["id"])

        # Test that integer fields are defined correctly
        self.assertEqual(schema["number_of_bedrooms"], "INTEGER")
        self.assertEqual(schema["price"], "INTEGER")

    def _search_html(self):
        """A minimal funda SSR search page: two listings embedded in a Nuxt
        __NUXT_DATA__ devalue payload (a flat array whose containers hold
        integer indices into the array)."""
        devalue = [
            {"root": 1},  # 0
            [2, 8],  # 1  array of listing references
            {  # 2  listing A
                "id": 3,
                "publish_date": 4,
                "object_detail_page_relative_url": 5,
                "address": 6,
                "price": 14,
            },
            111,  # 3
            "2026-08-19T06:00:00+02:00",  # 4
            "/detail/koop/amsterdam/appartement-111/",  # 5
            {"postal_code": 7, "country": 15},  # 6
            "1061AB",  # 7
            {  # 8  listing B
                "id": 9,
                "publish_date": 10,
                "object_detail_page_relative_url": 11,
                "address": 12,
                "price": 14,
            },
            222,  # 9
            "2026-08-01T06:00:00+02:00",  # 10
            "/detail/koop/amsterdam/appartement-222/",  # 11
            {"postal_code": 13, "country": 15},  # 12
            "1061CD",  # 13
            {"selling_price": 16},  # 14  (shared by both listings)
            "NL",  # 15
            [17],  # 16  (array elements are themselves references)
            500000,  # 17
        ]
        return (
            '<html><body><script id="__NUXT_DATA__" type="application/json">'
            + json.dumps(devalue)
            + "</script></body></html>"
        )

    @patch("fundatracker.funda._search_session.get")
    def test_get_results_success(self, mock_get):
        """get_results scrapes the SSR page and reshapes it into hits."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = self._search_html()
        mock_get.return_value = mock_response

        # no_preference => no date cutoff, so both listings are returned
        result = funda.get_results(postal_code4=1061, km_radius=2)

        # Only one page is fetched (2 listings < a full page)
        mock_get.assert_called_once()

        hits = result["responses"][0]["hits"]["hits"]
        self.assertEqual(result["responses"][0]["hits"]["total"]["value"], 2)
        self.assertEqual([h["_id"] for h in hits], ["111", "222"])
        # _source keeps the OpenSearch document shape parse_funda_results expects
        self.assertEqual(hits[0]["_source"]["address"]["postal_code"], "1061AB")
        self.assertEqual(hits[0]["_source"]["price"]["selling_price"], [500000])

    @patch("fundatracker.funda._search_session.get")
    def test_get_results_warms_up_session(self, mock_get):
        """get_results warms the session up before the first search request."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = self._search_html()
        mock_get.return_value = mock_response

        funda.get_results(postal_code4=1061, km_radius=2)

        self.mock_warm_up.assert_called_once()

    @patch("fundatracker.funda._search_session.get")
    def test_get_results_does_not_override_user_agent(self, mock_get):
        """The impersonated User-Agent must not be replaced (Akamai checks it)."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = self._search_html()
        mock_get.return_value = mock_response

        funda.get_results(postal_code4=1061, km_radius=2)

        headers = mock_get.call_args.kwargs.get("headers", {})
        self.assertNotIn("user-agent", {key.lower() for key in headers})

    @patch("fundatracker.funda._search_session.get")
    def test_get_results_start_index_short_circuits(self, mock_get):
        """A non-zero start_index returns nothing (pagination is internal)."""
        result = funda.get_results(postal_code4=1061, km_radius=2, start_index=100)
        self.assertEqual(result["responses"][0]["hits"]["total"]["value"], 0)
        mock_get.assert_not_called()

    @patch("fundatracker.funda._search_session.get")
    def test_get_results_failure(self, mock_get):
        """get_results raises on a non-200 response from funda."""
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.text = "Access Denied"
        mock_get.return_value = mock_response

        with self.assertRaises(Exception) as context:
            funda.get_results(postal_code4=1000, km_radius=5)

        self.assertIn("Failed to get results from funda", str(context.exception))
        self.assertIn("403", str(context.exception))

    @patch("fundatracker.funda._search_session.get")
    def test_get_results_parameters(self, mock_get):
        """get_results builds the expected funda.nl search URL."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = self._search_html()
        mock_get.return_value = mock_response

        funda.get_results(
            postal_code4=1000,
            km_radius=15,
            publication_date="now-3d",
            offering_type="buy",
        )

        mock_get.assert_called_once()

        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(mock_get.call_args[0][0])
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/zoeken/koop")
        self.assertEqual(query["selected_area"], ['["1000,15km"]'])
        self.assertEqual(query["sort"], ['"publish_date_utc_desc"'])

    @patch("fundatracker.funda.get_neighbourhood_insights")
    def test_parse_funda_results(self, mock_neighbourhood_insights):
        """Test parse_funda_results with the new API format."""
        # Mock neighbourhood insights
        mock_neighbourhood_insights.return_value = {
            "inhabitants": 50000,
            "averageAskingPricePerM2": 8500,
            "familiesWithChildren": 0.35,
        }

        result = funda.parse_funda_results(
            self.sample_response, use_listing_insights=False
        )

        # Check that we got results
        self.assertEqual(len(result), 1)

        # Check key fields in parsed result
        parsed_listing = result[0]
        self.assertEqual(parsed_listing["listing_id"], "6965113")
        self.assertEqual(parsed_listing["agent_id"], 24581)
        self.assertEqual(parsed_listing["agent_name"], "Tel Krop Makelaars")
        self.assertEqual(parsed_listing["address_city"], "Amsterdam")
        self.assertEqual(parsed_listing["address_neighbourhood"], "Landlust")
        self.assertEqual(parsed_listing["price"], 375000)
        self.assertEqual(parsed_listing["number_of_bedrooms"], 2)
        self.assertEqual(parsed_listing["number_of_rooms"], 3)
        self.assertEqual(parsed_listing["object_type"], "apartment")
        self.assertEqual(parsed_listing["energy_label"], "D")
        self.assertEqual(parsed_listing["floor_area"], 51)
        self.assertEqual(parsed_listing["plot_area"], 0)

        # Check neighbourhood insights were added
        self.assertEqual(parsed_listing["neighbourhood_inhabitants"], 50000)
        self.assertEqual(parsed_listing["neighbourhood_avg_askingprice_m2"], 8500)
        self.assertEqual(
            parsed_listing["neighbourhood_families_with_children_pct"], 0.35
        )

        # Check key fields in parsed result
        parsed_listing = result[0]
        self.assertEqual(parsed_listing["listing_id"], "6965113")
        self.assertEqual(parsed_listing["agent_id"], 24581)
        self.assertEqual(parsed_listing["agent_name"], "Tel Krop Makelaars")
        self.assertEqual(parsed_listing["address_city"], "Amsterdam")
        self.assertEqual(parsed_listing["address_neighbourhood"], "Landlust")
        self.assertEqual(parsed_listing["price"], 375000)
        self.assertEqual(parsed_listing["number_of_bedrooms"], 2)
        self.assertEqual(parsed_listing["number_of_rooms"], 3)
        self.assertEqual(parsed_listing["object_type"], "apartment")
        self.assertEqual(parsed_listing["energy_label"], "D")
        self.assertEqual(parsed_listing["floor_area"], 51)
        self.assertEqual(parsed_listing["plot_area"], 0)

        # Check neighbourhood insights were added
        self.assertEqual(parsed_listing["neighbourhood_inhabitants"], 50000)
        self.assertEqual(parsed_listing["neighbourhood_avg_askingprice_m2"], 8500)
        self.assertEqual(
            parsed_listing["neighbourhood_families_with_children_pct"], 0.35
        )

    def test_parse_funda_results_invalid_format(self):
        """Test parse_funda_results with invalid data format."""
        invalid_data = {"invalid": "structure"}

        with self.assertRaises(Exception) as context:
            funda.parse_funda_results(invalid_data)

        self.assertIn("Failed to parse results", str(context.exception))

    @patch("fundatracker.funda.requests.get")
    def test_get_listing_insights_success(self, mock_get):
        """Test get_listing_insights with successful response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"nrOfViews": 150, "nrOfSaves": 25}
        mock_get.return_value = mock_response

        result = funda.get_listing_insights("12345")

        self.assertEqual(result["nrOfViews"], 150)
        self.assertEqual(result["nrOfSaves"], 25)
        mock_get.assert_called_once()

        # Check URL construction
        call_args = mock_get.call_args
        expected_url = "https://marketinsights.funda.io/v1/objectinsights/12345"
        self.assertEqual(call_args[0][0], expected_url)

    @patch("fundatracker.funda.requests.get")
    def test_get_listing_insights_no_content(self, mock_get):
        """Test get_listing_insights with 204 response."""
        mock_response = Mock()
        mock_response.status_code = 204
        mock_get.return_value = mock_response

        result = funda.get_listing_insights("12345")

        self.assertEqual(result, {})

    @patch("fundatracker.funda.requests.get")
    def test_get_listing_insights_error(self, mock_get):
        """Test get_listing_insights with error response."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_get.return_value = mock_response

        result = funda.get_listing_insights("12345")

        # Should return empty dict on error
        self.assertEqual(result, {})

    @patch("fundatracker.funda.requests.get")
    @patch("fundatracker.funda.xxhash.xxh64")
    def test_get_neighbourhood_insights_success(self, mock_xxhash, mock_get):
        """Test get_neighbourhood_insights with successful response."""
        # Mock hash function
        mock_hash = Mock()
        mock_hash.hexdigest.return_value = "test_hash"
        mock_xxhash.return_value = mock_hash

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "inhabitants": 50000,
            "averageAskingPricePerM2": 8500,
            "familiesWithChildren": 0.35,
        }
        mock_get.return_value = mock_response

        result = funda.get_neighbourhood_insights("Amsterdam", "Landlust")

        self.assertEqual(result["inhabitants"], 50000)
        self.assertEqual(result["averageAskingPricePerM2"], 8500)
        mock_get.assert_called_once()

        # Check URL construction
        call_args = mock_get.call_args
        expected_url = "https://marketinsights.funda.io/v2/LocalInsights/preview/Amsterdam/Landlust"
        self.assertEqual(call_args[0][0], expected_url)

    @patch("fundatracker.funda.get_neighbourhood_insights")
    def test_parse_funda_results_missing_optional_fields(self, mock_insights):
        """Test parse_funda_results handles missing optional fields gracefully."""
        mock_insights.return_value = {}

        result = funda.parse_funda_results(
            self.minimal_response, use_listing_insights=False
        )

        # Should still parse without errors
        self.assertEqual(len(result), 1)
        parsed = result[0]
        self.assertEqual(parsed["listing_id"], "test123")
        self.assertEqual(parsed["address_country"], "NL")
        self.assertEqual(parsed["url_path"], "/test/path/")

        # Check that missing fields are handled gracefully
        self.assertIsNone(parsed["price"])
        self.assertEqual(parsed["agent_name"], "")
        self.assertIsNone(parsed["floor_area"])

    @patch("fundatracker.funda.get_neighbourhood_insights")
    @patch("fundatracker.funda.get_listing_insights")
    def test_parse_funda_results_with_listing_insights(
        self, mock_listing_insights, mock_neighbourhood_insights
    ):
        """Test parse_funda_results with listing insights enabled."""
        # Mock insights
        mock_neighbourhood_insights.return_value = {"inhabitants": 50000}
        mock_listing_insights.return_value = {"nrOfViews": 100, "nrOfSaves": 20}

        result = funda.parse_funda_results(
            self.sample_response, use_listing_insights=True
        )

        # Check that listing insights were fetched and added
        parsed_listing = result[0]
        self.assertEqual(parsed_listing["listing_nr_of_views"], 100)
        self.assertEqual(parsed_listing["listing_nr_of_saves"], 20)

        # Verify the insights function was called with correct ID
        mock_listing_insights.assert_called_once_with("6965113")

    def test_parse_funda_results_edge_cases(self):
        """Test parse_funda_results with edge cases and unusual data structures."""
        with patch("fundatracker.funda.get_neighbourhood_insights") as mock_insights:
            mock_insights.return_value = {}
            result = funda.parse_funda_results(
                self.empty_response, use_listing_insights=False
            )
            self.assertEqual(len(result), 0)

    @patch("fundatracker.funda.get_neighbourhood_insights")
    def test_parse_funda_results_complex_fields(self, mock_insights):
        """Test parsing of complex fields like amenities, surrounding, etc."""
        mock_insights.return_value = {}

        result = funda.parse_funda_results(
            self.sample_response, use_listing_insights=False
        )
        parsed = result[0]

        # Check complex field parsing
        self.assertEqual(parsed["amenities"], "balcony,garden")
        self.assertEqual(parsed["surrounding"], "park,school")
        self.assertEqual(parsed["construction_date_range"], "1980~1990")
        self.assertEqual(parsed["description"], "Mooie woning")


class TestWarmUpSearchSession(unittest.TestCase):
    def setUp(self):
        funda._search_session_warmed = False
        self.addCleanup(setattr, funda, "_search_session_warmed", False)

    @patch("fundatracker.funda._search_session.get")
    def test_warm_up_visits_homepage_once(self, mock_get):
        """The homepage is fetched on the first call only."""
        mock_get.return_value = Mock(status_code=200)

        funda._warm_up_search_session()
        funda._warm_up_search_session()

        mock_get.assert_called_once_with(funda.FUNDA_HOME_URL)

    @patch("fundatracker.funda._search_session.get")
    def test_warm_up_failure_is_not_fatal(self, mock_get):
        """A non-200 warm-up only logs; the search itself decides success."""
        mock_get.return_value = Mock(status_code=403)

        with self.assertLogs(level="WARNING"):
            funda._warm_up_search_session()


if __name__ == "__main__":
    # Run tests with more verbose output
    unittest.main(verbosity=2)
