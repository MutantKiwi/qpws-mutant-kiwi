# QPWS Permits Explorer

Interactive explorer for Queensland Parks and Wildlife Service camping and vehicle-access permit data,
2011 → 2026. FastAPI + DuckDB over Parquet; MapLibre / ECharts / Tabulator front end; deploys to Fly.io.

```
qpws-app/
├── etl/etl.py              raw CSV exports → data/parquet/{camping,vehicle,areas,postcodes}.parquet + dq_report.md
├── app/main.py             API (see /api/docs when running)
├── app/static/             index.html, app.js, app.css, vendor/ (maplibre, echarts, tabulator)
├── data/raw/
│   ├── camping/            YYYY_Qn_qpws-permits-camping-*.csv   (quarterly exports, 2016 Q3 →)
│   ├── vehicle/            YYYY_Qn_qpws-permits-vehicle*-*.csv
│   ├── legacy/             qpws-permits-{camping,vehicle}-master-2011-2016.csv
│   ├── bookeasy_operators.csv            current camping areas with lat/lon (scraped from the booking site)
│   └── lookups/
│       ├── area_aliases.csv              manual park/area → coordinates or operator id  (EDIT THIS to fix geocoding)
│       ├── postcodes.csv                 postcode → centroid, locality label, state, SA4
│       └── qld_school_terms.csv          Queensland state-school term dates 2010–2029 (add a year here when published)
├── etl/build_calendar.py   day-level calendar: QLD public holidays (holidays pkg) + school holidays + long weekends
├── data/parquet/           ETL output — this is what the app serves (baked into the Docker image)
├── Dockerfile, fly.toml, requirements.txt
```

## Run locally

```powershell
pip install -r requirements.txt
python etl\etl.py                      # ~40 s; writes data\parquet and data\parquet\dq_report.md
uvicorn app.main:app --reload --port 8080
# open http://localhost:8080   (API docs at /api/docs)
```

## Deploy to Fly.io

```powershell
fly launch --no-deploy --copy-config    # first time only; accept the app name in fly.toml or change it
fly deploy
```

The Parquet files (~60 MB) are copied into the image, so there is no volume or database to manage:
**refresh = run the ETL, then `fly deploy`.** With `auto_stop_machines` the app costs almost nothing idle.

## Adding a new quarter

1. Drop the new export(s) in `K:\Working\QPWS\qld_permits_csvs` and run `ingest_new_permits.ps1`
   (prefixes `YYYY_Qn_` and files them into `camping\` / `vehicle\`).
2. Copy/sync `camping\` and `vehicle\` into `data/raw/`.
3. `python etl\etl.py` → check `data\parquet\dq_report.md` (row counts per file, unparsed products, unmatched areas).
4. `fly deploy`.

If the new export has yet another column layout, add the column name to the relevant `coalesce(...)` in
`etl.py` (`camp_raw` / `veh_raw`) — that's the only place schema differences live.

## Data model

Two fact tables, deliberately kept separate (they describe different things):

**camping** — one row per camping booking
`booking_id, era, source_file, booking_date, arrival_date, departure_date, nights, adults, children, infants, people,
person_nights, park, area, postcode, state, year, quarter, month, lead_days, lat, lon`

**vehicle** — one row per vehicle access permit
`permit_id, era, source_file, booking_date, start_date, product_raw, area, duration, permit_class, days_valid,
postcode, state, year, quarter, month, lead_days`

Both fact tables also carry the calendar attributes of their arrival/start date:
`day_type, is_school_holiday, is_public_holiday, is_long_weekend, is_weekend, school_holiday, public_holiday, school_term`
and camping adds `sh_nights` / `we_nights` (how many of the booking's nights fall in school holidays / on weekends+public holidays).

Dimensions: **areas** (park, area, lat, lon, match method, bookings, first/last seen, **capacity**), **postcodes**, and **calendar**
(one row per day 2010–2029).

**area_day** — one row per camping area per night with guests: `bookings_in_house, people_in_house, sites_cap, people_cap,
site_occ_pct, people_occ_pct, occ_pct` plus the calendar flags. This is what the occupancy views read.

### Capacity and occupancy
Capacity comes from the booking site's site-type records (`data/raw/bookeasy_rooms.csv`, the
`getAccomRoomsDetails` endpoint flattened; "(edu)" duplicates dropped, rooms de-duplicated on RoomID). Two kinds:
- **sites** areas (numbered / un-numbered): `sites_cap` = Σ NoRooms, `people_cap` = Σ NoRooms × NoPersons. Occupancy = bookings in house ÷ sites.
- **people** areas (open camping zones, and areas configured as "N sites of 1 person" or a few sites with ≥ 20-person caps): `people_cap` only. Occupancy = people in house ÷ people cap.
`occ_pct` picks the right basis automatically; the API also exposes `basis=sites|people`.

Caveats: capacity is **today's** configuration applied to all years, so areas that were re-sited or re-zoned show
>100% in earlier years (notably the small K'gari western-beach areas); areas not in the booking site today (~20% of
bookings) have no occupancy. Treat occupancy as a strong relative signal, not an audit figure.

### Calendar / day types
- **Public holidays:** `holidays` package, AU‑QLD — includes the Royal Queensland Show (Brisbane only, treated statewide), the 2013–15 Labour Day move, Queen's/King's Birthday.
- **School holidays:** the gaps between Queensland *state* school terms (`lookups/qld_school_terms.csv`, sourced from education.qld.gov.au; 2010–13 via archived copies of the department's page). Labelled Easter / Winter / Spring / Summer by the term just finished. Independent/Catholic schools differ by a few days; NSW/VIC holidays are not included (yet).
- **Long weekend:** any run of ≥ 3 consecutive non‑working days (weekend + public holiday).
- **day_type** gives each day one label by priority: School holiday › Public holiday › Long weekend › Weekend › Weekday. So Easter Friday is "School holiday" (it is inside the Easter break); the October long weekend is "Long weekend" only in years when Term 4 has already started.
- The **Holiday effect** charts divide the metric by the number of calendar days of each type in the selected period, so a 17‑day school break is compared fairly with 250 weekdays. The multiplier is relative to Weekday / Term time.

### How the eras were reconciled

| Era | Source | Shape | Handling |
|---|---|---|---|
| `legacy` | 2011 – Jun 2016 masters | one row per booking, `DAYS` = nights | used for arrivals < 2016‑07‑01 |
| `stay_rows` | 2016 Q3 – Jan 2025 quarterlies | **one row per night stayed** | deduped on reservation id + stay date across files (bookings straddling quarters appear in both), then rolled up: arrival = min, nights = distinct stay dates |
| `span_rows` | Feb 2025 → | one row per booking with arrival/departure | deduped on all columns |

Other things the ETL corrects (details in `dq_report.md`):
- six different header layouts, cp1252 / UTF‑8 / UTF‑8‑BOM encodings, dates as `d/m/Y`, `d/m/y`, `d-Mon-y`, `d/m/Y 0:00`
- the Apr–Jun 2016 export stores adults/children as **person‑nights**; divided back through by nights
- the Feb–Mar 2025 export labels rows `Accommodation` not `Camping`, and duplicates lines
- `CAMPING_ADDON` (extra people/nights added to a permit) and `CREDIT` rows are excluded
- state normalised to codes; derived from postcode where missing; `OS` = overseas
- vehicle product strings parsed into recreation area / duration / class across all four historical formats

### Known gaps and caveats
- **Feb–Mar 2025 camping is ~50% of a normal month** — the export is thin around the booking-system changeover. Treat early‑2025 dips as a data artefact.
- Vehicle permits carry no coordinates; the map shows recreation areas at approximate centroids.
- ~2% of camping bookings (82 historical/renamed areas) have no coordinates; 279 more use their park's centroid. Add rows to `lookups/area_aliases.csv` to fix — the DQ report lists the biggest.
- Moreton Island (Mulgumpin) and Minjerribah areas are not in the booking site, so their coordinates in `area_aliases.csv` are hand‑placed approximations.
- Quarterly files are selected by *stay* date, so a booking's arrival can precede the labelled quarter.
- Current quarter is partial.

## API

All endpoints accept the same filter query string:
`dataset=camping|vehicle, from, to, state (repeatable), postcode (rep.), park (rep.), area (rep.), duration (rep.), permit_class (rep.), day_type (rep.), holiday (rep.), q`

| Endpoint | Purpose |
|---|---|
| `/api/meta` | date ranges, parks, areas, measures & dimensions per dataset |
| `/api/summary` | KPI tile values |
| `/api/timeseries?grain=&metric=&by=&top=` | time series, optional top‑N split |
| `/api/seasonality`, `/api/yoy` | month × year matrices |
| `/api/daytype?by=day_type|school_holiday&metric=` | per‑calendar‑day rates by day type / holiday period |
| `/api/occupancy/summary|timeseries|areas|daytype?basis=auto|sites|people` | occupancy vs capacity (camping; date/park/area/day‑type filters only) |
| `/api/calendar?from=&to=` | school‑holiday periods and public holidays (chart overlays) |
| `/api/breakdown?by=&metric=&top=` | ranked bars for any dimension |
| `/api/map/areas`, `/api/map/origins`, `/api/map/flows` | map layers |
| `/api/table?sort=&desc=&limit=&offset=` | paged records |
| `/api/export.csv` | filtered CSV (≤ 500k rows by default) |
| `/api/search?term=` | typeahead over parks, areas, postcodes, localities |

Interactive docs at `/api/docs`.
