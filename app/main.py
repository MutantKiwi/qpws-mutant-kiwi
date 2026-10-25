"""
QPWS Permits Explorer — FastAPI + DuckDB backend.

All endpoints take the same filter query-string (see Filters) and run parameterised SQL
against Parquet files loaded as DuckDB views. Nothing is mutated at runtime.
"""
from __future__ import annotations

import csv, io, os, threading
from datetime import date
from pathlib import Path
from typing import Literal, Optional

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data" / "parquet"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="QPWS Permits Explorer", version="1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")

# --------------------------------------------------------------------------- db
_db = duckdb.connect(database=":memory:")
_db.execute("SET threads TO 4")
for t in ("camping", "vehicle", "area_day"):
    d = DATA_DIR / t
    if not d.exists():
        raise SystemExit(f"missing {d}; run etl/etl.py first")
    _db.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{d.as_posix()}/**/*.parquet', hive_partitioning=true)")
for t in ("areas", "postcodes", "calendar"):
    p = DATA_DIR / f"{t}.parquet"
    if not p.exists():
        raise SystemExit(f"missing {p}; run etl/etl.py first")
    _db.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{p.as_posix()}')")
# camping joined to postcode centroid once, as a view
_db.execute("""CREATE VIEW camping_x AS
    SELECT c.*, p.lat AS o_lat, p.lon AS o_lon, p.locality AS o_locality, p.sa4_name AS o_sa4
    FROM camping c LEFT JOIN postcodes p USING (postcode)""")
_db.execute("""CREATE VIEW vehicle_x AS
    SELECT v.*, p.lat AS o_lat, p.lon AS o_lon, p.locality AS o_locality, p.sa4_name AS o_sa4
    FROM vehicle v LEFT JOIN postcodes p USING (postcode)""")
_lock = threading.Lock()

def q(sql: str, params: list | None = None) -> list[dict]:
    with _lock:
        cur = _db.cursor()
        try:
            res = cur.execute(sql, params or [])
            cols = [d[0] for d in res.description]
            return [dict(zip(cols, r)) for r in res.fetchall()]
        finally:
            cur.close()

# --------------------------------------------------------------------------- filters
Dataset = Literal["camping", "vehicle"]

class Filters(BaseModel):
    dataset: Dataset = "camping"
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    state: list[str] = []
    postcode: list[int] = []
    park: list[str] = []
    area: list[str] = []            # camping area / vehicle recreation area
    duration: list[str] = []        # vehicle only
    permit_class: list[str] = []    # vehicle only
    day_type: list[str] = []        # School holiday | Public holiday | Long weekend | Weekend | Weekday  (arrival/start date)
    holiday: list[str] = []         # Easter | Winter | Spring | Summer  (school holiday period)
    q: Optional[str] = None         # free text over park/area/product/locality

    @property
    def table(self) -> str:
        return "camping_x" if self.dataset == "camping" else "vehicle_x"

    @property
    def date_col(self) -> str:
        return "arrival_date" if self.dataset == "camping" else "start_date"

    def where(self) -> tuple[str, list]:
        w, p = ["1=1"], []
        d = self.date_col
        if self.date_from: w.append(f"{d} >= ?"); p.append(self.date_from)
        if self.date_to:   w.append(f"{d} <= ?"); p.append(self.date_to)
        def inlist(col, vals):
            if vals:
                w.append(f"{col} IN ({','.join('?'*len(vals))})"); p.extend(vals)
        inlist("state", self.state); inlist("postcode", self.postcode)
        inlist("day_type", self.day_type); inlist("school_holiday", self.holiday)
        if self.dataset == "camping":
            inlist("park", self.park); inlist("area", self.area)
        else:
            inlist("area", self.area); inlist("duration", self.duration); inlist("permit_class", self.permit_class)
        if self.q:
            like = f"%{self.q}%"
            if self.dataset == "camping":
                w.append("(park ILIKE ? OR area ILIKE ? OR o_locality ILIKE ? OR CAST(postcode AS VARCHAR) = ?)"); p += [like, like, like, self.q]
            else:
                w.append("(area ILIKE ? OR product_raw ILIKE ? OR o_locality ILIKE ? OR CAST(postcode AS VARCHAR) = ?)"); p += [like, like, like, self.q]
        return " AND ".join(w), p

def parse_filters(request: Request) -> Filters:
    qp = request.query_params
    def lst(k): return [v for v in qp.getlist(k) if v != ""]
    try:
        return Filters(
            dataset=qp.get("dataset", "camping"),
            date_from=qp.get("from") or None, date_to=qp.get("to") or None,
            state=lst("state"), postcode=[int(x) for x in lst("postcode")],
            park=lst("park"), area=lst("area"), duration=lst("duration"), permit_class=lst("permit_class"),
            day_type=lst("day_type"), holiday=lst("holiday"), q=qp.get("q") or None)
    except Exception as e:  # noqa
        raise HTTPException(400, f"bad filter: {e}")

# measures available per dataset
MEASURES = {
    "camping": {"bookings": "count(*)", "nights": "sum(nights)", "person_nights": "sum(person_nights)", "people": "sum(people)",
                "avg_nights": "avg(nights)", "avg_people": "avg(people)", "avg_lead_days": "avg(lead_days)"},
    "vehicle": {"permits": "count(*)", "permit_days": "sum(days_valid)", "avg_lead_days": "avg(lead_days)"},
}
DIMS = {
    "camping": {"park": "park", "area": "area", "state": "state", "sa4": "o_sa4", "postcode": "postcode", "locality": "o_locality",
                "year": "year", "quarter": "quarter", "month": "month", "dow": "dayofweek(arrival_date)",
                "nights_bucket": "CASE WHEN nights=1 THEN '1' WHEN nights=2 THEN '2' WHEN nights<=4 THEN '3-4' WHEN nights<=7 THEN '5-7' ELSE '8+' END",
                "party_size": "CASE WHEN people<=1 THEN '1' WHEN people=2 THEN '2' WHEN people<=4 THEN '3-4' WHEN people<=8 THEN '5-8' ELSE '9+' END",
                "lead_bucket": "CASE WHEN lead_days<=0 THEN 'same day' WHEN lead_days<=7 THEN '1-7d' WHEN lead_days<=30 THEN '8-30d' WHEN lead_days<=90 THEN '1-3m' WHEN lead_days<=180 THEN '3-6m' ELSE '6m+' END",
                "day_type": "day_type", "school_holiday": "coalesce(school_holiday, 'Term time')", "school_term": "school_term", "era": "era"},
    "vehicle": {"area": "area", "duration": "duration", "permit_class": "permit_class", "product": "product_raw", "state": "state", "sa4": "o_sa4",
                "postcode": "postcode", "locality": "o_locality", "year": "year", "quarter": "quarter", "month": "month", "dow": "dayofweek(start_date)",
                "lead_bucket": "CASE WHEN lead_days<=0 THEN 'same day' WHEN lead_days<=7 THEN '1-7d' WHEN lead_days<=30 THEN '8-30d' WHEN lead_days<=90 THEN '1-3m' WHEN lead_days<=180 THEN '3-6m' ELSE '6m+' END",
                "day_type": "day_type", "school_holiday": "coalesce(school_holiday, 'Term time')", "school_term": "school_term", "era": "era"},
}
DAY_TYPES = ["School holiday", "Public holiday", "Long weekend", "Weekend", "Weekday"]
HOLIDAYS = ["Easter", "Winter", "Spring", "Summer"]

def measure(f: Filters, m: str) -> str:
    try: return MEASURES[f.dataset][m]
    except KeyError: raise HTTPException(400, f"unknown measure '{m}' for {f.dataset}; options: {list(MEASURES[f.dataset])}")

def dim(f: Filters, d: str) -> str:
    try: return DIMS[f.dataset][d]
    except KeyError: raise HTTPException(400, f"unknown dimension '{d}' for {f.dataset}; options: {list(DIMS[f.dataset])}")

# --------------------------------------------------------------------------- endpoints
@app.get("/api/meta")
def meta():
    c = q("SELECT min(arrival_date) d0, max(arrival_date) d1, count(*) n, count(DISTINCT park) parks, count(DISTINCT area) areas FROM camping")[0]
    v = q("SELECT min(start_date) d0, max(start_date) d1, count(*) n FROM vehicle")[0]
    return {
        "camping": {"date_min": c["d0"], "date_max": c["d1"], "rows": c["n"], "parks": c["parks"], "areas": c["areas"],
                    "measures": list(MEASURES["camping"]), "dimensions": list(DIMS["camping"]),
                    "park_list": [r["park"] for r in q("SELECT park FROM camping GROUP BY 1 ORDER BY count(*) DESC")]},
        "vehicle": {"date_min": v["d0"], "date_max": v["d1"], "rows": v["n"],
                    "measures": list(MEASURES["vehicle"]), "dimensions": list(DIMS["vehicle"]),
                    "area_list": [r["area"] for r in q("SELECT area FROM vehicle GROUP BY 1 ORDER BY count(*) DESC")],
                    "duration_list": ["Daily", "Weekly", "Monthly", "Annual", "Other"],
                    "class_list": [r["permit_class"] for r in q("SELECT permit_class FROM vehicle GROUP BY 1 ORDER BY count(*) DESC")]},
        "states": ["QLD", "NSW", "VIC", "SA", "WA", "TAS", "NT", "ACT", "OS"],
        "day_types": DAY_TYPES, "holidays": HOLIDAYS,
    }

@app.get("/api/summary")
def summary(request: Request):
    f = parse_filters(request); w, p = f.where()
    if f.dataset == "camping":
        sql = f"""SELECT count(*) bookings, sum(nights) nights, sum(person_nights) person_nights, sum(people) people,
            round(avg(nights),2) avg_nights, round(avg(people),2) avg_people, round(median(lead_days)) median_lead_days,
            round(100.0*sum(CASE WHEN state<>'QLD' THEN 1 ELSE 0 END)/nullif(count(*),0),1) pct_non_qld,
            round(100.0*sum(CASE WHEN is_school_holiday THEN 1 ELSE 0 END)/nullif(count(*),0),1) pct_school_holiday,
            round(100.0*sum(sh_nights)/nullif(sum(nights),0),1) pct_nights_school_holiday,
            round(100.0*sum(CASE WHEN lat IS NOT NULL THEN 1 ELSE 0 END)/nullif(count(*),0),1) pct_geocoded,
            count(DISTINCT park) parks, count(DISTINCT area) areas, min(arrival_date) date_min, max(arrival_date) date_max
            FROM camping_x WHERE {w}"""
    else:
        sql = f"""SELECT count(*) permits, sum(days_valid) permit_days, round(median(lead_days)) median_lead_days,
            round(100.0*sum(CASE WHEN state<>'QLD' THEN 1 ELSE 0 END)/nullif(count(*),0),1) pct_non_qld,
            round(100.0*sum(CASE WHEN duration='Annual' THEN 1 ELSE 0 END)/nullif(count(*),0),1) pct_annual,
            round(100.0*sum(CASE WHEN is_school_holiday THEN 1 ELSE 0 END)/nullif(count(*),0),1) pct_school_holiday,
            count(DISTINCT area) areas, min(start_date) date_min, max(start_date) date_max
            FROM vehicle_x WHERE {w}"""
    return q(sql, p)[0]

@app.get("/api/timeseries")
def timeseries(request: Request, grain: Literal["day", "week", "month", "quarter", "year"] = "month",
               metric: str = None, by: Optional[str] = None, top: int = Query(8, le=30)):
    f = parse_filters(request); w, p = f.where()
    m = measure(f, metric or list(MEASURES[f.dataset])[0])
    period = f"date_trunc('{grain}', {f.date_col})"
    if by:
        d = dim(f, by)
        # keep the top-N groups, lump the rest as "Other"
        sql = f"""WITH topg AS (SELECT {d} g FROM {f.table} WHERE {w} GROUP BY 1 ORDER BY count(*) DESC LIMIT {top})
            SELECT {period} AS period, CASE WHEN {d} IN (SELECT g FROM topg) THEN CAST({d} AS VARCHAR) ELSE 'Other' END AS grp, {m} AS value
            FROM {f.table} WHERE {w} GROUP BY 1,2 ORDER BY 1,2"""
        return q(sql, p + p)
    return q(f"SELECT {period} AS period, {m} AS value FROM {f.table} WHERE {w} GROUP BY 1 ORDER BY 1", p)

@app.get("/api/seasonality")
def seasonality(request: Request, metric: str = None):
    """month x year matrix for heatmap"""
    f = parse_filters(request); w, p = f.where()
    m = measure(f, metric or list(MEASURES[f.dataset])[0])
    return q(f"SELECT year, month, {m} AS value FROM {f.table} WHERE {w} GROUP BY 1,2 ORDER BY 1,2", p)

@app.get("/api/breakdown")
def breakdown(request: Request, by: str = "park", metric: str = None, top: int = Query(25, le=500)):
    f = parse_filters(request); w, p = f.where()
    d = dim(f, by); m = measure(f, metric or list(MEASURES[f.dataset])[0])
    extra = ", round(avg(nights),2) avg_nights, sum(person_nights) person_nights" if f.dataset == "camping" else ", sum(days_valid) permit_days"
    return q(f"SELECT {d} AS key, count(*) AS n, {m} AS value{extra} FROM {f.table} WHERE {w} AND {d} IS NOT NULL GROUP BY 1 ORDER BY value DESC NULLS LAST LIMIT {top}", p)

@app.get("/api/yoy")
def yoy(request: Request, metric: str = None):
    """same-month comparison across years: rows = month, cols = year"""
    f = parse_filters(request); w, p = f.where()
    m = measure(f, metric or list(MEASURES[f.dataset])[0])
    return q(f"SELECT year, month, {m} AS value FROM {f.table} WHERE {w} GROUP BY 1,2 ORDER BY 1,2", p)

@app.get("/api/daytype")
def daytype(request: Request, metric: str = None, by: Literal["day_type", "school_holiday"] = "day_type"):
    """Per-day rate by day type: metric total / number of calendar days of that type in the filtered date range.
    Day counts come from the calendar (so they are unaffected by park/state filters); totals honour all filters."""
    f = parse_filters(request); w, p = f.where()
    m = measure(f, metric or list(MEASURES[f.dataset])[0])
    rng = q(f"SELECT min({f.date_col}) d0, max({f.date_col}) d1 FROM {f.table} WHERE {w}", p)[0]
    if not rng["d0"]: return []
    d0 = f.date_from or rng["d0"]; d1 = f.date_to or rng["d1"]
    key = "day_type" if by == "day_type" else "coalesce(school_holiday, 'Term time')"
    days = {r["k"]: r["n"] for r in q(f"SELECT {key} k, count(*) n FROM calendar WHERE date BETWEEN ? AND ? GROUP BY 1", [d0, d1])}
    rows = q(f"SELECT {key} AS key, count(*) AS n, {m} AS value FROM {f.table} WHERE {w} AND {f.date_col} BETWEEN ? AND ? GROUP BY 1", p + [d0, d1])
    order = DAY_TYPES if by == "day_type" else ["Term time"] + HOLIDAYS
    out = []
    for k in order:
        r = next((x for x in rows if x["key"] == k), None)
        nd = days.get(k, 0)
        if r or nd:
            out.append({"key": k, "days": nd, "n": r["n"] if r else 0, "value": r["value"] if r else 0,
                        "per_day": round((r["value"] or 0) / nd, 2) if (r and nd) else None})
    return out

@app.get("/api/calendar")
def calendar_periods(date_from: Optional[date] = Query(None, alias="from"), date_to: Optional[date] = Query(None, alias="to")):
    """School-holiday periods (as ranges) and public holidays, for chart overlays."""
    d0 = date_from or date(2010, 1, 1); d1 = date_to or date(2029, 12, 31)
    periods = q("""WITH x AS (SELECT date, school_holiday, date - to_days(row_number() OVER (PARTITION BY school_holiday ORDER BY date)::INTEGER) AS grp
                              FROM calendar WHERE is_school_holiday AND date BETWEEN ? AND ?)
                   SELECT school_holiday AS name, min(date) AS start, max(date) AS "end", count(*) AS days FROM x GROUP BY school_holiday, grp ORDER BY start""", [d0, d1])
    ph = q("SELECT date, public_holiday AS name, is_long_weekend FROM calendar WHERE is_public_holiday AND date BETWEEN ? AND ? ORDER BY date", [d0, d1])
    return {"school_holidays": periods, "public_holidays": ph}

# ---- occupancy (camping only; uses area_day = in-house counts per area per night vs current BookEasy capacity)
def occ_where(f: Filters) -> tuple[str, list]:
    w, p = ["occ_pct IS NOT NULL"], []
    if f.date_from: w.append("date >= ?"); p.append(f.date_from)
    if f.date_to:   w.append("date <= ?"); p.append(f.date_to)
    for col, vals in (("park", f.park), ("area", f.area), ("day_type", f.day_type), ("school_holiday", f.holiday)):
        if vals: w.append(f"{col} IN ({','.join('?'*len(vals))})"); p.extend(vals)
    if f.q: w.append("(park ILIKE ? OR area ILIKE ?)"); p += [f"%{f.q}%", f"%{f.q}%"]
    return " AND ".join(w), p

OCC_BASIS = {"auto": "occ_pct", "sites": "site_occ_pct", "people": "people_occ_pct"}

@app.get("/api/occupancy/summary")
def occupancy_summary(request: Request, basis: Literal["auto", "sites", "people"] = "auto"):
    f = parse_filters(request); w, p = occ_where(f); col = OCC_BASIS[basis]
    w = w.replace("occ_pct IS NOT NULL", f"{col} IS NOT NULL")
    return q(f"""SELECT count(*) area_nights, count(DISTINCT area) areas, round(avg({col}),1) avg_occ,
        round(100.0*sum(CASE WHEN {col} >= 90 THEN 1 ELSE 0 END)/nullif(count(*),0),1) pct_nights_full,
        round(100.0*sum(CASE WHEN {col} >= 50 THEN 1 ELSE 0 END)/nullif(count(*),0),1) pct_nights_half,
        max({col}) max_occ FROM area_day WHERE {w}""", p)[0]

@app.get("/api/occupancy/timeseries")
def occupancy_timeseries(request: Request, grain: Literal["day", "week", "month", "quarter", "year"] = "month",
                         basis: Literal["auto", "sites", "people"] = "auto", stat: Literal["avg", "max", "p90"] = "avg"):
    """Occupancy over time. For a single area this is its nightly occupancy; across several areas it is the
    average across area-nights (so an empty remote area pulls the average down - filter to what you care about)."""
    f = parse_filters(request); w, p = occ_where(f); col = OCC_BASIS[basis]
    w = w.replace("occ_pct IS NOT NULL", f"{col} IS NOT NULL")
    agg = {"avg": f"round(avg({col}),1)", "max": f"max({col})", "p90": f"round(quantile_cont({col}, 0.9),1)"}[stat]
    return q(f"""SELECT date_trunc('{grain}', date) AS period, {agg} AS value, count(*) AS area_nights,
        round(100.0*sum(CASE WHEN {col} >= 90 THEN 1 ELSE 0 END)/count(*),1) AS pct_full
        FROM area_day WHERE {w} GROUP BY 1 ORDER BY 1""", p)

@app.get("/api/occupancy/areas")
def occupancy_areas(request: Request, basis: Literal["auto", "sites", "people"] = "auto", top: int = Query(500, le=2000), min_nights: int = 30):
    """Per-area occupancy for the filtered period: average, % of nights >= 90% full, capacity."""
    f = parse_filters(request); w, p = occ_where(f); col = OCC_BASIS[basis]
    w = w.replace("occ_pct IS NOT NULL", f"{col} IS NOT NULL")
    return q(f"""SELECT park, area, any_value(lat) lat, any_value(lon) lon, any_value(capacity_type) capacity_type, any_value(sites_cap) sites_cap, any_value(people_cap) people_cap,
        count(*) AS nights_with_guests, round(avg({col}),1) AS avg_occ, round(quantile_cont({col},0.9),1) AS p90_occ,
        sum(CASE WHEN {col} >= 90 THEN 1 ELSE 0 END) AS nights_full, round(avg(people_in_house),1) AS avg_people
        FROM area_day WHERE {w} GROUP BY park, area HAVING count(*) >= {int(min_nights)} ORDER BY avg_occ DESC LIMIT {top}""", p)

@app.get("/api/occupancy/daytype")
def occupancy_daytype(request: Request, basis: Literal["auto", "sites", "people"] = "auto", by: Literal["day_type", "school_holiday", "dow"] = "day_type"):
    f = parse_filters(request); w, p = occ_where(f); col = OCC_BASIS[basis]
    w = w.replace("occ_pct IS NOT NULL", f"{col} IS NOT NULL")
    key = {"day_type": "day_type", "school_holiday": "coalesce(school_holiday,'Term time')", "dow": "dayofweek(date)"}[by]
    return q(f"""SELECT {key} AS key, count(*) area_nights, round(avg({col}),1) avg_occ,
        round(100.0*sum(CASE WHEN {col} >= 90 THEN 1 ELSE 0 END)/count(*),1) pct_full FROM area_day WHERE {w} GROUP BY 1 ORDER BY avg_occ DESC""", p)

# ---- plan ahead: future window vs equivalent nights in prior years ---------------------------------
from datetime import timedelta
_cal_cache: dict = {}
def _calendar() -> dict:
    """date -> calendar row, loaded once."""
    if not _cal_cache:
        for r in q("SELECT * FROM calendar"):
            _cal_cache[r["date"]] = r
    return _cal_cache

def _holiday_periods() -> list[dict]:
    rows = q("""WITH x AS (SELECT date, school_holiday, date - to_days(row_number() OVER (PARTITION BY school_holiday ORDER BY date)::INTEGER) AS grp
                           FROM calendar WHERE is_school_holiday)
                SELECT school_holiday AS name, min(date) AS start, max(date) AS "end" FROM x GROUP BY school_holiday, grp ORDER BY start""")
    return rows

def _match_prior(d: date, years_back: int, cal: dict, periods: list[dict]) -> list[tuple[int, date, str]]:
    """For target day d, return [(years_ago, matched_date, rule)] for 1..years_back."""
    out = []
    row = cal.get(d)
    period = next((p for p in periods if p["start"] <= d <= p["end"]), None)
    for n in range(1, years_back + 1):
        m, rule = None, None
        # 1. same named public holiday (Good Friday, Easter Monday, King's Birthday...) - the strongest anchor
        if row and row.get("is_public_holiday"):
            name = row["public_holiday"]
            for dd in sorted(range(-36, 37), key=abs):
                c = date(d.year - n, d.month, min(d.day, 28)) + timedelta(days=dd)
                if cal.get(c, {}).get("public_holiday") == name:
                    m, rule = c, name; break
        # 2. a day of a long weekend: keep position relative to its public holiday
        if m is None and row and row.get("is_long_weekend") and not row.get("is_public_holiday"):
            for off in (1, 2, 3, -1, -2, -3):
                r2 = cal.get(d + timedelta(days=off), {})
                if r2.get("is_public_holiday"):
                    name = r2["public_holiday"]
                    for dd in sorted(range(-36, 37), key=abs):
                        c = date(d.year - n, d.month, min(d.day, 28)) + timedelta(days=dd)
                        if cal.get(c, {}).get("public_holiday") == name:
                            m, rule = c - timedelta(days=off), f"{name} long weekend"; break
                    if m: break
        # 3. same offset into the same named school-holiday period n years earlier
        if m is None and period:
            k = (d - period["start"]).days
            cands = [p for p in periods if p["name"] == period["name"] and p["start"].year == period["start"].year - n]
            if cands:
                m, rule = cands[0]["start"] + timedelta(days=k), f"{period['name']} holidays day {k+1}"
        # 4. same weekday nearest the anniversary
        if m is None:
            anniv = date(d.year - n, d.month, min(d.day, 28))
            shift = (d.weekday() - anniv.weekday())
            if shift > 3: shift -= 7
            if shift < -3: shift += 7
            m, rule = anniv + timedelta(days=shift), "same weekday"
        out.append((n, m, rule))
    return out

@app.get("/api/plan")
def plan(request: Request, arrival: date, departure: date, years: int = Query(3, ge=1, le=10), basis: Literal["auto", "sites", "people"] = "auto"):
    """Future stay vs equivalent nights in prior years, for the park/area in the filters."""
    f = parse_filters(request)
    if departure <= arrival: raise HTTPException(400, "departure must be after arrival")
    if (departure - arrival).days > 60: raise HTTPException(400, "window limited to 60 nights")
    cal, periods = _calendar(), _holiday_periods()
    col = OCC_BASIS[basis]
    nights = [arrival + timedelta(days=i) for i in range((departure - arrival).days)]
    matches = {d: _match_prior(d, years, cal, periods) for d in nights}
    all_dates = sorted({m for ms in matches.values() for _, m, _ in ms})
    # area-level occupancy for the matched dates, honouring park/area/q filters
    w, p = ["date IN (" + ",".join("?" * len(all_dates)) + ")"], list(all_dates)
    for colname, vals in (("park", f.park), ("area", f.area)):
        if vals: w.append(f"{colname} IN ({','.join('?'*len(vals))})"); p.extend(vals)
    if f.q: w.append("(park ILIKE ? OR area ILIKE ?)"); p += [f"%{f.q}%", f"%{f.q}%"]
    hist = q(f"""SELECT date, count(*) areas_with_guests, round(avg({col}),1) occ, max({col}) occ_max, sum(bookings_in_house) bookings_in_house, sum(people_in_house) people_in_house,
                 sum(CASE WHEN {col} >= 90 THEN 1 ELSE 0 END) areas_full FROM area_day WHERE {' AND '.join(w)} GROUP BY date""", p)
    hist = {r["date"]: r for r in hist}
    data_end = q("SELECT max(arrival_date) d FROM camping")[0]["d"]
    for d in nights:  # matched dates beyond the data -> no data (not zero)
        matches[d] = [(n, m, rule) for n, m, rule in matches[d] if m <= data_end]
    # capacity of the selection (sum over selected areas)
    cw, cp = ["1=1"], []
    for colname, vals in (("park", f.park), ("area", f.area)):
        if vals: cw.append(f"{colname} IN ({','.join('?'*len(vals))})"); cp.extend(vals)
    cap = q(f"SELECT count(*) areas, sum(sites_cap) sites_cap, sum(people_cap) people_cap, count(capacity_type) areas_with_cap FROM areas WHERE {' AND '.join(cw)}", cp)[0]
    out_nights = []
    for d in nights:
        c = cal.get(d, {})
        prior = []
        for n, m, rule in matches[d]:
            h = hist.get(m)
            prior.append({"years_ago": n, "date": m, "rule": rule, "day_type": cal.get(m, {}).get("day_type"),
                          "occ": h["occ"] if h else None, "occ_max": h["occ_max"] if h else None,
                          "bookings_in_house": h["bookings_in_house"] if h else 0, "people_in_house": h["people_in_house"] if h else 0,
                          "areas_full": h["areas_full"] if h else 0, "areas_with_guests": h["areas_with_guests"] if h else 0})
        occs = [x["occ"] for x in prior if x["occ"] is not None]
        out_nights.append({"date": d, "dow": d.strftime("%a"), "day_type": c.get("day_type"), "school_holiday": c.get("school_holiday"),
                           "public_holiday": c.get("public_holiday"), "is_long_weekend": c.get("is_long_weekend"),
                           "avg_occ": round(sum(occs) / len(occs), 1) if occs else None, "max_occ": max(occs) if occs else None,
                           "years_full": sum(1 for x in prior if (x["occ"] or 0) >= 90), "prior": prior})
    per_year = []
    for n in range(1, years + 1):
        rows = [x for d in nights for x in matches[d] if x[0] == n]
        hs = [hist.get(m) for _, m, _ in rows]
        occs = [h["occ"] for h in hs if h and h["occ"] is not None]
        per_year.append({"years_ago": n, "year": arrival.year - n, "nights": len(rows), "avg_occ": round(sum(occs)/len(occs), 1) if occs else None,
                         "nights_full": sum(1 for h in hs if h and (h["occ"] or 0) >= 90),
                         "avg_people_in_house": round(sum(h["people_in_house"] for h in hs if h) / max(1, len(rows)), 1),
                         "avg_bookings_in_house": round(sum(h["bookings_in_house"] for h in hs if h) / max(1, len(rows)), 1)})
    return {"arrival": arrival, "departure": departure, "nights": out_nights, "per_year": [y for y in per_year if y["nights"]], "capacity": cap,
            "data_end": data_end, "selection": {"park": f.park, "area": f.area}}

@app.get("/api/occupancy/inhouse")
def occupancy_inhouse(request: Request, grain: Literal["day", "week", "month", "quarter", "year"] = "month"):
    """Nightly in-house bookings/people for the selected area(s), aggregated per period (avg + max), with capacity."""
    f = parse_filters(request); w, p = occ_where(f)
    w = w.replace("occ_pct IS NOT NULL", "1=1")
    rows = q(f"""SELECT date_trunc('{grain}', date) AS period, round(avg(bookings_in_house),1) avg_bookings, max(bookings_in_house) max_bookings,
                 round(avg(people_in_house),1) avg_people, max(people_in_house) max_people FROM area_day WHERE {w} GROUP BY 1 ORDER BY 1""", p)
    cw, cp = ["1=1"], []
    for colname, vals in (("park", f.park), ("area", f.area)):
        if vals: cw.append(f"{colname} IN ({','.join('?'*len(vals))})"); cp.extend(vals)
    cap = q(f"SELECT any_value(capacity_type) capacity_type, sum(sites_cap) sites_cap, sum(people_cap) people_cap, count(*) areas FROM areas WHERE {' AND '.join(cw)}", cp)[0]
    return {"rows": rows, "capacity": cap}

@app.get("/api/map/areas")
def map_areas(request: Request, metric: str = None):
    f = parse_filters(request); f.dataset = "camping"; w, p = f.where()
    m = measure(f, metric or "bookings")
    rows = q(f"""SELECT park, area, any_value(lat) lat, any_value(lon) lon, count(*) bookings, sum(nights) nights, sum(person_nights) person_nights,
        round(avg(nights),2) avg_nights, {m} AS value
        FROM camping_x WHERE {w} AND lat IS NOT NULL GROUP BY park, area ORDER BY value DESC""", p)
    caps = {(r["park"], r["area"]): r for r in q("SELECT park, area, capacity_type, sites_cap, people_cap, max_party FROM areas WHERE capacity_type IS NOT NULL")}
    for r in rows:
        c = caps.get((r["park"], r["area"]))
        if c: r.update(capacity_type=c["capacity_type"], sites_cap=c["sites_cap"], people_cap=c["people_cap"], max_party=c["max_party"])
    return rows

@app.get("/api/map/origins")
def map_origins(request: Request, metric: str = None, min_n: int = 1):
    f = parse_filters(request); w, p = f.where()
    m = measure(f, metric or list(MEASURES[f.dataset])[0])
    return q(f"""SELECT postcode, any_value(o_locality) locality, any_value(state) state, any_value(o_lat) lat, any_value(o_lon) lon,
        count(*) n, {m} AS value FROM {f.table} WHERE {w} AND o_lat IS NOT NULL GROUP BY postcode HAVING count(*) >= {int(min_n)} ORDER BY value DESC""", p)

@app.get("/api/map/flows")
def map_flows(request: Request, top: int = Query(200, le=2000)):
    """postcode -> camping area flows (camping only). Filter by park/area to make it meaningful."""
    f = parse_filters(request); f.dataset = "camping"; w, p = f.where()
    return q(f"""SELECT postcode, any_value(o_locality) locality, any_value(o_lat) o_lat, any_value(o_lon) o_lon,
        park, area, any_value(lat) lat, any_value(lon) lon, count(*) n
        FROM camping_x WHERE {w} AND lat IS NOT NULL AND o_lat IS NOT NULL
        GROUP BY postcode, park, area ORDER BY n DESC LIMIT {top}""", p)

TABLE_COLS = {
    "camping": ["row_id", "booking_id", "booking_date", "arrival_date", "departure_date", "nights", "adults", "children", "infants", "people",
                "park", "area", "area_raw", "postcode", "o_locality", "state", "lead_days", "day_type", "school_holiday", "public_holiday", "sh_nights", "era", "source_file"],
    "vehicle": ["row_id", "permit_id", "booking_date", "start_date", "area", "duration", "permit_class", "days_valid", "product_raw",
                "postcode", "o_locality", "state", "lead_days", "day_type", "school_holiday", "public_holiday", "era", "source_file"],
}

@app.get("/api/table")
def table(request: Request, sort: Optional[str] = None, desc: bool = True, limit: int = Query(100, le=1000), offset: int = 0):
    f = parse_filters(request); w, p = f.where()
    cols = TABLE_COLS[f.dataset]
    s = sort if sort in cols else f.date_col
    rows = q(f"SELECT {', '.join(cols)} FROM {f.table} WHERE {w} ORDER BY {s} {'DESC' if desc else 'ASC'} NULLS LAST LIMIT {limit} OFFSET {offset}", p)
    total = q(f"SELECT count(*) n FROM {f.table} WHERE {w}", p)[0]["n"]
    return {"total": total, "rows": rows, "columns": cols}

@app.get("/api/export.csv")
def export_csv(request: Request, limit: int = Query(500_000, le=2_000_000)):
    f = parse_filters(request); w, p = f.where()
    cols = TABLE_COLS[f.dataset]
    def gen():
        buf = io.StringIO(); wr = csv.writer(buf)
        wr.writerow(cols); yield buf.getvalue(); buf.seek(0); buf.truncate()
        with _lock:
            cur = _db.cursor()
            cur.execute(f"SELECT {', '.join(cols)} FROM {f.table} WHERE {w} ORDER BY {f.date_col} LIMIT {limit}", p)
            while True:
                chunk = cur.fetchmany(20_000)
                if not chunk: break
                wr.writerows(chunk); yield buf.getvalue(); buf.seek(0); buf.truncate()
            cur.close()
    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="qpws_{f.dataset}_export.csv"'})

@app.get("/api/search")
def search(term: str = Query(..., min_length=2), limit: int = 15):
    like = f"%{term}%"
    parks = q("SELECT 'park' kind, park label, NULL sub, count(*) n FROM camping WHERE park ILIKE ? GROUP BY 1,2,3 ORDER BY n DESC LIMIT ?", [like, limit])
    areas = q("SELECT 'area' kind, area label, park sub, count(*) n FROM camping WHERE area ILIKE ? GROUP BY 1,2,3 ORDER BY n DESC LIMIT ?", [like, limit])
    pcs = q("SELECT 'postcode' kind, CAST(postcode AS VARCHAR) label, locality || ', ' || state sub, 0 n FROM postcodes WHERE CAST(postcode AS VARCHAR) LIKE ? OR locality ILIKE ? LIMIT ?", [f"{term}%", like, limit])
    return parks + areas + pcs

@app.get("/api/areas")
def areas_dim():
    return q("SELECT * FROM areas ORDER BY bookings DESC")

@app.get("/api/health")
def health():
    return {"ok": True, "camping_rows": q("SELECT count(*) n FROM camping")[0]["n"], "vehicle_rows": q("SELECT count(*) n FROM vehicle")[0]["n"]}

# --------------------------------------------------------------------------- static
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import FileResponse
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")
