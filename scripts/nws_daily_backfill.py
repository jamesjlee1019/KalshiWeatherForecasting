#!/usr/bin/env python3
"""Download official NWS daily high and low temperatures for one station from RCC-ACIS.

ACIS (the Regional Climate Centers' Applied Climate Information System) serves the
NWS's quality-controlled daily climate records. For first-order stations like
Chicago Midway these are the same midnight-to-midnight local-standard-time days as
the NWS Daily Climate Report. Used here for climatology and as training labels.

Quick start (from the repo root):
    python scripts/nws_daily_backfill.py                       # Midway, 1991 to yesterday
    python scripts/nws_daily_backfill.py --sid ORD --start 1991-01-01

Writes data/nws_daily_<SID>.csv with columns: date, high_f, low_f.
No API key is needed.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import requests

ACIS_URL = "https://data.rcc-acis.org/StnData"
FIELDS = ["date", "high_f", "low_f"]


class AcisError(RuntimeError):
    pass


def parse_temp(value: Any) -> Optional[int]:
    """ACIS returns values as strings, with "M" for missing."""
    if value is None:
        return None
    try:
        return int(round(float(str(value).strip())))
    except ValueError:
        return None


def fetch(sid: str, start: date, end: date, url: str = ACIS_URL,
          session: Optional[Any] = None, timeout: float = 120.0) -> dict:
    """One request covers decades of daily data for one station."""
    params = {
        "sid": sid,
        "sdate": start.isoformat(),
        "edate": end.isoformat(),
        "elems": [{"name": "maxt"}, {"name": "mint"}],
        "meta": ["name", "sids", "state"],
    }
    http = session or requests
    resp = http.post(url, json=params, timeout=timeout)
    if resp.status_code != 200:
        raise AcisError(f"ACIS returned {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if "error" in data:  # ACIS reports bad requests (e.g. unknown station) this way
        raise AcisError(f"ACIS error for sid={sid!r}: {data['error']}")
    return data


def parse_rows(response: dict) -> list[dict]:
    rows = []
    for record in response.get("data") or []:
        day, high, low = (list(record) + [None, None, None])[:3]
        rows.append({"date": date.fromisoformat(day),
                     "high_f": parse_temp(high), "low_f": parse_temp(low)})
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: r["date"]):
            writer.writerow({"date": row["date"].isoformat(),
                             "high_f": "" if row["high_f"] is None else row["high_f"],
                             "low_f": "" if row["low_f"] is None else row["low_f"]})


def summarize(response: dict, rows: list[dict], sid: str) -> dict:
    meta = response.get("meta") or {}
    with_high = [r["date"] for r in rows if r["high_f"] is not None]
    missing_by_year = Counter(r["date"].year for r in rows if r["high_f"] is None)
    days_by_year = Counter(r["date"].year for r in rows)
    gappy_years = sorted(y for y, n in missing_by_year.items() if n > 0.05 * days_by_year[y])
    summary = {
        "station": meta.get("name"),
        "station_ids": meta.get("sids"),
        "days": len(rows),
        "days_with_high": len(with_high),
        "first_day_with_high": min(with_high).isoformat() if with_high else None,
        "last_day_with_high": max(with_high).isoformat() if with_high else None,
        "days_missing_high": len(rows) - len(with_high),
        "years_missing_over_5pct": gappy_years,
    }
    print("\nSummary")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    if sid.upper() in ("MDW", "KMDW") and "MIDWAY" not in str(meta.get("name", "")).upper():
        print("  warning: station name doesn't mention Midway; check --sid", file=sys.stderr)
    return summary


def build_parser() -> argparse.ArgumentParser:
    yesterday = date.today() - timedelta(days=1)
    parser = argparse.ArgumentParser(description="Download NWS daily highs and lows from ACIS.")
    parser.add_argument("--sid", default="MDW",
                        help="station id, e.g. MDW (Chicago Midway, default) or ORD")
    parser.add_argument("--start", type=date.fromisoformat, default=date(1991, 1, 1),
                        help="first day, YYYY-MM-DD (default 1991-01-01)")
    parser.add_argument("--end", type=date.fromisoformat, default=yesterday,
                        help="last day, YYYY-MM-DD (default yesterday)")
    parser.add_argument("--out", type=Path, help="output CSV (default data/nws_daily_<SID>.csv)")
    parser.add_argument("--url", default=ACIS_URL)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out = args.out or Path(f"data/nws_daily_{args.sid.upper()}.csv")
    try:
        response = fetch(args.sid, args.start, args.end, args.url)
    except (AcisError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    rows = parse_rows(response)
    write_csv(rows, out)
    print(f"Wrote {len(rows)} days to {out}")
    summary = summarize(response, rows, args.sid)
    return 0 if summary["days_with_high"] else 2


if __name__ == "__main__":
    sys.exit(main())
