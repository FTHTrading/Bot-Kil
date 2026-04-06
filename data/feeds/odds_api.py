"""
The Odds API — Live Odds Feed
==============================
https://the-odds-api.com — Free tier: 500 requests/month.
Pulls live odds from DraftKings, FanDuel, BetMGM, Caesars, etc.
"""
from __future__ import annotations
import os
import httpx
import asyncio
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ODDS_API_KEY", "")
BASE_URL = "https://api.the-odds-api.com/v4"

# Sports codes for The Odds API
SPORTS = {
    "nfl": "americanfootball_nfl",
    "nba": "basketball_nba",
    "mlb": "baseball_mlb",
    "nhl": "icehockey_nhl",
    "epl": "soccer_epl",
    "mls": "soccer_usa_mls",
    "ncaaf": "americanfootball_ncaaf",
    "ncaab": "basketball_ncaab",
    "ufc": "mma_mixed_martial_arts",
}

# Target sportsbooks (ranked by liquidity)
BOOKS = [
    "draftkings",
    "fanduel",
    "betmgm",
    "caesars",
    "pointsbetus",
    "bovada",
    "mybookieag",
]


async def get_odds(
    sport: str,
    markets: str = "h2h,spreads,totals",
    regions: str = "us",
    odds_format: str = "decimal",
) -> list[dict]:
    """
    Fetch live odds for a sport.
    
    Returns list of events with odds from all books.
    """
    sport_key = SPORTS.get(sport.lower(), sport)
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{BASE_URL}/sports/{sport_key}/odds",
            params={
                "apiKey": API_KEY,
                "regions": regions,
                "markets": markets,
                "oddsFormat": odds_format,
                "bookmakers": ",".join(BOOKS),
            }
        )
        resp.raise_for_status()
        data = resp.json()
    
    return _normalize_odds(data, sport)


def _normalize_odds(raw: list[dict], sport: str) -> list[dict]:
    """
    Normalize odds API response to our internal format.
    Output per-game: best available line per outcome across all books.
    """
    games = []
    for event in raw:
        game = {
            "id": event.get("id"),
            "sport": sport.upper(),
            "home_team": event.get("home_team"),
            "away_team": event.get("away_team"),
            "commence_time": event.get("commence_time"),
            "is_live": _is_live(event.get("commence_time", "")),
            "markets": {},
            "best_lines": {},
        }
        
        for bookmaker in event.get("bookmakers", []):
            book_name = bookmaker.get("key")
            for market in bookmaker.get("markets", []):
                mkt_key = market.get("key")  # h2h, spreads, totals
                if mkt_key not in game["markets"]:
                    game["markets"][mkt_key] = {}
                
                for outcome in market.get("outcomes", []):
                    name = outcome.get("name")
                    price = outcome.get("price")
                    point = outcome.get("point")
                    
                    if name not in game["markets"][mkt_key]:
                        game["markets"][mkt_key][name] = []
                    
                    game["markets"][mkt_key][name].append({
                        "book": book_name,
                        "odds": price,
                        "point": point,
                    })
        
        # Find best line per outcome per market
        for mkt, outcomes in game["markets"].items():
            game["best_lines"][mkt] = {}
            for outcome_name, lines in outcomes.items():
                if lines:
                    best = max(lines, key=lambda x: x["odds"])
                    game["best_lines"][mkt][outcome_name] = best
        
        games.append(game)
    
    return games


def _is_live(commence_time: str) -> bool:
    try:
        ct = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        now = datetime.now().astimezone()
        return ct <= now
    except Exception:
        return False


async def get_scores(sport: str, days_from: int = 1) -> list[dict]:
    """Fetch recent scores for result tracking."""
    sport_key = SPORTS.get(sport.lower(), sport)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{BASE_URL}/sports/{sport_key}/scores",
            params={"apiKey": API_KEY, "daysFrom": days_from}
        )
        resp.raise_for_status()
        return resp.json()


async def get_all_sports_odds() -> dict[str, list[dict]]:
    """
    Pull odds for all active sports simultaneously.
    Returns dict keyed by sport name.
    """
    active_sports = ["nba", "mlb", "nfl", "nhl"]
    
    tasks = {sport: get_odds(sport) for sport in active_sports}
    results = {}
    
    for sport, coro in tasks.items():
        try:
            results[sport] = await coro
        except Exception as e:
            results[sport] = []
            print(f"[OddsAPI] Error fetching {sport}: {e}")
    
    return results


async def scan_for_arb_opportunities(bankroll: float = 10_000) -> list[dict]:
    """
    Full arbitrage scan across all sports and books.
    """
    from engine.arbitrage import find_two_way_arb, find_three_way_arb
    
    arbs = []
    all_odds = await get_all_sports_odds()
    
    for sport, games in all_odds.items():
        for game in games:
            h2h = game.get("best_lines", {}).get("h2h", {})
            if not h2h:
                continue
            
            teams = list(h2h.keys())
            if len(teams) == 2:
                odds_a = h2h[teams[0]]["odds"]
                odds_b = h2h[teams[1]]["odds"]
                result = find_two_way_arb(odds_a, odds_b, bankroll * 0.05)
                if result:
                    arbs.append({
                        "sport": sport,
                        "event": f"{game['away_team']} @ {game['home_team']}",
                        "market": "moneyline",
                        "leg_a": {**h2h[teams[0]], "side": teams[0]},
                        "leg_b": {**h2h[teams[1]], "side": teams[1]},
                        **result,
                    })
            
            elif len(teams) == 3:  # soccer with draw
                odds_list = [h2h[t]["odds"] for t in teams]
                result = find_three_way_arb(*odds_list[:3], stake=bankroll * 0.05)
                if result:
                    arbs.append({
                        "sport": sport,
                        "event": f"{game['away_team']} @ {game['home_team']}",
                        "market": "moneyline_3way",
                        **result,
                    })
    
    return sorted(arbs, key=lambda x: x.get("profit_margin_pct", 0), reverse=True)


if __name__ == "__main__":
    async def demo():
        print("[OddsAPI] Fetching NBA odds...")
        games = await get_odds("nba")
        print(f"Found {len(games)} NBA games")
        if games:
            g = games[0]
            print(f"  {g['away_team']} @ {g['home_team']}")
            print(f"  Best lines: {g['best_lines'].get('h2h', {})}")
    
    asyncio.run(demo())
