"""Offline tests for scripts/nws_daily_backfill.py. No network calls."""
import csv
from datetime import date

import pytest

import nws_daily_backfill as nws

RESPONSE = {
    "meta": {"name": "CHICAGO MIDWAY AP", "sids": ["MDW 3", "KMDW 5"], "state": "IL"},
    "data": [["2026-08-14", "88", "70"], ["2026-08-15", "M", "68"], ["2026-08-16", "91", "M"]],
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.sent = payload, status, None

    def post(self, url, json=None, timeout=None):
        self.sent = json
        return FakeResponse(self.payload, self.status)


@pytest.mark.parametrize("raw, expected", [("88", 88), ("88.0", 88), ("-3", -3),
                                           ("M", None), ("", None), (None, None)])
def test_parse_temp(raw, expected):
    assert nws.parse_temp(raw) == expected


def test_fetch_sends_station_dates_and_elements():
    session = FakeSession(RESPONSE)
    nws.fetch("MDW", date(1991, 1, 1), date(2026, 11, 10), session=session)
    assert session.sent["sid"] == "MDW"
    assert (session.sent["sdate"], session.sent["edate"]) == ("1991-01-01", "2026-11-10")
    assert [e["name"] for e in session.sent["elems"]] == ["maxt", "mint"]


def test_fetch_raises_on_acis_error():
    with pytest.raises(nws.AcisError, match="no data"):
        nws.fetch("XXX", date(2026, 1, 1), date(2026, 1, 2),
                  session=FakeSession({"error": "no data available"}))
    with pytest.raises(nws.AcisError, match="503"):
        nws.fetch("MDW", date(2026, 1, 1), date(2026, 1, 2), session=FakeSession({}, 503))


def test_rows_and_summary(tmp_path):
    rows = nws.parse_rows(RESPONSE)
    assert rows[0] == {"date": date(2026, 8, 14), "high_f": 88, "low_f": 70}
    assert rows[1]["high_f"] is None and rows[2]["low_f"] is None

    out = tmp_path / "nws.csv"
    nws.write_csv(rows, out)
    written = list(csv.DictReader(open(out)))
    assert [r["high_f"] for r in written] == ["88", "", "91"]

    summary = nws.summarize(RESPONSE, rows, "MDW")
    assert summary["days_with_high"] == 2
    assert summary["first_day_with_high"] == "2026-08-14"
    assert summary["last_day_with_high"] == "2026-08-16"
    assert summary["years_missing_over_5pct"] == [2026]  # 1 of 3 days missing


def test_main_writes_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(nws, "fetch", lambda *args, **kwargs: RESPONSE)
    out = tmp_path / "out.csv"
    assert nws.main(["--out", str(out)]) == 0
    assert out.exists()
