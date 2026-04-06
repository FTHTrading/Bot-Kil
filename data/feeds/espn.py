"""
ESPN Unofficial API — Stats & Schedules Feed
=============================================
Uses ESPN's undocumented internal API (no key required).
Provides team stats, schedules, injuries, and scores.
"""
from __future__ import annotations
import httpx
import asyncio
from typing import Optional

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports"

SPORT_PATHS = {
    "nfl": ("football", "nfl"),
    "nba": ("basketball", "nba"),
    "mlb": ("baseball", "mlb"),
    "nhl": ("hockey", "nhl"),
    "ncaaf": ("football", "college-football"),
    "ncaab": ("basketball", "mens-college-basketball"),
}


async def get_schedule(sport: str, limit: int = 25) -> list[dict]:
    """Get upcoming games for a sport from ESPN."""
    s, l = SPORT_PATHS.get(sport.lower(), ("basketball", "nba"))
    url = f"{ESPN_BASE}/{s}/{l}/scoreboard"
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params={"limit": limit})
        resp.raise_for_status()
        data = resp.json()
    
    games = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        
        game = {
            "id": event.get("id"),
            "name": event.get("name"),
            "short_name": event.get("shortName"),
            "date": event.get("date"),
            "status": event.get("status", {}).get("type", {}).get("name"),
            "home_team": home.get("team", {}).get("displayName"),
            "home_abbr": home.get("team", {}).get("abbreviation"),
            "home_score": home.get("score"),
            "away_team": away.get("team", {}).get("displayName"),
            "away_abbr": away.get("team", {}).get("abbreviation"),
            "away_score": away.get("score"),
            "venue": comp.get("venue", {}).get("fullName"),
            "weather": _extract_weather(comp),
        }
        games.append(game)
    
    return games


def _extract_weather(comp: dict) -> Optional[dict]:
    w = comp.get("weather")
    if not w:
        return None
    return {
        "temp_f": w.get("temperature"),
        "condition": w.get("displayValue"),
        "wind_speed": w.get("windSpeed"),
        "wind_direction": w.get("windDirection"),
    }


async def get_team_stats(sport: str, team_id: str) -> dict:
    """Get team statistics."""
    s, l = SPORT_PATHS.get(sport.lower(), ("basketball", "nba"))
    url = f"{ESPN_BASE}/{s}/{l}/teams/{team_id}/statistics"
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def get_injuries(sport: str) -> list[dict]:
    """
    Get current injury report.
    Uses ESPN's injury endpoint.
    """
    s, l = SPORT_PATHS.get(sport.lower(), ("basketball", "nba"))
    url = f"{ESPN_BASE}/{s}/{l}/injuries"
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []
    
    injuries = []
    for player_data in data.get("injuries", []):
        injuries.append({
            "player": player_data.get("athlete", {}).get("displayName"),
            "team": player_data.get("team", {}).get("displayName"),
            "status": player_data.get("status"),
            "type": player_data.get("type"),
            "impact_estimate": _injury_to_prob_impact(player_data.get("status", "")),
        })
    
    return injuries


def _injury_to_prob_impact(status: str) -> float:
    """
    Convert injury status to win probability impact.
    From the 200-page guide: key player injuries = -3 to -8% win probability.
    """
    status_lower = status.lower()
    if "out" in status_lower:
        return -0.06
    elif "doubtful" in status_lower:
        return -0.04
    elif "questionable" in status_lower:
        return -0.02
    elif "probable" in status_lower:
        return -0.005
    return 0.0


async def get_mlb_pitcher_stats(team_abbr: str) -> dict:
    """Get MLB probable pitcher stats from ESPN."""
    url = f"{ESPN_BASE}/baseball/mlb/summary"
    # ESPN doesn't expose this cleanly — we use schedule endpoint
    games = await get_schedule("mlb")
    
    for game in games:
        if game.get("home_abbr") == team_abbr or game.get("away_abbr") == team_abbr:
            return game
    
    return {}


async def get_all_today(sport: str) -> dict:
    """
    Comprehensive daily data pull for a sport:
    schedule + injuries
    """
    schedule, injuries = await asyncio.gather(
        get_schedule(sport),
        get_injuries(sport),
    )
    
    return {
        "sport": sport.upper(),
        "games": schedule,
        "injuries": injuries,
        "injury_count": len(injuries),
    }
