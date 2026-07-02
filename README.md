Fetch all data from Funda for a specific 4-digit postal code in a x-KM radius and store it in a Postgres database.

1. Set up your Postgres database, e.g. with [DBngin](https://dbngin.com/) locally or anywhere else
2. Set environment variables for you Postgres `HOST`, `USER` and `PASSWORD`
3. `git clone https://github.com/dumkydewilde/funda-tracker.git`
4. `pip install -r requirements.txt`
5. `python fundatracker --postal_code 1011 --km_radius 5`

Among other things this will return:
- Object type (apartment, house, parking, land, etc.)
- Energy label
- Floor area in m2
- Plot area in m2
- Status (sold, sold_under_reservation, none)
- Amenities (boiler, bathtub, renewable_energy, etc.)
- Construction period (as far as funda still exposes it; see below)
- Construction year (`bag_bouwjaar`, enriched from the Dutch BAG registry)
- Offering type (buy, rent)
- Neighbourhood stats (Inhabitants, avg. asking price)
- Listing insights (saves, views)

### BAG construction year (`bag_bouwjaar`)

Funda removed `construction_period` from its search API around Sept-Oct 2025 (it
was ~98% populated through Aug 2025 and 0% since Oct 2025). Build era is therefore
enriched from the authoritative Dutch BAG building registry via PDOK's open APIs
(Locatieserver geocode + BAG WFS `pand` lookup) and stored in the `bag_bouwjaar`
column. Enrichment is on by default during a scrape (disable with `--no-bag`) and
never aborts a scrape if PDOK is unavailable (it stores `NULL` and logs).

Results are cached on disk (default `~/.cache/fundatracker/bag_cache.json`,
override with `$FUNDA_BAG_CACHE`).

## Command line

```bash
# Scrape (BAG enrichment on by default)
python -m fundatracker.cli scrape --postal_code 1011 --km_radius 5

# Backfill bag_bouwjaar for existing rows where it is NULL (resumable, cached).
# Use --limit for a small dry run first.
python -m fundatracker.cli backfill --limit 100
```

The old flag-only form (`fundatracker --postal_code 1011 --km_radius 5`) still
works and defaults to the scrape command.

| arg | description |
| --- | ---- |
| `--postal_code` | any 4 digit postal code  |
| `--km_radius` | [1,2,5,10,15,30,50,100] |
| `--publication_date` | ["now-1d","now-3d", "now-5d", "now-10d", "now-30d", "no_preference"] |
| `--no-bag` | skip BAG bouwjaar enrichment during a scrape |


NB. This is just a tool for convenience, so treat it as if you were a regular browser of the site.

## Development

### Setup
```bash
# Clone the repository
git clone https://github.com/dumkydewilde/funda-tracker.git
cd funda-tracker

# Install dependencies with uv
uv sync --extra dev

# Install pre-commit hooks
uv run pre-commit install
```

### Development Commands
```bash
# Run tests
make test
# or
uv run pytest

# Run linting and formatting
make check
# or
uv run ruff check . && uv run ruff format .

# Test pre-commit hooks
make pre-commit-test
# or
uv run pre-commit run --all-files
```

### Pre-commit Hooks
This project uses pre-commit hooks that will run automatically before each commit:
- **ruff** - Fast Python linter and formatter
- **pytest** - Run all tests
- **General hooks** - Check YAML, remove trailing whitespace, etc.

The hooks ensure code quality and that all tests pass before code is committed.
