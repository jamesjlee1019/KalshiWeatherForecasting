#!/usr/bin/env python3
"""Backfill Kalshi daily-high temperature market prices at fixed decision times.

For every settled market (one temperature bracket on one day) in a series such as
KXHIGHNY, this writes one CSV row per decision time with:

  * the bracket as inclusive whole-degree bounds lo_f..hi_f
  * how it settled: result (yes/no) and the settled temperature
  * the best YES bid/ask at the decision time, taken from the last candlestick that
    ended at or before that moment (never a later one, so no look-ahead)
  * a market-implied probability: each bracket's mid divided by the sum of that
    day's mids, so the day's brackets add up to 1

Kalshi keeps roughly the last few months of markets on its regular endpoints and
moves older ones to /historical endpoints with slightly different field names.
This script queries both. Market data doesn't need an API key.

Quick start (from the repo root):
    pip install -r requirements.txt
    python scripts/kalshi_backfill.py --series KXHIGHCHI --limit-days 7          # smoke test
    python scripts/kalshi_backfill.py --series KXHIGHCHI --decision 0@08:00 -1@16:00
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

import requests

DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

# Decision times are local clock time in the market's city. Add cities as you use them.
SERIES_TZ = {
    "KXHIGHNY": "America/New_York",  # settles on Central Park
    "KXHIGHCHI": "America/Chicago",  # settles on Chicago Midway
}

FIELDS = [
    "series_ticker", "event_ticker", "market_ticker", "climate_date",
    "bracket", "strike_type", "floor_strike", "cap_strike", "lo_f", "hi_f",
    "result", "settled_temp_f", "settles_consistently",
    "decision", "decision_local", "decision_utc", "market_state",
    "snapshot_end_utc", "staleness_min",
    "yes_bid", "yes_ask", "spread", "mid", "last_price", "open_interest",
    "n_brackets", "mid_sum", "implied_prob", "source",
]

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
_EVENT_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(?=-|$)")
_DECISION_RE = re.compile(r"^(-?\d+)@(\d{1,2}):(\d{2})$")


class KalshiError(RuntimeError):
    pass


class NotFound(KalshiError):
    pass


# --------------------------------------------------------------------------- client

class KalshiClient:
    """Minimal read-only client with client-side throttling and retries.

    Kalshi's 429 responses carry no Retry-After header, so this backs off
    exponentially on 429 and 5xx responses.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, max_rps: float = 8.0,
                 max_retries: int = 6, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        self._last_request = 0.0
        self.calls = 0

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        url = self.base_url + path
        error: Exception = KalshiError(f"no attempt made for {url}")
        for attempt in range(self.max_retries + 1):
            pause = self.min_interval - (time.monotonic() - self._last_request)
            if pause > 0:
                time.sleep(pause)
            self._last_request = time.monotonic()
            self.calls += 1
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                error = exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 404:
                    raise NotFound(f"404 for {resp.url}")
                error = KalshiError(f"{resp.status_code} for {resp.url}: {resp.text[:200]}")
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise error
            if attempt < self.max_retries:
                time.sleep(min(60.0, 2.0 ** attempt))
        raise error


# --------------------------------------------------------------------------- parsing

def parse_event_date(event_ticker: str) -> Optional[date]:
    """KXHIGHNY-26JUN10 -> 2026-06-10, the climate day the event settles on."""
    match = _EVENT_DATE_RE.search(event_ticker or "")
    if not match:
        return None
    yy, mon, dd = match.groups()
    if mon not in _MONTHS:
        return None
    try:
        return date(2000 + int(yy), _MONTHS[mon], int(dd))
    except ValueError:
        return None


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_number(value: Any) -> Optional[float]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_price(value: Any) -> Optional[float]:
    """Prices are fixed-point dollar strings ("0.5600"). Older payloads used integer cents."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value / 100.0
    return parse_number(value)


def bracket_bounds(strike_type: Optional[str], floor_strike: Any,
                   cap_strike: Any) -> tuple[Optional[int], Optional[int]]:
    """Inclusive whole-degree range that settles YES; None means open-ended.

    Settled temperatures are whole degrees F, so a strict ">81" becomes lo=82 and
    "<80" becomes hi=79.
    """
    kind = (strike_type or "").lower()
    low, high = parse_number(floor_strike), parse_number(cap_strike)
    if kind == "between" and low is not None and high is not None:
        return math.ceil(low), math.floor(high)
    strike = low if low is not None else high
    if strike is not None and kind == "greater":
        return math.floor(strike) + 1, None
    if strike is not None and kind == "greater_or_equal":
        return math.ceil(strike), None
    strike = high if high is not None else low
    if strike is not None and kind == "less":
        return None, math.ceil(strike) - 1
    if strike is not None and kind == "less_or_equal":
        return None, math.floor(strike)
    raise ValueError(f"unsupported bracket: strike_type={strike_type!r} "
                     f"floor={floor_strike!r} cap={cap_strike!r}")


def bracket_contains(lo: Optional[int], hi: Optional[int],
                     temp: Optional[float]) -> Optional[bool]:
    if temp is None:
        return None
    return (lo is None or temp >= lo) and (hi is None or temp <= hi)


# --------------------------------------------------------------------------- decision times

@dataclass(frozen=True)
class Decision:
    label: str       # e.g. "0@08:00"
    day_offset: int  # days relative to the market's climate day
    hour: int
    minute: int


def parse_decision(text: str) -> Decision:
    match = _DECISION_RE.match(text.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"bad decision time {text!r}: use DAY_OFFSET@HH:MM, e.g. 0@08:00 or -1@16:00")
    offset, hour, minute = (int(g) for g in match.groups())
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise argparse.ArgumentTypeError(f"bad clock time in {text!r}")
    return Decision(text.strip(), offset, hour, minute)


def decision_datetime(climate_day: date, decision: Decision, tz: ZoneInfo) -> datetime:
    """Local clock time in the market's city; zoneinfo handles daylight saving time."""
    day = climate_day + timedelta(days=decision.day_offset)
    return datetime(day.year, day.month, day.day, decision.hour, decision.minute, tzinfo=tz)


# --------------------------------------------------------------------------- candles

def normalize_candle(raw: dict) -> dict:
    """Map the live shape (close_dollars, open_interest_fp) and the historical shape
    (close, open_interest) onto the same keys."""
    def pick(obj: Any, *keys: str) -> Any:
        if isinstance(obj, dict):
            for key in keys:
                if obj.get(key) is not None:
                    return obj[key]
        return None

    price = raw.get("price") or {}
    last = pick(price, "close_dollars", "close")
    if last is None:  # no trade in this period: use the last trade before it
        last = pick(price, "previous_dollars", "previous")
    return {
        "end_ts": int(raw["end_period_ts"]),
        "yes_bid": parse_price(pick(raw.get("yes_bid"), "close_dollars", "close")),
        "yes_ask": parse_price(pick(raw.get("yes_ask"), "close_dollars", "close")),
        "last_price": parse_price(last),
        "open_interest": parse_number(pick(raw, "open_interest_fp", "open_interest")),
    }


def snapshot_at(candles: list[dict], ts: int) -> Optional[dict]:
    """Latest candle that ended at or before `ts`. A candle ending later contains prices
    from after the decision, so it is never used."""
    eligible = [c for c in candles if c["end_ts"] <= ts]
    return max(eligible, key=lambda c: c["end_ts"]) if eligible else None


def usable_mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """Midpoint of the YES quote, or None if a side is missing or the book is empty (0/1)."""
    if bid is None or ask is None or ask < bid:
        return None
    if bid <= 0.0 and ask >= 1.0:
        return None
    return (bid + ask) / 2.0


# --------------------------------------------------------------------------- fetching

def iter_markets(client, path: str, params: dict) -> Iterator[dict]:
    params = dict(params)
    while True:
        page = client.get(path, params)
        yield from page.get("markets") or []
        cursor = page.get("cursor")
        if not cursor or cursor == params.get("cursor"):
            return
        params["cursor"] = cursor


def list_settled_markets(client, series_ticker: str) -> list[dict]:
    """Settled markets from the live and historical tiers, tagged with where they came from."""
    found: dict[str, dict] = {}
    for m in iter_markets(client, "/markets",
                          {"series_ticker": series_ticker, "status": "settled", "limit": 1000}):
        found[m["ticker"]] = {**m, "_series": series_ticker, "_source": "live"}
    for m in iter_markets(client, "/historical/markets",
                          {"series_ticker": series_ticker, "limit": 1000}):
        found.setdefault(m["ticker"], {**m, "_series": series_ticker, "_source": "historical"})
    return list(found.values())


def fetch_candles(client, market: dict, start_ts: int, end_ts: int, period: int,
                  cache_dir: Optional[Path]) -> list[dict]:
    ticker = market["ticker"]
    cache_file = None
    if cache_dir is not None:
        cache_file = cache_dir / f"candles_p{period}" / f"{ticker}_{start_ts}_{end_ts}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period}
    live = f"/series/{market['_series']}/markets/{ticker}/candlesticks"
    historical = f"/historical/markets/{ticker}/candlesticks"
    # A market can move from the live tier to the archive between listing and this call.
    first, second = (live, historical) if market.get("_source") == "live" else (historical, live)
    try:
        data = client.get(first, params)
    except NotFound:
        data = client.get(second, params)
    candles = data.get("candlesticks") or []
    if cache_file is not None:  # settled markets never change, so reruns can skip them
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(candles))
    return candles


# --------------------------------------------------------------------------- building rows

def build_rows(client, markets: list[dict], decisions: list[Decision], tz: ZoneInfo,
               period: int = 60, lookback_hours: int = 24, cache_dir: Optional[Path] = None,
               start: Optional[date] = None, end: Optional[date] = None,
               limit_days: Optional[int] = None) -> tuple[list[dict], list[dict], dict]:
    stats: dict[str, int] = defaultdict(int)
    todo = []
    for m in markets:
        day = parse_event_date(m.get("event_ticker", ""))
        if day is None:
            stats["skipped_unparsed_date"] += 1
        elif (start is None or day >= start) and (end is None or day <= end):
            todo.append((day, m))
    todo.sort(key=lambda item: (item[0], item[1]["ticker"]))
    if limit_days:
        keep = set(sorted({day for day, _ in todo})[-limit_days:])
        todo = [(day, m) for day, m in todo if day in keep]

    rows = []
    for i, (day, m) in enumerate(todo, 1):
        moments = [(d, decision_datetime(day, d, tz)) for d in decisions]
        stamps = [int(dt.timestamp()) for _, dt in moments]
        try:
            raw = fetch_candles(client, m, min(stamps) - lookback_hours * 3600, max(stamps),
                                period, cache_dir)
            candles = [normalize_candle(c) for c in raw]
        except KalshiError as exc:
            stats["candle_errors"] += 1
            print(f"warning: no candles for {m['ticker']}: {exc}", file=sys.stderr)
            candles = []

        try:
            lo, hi = bracket_bounds(m.get("strike_type"), m.get("floor_strike"),
                                    m.get("cap_strike"))
        except ValueError:
            stats["unparsed_brackets"] += 1
            lo = hi = None
        settled = parse_number(m.get("expiration_value"))
        result = m.get("result") or None
        contains = bracket_contains(lo, hi, settled) if (lo, hi) != (None, None) else None
        consistent = (None if contains is None or result not in ("yes", "no")
                      else contains == (result == "yes"))
        opened, closed = parse_iso(m.get("open_time")), parse_iso(m.get("close_time"))

        for (decision, local_dt), ts in zip(moments, stamps):
            utc_dt = local_dt.astimezone(timezone.utc)
            if opened and utc_dt < opened:
                state = "not_open"
            elif closed and utc_dt > closed:
                state = "closed"
            else:
                state = "open"
            snap = snapshot_at(candles, ts) if state == "open" else None
            bid = snap["yes_bid"] if snap else None
            ask = snap["yes_ask"] if snap else None
            rows.append({
                "series_ticker": m["_series"],
                "event_ticker": m.get("event_ticker"),
                "market_ticker": m["ticker"],
                "climate_date": day.isoformat(),
                "bracket": m.get("yes_sub_title") or m.get("subtitle"),
                "strike_type": m.get("strike_type"),
                "floor_strike": m.get("floor_strike"),
                "cap_strike": m.get("cap_strike"),
                "lo_f": lo,
                "hi_f": hi,
                "result": result,
                "settled_temp_f": settled,
                "settles_consistently": consistent,
                "decision": decision.label,
                "decision_local": local_dt.isoformat(),
                "decision_utc": utc_dt.isoformat(),
                "market_state": state,
                "snapshot_end_utc": (datetime.fromtimestamp(snap["end_ts"], timezone.utc)
                                     .isoformat() if snap else None),
                "staleness_min": (ts - snap["end_ts"]) // 60 if snap else None,
                "yes_bid": bid,
                "yes_ask": ask,
                "spread": ask - bid if bid is not None and ask is not None else None,
                "mid": usable_mid(bid, ask),
                "last_price": snap["last_price"] if snap else None,
                "open_interest": snap["open_interest"] if snap else None,
                "source": m.get("_source"),
            })
        if i % 250 == 0:
            print(f"  {i}/{len(todo)} markets", file=sys.stderr)
    return rows, [m for _, m in todo], dict(stats)


def add_implied_probs(rows: list[dict]) -> None:
    """Normalize each day's bracket mids so they sum to 1. Left blank when any bracket
    that day has no usable quote, because a partial normalization would mislead."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["event_ticker"], row["decision"])].append(row)
    for group in groups.values():
        mids = [r.get("mid") for r in group]
        total = sum(x for x in mids if x is not None)
        complete = all(x is not None for x in mids) and total > 0
        for r in group:
            r["n_brackets"] = len(group)
            r["mid_sum"] = total if total > 0 else None
            r["implied_prob"] = r["mid"] / total if complete else None


def write_csv(rows: list[dict], path: Path) -> None:
    def cell(value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, float):
            return round(value, 6)
        return value

    rows.sort(key=lambda r: (r["climate_date"], r["decision"], r["event_ticker"] or "",
                             -math.inf if r["lo_f"] is None else r["lo_f"],
                             r["market_ticker"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: cell(row.get(k)) for k in FIELDS})


def summarize(markets: list[dict], rows: list[dict], stats: dict,
              calls: Optional[int] = None) -> dict:
    by_event: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        by_event[m.get("event_ticker")].append(m)
    odd_days = sorted(e for e, ms in by_event.items()
                      if sum(m.get("result") == "yes" for m in ms) != 1)
    inconsistent = sorted({r["market_ticker"] for r in rows
                           if r.get("settles_consistently") is False})
    groups = {(r["event_ticker"], r["decision"]) for r in rows}
    full = {(r["event_ticker"], r["decision"]) for r in rows if r.get("implied_prob") is not None}
    summary = {
        "markets": len(markets),
        "markets_live_tier": sum(m.get("_source") == "live" for m in markets),
        "markets_historical_tier": sum(m.get("_source") == "historical" for m in markets),
        "days": len(by_event),
        "days_without_exactly_one_yes": len(odd_days),
        "brackets_inconsistent_with_settlement": len(inconsistent),
        "rows": len(rows),
        "rows_with_usable_quote": sum(r.get("mid") is not None for r in rows),
        "day_decisions_with_full_probabilities": len(full),
        "day_decisions_total": len(groups),
        **stats,
    }
    if calls is not None:
        summary["api_calls"] = calls
    print("\nSummary")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    if odd_days:
        print(f"  days to inspect (not exactly one YES bracket): {', '.join(odd_days[:5])}")
    if inconsistent:
        print(f"  brackets to inspect (bounds disagree with result): {', '.join(inconsistent[:5])}")
    rule = next((m.get("rules_primary") for m in markets if m.get("rules_primary")), None)
    if rule:
        print(f"\nSettlement rule of the first market:\n  {rule}")
    return summary


def run(client, series: list[str], decisions: list[Decision], tz: ZoneInfo, out: Path,
        period: int = 60, lookback_hours: int = 24, cache_dir: Optional[Path] = None,
        start: Optional[date] = None, end: Optional[date] = None,
        limit_days: Optional[int] = None) -> dict:
    markets: list[dict] = []
    for s in series:
        found = list_settled_markets(client, s)
        print(f"{s}: {len(found)} settled markets", file=sys.stderr)
        markets.extend(found)
    rows, processed, stats = build_rows(client, markets, decisions, tz, period, lookback_hours,
                                        cache_dir, start, end, limit_days)
    add_implied_probs(rows)
    write_csv(rows, out)
    print(f"\nWrote {len(rows)} rows to {out}")
    return summarize(processed, rows, stats, getattr(client, "calls", None))


# --------------------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill Kalshi daily-high market prices at fixed decision times.")
    parser.add_argument("--series", nargs="+", required=True,
                        help="series ticker(s), e.g. KXHIGHNY")
    parser.add_argument("--decision", nargs="+", type=parse_decision,
                        help="DAY_OFFSET@HH:MM in the city's local time, relative to the "
                             "market's day: 0@08:00 = 8 a.m. that day (default), "
                             "-1@16:00 = 4 p.m. the day before")
    parser.add_argument("--tz", help="IANA time zone for decision times, e.g. America/New_York "
                                     "(required for series not listed in SERIES_TZ)")
    parser.add_argument("--start", type=date.fromisoformat, help="first climate day, YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, help="last climate day, YYYY-MM-DD")
    parser.add_argument("--limit-days", type=int, help="only the most recent N days (quick test)")
    parser.add_argument("--period", type=int, choices=(1, 60), default=60,
                        help="candle size in minutes (default 60)")
    parser.add_argument("--lookback-hours", type=int, default=24,
                        help="how far before a decision to look for the last candle")
    parser.add_argument("--out", type=Path, help="output CSV (default data/kalshi_<SERIES>.csv)")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/raw/kalshi"),
                        help="where raw candle responses are cached")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--max-rps", type=float, default=8.0, help="max requests per second")
    args = parser.parse_args(argv)

    if args.tz:
        tz_name = args.tz
    else:
        names = {SERIES_TZ.get(s) for s in args.series}
        if None in names or len(names) != 1:
            parser.error("pass --tz (e.g. --tz America/New_York) for this series")
        tz_name = names.pop()

    try:
        summary = run(
            KalshiClient(args.base_url, args.max_rps),
            series=args.series,
            decisions=args.decision or [parse_decision("0@08:00")],
            tz=ZoneInfo(tz_name),
            out=args.out or Path(f"data/kalshi_{args.series[0]}.csv"),
            period=args.period,
            lookback_hours=args.lookback_hours,
            cache_dir=None if args.no_cache else args.cache_dir,
            start=args.start,
            end=args.end,
            limit_days=args.limit_days,
        )
    except KalshiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0 if summary["markets"] else 2


if __name__ == "__main__":
    sys.exit(main())
