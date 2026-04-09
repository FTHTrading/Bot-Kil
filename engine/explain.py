"""
engine/explain.py — Human-readable decision explanations
=========================================================
Converts a TradeFilterResult (+ optional raw model inputs) into a
structured, readable explanation of why the agent did or did not bet.
Feeds both the LLM system-prompt context and the audit log.

Usage:
    from engine.explain import explain_decision

    text = explain_decision(filter_result, pick=pick, verbose=True)
    print(text)
"""
from __future__ import annotations

import textwrap
from typing import Optional

from engine.trade_filter import TradeFilterResult


def explain_decision(
    result: TradeFilterResult,
    pick: Optional[dict] = None,
    verbose: bool = False,
) -> str:
    """
    Return a concise human-readable explanation of the trade decision.

    Parameters
    ----------
    result  : TradeFilterResult from TradeFilter.evaluate().
    pick    : optional raw pick dict for extra context (ticker, side, price).
    verbose : if True include full notes list and regime reason string.
    """
    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────────────
    asset = (pick or {}).get("asset", "?") if pick else (result.regime.asset if result.regime else "?")
    side  = (pick or {}).get("side", "YES") if pick else "?"
    price = (pick or {}).get("yes_price") or (pick or {}).get("price") or "?"

    if result.approved:
        lines.append(f"✓ TRADE APPROVED — {asset} {side} @ {price}")
    else:
        reason_label = result.abstain_reason.value if result.abstain_reason else "error"
        lines.append(f"✗ TRADE BLOCKED — {asset} {side} @ {price}  [{reason_label}]")

    lines.append(f"  {result.detail}")
    lines.append("")

    # ── Probability summary ──────────────────────────────────────────────────
    if result.ensemble:
        ens = result.ensemble
        model_str = "  ".join(
            f"{m}={v:.2f}" for m, v in sorted(ens.raw_probs.items())
        )
        lines.append(f"  Models ({ens.n_models}): {model_str}")
        lines.append(
            f"  Ensemble: prob={ens.weighted_prob:.3f}  "
            f"disagreement={ens.disagreement:.3f}  "
            f"confidence={ens.confidence:.3f}"
        )

    lines.append(
        f"  Calibration: raw={result.raw_edge_pct*100:+.1f}%  "
        f"cal={result.calibrated_edge_pct*100:+.1f}%  "
        f"method={result.calib_meta.get('method','?')}(n={result.calib_meta.get('n_samples',0)})"
    )

    if result.approved:
        lines.append(f"  Recommended stake: ${result.recommended_stake:.2f}")

    # ── Regime summary ───────────────────────────────────────────────────────
    if result.regime:
        r = result.regime
        lines.append(
            f"  Regime: {r.vol_regime}-vol  {r.trend}(conf={r.trend_confidence:.2f})  "
            f"ttc={r.hours_to_close:.1f}h({r.ttc_bucket})  dq={r.data_quality}"
        )
        rsi_str = f"rsi={r.rsi:.1f}" if r.rsi is not None else ""
        pb_str  = f"pb={r.bollinger_pb:.2f}" if r.bollinger_pb is not None else ""
        signals = "  ".join(filter(None, [rsi_str, pb_str]))
        if signals:
            lines.append(f"  Signals: {signals}")

    # ── Verbose notes ────────────────────────────────────────────────────────
    if verbose and result.notes:
        lines.append("")
        lines.append("  Decision notes:")
        for note in result.notes:
            lines.append(f"    • {note}")

    return "\n".join(lines)


def explain_abstain_short(result: TradeFilterResult) -> str:
    """One-line abstain summary for JSON tool responses."""
    if result.approved:
        return f"approved (edge={result.calibrated_edge_pct*100:+.1f}%, stake=${result.recommended_stake:.2f})"
    reason = result.abstain_reason.value if result.abstain_reason else "error"
    return f"blocked[{reason}]: {result.detail}"


def format_regime_for_prompt(result: TradeFilterResult) -> str:
    """
    Compact regime context string to inject into the LLM system prompt so
    the model has up-to-date market state without needing tool calls.
    """
    if not result.regime:
        return "regime=unknown"
    r = result.regime
    ens_conf = f"ens_conf={result.ensemble.confidence:.2f}" if result.ensemble else ""
    cal_n    = result.calib_meta.get("n_samples", 0)
    return (
        f"regime={r.vol_regime}-vol/{r.trend}(tc={r.trend_confidence:.2f}) "
        f"ttc={r.hours_to_close:.1f}h/{r.ttc_bucket} "
        f"dq={r.data_quality} {ens_conf} cal_n={cal_n}"
    )
