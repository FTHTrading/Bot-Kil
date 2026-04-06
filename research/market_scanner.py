"""
market_scanner.py — Universal Kalshi market scanner
====================================================
Scans ALL active Kalshi markets across every category:

  • crypto     — BTC/ETH/SOL/DOGE/XRP 15-minute, hourly, daily
  • econ       — CPI, NFP, GDP, Fed rate, retail sales, housing
  • political  — elections, approval ratings, geopolitical
  • weather    — temperature, precipitation, storm tracks
  • sports     — MLB/NBA/NFL/NHL game outcomes
  • misc       — any other active markets

For each market it:
  1. Normalises the schema to a standard dict
  2. Enriches with volume-ratio, OI-delta, timeframe peers
  3. Fetches external context (momentum, FedWatch, polls) as needed
  4. Scores via strategy_library.score_market()
  5. Returns ranked opportunities table

Public API:
    from research.market_scanner import scan_all, scan_category
    opps = await scan_all(context_override={})
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

_PROJECT_ROOT = Path(__file__).parent.parent

# ─── Kalshi auth (mirrors kalshi_intraday.py pattern) ────────────────────────

_BASE     = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
_KEY_ID   = os.getenv("KALSHI_API_KEY", "")
_PEM_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")


def _load_pem_key():
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    pem = _PEM_PATH if Path(_PEM_PATH).is_absolute() else _PROJECT_ROOT / _PEM_PATH
    with open(str(pem), "rb") as f:
        return load_pem_private_key(f.read(), password=None)


def _sign_headers(method: str, path: str) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_pad
    priv = _load_pem_key()
    ts = str(int(time.time() * 1000))
    full_path = "/trade-api/v2" + path
    sig = priv.sign(
        f"{ts}{method}{full_path}".encode(),
        asym_pad.PSS(mgf=asym_pad.MGF1(hashes.SHA256()),
                     salt_length=asym_pad.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": _KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }


async def _kalshi_get(path: str, params: dict = None) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE}{path}",
                headers=_sign_headers("GET", path),
                params=params or {},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f"[Scanner] Kalshi GET {path} error: {e}")
    return None


# ─── Category classifiers ─────────────────────────────────────────────────────

_CRYPTO_SERIES = {"KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP", "KXBNB", "KXAVAX", "KXLINK"}
_ECON_WORDS    = {"CPI", "NFP", "PAYROLL", "GDP", "PCE", "FOMC", "FED", "RETAIL", "HOUSING", "ISM", "PMI", "PPI", "UNEMPLOY", "INFLATION"}
_POLITICAL     = {"PRES", "SENATE", "HOUSE", "GOVERN", "APPROV", "ELECTION", "VOTE", "POLITIC"}
_WEATHER       = {"TEMP", "PRECIP", "RAIN", "SNOW", "STORM", "HURRICANE", "WEATHER", "DEGREE"}
_SPORTS        = {"NFL", "NBA", "MLB", "NHL", "NCAAB", "NCAAF", "MLS", "SOCCER", "EPL"}

def _classify(ticker: str, title: str) -> str:
    t = (ticker + " " + title).upper()
    if any(s in t for s in _CRYPTO_SERIES):
        return "crypto"
    if any(s in t for s in _ECON_WORDS):
        return "econ"
    if any(s in t for s in _POLITICAL):
        return "political"
    if any(s in t for s in _WEATHER):
        return "weather"
    if any(s in t for s in _SPORTS):
        return "sports"
    return "misc"


def _asset_from_ticker(ticker: str) -> Optional[str]:
    for prefix in ("BTC", "ETH", "SOL", "DOGE", "XRP", "BNB", "AVAX", "LINK"):
        if prefix in ticker.upper():
            return prefix
    return None


def _timeframe_from_ticker(ticker: str) -> str:
    t = ticker.upper()
    if "15M" in t:
        return "15min"
    if "1H" in t or "HOURLY" in t:
        return "1hr"
    if "4H" in t:
        return "4hr"
    return "daily"


def _minutes_remaining(market: dict) -> float:
    raw = market.get("close_time") or market.get("expiration_time")
    if not raw:
        return 999.0
    try:
        if isinstance(raw, str):
            # ISO-8601 UTC
            raw = raw.rstrip("Z")
            if "." in raw:
                close_dt = datetime.fromisoformat(raw + "+00:00") if "+" not in raw else datetime.fromisoformat(raw)
            else:
                close_dt = datetime.fromisoformat(raw + "+00:00") if "+" not in raw else datetime.fromisoformat(raw)
        else:
            from datetime import datetime as dt
            close_dt = dt.fromtimestamp(raw / 1000, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (close_dt - now).total_seconds() / 60.0)
    except Exception:
        return 999.0


def _normalise_market(raw: dict, category: str) -> dict:
    """Convert raw Kalshi market API response to scanner standard dict."""
    ticker = raw.get("ticker", "")
    yes_ask_cents = raw.get("yes_ask") or raw.get("last_price") or 50
    no_ask_cents  = raw.get("no_ask")  or (100 - yes_ask_cents)
    min_rem = _minutes_remaining(raw)

    return {
        "ticker":            ticker,
        "title":             raw.get("title", ""),
        "series":            raw.get("series_ticker", ticker.split("-")[0] if "-" in ticker else ticker),
        "market_type":       category,
        "timeframe":         _timeframe_from_ticker(ticker),
        "asset":             _asset_from_ticker(ticker),
        "floor_strike":      raw.get("floor_strike") or raw.get("cap_strike"),
        "yes_ask":           yes_ask_cents / 100.0,
        "no_ask":            no_ask_cents  / 100.0,
        "yes_ask_cents":     yes_ask_cents,
        "open_interest":     raw.get("open_interest", 0) or 0,
        "volume":            raw.get("volume", 0) or 0,
        "minutes_remaining": min_rem,
        "hours_to_expiry":   min_rem / 60.0,
        "_raw":              raw,
    }


# ─── External context fetchers ────────────────────────────────────────────────

async def _get_momentum_context(assets: list[str]) -> dict:
    """Load real-time momentum signals from btc_momentum.py."""
    try:
        from data.feeds.btc_momentum import get_momentum_signals
        signals = await get_momentum_signals(assets)
        return signals
    except Exception as e:
        print(f"[Scanner] Momentum context error: {e}")
        return {}


async def _get_fedwatch_context() -> dict:
    """
    Scrape CME FedWatch probabilities (public, no auth required).
    Returns {'hold': P, 'cut_25': P, 'cut_50': P, 'hike': P}
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.cmegroup.com/CmeWS/mvc/MeetingProbability/2024",
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200:
                data = resp.json()
                # Parse CME's response format
                probs = {}
                for item in data.get("probabilities", []):
                    label = item.get("description", "").lower()
                    prob  = float(item.get("probability", 0)) / 100.0
                    if "no change" in label or "hold" in label:
                        probs["hold"] = prob
                    elif "25" in label and "cut" in label:
                        probs["cut_25"] = prob
                    elif "50" in label and "cut" in label:
                        probs["cut_50"] = prob
                    elif "hike" in label or "increase" in label:
                        probs["hike"] = prob
                if probs:
                    return probs
    except Exception:
        pass
    # Fallback: estimate from Fed funds futures (rough)
    return {}


async def _get_econ_consensus_context() -> dict:
    """
    Fetch economic consensus estimates from a public source.
    Uses Trading Economics / FRED interpolation as fallback.
    Returns {series_name: {estimate, std}}
    """
    # Static reasonable defaults (updated weekly in production via research agent)
    # These would be refreshed by calling research_agent.update_consensus()
    defaults = {
        "CPI":      {"estimate": 0.3,  "std": 0.1,  "source": "default"},
        "NFP":      {"estimate": 175,  "std": 50,   "source": "default"},
        "PAYROLLS": {"estimate": 175,  "std": 50,   "source": "default"},
        "GDP":      {"estimate": 2.0,  "std": 0.4,  "source": "default"},
        "PCE":      {"estimate": 0.2,  "std": 0.08, "source": "default"},
    }
    # Try to load from cached file if available
    cache_path = _PROJECT_ROOT / "db" / "econ_consensus.json"
    if cache_path.exists():
        try:
            import json
            with open(str(cache_path)) as f:
                cached = json.load(f)
            defaults.update(cached)
        except Exception:
            pass
    return defaults


async def _enrich_with_oi_delta(markets: list[dict]) -> list[dict]:
    """
    For each market, look up the last two snapshots from learning_tracker
    and compute OI delta % and price direction.
    """
    try:
        from research.learning_tracker import _db
        for m in markets:
            ticker = m["ticker"]
            with _db() as conn:
                snaps = conn.execute(
                    """SELECT yes_price, open_interest FROM market_snapshots
                       WHERE ticker=? ORDER BY captured_at DESC LIMIT 2""",
                    (ticker,)
                ).fetchall()
            if len(snaps) >= 2:
                oi_new = snaps[0]["open_interest"] or 0
                oi_old = snaps[1]["open_interest"] or 1
                p_new  = snaps[0]["yes_price"] or 50
                p_old  = snaps[1]["yes_price"] or 50
                m["oi_delta_pct"]    = (oi_new - oi_old) / max(1, oi_old)
                m["price_delta_pct"] = (p_new  - p_old)  / 100.0
                m["price_direction"] = 1 if p_new > p_old else (-1 if p_new < p_old else 0)
    except Exception:
        pass
    return markets


async def _enrich_with_volume_ratio(markets: list[dict]) -> list[dict]:
    """Compute 24h volume ratio vs rolling average of recent snapshots."""
    try:
        from research.learning_tracker import _db
        for m in markets:
            ticker = m["ticker"]
            with _db() as conn:
                rows = conn.execute(
                    """SELECT volume FROM market_snapshots
                       WHERE ticker=? ORDER BY captured_at DESC LIMIT 24""",
                    (ticker,)
                ).fetchall()
            if rows and len(rows) > 2:
                volumes = [r["volume"] or 0 for r in rows]
                avg_vol = sum(volumes[1:]) / max(1, len(volumes) - 1)
                current_vol = m.get("volume", 0) or 0
                m["volume_ratio"] = current_vol / max(1, avg_vol)
    except Exception:
        pass
    return markets


async def _attach_timeframe_peers(markets: list[dict]) -> list[dict]:
    """
    For each market, find other markets on the same asset with different timeframes
    and attach them as `timeframe_peers`.
    """
    # Group by asset
    by_asset: dict[str, list[dict]] = {}
    for m in markets:
        asset = m.get("asset") or m.get("series", m["ticker"])
        by_asset.setdefault(asset, []).append(m)

    for m in markets:
        asset = m.get("asset") or m.get("series", m["ticker"])
        peers = [p for p in by_asset.get(asset, []) if p["ticker"] != m["ticker"]]
        m["timeframe_peers"] = peers
    return markets


# ─── Primary scan functions ───────────────────────────────────────────────────

async def fetch_all_active_markets(limit: int = 1000) -> list[dict]:
    """
    Fetch up to `limit` active Kalshi markets across all series/events.
    Returns raw dicts as returned by the Kalshi API /markets endpoint.
    """
    markets = []
    cursor = None
    page_size = 200

    try:
        while len(markets) < limit:
            params = {"status": "active", "limit": min(page_size, limit - len(markets))}
            if cursor:
                params["cursor"] = cursor

            data = await _kalshi_get("/markets", params)
            if not data:
                break

            batch = data.get("markets", [])
            markets.extend(batch)

            cursor = data.get("cursor")
            if not cursor or len(batch) < page_size:
                break
    except Exception as e:
        print(f"[Scanner] fetch_all_active_markets error: {e}")

    return markets


async def scan_category(
    category: str,
    context_override: dict = None,
    min_edge: float = 0.04,
    limit: int = 200,
) -> list[dict]:
    """
    Scan markets in a single category ('crypto', 'econ', 'political', etc.)
    Returns list of opportunity dicts sorted by weighted_edge descending.
    """
    from research.strategy_library import score_market
    from research.learning_tracker import get_strategy_weights

    raw_markets = await fetch_all_active_markets(limit=limit)
    category_markets = []
    for raw in raw_markets:
        ticker = raw.get("ticker", "")
        title  = raw.get("title", "")
        cat    = _classify(ticker, title)
        if cat == category:
            category_markets.append(_normalise_market(raw, cat))

    if not category_markets:
        return []

    # Enrich
    category_markets = await _enrich_with_oi_delta(category_markets)
    category_markets = await _enrich_with_volume_ratio(category_markets)
    category_markets = await _attach_timeframe_peers(category_markets)

    # Build context
    context = context_override or {}
    if category == "crypto" and "momentum" not in context:
        assets = list({m["asset"] for m in category_markets if m.get("asset")})
        if assets:
            context["momentum"] = await _get_momentum_context(assets)

    if category == "econ" and "econ_consensus" not in context:
        context["econ_consensus"] = await _get_econ_consensus_context()

    if category == "econ" and "fedwatch" not in context:
        context["fedwatch"] = await _get_fedwatch_context()

    # Load strategy weights from learning tracker
    try:
        weights = get_strategy_weights()
    except Exception:
        weights = {}

    # Score all markets
    opportunities = []
    for market in category_markets:
        # Skip markets expiring in <2 min or >7 days
        if market["minutes_remaining"] < 2:
            continue
        if market["minutes_remaining"] > 60 * 24 * 7:
            continue

        scores = score_market(market, context, weights)
        for score in scores:
            opp = {**market, **score, "category": category}
            opp.pop("_raw", None)
            opp.pop("timeframe_peers", None)
            if abs(opp["edge_pct"]) >= min_edge:
                opportunities.append(opp)
                break  # take best strategy per market

    opportunities.sort(key=lambda x: abs(x["weighted_edge"]), reverse=True)
    return opportunities


async def scan_all(
    categories: list[str] = None,
    context_override: dict = None,
    min_edge: float = 0.04,
    top_n: int = 50,
) -> list[dict]:
    """
    Scan ALL categories in parallel, return top_n opportunities.
    categories: subset to scan, or None for all.
    """
    all_cats = categories or ["crypto", "econ", "political", "weather", "sports", "misc"]

    # Build shared context once
    shared_context = dict(context_override or {})

    # Fetch all markets once and share across category scanners
    print(f"[Scanner] Fetching all active Kalshi markets...")
    raw_all = await fetch_all_active_markets(limit=1000)
    print(f"[Scanner] Fetched {len(raw_all)} active markets")

    # Get momentum + fedwatch in parallel if needed
    tasks = {}
    if "crypto" in all_cats and "momentum" not in shared_context:
        tasks["momentum"] = _get_momentum_context(["BTC", "ETH", "SOL", "DOGE", "XRP"])
    if ("econ" in all_cats) and "fedwatch" not in shared_context:
        tasks["fedwatch"] = _get_fedwatch_context()
    if ("econ" in all_cats) and "econ_consensus" not in shared_context:
        tasks["econ_consensus"] = _get_econ_consensus_context()

    if tasks:
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for key, result in zip(tasks.keys(), results):
            if not isinstance(result, Exception):
                shared_context[key] = result

    from research.strategy_library import score_market
    from research.learning_tracker import get_strategy_weights
    try:
        weights = get_strategy_weights()
    except Exception:
        weights = {}

    # Classify and normalise all markets
    all_normed = []
    for raw in raw_all:
        ticker = raw.get("ticker", "")
        cat    = _classify(ticker, raw.get("title", ""))
        if cat not in all_cats:
            continue
        all_normed.append(_normalise_market(raw, cat))

    # Enrich
    all_normed = await _enrich_with_oi_delta(all_normed)
    all_normed = await _enrich_with_volume_ratio(all_normed)
    all_normed = await _attach_timeframe_peers(all_normed)

    # Score every market
    opportunities = []
    for market in all_normed:
        if market["minutes_remaining"] < 2 or market["minutes_remaining"] > 60 * 24 * 7:
            continue
        scores = score_market(market, shared_context, weights)
        if scores:
            best = scores[0]
            opp = {**market, **best, "category": market["market_type"]}
            opp.pop("_raw", None)
            opp.pop("timeframe_peers", None)
            if abs(opp["edge_pct"]) >= min_edge:
                opportunities.append(opp)

    opportunities.sort(key=lambda x: abs(x["weighted_edge"]), reverse=True)
    return opportunities[:top_n]


async def get_market_detail(ticker: str) -> Optional[dict]:
    """Fetch full market detail + orderbook for deep analysis."""
    market_data = await _kalshi_get(f"/markets/{ticker}")
    if not market_data:
        return None
    raw = market_data.get("market", market_data)
    cat = _classify(raw.get("ticker", ticker), raw.get("title", ""))
    normed = _normalise_market(raw, cat)

    # Try to get orderbook depth
    try:
        ob_data = await _kalshi_get(f"/markets/{ticker}/orderbook")
        if ob_data:
            normed["orderbook"] = ob_data.get("orderbook", {})
    except Exception:
        pass

    return normed


async def snapshot_market_prices(tickers: list[str]) -> None:
    """
    Capture a price snapshot of the given tickers into learning_tracker's
    market_snapshots table for later enrichment.
    """
    from research.learning_tracker import record_snapshot
    for ticker in tickers:
        data = await _kalshi_get(f"/markets/{ticker}")
        if not data:
            continue
        raw = data.get("market", data)
        yes_p = raw.get("yes_ask") or raw.get("last_price") or 50
        no_p  = raw.get("no_ask")  or (100 - yes_p)
        record_snapshot(
            ticker,
            yes_price=yes_p,
            no_price=no_p,
            open_interest=raw.get("open_interest", 0) or 0,
            volume=raw.get("volume", 0) or 0,
            minutes_remaining=_minutes_remaining(raw),
        )
