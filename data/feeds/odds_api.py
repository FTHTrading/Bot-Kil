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
    Falls back to a realistic mock slate when ODDS_API_KEY is not configured.
    """
    if not API_KEY or API_KEY == "YOUR_ODDS_API_KEY_HERE":
        print("[OddsAPI] No API key — using built-in daily slate (Apr 6 2026)")
        return _get_mock_slate()

    active_sports = ["nba", "mlb", "nhl", "ncaab"]   # NFL is offseason in April

    tasks = {sport: get_odds(sport) for sport in active_sports}
    results = {}

    for sport, coro in tasks.items():
        try:
            results[sport] = await coro
        except Exception as e:
            results[sport] = []
            print(f"[OddsAPI] Error fetching {sport}: {e}")

    # Fall back to mock on complete failure
    if not any(results.values()):
        print("[OddsAPI] All fetches failed — using mock slate")
        return _get_mock_slate()

    return results


def _make_game(
    sport: str,
    home: str,
    away: str,
    home_ml_dec: float,
    away_ml_dec: float,
    spread_home: float,
    spread_odds: float,
    total: float,
    book: str = "draftkings",
    start_hour: int = 19,
) -> dict:
    """Helper — build a normalised game dict matching _normalize_odds() output format."""
    from datetime import timezone
    now = datetime.now(timezone.utc)
    commence = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    spread_odds_dec = (100 / abs(spread_odds) + 1) if spread_odds < 0 else (spread_odds / 100 + 1)
    return {
        "id": f"mock_{home[:3].lower()}_{away[:3].lower()}",
        "sport": sport.upper(),
        "home_team": home,
        "away_team": away,
        "commence_time": commence.isoformat(),
        "is_live": False,
        "markets": {},
        "best_lines": {
            "h2h": {
                home: {"book": book, "odds": home_ml_dec, "point": None},
                away: {"book": book, "odds": away_ml_dec, "point": None},
            },
            "spreads": {
                home: {"book": book, "odds": spread_odds_dec, "point": spread_home},
                away: {"book": book, "odds": spread_odds_dec, "point": -spread_home},
            },
            "totals": {
                "Over":  {"book": book, "odds": 1.909, "point": total},
                "Under": {"book": book, "odds": 1.909, "point": total},
            },
        },
    }


def _get_mock_slate() -> dict[str, list[dict]]:
    """
    Full Apr 6 2026 daily slate — NBA late regular season, MLB week 2,
    NHL late regular season.  NFL is offseason (draft ~Apr 23-25).

    Odds are decimal format; slight deliberate mispricing on select games
    so the model can identify >= 3 % edge and generate executable picks.
    """
    nba = [
        # Celtics slight mispricing — sharp books show Celtics -152 but market at -138
        _make_game("nba", "Boston Celtics",       "Golden State Warriors",  1.725, 2.18,   -4.5, -110, 214.5, "DraftKings", 20),
        # OKC dominant; Wolves late-season rest
        _make_game("nba", "Minnesota Timberwolves","Oklahoma City Thunder",  2.85,  1.444,  +6.5, -110, 218.0, "FanDuel",    20),
        # Jokic on trail; LA fatigue
        _make_game("nba", "Los Angeles Lakers",   "Denver Nuggets",         2.30,  1.667,  +3.0, -112, 216.5, "BetMGM",     22),
        # Cavs solid road team; NYK slight home book bias
        _make_game("nba", "New York Knicks",       "Cleveland Cavaliers",   2.08,  1.826,  +1.5, -115, 210.0, "Caesars",    19),
    ]

    mlb = [
        # Dodgers undervalued at -148 (true price ~-168) — model edge
        _make_game("mlb", "San Francisco Giants", "Los Angeles Dodgers",    2.55,  1.645,  +1.5, -115,  7.5, "DraftKings", 22),
        # Yankees / Cole; Baltimore open-season sluggish
        _make_game("mlb", "Baltimore Orioles",    "New York Yankees",       2.18,  1.724,  +1.5, -115,  8.5, "FanDuel",    19),
        # Astros road edge; Texas bullpen taxed
        _make_game("mlb", "Texas Rangers",        "Houston Astros",         2.05,  1.847,  +1.5, -118,  9.0, "BetMGM",     20),
        # Even-money Cubs/Cardinals; model likes Cubs rotation depth
        _make_game("mlb", "St. Louis Cardinals",  "Chicago Cubs",           2.02,  1.926,   0.0, -110,  8.5, "Caesars",    20),
        # Braves road value vs Mets early-season shaky pen
        _make_game("mlb", "New York Mets",        "Atlanta Braves",         1.961, 2.05,   +1.5, -118,  8.0, "DraftKings", 19),
    ]

    nhl = [
        # Leafs protecting Wild Card spot; Ottawa eliminated  
        _make_game("nhl", "Ottawa Senators",     "Toronto Maple Leafs",     2.30,  1.724,  +1.5, -115,  5.5, "PointsBet",  19),
        # Tampa home ice; Boston fatigue (3rd road game in 4 nights)
        _make_game("nhl", "Tampa Bay Lightning", "Boston Bruins",           1.847, 2.18,   -1.5, +125,  6.0, "FanDuel",    19),
        # Jets tight race; Calgary road dog value
        _make_game("nhl", "Winnipeg Jets",       "Calgary Flames",          1.781, 2.26,   -1.5, +120,  5.5, "DraftKings", 19),
    ]

    return {"nba": nba, "mlb": mlb, "nhl": nhl}


async def get_player_props(
    sport: str,
    event_id: str,
    prop_markets: str = "player_points,player_rebounds,player_assists,player_threes,player_blocks,player_steals",
) -> list[dict]:
    """
    Fetch player prop markets for a specific event.
    prop_markets: comma-separated Odds API player prop market keys.
    For NFL use: player_pass_yds,player_rush_yds,player_reception_yds,player_anytime_td
    For MLB use: batter_hits,batter_total_bases,pitcher_strikeouts
    For NHL use: player_shots_on_goal,player_goal,player_assists,player_points
    """
    sport_key = SPORTS.get(sport.lower(), sport)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{BASE_URL}/sports/{sport_key}/events/{event_id}/odds",
            params={
                "apiKey": API_KEY,
                "regions": "us",
                "markets": prop_markets,
                "oddsFormat": "decimal",
                "bookmakers": ",".join(BOOKS[:4]),  # top 4 books for props
            }
        )
        resp.raise_for_status()
        return resp.json()


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
