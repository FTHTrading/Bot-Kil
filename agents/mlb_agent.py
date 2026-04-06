"""
MLB Agent — Baseball Game Outcome Predictor (BGOP)
====================================================
Implements the full BGOP framework from mlb agent metrics.pdf.
Combines sabermetrics, Monte Carlo, and bullpen analysis.
"""
from __future__ import annotations
import asyncio
from typing import Optional
from engine.mlb_metrics import analyze_mlb_matchup, fip, woba, era, bullpen_fatigue_factor
from engine.monte_carlo import mlb_game_sim
from engine.kelly import calculate_kelly, american_to_decimal, profit_machine_split
from engine.ev import calculate_ev, acts_of_god_adjustment


async def analyze_game(
    home_team: str,
    away_team: str,
    home_starter_stats: dict,
    away_starter_stats: dict,
    home_team_stats: dict,
    away_team_stats: dict,
    park_factor: float = 1.00,
    weather: Optional[dict] = None,
    total_line: float = 8.5,
    moneyline_home: Optional[int] = None,
    moneyline_away: Optional[int] = None,
    bankroll: float = 10_000,
) -> dict:
    """
    Full BGOP analysis for a single MLB game.
    
    Starter stats dict: {"fip": 3.45, "era": 3.80, "whip": 1.10, "k9": 9.1, "bb9": 2.3}
    Team stats dict: {"wrc_plus": 115, "ops": 0.780, "bullpen_era": 4.2}
    """
    # Calculate FIP from raw stats if not pre-computed
    h_fip = home_starter_stats.get("fip", home_starter_stats.get("era", 4.00))
    a_fip = away_starter_stats.get("fip", away_starter_stats.get("era", 4.00))
    
    h_wrc = home_team_stats.get("wrc_plus", 100)
    a_wrc = away_team_stats.get("wrc_plus", 100)
    
    h_bull_era = home_team_stats.get("bullpen_era", 4.00)
    a_bull_era = away_team_stats.get("bullpen_era", 4.00)
    
    # Weather adjustments
    temp_f = weather.get("temp_f", 72) if weather else 72
    wind_mph = weather.get("wind_speed", 0) if weather else 0
    wind_out = weather.get("wind_out", False) if weather else False
    
    # Core sabermetric matchup
    matchup = analyze_mlb_matchup(
        home_team=home_team,
        away_team=away_team,
        home_starter_fip=h_fip,
        away_starter_fip=a_fip,
        home_team_wrc_plus=h_wrc,
        away_team_wrc_plus=a_wrc,
        park_factor=park_factor,
        home_bullpen_era=h_bull_era,
        away_bullpen_era=a_bull_era,
        temp_f=temp_f,
        wind_mph=wind_mph,
        wind_out=wind_out,
        total_line=total_line,
    )
    
    # Monte Carlo (50k simulations)
    sim = mlb_game_sim(
        home_era=h_fip,
        away_era=a_fip,
        home_wrc_plus=h_wrc,
        away_wrc_plus=a_wrc,
        park_factor=park_factor,
        wind_mph=wind_mph,
        wind_out=wind_out,
        total_line=total_line,
        n_sims=50_000,
    )
    
    home_win_prob = (matchup.home_win_prob + sim.home_win_prob) / 2
    
    # Kelly sizing for moneyline if provided
    picks = []
    
    if moneyline_home is not None:
        home_dec = american_to_decimal(moneyline_home)
        ev = calculate_ev(home_win_prob, home_dec)
        kelly = calculate_kelly(home_win_prob, home_dec, bankroll, 0.25)
        if kelly.fraction > 0:
            picks.append({
                "pick": home_team,
                "market": "moneyline",
                "american_odds": moneyline_home,
                "our_prob": round(home_win_prob * 100, 1),
                "edge_pct": round(ev.edge * 100, 2),
                "ev_pct": round(ev.ev_pct, 2),
                "stake": kelly.bet_amount,
                "verdict": ev.confidence,
                "split": profit_machine_split(bankroll, "standard"),
            })
    
    if moneyline_away is not None:
        away_win_prob = 1.0 - home_win_prob
        away_dec = american_to_decimal(moneyline_away)
        ev = calculate_ev(away_win_prob, away_dec)
        kelly = calculate_kelly(away_win_prob, away_dec, bankroll, 0.25)
        if kelly.fraction > 0:
            picks.append({
                "pick": away_team,
                "market": "moneyline",
                "american_odds": moneyline_away,
                "our_prob": round(away_win_prob * 100, 1),
                "edge_pct": round(ev.edge * 100, 2),
                "ev_pct": round(ev.ev_pct, 2),
                "stake": kelly.bet_amount,
                "verdict": ev.confidence,
                "split": profit_machine_split(bankroll, "standard"),
            })
    
    # Total bets
    if matchup.edge_over and total_line > 0:
        for direction in ["over", "under"]:
            prob = matchup.edge_over if direction == "over" else matchup.edge_under
            if prob and prob > 0.55:
                picks.append({
                    "pick": f"{direction.upper()} {total_line}",
                    "market": "total",
                    "our_prob": round(prob * 100, 1),
                    "edge_pct": round((prob - 0.52) * 100, 2),
                })
    
    return {
        "game": f"{away_team} @ {home_team}",
        "sabermetrics": {
            "home_starter_fip": h_fip,
            "away_starter_fip": a_fip,
            "home_wrc_plus": h_wrc,
            "away_wrc_plus": a_wrc,
            "park_factor": park_factor,
        },
        "predictions": {
            "home_win_prob": round(home_win_prob * 100, 1),
            "away_win_prob": round((1 - home_win_prob) * 100, 1),
            "predicted_score": f"{matchup.predicted_away_runs:.1f} — {matchup.predicted_home_runs:.1f}",
            "total_predicted": matchup.total_predicted,
            "over_prob": round((matchup.edge_over or 0.5) * 100, 1),
        },
        "monte_carlo": {
            "n_sims": 50_000,
            "home_win_pct": round(sim.home_win_prob * 100, 1),
            "over_pct": round(sim.over_prob * 100, 1),
        },
        "picks": picks,
        "weather": weather,
    }
