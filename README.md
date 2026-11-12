# Kalshi Weather Forecasting

Probabilistic forecasts of Chicago's daily high temperature, scored against Kalshi's market prices for the same day.

The target is Kalshi's `KXHIGHCHI` market: the day's maximum temperature at **Chicago Midway** (`CLIMDW`). Current markets settle on The Weather Company's reading for that station. Older markets settled on the National Weather Service Daily Climate Report for Midway; the switch was reported as August 14, 2026. The backfill records which rule each market used in a `settlement_source` column. The NWS climate day runs midnight to midnight in local *standard* time, so during daylight saving time it is 1 a.m. to 1 a.m. on the clock.

## Status

- [x] Kalshi price backfill: `scripts/kalshi_backfill.py`
- [ ] Daily logger: forecasts, prices and order book at fixed decision times
- [ ] Observed NWS daily highs at Midway, for climatology and labels: `scripts/nws_daily_backfill.py` (written, not yet run)
- [ ] Settlement agreement check, Kalshi vs NWS: `scripts/check_settlement.py` (written, not yet run)
- [ ] NBM forecasts as issued
- [ ] Baselines: climatology, NWS forecast + historical error spread, market prices
- [ ] Model and calibration
- [ ] Docker, CI and scheduled runs
- [ ] Results table and reliability diagram

## Setup

Requires Python 3.9+.

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # only if activation is blocked; lasts for this window
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Backfill Kalshi prices

```bash
# Quick test: the most recent 7 days
python scripts/kalshi_backfill.py --series KXHIGHCHI --limit-days 7

# Full history, priced at 8 a.m. on the day and 4 p.m. the day before (Chicago time)
python scripts/kalshi_backfill.py --series KXHIGHCHI --decision D0@08:00 D-1@16:00
```

Decision times are `D<offset>@HH:MM` in Chicago time, relative to the market's day: `D0@08:00` is 8 a.m. on the day, `D-1@16:00` is 4 p.m. the day before.

This writes `data/kalshi_KXHIGHCHI.csv`, with one row per temperature bracket, per day, per decision time. Each row has the bracket's bounds, how it settled, the bid and ask at the decision time, and a market-implied probability (each day's brackets sum to 1). Prices come only from candlesticks that ended at or before the decision time, so nothing from later in the day leaks in. No API key is needed. Raw responses are cached in `data/raw/kalshi/`, so reruns are fast.

## NWS daily highs

```bash
python scripts/nws_daily_backfill.py
```

This downloads Midway's official NWS daily highs and lows from 1991 to yesterday, from [RCC-ACIS](https://www.rcc-acis.org/), and writes `data/nws_daily_MDW.csv`. One request covers the whole range. Use `--sid` for another station and `--start`/`--end` for other dates. The summary lists any years with more than 5% of days missing.

## Settlement agreement check

```bash
python scripts/check_settlement.py
```

The model trains on decades of NWS highs, but current markets settle on The Weather Company. This compares the temperature each Kalshi day settled on with the NWS high for that day, separately for each settlement source, and writes `data/settlement_check.csv`. It reports exact matches, whether the NWS high falls inside the bracket that settled YES, and whether mismatches cluster in daylight saving time. A cluster there would mean The Weather Company's day boundary differs from the NWS's local-standard-time day.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run offline against fake Kalshi and ACIS responses.
