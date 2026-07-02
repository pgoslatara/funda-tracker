"""BAG bouwjaar (construction year) enrichment for funda listings.

Funda removed ``construction_period`` from its search API around Sept-Oct 2025
(it was ~98% populated through Aug 2025, 0% since Oct 2025), so the build era can
no longer be obtained from funda itself. This module enriches a listing's build
year from the authoritative Dutch BAG building registry via PDOK's open APIs.

Pipeline per address:

1. Geocode the full address via the PDOK Locatieserver
   (``fq=type:adres``) to get a precise ``centroide_ll`` point.
2. Look up the BAG ``pand`` (building) that contains that point via the PDOK BAG
   WFS and read its ``bouwjaar``.

Gotchas encoded here (do NOT "simplify" them away):

- The WFS ``cql_filter`` parameter is SILENTLY IGNORED, so we cannot filter by
  attribute. Instead we query with the native ``bbox`` parameter (a tiny box
  around the centroid) and pick the polygon that actually CONTAINS the point via
  a ray-casting point-in-polygon test.
- Under ``srsName=urn:ogc:def:crs:EPSG::4326`` the WFS returns coordinates in
  lat,lon order, so we swap to lon,lat before the ray-casting test.
- BAG uses sentinel bouwjaar values (e.g. 1005) for undated monuments; anything
  below 1500 is treated as unknown/None.

Results are cached on disk (keyed by normalised address) so re-runs and the
backfill are cheap and polite to PDOK.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path

import requests

LS_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"

# Politeness: small sleep between *new* (uncached) PDOK lookups.
_SLEEP_BETWEEN_LOOKUPS_SEC = 0.05

# BAG sentinels (e.g. 1005 for undated monuments) are treated as unknown.
_MIN_VALID_YEAR = 1500
_MAX_VALID_YEAR = 2035


def _default_cache_path() -> Path:
    """On-disk cache location. Override with ``$FUNDA_BAG_CACHE``."""
    override = os.environ.get("FUNDA_BAG_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "fundatracker" / "bag_cache.json"


class BagEnricher:
    """Reusable BAG bouwjaar lookup with an internal session + on-disk cache.

    Instantiate once and reuse across many addresses; the HTTP session is kept
    alive and results are memoised both in-memory and on disk.
    """

    _MIN_VALID_YEAR = _MIN_VALID_YEAR
    _MAX_VALID_YEAR = _MAX_VALID_YEAR

    def __init__(self, cache_path: Path | None = None):
        self.cache_path = cache_path or _default_cache_path()
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._cache: dict[str, int | None] = self._load_cache()

    # -- cache ------------------------------------------------------------
    def _load_cache(self) -> dict[str, int | None]:
        try:
            if self.cache_path.exists():
                return json.loads(self.cache_path.read_text())
        except Exception as e:  # noqa: BLE001 - corrupt cache must not break scrape
            logging.warning(f"Could not read BAG cache {self.cache_path}: {e}")
        return {}

    def save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache))
        except Exception as e:  # noqa: BLE001
            logging.warning(f"Could not write BAG cache {self.cache_path}: {e}")

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _addr_key(street, housenumber, suffix, postal_code, city) -> str:
        num = str(housenumber or "").strip()
        suf = str(suffix or "").strip()
        pc = re.sub(r"\s+", "", str(postal_code or "")).upper()
        st = str(street or "").strip()
        cty = str(city or "").strip()
        return f"{st}|{num}|{suf}|{pc}|{cty}"

    def _ls_lookup(self, street, housenumber, suffix, postal_code, city):
        """Geocode a full address -> (lat, lon) precise centroid, or None."""
        num = str(housenumber or "").strip()
        suf = str(suffix or "").strip()
        pc = re.sub(r"\s+", "", str(postal_code or "")).upper()
        st = str(street or "").strip()
        cty = str(city or "").strip()
        pc_fmt = f"{pc[:4]} {pc[4:]}" if len(pc) == 6 else pc
        # Try with the suffix first (more precise), then without.
        queries = [
            f"{st} {num}{suf}, {pc_fmt} {cty}".strip(),
            f"{st} {num}, {pc_fmt} {cty}".strip(),
        ]
        for q in queries:
            docs = (
                self._session.get(
                    LS_URL,
                    params={
                        "q": q,
                        "fq": "type:adres",
                        "rows": 1,
                        "fl": "centroide_ll,adresseerbaarobject_id",
                    },
                    timeout=15,
                )
                .json()
                .get("response", {})
                .get("docs", [])
            )
            if docs:
                m = re.search(
                    r"POINT\(([-\d.]+) ([-\d.]+)\)", docs[0].get("centroide_ll", "")
                )
                if m:
                    return float(m.group(2)), float(m.group(1))  # lat, lon
        return None

    @staticmethod
    def _point_in_ring(lon: float, lat: float, ring: list) -> bool:
        """Ray-casting point-in-polygon. ``ring`` = [[lon, lat], ...]."""
        inside = False
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if ((yi > lat) != (yj > lat)) and (
                lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
            ):
                inside = not inside
            j = i
        return inside

    @classmethod
    def _clean_year(cls, yr) -> int | None:
        try:
            yr = int(yr) if yr else 0
        except (TypeError, ValueError):
            return None
        return yr if cls._MIN_VALID_YEAR <= yr <= cls._MAX_VALID_YEAR else None

    def _bag_bouwjaar(self, lat: float, lon: float) -> int | None:
        """Construction year of the BAG 'pand' that contains this point.

        PDOK's WFS ignores ``cql_filter``, so we query with the native ``bbox``
        parameter (which works) and select the polygon that actually contains the
        point. Under the urn CRS the WFS returns coordinates in lat,lon order, so
        we swap to lon,lat before the point-in-polygon test.
        """
        dlat = 0.00006
        dlon = 0.00006 / math.cos(math.radians(lat))
        bbox = (
            f"{lat - dlat},{lon - dlon},{lat + dlat},{lon + dlon},"
            "urn:ogc:def:crs:EPSG::4326"
        )
        feats = (
            self._session.get(
                BAG_WFS,
                params={
                    "service": "WFS",
                    "version": "2.0.0",
                    "request": "GetFeature",
                    "typeNames": "bag:pand",
                    "outputFormat": "application/json",
                    "count": 25,
                    "srsName": "urn:ogc:def:crs:EPSG::4326",
                    "bbox": bbox,
                },
                timeout=20,
            )
            .json()
            .get("features", [])
        )

        fallback: int | None = None
        for f in feats:
            g = f.get("geometry") or {}
            yr = self._clean_year(f.get("properties", {}).get("bouwjaar"))
            polys = (
                g.get("coordinates", [])
                if g.get("type") == "MultiPolygon"
                else [g.get("coordinates", [])]
            )
            for poly in polys:
                if not poly:
                    continue
                ring = [[c[1], c[0]] for c in poly[0]]  # swap lat,lon -> lon,lat
                if self._point_in_ring(lon, lat, ring):
                    return yr
            if fallback is None:
                fallback = yr
        # Point not inside any polygon: best-effort nearest pand in the bbox.
        return fallback

    # -- public API -------------------------------------------------------
    def lookup_bouwjaar(
        self, street, housenumber, suffix, postal_code, city
    ) -> int | None:
        """Return the BAG construction year for an address, or ``None``.

        Cached by normalised address (in-memory + on disk). A failure in any
        remote call returns ``None`` and is cached as such (so we don't hammer
        PDOK for known-bad addresses within a run); it never raises.
        """
        key = self._addr_key(street, housenumber, suffix, postal_code, city)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        year: int | None = None
        try:
            ls = self._ls_lookup(street, housenumber, suffix, postal_code, city)
            if ls:
                lat, lon = ls
                year = self._bag_bouwjaar(lat, lon)
            time.sleep(_SLEEP_BETWEEN_LOOKUPS_SEC)  # be polite on new lookups
        except Exception as e:  # noqa: BLE001 - BAG must never break the scrape
            logging.warning(
                f"BAG lookup failed for {street} {housenumber}{suffix or ''} "
                f"{postal_code} {city}: {e}"
            )
            year = None

        with self._lock:
            self._cache[key] = year
        return year


# Module-level convenience: a shared enricher for simple one-off calls.
_default_enricher: BagEnricher | None = None
_default_enricher_lock = threading.Lock()


def get_default_enricher() -> BagEnricher:
    global _default_enricher
    with _default_enricher_lock:
        if _default_enricher is None:
            _default_enricher = BagEnricher()
        return _default_enricher


def lookup_bouwjaar(street, housenumber, suffix, postal_code, city) -> int | None:
    """Look up a BAG bouwjaar using a process-wide shared enricher/cache.

    Convenience wrapper around :class:`BagEnricher` for callers that don't want
    to manage an instance. Prefer instantiating a :class:`BagEnricher` and
    calling ``.save_cache()`` yourself for batch work (e.g. the backfill).
    """
    return get_default_enricher().lookup_bouwjaar(
        street, housenumber, suffix, postal_code, city
    )
