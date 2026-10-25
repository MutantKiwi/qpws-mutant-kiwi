"""
Build the day-level calendar dimension: one row per date 2010-01-01 .. 2029-12-31.

  date, year, month, dow, is_weekend,
  public_holiday (name|NULL), is_public_holiday,
  school_holiday (Easter|Winter|Spring|Summer|NULL), is_school_holiday, school_term (1-4|NULL),
  is_long_weekend  (Sat/Sun/public-holiday days that form a run of >= 3 non-working days touching a weekend),
  day_type         one label per day, priority: School holiday > Public holiday > Long weekend > Weekend > Weekday

Public holidays: `holidays` package, AU/QLD (includes Ekka/Royal Queensland Show, Labour Day moves 2013-15, King's/Queen's Birthday).
School holidays: gaps between Queensland state-school terms in lookups/qld_school_terms.csv (sourced from education.qld.gov.au).
"""
import csv
from datetime import date, timedelta
from pathlib import Path

import holidays


def build(terms_csv: Path, start=date(2010, 1, 1), end=date(2029, 12, 31)) -> list[dict]:
    ph = holidays.country_holidays("AU", subdiv="QLD", years=range(start.year, end.year + 1))
    terms = []
    with open(terms_csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            terms.append((int(r["year"]), int(r["term"]), date.fromisoformat(r["start"]), date.fromisoformat(r["end"])))
    terms.sort(key=lambda t: t[2])

    # school holiday = days strictly between consecutive terms; label by the term just finished
    label = {1: "Easter", 2: "Winter", 3: "Spring", 4: "Summer"}
    term_of, hol_of = {}, {}
    for (y, t, s, e) in terms:
        d = s
        while d <= e:
            term_of[d] = t; d += timedelta(days=1)
    for (a, b) in zip(terms, terms[1:]):
        d = a[3] + timedelta(days=1)
        while d < b[2]:
            hol_of[d] = label[a[1]]; d += timedelta(days=1)
    # before the first term / after the last term in the file -> Summer
    first_start, last_end = terms[0][2], terms[-1][3]
    d = start
    while d < first_start:
        hol_of[d] = "Summer"; d += timedelta(days=1)
    d = last_end + timedelta(days=1)
    while d <= end:
        hol_of[d] = "Summer"; d += timedelta(days=1)

    days = []
    d = start
    while d <= end:
        days.append(d); d += timedelta(days=1)
    off = {d: (d.weekday() >= 5 or d in ph) for d in days}

    # long weekend: a maximal run of consecutive non-working days of length >= 3 that includes a Sat or Sun
    long_we = set()
    i = 0
    while i < len(days):
        if off[days[i]]:
            j = i
            while j + 1 < len(days) and off[days[j + 1]]:
                j += 1
            run = days[i:j + 1]
            if len(run) >= 3 and any(x.weekday() >= 5 for x in run):
                long_we.update(run)
            i = j + 1
        else:
            i += 1

    rows = []
    for d in days:
        is_ph = d in ph
        sh = hol_of.get(d)
        we = d.weekday() >= 5
        lw = d in long_we
        day_type = "School holiday" if sh else "Public holiday" if is_ph else "Long weekend" if lw else "Weekend" if we else "Weekday"
        rows.append(dict(date=d.isoformat(), year=d.year, month=d.month, dow=(d.weekday() + 1) % 7,  # 0=Sun like DuckDB dayofweek
                         is_weekend=we, public_holiday=ph.get(d), is_public_holiday=is_ph,
                         school_holiday=sh, is_school_holiday=sh is not None, school_term=term_of.get(d),
                         is_long_weekend=lw, day_type=day_type))
    return rows


def write_csv(rows: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)


if __name__ == "__main__":
    import sys
    here = Path(__file__).resolve().parent.parent
    rows = build(here / "data/raw/lookups/qld_school_terms.csv")
    write_csv(rows, here / "data/work/calendar.csv")
    from collections import Counter
    print(len(rows), "days;", Counter(r["day_type"] for r in rows))
