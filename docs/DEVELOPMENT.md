 QPWS Permits Explorer$# QPWS Permits Explorer
 QPWS Permits Explorer$
 QPWS Permits Explorer$Interactive explorer for Queensland Parks and Wildlife Service camping and vehicle-access permit data,
 QPWS Permits Explorer$2011 → 2026. FastAPI + DuckDB over Parquet; MapLibre / ECharts / Tabulator front end; deploys to Fly.io.
 QPWS Permits Explorer$
 QPWS Permits Explorer$```
 QPWS Permits Explorer$qpws-app/
 QPWS Permits Explorer$├── etl/etl.py              raw CSV exports → data/parquet/{camping,vehicle,areas,postcodes}.parquet + dq_report.md
 QPWS Permits Explorer$├── app/main.py             API (see /api/docs when running)
 QPWS Permits Explorer$├── app/static/             index.html, app.js, app.css, vendor/ (maplibre, echarts, tabulator)
 QPWS Permits Explorer$├── data/raw/
 QPWS Permits Explorer$│   ├── camping/            YYYY_Qn_qpws-permits-camping-*.csv   (quarterly exports, 2016 Q3 →)
 QPWS Permits Explorer$│   ├── vehicle/            YYYY_Qn_qpws-permits-vehicle*-*.csv
 QPWS Permits Explorer$│   ├── legacy/             qpws-permits-{camping,vehicle}-master-2011-2016.csv
 QPWS Permits Explorer$│   ├── bookeasy_operators.csv            current camping areas with lat/lon (scraped from the booking site)
 QPWS Permits Explorer$│   └── lookups/
 QPWS Permits Explorer$│       ├── area_aliases.csv              manual park/area → coordinates or operator id  (EDIT THIS to fix geocoding)
 QPWS Permits Explorer$│       ├── postcodes.csv                 postcode → centroid, locality label, state, SA4
 QPWS Permits Explorer$│       └── qld_school_terms.csv          Queensland state-school term dates 2010–2029 (add a year here when published)
 QPWS Permits Explorer$├── etl/build_calendar.py   day-level calendar: QLD public holidays (holidays pkg) + school holidays + long weekends
 QPWS Permits Explorer$├── data/parquet/           ETL output — this is what the app serves (baked into the Docker image)
 QPWS Permits Explorer$├── Dockerfile, fly.toml, requirements.txt
 QPWS Permits Explorer$```
 QPWS Permits Explorer$
 QPWS Permits Explorer$## Run locally
 QPWS Permits Explorer$
 QPWS Permits Explorer$```powershell
 QPWS Permits Explorer$pip install -r requirements.txt
 QPWS Permits Explorer$python etl\etl.py                      # ~40 s; writes data\parquet and data\parquet\dq_report.md
 QPWS Permits Explorer$uvicorn app.main:app --reload --port 8080
 QPWS Permits Explorer$# open http://localhost:8080   (API docs at /api/docs)
 QPWS Permits Explorer$```
 QPWS Permits Explorer$
 QPWS Permits Explorer$## Deploy to Fly.io
 QPWS Permits Explorer$
 QPWS Permits Explorer$```powershell
 QPWS Permits Explorer$fly launch --no-deploy --copy-config    # first time only; app name qpws-mutant-kiwi is in fly.toml
 QPWS Permits Explorer$fly deploy
 QPWS Permits Explorer$```
 QPWS Permits Explorer$
 QPWS Permits Explorer$The Parquet files (~60 MB) are copied into the image, so there is no volume or database to manage:
 QPWS Permits Explorer$**refresh = run the ETL, then `fly deploy`.** With `auto_stop_machines` the app costs almost nothing idle.
 QPWS Permits Explorer$
 QPWS Permits Explorer$## Adding a new quarter
 QPWS Permits Explorer$
 QPWS Permits Explorer$1. Drop the new export(s) in `K:\Working\QPWS\qld_permits_csvs` and run `ingest_new_permits.ps1`
 QPWS Permits Explorer$   (prefixes `YYYY_Qn_` and files them into `camping\` / `vehicle\`).
 QPWS Permits Explorer$2. Copy/sync `camping\` and `vehicle\` into `data/raw/`.
 QPWS Permits Explorer$3. `python etl\etl.py` → check `data\parquet\dq_report.md` (row counts per file, unparsed products, unmatched areas).
 QPWS Permits Explorer$4. `fly deploy`.
 QPWS Permits Explorer$
 QPWS Permits Explorer$If the new export has yet another column layout, add the column name to the relevant `coalesce(...)` in
 QPWS Permits Explorer$`etl.py` (`camp_raw` / `veh_raw`) — that's the only place schema differences live.
 QPWS Permits Explorer$
 QPWS Permits Explorer$## Data model
 QPWS Permits Explorer$
 QPWS Permits Explorer$Two fact tables, deliberately kept separate (they describe different things):
 QPWS Permits Explorer$
 QPWS Permits Explorer$**camping** — one row per camping booking
 QPWS Permits Explorer$`booking_id, era, source_file, booking_date, arrival_date, departure_date, nights, adults, children, infants, people,
 QPWS Permits Explorer$person_nights, park, area, postcode, state, year, quarter, month, lead_days, lat, lon`
 QPWS Permits Explorer$
 QPWS Permits Explorer$**vehicle** — one row per vehicle access permit
 QPWS Permits Explorer$`permit_id, era, source_file, booking_date, start_date, product_raw, area, duration, permit_class, days_valid,
 QPWS Permits Explorer$postcode, state, year, quarter, month, lead_days`
 QPWS Permits Explorer$
 QPWS Permits Explorer$Both fact tables also carry the calendar attributes of their arrival/start date:
 QPWS Permits Explorer$`day_type, is_school_holiday, is_public_holiday, is_long_weekend, is_weekend, school_holiday, public_holiday, school_term`
 QPWS Permits Explorer$and camping adds `sh_nights` / `we_nights` (how many of the booking's nights fall in school holidays / on weekends+public holidays).
 QPWS Permits Explorer$
 QPWS Permits Explorer$Dimensions: **areas** (park, area, lat, lon, match method, bookings, first/last seen, **capacity**), **postcodes**, and **calendar**
 QPWS Permits Explorer$(one row per day 2010–2029).
 QPWS Permits Explorer$
 QPWS Permits Explorer$**area_day** — one row per camping area per night with guests: `bookings_in_house, people_in_house, sites_cap, people_cap,
 QPWS Permits Explorer$site_occ_pct, people_occ_pct, occ_pct` plus the calendar flags. This is what the occupancy views read.
 QPWS Permits Explorer$
 QPWS Permits Explorer$### Capacity and occupancy
 QPWS Permits Explorer$Capacity comes from the booking site's site-type records (`data/raw/bookeasy_rooms.csv`, the
 QPWS Permits Explorer$`getAccomRoomsDetails` endpoint flattened; "(edu)" duplicates dropped, rooms de-duplicated on RoomID). Two kinds:
 QPWS Permits Explorer$- **sites** areas (numbered / un-numbered): `sites_cap` = Σ NoRooms, `people_cap` = Σ NoRooms × NoPersons. Occupancy = bookings in house ÷ sites.
 QPWS Permits Explorer$- **people** areas (open camping zones, and areas configured as "N sites of 1 person" or a few sites with ≥ 20-person caps): `people_cap` only. Occupancy = people in house ÷ people cap.
 QPWS Permits Explorer$`occ_pct` picks the right basis automatically; the API also exposes `basis=sites|people`.
 QPWS Permits Explorer$
 QPWS Permits Explorer$Caveats: capacity is **today's** configuration applied to all years, so areas that were re-sited or re-zoned show
 QPWS Permits Explorer$>100% in earlier years (notably the small K'gari western-beach areas); areas not in the booking site today (~20% of
 QPWS Permits Explorer$bookings) have no occupancy. Treat occupancy as a strong relative signal, not an audit figure.
 QPWS Permits Explorer$
 QPWS Permits Explorer$### Calendar / day types
 QPWS Permits Explorer$- **Public holidays:** `holidays` package, AU‑QLD — includes the Royal Queensland Show (Brisbane only, treated statewide), the 2013–15 Labour Day move, Queen's/King's Birthday.
 QPWS Permits Explorer$- **School holidays:** the gaps between Queensland *state* school terms (`lookups/qld_school_terms.csv`, sourced from education.qld.gov.au; 2010–13 via archived copies of the department's page). Labelled Easter / Winter / Spring / Summer by the term just finished. Independent/Catholic schools differ by a few days; NSW/VIC holidays are not included (yet).
 QPWS Permits Explorer$- **Long weekend:** any run of ≥ 3 consecutive non‑working days (weekend + public holiday).
 QPWS Permits Explorer$- **day_type** gives each day one label by priority: School holiday › Public holiday › Long weekend › Weekend › Weekday. So Easter Friday is "School holiday" (it is inside the Easter break); the October long weekend is "Long weekend" only in years when Term 4 has already started.
 QPWS Permits Explorer$- The **Holiday effect** charts divide the metric by the number of calendar days of each type in the selected period, so a 17‑day school break is compared fairly with 250 weekdays. The multiplier is relative to Weekday / Term time.
 QPWS Permits Explorer$
 QPWS Permits Explorer$### How the eras were reconciled
 QPWS Permits Explorer$
 QPWS Permits Explorer$| Era | Source | Shape | Handling |
 QPWS Permits Explorer$|---|---|---|---|
 QPWS Permits Explorer$| `legacy` | 2011 – Jun 2016 masters | one row per booking, `DAYS` = nights | used for arrivals < 2016‑07‑01 |
 QPWS Permits Explorer$| `stay_rows` | 2016 Q3 – Jan 2025 quarterlies | **one row per night stayed** | deduped on reservation id + stay date across files (bookings straddling quarters appear in both), then rolled up: arrival = min, nights = distinct stay dates |
 QPWS Permits Explorer$| `span_rows` | Feb 2025 → | one row per booking with arrival/departure | deduped on all columns |
 QPWS Permits Explorer$
 QPWS Permits Explorer$Other things the ETL corrects (details in `dq_report.md`):
 QPWS Permits Explorer$- six different header layouts, cp1252 / UTF‑8 / UTF‑8‑BOM encodings, dates as `d/m/Y`, `d/m/y`, `d-Mon-y`, `d/m/Y 0:00`
 QPWS Permits Explorer$- the Apr–Jun 2016 export stores adults/children as **person‑nights**; divided back through by nights
 QPWS Permits Explorer$- the Feb–Mar 2025 export labels rows `Accommodation` not `Camping`, and duplicates lines
 QPWS Permits Explorer$- `CAMPING_ADDON` (extra people/nights added to a permit) and `CREDIT` rows are excluded
 QPWS Permits Explorer$- state normalised to codes; derived from postcode where missing; `OS` = overseas
 QPWS Permits Explorer$- vehicle product strings parsed into recreation area / duration / class across all four historical formats
 QPWS Permits Explorer$
 QPWS Permits Explorer$### Known gaps and caveats
 QPWS Permits Explorer$- **Feb–Mar 2025 camping is ~50% of a normal month** — the export is thin around the booking-system changeover. Treat early‑2025 dips as a data artefact.
 QPWS Permits Explorer$- Vehicle permits carry no coordinates; the map shows recreation areas at approximate centroids.
 QPWS Permits Explorer$- ~2% of camping bookings (82 historical/renamed areas) have no coordinates; 279 more use their park's centroid. Add rows to `lookups/area_aliases.csv` to fix — the DQ report lists the biggest.
 QPWS Permits Explorer$- Moreton Island (Mulgumpin) and Minjerribah areas are not in the booking site, so their coordinates in `area_aliases.csv` are hand‑placed approximations.
 QPWS Permits Explorer$- Quarterly files are selected by *stay* date, so a booking's arrival can precede the labelled quarter.
 QPWS Permits Explorer$- Current quarter is partial.
 QPWS Permits Explorer$
 QPWS Permits Explorer$## API
 QPWS Permits Explorer$
 QPWS Permits Explorer$All endpoints accept the same filter query string:
 QPWS Permits Explorer$`dataset=camping|vehicle, from, to, state (repeatable), postcode (rep.), park (rep.), area (rep.), duration (rep.), permit_class (rep.), day_type (rep.), holiday (rep.), q`
 QPWS Permits Explorer$
 QPWS Permits Explorer$| Endpoint | Purpose |
 QPWS Permits Explorer$|---|---|
 QPWS Permits Explorer$| `/api/meta` | date ranges, parks, areas, measures & dimensions per dataset |
 QPWS Permits Explorer$| `/api/summary` | KPI tile values |
 QPWS Permits Explorer$| `/api/timeseries?grain=&metric=&by=&top=` | time series, optional top‑N split |
 QPWS Permits Explorer$| `/api/seasonality`, `/api/yoy` | month × year matrices |
 QPWS Permits Explorer$| `/api/daytype?by=day_type|school_holiday&metric=` | per‑calendar‑day rates by day type / holiday period |
 QPWS Permits Explorer$| `/api/occupancy/summary|timeseries|areas|daytype?basis=auto|sites|people` | occupancy vs capacity (camping; date/park/area/day‑type filters only) |
 QPWS Permits Explorer$| `/api/calendar?from=&to=` | school‑holiday periods and public holidays (chart overlays) |
 QPWS Permits Explorer$| `/api/breakdown?by=&metric=&top=` | ranked bars for any dimension |
 QPWS Permits Explorer$| `/api/map/areas`, `/api/map/origins`, `/api/map/flows` | map layers |
 QPWS Permits Explorer$| `/api/table?sort=&desc=&limit=&offset=` | paged records |
 QPWS Permits Explorer$| `/api/export.csv` | filtered CSV (≤ 500k rows by default) |
 QPWS Permits Explorer$| `/api/search?term=` | typeahead over parks, areas, postcodes, localities |
 QPWS Permits Explorer$
 QPWS Permits Explorer$Interactive docs at `/api/docs`.
