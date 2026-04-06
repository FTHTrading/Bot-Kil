"""
MLB Sabermetrics Engine
========================
All formulas from mlb docs and betting calcs.pdf + mlb agent metrics.pdf.
Covers: BA, OBP, SLG, wOBA, FIP, ERA, WHIP, WAR, wRC+, park factors, bullpen fatigue.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import math


# ── Batting Metrics ────────────────────────────────────────────────────────

def batting_average(hits: int, at_bats: int) -> float:
    """BA = H / AB"""
    return hits / at_bats if at_bats > 0 else 0.0


def on_base_percentage(hits: int, walks: int, hbp: int, at_bats: int, sac_flies: int) -> float:
    """OBP = (H + BB + HBP) / (AB + BB + HBP + SF)"""
    numerator = hits + walks + hbp
    denominator = at_bats + walks + hbp + sac_flies
    return numerator / denominator if denominator > 0 else 0.0


def slugging_percentage(singles: int, doubles: int, triples: int, hr: int, at_bats: int) -> float:
    """SLG = (1B + 2×2B + 3×3B + 4×HR) / AB"""
    total_bases = singles + 2 * doubles + 3 * triples + 4 * hr
    return total_bases / at_bats if at_bats > 0 else 0.0


def ops(obp: float, slg: float) -> float:
    """OPS = OBP + SLG"""
    return obp + slg


def woba(
    ubb: int,      # unintentional walks
    hbp: int,
    singles: int,
    doubles: int,
    triples: int,
    hr: int,
    ab: int,
    bb: int,       # all walks
    ibb: int,      # intentional walks
    sf: int,
) -> float:
    """
    Weighted On-Base Average (wOBA) — from mlb docs and betting calcs.pdf
    
    wOBA = (0.69×uBB + 0.72×HBP + 0.89×1B + 1.27×2B + 1.62×3B + 2.10×HR)
           / (AB + BB - IBB + SF + HBP)
    """
    numerator = (0.69 * ubb + 0.72 * hbp + 0.89 * singles +
                 1.27 * doubles + 1.62 * triples + 2.10 * hr)
    denominator = ab + bb - ibb + sf + hbp
    return numerator / denominator if denominator > 0 else 0.0


# ── Pitching Metrics ────────────────────────────────────────────────────────

def era(earned_runs: int, innings_pitched: float) -> float:
    """ERA = (ER / IP) × 9"""
    return (earned_runs / innings_pitched * 9) if innings_pitched > 0 else 0.0


def whip(walks: int, hits: int, innings_pitched: float) -> float:
    """WHIP = (BB + H) / IP"""
    return (walks + hits) / innings_pitched if innings_pitched > 0 else 0.0


def fip(
    home_runs: int,
    walks: int,
    hbp: int,
    strikeouts: int,
    innings_pitched: float,
    fip_constant: float = 3.10,   # league-average FIP constant (approx 2024)
) -> float:
    """
    Fielding Independent Pitching (FIP)
    FIP = (13×HR + 3×(BB+HBP) - 2×K) / IP + FIP_constant
    """
    if innings_pitched <= 0:
        return 0.0
    return (13 * home_runs + 3 * (walks + hbp) - 2 * strikeouts) / innings_pitched + fip_constant


def xfip(
    fly_balls: int,
    walks: int,
    hbp: int,
    strikeouts: int,
    innings_pitched: float,
    lg_hr_fb_rate: float = 0.103,  # league-average HR/FB rate
    fip_constant: float = 3.10,
) -> float:
    """
    Expected FIP — normalizes HR using league-average HR/FB rate.
    More stable predictor than FIP alone.
    """
    expected_hr = fly_balls * lg_hr_fb_rate
    return fip(int(expected_hr), walks, hbp, strikeouts, innings_pitched, fip_constant)


def babip(hits: int, hr: int, at_bats: int, strikeouts: int, sac_flies: int) -> float:
    """
    Batting Average on Balls In Play
    BABIP = (H - HR) / (AB - K - HR + SF)
    """
    numerator = hits - hr
    denominator = at_bats - strikeouts - hr + sac_flies
    return numerator / denominator if denominator > 0 else 0.0


# ── Advanced Metrics ────────────────────────────────────────────────────────

def wrc_plus(wraa: float, lg_r_per_pa: float, pa: int, lg_r: float, park_factor: float = 1.0) -> float:
    """
    Weighted Runs Created Plus (wRC+)
    100 = league average, 150 = 50% above average
    
    wRC+ = ((wRAA/PA + lgR/PA + (lgR - lgR × PF)) / lgR) × 100
    Simplified version that scales to 100.
    """
    if pa <= 0 or lg_r <= 0:
        return 100.0
    
    # Simplified approximation
    wrc_raw = wraa / pa + lg_r_per_pa
    wrc_adjusted = wrc_raw / (lg_r_per_pa * park_factor)
    return wrc_adjusted * 100


def park_factor_adjusted(raw_stat: float, park_factor: float) -> float:
    """Adjust any rate stat for park factor."""
    return raw_stat / park_factor if park_factor > 0 else raw_stat


# ── Game-Level Prediction ────────────────────────────────────────────────────

@dataclass
class MLBMatchupAnalysis:
    home_team: str
    away_team: str
    home_starter_fip: float
    away_starter_fip: float
    home_wrc_plus: float
    away_wrc_plus: float
    park_factor: float
    home_bullpen_era: float
    away_bullpen_era: float
    weather_adjustment: float
    predicted_home_runs: float
    predicted_away_runs: float
    home_win_prob: float
    total_predicted: float
    edge_over: Optional[float]    # probability total goes over given line
    edge_under: Optional[float]


def analyze_mlb_matchup(
    home_team: str,
    away_team: str,
    home_starter_fip: float,
    away_starter_fip: float,
    home_team_wrc_plus: float,
    away_team_wrc_plus: float,
    park_factor: float = 1.00,
    home_bullpen_era: float = 4.00,
    away_bullpen_era: float = 4.00,
    temp_f: float = 72.0,
    wind_mph: float = 0.0,
    wind_out: bool = False,       # wind blowing out = more runs
    total_line: float = 8.5,
) -> MLBMatchupAnalysis:
    """
    Full MLB game matchup analysis combining sabermetrics.
    """
    # Temperature adjustment (every 10°F above 75 = ~+0.3 runs)
    temp_adj = (temp_f - 75.0) / 10.0 * 0.3
    
    # Wind adjustment
    wind_adj = wind_mph * 0.04 if wind_out else -wind_mph * 0.02
    
    # Predicted runs for starter portion (6 IP average)
    starter_ip = 6.0
    relief_ip = 3.0
    
    # Home team runs = away pitcher vulnerability × home offense × park
    home_starter_era_equiv = home_starter_fip  # treat FIP as expected ERA
    away_starter_era_equiv = away_starter_fip
    
    home_runs_starter = (away_starter_era_equiv / 9.0 * starter_ip) * (home_team_wrc_plus / 100.0) * park_factor
    home_runs_bullpen = (away_bullpen_era / 9.0 * relief_ip) * (home_team_wrc_plus / 100.0) * park_factor
    home_runs_predicted = home_runs_starter + home_runs_bullpen + temp_adj + wind_adj
    
    away_runs_starter = (home_starter_era_equiv / 9.0 * starter_ip) * (away_team_wrc_plus / 100.0) / park_factor
    away_runs_bullpen = (home_bullpen_era / 9.0 * relief_ip) * (away_team_wrc_plus / 100.0) / park_factor
    away_runs_predicted = away_runs_starter + away_runs_bullpen + temp_adj * 0.8 + wind_adj * 0.8
    
    home_runs_predicted = max(1.0, home_runs_predicted)
    away_runs_predicted = max(1.0, away_runs_predicted)
    
    total_predicted = home_runs_predicted + away_runs_predicted
    
    # Win probability from run differential
    run_diff = home_runs_predicted - away_runs_predicted
    # Empirical: each 1 run = ~12% win prob shift from 50%
    home_win_prob = 0.50 + (run_diff * 0.12)
    home_win_prob = max(0.10, min(0.90, home_win_prob))
    
    # Total edge
    edge_over = None
    edge_under = None
    if total_line > 0:
        # Simple logistic probability around the total
        diff = total_predicted - total_line
        prob_over = 1 / (1 + math.exp(-diff * 0.4))
        edge_over = prob_over
        edge_under = 1.0 - prob_over
    
    return MLBMatchupAnalysis(
        home_team=home_team,
        away_team=away_team,
        home_starter_fip=home_starter_fip,
        away_starter_fip=away_starter_fip,
        home_wrc_plus=home_team_wrc_plus,
        away_wrc_plus=away_team_wrc_plus,
        park_factor=park_factor,
        home_bullpen_era=home_bullpen_era,
        away_bullpen_era=away_bullpen_era,
        weather_adjustment=temp_adj + wind_adj,
        predicted_home_runs=round(home_runs_predicted, 2),
        predicted_away_runs=round(away_runs_predicted, 2),
        home_win_prob=round(home_win_prob, 4),
        total_predicted=round(total_predicted, 2),
        edge_over=round(edge_over, 4) if edge_over else None,
        edge_under=round(edge_under, 4) if edge_under else None,
    )


# ── Bullpen Fatigue ────────────────────────────────────────────────────────

def bullpen_fatigue_factor(pitchers_used_yesterday: int, total_pitches_yesterday: int) -> float:
    """
    Estimate bullpen fatigue. Returns multiplier:
    1.0 = fresh, 0.85 = heavily used, 0.75 = exhausted.
    """
    fatigue = 1.0 - (pitchers_used_yesterday * 0.02) - (total_pitches_yesterday / 1000)
    return max(0.75, min(1.0, fatigue))
