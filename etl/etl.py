#!/usr/bin/env python3
"""
QPWS permits ETL: raw CSV exports (2011-2026, many schema eras) -> two canonical Parquet tables.

    python etl/etl.py --raw data/raw --out data/parquet

Inputs (under --raw):
  legacy/qpws-permits-camping-master-2011-2016.csv   (merged 2011-16 export, normalised headers)
  legacy/qpws-permits-vehicle-master-2011-2016.csv
  camping/YYYY_Qn_*.csv                              (quarterly exports 2016Q3 ->)
  vehicle/YYYY_Qn_*.csv
  bookeasy_operators.csv                             (current camping areas with lat/lon)
  lookups/area_aliases.csv                           (optional manual park/area -> operator fixes)
  lookups/postcodes.csv                              (postcode, lat, lon, locality, state)

Outputs (under --out):
  camping.parquet   one row per camping booking
  vehicle.parquet   one row per vehicle access permit
  areas.parquet     camping areas dimension (name, park, lat/lon, match method)
  postcodes.parquet postcode dimension
  dq_report.md      data-quality report
"""
import argparse, csv, glob, io, os, re, sys, time
from pathlib import Path
import duckdb

# --------------------------------------------------------------------------- helpers
def sniff_encoding(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        raw.decode("utf-8"); return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"

def to_utf8(src: Path, dst: Path) -> None:
    """Re-encode to clean UTF-8 (no BOM), normalising line endings."""
    enc = sniff_encoding(src)
    text = src.read_bytes().decode(enc, errors="replace")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")

def quarter_from_name(name: str):
    m = re.match(r"(\d{4})_Q(\d)_", name)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)

# SQL fragments -------------------------------------------------------------
# Dates appear as 1/01/2011, 01/10/15, 10-Jan-17, 2019-04-21 ... try each.
# strip any trailing time ("1/07/2019 0:00"), then pick the format by shape so 2-digit years never parse as year 0015
DATE_EXPR = """(SELECT CASE
    WHEN regexp_matches(d, '^\\d{{1,2}}/\\d{{1,2}}/\\d{{4}}$') THEN try_strptime(d, '%d/%m/%Y')
    WHEN regexp_matches(d, '^\\d{{1,2}}/\\d{{1,2}}/\\d{{2}}$')  THEN try_strptime(d, '%d/%m/%y')
    WHEN regexp_matches(d, '^\\d{{1,2}}-[A-Za-z]{{3}}-\\d{{2}}$') THEN try_strptime(d, '%d-%b-%y')
    WHEN regexp_matches(d, '^\\d{{1,2}}-[A-Za-z]{{3}}-\\d{{4}}$') THEN try_strptime(d, '%d-%b-%Y')
    WHEN regexp_matches(d, '^\\d{{4}}-\\d{{2}}-\\d{{2}}$') THEN try_strptime(d, '%Y-%m-%d')
    WHEN regexp_matches(d, '^\\d{{1,2}}-\\d{{1,2}}-\\d{{4}}$') THEN try_strptime(d, '%d-%m-%Y')
    END FROM (SELECT regexp_replace(trim({c}), '\\s+\\d{{1,2}}:\\d{{2}}(:\\d{{2}})?\\s*$', '') AS d))::DATE"""

STATE_EXPR = """CASE upper(regexp_replace(trim(coalesce({c},'')), '[^A-Za-z ]', '', 'g'))
    WHEN 'QLD' THEN 'QLD' WHEN 'QUEENSLAND' THEN 'QLD' WHEN 'AUSTRALIA QLD' THEN 'QLD'
    WHEN 'NSW' THEN 'NSW' WHEN 'NEW SOUTH WALES' THEN 'NSW'
    WHEN 'VIC' THEN 'VIC' WHEN 'VICTORIA' THEN 'VIC'
    WHEN 'SA'  THEN 'SA'  WHEN 'SOUTH AUSTRALIA' THEN 'SA'
    WHEN 'WA'  THEN 'WA'  WHEN 'WESTERN AUSTRALIA' THEN 'WA'
    WHEN 'TAS' THEN 'TAS' WHEN 'TASMANIA' THEN 'TAS'
    WHEN 'NT'  THEN 'NT'  WHEN 'NORTHERN TERRITORY' THEN 'NT'
    WHEN 'ACT' THEN 'ACT' WHEN 'AUSTRALIAN CAPITAL TERRITORY' THEN 'ACT'
    WHEN 'OVERSEAS' THEN 'OS' WHEN 'INTERNATIONAL' THEN 'OS' WHEN 'OTHER' THEN 'OS'
    ELSE NULL END"""

# Fallback: derive state from postcode range when the state field is junk.
STATE_FROM_PC = """CASE
    WHEN pc BETWEEN 4000 AND 4999 OR pc BETWEEN 9000 AND 9999 THEN 'QLD'
    WHEN pc BETWEEN 1000 AND 2599 OR pc BETWEEN 2619 AND 2899 OR pc BETWEEN 2921 AND 2999 THEN 'NSW'
    WHEN pc BETWEEN 2600 AND 2618 OR pc BETWEEN 2900 AND 2920 THEN 'ACT'
    WHEN pc BETWEEN 3000 AND 3999 OR pc BETWEEN 8000 AND 8999 THEN 'VIC'
    WHEN pc BETWEEN 5000 AND 5999 THEN 'SA'
    WHEN pc BETWEEN 6000 AND 6999 THEN 'WA'
    WHEN pc BETWEEN 7000 AND 7999 THEN 'TAS'
    WHEN pc BETWEEN 800 AND 999 THEN 'NT'
    ELSE NULL END"""

READ = "all_varchar=true, header=true, quote='\"', escape='\"', union_by_name=true, filename=true, null_padding=true, strict_mode=false"

# Vehicle product parsing: area + duration class from whatever string we get.
AREA_EXPR = """CASE
    WHEN p ILIKE '%combined%' OR p ILIKE '%multi access%' OR p ILIKE '%combo%' THEN 'Combined Cooloola & K''gari'
    WHEN p ILIKE '%cooloola%' THEN 'Cooloola'
    WHEN p ILIKE '%fraser%' OR p ILIKE '%k''gari%' OR p ILIKE '%kgari%' THEN 'K''gari (Fraser Island)'
    WHEN p ILIKE '%bribie%' THEN 'Bribie Island'
    WHEN p ILIKE '%moreton%' THEN 'Moreton Island'
    WHEN p ILIKE '%inskip%' THEN 'Inskip Peninsula'
    WHEN p ILIKE '%tag-along%' OR p ILIKE '%tag along%' THEN 'Tag-along tour'
    ELSE 'Other' END"""

DURATION_EXPR = """CASE
    WHEN p ILIKE '%annual%' OR p ILIKE '%12 month%' THEN 'Annual'
    WHEN p ILIKE '%month%' THEN 'Monthly'
    WHEN p ILIKE '%week%' THEN 'Weekly'
    WHEN p ILIKE '%daily%' OR p ILIKE '%1 day%' OR p ILIKE '% day %' THEN 'Daily'
    ELSE 'Other' END"""

FLAGS_EXPR = """CASE
    WHEN p ILIKE '%exempt%' THEN 'Fee exempt'
    WHEN p ILIKE '%educational%' THEN 'Educational'
    WHEN p ILIKE '%assisted%' THEN 'Assisted'
    WHEN p ILIKE '%counter%' THEN 'Counter sale'
    ELSE 'Standard' END"""

# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--out", default="data/parquet")
    ap.add_argument("--work", default="data/work")
    a = ap.parse_args()
    raw, out, work = Path(a.raw), Path(a.out), Path(a.work)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    report = ["# QPWS permits ETL — data quality report", f"_Generated {time.strftime('%Y-%m-%d %H:%M')}_", ""]

    # 1. encoding pass -------------------------------------------------------
    print("Re-encoding raw files to UTF-8 ...")
    files = {"camping": [], "vehicle": []}
    import shutil
    if work.exists(): shutil.rmtree(work)          # always rebuild the work dir so removed/renamed inputs don't linger
    for kind in files:
        for src in sorted((raw / kind).glob("*.csv")):
            if "master" in src.name.lower():       # 2011-16 merged masters may sit in camping/ or vehicle/; they have the legacy schema
                to_utf8(src, work / "legacy" / f"qpws-permits-{kind}-master-2011-2016.csv"); continue
            dst = work / kind / src.name
            to_utf8(src, dst); files[kind].append(dst)
    for src in (raw / "legacy").glob("*.csv"):
        kind = "camping" if "camping" in src.name else "vehicle"
        to_utf8(src, work / "legacy" / f"qpws-permits-{kind}-master-2011-2016.csv")
    for kind in files:
        if not (work / "legacy" / f"qpws-permits-{kind}-master-2011-2016.csv").exists():
            raise SystemExit(f"legacy {kind} master not found in data/raw/legacy, data/raw/camping or data/raw/vehicle")
    print(f"  {len(files['camping'])} camping, {len(files['vehicle'])} vehicle quarterly files")

    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")

    # 2. CAMPING ---------------------------------------------------------------
    print("Camping: loading quarterly files ...")
    cq = str(work / "camping" / "*.csv")
    con.execute(f"""
    CREATE OR REPLACE TABLE camp_raw AS
    SELECT
        coalesce("Camping Reservation Unique ID","camping_reservation_unique_id","Reservation Number","Itinerary ID","BookingID") AS res_id,
        coalesce("Postcode","postcode") AS postcode_raw,
        coalesce("State","state") AS state_raw,
        coalesce("Booking Date","booking_date") AS booking_raw,
        coalesce("Start Date","start_date","Arrival Date") AS start_raw,
        coalesce("Stay Date","stay_date") AS stay_raw,
        "Departure Date" AS departure_raw,
        coalesce("Park","park","Park Name") AS park,
        coalesce("Facility","facility","Camping Area") AS area,
        coalesce("Adults","adults") AS adults_raw, coalesce("Children","children") AS children_raw, coalesce("Infants","infants") AS infants_raw,
        regexp_extract(filename, '([^/\\\\]+)$', 1) AS source_file
    FROM read_csv('{cq}', {READ})
    """)  # no Type filter: the 2025 feb-mar export labels rows 'Accommodation'; every row in a camping file is a camping record
    con.execute(f"""
    CREATE OR REPLACE TABLE camp_typed AS
    SELECT res_id, trim(park) AS park, trim(area) AS area, source_file,
        {DATE_EXPR.format(c='booking_raw')} AS booking_date,
        {DATE_EXPR.format(c='start_raw')}   AS start_date,
        {DATE_EXPR.format(c='stay_raw')}    AS stay_date,
        {DATE_EXPR.format(c='departure_raw')} AS departure_date,
        try_cast(regexp_extract(trim(postcode_raw), '(\\d{{3,4}})', 1) AS INTEGER) AS postcode,
        {STATE_EXPR.format(c='state_raw')} AS state_given,
        try_cast(adults_raw AS INTEGER) AS adults, try_cast(children_raw AS INTEGER) AS children, try_cast(infants_raw AS INTEGER) AS infants,
        booking_raw, start_raw, stay_raw
    FROM camp_raw""")

    # Era A: stay-night rows (has stay_date) -> roll up to one booking per reservation.
    # Reservations without an id (2018_Q1) use a composite key.
    con.execute("""
    CREATE OR REPLACE TABLE camp_stay AS
    WITH keyed AS (
        SELECT *, coalesce(res_id, 'K:'||coalesce(postcode::VARCHAR,'')||'|'||coalesce(booking_raw,'')||'|'||coalesce(start_raw,'')||'|'||coalesce(park,'')||'|'||coalesce(area,'')||'|'||coalesce(adults,-1)||'|'||coalesce(children,-1)||'|'||coalesce(infants,-1)) AS k
        FROM camp_typed WHERE stay_date IS NOT NULL
    ), dedup AS (
        SELECT DISTINCT ON (k, park, area, stay_date) * FROM keyed
    )
    SELECT
        CASE WHEN res_id IS NOT NULL THEN res_id END AS booking_id,
        min(booking_date) AS booking_date,
        coalesce(min(start_date), min(stay_date)) AS arrival_date,
        (max(stay_date) + INTERVAL 1 DAY)::DATE AS departure_date,
        count(DISTINCT stay_date)::INTEGER AS nights,
        max(adults) AS adults, max(children) AS children, max(infants) AS infants,
        park, area, any_value(postcode) AS postcode, any_value(state_given) AS state_given,
        min(source_file) AS source_file, 'stay_rows' AS era
    FROM dedup GROUP BY k, res_id, park, area""")

    # Era B: one row per booking with arrival/departure (2025Q1 ->)
    # Rows with an id: dedupe on id. Rows without: a booking straddling two quarters appears in both files,
    # but two genuinely different bookings can also be identical (same postcode/dates/area/party). So per
    # identical key keep the MAX count seen in any single file — removes cross-file duplicates only.
    con.execute("""
    CREATE OR REPLACE TABLE camp_span AS
    WITH base AS (SELECT * FROM camp_typed WHERE stay_date IS NULL),
    with_id AS (
        SELECT DISTINCT ON (res_id, park, area, start_date) * FROM base WHERE res_id IS NOT NULL
    ),
    no_id AS (
        SELECT postcode, state_given, booking_date, start_date, departure_date, park, area, adults, children, infants,
               min(source_file) AS source_file, max(n) AS n
        FROM (SELECT postcode, state_given, booking_date, start_date, departure_date, park, area, adults, children, infants, source_file, count(*) n
              FROM base WHERE res_id IS NULL GROUP BY ALL)
        GROUP BY ALL
    ),
    no_id_x AS (SELECT * EXCLUDE (n), unnest(range(n)) AS i FROM no_id)
    SELECT res_id AS booking_id, booking_date, start_date AS arrival_date, departure_date,
        greatest(1, date_diff('day', start_date, departure_date))::INTEGER AS nights,
        adults, children, infants, park, area, postcode, state_given, source_file, 'span_rows' AS era FROM with_id
    UNION ALL
    SELECT NULL, booking_date, start_date, departure_date,
        greatest(1, date_diff('day', start_date, departure_date))::INTEGER,
        adults, children, infants, park, area, postcode, state_given, source_file, 'span_rows' FROM no_id_x""")

    # Era L: 2011-2016 legacy master (already one row per booking; DAYS = nights)
    leg_c = str(work / "legacy" / "qpws-permits-camping-master-2011-2016.csv")
    con.execute(f"""
    CREATE OR REPLACE TABLE camp_legacy AS
    SELECT NULL::VARCHAR AS booking_id,
        {DATE_EXPR.format(c='"BOOKING_DATE"')} AS booking_date,
        {DATE_EXPR.format(c='"START_DATE"')} AS arrival_date,
        ({DATE_EXPR.format(c='"START_DATE"')} + to_days(try_cast("DAYS" AS INTEGER)))::DATE AS departure_date,
        try_cast("DAYS" AS INTEGER) AS nights,
        -- the apr2016-jun2016 export stores ADULTS/CHILDREN as person-nights (99.4% divisible by DAYS); normalise back to people
        CASE WHEN "SOURCE_FILE" = 'qpws-permits-apr2016-jun2016.csv' THEN round(try_cast("ADULTS" AS INTEGER) / greatest(1, try_cast("DAYS" AS INTEGER)))::INTEGER ELSE try_cast("ADULTS" AS INTEGER) END AS adults,
        CASE WHEN "SOURCE_FILE" = 'qpws-permits-apr2016-jun2016.csv' THEN round(try_cast("CHILDREN" AS INTEGER) / greatest(1, try_cast("DAYS" AS INTEGER)))::INTEGER ELSE try_cast("CHILDREN" AS INTEGER) END AS children,
        NULL::INTEGER AS infants,
        trim("LOCATION") AS park, trim("AREA_PERMIT_TYPE") AS area,
        try_cast(regexp_extract(trim("POSTCODE"), '(\\d{{3,4}})', 1) AS INTEGER) AS postcode,
        {STATE_EXPR.format(c='"STATE"')} AS state_given,
        "SOURCE_FILE" AS source_file, 'legacy' AS era
    FROM read_csv('{leg_c}', all_varchar=true, header=true, quote='"', escape='"')
    WHERE upper("ITEM_TYPE") <> 'CAMPING_ADDON'""")

    con.execute(f"""
    CREATE OR REPLACE TABLE camping AS
    SELECT
        row_number() OVER () AS row_id, booking_id, era, source_file,
        booking_date, arrival_date, departure_date, nights,
        adults, children, infants, coalesce(adults,0)+coalesce(children,0)+coalesce(infants,0) AS people,
        nights * (coalesce(adults,0)+coalesce(children,0)+coalesce(infants,0)) AS person_nights,
        park, area, postcode,
        coalesce(state_given, {STATE_FROM_PC.replace('pc','postcode')}) AS state,
        year(arrival_date) AS year, quarter(arrival_date) AS quarter, month(arrival_date) AS month,
        CASE WHEN booking_date IS NOT NULL AND arrival_date IS NOT NULL THEN date_diff('day', booking_date, arrival_date) END AS lead_days
    FROM (
        SELECT * FROM camp_legacy WHERE arrival_date < DATE '2016-07-01'          -- legacy export covers to Jun 2016
        UNION ALL SELECT * FROM camp_stay WHERE arrival_date >= DATE '2016-07-01' -- quarterly stay-night exports take over
        UNION ALL SELECT * FROM camp_span
    )
    WHERE arrival_date IS NOT NULL AND arrival_date BETWEEN DATE '2010-01-01' AND DATE '2027-12-31'""")

    # 3. VEHICLE ---------------------------------------------------------------
    print("Vehicle: loading quarterly files ...")
    vq = str(work / "vehicle" / "*.csv")
    con.execute(f"""
    CREATE OR REPLACE TABLE veh_raw AS
    SELECT
        coalesce("Vehicle Permit Unique ID","Confirmation Number","Itinerary ID","BookingID") AS permit_id,
        "Postcode" AS postcode_raw, "State" AS state_raw, "Booking Date" AS booking_raw, "Start Date" AS start_raw,
        trim(concat_ws(' - ', nullif(trim(coalesce("Vehicle Permit","Facility")),''), nullif(trim(coalesce("Product","Vehicle Permit Duration")),''))) AS product_raw,
        regexp_extract(filename, '([^/\\\\]+)$', 1) AS source_file
    FROM read_csv('{vq}', {READ})""")
    leg_v = str(work / "legacy" / "qpws-permits-vehicle-master-2011-2016.csv")
    con.execute(f"""
    CREATE OR REPLACE TABLE veh_legacy_raw AS
    SELECT NULL::VARCHAR AS permit_id, "POSTCODE" AS postcode_raw, "STATE" AS state_raw, "BOOKING_DATE" AS booking_raw, "START_DATE" AS start_raw,
        trim(concat_ws(' - ', trim("LOCATION"), trim("AREA_PERMIT_TYPE"))) AS product_raw, "SOURCE_FILE" AS source_file
    FROM read_csv('{leg_v}', all_varchar=true, header=true, quote='"', escape='"')""")
    con.execute(f"""
    CREATE OR REPLACE TABLE vehicle AS
    WITH typed AS (
        SELECT permit_id, source_file, product_raw AS p,
            {DATE_EXPR.format(c='booking_raw')} AS booking_date,
            {DATE_EXPR.format(c='start_raw')} AS start_date,
            try_cast(regexp_extract(trim(postcode_raw), '(\\d{{3,4}})', 1) AS INTEGER) AS postcode,
            {STATE_EXPR.format(c='state_raw')} AS state_given,
            CASE WHEN permit_id IS NULL THEN 'legacy_or_span' ELSE 'id_rows' END AS era
        FROM (SELECT * FROM veh_raw UNION ALL SELECT * FROM veh_legacy_raw)
    ), with_id AS (
        SELECT DISTINCT ON (permit_id, p, start_date) * FROM typed WHERE permit_id IS NOT NULL
    ), no_id AS (
        -- no permit id (legacy + 2025Q2→): keep max per-file count of each identical key (see camping note)
        SELECT source_file, p, booking_date, start_date, postcode, state_given, era, max(n) AS n FROM (
            SELECT min(source_file) source_file, p, booking_date, start_date, postcode, state_given, era, source_file AS sf, count(*) n
            FROM typed WHERE permit_id IS NULL GROUP BY p, booking_date, start_date, postcode, state_given, era, sf)
        GROUP BY ALL
    ), dedup AS (
        SELECT permit_id, source_file, p, booking_date, start_date, postcode, state_given, era FROM with_id
        UNION ALL
        SELECT NULL, source_file, p, booking_date, start_date, postcode, state_given, era FROM (SELECT * EXCLUDE (n), unnest(range(n)) i FROM no_id)
    )
    SELECT row_number() OVER () AS row_id, permit_id, era, source_file, booking_date, start_date,
        p AS product_raw, {AREA_EXPR} AS area, {DURATION_EXPR} AS duration, {FLAGS_EXPR} AS permit_class,
        CASE {DURATION_EXPR} WHEN 'Daily' THEN 1 WHEN 'Weekly' THEN 7 WHEN 'Monthly' THEN 30 WHEN 'Annual' THEN 365 END AS days_valid,
        postcode, coalesce(state_given, {STATE_FROM_PC.replace('pc','postcode')}) AS state,
        year(start_date) AS year, quarter(start_date) AS quarter, month(start_date) AS month,
        CASE WHEN booking_date IS NOT NULL AND start_date IS NOT NULL THEN date_diff('day', booking_date, start_date) END AS lead_days
    FROM dedup WHERE start_date IS NOT NULL AND start_date BETWEEN DATE '2010-01-01' AND DATE '2027-12-31'""")

    # 4. AREAS dimension (camping areas -> BookEasy lat/lon) ---------------------
    print("Geocoding camping areas ...")
    ops = raw / "bookeasy_operators.csv"
    con.execute(f"CREATE OR REPLACE TABLE ops AS SELECT * FROM read_csv('{ops}', all_varchar=true, header=true)")
    opcols = [c[0] for c in con.execute("DESCRIBE ops").fetchall()]
    def pick(*cands):
        for c in cands:
            for oc in opcols:
                if oc.lower() == c.lower(): return f'"{oc}"'
        return "NULL"
    name_c, park_c, lat_c, lon_c, id_c = pick("name","OperatorName","tradingname"), pick("park","location","Location"), pick("lat","latitude","Latitude"), pick("lon","lng","longitude","Longitude"), pick("operator_id","OperatorId","operatorid","id")
    con.execute(f"""
    CREATE OR REPLACE TABLE op_norm AS
    SELECT {id_c} AS operator_id, trim({name_c}) AS op_name, trim({park_c}) AS op_park,
        try_cast({lat_c} AS DOUBLE) AS lat, try_cast({lon_c} AS DOUBLE) AS lon,
        lower(regexp_replace(regexp_replace(trim({name_c}), '\\(.*?\\)', '', 'g'), '[^a-z0-9]+', ' ', 'g')) AS key
    FROM ops WHERE {lat_c} IS NOT NULL""")

    alias_path = raw / "lookups" / "area_aliases.csv"
    if alias_path.exists():
        con.execute(f"CREATE OR REPLACE TABLE aliases AS SELECT trim(park) park, trim(area) area, try_cast(operator_id AS VARCHAR) operator_id, try_cast(lat AS DOUBLE) lat, try_cast(lon AS DOUBLE) lon FROM read_csv('{alias_path}', all_varchar=true, header=true)")
    else:
        con.execute("CREATE OR REPLACE TABLE aliases (park VARCHAR, area VARCHAR, operator_id VARCHAR, lat DOUBLE, lon DOUBLE)")

    con.execute("""
    CREATE OR REPLACE TABLE areas AS
    WITH distinct_areas AS (
        SELECT park, area, count(*) AS bookings, min(arrival_date) AS first_seen, max(arrival_date) AS last_seen,
            lower(regexp_replace(regexp_replace(coalesce(area,''), '\\(.*?\\)', '', 'g'), '[^a-z0-9]+', ' ', 'g')) AS key
        FROM camping WHERE area IS NOT NULL GROUP BY park, area
    ), m_alias AS (
        SELECT d.*, coalesce(a.lat, o.lat) AS lat, coalesce(a.lon, o.lon) AS lon, coalesce(a.operator_id, o.operator_id) AS operator_id, 'alias' AS match
        FROM distinct_areas d JOIN aliases a ON a.park = d.park AND a.area = d.area LEFT JOIN op_norm o ON o.operator_id = a.operator_id
    ), rest1 AS (SELECT * FROM distinct_areas WHERE (park, area) NOT IN (SELECT park, area FROM m_alias)),
    m_exact AS (
        SELECT d.*, o.lat, o.lon, o.operator_id, 'exact' AS match
        FROM rest1 d JOIN (SELECT DISTINCT ON (key) * FROM op_norm) o ON o.key = trim(d.key)
    ), rest2 AS (SELECT * FROM rest1 WHERE (park, area) NOT IN (SELECT park, area FROM m_exact))
    SELECT park, area, bookings, first_seen, last_seen, lat, lon, operator_id, match FROM m_alias
    UNION ALL SELECT park, area, bookings, first_seen, last_seen, lat, lon, operator_id, match FROM m_exact
    UNION ALL SELECT park, area, bookings, first_seen, last_seen, NULL, NULL, NULL, 'unmatched' FROM rest2""")
    # fuzzy pass (jaro-winkler >= 0.93 on the normalised name, same park word overlap) for the leftovers
    con.execute("""
    CREATE OR REPLACE TABLE fuzzy AS
    SELECT park, area, operator_id, lat, lon FROM (
        SELECT a.park, a.area, o.operator_id, o.lat, o.lon,
            jaro_winkler_similarity(lower(regexp_replace(regexp_replace(a.area,'\\(.*?\\)','','g'),'[^a-z0-9]+',' ','g')), o.key) AS sim,
            row_number() OVER (PARTITION BY a.park, a.area ORDER BY jaro_winkler_similarity(lower(regexp_replace(regexp_replace(a.area,'\\(.*?\\)','','g'),'[^a-z0-9]+',' ','g')), o.key) DESC) rn
        FROM areas a CROSS JOIN op_norm o
        WHERE a.match = 'unmatched'
          AND (jaro_winkler_similarity(lower(a.park), lower(o.op_park)) >= 0.80 OR o.op_park ILIKE '%' || split_part(a.park, ' ', 1) || '%')
    ) WHERE rn = 1 AND sim >= 0.90""")
    con.execute("""
    UPDATE areas SET lat = f.lat, lon = f.lon, operator_id = f.operator_id, match = 'fuzzy'
    FROM fuzzy f WHERE areas.park = f.park AND areas.area = f.area""")
    # park-level fallback: centroid of matched areas in the same park
    con.execute("""
    UPDATE areas SET lat = p.lat, lon = p.lon, match = 'park_centroid'
    FROM (SELECT park, avg(lat) lat, avg(lon) lon FROM areas WHERE lat IS NOT NULL GROUP BY park) p
    WHERE areas.park = p.park AND areas.lat IS NULL""")
    # ---- canonical names: areas that resolve to the same booking-site operator get its current park/area name
    #      ("M.V. Sarawak" / "MV Sarawak", "Fraser Island (Great Sandy NP)" / "K'gari (Fraser Island) Recreation Area")
    con.execute("ALTER TABLE areas RENAME TO areas_raw")
    con.execute("""ALTER TABLE areas_raw ADD COLUMN park_c VARCHAR; ALTER TABLE areas_raw ADD COLUMN area_c VARCHAR""")
    con.execute("""UPDATE areas_raw SET park_c = coalesce(o.op_park, areas_raw.park), area_c = coalesce(o.op_name, areas_raw.area)
                   FROM (SELECT DISTINCT ON (operator_id) operator_id, op_park, op_name FROM op_norm) o WHERE areas_raw.operator_id = o.operator_id""")
    con.execute("UPDATE areas_raw SET park_c = park WHERE park_c IS NULL"); con.execute("UPDATE areas_raw SET area_c = area WHERE area_c IS NULL")
    # park renames over the years for areas with no operator match
    PARK_ALIASES = {
        "Fraser Island (Great Sandy National Park)": "K'gari (Fraser Island) Recreation Area",
        "Fraser Island Recreation Area": "K'gari (Fraser Island) Recreation Area",
        "Fraser Island Great Walk": "K'gari (Fraser Island) Great Walk",
        "Moreton Island National Park": "Moreton Island Recreation Area",
        "D Aguilar National Park": "D'Aguilar National Park",
        "Lamington National Park (Green Mountains)": "Lamington National Park",
    }
    for old_p, new_p in PARK_ALIASES.items():
        con.execute("UPDATE areas_raw SET park_c = ? WHERE park_c = ?", [new_p, old_p])
    con.execute("ALTER TABLE camping ADD COLUMN park_raw VARCHAR"); con.execute("ALTER TABLE camping ADD COLUMN area_raw VARCHAR")
    con.execute("UPDATE camping SET park_raw = park, area_raw = area")
    con.execute("""UPDATE camping SET park = a.park_c, area = a.area_c FROM areas_raw a
                   WHERE camping.park_raw = a.park AND camping.area_raw IS NOT DISTINCT FROM a.area""")
    con.execute("""CREATE OR REPLACE TABLE areas AS
        SELECT park_c AS park, area_c AS area, sum(bookings) AS bookings, min(first_seen) AS first_seen, max(last_seen) AS last_seen,
               any_value(lat) AS lat, any_value(lon) AS lon, any_value(operator_id) AS operator_id,
               min(match) AS match, count(*) AS raw_name_variants, string_agg(DISTINCT area, ' | ') AS raw_names
        FROM areas_raw GROUP BY park_c, area_c""")
    con.execute("ALTER TABLE camping ADD COLUMN lat DOUBLE"); con.execute("ALTER TABLE camping ADD COLUMN lon DOUBLE")
    con.execute("UPDATE camping SET lat = a.lat, lon = a.lon FROM areas a WHERE camping.park = a.park AND camping.area IS NOT DISTINCT FROM a.area")

    # 5. POSTCODES -----------------------------------------------------------------
    pcs = raw / "lookups" / "postcodes.csv"
    if pcs.exists():
        con.execute(f"""CREATE OR REPLACE TABLE postcodes AS
            SELECT DISTINCT ON (postcode) try_cast(postcode AS INTEGER) postcode, locality, state, try_cast(lat AS DOUBLE) lat, try_cast(lon AS DOUBLE) lon, sa4_name
            FROM read_csv('{pcs}', all_varchar=true, header=true) WHERE try_cast(postcode AS INTEGER) IS NOT NULL""")
    else:
        con.execute("CREATE OR REPLACE TABLE postcodes (postcode INTEGER, locality VARCHAR, state VARCHAR, lat DOUBLE, lon DOUBLE, sa4_name VARCHAR)")

    # 5b. CALENDAR (public + school holidays) -> day_type on both fact tables -----------------
    print("Calendar ...")
    import sys; sys.path.insert(0, str(Path(__file__).resolve().parent))
    from build_calendar import build as build_cal, write_csv as write_cal
    cal_csv = work / "calendar.csv"
    write_cal(build_cal(raw / "lookups" / "qld_school_terms.csv"), cal_csv)
    con.execute(f"CREATE OR REPLACE TABLE calendar AS SELECT * FROM read_csv('{cal_csv.as_posix()}', header=true)")
    for t, dcol in (("camping", "arrival_date"), ("vehicle", "start_date")):
        con.execute(f"""CREATE OR REPLACE TABLE {t}_cal AS
            SELECT f.*, c.day_type, c.is_school_holiday, c.is_public_holiday, c.is_long_weekend, c.is_weekend,
                   c.school_holiday, c.public_holiday, c.school_term
            FROM {t} f LEFT JOIN calendar c ON c.date = f.{dcol}""")
        con.execute(f"DROP TABLE {t}"); con.execute(f"ALTER TABLE {t}_cal RENAME TO {t}")
    # camping: how many of the booking's nights fall in school holidays / on weekends
    con.execute("""CREATE OR REPLACE TABLE nights_x AS
        SELECT b.row_id, count(*) FILTER (WHERE c.is_school_holiday) AS sh_nights, count(*) FILTER (WHERE c.is_weekend OR c.is_public_holiday) AS we_nights
        FROM (SELECT row_id, arrival_date, nights FROM camping WHERE nights BETWEEN 1 AND 60) b,
             LATERAL (SELECT unnest(range(0, b.nights)) AS i) g
        JOIN calendar c ON c.date = b.arrival_date + to_days(g.i::INTEGER)
        GROUP BY b.row_id""")
    con.execute("ALTER TABLE camping ADD COLUMN sh_nights INTEGER"); con.execute("ALTER TABLE camping ADD COLUMN we_nights INTEGER")
    con.execute("UPDATE camping SET sh_nights = n.sh_nights, we_nights = n.we_nights FROM nights_x n WHERE camping.row_id = n.row_id")

    # 5c. CAPACITY (BookEasy rooms) + nightly OCCUPANCY per area -------------------------------
    print("Capacity & occupancy ...")
    rooms = raw / "bookeasy_rooms.csv"
    if rooms.exists():
        con.execute(f"""CREATE OR REPLACE TABLE capacity AS
            WITH rooms AS (
                SELECT DISTINCT ON (OperatorId, RoomID) OperatorId, RoomID, Name, "Attr_CAMPING TYPE" AS ctype, "Attr_STAY IN A" AS stay_in, "Attr_SITE ACCESS" AS access,
                    try_cast(NoRooms AS INTEGER) AS n_rooms, try_cast(NoPersons AS INTEGER) AS n_persons
                FROM read_csv('{rooms.as_posix()}', all_varchar=true, header=true)
                WHERE Name NOT ILIKE '%(edu)%' AND try_cast(NoRooms AS INTEGER) > 0
            ), typed AS (
                -- a room is a PEOPLE cap when it is open camping, or 'N sites of 1 person' (N = people), or a few sites with huge per-site caps
                SELECT *, (coalesce(ctype,'') ILIKE '%open%' OR n_persons = 1 OR n_persons >= 20) AS people_style FROM rooms
            )
            SELECT OperatorId AS operator_id,
                CASE WHEN sum(CASE WHEN people_style THEN n_rooms * n_persons ELSE 0 END) >= 0.5 * sum(n_rooms * n_persons) THEN 'people' ELSE 'sites' END AS capacity_type,
                nullif(sum(CASE WHEN people_style THEN 0 ELSE n_rooms END), 0) AS sites_cap,
                sum(n_rooms * n_persons) AS people_cap,
                max(CASE WHEN people_style THEN NULL ELSE n_persons END) AS max_party,
                count(*) AS site_types,
                string_agg(DISTINCT stay_in, ' | ') AS stay_in, string_agg(DISTINCT access, ' | ') AS access
            FROM typed GROUP BY OperatorId""")
    else:
        con.execute("CREATE OR REPLACE TABLE capacity (operator_id VARCHAR, capacity_type VARCHAR, sites_cap INTEGER, people_cap INTEGER, max_party INTEGER, site_types INTEGER, stay_in VARCHAR, access VARCHAR)")
    for col, typ in (("capacity_type","VARCHAR"),("sites_cap","INTEGER"),("people_cap","INTEGER"),("max_party","INTEGER"),("site_types","INTEGER"),("stay_in","VARCHAR"),("access","VARCHAR")):
        con.execute(f"ALTER TABLE areas ADD COLUMN IF NOT EXISTS {col} {typ}")
    con.execute("""UPDATE areas SET capacity_type=c.capacity_type, sites_cap=c.sites_cap, people_cap=c.people_cap, max_party=c.max_party, site_types=c.site_types, stay_in=c.stay_in, access=c.access
                   FROM capacity c WHERE areas.operator_id = c.operator_id""")
    # nightly in-house counts per area (explode bookings into nights), then occupancy vs current capacity
    con.execute("""CREATE OR REPLACE TABLE area_day AS
        WITH nights AS (
            SELECT b.park, b.area, b.arrival_date + to_days(g.i::INTEGER) AS date, b.people
            FROM (SELECT park, area, arrival_date, nights, people FROM camping WHERE nights BETWEEN 1 AND 60 AND area IS NOT NULL) b,
                 LATERAL (SELECT unnest(range(0, b.nights)) AS i) g
        ), agg AS (
            SELECT park, area, date, count(*) AS bookings_in_house, sum(people) AS people_in_house FROM nights GROUP BY ALL
        )
        SELECT a.park, a.area, a.date, year(a.date) AS year, a.bookings_in_house, a.people_in_house,
            ar.capacity_type, ar.sites_cap, ar.people_cap, ar.lat, ar.lon,
            CASE WHEN ar.sites_cap  > 0 THEN round(100.0 * a.bookings_in_house / ar.sites_cap, 1) END AS site_occ_pct,
            CASE WHEN ar.people_cap > 0 THEN round(100.0 * a.people_in_house  / ar.people_cap, 1) END AS people_occ_pct,
            CASE WHEN ar.capacity_type = 'people' AND ar.people_cap > 0 THEN round(100.0 * a.people_in_house / ar.people_cap, 1)
                 WHEN ar.sites_cap > 0 THEN round(100.0 * a.bookings_in_house / ar.sites_cap, 1) END AS occ_pct,
            c.day_type, c.is_school_holiday, c.is_public_holiday, c.is_long_weekend, c.is_weekend, c.school_holiday
        FROM agg a LEFT JOIN areas ar ON ar.park = a.park AND ar.area = a.area LEFT JOIN calendar c ON c.date = a.date""")

    # 6. WRITE ---------------------------------------------------------------------
    import shutil
    for t in ("camping", "vehicle", "area_day"):   # partitioned by year: keeps every file small and lets DuckDB prune by date
        d = out / t
        if d.exists(): shutil.rmtree(d)
        con.execute(f"COPY (SELECT * FROM {t}) TO '{d.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (year), OVERWRITE_OR_IGNORE)")
        print(f"  wrote {t}/year=*/  rows={con.execute(f'select count(*) from {t}').fetchone()[0]:,}")
    for t in ("areas", "postcodes", "calendar"):
        con.execute(f"COPY {t} TO '{(out / (t + '.parquet')).as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        print(f"  wrote {t}.parquet  rows={con.execute(f'select count(*) from {t}').fetchone()[0]:,}")

    # 7. DQ REPORT -------------------------------------------------------------------
    def tbl(sql, hdr):
        rows = con.execute(sql).fetchall()
        s = ["| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
        s += ["| " + " | ".join("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)) for v in r) + " |" for r in rows]
        return "\n".join(s)
    report += ["## Row counts", tbl("""SELECT 'camping' t, count(*) rows, min(arrival_date) first, max(arrival_date) last, sum(nights) nights, sum(person_nights) person_nights FROM camping
        UNION ALL SELECT 'vehicle', count(*), min(start_date), max(start_date), NULL, NULL FROM vehicle""", ["table","rows","first","last","nights","person_nights"]), ""]
    report += ["## Camping rows by era", tbl("SELECT era, count(*) rows, min(arrival_date) first, max(arrival_date) last FROM camping GROUP BY 1 ORDER BY 3", ["era","rows","first","last"]), ""]
    report += ["## Camping bookings by year", tbl("SELECT year, count(*) bookings, sum(nights) nights, sum(person_nights) person_nights, round(avg(nights),2) avg_nights FROM camping GROUP BY 1 ORDER BY 1", ["year","bookings","nights","person_nights","avg_nights"]), ""]
    report += ["## Vehicle permits by year", tbl("SELECT year, count(*) permits FROM vehicle GROUP BY 1 ORDER BY 1", ["year","permits"]), ""]
    report += ["## Vehicle area x duration", tbl("SELECT area, duration, permit_class, count(*) n FROM vehicle GROUP BY ALL ORDER BY n DESC", ["area","duration","class","n"]), ""]
    report += ["## Vehicle 'Other' products (unparsed)", tbl("SELECT product_raw, count(*) n FROM vehicle WHERE area='Other' OR duration='Other' GROUP BY 1 ORDER BY n DESC LIMIT 30", ["product_raw","n"]), ""]
    report += ["## Area names merged into one canonical campground (top)", tbl("SELECT park, area, raw_name_variants, raw_names FROM areas WHERE raw_name_variants > 1 ORDER BY bookings DESC LIMIT 40", ["park","canonical area","variants","raw names"]), ""]
    report += ["## Camping area geocoding (raw names)", tbl("SELECT match, count(*) areas, sum(bookings) bookings FROM areas_raw GROUP BY 1 ORDER BY 3 DESC", ["match","areas","bookings"]), ""]
    report += ["## Top unmatched / park-centroid areas (add to lookups/area_aliases.csv)", tbl("SELECT park, area, bookings, first_seen, last_seen, match FROM areas_raw WHERE match IN ('unmatched','park_centroid') ORDER BY bookings DESC LIMIT 60", ["park","area","bookings","first","last","match"]), ""]
    report += ["## Date parse failures (raw rows with unparseable arrival/start)", tbl("""SELECT 'camping' t, count(*) n FROM camp_typed WHERE start_date IS NULL AND stay_date IS NULL
        UNION ALL SELECT 'vehicle', count(*) FROM (SELECT * FROM veh_raw) r WHERE """ + DATE_EXPR.format(c='start_raw') + " IS NULL", ["table","rows_dropped"]), ""]
    report += ["## Camping bookings per day by day type (arrival date, whole period)", tbl("""SELECT c.day_type, count(DISTINCT c.date) AS n_days, count(b.row_id) AS bookings, round(count(b.row_id)*1.0/count(DISTINCT c.date),1) per_day
        FROM calendar c LEFT JOIN camping b ON b.arrival_date = c.date WHERE c.date BETWEEN (SELECT min(arrival_date) FROM camping) AND (SELECT max(arrival_date) FROM camping) GROUP BY 1 ORDER BY per_day DESC""", ["day_type","days","bookings","bookings_per_day"]), ""]
    report += ["## Capacity coverage (current BookEasy site config applied to all years)", tbl("""SELECT capacity_type, count(*) areas, sum(bookings) bookings, sum(sites_cap) sites, sum(people_cap) people_cap FROM areas GROUP BY 1 ORDER BY 2 DESC""", ["capacity_type","areas","bookings","sites","people_cap"]), ""]
    report += ["## Occupancy sanity: area-nights over 100% of cap (expect some — group bookings, config changes)", tbl("""SELECT CASE WHEN occ_pct IS NULL THEN 'no cap' WHEN occ_pct<=50 THEN '0-50%' WHEN occ_pct<=90 THEN '50-90%' WHEN occ_pct<=100 THEN '90-100%' WHEN occ_pct<=150 THEN '100-150%' ELSE '>150%' END band, count(*) area_nights FROM area_day GROUP BY 1 ORDER BY 1""", ["band","area_nights"]), ""]
    report += ["## State normalisation", tbl("SELECT state, count(*) n FROM camping GROUP BY 1 ORDER BY n DESC", ["state","camping_rows"]), ""]
    report += ["## Suspicious values", tbl("""SELECT 'camping nights > 60' chk, count(*) n FROM camping WHERE nights > 60
        UNION ALL SELECT 'camping people > 30', count(*) FROM camping WHERE people > 30
        UNION ALL SELECT 'camping lead_days < 0', count(*) FROM camping WHERE lead_days < 0
        UNION ALL SELECT 'camping lead_days > 400', count(*) FROM camping WHERE lead_days > 400
        UNION ALL SELECT 'vehicle lead_days < 0', count(*) FROM vehicle WHERE lead_days < 0
        UNION ALL SELECT 'camping postcode null', count(*) FROM camping WHERE postcode IS NULL
        UNION ALL SELECT 'vehicle postcode null', count(*) FROM vehicle WHERE postcode IS NULL""", ["check","rows"]), ""]
    report += ["## Source files: labelled quarter vs arrival-date span", tbl("""SELECT source_file, count(*) rows, min(arrival_date) first, max(arrival_date) last FROM camping WHERE era <> 'legacy' GROUP BY 1 ORDER BY 1""", ["file","rows","first_arrival","last_arrival"]), ""]
    (out / "dq_report.md").write_text("\n".join(report), encoding="utf-8")
    print(f"Done in {time.time()-t0:.0f}s. Report: {out/'dq_report.md'}")

if __name__ == "__main__":
    main()
