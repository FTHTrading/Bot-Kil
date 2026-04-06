"""
Kalshi Prediction Market Feed
================================
Integrates with Kalshi (trading-api.kalshi.com) to:
1. List active sports markets
2. Get current yes/no probabilities
3. Find cross-platform arbitrage vs sportsbook odds
4. Place/track orders (optional)

Kalshi markets are event contracts: "Will X happen? Yes/No"
They trade as cents (0–100 cents = 0–100% probability)
This lets us cross-reference against sportsbook implied probs for edge.

API Reference: https://trading-api.kalshi.com/trade-api/v2
Auth: RSA key-based (API key + private key) or simple key auth depending on tier
"""
from __future__ import annotations
import os
import json
import asyncio
from typing import Optional
from datetime import datetime
import httpx


# ─── Config ───────────────────────────────────────────────────────────────────

KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://trading-api.kalshi.com/trade-api/v2")
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
KALSHI_API_SECRET = os.getenv("KALSHI_API_SECRET", "")

# Sports ticker prefixes on Kalshi
SPORTS_PREFIXES = [
    "NFL", "NBA", "MLB", "NHL", "NCAAF", "NCAAB", "MLS", "SOCCER"
]


# ─── HTTP Client ──────────────────────────────────────────────────────────────

def _get_headers() -> dict:
    """
    Kalshi uses simple API key authentication for demo/basic tier.
    For production RSA-signed requests, extend this.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if KALSHI_API_KEY:
        headers["Authorization"] = f"Bearer {KALSHI_API_KEY}"
    return headers


# ─── Market Fetching ──────────────────────────────────────────────────────────

async def get_active_markets(
    category: str = "sports",
    status: str = "open",
    limit: int = 200,
) -> list[dict]:
    """
    Fetch all active Kalshi markets matching category.
    Returns list of market objects with yes_bid, yes_ask, no_bid, no_ask.
    """
    if not KALSHI_API_KEY:
        return _mock_kalshi_markets()

    params = {
        "status": status,
        "limit": limit,
    }
    if category:
        params["category"] = category

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{KALSHI_BASE_URL}/markets",
                headers=_get_headers(),
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("markets", [])
    except Exception as e:
        print(f"[Kalshi] Error fetching markets: {e}")
        return _mock_kalshi_markets()


async def get_market(ticker: str) -> Optional[dict]:
    """Fetch a specific market by ticker (e.g. 'NFL-2024-CHIEFS-WIN')."""
    if not KALSHI_API_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{KALSHI_BASE_URL}/markets/{ticker}",
                headers=_get_headers(),
            )
            resp.raise_for_status()
            return resp.json().get("market")
    except Exception as e:
        print(f"[Kalshi] Error fetching market {ticker}: {e}")
        return None


async def get_market_orderbook(ticker: str) -> Optional[dict]:
    """
    Get the order book (best bid/ask) for a market.
    Returns yes_bid, yes_ask, no_bid, no_ask in cents (1–99).
    """
    if not KALSHI_API_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook",
                headers=_get_headers(),
            )
            resp.raise_for_status()
            book = resp.json().get("orderbook", {})
            return book
    except Exception as e:
        print(f"[Kalshi] Error fetching orderbook {ticker}: {e}")
        return None


# ─── Account / Portfolio ──────────────────────────────────────────────────────

async def get_portfolio() -> dict:
    """Get current positions and open orders."""
    if not KALSHI_API_KEY:
        return {"positions": [], "open_orders": []}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{KALSHI_BASE_URL}/portfolio/positions",
                headers=_get_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        print(f"[Kalshi] Error fetching portfolio: {e}")
        return {"positions": [], "open_orders": []}


async def get_balance() -> float:
    """Get available Kalshi balance in USD."""
    if not KALSHI_API_KEY:
        return 0.0

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{KALSHI_BASE_URL}/portfolio/balance",
                headers=_get_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            # Balance returned in cents
            return data.get("balance", 0) / 100.0
    except Exception as e:
        print(f"[Kalshi] Error fetching balance: {e}")
        return 0.0


async def place_order(
    ticker:    str,
    side:      str,     # "yes" or "no"
    count:     int,     # number of contracts ($0.01 each at 100 cent payout)
    yes_price: int,     # price in cents (1-99)
    action:    str = "buy",
    order_type: str = "limit",
) -> dict:
    """
    Place a limit order on Kalshi.

    Each contract pays $1 if correct, costs yes_price cents.
    side="yes"  → backing the event to happen
    side="no"   → backing the event NOT to happen (pays 100-yes_price cents)

    Kalshi API: POST /portfolio/orders
    """
    if not KALSHI_API_KEY:
        return {"error": "KALSHI_API_KEY not configured"}

    body: dict = {
        "ticker":     ticker,
        "action":     action,
        "side":       side,
        "type":       order_type,
        "count":      count,
        "yes_price":  yes_price,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{KALSHI_BASE_URL}/portfolio/orders",
                headers=_get_headers(),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code}: {exc.response.text}"}
    except Exception as exc:
        return {"error": str(exc)}


async def cancel_order(order_id: str) -> dict:
    """Cancel an open Kalshi order by ID."""
    if not KALSHI_API_KEY:
        return {"error": "KALSHI_API_KEY not configured"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                f"{KALSHI_BASE_URL}/portfolio/orders/{order_id}",
                headers=_get_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": str(exc)}


async def get_orders(status: str = "resting") -> list[dict]:
    """
    List Kalshi orders.
    status: "resting" (open), "canceled", "executed", "all"
    """
    if not KALSHI_API_KEY:
        return []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{KALSHI_BASE_URL}/portfolio/orders",
                headers=_get_headers(),
                params={"status": status, "limit": 100},
            )
            resp.raise_for_status()
            return resp.json().get("orders", [])
    except Exception as exc:
        print(f"[Kalshi] Error fetching orders: {exc}")
        return []


async def get_settlements() -> list[dict]:
    """
    Get settled/filled order history for P&L tracking.
    """
    if not KALSHI_API_KEY:
        return []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{KALSHI_BASE_URL}/portfolio/settlements",
                headers=_get_headers(),
                params={"limit": 200},
            )
            resp.raise_for_status()
            return resp.json().get("settlements", [])
    except Exception as exc:
        print(f"[Kalshi] Error fetching settlements: {exc}")
        return []


# ─── Cross-Market Arbitrage (Kalshi vs Sportsbook) ───────────────────────────

def _kalshi_to_american_odds(yes_price_cents: float) -> int:
    """
    Convert Kalshi yes price (1–99 cents = probability %) to American odds.
    Kalshi: 60 cents means ~60% implied probability.
    """
    prob = yes_price_cents / 100.0
    if prob <= 0 or prob >= 1:
        return 0
    if prob > 0.5:
        # Favorite
        odds = -round((prob / (1 - prob)) * 100)
    else:
        # Underdog
        odds = round(((1 - prob) / prob) * 100)
    return odds


def _american_to_prob(american_odds: int) -> float:
    """Convert American odds to implied probability (WITH vig)."""
    if american_odds > 0:
        return 100 / (american_odds + 100)
    else:
        return abs(american_odds) / (abs(american_odds) + 100)


def find_kalshi_arb(
    kalshi_markets: list[dict],
    sportsbook_games: list[dict],
    min_profit_pct: float = 0.01,
) -> list[dict]:
    """
    Cross-reference Kalshi game winner markets vs sportsbook moneylines.
    
    If Kalshi yes price implies different probability than sportsbook,
    you can construct a guaranteed profit:
    - Buy YES on Kalshi if implied prob < sportsbook's implied probability
    - Back team on sportsbook if Kalshi overstates probability
    
    Returns list of arb opportunities with instructions.
    """
    arbs = []

    for market in kalshi_markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "").lower()

        # Try to match Kalshi market to a sportsbook game
        for game in sportsbook_games:
            home = game.get("home_team", "").lower()
            away = game.get("away_team", "").lower()

            # Basic fuzzy match
            matched_team = None
            side = None
            if home in title or any(word in title for word in home.split()):
                matched_team = game.get("home_team")
                side = "home"
            elif away in title or any(word in title for word in away.split()):
                matched_team = game.get("away_team")
                side = "away"

            if not matched_team:
                continue

            # Kalshi yes price (cents = probability)
            yes_bid = market.get("yes_bid", 0)
            yes_ask = market.get("yes_ask", 0)
            if not yes_ask:
                continue

            kalshi_yes_prob = yes_ask / 100.0  # Cost to buy YES

            # Find sportsbook odds for matched team
            book_odds = None
            if side == "home":
                for bookmaker in game.get("bookmakers", []):
                    for mkt in bookmaker.get("markets", []):
                        if mkt.get("key") == "h2h":
                            outcomes = mkt.get("outcomes", [])
                            for o in outcomes:
                                if o.get("name", "").lower() == home:
                                    book_odds = o.get("price")
                                    break
            elif side == "away":
                for bookmaker in game.get("bookmakers", []):
                    for mkt in bookmaker.get("markets", []):
                        if mkt.get("key") == "h2h":
                            outcomes = mkt.get("outcomes", [])
                            for o in outcomes:
                                if o.get("name", "").lower() == away:
                                    book_odds = o.get("price")
                                    break

            if not book_odds:
                continue

            # book_odds is decimal, convert to implied prob
            book_implied_prob = 1 / book_odds

            # Arb: if Kalshi overstates probability (Yes too cheap vs sportsbook)
            # Buy YES on Kalshi + Bet against on sportsbook
            prob_diff = book_implied_prob - kalshi_yes_prob

            if abs(prob_diff) >= min_profit_pct:
                arbs.append({
                    "type": "kalshi_vs_sportsbook",
                    "market_title": market.get("title"),
                    "ticker": ticker,
                    "matched_team": matched_team,
                    "event": f"{game.get('away_team')} @ {game.get('home_team')}",
                    "kalshi_yes_prob": round(kalshi_yes_prob, 4),
                    "book_implied_prob": round(book_implied_prob, 4),
                    "prob_diff_pct": round(prob_diff * 100, 2),
                    "action": "BUY YES on Kalshi" if prob_diff > 0 else "BUY NO on Kalshi",
                    "kalshi_yes_price_cents": yes_ask,
                    "sportsbook_decimal_odds": book_odds,
                    "potential_edge_pct": round(abs(prob_diff) * 100, 2),
                })

    arbs.sort(key=lambda x: abs(x["potential_edge_pct"]), reverse=True)
    return arbs


# ─── Normalize Kalshi Market ──────────────────────────────────────────────────

def normalize_kalshi_market(market: dict) -> dict:
    """
    Convert raw Kalshi API response into standardized format
    consistent with the rest of the feeds.
    """
    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)
    yes_bid = market.get("yes_bid", 0)

    yes_prob = yes_ask / 100.0 if yes_ask else 0.0
    no_prob = no_ask / 100.0 if no_ask else 0.0

    # Mid price
    yes_mid = (yes_bid + yes_ask) / 2 / 100.0 if (yes_bid and yes_ask) else yes_prob

    return {
        "source": "kalshi",
        "ticker": market.get("ticker"),
        "title": market.get("title"),
        "category": market.get("category"),
        "status": market.get("status"),
        "yes_prob": round(yes_prob, 4),
        "no_prob": round(no_prob, 4),
        "yes_mid_prob": round(yes_mid, 4),
        "yes_american_odds": _kalshi_to_american_odds(yes_ask),
        "no_american_odds": _kalshi_to_american_odds(no_ask),
        "close_time": market.get("close_time"),
        "volume": market.get("volume", 0),
        "open_interest": market.get("open_interest", 0),
        "liquidity": market.get("liquidity", 0),
    }


# ─── Sports-Specific Market Filters ──────────────────────────────────────────

async def get_sports_markets_today() -> list[dict]:
    """
    Fetch all open sports markets on Kalshi, normalized.
    Filters to games closing today (intraday markets).
    """
    markets = await get_active_markets(category="sports")
    normalized = [normalize_kalshi_market(m) for m in markets]

    today = datetime.utcnow().date()
    filtered = []
    for m in normalized:
        if m.get("status") != "open":
            continue
        close_time = m.get("close_time")
        if close_time:
            try:
                close_date = datetime.fromisoformat(close_time.replace("Z", "+00:00")).date()
                if close_date == today:
                    filtered.append(m)
            except Exception:
                filtered.append(m)  # include if can't parse
        else:
            filtered.append(m)

    return filtered


# ─── Mock Data (when no API key) ─────────────────────────────────────────────

def _mock_kalshi_markets() -> list[dict]:
    """
    Apr 6 2026 daily Kalshi market slate.
    NBA late regular season, MLB week 2, NHL end-of-season push.
    NFL is offseason (draft ~Apr 23-25) — no NFL markets.
    yes_bid/ask are cents (= %) matching approximate sportsbook implied probs.
    """
    close = datetime.utcnow().strftime("%Y-%m-%dT23:59:00Z")
    return [
        # ── NBA ────────────────────────────────────────────────────────────────
        {
            "ticker": "NBA-2026-CELTICS-WIN-0406",
            "title": "Will the Celtics win vs the Warriors tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 57, "yes_ask": 59,   # ~58 % = Celtics -138 ML
            "no_bid": 41, "no_ask": 43,
            "volume": 3120000, "open_interest": 710000, "liquidity": 1140000,
            "close_time": close,
        },
        {
            "ticker": "NBA-2026-THUNDER-WIN-0406",
            "title": "Will the Thunder win vs the Timberwolves tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 65, "yes_ask": 67,   # ~66 % = Thunder -200 ML
            "no_bid": 33, "no_ask": 35,
            "volume": 2780000, "open_interest": 620000, "liquidity": 980000,
            "close_time": close,
        },
        {
            "ticker": "NBA-2026-NUGGETS-WIN-0406",
            "title": "Will the Nuggets win at the Lakers tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 58, "yes_ask": 60,   # ~59 % = Nuggets -145 ML
            "no_bid": 40, "no_ask": 42,
            "volume": 2460000, "open_interest": 550000, "liquidity": 870000,
            "close_time": close,
        },
        {
            "ticker": "NBA-2026-CAVALIERS-WIN-0406",
            "title": "Will the Cavaliers win at the Knicks tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 59, "yes_ask": 61,   # ~60 % = Cavs -150 ML
            "no_bid": 39, "no_ask": 41,
            "volume": 1940000, "open_interest": 430000, "liquidity": 690000,
            "close_time": close,
        },
        # ── MLB ────────────────────────────────────────────────────────────────
        {
            "ticker": "MLB-2026-DODGERS-WIN-0406",
            "title": "Will the Dodgers win at the Giants tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 60, "yes_ask": 62,   # ~61 % = Dodgers -155 ML
            "no_bid": 38, "no_ask": 40,
            "volume": 3840000, "open_interest": 870000, "liquidity": 1420000,
            "close_time": close,
        },
        {
            "ticker": "MLB-2026-YANKEES-WIN-0406",
            "title": "Will the Yankees win vs the Orioles tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 57, "yes_ask": 59,   # ~58 % = Yankees -138 ML
            "no_bid": 41, "no_ask": 43,
            "volume": 2910000, "open_interest": 640000, "liquidity": 1030000,
            "close_time": close,
        },
        {
            "ticker": "MLB-2026-ASTROS-WIN-0406",
            "title": "Will the Astros win at the Rangers tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 53, "yes_ask": 55,   # ~54 % = Astros -118 ML
            "no_bid": 45, "no_ask": 47,
            "volume": 1680000, "open_interest": 370000, "liquidity": 590000,
            "close_time": close,
        },
        {
            "ticker": "MLB-2026-CUBS-WIN-0406",
            "title": "Will the Cubs win at the Cardinals tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 51, "yes_ask": 53,   # ~52 % = Cubs -108 ML (near even)
            "no_bid": 47, "no_ask": 49,
            "volume": 1320000, "open_interest": 290000, "liquidity": 460000,
            "close_time": close,
        },
        {
            "ticker": "MLB-2026-BRAVES-WIN-0406",
            "title": "Will the Braves win at the Mets tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 50, "yes_ask": 52,   # ~51 % = Braves +104 ML
            "no_bid": 48, "no_ask": 50,
            "volume": 1850000, "open_interest": 420000, "liquidity": 680000,
            "close_time": close,
        },
        # ── NHL ────────────────────────────────────────────────────────────────
        {
            "ticker": "NHL-2026-LEAFS-WIN-0406",
            "title": "Will the Maple Leafs win at the Senators tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 57, "yes_ask": 59,   # ~58 % = Leafs -138 ML
            "no_bid": 41, "no_ask": 43,
            "volume": 1160000, "open_interest": 250000, "liquidity": 410000,
            "close_time": close,
        },
        {
            "ticker": "NHL-2026-LIGHTNING-WIN-0406",
            "title": "Will the Lightning win vs the Bruins tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 53, "yes_ask": 55,   # ~54 % = Tampa -118 ML
            "no_bid": 45, "no_ask": 47,
            "volume": 980000, "open_interest": 210000, "liquidity": 340000,
            "close_time": close,
        },
        {
            "ticker": "NHL-2026-JETS-WIN-0406",
            "title": "Will the Jets win vs the Flames tonight? (Apr 6)",
            "category": "sports",
            "status": "open",
            "yes_bid": 55, "yes_ask": 57,   # ~56 % = Jets -128 ML
            "no_bid": 43, "no_ask": 45,
            "volume": 820000, "open_interest": 180000, "liquidity": 290000,
            "close_time": close,
        },
    ]


# ─── Quick Test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def main():
        print("Fetching Kalshi sports markets...")
        markets = await get_active_markets()
        print(f"Found {len(markets)} markets")
        for m in markets[:5]:
            norm = normalize_kalshi_market(m)
            print(f"  {norm['ticker']} | Yes: {norm['yes_prob']:.0%} ({norm['yes_american_odds']:+d}) | Vol: {norm['volume']}")

    asyncio.run(main())
