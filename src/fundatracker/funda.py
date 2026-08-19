import datetime
import json
import logging
import re
import time
import urllib.parse
import uuid
from functools import lru_cache
from typing import Literal

import xxhash
from curl_cffi import requests

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"

run_id = str(uuid.uuid4())
neighbourhood_insights = {}


def get_authorization_key():
    # Funda seems to use a basic auth key (base64 encoded) for all anonymous requests
    return "Basic ZjVhMjQyZGIxZmUwOjM5ZDYxMjI3LWQ1YTgtNDIxMi04NDY4LWU1NWQ0MjhjMmM2Zg=="


def get_funda_schema():
    return {
        "id": "VARCHAR(100) PRIMARY KEY",
        "agent_id": "VARCHAR(100)",
        "agent_url": "VARCHAR(500)",
        "listing_id": "VARCHAR(100)",
        "agent_name": "VARCHAR(500)",
        "agent_association": "VARCHAR(500)",
        "address_country": "VARCHAR(100)",
        "address_province": "VARCHAR(100)",
        "address_city": "VARCHAR(100)",
        "address_neighbourhood": "VARCHAR(100)",
        "address_municipality": "VARCHAR(100)",
        "address_house_number": "VARCHAR(100)",
        "address_house_number_suffix": "VARCHAR(100)",
        "address_postal_code": "VARCHAR(100)",
        "address_street_name": "VARCHAR(500)",
        "number_of_bedrooms": "INTEGER",
        "number_of_rooms": "INTEGER",
        "object_type": "VARCHAR(100)",
        "energy_label": "VARCHAR(100)",
        "floor_area": "INTEGER",
        "plot_area": "INTEGER",
        "publish_date": "TIMESTAMP",
        "url_path": "VARCHAR(500)",
        "status": "VARCHAR(100)",
        "price": "INTEGER",
        "price_type": "VARCHAR(100)",
        "price_condition": "VARCHAR(100)",
        "placement_type": "VARCHAR(100)",
        "availability": "VARCHAR(100)",
        "amenities": "VARCHAR(1000)",
        "construction_date_range": "VARCHAR(100)",
        "construction_period": "VARCHAR(100)",
        "construction_type": "VARCHAR(100)",
        "handover_date_range": "VARCHAR(100)",
        "offering_type": "VARCHAR(100)",
        "project": "VARCHAR(100)",
        "sale_date_range": "VARCHAR(100)",
        "selected_area": "VARCHAR(100)",
        "description": "VARCHAR",
        "description_tags": "VARCHAR(1000)",
        "zoning": "VARCHAR(100)",
        "surrounding": "VARCHAR(1000)",
        "exterior_space_garden_size": "VARCHAR(100)",
        "exterior_space_type": "VARCHAR(100)",
        "exterior_space_garden_orientation": "VARCHAR(100)",
        "garage_capacity": "VARCHAR(100)",
        "garage_type": "VARCHAR(100)",
        "neighbourhood_inhabitants": "INTEGER",
        "neighbourhood_avg_askingprice_m2": "INTEGER",
        "neighbourhood_families_with_children_pct": "REAL",
        "listing_nr_of_saves": "INTEGER",
        "listing_nr_of_views": "INTEGER",
        "search_query": "VARCHAR(500)",
        "_processing_time": "TIMESTAMP",
        "_run_id": "VARCHAR(100)",
    }


# Funda retired its anonymous OpenSearch backend (listing-search-wonen.funda.io):
# it now returns HTTP 401 "no token provided", and the replacement host
# (…funda.nl) sits behind Akamai Bot Manager. The public www.funda.nl search
# pages, however, are server-side rendered and still reachable with curl_cffi's
# browser impersonation. Each page embeds the same listing objects (the former
# OpenSearch `_source` documents) in a Nuxt `__NUXT_DATA__` payload, so we scrape
# those and re-shape them into the `hits.hits[]._source` structure the rest of
# this module already expects.
SEARCH_PAGE_SIZE = 15  # funda renders 15 listings per SSR page
MAX_SEARCH_PAGES = 40  # safety cap (~600 listings) mirroring the ES window cap

# Map publication_date to a "listed within N days" cutoff.
PUBLICATION_DATE_DAYS = {
    "now-1d": 1,
    "now-3d": 3,
    "now-5d": 5,
    "now-10d": 10,
    "now-30d": 30,
    "no_preference": None,
}

# One session so Akamai's bot cookies persist across paginated requests.
_search_session = requests.Session(impersonate="chrome")


def _deref_nuxt(ref, data):
    """Resolve one Nuxt devalue pointer (an index into the flat `data` array).

    devalue stores every value once in a flat array; containers hold integer
    indices into it. A pointer dereferences exactly once — the value found is
    the literal (a scalar int is a real number, not a further index). Negative
    indices are devalue sentinels (undefined/NaN), returned as None.
    """
    if not isinstance(ref, int):
        return _materialise_nuxt(ref, data)
    if ref < 0 or ref >= len(data):
        return None
    return _materialise_nuxt(data[ref], data)


def _materialise_nuxt(raw, data):
    if isinstance(raw, dict):
        return {k: _deref_nuxt(v, data) for k, v in raw.items()}
    if isinstance(raw, list):
        return [_deref_nuxt(x, data) for x in raw]
    return raw  # scalar literal


def _parse_listings_from_html(html):
    """Extract the listing `_source` documents from a funda SSR search page."""
    match = re.search(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        return []
    data = json.loads(match.group(1))
    # Find the array of listing references: a short int array whose every
    # element resolves to a dict carrying `object_detail_page_relative_url`.
    for node in data:
        if (
            isinstance(node, list)
            and 1 <= len(node) <= 40
            and all(isinstance(x, int) for x in node)
        ):
            candidates = [data[x] for x in node if 0 <= x < len(data)]
            if candidates and all(
                isinstance(c, dict) and "object_detail_page_relative_url" in c
                for c in candidates
            ):
                return [_materialise_nuxt(data[ref], data) for ref in node]
    return []


def get_results(
    postal_code4: Literal[1000, 9999],
    km_radius: Literal[1, 2, 5, 10, 15, 30, 50, 100, None] = 1,
    publication_date: Literal[
        "now-1d", "now-3d", "now-5d", "now-10d", "now-30d", "no_preference"
    ] = "no_preference",
    offering_type: Literal["buy", "rent"] = "buy",
    start_index: int = 0,
    page_size: int = 100,
):
    """
    Get property listings by scraping funda.nl's server-side-rendered search
    pages, returned in the same shape as the former OpenSearch API response.

    All matching listings (newest first, filtered to `publication_date`) are
    returned in a single call, so pagination happens here rather than in the
    caller: any call with a non-zero `start_index` returns no hits.

    Args:
        postal_code4: 4-digit postal code for location search
        km_radius: Search radius in kilometers
        publication_date: Only return listings published within this window
        offering_type: Type of offering (buy/rent)
        start_index: Non-zero short-circuits to an empty response (see above)
        page_size: Unused; kept for signature compatibility

    Returns:
        dict: {"responses": [{"hits": {"total": {"value": N}, "hits": [...]}}]}
    """
    if start_index:
        return {"responses": [{"hits": {"total": {"value": 0}, "hits": []}}]}

    days = PUBLICATION_DATE_DAYS.get(publication_date)
    cutoff = None
    if days is not None:
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)

    # Postal code plus optional radius, e.g. ["1061,2km"].
    area = f"{postal_code4},{km_radius}km" if km_radius else str(postal_code4)
    path = "koop" if offering_type == "buy" else "huur"
    base_url = f"https://www.funda.nl/zoeken/{path}"

    hits = []
    for page in range(1, MAX_SEARCH_PAGES + 1):
        params = {
            "selected_area": json.dumps([area]),
            # Newest first so we can stop paging once past the date cutoff.
            "sort": json.dumps("publish_date_utc_desc"),
        }
        if page > 1:
            params["search_result"] = page
        url = f"{base_url}?{urllib.parse.urlencode(params)}"

        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.funda.nl/",
            "User-Agent": USER_AGENT,
        }
        res = _search_session.get(url, headers=headers, impersonate="chrome")
        if res.status_code != 200:
            raise Exception(
                f"Failed to get results from funda. Status code: {res.status_code}. Response: {res.text}"
            )

        listings = _parse_listings_from_html(res.text)
        if not listings:
            break

        reached_cutoff = False
        for listing in listings:
            publish_date = listing.get("publish_date")
            if cutoff is not None and publish_date:
                try:
                    if datetime.datetime.fromisoformat(publish_date) < cutoff:
                        reached_cutoff = True
                        break
                except ValueError:
                    pass
            hits.append({"_id": str(listing.get("id")), "_source": listing})

        # Stop once we hit an older listing or funda runs out of pages.
        if reached_cutoff or len(listings) < SEARCH_PAGE_SIZE:
            break

    return {"responses": [{"hits": {"total": {"value": len(hits)}, "hits": hits}}]}


@lru_cache
def get_listing_insights(listing_id):
    url = f"https://marketinsights.funda.io/v1/objectinsights/{listing_id}"

    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": get_authorization_key(),
    }

    res = requests.get(url, headers=headers, impersonate="chrome")

    if res.status_code == 200:
        return res.json()
    if res.status_code == 204:
        # No insights available
        return {}
    else:
        logging.error(
            f"Failed to get listing insights for {listing_id}. Status code: {res.status_code}. Response: {res.text}"
        )
        return {}


@lru_cache(maxsize=2400)
def get_neighbourhood_insights(city, neighbourhood):
    global neighbourhood_insights
    neighbourhood = neighbourhood.replace("/", "-").replace(" ", "-").replace("--", "-")
    neighbourhood_key = xxhash.xxh64(f"{city}-{neighbourhood}").hexdigest()
    if neighbourhood_key in neighbourhood_insights:
        return neighbourhood_insights[neighbourhood_key]

    url = f"https://marketinsights.funda.io/v2/LocalInsights/preview/{city}/{neighbourhood}"

    headers = {"User-Agent": USER_AGENT}

    res = requests.get(url, headers=headers, impersonate="chrome")

    if res.status_code == 200:
        neighbourhood_insights[neighbourhood_key] = res.json()
        return neighbourhood_insights[neighbourhood_key]
    if res.status_code == 204:
        # No insights available
        return {}
    else:
        logging.error(
            f"Failed to get listing insights for {city} - {neighbourhood}. Status code: {res.status_code}. Response: {res.text}"
        )
        return {}


def parse_funda_results(results_object, use_listing_insights=True):
    """
    Parse Funda API results from the new API format.

    Args:
        results_object: API response object
        use_listing_insights: Whether to fetch additional listing insights

    Returns:
        list: Parsed listing data
    """
    try:
        listings = results_object["responses"][0]["hits"]["hits"]
    except Exception as e:
        raise Exception(f"Failed to parse results. Error: {e} — Got: {results_object}")

    logging.debug(f"Parsing {len(listings)} listings...")
    parsed_results = []
    for listing in listings:
        try:
            listing_details = listing["_source"]
            listing_parsed = {
                "listing_id": listing["_id"],
                "agent_id": listing_details.get("agent", [{}])[0].get("id", ""),
                "agent_url": listing_details.get("agent", [{}])[0].get(
                    "relative_url", ""
                ),
                "agent_name": listing_details.get("agent", [{}])[0].get("name", ""),
                "agent_association": listing_details.get("agent", [{}])[0].get(
                    "association", ""
                ),
                "address_country": listing_details["address"]["country"],
                "address_province": listing_details["address"].get("province", ""),
                "address_city": listing_details["address"].get("city", ""),
                "address_neighbourhood": listing_details["address"].get(
                    "neighbourhood", ""
                ),
                "address_municipality": listing_details["address"].get(
                    "municipality", ""
                ),
                "address_house_number": listing_details["address"].get(
                    "house_number", ""
                ),
                "address_house_number_suffix": listing_details["address"].get(
                    "house_number_suffix", ""
                ),
                "address_postal_code": listing_details["address"]["postal_code"],
                "address_street_name": listing_details["address"].get(
                    "street_name", ""
                ),
                "number_of_bedrooms": listing_details.get("number_of_bedrooms", None),
                "number_of_rooms": listing_details.get("number_of_rooms", None),
                "object_type": listing_details.get("object_type", None),
                "energy_label": listing_details.get("energy_label", None),
                "floor_area": listing_details.get("floor_area", [None])[0],
                "plot_area": listing_details.get("plot_area", [None])[0],
                "publish_date": listing_details["publish_date"],
                "url_path": listing_details["object_detail_page_relative_url"],
                "status": listing_details.get("status", ""),
                "price": listing_details["price"].get("selling_price", [None])[0],
                "price_type": listing_details["price"].get("selling_price_type", ""),
                "price_condition": listing_details["price"].get(
                    "selling_price_condition", ""
                ),
                "placement_type": listing_details.get("placement_type", ""),
                "availability": listing_details.get("availability", ""),
                "amenities": ",".join(listing_details.get("amenities", [])),
                "construction_date_range": f"{listing_details.get('construction_date_range', {}).get('gte', '')}~{listing_details.get('construction_date_range', {}).get('lte', '')}",
                "construction_period": listing_details.get("construction_period", ""),
                "construction_type": listing_details.get("construction_type", ""),
                "offering_type": listing_details.get("offering_type", ""),
                "project": listing_details.get("project", {}).get("id", ""),
                "sale_date_range": f"{listing_details.get('sale_date_range', {}).get('gte', '')}~{listing_details.get('sale_date_range', {}).get('lte', '')}",
                "selected_area": listing_details.get("selected_area", ""),
                "description": listing_details.get("description", {}).get("dutch", ""),
                "description_tags": listing_details.get("description", {}).get(
                    "tags", ""
                ),
                "zoning": listing_details.get("zoning", ""),
                "surrounding": ",".join(listing_details.get("surrounding", [])),
                "exterior_space_garden_size": listing_details.get(
                    "exterior_space_garden_size", ""
                ),
                "exterior_space_type": listing_details.get("exterior_space_type", ""),
                "exterior_space_garden_orientation": listing_details.get(
                    "exterior_space_garden_orientation", ""
                ),
                "garage_capacity": listing_details.get("garage_capacity", ""),
                "garage_type": listing_details.get("garage_type", ""),
            }

            neightbourhood_insights = get_neighbourhood_insights(
                listing_parsed["address_city"], listing_parsed["address_neighbourhood"]
            )
            listing_parsed["neighbourhood_inhabitants"] = neightbourhood_insights.get(
                "inhabitants", None
            )
            listing_parsed["neighbourhood_avg_askingprice_m2"] = (
                neightbourhood_insights.get("averageAskingPricePerM2", None)
            )
            listing_parsed["neighbourhood_families_with_children_pct"] = (
                neightbourhood_insights.get("familiesWithChildren", None)
            )

            if use_listing_insights:
                try:
                    listing_insights = get_listing_insights(
                        listing_parsed["listing_id"]
                    )
                    listing_parsed["listing_nr_of_views"] = listing_insights[
                        "nrOfViews"
                    ]
                    listing_parsed["listing_nr_of_saves"] = listing_insights[
                        "nrOfSaves"
                    ]
                except Exception as e:
                    logging.debug(
                        f"Failed to get listing insights for {listing_parsed['listing_id']}: {e}"
                    )
                    pass

            parsed_results.append(listing_parsed)
        except Exception as e:
            print(f"Failed to parse listing. Error: {e} — {listing}")
            continue

    return parsed_results


def store_results(results, table, conn):
    cursor = conn.cursor()
    logging.debug(f"Storing {len(results)} results...")
    for result in results:
        try:
            data = {
                "id": xxhash.xxh64(
                    "~~".join([str(x) for x in result.values()])
                ).hexdigest(),
                **result,
                "_processing_time": str(datetime.datetime.now()),
                "_run_id": run_id,
            }

            query = f"""
                INSERT INTO {table}({", ".join(data.keys())})
                VALUES({", ".join(["%s"] * len(data.keys()))})
                ON CONFLICT (id) DO NOTHING
            """

            cursor.execute(query, tuple(data.values()))

        except Exception as e:
            logging.error(
                f"Error storing results for {result['listing_id']} ({result}) \n\n {query}"
            )
            logging.error(e)


def tracker(
    postal_code, km_radius, publication_date, connection, sleep_between_requests_sec=5
):
    ES_MAX_RESULT_WINDOW = 10000
    results_processed = 0
    results_total = 1
    page_size = 100
    while (
        results_processed < results_total
        and results_processed + page_size <= ES_MAX_RESULT_WINDOW
    ):
        res = get_results(
            postal_code4=postal_code,
            km_radius=km_radius,
            publication_date=publication_date,
            start_index=results_processed,
            page_size=page_size,
        )

        try:
            results_total = res["responses"][0]["hits"]["total"]["value"]
            results_current_length = len(res["responses"][0]["hits"]["hits"])

        except Exception as e:
            raise Exception(
                f"Failed to get results from funda. Got: {res}\n\nError: {e}"
            )

        if results_total == 0:
            logging.info("No results returned.")
            return

        logging.info(
            f"Processing results {results_processed}-{results_processed + results_current_length}/{results_total}..."
        )

        parsed_results = [
            {**x, "search_query": f"{postal_code}~{km_radius}~{publication_date}"}
            for x in parse_funda_results(res)
            if x
        ]

        store_results(parsed_results, "funda", connection)

        results_processed += results_current_length

        time.sleep(sleep_between_requests_sec)

    if results_processed >= ES_MAX_RESULT_WINDOW and results_processed < results_total:
        logging.warning(
            f"Reached Elasticsearch max result window ({ES_MAX_RESULT_WINDOW}). "
            f"Processed {results_processed}/{results_total} results for postal code {postal_code}. "
            f"Consider using a smaller radius or more restrictive date filter."
        )

    return
