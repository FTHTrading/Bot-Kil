"""
kalshi_intraday.py — Fetch Kalshi 15-minute directional markets
===============================================================
Covers: KXBTC15M, KXETH15M, KXSOL15M, KXDOGE15M, KXXRP15M, KXBNB15M

These markets resolve YES if price(close) >= price(open) over a 15-min window.
The `floor_strike` field holds the opening reference price (BRTI average at
market open, set by Kalshi).

Returns normalised market dicts suitable for the intraday_ev engine.

Public interface:
    from data.feeds.kalshi_intraday import get_intraday_markets
    markets = await get_intraday_markets()
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_pad
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# ---------------------------------------------------------------------------
# Auth  (same pattern as kalshi_crypto.py)
# ---------------------------------------------------------------------------
_BASE = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
_KEY_ID  = os.getenv("KALSHI_API_KEY", "")
_PEM_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
_PROJECT_ROOT = Path(__file__).parent.parent.parent


def _load_key():
    pem = _PEM_PATH if Path(_PEM_PATH).is_absolute() else _PROJECT_ROOT / _PEM_PATH
    with open(pem, "rb") as f:
        return load_pem_private_key(f.read(), password=None)


def _headers(method: str, url: str) -> dict:
    path = "/trade-api/v2" + url
    priv = _load_key()
    ts = str(int(time.time() * 1000))
    sig = priv.sign(
        f"{ts}{method}{path}".encode(),
        asym_pad.PSS(
            mgf=asym_pad.MGF1(hashes.SHA256()),
            salt_length=asym_pad.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": _KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


# ---------------------------------------------------------------------------
# Series → asset mapping
# ---------------------------------------------------------------------------
_INTRADAY_SERIES: list[tuple[str, str]] = [
    ("KXBTC15M",  "BTC"),
    ("KXETH15M",  "ETH"),
    ("KXSOL15M",  "SOL"),
    ("KXDOGE15M", "DOGE"),
    ("KXXRP15M",  "XRP"),
    ("KXBNB15M",  "BNB"),
]

# Minimum liquidity (open interest) to bother with
_MIN_OI = 100.0


def _hours_to_close(close_time_str: str) -> float:
    try:
        close = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max((close - now).total_seconds() / 3600, 0.0)
    except Exception:
        return 0.0


def _minutes_to_close(close_time_str: str) -> float:
    return _hours_to_close(close_time_str) * 60.0


def _open_hours_ago(open_time_str: str) -> float:
    """How many hours ago did the market open (positive = in the past)."""
    try:
        open_dt = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - open_dt).total_seconds() / 3600
    except Exception:
        return 0.0


def _norm_intraday(raw: dict, asset: str, series: str) -> Optional[dict]:
    """
    Normalise a raw Kalshi 15M market dict.
    Returns None if the market is not actionable.
    """
    ticker = raw.get("ticker", "")
    if not ticker:
        return None

    # floor_strike = BTC price at market open (reference / start price)
    floor_strike = raw.get("floor_strike")
    try:
        floor_strike = float(floor_strike)
    except (TypeError, ValueError):
        return None

    yes_ask_str = raw.get("yes_ask_dollars")
    no_ask_str  = raw.get("no_ask_dollars")
    try:
        yes_ask = float(yes_ask_str)
        no_ask  = float(no_ask_str)
    except (TypeError, ValueError):
        return None

    # Skip degenerate / fully-decided markets
    if yes_ask <= 0.03 or yes_ask >= 0.97:
        return None

    # Skip illiquid markets
    try:
        oi = float(raw.get("open_interest_fp") or 0)
    except (TypeError, ValueError):
        oi = 0.0
    if oi < _MIN_OI:
        return None

    minutes_remaining = _minutes_to_close(raw.get("close_time", ""))
    if minutes_remaining <= 0 or minutes_remaining > 20:
        # Only care about the current/next 15-min window (skip distant future)
        return None

    opened_ago = _open_hours_ago(raw.get("open_time", "")) * 60  # minutes

    return {
        "ticker":            ticker,
        "series":            series,
        "asset":             asset,
        "floor_strike":      floor_strike,       # reference price at open
        "yes_ask":           yes_ask,            # 0-1 float
        "no_ask":            no_ask,             # 0-1 float
        "open_interest":     oi,
        "close_time":        raw.get("close_time", ""),
        "open_time":         raw.get("open_time", ""),
        "minutes_remaining": minutes_remaining,
        "opened_ago_min":    opened_ago,
        "title":             raw.get("title", ""),
        "market_type":       "directional_15m",
        "volume_24h":        float(raw.get("volume_24h_fp") or 0),
    }


async def _fetch_intraday_series(
    client: httpx.AsyncClient, series: str, asset: str
) -> list[dict]:
    """Fetch open 15M markets for one series."""
    path = "/markets"
    try:
        r = await client.get(
            _BASE + path,
            headers=_headers("GET", path),
            params={"series_ticker": series, "status": "open", "limit": 20},
            timeout=12,
        )
        if r.status_code != 200:
            return []
        markets_raw = r.json().get("markets", [])
    except Exception:
        return []

    result = []
    for raw in markets_raw:
        nm = _norm_intraday(raw, asset, series)
        if nm:
            result.append(nm)
    return result


async def get_intraday_markets() -> list[dict]:
    """
    Fetch all currently-open 15-minute directional markets.
    Only returns markets in the CURRENT 15-min window (closes ≤17 min away).

    Returns a list of normalised market dicts sorted by minutes_remaining asc.
    """
    async with httpx.AsyncClient() as c:
        tasks = [
            _fetch_intraday_series(c, series, asset)
            for series, asset in _INTRADAY_SERIES
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out = []
    for r in results:
        if isinstance(r, list):
            out.extend(r)

    return sorted(out, key=lambda m: m["minutes_remaining"])


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    async def _test():
        markets = await get_intraday_markets()
        if not markets:
            print("No intraday markets open right now.")
            return
        print(f"\nOpen 15-min markets ({len(markets)}):")
        for m in markets:
            print(
                f"  {m['ticker']:<40}  floor=${m['floor_strike']:>12,.4f}  "
                f"YES={m['yes_ask']:.2f}  NO={m['no_ask']:.2f}  "
                f"{m['minutes_remaining']:.1f}min left  OI={m['open_interest']:.0f}"
            )

    asyncio.run(_test())
