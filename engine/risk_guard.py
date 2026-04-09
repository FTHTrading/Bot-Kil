"""
engine/risk_guard.py — Per-bet and per-session risk limits
==========================================================
Enforces hard stops on individual bet sizing and session-level drawdown.
This module is intentionally stateless about positions (see portfolio_guard.py
for that); it only cares about dollar amounts and session P&L.

Typical call sequence in _tool_place_bet():
    guard = RiskGuard.from_session_log()    # or passed in as singleton
    verdict = guard.check(stake, bankroll, session_pnl)
    if not verdict.allowed:
        return {"status": "blocked", "reason": verdict.reason}

Usage:
    from engine.risk_guard import RiskGuard, RiskVerdict
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# ── Hard limits — edit here, not scattered across the codebase ───────────────

# Per-bet sizing
_MAX_BET_DOLLARS       = 25.0    # hard cap per single bet
_MIN_BET_DOLLARS       = 1.0     # minimum meaningful bet
_MAX_BET_PCT_BANKROLL  = 0.08    # no single bet may exceed 8% of current bankroll

# Session / rolling limits
_MAX_SESSION_LOSS_PCT  = 0.15    # stop trading after losing 15% of starting bankroll
_MAX_DAILY_BETS        = 20      # circuit breaker: ≤20 bets per session loop

# Consecutive loss circuit breaker
_MAX_CONSECUTIVE_LOSSES = 4      # pause after 4 back-to-back settled losses


# ── Verdict dataclass ────────────────────────────────────────────────────────

@dataclass
class RiskVerdict:
    allowed:       bool
    reason:        Optional[str]   # None when allowed
    clamped_stake: float           # possibly reduced stake (≤ original)

    def to_dict(self) -> dict:
        return {
            "allowed":       self.allowed,
            "reason":        self.reason,
            "clamped_stake": round(self.clamped_stake, 2),
        }


# ── Guard class ──────────────────────────────────────────────────────────────

class RiskGuard:
    """
    Stateful risk controller.  Instantiate once per agent session and pass the
    same instance through every bet-placing call so consecutive-loss counting
    and session-bet counts remain accurate.
    """

    def __init__(
        self,
        starting_bankroll: float = 200.0,
        max_bet_dollars:   float = _MAX_BET_DOLLARS,
        max_session_loss_pct: float = _MAX_SESSION_LOSS_PCT,
        max_daily_bets:    int   = _MAX_DAILY_BETS,
        max_consec_losses: int   = _MAX_CONSECUTIVE_LOSSES,
    ):
        self.starting_bankroll    = starting_bankroll
        self.max_bet_dollars      = max_bet_dollars
        self.max_session_loss_pct = max_session_loss_pct
        self.max_daily_bets       = max_daily_bets
        self.max_consec_losses    = max_consec_losses

        self._bets_this_session: int  = 0
        self._consec_losses:     int  = 0

    # ── Public interface ─────────────────────────────────────────────────────

    def check(
        self,
        proposed_stake: float,
        current_bankroll: float,
        session_pnl: float = 0.0,
    ) -> RiskVerdict:
        """
        Validate a proposed stake and return a verdict.

        Parameters
        ----------
        proposed_stake   : dollar amount from Kelly sizing.
        current_bankroll : live balance (used for % cap check).
        session_pnl      : cumulative P&L this session (negative = loss).
        """
        # ── Circuit breaker: session bet count ──────────────────────────────
        if self._bets_this_session >= self.max_daily_bets:
            return RiskVerdict(
                allowed=False,
                reason=f"session bet limit reached ({self._bets_this_session}/{self.max_daily_bets})",
                clamped_stake=0.0,
            )

        # ── Circuit breaker: consecutive losses ─────────────────────────────
        if self._consec_losses >= self.max_consec_losses:
            return RiskVerdict(
                allowed=False,
                reason=(
                    f"{self._consec_losses} consecutive losses — pausing; "
                    "call reset_consecutive_losses() after review"
                ),
                clamped_stake=0.0,
            )

        # ── Session drawdown limit ───────────────────────────────────────────
        max_loss_dollars = self.starting_bankroll * self.max_session_loss_pct
        if session_pnl <= -max_loss_dollars:
            return RiskVerdict(
                allowed=False,
                reason=(
                    f"session loss ${-session_pnl:.2f} exceeds "
                    f"{self.max_session_loss_pct*100:.0f}% limit "
                    f"(${max_loss_dollars:.2f})"
                ),
                clamped_stake=0.0,
            )

        # ── Per-bet dollar caps ───────────────────────────────────────────────
        stake = proposed_stake
        if stake < _MIN_BET_DOLLARS:
            return RiskVerdict(
                allowed=False,
                reason=f"stake ${stake:.2f} below minimum ${_MIN_BET_DOLLARS:.2f}",
                clamped_stake=0.0,
            )

        # Hard cap
        if stake > self.max_bet_dollars:
            log.debug("[risk_guard] stake $%.2f clamped to max $%.2f", stake, self.max_bet_dollars)
            stake = self.max_bet_dollars

        # Bankroll % cap
        pct_cap = current_bankroll * _MAX_BET_PCT_BANKROLL
        if stake > pct_cap:
            log.debug("[risk_guard] stake $%.2f clamped to %.0f%% of bankroll $%.2f", stake, _MAX_BET_PCT_BANKROLL*100, current_bankroll)
            stake = max(_MIN_BET_DOLLARS, pct_cap)

        return RiskVerdict(allowed=True, reason=None, clamped_stake=round(stake, 2))

    def record_bet_placed(self):
        """Call whenever a bet order is successfully submitted."""
        self._bets_this_session += 1
        log.debug("[risk_guard] bets_this_session=%d", self._bets_this_session)

    def record_outcome(self, won: bool):
        """
        Call when a position settles.
        won=True  → reset consecutive-loss counter
        won=False → increment it
        """
        if won:
            self._consec_losses = 0
        else:
            self._consec_losses += 1
            log.warning("[risk_guard] consecutive_losses=%d", self._consec_losses)

    def reset_consecutive_losses(self):
        """Manual override after human review — re-enables betting."""
        log.info("[risk_guard] consecutive_loss counter reset from %d", self._consec_losses)
        self._consec_losses = 0

    def reset_session(self):
        """Call at the start of a new session / trading day."""
        self._bets_this_session = 0
        self._consec_losses     = 0

    def status(self) -> dict:
        return {
            "bets_this_session": self._bets_this_session,
            "consec_losses":     self._consec_losses,
            "max_daily_bets":    self.max_daily_bets,
            "max_consec_losses": self.max_consec_losses,
            "session_loss_limit_pct": self.max_session_loss_pct,
        }
