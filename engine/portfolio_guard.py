"""
engine/portfolio_guard.py — Cross-position correlation and concentration limits
===============================================================================
Prevents the agent from building too much correlated or concentrated exposure
across open positions.  Works on the live positions list returned by Kalshi's
`/portfolio/positions` endpoint (as parsed by _tool_get_positions).

Usage:
    from engine.portfolio_guard import PortfolioGuard

    guard  = PortfolioGuard()
    result = guard.check(candidate_pick, open_positions)
    if not result.allowed:
        log.info("[portfolio_guard] %s", result.reason)
        return
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# ── Limits ────────────────────────────────────────────────────────────────────

# Max simultaneous open bets on the same underlying asset
_MAX_SAME_ASSET_BETS     = 2

# Max total open bets across the whole portfolio
_MAX_TOTAL_OPEN_BETS     = 6

# Max total capital at risk (sum of cost_basis for all open positions) as
# a fraction of the reported bankroll.  Set to None to disable.
_MAX_OPEN_EXPOSURE_PCT   = 0.40    # 40% of bankroll tied up at once

# Crypto assets treated as correlated to BTC for concentration purposes.
# If the candidate is one of these AND there are already N BTC bets, block.
_BTC_CORRELATED_ASSETS   = {"BTC", "ETH"}
_MAX_BTC_CORRELATED_BETS = 3


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class PortfolioVerdict:
    allowed: bool
    reason:  Optional[str]

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason}


# ── Guard ────────────────────────────────────────────────────────────────────

class PortfolioGuard:
    """
    Stateless guard — takes the current open positions list on every call so
    it always works from live data rather than a cached snapshot.
    """

    def check(
        self,
        candidate_pick: dict,
        open_positions: list[dict],
        bankroll: float = 200.0,
    ) -> PortfolioVerdict:
        """
        Evaluate whether adding `candidate_pick` would breach portfolio limits.

        Parameters
        ----------
        candidate_pick : pick dict; must contain 'asset'.
        open_positions : list of position dicts from _tool_get_positions().
                         Expected keys per position: asset, status, cost,
                         market_id, side.  Missing keys are handled gracefully.
        bankroll       : current balance used for exposure % check.
        """
        asset  = candidate_pick.get("asset", "UNKNOWN")
        active = [
            p for p in open_positions
            if p.get("status") not in ("settled", "closed", "expired")
        ]

        # ── 1. Total open bet count ───────────────────────────────────────
        if len(active) >= _MAX_TOTAL_OPEN_BETS:
            return PortfolioVerdict(
                allowed=False,
                reason=(
                    f"total open positions={len(active)} >= limit={_MAX_TOTAL_OPEN_BETS}"
                ),
            )

        # ── 2. Same-asset concentration ───────────────────────────────────
        same_asset_bets = sum(
            1 for p in active if p.get("asset") == asset
        )
        if same_asset_bets >= _MAX_SAME_ASSET_BETS:
            return PortfolioVerdict(
                allowed=False,
                reason=(
                    f"already {same_asset_bets} open bet(s) on {asset} "
                    f"(limit={_MAX_SAME_ASSET_BETS})"
                ),
            )

        # ── 3. BTC-correlated cluster ─────────────────────────────────────
        if asset in _BTC_CORRELATED_ASSETS:
            btc_cluster = sum(
                1 for p in active if p.get("asset") in _BTC_CORRELATED_ASSETS
            )
            if btc_cluster >= _MAX_BTC_CORRELATED_BETS:
                return PortfolioVerdict(
                    allowed=False,
                    reason=(
                        f"BTC-correlated cluster already at {btc_cluster} "
                        f"(limit={_MAX_BTC_CORRELATED_BETS}); "
                        f"asset={asset} is in {{BTC,ETH}} cluster"
                    ),
                )

        # ── 4. Total capital at risk ───────────────────────────────────────
        if _MAX_OPEN_EXPOSURE_PCT is not None and bankroll > 0:
            open_exposure = sum(
                float(p.get("cost") or p.get("resting_orders_cost") or 0.0)
                for p in active
            )
            exposure_pct = open_exposure / bankroll
            if exposure_pct >= _MAX_OPEN_EXPOSURE_PCT:
                return PortfolioVerdict(
                    allowed=False,
                    reason=(
                        f"open exposure ${open_exposure:.2f} "
                        f"({exposure_pct*100:.1f}% of bankroll) "
                        f">= limit {_MAX_OPEN_EXPOSURE_PCT*100:.0f}%"
                    ),
                )

        return PortfolioVerdict(allowed=True, reason=None)

    def summary(self, open_positions: list[dict], bankroll: float = 200.0) -> dict:
        """Return a snapshot of the current portfolio risk profile."""
        active = [
            p for p in open_positions
            if p.get("status") not in ("settled", "closed", "expired")
        ]
        asset_counts: dict[str, int] = {}
        total_cost = 0.0
        for p in active:
            a = p.get("asset", "?")
            asset_counts[a] = asset_counts.get(a, 0) + 1
            total_cost += float(p.get("cost") or 0.0)

        btc_cluster = sum(
            cnt for ast, cnt in asset_counts.items()
            if ast in _BTC_CORRELATED_ASSETS
        )
        return {
            "open_bets":        len(active),
            "asset_breakdown":  asset_counts,
            "btc_cluster":      btc_cluster,
            "total_at_risk":    round(total_cost, 2),
            "exposure_pct":     round(total_cost / bankroll * 100, 1) if bankroll > 0 else 0.0,
            "limits": {
                "max_total_open":      _MAX_TOTAL_OPEN_BETS,
                "max_same_asset":      _MAX_SAME_ASSET_BETS,
                "max_btc_cluster":     _MAX_BTC_CORRELATED_BETS,
                "max_exposure_pct":    (_MAX_OPEN_EXPOSURE_PCT or 0) * 100,
            },
        }
