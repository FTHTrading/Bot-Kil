"""
NFL Agent — DVOA / EPA / Next Gen Stats Analyzer
==================================================
Based on ai_betting.pdf and betting69.pdf:
- DVOA (Defense-adjusted Value Over Average)
- EPA per play (Expected Points Added)
- Success rate, yards per play
- Turnover differential
- Red zone efficiency
- Weather adjustments (QB accuracy, rushing vs passing)
- Short week / divisional game factors
- Home field advantage quantification (~2.5 pts)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.kelly import calculate_kelly, american_to_decimal
from engine.ev import calculate_ev, acts_of_god_adjustment
from engine.monte_carlo import nfl_game_sim


# ─── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class NFLTeamStats:
    name: str
    # DVOA (percentage, positive = better than average)
    off_dvoa: float          # Offensive DVOA (e.g. +15.2)
    def_dvoa: float          # Defensive DVOA (negative = better, e.g. -8.4)
    special_teams_dvoa: float = 0.0  # Usually small, +/-5
    # EPA per play
    off_epa_per_play: float = 0.0    # e.g. 0.12 (good offense)
    def_epa_per_play: float = 0.0    # Negative = stout defense
    # Efficiency stats
    yards_per_play: float = 5.5
    pass_yards_per_attempt: float = 7.0
    rush_yards_per_attempt: float = 4.3
    success_rate: float = 0.48       # % of plays gaining >= 40%/60%/90% req yards
    # Scoring
    points_per_game: float = 23.0
    points_allowed_per_game: float = 23.0
    # Turnover differential (positive = good)
    turnover_diff: int = 0
    # Red zone
    red_zone_td_pct: float = 0.56
    red_zone_def_td_pct: float = 0.56
    # Situational
    third_down_conv_rate: float = 0.40
    third_down_def_rate: float = 0.40
    # Injuries (accumulated impact)
    key_player_injury_impact: float = 0.0   # 0.0 = healthy, up to 0.20 = major starter out
    # Record
    wins: int = 5
    losses: int = 5
    home_record_wins: int = 3
    # Streak
    current_streak: int = 0   # positive = win streak, negative = losing streak


@dataclass
class NFLGameContext:
    """Environmental and situational factors."""
    home_team: str
    away_team: str
    spread: float            # Vegas spread (negative = home favorite, e.g. -3.5)
    total_line: float        # Over/under total
    home_moneyline: int      # American odds
    away_moneyline: int
    # Weather
    temp_f: float = 70.0
    wind_mph: float = 5.0
    precipitation: bool = False
    outdoor_stadium: bool = True
    # Schedule context
    home_short_week: bool = False   # Thursday night game
    away_short_week: bool = False
    divisional_game: bool = False
    playoff_implications: bool = False
    # Rest days
    home_rest_days: int = 7
    away_rest_days: int = 7


@dataclass
class NFLPickResult:
    event: str
    spread_pick: Optional[str]
    spread_edge_pct: float
    moneyline_pick: Optional[str]
    ml_edge_pct: float
    total_pick: Optional[str]           # "Over" or "Under"
    total_edge_pct: float
    sim_home_win_prob: float
    sim_avg_total: float
    sim_spread_cover_prob: float        # Prob home covers spread
    recommended_bets: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ─── Core Analysis ─────────────────────────────────────────────────────────────

class NFLAgent:
    """
    Analyzes NFL games using DVOA, EPA, Monte Carlo simulation.
    Outputs spread / ML / total picks with Kelly-sized stakes.
    """

    def __init__(self, bankroll: float = 10000, kelly_multiplier: float = 0.25,
                 min_edge: float = 0.03):
        self.bankroll = bankroll
        self.kelly_multiplier = kelly_multiplier
        self.min_edge = min_edge

    # ── Home field advantage ──────────────────────────────────────────────────

    def _home_field_advantage(self, context: NFLGameContext) -> float:
        """
        Empirical NFL home field advantage ~2.5 points (pre-COVID era).
        Post-COVID average dropped to ~1.8 pts in some analyses.
        Divisional games: slight bump (+0.3 pts) — crowd familiarity.
        """
        base_hfa = 2.2  # current era estimate
        if context.divisional_game:
            base_hfa += 0.3
        # Dome stadiums — visitor noise disadvantage is smaller
        if not context.outdoor_stadium:
            base_hfa -= 0.4
        return base_hfa

    # ── Expected points from DVOA ─────────────────────────────────────────────

    def _dvoa_to_expected_points(self, off_dvoa: float, def_dvoa_opp: float,
                                  league_avg_ppg: float = 23.0) -> float:
        """
        Convert DVOA matchup into expected points.
        DVOA is percentage relative to average (0 = average).
        +10 off_dvoa means 10% better than average offense.
        -8 def_dvoa means 8% better than average defense (negative is better for D).
        """
        # Each 1 DVOA point ≈ 0.18 pts per game (rough empirical from betlabs data)
        dvoa_scale = 0.18
        expected = league_avg_ppg + (off_dvoa * dvoa_scale) + (def_dvoa_opp * dvoa_scale)
        return max(3, expected)  # floor at 3 (safety or FG minimum)

    # ── Weather factor ────────────────────────────────────────────────────────

    def _weather_adjustment(self, context: NFLGameContext) -> tuple[float, float]:
        """
        Returns (pass_penalty, rush_bonus) as probability adjustments.
        Cold + wind dramatically depresses passing game and total.
        """
        if not context.outdoor_stadium:
            return 0.0, 0.0   # dome game, no weather

        pass_penalty = 0.0
        rush_bonus = 0.0

        # Wind impact on passing (from betting69.pdf: >15mph = significant)
        if context.wind_mph > 20:
            pass_penalty += 0.08
            rush_bonus += 0.04
        elif context.wind_mph > 15:
            pass_penalty += 0.05
            rush_bonus += 0.02
        elif context.wind_mph > 10:
            pass_penalty += 0.02

        # Cold weather: <32°F impacts QB arm, ball grip
        if context.temp_f < 20:
            pass_penalty += 0.06
        elif context.temp_f < 32:
            pass_penalty += 0.04
        elif context.temp_f < 45:
            pass_penalty += 0.02

        # Precipitation (rain/snow)
        if context.precipitation:
            pass_penalty += 0.05
            rush_bonus += 0.02

        return pass_penalty, rush_bonus

    # ── Short week penalty ────────────────────────────────────────────────────

    def _short_week_adjustment(self, context: NFLGameContext) -> tuple[float, float]:
        """
        Thursday Night Football: teams on short week (4 days rest) lose sharply.
        Away team on short week is doubly disadvantaged.
        Returns (home_pts_adj, away_pts_adj).
        """
        home_adj = 0.0
        away_adj = 0.0
        if context.home_short_week:
            home_adj -= 1.5  # short week = -1.5 expected points
        if context.away_short_week:
            away_adj -= 2.5  # away + short week = more brutal
        return home_adj, away_adj

    # ── Turnover regression ───────────────────────────────────────────────────

    def _turnover_regression_adj(self, home: NFLTeamStats, away: NFLTeamStats) -> float:
        """
        Turnovers are highly random (fumble luck, tipped INTs).
        Teams with extreme TO differential will regress to mean.
        Negative diff = bet against them slightly.
        Returns home team expected point adjustment from TO regression.
        """
        # If home has very favorable TO diff, discount it 40%
        home_regressed = home.turnover_diff * 0.6
        away_regressed = away.turnover_diff * 0.6
        # Each net turnover worth ~3.5 pts over season
        pts_per_to = 3.5 / 16  # per game contribution from season-long differential
        return (home_regressed - away_regressed) * pts_per_to

    # ── Full game analysis ────────────────────────────────────────────────────

    def analyze_game(
        self,
        home: NFLTeamStats,
        away: NFLTeamStats,
        context: NFLGameContext,
        bankroll: Optional[float] = None,
    ) -> NFLPickResult:
        """
        Main analysis function. Returns picks for spread, ML, and total.
        """
        bk = bankroll or self.bankroll
        pick = NFLPickResult(
            event=f"{away.name} @ {home.name}",
            spread_pick=None, spread_edge_pct=0.0,
            moneyline_pick=None, ml_edge_pct=0.0,
            total_pick=None, total_edge_pct=0.0,
            sim_home_win_prob=0.5, sim_avg_total=0.0,
            sim_spread_cover_prob=0.5,
        )

        # ── Step 1: Expected points via DVOA ─────────────────────────────────
        home_expected_pts = self._dvoa_to_expected_points(
            home.off_dvoa, away.def_dvoa
        )
        away_expected_pts = self._dvoa_to_expected_points(
            away.off_dvoa, home.def_dvoa
        )

        # ── Step 2: HFA ──────────────────────────────────────────────────────
        hfa = self._home_field_advantage(context)
        home_expected_pts += hfa

        # ── Step 3: Weather ───────────────────────────────────────────────────
        pass_pen, rush_bonus = self._weather_adjustment(context)
        # Penalize both offenses for passing, slightly favor rushing teams
        home_pass_pct = 0.60  # typical NFL pass rate
        home_rush_pct = 1 - home_pass_pct
        away_pass_pct = 0.60
        away_rush_pct = 1 - away_pass_pct

        home_weather_adj = -(home_pass_pct * pass_pen * home_expected_pts) + (rush_bonus * home_rush_pct * 2)
        away_weather_adj = -(away_pass_pct * pass_pen * away_expected_pts) + (rush_bonus * away_rush_pct * 2)
        home_expected_pts += home_weather_adj
        away_expected_pts += away_weather_adj

        if pass_pen > 0.04:
            pick.notes.append(f"Weather suppresses passing: wind={context.wind_mph}mph, temp={context.temp_f}F")

        # ── Step 4: Short week ────────────────────────────────────────────────
        home_sw_adj, away_sw_adj = self._short_week_adjustment(context)
        home_expected_pts += home_sw_adj
        away_expected_pts += away_sw_adj
        if home_sw_adj < 0:
            pick.notes.append(f"Home team on short week (-{abs(home_sw_adj):.1f} pts penalized)")
        if away_sw_adj < 0:
            pick.notes.append(f"Away team on short week (-{abs(away_sw_adj):.1f} pts penalized)")

        # ── Step 5: Turnover regression ───────────────────────────────────────
        to_pts_adj = self._turnover_regression_adj(home, away)
        home_expected_pts += to_pts_adj

        # ── Step 6: Key injuries ──────────────────────────────────────────────
        home_injury_factor = 1.0 - home.key_player_injury_impact
        away_injury_factor = 1.0 - away.key_player_injury_impact
        home_expected_pts *= home_injury_factor
        away_expected_pts *= away_injury_factor

        # ── Step 7: Monte Carlo simulation ───────────────────────────────────
        weather_factor = max(0.70, 1.0 - (pass_pen * 2))
        home_short = 1 if context.home_short_week else 0
        away_short = 1 if context.away_short_week else 0

        sim = nfl_game_sim(
            home_dvoa=home.off_dvoa - away.def_dvoa,
            away_dvoa=away.off_dvoa - home.def_dvoa,
            home_epa=home.off_epa_per_play,
            away_epa=away.off_epa_per_play,
            weather_factor=weather_factor,
            home_short_week=home_short,
            away_short_week=away_short,
        )

        pick.sim_home_win_prob = sim.home_win_prob
        pick.sim_avg_total = sim.avg_total
        pick.sim_spread_cover_prob = sim.spread_cover_prob

        # ── Step 8: Derive probabilities ──────────────────────────────────────
        # Blend DVOA model + Monte Carlo (70/30 weight)
        home_win_prob = (0.70 * (home_expected_pts / (home_expected_pts + away_expected_pts))
                        + 0.30 * sim.home_win_prob)
        home_win_prob = max(0.10, min(0.90, home_win_prob))
        away_win_prob = 1.0 - home_win_prob

        expected_diff = home_expected_pts - away_expected_pts  # positive = home favored
        expected_total = home_expected_pts + away_expected_pts

        # ── Step 9: Spread pick ───────────────────────────────────────────────
        # Vegas spread = context.spread (negative = home favored, e.g. -3.5)
        vegas_home_favored_by = -context.spread  # convert to positive = home favored
        model_home_favored_by = expected_diff

        spread_diff = model_home_favored_by - vegas_home_favored_by  # +2 = home is +2 better than implied
        # Each point of spread ~= 3% cover probability shift
        spread_cover_prob = 0.50 + (spread_diff * 0.03)
        spread_cover_prob = max(0.30, min(0.70, spread_cover_prob))

        # Bet home covers if we think they're better than spread implies
        if spread_diff > 1.0:
            spread_implied_prob = 1 / american_to_decimal(-110)  # standard juice
            spread_ev = calculate_ev(spread_cover_prob, american_to_decimal(-110))
            if spread_ev.edge_pct >= self.min_edge:
                pick.spread_pick = f"{home.name} {context.spread}"
                pick.spread_edge_pct = spread_ev.edge_pct
                kelly = calculate_kelly(spread_cover_prob, american_to_decimal(-110), bk, self.kelly_multiplier)
                pick.recommended_bets.append({
                    "market": "Spread",
                    "pick": f"{home.name} {context.spread}",
                    "stake": kelly.recommended_stake,
                    "kelly_pct": kelly.kelly_pct,
                    "edge_pct": spread_ev.edge_pct,
                    "odds": -110,
                })
        elif spread_diff < -1.0:
            away_spread = (-context.spread) * -1  # flip for away
            away_cover_prob = 1 - spread_cover_prob
            away_ev = calculate_ev(away_cover_prob, american_to_decimal(-110))
            if away_ev.edge_pct >= self.min_edge:
                pick.spread_pick = f"{away.name} +{abs(context.spread)}"
                pick.spread_edge_pct = away_ev.edge_pct
                kelly = calculate_kelly(away_cover_prob, american_to_decimal(-110), bk, self.kelly_multiplier)
                pick.recommended_bets.append({
                    "market": "Spread",
                    "pick": f"{away.name} +{abs(context.spread)}",
                    "stake": kelly.recommended_stake,
                    "kelly_pct": kelly.kelly_pct,
                    "edge_pct": away_ev.edge_pct,
                    "odds": -110,
                })

        # ── Step 10: Moneyline pick ───────────────────────────────────────────
        home_ml_dec = american_to_decimal(context.home_moneyline)
        away_ml_dec = american_to_decimal(context.away_moneyline)

        home_ml_ev = calculate_ev(home_win_prob, home_ml_dec)
        away_ml_ev = calculate_ev(away_win_prob, away_ml_dec)

        if home_ml_ev.edge_pct >= self.min_edge:
            pick.moneyline_pick = home.name
            pick.ml_edge_pct = home_ml_ev.edge_pct
            kelly = calculate_kelly(home_win_prob, home_ml_dec, bk, self.kelly_multiplier)
            pick.recommended_bets.append({
                "market": "Moneyline",
                "pick": home.name,
                "stake": kelly.recommended_stake,
                "kelly_pct": kelly.kelly_pct,
                "edge_pct": home_ml_ev.edge_pct,
                "odds": context.home_moneyline,
            })
        elif away_ml_ev.edge_pct >= self.min_edge:
            pick.moneyline_pick = away.name
            pick.ml_edge_pct = away_ml_ev.edge_pct
            kelly = calculate_kelly(away_win_prob, away_ml_dec, bk, self.kelly_multiplier)
            pick.recommended_bets.append({
                "market": "Moneyline",
                "pick": away.name,
                "stake": kelly.recommended_stake,
                "kelly_pct": kelly.kelly_pct,
                "edge_pct": away_ml_ev.edge_pct,
                "odds": context.away_moneyline,
            })

        # ── Step 11: Total pick ───────────────────────────────────────────────
        total_diff = expected_total - context.total_line
        total_std = 7.0   # typical NFL scoring std dev

        # Blend: model says +3.5 over the line → likely Over
        if abs(total_diff) >= 2.0:
            # Z-score based probability
            import math
            over_prob = 0.50 + (total_diff / (2 * total_std))
            over_prob = max(0.30, min(0.70, over_prob))
            under_prob = 1 - over_prob

            if over_prob > 0.55:
                over_ev = calculate_ev(over_prob, american_to_decimal(-110))
                if over_ev.edge_pct >= self.min_edge:
                    pick.total_pick = "Over"
                    pick.total_edge_pct = over_ev.edge_pct
                    kelly = calculate_kelly(over_prob, american_to_decimal(-110), bk, self.kelly_multiplier)
                    pick.recommended_bets.append({
                        "market": "Total",
                        "pick": f"Over {context.total_line}",
                        "stake": kelly.recommended_stake,
                        "kelly_pct": kelly.kelly_pct,
                        "edge_pct": over_ev.edge_pct,
                        "odds": -110,
                    })
            elif under_prob > 0.55:
                under_ev = calculate_ev(under_prob, american_to_decimal(-110))
                if under_ev.edge_pct >= self.min_edge:
                    pick.total_pick = "Under"
                    pick.total_edge_pct = under_ev.edge_pct
                    kelly = calculate_kelly(under_prob, american_to_decimal(-110), bk, self.kelly_multiplier)
                    pick.recommended_bets.append({
                        "market": "Total",
                        "pick": f"Under {context.total_line}",
                        "stake": kelly.recommended_stake,
                        "kelly_pct": kelly.kelly_pct,
                        "edge_pct": under_ev.edge_pct,
                        "odds": -110,
                    })

        # ── Divisional/playoff boost note ─────────────────────────────────────
        if context.divisional_game:
            pick.notes.append("Divisional game: expect tighter margins, slight fade on large spreads")
        if context.playoff_implications:
            pick.notes.append("Playoff implications: motivated team plays above level")

        # Streak bias note
        if abs(home.current_streak) >= 4:
            direction = "hot" if home.current_streak > 0 else "cold"
            pick.notes.append(f"Home team on {abs(home.current_streak)}-game {direction} streak (regress)")

        return pick


# ─── Quick Test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    agent = NFLAgent(bankroll=10000)

    chiefs = NFLTeamStats(
        name="Kansas City Chiefs",
        off_dvoa=22.4, def_dvoa=-14.2, special_teams_dvoa=4.1,
        off_epa_per_play=0.21, def_epa_per_play=-0.18,
        yards_per_play=6.1, points_per_game=27.4, points_allowed_per_game=18.2,
        turnover_diff=8, red_zone_td_pct=0.68, wins=12, losses=3,
    )
    ravens = NFLTeamStats(
        name="Baltimore Ravens",
        off_dvoa=18.6, def_dvoa=-9.8, special_teams_dvoa=2.3,
        off_epa_per_play=0.18, def_epa_per_play=-0.12,
        yards_per_play=5.9, points_per_game=26.1, points_allowed_per_game=19.4,
        turnover_diff=4, red_zone_td_pct=0.62, wins=11, losses=4,
    )
    ctx = NFLGameContext(
        home_team="Baltimore Ravens", away_team="Kansas City Chiefs",
        spread=3.0,   # BAL favored by 3 at home
        total_line=48.5,
        home_moneyline=-145, away_moneyline=+124,
        temp_f=34, wind_mph=12, precipitation=False, outdoor_stadium=True,
        home_short_week=False, away_short_week=False,
        divisional_game=False, playoff_implications=True,
        home_rest_days=7, away_rest_days=7,
    )
    result = agent.analyze_game(home=ravens, away=chiefs, context=ctx)
    print(f"Game: {result.event}")
    print(f"Home win prob: {result.sim_home_win_prob:.2%}")
    print(f"Avg total: {result.sim_avg_total:.1f}")
    print(f"Spread pick: {result.spread_pick} (edge {result.spread_edge_pct:.2f}%)")
    print(f"ML pick: {result.moneyline_pick} (edge {result.ml_edge_pct:.2f}%)")
    print(f"Total pick: {result.total_pick} (edge {result.total_edge_pct:.2f}%)")
    print(f"Notes: {result.notes}")
    print(f"Bets: {result.recommended_bets}")
