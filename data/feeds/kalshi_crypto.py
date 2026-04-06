"""
kalshi_crypto.py — Fetch Kalshi price-prediction markets
=========================================================
Covers:  BTC daily (KXBTCD),  ETH daily (KXETH),  Fed rate (KXFED),
         S&P 500 daily (KXINXD), WTI oil (WTIH)

Returns normalised market dicts suitable for the crypto_ev engine.

Public interface:
    from data.feeds.kalshi_crypto import get_crypto_markets
    markets = await get_crypto_markets()
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_pad
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
_BASE = os.getenv(
    "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
)
_KEY_ID = os.getenv("KALSHI_API_KEY", "")
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
# Helpers
# ---------------------------------------------------------------------------

def _parse_threshold(ticker: str) -> Optional[float]:
    """Extract numeric threshold from ticker like KXBTCD-26APR0611-T69499.99"""
    m = re.search(r"-T([\d.]+)$", ticker)
    return float(m.group(1)) if m else None


def _hours_to_close(close_time_str: str) -> float:
    """Hours from now until market close (UTC ISO string)."""
    try:
        close = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = (close - now).total_seconds() / 3600
        return max(delta, 0.0)
    except Exception:
        return 0.0


def _norm_market(raw: dict, asset: str, series: str) -> Optional[dict]:
    """
    Convert raw Kalshi market dict → normalised market dict.
    Returns None when the market lacks a parseable threshold or has no price.
    """
    threshold = _parse_threshold(raw.get("ticker", ""))
    if threshold is None:
        return None

    yes_ask_str = raw.get("yes_ask_dollars") or raw.get("yes_ask")
    no_ask_str  = raw.get("no_ask_dollars")  or raw.get("no_ask")

    try:
        yes_ask = float(yes_ask_str)
        no_ask  = float(no_ask_str)
    except (TypeError, ValueError):
        return None

    # Skip fully illiquid sentinel prices
    if yes_ask <= 0.01 and no_ask >= 0.99:
        return None

    hours = _hours_to_close(raw.get("close_time", ""))
    if hours <= 0:
        return None

    return {
        "ticker":        raw["ticker"],
        "series":        series,
        "asset":         asset,
        "strike_type":   raw.get("strike_type", "greater"),
        "threshold":     threshold,
        "yes_ask":       yes_ask,     # price to BUY yes  (0-1)
        "no_ask":        no_ask,      # price to BUY no   (0-1)
        "yes_prob":      yes_ask,     # last-ask approximation to market probability
        "close_time":    raw.get("close_time", ""),
        "hours_to_close": hours,
        "title":         raw.get("title", ""),
    }


# ---------------------------------------------------------------------------
# Per-series fetchers
# ---------------------------------------------------------------------------

async def _fetch_series(
    client: httpx.AsyncClient,
    series_ticker: str,
    asset: str,
    max_pages: int = 5,
) -> list[dict]:
    markets = []
    cursor = None
    for _ in range(max_pages):
        params: dict = {"series_ticker": series_ticker, "status": "open", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        path = "/markets"
        try:
            r = await client.get(
                _BASE + path,
                headers=_headers("GET", path),
                params=params,
                timeout=12,
            )
            if r.status_code != 200:
                break
            data = r.json()
        except Exception:
            break

        for raw in data.get("markets", []):
            nm = _norm_market(raw, asset, series_ticker)
            if nm:
                markets.append(nm)

        cursor = data.get("cursor")
        if not cursor:
            break

    return markets


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Series to fetch and their asset label
_SERIES = [
    ("KXBTCD",  "BTC"),   # Bitcoin daily price at specific hour
    ("KXBTCW",  "BTC"),   # Bitcoin weekly price
    ("KXETH",   "ETH"),   # Ethereum price  (T-type only)
    ("KXFED",   "FED"),   # Fed funds rate at next FOMC
    ("KXINXD",  "SPX"),   # S&P 500 index daily
    ("WTIH",    "OIL"),   # WTI crude oil
]


async def get_crypto_markets(
    min_hours: float = 1.0,
    max_hours: float = 120.0,
    min_yes: float = 0.05,
    max_yes: float = 0.95,
) -> list[dict]:
    """
    Fetch all Kalshi price-prediction markets, normalise them, filter by:
    - Close time within [min_hours, max_hours] from now
    - YES ask strictly between min_yes and max_yes (meaningful price)

    Returns a list of normalised market dicts sorted by closeness to deadline.
    """
    async with httpx.AsyncClient() as c:
        tasks = [
            _fetch_series(c, series, asset)
            for series, asset in _SERIES
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    markets = []
    for r in results:
        if isinstance(r, list):
            markets.extend(r)

    # Apply filters
    markets = [
        m for m in markets
        if min_hours <= m["hours_to_close"] <= max_hours
        and min_yes < m["yes_ask"] < max_yes
    ]

    markets.sort(key=lambda m: m["hours_to_close"])
    return markets


if __name__ == "__main__":
    import json

    mkts = asyncio.run(get_crypto_markets())
    print(f"Found {len(mkts)} markets\n")
    for m in mkts[:15]:
        hrs = m["hours_to_close"]
        print(
            f"[{m['asset']:3}] {m['ticker']:<45} "
            f"threshold={m['threshold']:>12,.2f}  "
            f"YES={m['yes_ask']:.2f}  NO={m['no_ask']:.2f}  "
            f"closes in {hrs:.1f}h"
        )
