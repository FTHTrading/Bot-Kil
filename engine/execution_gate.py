"""
engine/execution_gate.py — Pre-order quality gate
===================================================
Fetches the live orderbook and scores the fill environment before
any real order goes to Kalshi.  Returns a structured result that
tells the caller whether to proceed, reduce contracts, or abort.

Usage
-----
    from engine.execution_gate import ExecutionGate, ExecutionGateResult
    gate = ExecutionGate()
    result = await gate.check(ticker, side="yes", yes_price=42, contracts=2)
    if not result.approved:
        return REJECTED
    contracts = result.recommended_contracts
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# ── Quality thresholds ────────────────────────────────────────────────────────
_MAX_SPREAD_CENTS       = 15     # hard reject if spread > this
_MIN_DEPTH_CONTRACTS    = 15     # minimum total depth at best level to proceed
_MAX_SLIPPAGE_FRACTION  = 0.30   # reject if contracts > 30 % of best-level depth
_SETTLEMENT_BUFFER_SECS = 180    # seconds before close to block execution

# Quality-score bands
_SCORE_FULL   = 0.70   # ≥ 0.70 → proceed at requested contracts
_SCORE_HALF   = 0.40   # ≥ 0.40 → reduce to half and proceed
                       # < 0.40 → reject


@dataclass
class ExecutionGateResult:
    approved:             bool
    reason:               str
    quality_score:        float   # 0-1 composite liquidity/spread score
    recommended_contracts: int    # may be reduced from requested
    spread_cents:         float
    slippage_estimate_cents: float
    depth_yes:            int     # contracts available at best yes bid
    depth_no:             int     # contracts available at best no bid
    liquidity_confidence: float   # 0-1 how reliable the book looks


class ExecutionGate:
    """
    Stateless gate — instantiate once and call `check()` per intended order.
    """

    async def check(
        self,
        ticker:          str,
        side:            str,    # "yes" or "no"
        yes_price:       int,    # cents, 1-99
        contracts:       int,
        seconds_to_settle: Optional[float] = None,
    ) -> ExecutionGateResult:
        """
        Perform all quality checks.  Never raises — returns a rejected result
        on any exception so the caller can log and skip cleanly.
        """
        try:
            return await self._check(ticker, side, yes_price, contracts, seconds_to_settle)
        except Exception as exc:
            log.error("[execution_gate] Unexpected error for %s: %s", ticker, exc)
            return ExecutionGateResult(
                approved=False,
                reason=f"gate_exception: {exc}",
                quality_score=0.0,
                recommended_contracts=0,
                spread_cents=0.0,
                slippage_estimate_cents=0.0,
                depth_yes=0,
                depth_no=0,
                liquidity_confidence=0.0,
            )

    async def _check(
        self,
        ticker:          str,
        side:            str,
        yes_price:       int,
        contracts:       int,
        seconds_to_settle: Optional[float],
    ) -> ExecutionGateResult:

        # ── Settlement buffer ─────────────────────────────────────────────────
        if seconds_to_settle is not None and seconds_to_settle < _SETTLEMENT_BUFFER_SECS:
            return ExecutionGateResult(
                approved=False,
                reason=f"settlement_buffer: {seconds_to_settle:.0f}s < {_SETTLEMENT_BUFFER_SECS}s",
                quality_score=0.0,
                recommended_contracts=0,
                spread_cents=0.0,
                slippage_estimate_cents=0.0,
                depth_yes=0,
                depth_no=0,
                liquidity_confidence=0.0,
            )

        # ── Fetch live orderbook ──────────────────────────────────────────────
        ob = await self._fetch_orderbook(ticker)
        if ob is None:
            return ExecutionGateResult(
                approved=False,
                reason="orderbook_unavailable",
                quality_score=0.0,
                recommended_contracts=0,
                spread_cents=0.0,
                slippage_estimate_cents=0.0,
                depth_yes=0,
                depth_no=0,
                liquidity_confidence=0.0,
            )

        yes_levels = ob.get("yes", [])
        no_levels  = ob.get("no",  [])

        # Best bid/ask
        best_yes_bid = max((p for p, _ in yes_levels), default=0)
        best_no_bid  = max((p for p, _ in no_levels),  default=0)
        yes_ask      = 100 - best_no_bid  # Kalshi: yes_ask = 100 - best_no_bid

        spread_cents = float(max(0, yes_ask - best_yes_bid))

        # ── Spread gate ───────────────────────────────────────────────────────
        if spread_cents > _MAX_SPREAD_CENTS:
            return ExecutionGateResult(
                approved=False,
                reason=f"spread_too_wide: {spread_cents:.1f}¢ > {_MAX_SPREAD_CENTS}¢",
                quality_score=0.0,
                recommended_contracts=0,
                spread_cents=spread_cents,
                slippage_estimate_cents=0.0,
                depth_yes=0,
                depth_no=0,
                liquidity_confidence=0.0,
            )

        # ── Depth (top 3 levels) ──────────────────────────────────────────────
        depth_yes = sum(s for _, s in yes_levels[:3])
        depth_no  = sum(s for _, s in no_levels[:3])
        total_depth = depth_yes + depth_no

        if total_depth < _MIN_DEPTH_CONTRACTS:
            return ExecutionGateResult(
                approved=False,
                reason=f"depth_too_low: {total_depth} < {_MIN_DEPTH_CONTRACTS}",
                quality_score=0.0,
                recommended_contracts=0,
                spread_cents=spread_cents,
                slippage_estimate_cents=0.0,
                depth_yes=depth_yes,
                depth_no=depth_no,
                liquidity_confidence=0.0,
            )

        # ── Slippage estimate ─────────────────────────────────────────────────
        side_depth   = depth_yes if side.lower() == "yes" else depth_no
        best_depth   = max(side_depth, 1)
        fill_fraction = contracts / best_depth
        # Rough linear slippage assumption: each % of book consumed ≈ 0.5 ¢
        slippage_estimate = fill_fraction * spread_cents * 0.5

        if fill_fraction > _MAX_SLIPPAGE_FRACTION:
            # Too large relative to book — cap contracts
            safe_contracts = max(1, int(best_depth * _MAX_SLIPPAGE_FRACTION))
            contracts      = min(contracts, safe_contracts)

        # ── Quality score ─────────────────────────────────────────────────────
        quality_score     = _score(spread_cents, total_depth, fill_fraction)
        liquidity_confidence = min(1.0, total_depth / 100.0)

        if quality_score < _SCORE_HALF:
            return ExecutionGateResult(
                approved=False,
                reason=f"quality_too_low: {quality_score:.2f}",
                quality_score=quality_score,
                recommended_contracts=0,
                spread_cents=spread_cents,
                slippage_estimate_cents=slippage_estimate,
                depth_yes=depth_yes,
                depth_no=depth_no,
                liquidity_confidence=liquidity_confidence,
            )

        # Reduce to half if only marginally acceptable
        if quality_score < _SCORE_FULL:
            contracts = max(1, contracts // 2)

        return ExecutionGateResult(
            approved=True,
            reason=f"approved: quality={quality_score:.2f}",
            quality_score=quality_score,
            recommended_contracts=contracts,
            spread_cents=spread_cents,
            slippage_estimate_cents=slippage_estimate,
            depth_yes=depth_yes,
            depth_no=depth_no,
            liquidity_confidence=liquidity_confidence,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _fetch_orderbook(self, ticker: str) -> Optional[dict]:
        try:
            from data.feeds.kalshi import get_market_orderbook
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, get_market_orderbook, ticker)
        except Exception as exc:
            log.warning("[execution_gate] orderbook fetch error for %s: %s", ticker, exc)
            return None


def _score(spread_cents: float, depth: int, fill_fraction: float) -> float:
    """
    Composite quality score 0-1.
    Spread: 0 ¢ → 1.0, 15 ¢ → 0.0 (linear)
    Depth: 0 → 0.0, 100+ → 1.0 (capped)
    Fill:  0 % → 1.0, 30 % → 0.0 (linear)
    Weighted average: 50 % spread, 30 % depth, 20 % fill
    """
    s_score = max(0.0, 1.0 - spread_cents / _MAX_SPREAD_CENTS)
    d_score = min(1.0, depth / 100.0)
    f_score = max(0.0, 1.0 - fill_fraction / _MAX_SLIPPAGE_FRACTION)
    return 0.50 * s_score + 0.30 * d_score + 0.20 * f_score
