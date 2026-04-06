"""
NBA Agent — Profit Machine Protocol 2.0
=========================================
Implements the full NBA strategy from gambling.pdf.
50/20/20/10 bankroll split with prop targeting.
"""
from __future__ import annotations
from typing import Optional
from engine.monte_carlo import nba_game_sim
from engine.kelly import calculate_kelly, american_to_decimal, profit_machine_split
from engine.ev import calculate_ev, acts_of_god_adjustment


async def analyze_game(
    home_team: str,
    away_team: str,
    home_stats: dict,
    away_stats: dict,
    spread: float = 0.0,
    total_line: float = 220.0,
    moneyline_home: Optional[int] = None,
    moneyline_away: Optional[int] = None,
    bankroll: float = 10_000,
) -> dict:
    """
    Full Profit Machine Protocol 2.0 analysis for NBA.
    
    home_stats: {
        "off_rtg": 115.0, "def_rtg": 110.0, "pace": 101.0,
        "back_to_back": False, 
        "key_player_injured": False, "injury_impact": 0.0
    }
    """
    h_off = home_stats.get("off_rtg", 112.0)
    h_def = home_stats.get("def_rtg", 112.0)
    h_pace = home_stats.get("pace", 100.0)
    h_b2b = home_stats.get("back_to_back", False)
    
    a_off = away_stats.get("off_rtg", 112.0)
    a_def = away_stats.get("def_rtg", 112.0)
    a_pace = away_stats.get("pace", 100.0)
    a_b2b = away_stats.get("back_to_back", False)
    
    # Monte Carlo simulation
    sim = nba_game_sim(
        home_off_rtg=h_off,
        home_def_rtg=h_def,
        away_off_rtg=a_off,
        away_def_rtg=a_def,
        home_pace=h_pace,
        away_pace=a_pace,
        back_to_back_home=h_b2b,
        back_to_back_away=a_b2b,
        spread=spread,
        total_line=total_line,
        n_sims=50_000,
    )
    
    # Apply injury/fatigue adjustments
    home_win_prob = acts_of_god_adjustment(
        sim.home_win_prob,
        travel_impact=-0.03 if h_b2b else 0.0,
        injury_impact=home_stats.get("injury_impact", 0.0),
    )
    
    picks = []
    split = profit_machine_split(bankroll, "high" if abs(home_win_prob - 0.5) > 0.10 else "standard")
    
    # Primary bet (50%)
    if moneyline_home is not None:
        h_dec = american_to_decimal(moneyline_home)
        ev = calculate_ev(home_win_prob, h_dec)
        kelly = calculate_kelly(home_win_prob, h_dec, bankroll, 0.25)
        if kelly.fraction > 0:
            picks.append({
                "leg": "primary_50pct",
                "pick": home_team,
                "market": "moneyline",
                "american_odds": moneyline_home,
                "stake": split["primary_50pct"],
                "our_prob": round(home_win_prob * 100, 1),
                "edge_pct": round(ev.edge * 100, 2),
                "verdict": ev.confidence,
            })
    
    if moneyline_away is not None:
        away_win_prob = 1.0 - home_win_prob
        a_dec = american_to_decimal(moneyline_away)
        ev = calculate_ev(away_win_prob, a_dec)
        kelly = calculate_kelly(away_win_prob, a_dec, bankroll, 0.25)
        if kelly.fraction > 0:
            picks.append({
                "leg": "primary_50pct",
                "pick": away_team,
                "market": "moneyline",
                "american_odds": moneyline_away,
                "stake": split["primary_50pct"],
                "our_prob": round(away_win_prob * 100, 1),
                "edge_pct": round(ev.edge * 100, 2),
                "verdict": ev.confidence,
            })
    
    # Spread cover probability
    spread_prob = sim.spread_cover_prob
    
    # Total direction
    over_prob = sim.over_prob
    total_direction = "over" if over_prob > 0.53 else "under" if over_prob < 0.47 else None
    
    return {
        "game": f"{away_team} @ {home_team}",
        "predictions": {
            "home_win_prob": round(home_win_prob * 100, 1),
            "away_win_prob": round((1 - home_win_prob) * 100, 1),
            "home_cover_spread": round(spread_prob * 100, 1),
            "over_prob": round(over_prob * 100, 1),
            "predicted_home": round(sim.median_home_score, 1),
            "predicted_away": round(sim.median_away_score, 1),
            "total_predicted": round(sim.median_home_score + sim.median_away_score, 1),
            "recommended_total": total_direction,
        },
        "flags": {
            "home_back_to_back": h_b2b,
            "away_back_to_back": a_b2b,
            "high_pace_game": (h_pace + a_pace) / 2 > 102,
            "low_total_risk": over_prob > 0.55 or over_prob < 0.45,
        },
        "profit_machine_split": split,
        "picks": picks,
        "monte_carlo_sims": 50_000,
    }


def get_prop_targets(player_stats: list[dict], matchup_context: dict) -> list[dict]:
    """
    Identify player prop targets using BPM/PIE and matchup data.
    From gambling.pdf: props have 65-75% win rate when targeted correctly.
    
    player_stats: [{"name": "LBJ", "ppg": 27.5, "usage_rate": 0.32, "bpm": 8.2}]
    matchup_context: {"opponent_def_rtg": 115.0, "pace": 102.0}
    """
    targets = []
    opp_def = matchup_context.get("opponent_def_rtg", 112.0)
    
    for player in player_stats:
        ppg = player.get("ppg", 0)
        usage = player.get("usage_rate", 0.20)
        bpm = player.get("bpm", 0)
        
        # High usage vs weak defense = prop target
        def_factor = (opp_def - 110.0) / 10.0  # positive = weak defense
        projected_pts = ppg * (1 + def_factor * 0.10) * (usage / 0.25)
        
        targets.append({
            "player": player["name"],
            "ppg_season": ppg,
            "projected_points": round(projected_pts, 1),
            "confidence": "HIGH" if usage > 0.28 and opp_def > 113 else "MEDIUM",
            "bet_direction": "over" if projected_pts > ppg * 1.05 else "under",
        })
    
    return sorted(targets, key=lambda x: abs(x["projected_points"] - x["ppg_season"]), reverse=True)
