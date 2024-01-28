Fetch all data from Funda for a specific 4-digit postal code in a x-KM radius and store it in a DuckDB database.

1. `poetry install`
1. `python fundatracker --postal_code 1011 --km_radius 5`

Among other things this will return:
- Object type (apartment, house, parking, land, etc.)
- Energy label
- Floor area in m2
- Plot area in m2
- Status (sold, sold_under_reservation, none)
- Amenities (boiler, bathtub, renewable_energy, etc.)
- Construction period
- Offering type (buy, rent)
- Neighbourhood stats (Inhabitants, avg. asking price)
- Listing insights (saves, views)

## Command line options
| arg | description |
| --- | ---- |
| `--postal_code` | any 4 digit postal code  |
| `--km_radius` | [1,2,5,10,15,30,50,100] |
| `--publication_date` | ["now-1d","now-3d", "now-5d", "now-10d", "now-30d", "no_preference"] |


NB. This is just a tool for convenience, so treat it as if you were a regular browser of the site.
