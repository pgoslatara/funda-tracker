import argparse
import logging
import os

from . import bag, utils
from .funda import get_funda_schema, tracker

CONNECTION = None


def _connect():
    global CONNECTION
    CONNECTION = utils.get_database_connection(
        db_name="funda",
        db_user=os.environ.get("USER"),
        db_password=os.environ.get("PASSWORD"),
        db_host=os.environ.get("HOST"),
    )
    utils.db_setup("funda", get_funda_schema(), CONNECTION)
    return CONNECTION


def _run_scrape(args):
    conn = _connect()
    print(f"🏃 Running with args: {vars(args)}")
    tracker(
        args.postal_code,
        args.km_radius,
        args.publication_date,
        connection=conn,
        enrich_bag=not args.no_bag,
    )
    print("🏁 Finished")


def _run_backfill(args):
    """Fill bag_bouwjaar for existing rows where it's NULL.

    Deduplicates by address (so each unique address is looked up once), caches to
    disk, and is resumable: only rows still NULL are processed, and every unique
    address updates ALL matching NULL rows in one statement.
    """
    conn = _connect()
    enricher = bag.BagEnricher()

    # Distinct addresses that still need a bouwjaar. DISTINCT dedups the work.
    select_sql = """
        SELECT DISTINCT
            address_street_name,
            address_house_number,
            address_house_number_suffix,
            address_postal_code,
            address_city
        FROM funda
        WHERE bag_bouwjaar IS NULL
          AND address_postal_code IS NOT NULL
          AND address_postal_code <> ''
    """
    if args.limit:
        select_sql += f"\n        LIMIT {int(args.limit)}"

    with conn.cursor() as cur:
        cur.execute(select_sql)
        addresses = cur.fetchall()

    total = len(addresses)
    print(f"🔎 Backfilling bag_bouwjaar for {total} distinct NULL addresses...")

    updated_rows = 0
    filled = 0
    for i, (street, num, suffix, postal_code, city) in enumerate(addresses, 1):
        year = enricher.lookup_bouwjaar(street, num, suffix, postal_code, city)
        if year is not None:
            filled += 1

        # Update every NULL row with this exact address. We only touch NULL rows
        # so re-running is safe/idempotent. NULL-safe equality via IS NOT DISTINCT
        # FROM handles nullable suffix/street/city.
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE funda
                SET bag_bouwjaar = %s
                WHERE bag_bouwjaar IS NULL
                  AND address_street_name IS NOT DISTINCT FROM %s
                  AND address_house_number IS NOT DISTINCT FROM %s
                  AND address_house_number_suffix IS NOT DISTINCT FROM %s
                  AND address_postal_code IS NOT DISTINCT FROM %s
                  AND address_city IS NOT DISTINCT FROM %s
                """,
                (year, street, num, suffix, postal_code, city),
            )
            updated_rows += cur.rowcount

        if i % 50 == 0:
            enricher.save_cache()
            print(f"   ...{i}/{total} addresses ({filled} with a year so far)")

    enricher.save_cache()
    print(
        f"🏁 Backfill finished: {filled}/{total} addresses resolved, "
        f"{updated_rows} rows updated."
    )


def cli():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    parser = argparse.ArgumentParser(prog="fundatracker")
    subparsers = parser.add_subparsers(dest="command")

    # Default scrape command (kept as the top-level flags too, for back-compat).
    scrape = subparsers.add_parser("scrape", help="Scrape funda listings (default)")
    scrape.add_argument("--postal_code", type=int, required=True)
    scrape.add_argument("--km_radius", type=int, required=True)
    scrape.add_argument("--publication_date", type=str, default="now-30d")
    scrape.add_argument(
        "--no-bag",
        action="store_true",
        help="Skip BAG bouwjaar enrichment during the scrape.",
    )
    scrape.set_defaults(func=_run_scrape)

    # Backfill command.
    backfill = subparsers.add_parser(
        "backfill",
        help="Fill bag_bouwjaar for existing rows where it is NULL (resumable).",
    )
    backfill.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process this many distinct addresses (for a dry run).",
    )
    backfill.set_defaults(func=_run_backfill)

    # Back-compat: allow the old flag-only invocation without a subcommand.
    parser.add_argument("--postal_code", type=int)
    parser.add_argument("--km_radius", type=int)
    parser.add_argument("--publication_date", type=str, default="now-30d")
    parser.add_argument("--no-bag", action="store_true")

    args = parser.parse_args()

    print("🔌 Connecting to database...")
    if getattr(args, "func", None):
        args.func(args)
    elif args.postal_code is not None and args.km_radius is not None:
        _run_scrape(args)
    else:
        parser.error(
            "provide a subcommand (scrape/backfill) or --postal_code and --km_radius"
        )


if __name__ == "__main__":
    cli()
