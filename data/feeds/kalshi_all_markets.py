"""
kalshi_all_markets.py — Unified market scanner for ALL Kalshi categories.
=========================================================================
Fetches live/upcoming markets across:
  - Crypto 15-min directional (KXBTC15M, KXETH15M, etc.)
  - MLB game, spread, total
  - NBA game, spread, total
  - NHL game, spread, total
  - NCAA basketball
  - CS2/esports
  - Weather/temperature

Returns normalised market dicts for the edge evaluation engine.
"""
from __future__ import annotations
import asyncio, os, time, base64, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_pad
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# ── Auth (same as kalshi.py) ─────────────────────────────────────────────
_BASE = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
_KEY_ID = os.getenv("KALSHI_API_KEY", "")
_PEM_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
_ROOT = Path(__file__).parent.parent.parent

def _load_key():
    pem = _PEM_PATH if Path(_PEM_PATH).is_absolute() else _ROOT / _PEM_PATH
    with open(pem, "rb") as f:
        return load_pem_private_key(f.read(), password=None)

def _headers(method: str, path: str) -> dict:
    full = "/trade-api/v2" + path
    priv = _load_key()
    ts = str(int(time.time() * 1000))
    sig = priv.sign(
        f"{ts}{method}{full}".encode(),
        asym_pad.PSS(mgf=asym_pad.MGF1(hashes.SHA256()), salt_length=asym_pad.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": _KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }

def _f(m, key, default=0.0):
    v = m.get(key)
    if v is None: return default
    try: return float(v)
    except: return default

def _minutes_to_close(close_str: str) -> float:
    try:
        ct = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        return max((ct - datetime.now(timezone.utc)).total_seconds() / 60, 0)
    except:
        return 0.0

# ── Series definitions ────────────────────────────────────────────────────
# (series_ticker, category, market_type)
SPORTS_SERIES = [
    ("KXMLBGAME",     "MLB",    "moneyline"),
    ("KXMLBSPREAD",   "MLB",    "spread"),
    ("KXMLBTOTAL",    "MLB",    "total"),
    ("KXNBAGAME",     "NBA",    "moneyline"),
    ("KXNBASPREAD",   "NBA",    "spread"),
    ("KXNBATOTAL",    "NBA",    "total"),
    ("KXNHLGAME",     "NHL",    "moneyline"),
    ("KXNHLSPREAD",   "NHL",    "spread"),
    ("KXNHLTOTAL",    "NHL",    "total"),
    ("KXNCAAMBGAME",  "NCAAB",  "moneyline"),
    ("KXNCAAMBSPREAD","NCAAB",  "spread"),
    ("KXNCAAMBTOTAL", "NCAAB",  "total"),
    ("KXUFCGAME",     "UFC",    "moneyline"),
    ("KXCS2GAME",     "CS2",    "moneyline"),
]

CRYPTO_SERIES = [
    ("KXBTC15M",  "BTC"),
    ("KXETH15M",  "ETH"),
    ("KXSOL15M",  "SOL"),
    ("KXDOGE15M", "DOGE"),
    ("KXXRP15M",  "XRP"),
    ("KXBNB15M",  "BNB"),
    ("KXHYPE15M", "HYPE"),
]

MIN_OI = 50  # minimum open interest to consider


async def _fetch_series_markets(client: httpx.AsyncClient, series: str) -> list[dict]:
    """Fetch open markets for a series."""
    try:
        r = await client.get(
            f"{_BASE}/markets",
            headers=_headers("GET", "/markets"),
            params={"series_ticker": series, "status": "open", "limit": 50},
            timeout=12,
        )
        if r.status_code != 200:
            return []
        return r.json().get("markets", [])
    except Exception:
        return []


def _norm_sports(raw: dict, category: str, mtype: str) -> Optional[dict]:
    """Normalise a sports market."""
    ticker = raw.get("ticker", "")
    if not ticker or "KXMVE" in ticker:
        return None

    ya = _f(raw, "yes_ask_dollars")
    na = _f(raw, "no_ask_dollars")
    yb = _f(raw, "yes_bid_dollars")
    nb = _f(raw, "no_bid_dollars")

    if ya <= 0 and na <= 0:
        return None

    oi = _f(raw, "open_interest_fp")
    mins = _minutes_to_close(raw.get("close_time", ""))
    if mins <= 0:
        return None

    title = raw.get("title") or raw.get("yes_sub_title") or ""
    # Parse team from title: "Will X win..." or "X vs Y Total..."
    subtitle = raw.get("subtitle") or raw.get("yes_sub_title") or ""

    return {
        "ticker": ticker,
        "category": category,
        "market_type": mtype,
        "yes_ask": ya,
        "no_ask": na,
        "yes_bid": yb,
        "no_bid": nb,
        "spread": ya - yb if yb > 0 else 0,
        "implied_prob_yes": ya,  # yes_ask ≈ implied probability
        "implied_prob_no": na,
        "open_interest": oi,
        "volume": _f(raw, "volume_fp"),
        "volume_24h": _f(raw, "volume_24h_fp"),
        "liquidity": _f(raw, "liquidity_dollars"),
        "minutes_remaining": mins,
        "close_time": raw.get("close_time", ""),
        "title": title,
        "subtitle": subtitle,
        "event_ticker": raw.get("event_ticker", ""),
        "floor_strike": _f(raw, "floor_strike") or None,
    }


def _norm_crypto(raw: dict, asset: str, series: str) -> Optional[dict]:
    """Normalise a crypto 15m market."""
    ticker = raw.get("ticker", "")
    if not ticker:
        return None

    ya = _f(raw, "yes_ask_dollars")
    na = _f(raw, "no_ask_dollars")
    if ya <= 0.03 or ya >= 0.97:
        return None

    oi = _f(raw, "open_interest_fp")
    if oi < MIN_OI:
        return None

    mins = _minutes_to_close(raw.get("close_time", ""))
    if mins <= 0 or mins > 20:
        return None

    floor_strike = _f(raw, "floor_strike")
    if floor_strike <= 0:
        return None

    return {
        "ticker": ticker,
        "category": "CRYPTO_15M",
        "market_type": "directional_15m",
        "asset": asset,
        "series": series,
        "floor_strike": floor_strike,
        "yes_ask": ya,
        "no_ask": na,
        "yes_bid": _f(raw, "yes_bid_dollars"),
        "no_bid": _f(raw, "no_bid_dollars"),
        "spread": ya - _f(raw, "yes_bid_dollars"),
        "open_interest": oi,
        "volume": _f(raw, "volume_fp"),
        "volume_24h": _f(raw, "volume_24h_fp"),
        "liquidity": _f(raw, "liquidity_dollars"),
        "minutes_remaining": mins,
        "close_time": raw.get("close_time", ""),
        "title": raw.get("title", ""),
        "event_ticker": raw.get("event_ticker", ""),
    }


async def get_all_markets() -> dict:
    """
    Fetch ALL tradeable Kalshi markets across all categories.
    Returns { "crypto": [...], "sports": [...], "summary": {...} }
    """
    async with httpx.AsyncClient() as c:
        # Fetch all series concurrently
        crypto_tasks = {
            asset: _fetch_series_markets(c, series)
            for series, asset in CRYPTO_SERIES
        }
        sports_tasks = {
            f"{cat}_{mtype}": _fetch_series_markets(c, series)
            for series, cat, mtype in SPORTS_SERIES
        }

        all_results = await asyncio.gather(
            *(crypto_tasks.values()),
            *(sports_tasks.values()),
            return_exceptions=True,
        )

        # Split results
        crypto_results = list(all_results[:len(crypto_tasks)])
        sports_results = list(all_results[len(crypto_tasks):])

        # Normalise crypto
        crypto_markets = []
        for (series, asset), raws in zip(CRYPTO_SERIES, crypto_results):
            if isinstance(raws, list):
                for raw in raws:
                    nm = _norm_crypto(raw, asset, series)
                    if nm:
                        crypto_markets.append(nm)

        # Normalise sports
        sports_markets = []
        for (series, cat, mtype), raws in zip(SPORTS_SERIES, sports_results):
            if isinstance(raws, list):
                for raw in raws:
                    nm = _norm_sports(raw, cat, mtype)
                    if nm:
                        sports_markets.append(nm)

    return {
        "crypto": sorted(crypto_markets, key=lambda m: m["minutes_remaining"]),
        "sports": sorted(sports_markets, key=lambda m: m["open_interest"], reverse=True),
        "summary": {
            "crypto_count": len(crypto_markets),
            "sports_count": len(sports_markets),
            "categories": list(set(m["category"] for m in sports_markets)),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }


# ── CLI test ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    async def _test():
        data = await get_all_markets()
        print(f"Crypto markets: {data['summary']['crypto_count']}")
        for m in data["crypto"]:
            print(f"  {m['ticker']:<45} {m['asset']:5s} ya=${m['yes_ask']:.2f} OI={m['open_interest']:.0f} {m['minutes_remaining']:.1f}min")
        print(f"\nSports markets: {data['summary']['sports_count']}")
        for m in data["sports"][:20]:
            print(f"  {m['ticker']:<50} {m['category']:5s} {m['market_type']:10s} ya=${m['yes_ask']:.2f} OI={m['open_interest']:.0f} {m['title'][:40]}")
        if not data["crypto"] and not data["sports"]:
            print("No tradeable markets found right now.")

    asyncio.run(_test())
