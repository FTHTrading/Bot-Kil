"""
fetch_training_data.py
======================
Fetch historical settled Kalshi 15-min crypto markets + matching Coinbase
OHLCV candles to build a supervised training dataset.

For each settled KXBTC15M / KXETH15M / KXSOL15M / KXDOGE15M / KXXRP15M market:
  - Record floor_strike, close_time, result (yes/no), asset
  - Fetch Coinbase 1-min candles covering [open-30min, close_time]
  - Compute momentum features at {15, 10, 5, 3, 1} minutes-remaining
  - Write one row per feature snapshot → data/training_data.jsonl

Run once (or daily) to build the dataset:
    python scripts/fetch_training_data.py

Output: data/training_data.jsonl
Each line:
{
  "ticker": "KXBTC15M-...", "asset": "BTC",
  "t_remaining": 7.5,           # minutes remaining when features computed
  "floor_strike": 68000.0,      # 15m opening price (= YES threshold)
  "current_price": 68150.0,     # price at feature snapshot
  "gap_pct": 0.0022,            # (current - floor) / floor
  "mom_1m": 0.0012, "mom_3m": 0.0008, "mom_5m": 0.0015, "mom_15m": 0.0021,
  "realized_vol": 0.00085,
  "trend_at_snap": "up",        # "up"/"down"/"flat"
  "hour_utc": 14,               # hour of day (market seasonality)
  "label": 1                    # 1 = YES won, 0 = NO won
}
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import math

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_pad
from cryptography.hazmat.primitives.serialization import load_pem_private_key

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
OUT_FILE = ROOT / "data" / "training_data.jsonl"

# Load .env before reading env-vars
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# ── Kalshi auth ────────────────────────────────────────────────────────────────
_BASE    = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
_KEY_ID  = os.getenv("KALSHI_API_KEY", "")
_PEM     = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

def _load_key():
    p = Path(_PEM) if Path(_PEM).is_absolute() else ROOT / _PEM
    with open(p, "rb") as f:
        return load_pem_private_key(f.read(), password=None)

def _hdrs(method: str, path_suffix: str) -> dict:
    path = "/trade-api/v2" + path_suffix
    priv = _load_key()
    ts   = str(int(time.time() * 1000))
    sig  = priv.sign(
        f"{ts}{method}{path}".encode(),
        asym_pad.PSS(mgf=asym_pad.MGF1(hashes.SHA256()), salt_length=asym_pad.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       _KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }

# ── Fetch all settled Kalshi 15m crypto markets ────────────────────────────────
SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXXRP15M"]
ASSET_MAP = {
    "KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL",
    "KXDOGE15M": "DOGE", "KXXRP15M": "XRP"
}
CB_PAIRS = {
    "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
    "DOGE": "DOGE-USD", "XRP": "XRP-USD",
}

async def fetch_settled_markets(client: httpx.AsyncClient, series: str) -> list[dict]:
    """Fetch up to 1000 settled markets for a given series."""
    markets = []
    cursor  = None
    while True:
        suffix = f"/markets?series_ticker={series}&status=settled&limit=200"
        if cursor:
            suffix += f"&cursor={cursor}"
        hdrs = _hdrs("GET", f"/markets?series_ticker={series}&status=settled&limit=200" + (f"&cursor={cursor}" if cursor else ""))
        r = await client.get(_BASE + suffix, headers=hdrs, timeout=15)
        if r.status_code != 200:
            print(f"  [!] {series} HTTP {r.status_code}: {r.text[:200]}")
            break
        data     = r.json()
        batch    = data.get("markets", [])
        markets.extend(batch)
        cursor   = data.get("cursor")
        print(f"  {series}: fetched {len(batch)} (total {len(markets)})")
        if not cursor or not batch:
            break
        await asyncio.sleep(0.2)
    return markets


async def fetch_cb_candles_1m(
    client: httpx.AsyncClient,
    pair: str,
    start_unix: int,
    end_unix: int,
) -> list:
    """Fetch Coinbase 1-min candles [time, low, high, open, close, vol] in [start, end]."""
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles"
    try:
        r = await client.get(url, params={"granularity": 60, "start": start_unix, "end": end_unix}, timeout=12)
        if r.status_code == 200:
            data = r.json()
            # newest first → reverse to chron order
            return sorted(data, key=lambda c: c[0])
    except Exception as e:
        print(f"  [CB] {pair} candles error: {e}")
    return []


def _compute_features_at_t(
    candles_1m: list,
    floor_strike: float,
    close_unix: int,
    t_remaining_min: float,
) -> dict | None:
    """
    Given 1-min candles and a floor_strike, compute features as if we're
    t_remaining_min minutes before the market closes.
    """
    # timestamp of the "feature snapshot" moment
    snap_unix = close_unix - int(t_remaining_min * 60)

    # find candles up to snap_unix
    hist = [c for c in candles_1m if c[0] <= snap_unix]
    if len(hist) < 5:
        return None

    # close prices (index 4)
    closes = [float(c[4]) for c in hist]
    current = closes[-1]
    if current <= 0 or floor_strike <= 0:
        return None

    # momentum
    def _mom(n):
        if len(closes) >= n + 1:
            prev = closes[-(n + 1)]
            return (current - prev) / prev if prev else 0.0
        return 0.0

    mom_1m  = _mom(1)
    mom_3m  = _mom(3)
    mom_5m  = _mom(5)
    mom_15m = _mom(15)

    # realized vol (std of 5-min log returns using 1-min closes)
    recent_c = closes[-13:]
    log_rets = [math.log(recent_c[i] / recent_c[i-1]) for i in range(1, len(recent_c)) if recent_c[i-1] > 0]
    realized_vol = math.sqrt(sum(r*r for r in log_rets) / max(len(log_rets), 1)) if log_rets else 0.0

    # trend from last 4 1-min closes
    last4  = closes[-4:]
    ups    = sum(1 for i in range(1, len(last4)) if last4[i] > last4[i-1])
    downs  = sum(1 for i in range(1, len(last4)) if last4[i] < last4[i-1])
    if ups >= 3 and mom_1m > 0:
        trend = "up"
    elif downs >= 3 and mom_1m < 0:
        trend = "down"
    else:
        trend = "flat"

    gap_pct = (current - floor_strike) / floor_strike

    return {
        "current_price": current,
        "gap_pct":       gap_pct,
        "mom_1m":        mom_1m,
        "mom_3m":        mom_3m,
        "mom_5m":        mom_5m,
        "mom_15m":       mom_15m,
        "realized_vol":  realized_vol,
        "trend_at_snap": trend,
        "t_remaining":   t_remaining_min,
    }


async def build_rows_for_market(
    client: httpx.AsyncClient,
    market: dict,
    asset: str,
) -> list[dict]:
    """Build training rows for one settled market."""
    ticker      = market.get("ticker", "")
    floor       = market.get("floor_strike")
    result      = market.get("result")          # "yes" or "no"
    close_time  = market.get("close_time")      # ISO 8601

    if not floor or not result or not close_time:
        return []
    try:
        floor = float(floor)
    except (TypeError, ValueError):
        return []

    label = 1 if result == "yes" else 0

    # parse close_time
    try:
        if close_time.endswith("Z"):
            close_time = close_time[:-1] + "+00:00"
        close_dt   = datetime.fromisoformat(close_time)
        close_unix = int(close_dt.timestamp())
    except Exception:
        return []

    hour_utc = close_dt.hour

    # fetch 1-min candles for 35 min before close → gives enough history
    start_unix = close_unix - 35 * 60
    pair       = CB_PAIRS.get(asset)
    if not pair:
        return []

    await asyncio.sleep(0.05)  # rate limit courtesy
    candles = await fetch_cb_candles_1m(client, pair, start_unix, close_unix)
    if not candles:
        return []

    # build a feature snapshot at multiple time-remaining points
    rows = []
    for t_rem in [14.0, 10.0, 7.0, 5.0, 3.0, 2.0, 1.0]:
        feats = _compute_features_at_t(candles, floor, close_unix, t_rem)
        if feats is None:
            continue
        row = {
            "ticker":       ticker,
            "asset":        asset,
            "floor_strike": floor,
            "hour_utc":     hour_utc,
            "label":        label,
            **feats,
        }
        rows.append(row)

    return rows


async def main():
    print("=" * 60)
    print("Fetching historical Kalshi 15m crypto training data")
    print("=" * 60)

    OUT_FILE.parent.mkdir(exist_ok=True)

    # Load already-fetched tickers to allow resume
    done_tickers: set[str] = set()
    if OUT_FILE.exists():
        existing = [json.loads(l) for l in OUT_FILE.read_text().splitlines() if l.strip()]
        done_tickers = {r["ticker"] for r in existing}
        print(f"Resuming — {len(done_tickers)} tickers already in dataset ({len(existing)} rows)")

    async with httpx.AsyncClient() as client:
        # Step 1: fetch all settled market metadata
        all_markets: list[dict] = []
        for series in SERIES:
            print(f"\nFetching {series}...")
            ms = await fetch_settled_markets(client, series)
            for m in ms:
                m["_asset"] = ASSET_MAP[series]
            all_markets.extend(ms)

        total = len(all_markets)
        new_m = [m for m in all_markets if m.get("ticker") not in done_tickers]
        print(f"\nTotal settled markets: {total}  |  New (unfetched): {len(new_m)}")

        if not new_m:
            print("Nothing new to fetch.")
            return

        # Step 2: fetch Coinbase candles and build rows
        total_rows = 0
        with open(OUT_FILE, "a", encoding="utf-8") as fh:
            for i, market in enumerate(new_m):
                asset = market["_asset"]
                ticker = market.get("ticker", "")
                if i % 50 == 0:
                    print(f"  [{i}/{len(new_m)}] {ticker}...")
                rows = await build_rows_for_market(client, market, asset)
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
                total_rows += len(rows)

    print(f"\nDone. Wrote {total_rows} new training rows → {OUT_FILE}")
    print("Now run:  python scripts/train_neural_model.py")


if __name__ == "__main__":
    asyncio.run(main())
