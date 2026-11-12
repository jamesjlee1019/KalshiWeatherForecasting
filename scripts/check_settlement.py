#!/usr/bin/env python3
"""Compare the temperature each Kalshi day settled on with the NWS daily high.

Kalshi's Chicago daily-high markets used to settle on the NWS Daily Climate Report and
now settle on The Weather Company's reading for the same station. Training uses
decades of NWS highs, so this measures how often the two agree, per settlement source.

For every day in the Kalshi backfill CSV it reports:
  * exact_match: Kalshi's settled temperature equals the NWS high
  * nws_in_yes_bracket: the NWS high falls inside the bracket that settled YES
    (works even when Kalshi's settled temperature is missing)
  * daylight_time: whether the day falls in daylight saving time. The NWS climate day
    uses local standard time, so if The Weather Company uses clock midnight instead,
    mismatches should cluster on these days.

Quick start (from the repo root), after running both backfills:
    python scripts/check_settlement.py

Writes data/settlement_check.csv.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

FIELDS = ["climate_date", "settlement_source", "daylight_time", "kalshi_settled_f",
          "yes_lo_f", "yes_hi_f", "nws_high_f", "exact_match", "nws_in_yes_bracket"]


def to_int(text: Optional[str]) -> Optional[int]:
    if text is None or str(text).strip() == "":
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def load_kalshi(path: Path) -> dict[str, dict]:
    """One entry per climate day from the backfill CSV, which repeats each market once
    per decision time."""
    days: dict[str, dict] = defaultdict(lambda: {"sources": set(), "settled": set(),
                                                 "yes": [], "seen": set()})
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            day = days[row["climate_date"]]
            if row["market_ticker"] in day["seen"]:
                continue
            day["seen"].add(row["market_ticker"])
            if row.get("settlement_source"):
                day["sources"].add(row["settlement_source"])
            settled = to_int(row.get("settled_temp_f"))
            if settled is not None:
                day["settled"].add(settled)
            if row.get("result") == "yes":
                day["yes"].append((to_int(row.get("lo_f")), to_int(row.get("hi_f"))))
    return dict(days)


def load_nws(path: Path) -> dict[str, Optional[int]]:
    with open(path, newline="") as fh:
        return {row["date"]: to_int(row.get("high_f")) for row in csv.DictReader(fh)}


def in_daylight_time(day: str, tz: ZoneInfo) -> bool:
    d = date.fromisoformat(day)
    return bool(datetime(d.year, d.month, d.day, 12, tzinfo=tz).dst())


def compare(kalshi: dict[str, dict], nws: dict[str, Optional[int]], tz: ZoneInfo) -> list[dict]:
    rows = []
    for day in sorted(kalshi):
        info = kalshi[day]
        source = "/".join(sorted(info["sources"])) or "unknown"
        settled = next(iter(info["settled"])) if len(info["settled"]) == 1 else None
        yes = info["yes"][0] if len(info["yes"]) == 1 else (None, None)
        lo, hi = yes
        high = nws.get(day)
        exact = None if settled is None or high is None else settled == high
        in_bracket = (None if high is None or len(info["yes"]) != 1
                      else (lo is None or high >= lo) and (hi is None or high <= hi))
        rows.append({
            "climate_date": day,
            "settlement_source": source,
            "daylight_time": in_daylight_time(day, tz),
            "kalshi_settled_f": settled,
            "yes_lo_f": lo,
            "yes_hi_f": hi,
            "nws_high_f": high,
            "exact_match": exact,
            "nws_in_yes_bracket": in_bracket,
        })
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if row[k] is None else row[k] for k in FIELDS})


def summarize(rows: list[dict]) -> dict:
    by_source: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_source[row["settlement_source"]].append(row)
    summary = {}
    print("\nAgreement between Kalshi settlement and the NWS daily high")
    for source in sorted(by_source):
        group = by_source[source]
        exact = [r for r in group if r["exact_match"] is not None]
        bracket = [r for r in group if r["nws_in_yes_bracket"] is not None]
        misses = [r for r in group if r["exact_match"] is False
                  or (r["exact_match"] is None and r["nws_in_yes_bracket"] is False)]
        stats = {
            "days": len(group),
            "first_day": group[0]["climate_date"],
            "last_day": group[-1]["climate_date"],
            "days_without_nws_high": sum(r["nws_high_f"] is None for r in group),
            "exact_compared": len(exact),
            "exact_matches": sum(r["exact_match"] for r in exact),
            "bracket_compared": len(bracket),
            "nws_in_yes_bracket": sum(r["nws_in_yes_bracket"] for r in bracket),
            "mismatch_days": len(misses),
            "mismatches_in_daylight_time": sum(r["daylight_time"] for r in misses),
        }
        summary[source] = stats
        print(f"\n  {source}")
        for key, value in stats.items():
            print(f"    {key}: {value}")
        for r in misses[:10]:
            print(f"    mismatch {r['climate_date']}: Kalshi {r['kalshi_settled_f']} "
                  f"(YES {r['yes_lo_f']}..{r['yes_hi_f']}), NWS {r['nws_high_f']}"
                  f"{', daylight time' if r['daylight_time'] else ''}")
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kalshi", type=Path, default=Path("data/kalshi_KXHIGHCHI.csv"))
    parser.add_argument("--nws", type=Path, default=Path("data/nws_daily_MDW.csv"))
    parser.add_argument("--out", type=Path, default=Path("data/settlement_check.csv"))
    parser.add_argument("--tz", default="America/Chicago")
    args = parser.parse_args(argv)
    for path in (args.kalshi, args.nws):
        if not path.exists():
            print(f"error: {path} not found; run the backfill scripts first", file=sys.stderr)
            return 1
    rows = compare(load_kalshi(args.kalshi), load_nws(args.nws), ZoneInfo(args.tz))
    write_csv(rows, args.out)
    print(f"Wrote {len(rows)} days to {args.out}")
    summarize(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
