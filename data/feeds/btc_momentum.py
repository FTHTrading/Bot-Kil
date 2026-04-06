"""
btc_momentum.py — Short-term price momentum signals for crypto assets
=====================================================================
Fetches recent 5-min OHLCV candles from Binance (public, no key) to
compute 5-min and 15-min momentum for BTC / ETH / SOL / DOGE / XRP / BNB.

Also computes realized intraday volatility from the last 12 x 5-min candles
(1 hour), giving a better σ estimate than a fixed daily assumption.

Usage:
    from data.feeds.btc_momentum import get_momentum_signals
    signals = await get_momentum_signals()
    # {
    #   "BTC": {
    #       "current":       69700.0,   # latest close price
    #       "mom_5m":        +0.0013,   # % change as decimal over last 5 min
    #       "mom_15m":       -0.0008,   # % change over last 15 min
    #       "realized_vol":  0.00035,   # realized σ per 5-min period (not annualised)
    #       "trend":         "up"|"down"|"flat",
    #   },
    #   "ETH": {...},
    #   ...
    # }
"""
from __future__ import annotations

import asyncio
import math
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Asset → Binance symbol mapping
# ---------------------------------------------------------------------------
_SYMBOLS: dict[str, str] = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "SOL":  "SOLUSDT",
    "DOGE": "DOGEUSDT",
    "XRP":  "XRPUSDT",
    "BNB":  "BNBUSDT",
}

_BINANCE_CANDLES = "https://api.binance.com/api/v3/klines"
_BINANCE_TICKER  = "https://api.binance.com/api/v3/ticker/price"
_NUM_CANDLES = 16   # 16 x 5-min = 80 minutes of history


# ---------------------------------------------------------------------------
# Fallback: Coinbase candles + ticker for BTC/ETH/SOL (and XRP/DOGE)
# ---------------------------------------------------------------------------
_COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{pair}/candles"
_COINBASE_TICKER  = "https://api.exchange.coinbase.com/products/{pair}/ticker"
_COINBASE_PAIRS = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
    "SOL":  "SOL-USD",
    "XRP":  "XRP-USD",
    "DOGE": "DOGE-USD",
}


async def _binance_candles(
    client: httpx.AsyncClient, symbol: str, limit: int = _NUM_CANDLES
) -> Optional[list]:
    """Return list of 5-min candles [open_ms, open, high, low, close, vol, ...]."""
    try:
        r = await client.get(
            _BINANCE_CANDLES,
            params={"symbol": symbol, "interval": "5m", "limit": limit},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


async def _binance_spot_price(
    client: httpx.AsyncClient, symbol: str
) -> Optional[float]:
    """Return the real-time Binance spot price for a symbol (may be geo-blocked)."""
    try:
        r = await client.get(
            _BINANCE_TICKER,
            params={"symbol": symbol},
            timeout=5,
        )
        if r.status_code == 200:
            return float(r.json().get("price", 0) or 0)
    except Exception:
        pass
    return None


async def _coinbase_spot_price(
    client: httpx.AsyncClient, pair: str
) -> Optional[float]:
    """Return real-time Coinbase spot price from the ticker endpoint."""
    try:
        r = await client.get(
            _COINBASE_TICKER.format(pair=pair),
            timeout=5,
        )
        if r.status_code == 200:
            price = r.json().get("price")
            if price:
                return float(price)
    except Exception:
        pass
    return None


async def _coinbase_candles(
    client: httpx.AsyncClient, pair: str, limit: int = _NUM_CANDLES
) -> Optional[list]:
    """Return Coinbase candles as list of [time, low, high, open, close, vol]."""
    try:
        r = await client.get(
            _COINBASE_CANDLES.format(pair=pair),
            params={"granularity": 300},  # 300 sec = 5 min
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            # Coinbase returns newest first; reverse to chronological order
            return list(reversed(data))
    except Exception:
        pass
    return None


def _compute_signals(candles: list, source: str = "binance") -> dict:
    """
    Given a list of 5-min candles, compute momentum / vol signals.

    Binance candle index: [0]=open_ms [1]=open [2]=high [3]=low [4]=close [5]=vol
    Coinbase candle index: [0]=time [1]=low [2]=high [3]=open [4]=close [5]=vol
    """
    if len(candles) < 4:
        return {}

    close_idx = 4  # same for both Binance and Coinbase

    closes = [float(c[close_idx]) for c in candles]

    current    = closes[-1]
    price_5m   = closes[-2]   # 1 candle back  = ~5 min ago
    price_15m  = closes[-4] if len(closes) >= 4 else closes[0]  # 3 candles back = ~15 min

    mom_5m  = (current - price_5m)  / price_5m  if price_5m  else 0.0
    mom_15m = (current - price_15m) / price_15m if price_15m else 0.0

    # Realized vol: std-dev of 5-min log returns over last 12 candles (1 hour)
    recent = closes[-13:] if len(closes) >= 13 else closes
    log_returns = [
        math.log(recent[i] / recent[i - 1])
        for i in range(1, len(recent))
        if recent[i - 1] > 0
    ]
    realized_vol_5m = float(math.sqrt(sum(r**2 for r in log_returns) / max(len(log_returns), 1)))

    # Trend: consistent direction in last 3 candles
    last3 = closes[-4:]
    up_moves   = sum(1 for i in range(1, len(last3)) if last3[i] > last3[i - 1])
    down_moves = sum(1 for i in range(1, len(last3)) if last3[i] < last3[i - 1])

    if up_moves >= 2 and mom_5m > 0:
        trend = "up"
    elif down_moves >= 2 and mom_5m < 0:
        trend = "down"
    else:
        trend = "flat"

    return {
        "current":      current,
        "mom_5m":       mom_5m,
        "mom_15m":      mom_15m,
        "realized_vol": realized_vol_5m,   # σ per 5-min period
        "trend":        trend,
        "closes":       closes[-6:],        # last 6 closes for display
    }


async def _fetch_asset(
    client: httpx.AsyncClient, asset: str
) -> tuple[str, dict]:
    """Fetch candles + live spot price and compute signals for one asset."""
    symbol     = _SYMBOLS.get(asset)
    cb_pair    = _COINBASE_PAIRS.get(asset)

    # Fetch candles and real-time spot prices in parallel (Binance + Coinbase)
    tasks = [
        _binance_candles(client, symbol) if symbol else asyncio.sleep(0, None),
        _binance_spot_price(client, symbol) if symbol else asyncio.sleep(0, None),
        _coinbase_spot_price(client, cb_pair) if cb_pair else asyncio.sleep(0, None),
    ]
    candles, binance_spot, coinbase_spot = await asyncio.gather(*tasks, return_exceptions=False)

    # Coinbase fallback for candles
    if not candles and cb_pair:
        candles = await _coinbase_candles(client, cb_pair)

    if not candles:
        return asset, {}

    signals = _compute_signals(candles)
    if not signals:
        return asset, {}

    # Prefer live Binance spot → Coinbase spot → stale candle close
    live_price = binance_spot or coinbase_spot
    if live_price and live_price > 0:
        signals["current"]    = live_price
        signals["spot_live"]  = True
        signals["spot_source"] = "binance" if binance_spot else "coinbase"
    else:
        signals["spot_live"]  = False
        signals["spot_source"] = "stale_candle"

    return asset, signals


async def get_momentum_signals(
    assets: Optional[list[str]] = None,
) -> dict[str, dict]:
    """
    Fetch 5-min candles for all assets and return momentum signals dict.

    Parameters
    ----------
    assets : list of asset names to fetch (default: all 6)

    Returns
    -------
    dict mapping asset name → signals dict (empty dict if fetch failed)
    """
    if assets is None:
        assets = list(_SYMBOLS.keys())

    async with httpx.AsyncClient() as c:
        results = await asyncio.gather(
            *[_fetch_asset(c, a) for a in assets],
            return_exceptions=True,
        )

    out = {}
    for item in results:
        if isinstance(item, Exception):
            continue
        asset, signals = item
        out[asset] = signals

    return out


# ---------------------------------------------------------------------------
# Quick CLI test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio

    async def _test():
        signals = await get_momentum_signals()
        for asset, s in signals.items():
            if s:
                live = "LIVE" if s.get("spot_live") else "stale"
                print(
                    f"{asset:<6}  current={s['current']:>12,.4f}  [{live}]  "
                    f"mom_5m={s['mom_5m']:+.4f}  mom_15m={s['mom_15m']:+.4f}  "
                    f"vol_5m={s['realized_vol']:.5f}  trend={s['trend']}"
                )

    asyncio.run(_test())
