# Kalshi Weather Forecasting

Probabilistic forecasts of Chicago's daily high temperature, scored against Kalshi's market prices for the same day.

The target is Kalshi's `KXHIGHCHI` market: the day's maximum temperature at **Chicago Midway** (`CLIMDW`). Current markets settle on The Weather Company's reading for that station. Older markets settled on the National Weather Service Daily Climate Report for Midway; the switch was reported as August 14, 2026. The backfill records which rule each market used in a `settlement_source` column. The NWS climate day runs midnight to midnight in local *standard* time, so during daylight saving time it is 1 a.m. to 1 a.m. on the clock.

## Status

- [x] Kalshi price backfill: `scripts/kalshi_backfill.py`
- [ ] Daily logger: forecasts, prices and order book at fixed decision times
- [ ] Observed daily highs at Midway, for climatology
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
python scripts/kalshi_backfill.py --series KXHIGHCHI --decision 0@08:00 -1@16:00
```

This writes `data/kalshi_KXHIGHCHI.csv`, with one row per temperature bracket, per day, per decision time. Each row has the bracket's bounds, how it settled, the bid and ask at the decision time, and a market-implied probability (each day's brackets sum to 1). Prices come only from candlesticks that ended at or before the decision time, so nothing from later in the day leaks in. No API key is needed. Raw responses are cached in `data/raw/kalshi/`, so reruns are fast.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run offline against a fake Kalshi client.
