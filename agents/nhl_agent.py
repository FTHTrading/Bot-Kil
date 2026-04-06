"""
NHL Agent — Corsi / Fenwick / xG / Goalie Analyzer
====================================================
Advanced hockey analytics from ai_betting.pdf and betting69.pdf:
- Corsi% (shot attempt differential)
- Fenwick% (unblocked shot attempt differential)
- Expected Goals (xG) — quality-adjusted shot metric
- PDO (shooting% + save% — regresses to 100)
- Goalie save% and goals saved above expected (GSAX)
- Power play / penalty kill efficiency
- Back-to-back fatigue model
- Home ice advantage (~0.15 goals per game historical)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.kelly import calculate_kelly, american_to_decimal
from engine.ev import calculate_ev
from engine.monte_carlo import nhl_game_sim


# ─── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class NHLTeamStats:
    name: str
    # Corsi/Fenwick (5-on-5, score-adjusted)
    corsi_for_pct: float      # CF% e.g. 52.4 = 52.4% of all shot attempts
    fenwick_for_pct: float    # FF% e.g. 51.8
    # Expected Goals
    xg_for: float             # xGF per 60 minutes of 5v5 play
    xg_against: float         # xGA per 60 minutes of 5v5 play
    # PDO (shooting% + save% combined, regresses to 100)
    pdo: float                # e.g. 101.4 (anything >101 = luck component)
    shooting_pct: float       # e.g. 9.2 (%)
    save_pct: float           # Team even-strength save% e.g. 0.918
    # Goalie stats
    starter_sv_pct: float     # Starting goalie save% e.g. 0.922
    starter_gsax: float       # Goals saved above expected (season or per60)
    backup_sv_pct: float = 0.900
    # Special teams
    power_play_pct: float     # PP% e.g. 22.4
    penalty_kill_pct: float   # PK% e.g. 80.5
    # Scoring
    goals_for_per_game: float = 3.1
    goals_against_per_game: float = 2.8
    # Rest
    games_played_last_3_days: int = 0  # 0=fresh, 1=one game, 2=back-to-back
    # Record
    wins: int = 25
    losses: int = 15
    ot_losses: int = 5


@dataclass
class NHLGameContext:
    home_team: str
    away_team: str
    puck_line: float        # Spread equivalent (-1.5 / +1.5 mostly)
    total_line: float       # Over/under e.g. 5.5
    home_moneyline: int
    away_moneyline: int
    home_starter_confirmed: bool = True
    away_starter_confirmed: bool = True
    # Whether this is a back-to-back situation
    home_b2b: bool = False
    away_b2b: bool = False
    # Rivalry / divisional
    divisional_game: bool = False


@dataclass
class NHLPickResult:
    event: str
    ml_pick: Optional[str]
    ml_edge_pct: float
    puck_line_pick: Optional[str]
    puck_line_edge_pct: float
    total_pick: Optional[str]
    total_edge_pct: float
    sim_home_win_prob: float
    sim_avg_total: float
    pdo_warning: list[str]
    recommended_bets: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ─── Core Agent ────────────────────────────────────────────────────────────────

class NHLAgent:
    """
    NHL game analyzer using Corsi, Fenwick, xG, PDO, and goalie metrics.
    """

    def __init__(self, bankroll: float = 10000, kelly_multiplier: float = 0.25,
                 min_edge: float = 0.03):
        self.bankroll = bankroll
        self.kelly_multiplier = kelly_multiplier
        self.min_edge = min_edge

    # ── PDO regression warning ────────────────────────────────────────────────

    def _pdo_warnings(self, home: NHLTeamStats, away: NHLTeamStats) -> list[str]:
        """
        PDO > 102: team is running hot on luck, expect regression.
        PDO < 98:  team is running cold, expect positive regression.
        From betting69.pdf: PDO is one of the strongest fade signals in NHL.
        """
        warnings = []
        if home.pdo > 102.5:
            warnings.append(f"{home.name} PDO={home.pdo:.1f} — OVERPERFORMING (fade/under)")
        elif home.pdo < 97.5:
            warnings.append(f"{home.name} PDO={home.pdo:.1f} — UNDERPERFORMING (value bet)")
        if away.pdo > 102.5:
            warnings.append(f"{away.name} PDO={away.pdo:.1f} — OVERPERFORMING (fade/under)")
        elif away.pdo < 97.5:
            warnings.append(f"{away.name} PDO={away.pdo:.1f} — UNDERPERFORMING (value bet)")
        return warnings

    # ── xG-based expected goals ────────────────────────────────────────────────

    def _expected_goals_model(
        self, home: NHLTeamStats, away: NHLTeamStats, context: NHLGameContext
    ) -> tuple[float, float]:
        """
        Compute expected goals for each team by blending:
        - Team xGF/xGA per-60 (quality of chances)
        - Goalie save% (opponent's goalie cancels some offense)
        - Special teams differential
        - Home ice advantage (~0.15 goals)
        - PDO regression
        - Back-to-back penalty
        """
        # Base: use xG per 60, scale to ~2.5 periods = 60 mins net
        # xGF is goals the team should score based on shot quality
        # Factor in opponent's goalie: better goalie → fewer goals
        home_xg = home.xg_for * (1.0 - (away.starter_sv_pct - 0.900) * 5)
        away_xg = away.xg_for * (1.0 - (home.starter_sv_pct - 0.900) * 5)

        # Special teams: net PP% advantage converts to ~0.1 goals per 5% gap
        # Each 5% of PP advantage vs PK adds ~0.08 goals
        home_pp_net = home.power_play_pct - (100 - away.penalty_kill_pct)
        away_pp_net = away.power_play_pct - (100 - home.penalty_kill_pct)
        home_xg += home_pp_net * 0.008
        away_xg += away_pp_net * 0.008

        # Home ice advantage
        home_xg += 0.15

        # PDO regression: if a team has extreme PDO, regress their scoring rate
        home_pdo_factor = 1.0
        away_pdo_factor = 1.0
        if home.pdo > 102:
            home_pdo_factor = 0.92  # expect some regression
        elif home.pdo < 98:
            home_pdo_factor = 1.06
        if away.pdo > 102:
            away_pdo_factor = 0.92
        elif away.pdo < 98:
            away_pdo_factor = 1.06
        home_xg *= home_pdo_factor
        away_xg *= away_pdo_factor

        # Back-to-back fatigue: scoring drops ~8% on second of B2B
        if context.home_b2b:
            home_xg *= 0.92
        if context.away_b2b:
            away_xg *= 0.92

        # Goalie unconfirmed: high uncertainty → expand variance, don't use for ML
        if not context.home_starter_confirmed:
            # Use league-average backup save% (0.900 vs starter's typically higher)
            home_xg *= (1.0 + (home.starter_sv_pct - home.backup_sv_pct) * 5)
        if not context.away_starter_confirmed:
            away_xg *= (1.0 + (away.starter_sv_pct - away.backup_sv_pct) * 5)

        # Clip to realism: NHL goals range 1.5 – 5.5 per team
        home_xg = max(1.0, min(5.5, home_xg))
        away_xg = max(1.0, min(5.5, away_xg))

        return home_xg, away_xg

    # ── Win probability from xG ────────────────────────────────────────────────

    def _xg_to_win_prob(self, home_xg: float, away_xg: float) -> tuple[float, float]:
        """
        Model win probability from expected goals using Poisson distribution.
        P(home wins) = sum over x,y where x > y of P(X=x) * P(Y=y)
        Approximated analytically.
        """
        import math

        def poisson_pmf(k: int, lam: float) -> float:
            return (lam ** k) * math.exp(-lam) / math.factorial(k)

        max_goals = 10
        home_win_p = 0.0
        tie_p = 0.0
        away_win_p = 0.0

        for h in range(max_goals + 1):
            for a in range(max_goals + 1):
                prob = poisson_pmf(h, home_xg) * poisson_pmf(a, away_xg)
                if h > a:
                    home_win_p += prob
                elif h == a:
                    tie_p += prob
                else:
                    away_win_p += prob

        # Ties go to OT: home wins ~52% of OT games (home ice advantage in OT)
        home_win_p += tie_p * 0.52
        away_win_p += tie_p * 0.48

        return home_win_p, away_win_p

    # ── Full analysis ──────────────────────────────────────────────────────────

    def analyze_game(
        self,
        home: NHLTeamStats,
        away: NHLTeamStats,
        context: NHLGameContext,
        bankroll: Optional[float] = None,
    ) -> NHLPickResult:
        bk = bankroll or self.bankroll
        result = NHLPickResult(
            event=f"{away.name} @ {home.name}",
            ml_pick=None, ml_edge_pct=0.0,
            puck_line_pick=None, puck_line_edge_pct=0.0,
            total_pick=None, total_edge_pct=0.0,
            sim_home_win_prob=0.5, sim_avg_total=0.0,
            pdo_warning=[],
        )

        # ── PDO warnings ──────────────────────────────────────────────────────
        result.pdo_warning = self._pdo_warnings(home, away)

        # ── Expected goals model ──────────────────────────────────────────────
        home_xg, away_xg = self._expected_goals_model(home, away, context)
        result.sim_avg_total = home_xg + away_xg

        # ── Poisson win probability ───────────────────────────────────────────
        home_win_prob, away_win_prob = self._xg_to_win_prob(home_xg, away_xg)
        result.sim_home_win_prob = home_win_prob

        # ── Monte Carlo blend ─────────────────────────────────────────────────
        mc_home_b2b = 1 if context.home_b2b else 0
        mc_away_b2b = 1 if context.away_b2b else 0
        sim = nhl_game_sim(
            home_corsi_pct=home.corsi_for_pct,
            away_corsi_pct=away.corsi_for_pct,
            home_xg=home_xg,
            away_xg=away_xg,
            home_save_pct=home.starter_sv_pct,
            away_save_pct=away.starter_sv_pct,
            home_b2b=mc_home_b2b,
            away_b2b=mc_away_b2b,
        )

        # 60% xG model / 40% Monte Carlo
        home_win_prob = 0.60 * home_win_prob + 0.40 * sim.home_win_prob
        away_win_prob = 1.0 - home_win_prob
        blended_total = 0.60 * result.sim_avg_total + 0.40 * sim.avg_total
        result.sim_avg_total = blended_total
        result.sim_home_win_prob = home_win_prob

        # ── Moneyline pick ────────────────────────────────────────────────────
        home_ml_dec = american_to_decimal(context.home_moneyline)
        away_ml_dec = american_to_decimal(context.away_moneyline)

        home_ev = calculate_ev(home_win_prob, home_ml_dec)
        away_ev = calculate_ev(away_win_prob, away_ml_dec)

        if home_ev.edge_pct >= self.min_edge and home_ev.edge_pct > away_ev.edge_pct:
            result.ml_pick = home.name
            result.ml_edge_pct = home_ev.edge_pct
            kelly = calculate_kelly(home_win_prob, home_ml_dec, bk, self.kelly_multiplier)
            result.recommended_bets.append({
                "market": "Moneyline",
                "pick": home.name,
                "stake": kelly.recommended_stake,
                "kelly_pct": kelly.kelly_pct,
                "edge_pct": home_ev.edge_pct,
                "odds": context.home_moneyline,
            })
        elif away_ev.edge_pct >= self.min_edge:
            result.ml_pick = away.name
            result.ml_edge_pct = away_ev.edge_pct
            kelly = calculate_kelly(away_win_prob, away_ml_dec, bk, self.kelly_multiplier)
            result.recommended_bets.append({
                "market": "Moneyline",
                "pick": away.name,
                "stake": kelly.recommended_stake,
                "kelly_pct": kelly.kelly_pct,
                "edge_pct": away_ev.edge_pct,
                "odds": context.away_moneyline,
            })

        # ── Total pick ────────────────────────────────────────────────────────
        total_diff = blended_total - context.total_line

        if abs(total_diff) >= 0.25:
            over_prob = 0.50 + min(0.20, total_diff * 0.10)
            under_prob = 1.0 - over_prob

            if over_prob > 0.53:
                ev = calculate_ev(over_prob, american_to_decimal(-110))
                if ev.edge_pct >= self.min_edge:
                    result.total_pick = "Over"
                    result.total_edge_pct = ev.edge_pct
                    kelly = calculate_kelly(over_prob, american_to_decimal(-110), bk, self.kelly_multiplier)
                    result.recommended_bets.append({
                        "market": "Total",
                        "pick": f"Over {context.total_line}",
                        "stake": kelly.recommended_stake,
                        "kelly_pct": kelly.kelly_pct,
                        "edge_pct": ev.edge_pct,
                        "odds": -110,
                    })
            elif under_prob > 0.53:
                ev = calculate_ev(under_prob, american_to_decimal(-110))
                if ev.edge_pct >= self.min_edge:
                    result.total_pick = "Under"
                    result.total_edge_pct = ev.edge_pct
                    kelly = calculate_kelly(under_prob, american_to_decimal(-110), bk, self.kelly_multiplier)
                    result.recommended_bets.append({
                        "market": "Total",
                        "pick": f"Under {context.total_line}",
                        "stake": kelly.recommended_stake,
                        "kelly_pct": kelly.kelly_pct,
                        "edge_pct": ev.edge_pct,
                        "odds": -110,
                    })

        # ── Puck line pick ────────────────────────────────────────────────────
        # Standard puck line: -1.5 for favorite, +1.5 for dog
        # Favorite -1.5 typically only covers if win prob > 60%
        if home_win_prob >= 0.60:
            # Check -1.5 puck line (typically around +120 to +150)
            # Probability of winning by 2+ is roughly (win_prob^1.5) empirically
            pl_cover_prob = home_win_prob ** 1.5
            pl_odds_dec = 1.30  # typical puck line price for -1.5 favorite
            pl_ev = calculate_ev(pl_cover_prob, pl_odds_dec)
            if pl_ev.edge_pct >= self.min_edge:
                result.puck_line_pick = f"{home.name} -1.5"
                result.puck_line_edge_pct = pl_ev.edge_pct
                kelly = calculate_kelly(pl_cover_prob, pl_odds_dec, bk, self.kelly_multiplier)
                result.recommended_bets.append({
                    "market": "Puck Line",
                    "pick": f"{home.name} -1.5",
                    "stake": kelly.recommended_stake,
                    "kelly_pct": kelly.kelly_pct,
                    "edge_pct": pl_ev.edge_pct,
                    "odds": "+130 (approx)",
                })
        elif away_win_prob >= 0.60:
            pl_cover_prob = away_win_prob ** 1.5
            pl_odds_dec = 1.30
            pl_ev = calculate_ev(pl_cover_prob, pl_odds_dec)
            if pl_ev.edge_pct >= self.min_edge:
                result.puck_line_pick = f"{away.name} -1.5"
                result.puck_line_edge_pct = pl_ev.edge_pct
                kelly = calculate_kelly(pl_cover_prob, pl_odds_dec, bk, self.kelly_multiplier)
                result.recommended_bets.append({
                    "market": "Puck Line",
                    "pick": f"{away.name} -1.5",
                    "stake": kelly.recommended_stake,
                    "kelly_pct": kelly.kelly_pct,
                    "edge_pct": pl_ev.edge_pct,
                    "odds": "+130 (approx)",
                })

        # ── Contextual notes ──────────────────────────────────────────────────
        if context.home_b2b:
            result.notes.append(f"{home.name} on back-to-back (fatigue penalty applied)")
        if context.away_b2b:
            result.notes.append(f"{away.name} on back-to-back (fatigue penalty applied)")
        if not context.home_starter_confirmed:
            result.notes.append(f"⚠️ {home.name} starter UNCONFIRMED — variance elevated, reduce stake")
        if not context.away_starter_confirmed:
            result.notes.append(f"⚠️ {away.name} starter UNCONFIRMED — variance elevated, reduce stake")

        # Corsi signal
        corsi_diff = home.corsi_for_pct - away.corsi_for_pct
        if abs(corsi_diff) > 5:
            stronger = home.name if corsi_diff > 0 else away.name
            result.notes.append(f"{stronger} dominates shot share (CF% gap = {abs(corsi_diff):.1f}%)")

        return result


# ─── Prop Targets (NHL) ───────────────────────────────────────────────────────

def get_nhl_prop_targets(
    player_name: str,
    shots_on_goal_avg: float,
    line: float,
    corsi_rel: float,          # player's Corsi relative to their team
    ice_time_avg_min: float,   # average ice time in minutes
    opp_sv_pct: float,         # opposing goalie save%
    pp_time_avg: float = 0.0,  # avg power play time
) -> dict:
    """
    Evaluate a shots on goal prop bet using usage and matchup data.
    """
    # Adjust expected shots for opponent goalie difficulty
    goalie_factor = 1.0 - (opp_sv_pct - 0.900) * 3  # tougher goalie = fewer shots on goal (some blocked)
    adjusted_avg = shots_on_goal_avg * goalie_factor

    # Ice time proxy for usage
    if ice_time_avg_min < 14:
        adjusted_avg *= 0.85  # reduced usage
    elif ice_time_avg_min > 20:
        adjusted_avg *= 1.08  # top-line deployment

    # Corsi rel: player who drives more shots generates more SOG
    if corsi_rel > 3:
        adjusted_avg *= 1.05
    elif corsi_rel < -3:
        adjusted_avg *= 0.95

    # PP time adds high-quality shots
    adjusted_avg += pp_time_avg * 0.05

    over_prob = 0.50 + min(0.20, (adjusted_avg - line) * 0.15)
    under_prob = 1.0 - over_prob

    from engine.ev import calculate_ev
    from engine.kelly import american_to_decimal
    if over_prob > 0.55:
        ev = calculate_ev(over_prob, american_to_decimal(-115))
        direction = "Over"
        play_prob = over_prob
    elif under_prob > 0.55:
        ev = calculate_ev(under_prob, american_to_decimal(-115))
        direction = "Under"
        play_prob = under_prob
    else:
        return {"player": player_name, "recommendation": "PASS — no edge", "edge_pct": 0.0}

    return {
        "player": player_name,
        "market": f"Shots on Goal {direction} {line}",
        "recommendation": direction,
        "adjusted_avg_sog": round(adjusted_avg, 2),
        "probability": round(play_prob, 4),
        "edge_pct": round(ev.edge_pct, 2),
        "ev_pct": round(ev.ev_pct, 2),
    }


# ─── Quick Test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    agent = NHLAgent(bankroll=10000)

    bruins = NHLTeamStats(
        name="Boston Bruins",
        corsi_for_pct=53.8, fenwick_for_pct=52.9,
        xg_for=3.21, xg_against=2.44,
        pdo=100.8, shooting_pct=9.4, save_pct=0.920,
        starter_sv_pct=0.924, starter_gsax=8.2,
        power_play_pct=24.2, penalty_kill_pct=82.1,
        goals_for_per_game=3.3, goals_against_per_game=2.6,
        wins=32, losses=18, ot_losses=6,
    )
    leafs = NHLTeamStats(
        name="Toronto Maple Leafs",
        corsi_for_pct=51.2, fenwick_for_pct=50.4,
        xg_for=3.05, xg_against=2.68,
        pdo=103.2,  # running hot — fade signal
        shooting_pct=10.8, save_pct=0.914,
        starter_sv_pct=0.918, starter_gsax=5.1,
        power_play_pct=22.8, penalty_kill_pct=79.4,
        goals_for_per_game=3.5, goals_against_per_game=2.9,
        wins=29, losses=20, ot_losses=7,
    )
    ctx = NHLGameContext(
        home_team="Boston Bruins", away_team="Toronto Maple Leafs",
        puck_line=-1.5, total_line=5.5,
        home_moneyline=-135, away_moneyline=+115,
        home_starter_confirmed=True, away_starter_confirmed=True,
        home_b2b=False, away_b2b=True,   # Leafs on B2B
        divisional_game=True,
    )
    result = agent.analyze_game(bruins, leafs, ctx)
    print(f"Game: {result.event}")
    print(f"Home win prob (blend): {result.sim_home_win_prob:.2%}")
    print(f"Expected total: {result.sim_avg_total:.2f}")
    print(f"ML pick: {result.ml_pick} (edge {result.ml_edge_pct:.2f}%)")
    print(f"Total: {result.total_pick} (edge {result.total_edge_pct:.2f}%)")
    print(f"PDO warnings: {result.pdo_warning}")
    print(f"Notes: {result.notes}")
