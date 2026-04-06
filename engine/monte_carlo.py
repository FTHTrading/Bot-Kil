"""
Monte Carlo Simulation Engine
==============================
Game simulation for MLB, NBA, NFL, NHL.
Based on the BGOP (Baseball Game Outcome Predictor) and MLB Betting Pro frameworks.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Optional
import random
import math
import statistics


@dataclass
class SimResult:
    home_win_prob: float
    away_win_prob: float
    draw_prob: float
    median_home_score: float
    median_away_score: float
    over_prob: float        # probability total exceeds the line
    spread_cover_prob: float  # probability home covers the spread
    confidence_interval_95: tuple[float, float]
    n_simulations: int


def simulate_game(
    home_score_dist: Callable[[], float],
    away_score_dist: Callable[[], float],
    spread: float = 0.0,
    total_line: float = 0.0,
    n_sims: int = 50_000,
    seed: Optional[int] = None,
) -> SimResult:
    """
    Core Monte Carlo game simulator.
    
    Args:
        home_score_dist: callable returning random home score sample
        away_score_dist: callable returning random away score sample
        spread: point spread (positive = home is favorite)
        total_line: over/under line
        n_sims: number of simulations
    """
    if seed is not None:
        random.seed(seed)
    
    home_wins = 0
    away_wins = 0
    draws = 0
    home_scores = []
    away_scores = []
    overs = 0
    covers = 0
    
    for _ in range(n_sims):
        hs = home_score_dist()
        as_ = away_score_dist()
        home_scores.append(hs)
        away_scores.append(as_)
        
        if hs > as_:
            home_wins += 1
        elif as_ > hs:
            away_wins += 1
        else:
            draws += 1
        
        # Spread cover: home team wins by more than spread
        if spread != 0 and (hs - as_) > spread:
            covers += 1
        
        # Over
        if total_line > 0 and (hs + as_) > total_line:
            overs += 1
    
    home_win_prob = home_wins / n_sims
    margin_samples = [h - a for h, a in zip(home_scores, away_scores)]
    
    # 95% CI on home win probability
    se = math.sqrt(home_win_prob * (1 - home_win_prob) / n_sims)
    ci = (home_win_prob - 1.96 * se, home_win_prob + 1.96 * se)
    
    return SimResult(
        home_win_prob=home_win_prob,
        away_win_prob=away_wins / n_sims,
        draw_prob=draws / n_sims,
        median_home_score=statistics.median(home_scores),
        median_away_score=statistics.median(away_scores),
        over_prob=overs / n_sims if total_line > 0 else 0.0,
        spread_cover_prob=covers / n_sims if spread != 0 else home_win_prob,
        confidence_interval_95=ci,
        n_simulations=n_sims,
    )


# ── Sport-Specific Distributions ─────────────────────────────

def mlb_game_sim(
    home_era: float = 4.00,     # starting pitcher ERA
    away_era: float = 4.00,
    home_wrc_plus: float = 100, # team wRC+
    away_wrc_plus: float = 100,
    park_factor: float = 1.00,  # 1.00 = neutral
    wind_mph: float = 0.0,
    dome: bool = False,
    spread: float = 0.0,
    total_line: float = 8.5,
    n_sims: int = 50_000,
) -> SimResult:
    """
    MLB-specific Monte Carlo using sabermetrics.
    
    Formulas from mlb docs and betting calcs.pdf:
    - FIP-based run expectancy
    - wRC+ offensive adjustment
    - Park factor and weather corrections
    """
    # ERA-to-runs-per-game conversion (9 innings)
    # Adjust for park factor and wind
    wind_bonus = wind_mph * 0.02 if not dome else 0
    
    def home_score_dist():
        # Home team scores based on AWAY pitcher ERA + home offense wRC+
        base_runs = (away_era / 9.0) * (home_wrc_plus / 100.0) * park_factor
        base_runs += wind_bonus * 0.5
        # Negative binomial approximation: use Poisson with slight overdispersion
        mean = max(0.5, base_runs)
        # Add variance (games are overdispersed vs Poisson)
        runs = max(0, int(random.gauss(mean, math.sqrt(mean * 1.3))))
        return runs
    
    def away_score_dist():
        base_runs = (home_era / 9.0) * (away_wrc_plus / 100.0) / park_factor
        base_runs += wind_bonus * 0.3
        mean = max(0.5, base_runs)
        runs = max(0, int(random.gauss(mean, math.sqrt(mean * 1.3))))
        return runs
    
    return simulate_game(home_score_dist, away_score_dist, spread=spread, total_line=total_line, n_sims=n_sims)


def nba_game_sim(
    home_off_rtg: float = 112.0,   # offensive rating (points per 100 possessions)
    home_def_rtg: float = 112.0,
    away_off_rtg: float = 112.0,
    away_def_rtg: float = 112.0,
    home_pace: float = 100.0,
    away_pace: float = 100.0,
    back_to_back_home: bool = False,
    back_to_back_away: bool = False,
    spread: float = 0.0,
    total_line: float = 220.0,
    n_sims: int = 50_000,
) -> SimResult:
    """
    NBA Monte Carlo using pace and efficiency ratings.
    
    From the Gambling.pdf Profit Machine Protocol 2.0:
    - Pace determines possessions per game
    - Off/Def rating determines scoring efficiency
    - Back-to-back fatigue factors applied
    """
    avg_pace = (home_pace + away_pace) / 2.0
    possessions = avg_pace  # possessions per game
    
    # Fatigue adjustment (back-to-backs reduce performance ~3%)
    home_fatigue = 0.97 if back_to_back_home else 1.0
    away_fatigue = 0.97 if back_to_back_away else 1.0
    
    def home_score_dist():
        # Home offense vs away defense
        eff = (home_off_rtg / away_def_rtg) * home_fatigue
        mean_pts = possessions * (home_off_rtg / 100.0) * eff * 0.95  # slight normalization
        mean_pts = max(80, min(150, mean_pts))
        return max(0, random.gauss(mean_pts, 10.0))
    
    def away_score_dist():
        eff = (away_off_rtg / home_def_rtg) * away_fatigue
        mean_pts = possessions * (away_off_rtg / 100.0) * eff * 0.97  # away disadvantage
        mean_pts = max(80, min(150, mean_pts))
        return max(0, random.gauss(mean_pts, 10.0))
    
    return simulate_game(home_score_dist, away_score_dist, spread=spread, total_line=total_line, n_sims=n_sims)


def nfl_game_sim(
    home_dvoa: float = 0.0,     # DVOA (0 = average, positive = better)
    away_dvoa: float = 0.0,
    home_epa: float = 0.0,      # EPA per play
    away_epa: float = 0.0,
    weather_factor: float = 1.0, # 1.0 = neutral, 0.9 = bad weather
    short_week_home: bool = False,
    short_week_away: bool = False,
    spread: float = 0.0,
    total_line: float = 44.5,
    n_sims: int = 50_000,
) -> SimResult:
    """NFL Monte Carlo using DVOA and EPA metrics."""
    
    def home_score_dist():
        # Convert DVOA/EPA to expected points
        base_pts = 21.0  # league average
        dvoa_adj = home_dvoa * 50  # DVOA 10% = +5 pts
        epa_adj = home_epa * 10
        short_week_penalty = -1.5 if short_week_home else 0
        mean_pts = (base_pts + dvoa_adj + epa_adj + short_week_penalty) * weather_factor
        mean_pts = max(6, min(50, mean_pts))
        return max(0, random.gauss(mean_pts, 9.0))
    
    def away_score_dist():
        base_pts = 19.5  # away disadvantage
        dvoa_adj = away_dvoa * 50
        epa_adj = away_epa * 10
        short_week_penalty = -1.5 if short_week_away else 0
        mean_pts = (base_pts + dvoa_adj + epa_adj + short_week_penalty) * weather_factor
        mean_pts = max(6, min(50, mean_pts))
        return max(0, random.gauss(mean_pts, 9.0))
    
    return simulate_game(home_score_dist, away_score_dist, spread=spread, total_line=total_line, n_sims=n_sims)


def nhl_game_sim(
    home_corsi_pct: float = 0.50,   # Corsi For % (possession proxy)
    away_corsi_pct: float = 0.50,
    home_xg: float = 2.5,           # expected goals
    away_xg: float = 2.5,
    home_save_pct: float = 0.913,
    away_save_pct: float = 0.913,
    spread: float = 0.0,
    total_line: float = 5.5,
    n_sims: int = 50_000,
) -> SimResult:
    """NHL Monte Carlo using Corsi, xG, and goalie save percentage."""
    
    def home_score_dist():
        # Shots on goal × (1 - opposing save_pct) × corsi adjustment
        shots_on_goal = home_xg * (home_corsi_pct / 0.50) * 1.2
        goals = max(0, int(random.gauss(shots_on_goal * (1 - away_save_pct), 0.8)))
        return goals
    
    def away_score_dist():
        shots_on_goal = away_xg * (away_corsi_pct / 0.50) * 1.0  # away disadvantage
        goals = max(0, int(random.gauss(shots_on_goal * (1 - home_save_pct), 0.8)))
        return goals
    
    return simulate_game(home_score_dist, away_score_dist, spread=spread, total_line=total_line, n_sims=n_sims)


def z_score_spread_prob(spread: float, expected_diff: float, std_dev: float = 14.0) -> float:
    """
    Z-score method for spread coverage probability.
    From mlb docs and betting calcs.pdf.
    
    Z = (spread - expected_diff) / std_dev
    """
    z = (spread - expected_diff) / std_dev
    # Approximate normal CDF
    return 0.5 * (1 + math.erf(-z / math.sqrt(2)))
