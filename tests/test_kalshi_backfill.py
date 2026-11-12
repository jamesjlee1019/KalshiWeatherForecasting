"""Offline tests for scripts/kalshi_backfill.py. No network calls."""
import csv
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import kalshi_backfill as kb

CHICAGO = ZoneInfo("America/Chicago")


def test_parse_event_date():
    assert kb.parse_event_date("KXHIGHCHI-26NOV10") == date(2026, 11, 10)
    assert kb.parse_event_date("KXHIGHCHI-26NOV10-B54.5") == date(2026, 11, 10)
    assert kb.parse_event_date("not-a-ticker") is None


@pytest.mark.parametrize("strike_type, floor, cap, expected", [
    ("between", 54, 55, (54, 55)),
    ("greater", 55, None, (56, None)),        # strictly above 55 -> 56 or more
    ("greater_or_equal", 55, None, (55, None)),
    ("less", None, 54, (None, 53)),           # strictly below 54 -> 53 or less
    ("less_or_equal", None, 54, (None, 54)),
])
def test_bracket_bounds_are_inclusive_whole_degrees(strike_type, floor, cap, expected):
    assert kb.bracket_bounds(strike_type, floor, cap) == expected


def test_decision_time_follows_daylight_saving():
    eight_am = kb.parse_decision("0@08:00")
    summer = kb.decision_datetime(date(2026, 7, 4), eight_am, CHICAGO)
    winter = kb.decision_datetime(date(2026, 1, 15), eight_am, CHICAGO)
    assert summer.astimezone(timezone.utc).hour == 13  # CDT is UTC-5
    assert winter.astimezone(timezone.utc).hour == 14  # CST is UTC-6


def test_snapshot_never_uses_a_candle_that_ends_after_the_decision():
    decision_ts = 1_000_000
    candles = [{"end_ts": decision_ts - 3600, "tag": "earlier"},
               {"end_ts": decision_ts, "tag": "at decision"},
               {"end_ts": decision_ts + 60, "tag": "future"}]
    assert kb.snapshot_at(candles, decision_ts)["tag"] == "at decision"
    assert kb.snapshot_at(candles, decision_ts - 1)["tag"] == "earlier"
    assert kb.snapshot_at(candles, decision_ts - 7200) is None


def test_implied_probs_left_blank_when_a_bracket_has_no_quote():
    rows = [{"event_ticker": "E", "decision": "d", "mid": 0.5},
            {"event_ticker": "E", "decision": "d", "mid": None}]
    kb.add_implied_probs(rows)
    assert all(r["implied_prob"] is None for r in rows)


class FakeClient:
    """Serves one Chicago day with three brackets. Each market has one candle
    before the decision and one after it with very different prices."""
    calls = 0

    def get(self, path, params=None):
        self.calls += 1
        if path == "/markets":
            def market(ticker, strike_type, floor, cap, result):
                return {"ticker": ticker, "event_ticker": "KXHIGHCHI-26NOV10",
                        "strike_type": strike_type, "floor_strike": floor, "cap_strike": cap,
                        "result": result, "expiration_value": "55",
                        "open_time": "2026-11-08T15:00:00Z",
                        "close_time": "2026-11-11T05:00:00Z"}
            return {"markets": [market("LOW", "less", None, 54, "no"),
                                market("MID", "between", 54, 55, "yes"),
                                market("HIGH", "greater", 55, None, "no")], "cursor": ""}
        if path == "/historical/markets":
            return {"markets": [], "cursor": ""}
        decision_ts = params["end_ts"]
        return {"candlesticks": [
            {"end_period_ts": decision_ts - 1800,
             "yes_bid": {"close_dollars": "0.30"}, "yes_ask": {"close_dollars": "0.34"},
             "price": {"close_dollars": "0.32"}, "open_interest_fp": "100"},
            {"end_period_ts": decision_ts + 3600,
             "yes_bid": {"close_dollars": "0.99"}, "yes_ask": {"close_dollars": "1.00"}},
        ]}


def test_end_to_end_with_fake_client(tmp_path):
    out = tmp_path / "kalshi.csv"
    summary = kb.run(FakeClient(), ["KXHIGHCHI"], [kb.parse_decision("0@08:00")],
                     CHICAGO, out, cache_dir=None)
    rows = list(csv.DictReader(open(out)))

    assert summary["days_without_exactly_one_yes"] == 0
    assert summary["brackets_inconsistent_with_settlement"] == 0
    assert [r["market_ticker"] for r in rows] == ["LOW", "MID", "HIGH"]
    assert all(float(r["mid"]) == pytest.approx(0.32) for r in rows)  # not the later 0.995
    # The CSV rounds to 6 decimals, so the sum can be off by a few millionths.
    assert sum(float(r["implied_prob"]) for r in rows) == pytest.approx(1.0, abs=1e-5)
    assert datetime.fromisoformat(rows[0]["decision_local"]).hour == 8
