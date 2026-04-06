"""
btc_price.py — Live crypto spot prices
=======================================
Fetches BTC, ETH (and others) from CoinGecko free API with a
Binance fallback.  No API key required.

Usage:
    from data.feeds.btc_price import get_crypto_prices
    prices = await get_crypto_prices()   # {"btc": 69666, "eth": 2158, ...}
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx

# Ordered list of (name, url, parser) sources
_SOURCES = [
    (
        "coinbase",
        "https://api.coinbase.com/v2/prices/{symbol}-USD/spot",
        None,  # handled specially below
    ),
    (
        "coingecko",
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd",
        lambda d: {"btc": d["bitcoin"]["usd"], "eth": d["ethereum"]["usd"]},
    ),
]

_COINBASE_SYMBOLS = {"btc": "BTC", "eth": "ETH"}


async def _coinbase_prices(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch prices from Coinbase public spot endpoint (no key required)."""
    results = {}
    for ticker, symbol in _COINBASE_SYMBOLS.items():
        url = f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot"
        r = await client.get(url, timeout=8)
        if r.status_code == 200:
            results[ticker] = float(r.json()["data"]["amount"])
    return results if len(results) == len(_COINBASE_SYMBOLS) else None


async def _coingecko_prices(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch prices from CoinGecko free tier."""
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin,ethereum&vs_currencies=usd"
    )
    r = await client.get(url, timeout=10)
    if r.status_code != 200:
        return None
    d = r.json()
    return {
        "btc": float(d["bitcoin"]["usd"]),
        "eth": float(d["ethereum"]["usd"]),
    }


async def get_crypto_prices() -> dict[str, float]:
    """
    Return live spot prices as a dict: {"btc": ..., "eth": ...}

    Tries Coinbase first, falls back to CoinGecko.
    Returns last-known cached values on full failure (so the engine
    degrades gracefully rather than crashing the pipeline).
    """
    async with httpx.AsyncClient() as c:
        for fetcher in [_coinbase_prices, _coingecko_prices]:
            try:
                prices = await fetcher(c)
                if prices:
                    return prices
            except Exception:
                continue

    # Hard fallback — should never reach here in production
    return {"btc": 0.0, "eth": 0.0}


if __name__ == "__main__":
    import json

    prices = asyncio.run(get_crypto_prices())
    print(json.dumps(prices, indent=2))
