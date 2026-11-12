"""Offline tests for scripts/check_settlement.py."""
import csv
from zoneinfo import ZoneInfo

import check_settlement as cs

KALSHI_FIELDS = ["climate_date", "market_ticker", "settlement_source", "settled_temp_f",
                 "result", "lo_f", "hi_f", "decision"]


def market_rows(day, source, settled, brackets, yes_index):
    """One row per bracket per decision time, like the backfill CSV."""
    rows = []
    for decision in ("D-1@16:00", "D0@08:00"):
        for i, (lo, hi) in enumerate(brackets):
            rows.append({"climate_date": day, "market_ticker": f"{day}-{i}",
                         "settlement_source": source, "settled_temp_f": settled,
                         "result": "yes" if i == yes_index else "no",
                         "lo_f": "" if lo is None else lo, "hi_f": "" if hi is None else hi,
                         "decision": decision})
    return rows


def write(path, fields, rows):
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_compare_and_summary(tmp_path):
    brackets_aug = [(None, 79), (80, 81), (82, 83), (84, None)]
    brackets_nov = [(None, 53), (54, 55), (56, None)]
    kalshi = (market_rows("2026-07-01", "nws", "80", brackets_aug, 1)              # NWS says 82
              + market_rows("2026-11-04", "weather_company", "55.0", brackets_nov, 1)  # match
              + market_rows("2026-11-05", "weather_company", "", brackets_nov, 2))    # no temp
    write(tmp_path / "kalshi.csv", KALSHI_FIELDS, kalshi)
    write(tmp_path / "nws.csv", ["date", "high_f", "low_f"],
          [{"date": "2026-07-01", "high_f": "82", "low_f": "70"},
           {"date": "2026-11-04", "high_f": "55", "low_f": "40"},
           {"date": "2026-11-05", "high_f": "57", "low_f": "41"}])

    rows = cs.compare(cs.load_kalshi(tmp_path / "kalshi.csv"),
                      cs.load_nws(tmp_path / "nws.csv"), ZoneInfo("America/Chicago"))
    by_day = {r["climate_date"]: r for r in rows}

    july = by_day["2026-07-01"]
    assert (july["exact_match"], july["nws_in_yes_bracket"], july["daylight_time"]) == (
        False, False, True)

    nov4 = by_day["2026-11-04"]
    assert (nov4["kalshi_settled_f"], nov4["exact_match"], nov4["nws_in_yes_bracket"]) == (
        55, True, True)
    assert nov4["daylight_time"] is False  # DST ended Nov 1, 2026

    nov5 = by_day["2026-11-05"]  # settled temperature missing: fall back to the bracket
    assert nov5["exact_match"] is None and nov5["nws_in_yes_bracket"] is True

    summary = cs.summarize(rows)
    assert summary["nws"]["mismatch_days"] == 1
    assert summary["nws"]["mismatches_in_daylight_time"] == 1
    assert summary["weather_company"]["exact_matches"] == 1
    assert summary["weather_company"]["nws_in_yes_bracket"] == 2
    assert summary["weather_company"]["mismatch_days"] == 0


def test_main_reports_missing_inputs(tmp_path):
    assert cs.main(["--kalshi", str(tmp_path / "nope.csv"),
                    "--nws", str(tmp_path / "nope2.csv")]) == 1
