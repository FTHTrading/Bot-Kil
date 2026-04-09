#!/usr/bin/env python
"""
scripts/backtest_agent.py — Backtest the full engine pipeline against
historical Kalshi settlements.

What it does
------------
1. Loads all settled orders from the Kalshi API (or a local JSON cache).
2. For each settled position, reconstructs what our engine *would have*
   decided at the time.
3. Runs that pick through TradeFilter (regime + ensemble + calibration +
   abstain) using stored/simulated momentum snapshots.
4. Compares the engine's recommended action (BET / ABSTAIN) to the
   actual outcome and computes:
     - win rate, ROI, Brier score, edge calibration gap
     - per-regime and per-asset breakdowns
     - abstain rate and reasons

Usage
-----
    python scripts/backtest_agent.py              # uses live Kalshi history
    python scripts/backtest_agent.py --from-file logs/settlements.json
    python scripts/backtest_agent.py --dry-run    # print first 5 records only
    python scripts/backtest_agent.py --min-edge 0.05

Output
------
    Prints summary table to stdout.
    Writes detailed results to logs/backtest_results.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Add project root to path ─────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.trade_filter import TradeFilter
from engine.abstain      import NoTradeReason

log = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(message)s")


# ── Constants ─────────────────────────────────────────────────────────────────

_BANKROLL       = 200.0
_FEE_RATE       = 0.02
_RESULTS_PATH   = ROOT / "logs" / "backtest_results.json"
_CACHE_PATH     = ROOT / "logs" / "settlements.json"

# Crypto assets the engine knows about
_KNOWN_ASSETS   = {"BTC", "ETH", "SOL", "DOGE", "XRP"}

# Settlement result → win
_WIN_RESULT     = {"yes"}   # Kalshi result == "yes" means YES side wins


# ── Helpers ───────────────────────────────────────────────────────────────────

def _asset_from_ticker(ticker: str) -> str:
    """Extract crypto asset from a Kalshi market ticker."""
    t = ticker.upper()
    for a in ("BTC", "ETH", "SOL", "DOGE", "XRP"):
        if a in t:
            return a
    return "BTC"   # fallback


def _side_from_order(order: dict) -> str:
    return str(order.get("side", "yes")).lower()


def _price_from_order(order: dict, side: str) -> float:
    """Return the fill price (0-1 fraction) for the given side."""
    if side == "yes":
        cp = order.get("yes_price") or order.get("avg_price") or 50
    else:
        cp = order.get("no_price") or 50
    return float(cp) / 100.0


def _ttc_hours_from_order(order: dict) -> float:
    """Approximate hours-to-close at time of order from metadata."""
    # Orders settled same session → ~1h; absent data → assume 3h
    exp = order.get("expiration_time") or order.get("close_time")
    created = order.get("created_time") or order.get("taker_fill_cost")
    if exp and created:
        try:
            t0 = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            hours = (t1 - t0).total_seconds() / 3600
            return max(0.1, min(hours, 168.0))
        except Exception:
            pass
    return 3.0


def _mock_momentum(asset: str) -> dict:
    """
    Generate a plausible baseline momentum snapshot for backtesting when
    live feeds are unavailable.  Values are deliberately neutral / moderate
    so that the engine produces realistic but not always-approve outcomes.
    """
    return {
        "mom_5m":       0.0003,
        "mom_15m":      0.0001,
        "realized_vol": 0.003,     # ~normal vol
        "trend":        "flat",
        "closes":       [60000.0 + i * 10 for i in range(5)],
        "current":      60040.0,
    }


def _outcome_from_settlement(settlement: dict, side: str) -> int:
    """Return 1 (win) or 0 (loss) for the given side vs settlement result."""
    result = str(settlement.get("result", "")).lower()
    if result == "yes":
        return 1 if side == "yes" else 0
    if result == "no":
        return 0 if side == "yes" else 1
    return -1   # unknown / void


# ── Load settled positions ────────────────────────────────────────────────────

async def _load_settlements(from_file: Optional[str]) -> list[dict]:
    """Load from file cache or live API."""
    if from_file:
        path = Path(from_file)
        if not path.exists():
            log.error("File not found: %s", from_file)
            return []
        with open(path) as f:
            data = json.load(f)
        records = data if isinstance(data, list) else data.get("settlements", [])
        log.info("Loaded %d records from %s", len(records), path)
        return records

    # Live API
    try:
        from data.feeds import kalshi
        settlements = await kalshi.get_settlements()
        log.info("Fetched %d settlements from API", len(settlements))
        # Cache for re-use
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump(settlements, f, indent=2)
        return settlements
    except Exception as exc:
        log.warning("Could not fetch live settlements: %s", exc)
        return []


# ── Per-record engine evaluation ─────────────────────────────────────────────

def _evaluate_one(
    tf: TradeFilter,
    settlement: dict,
    min_edge: float = 0.0,
) -> Optional[dict]:
    """
    Run one settled position through the engine pipeline.
    Returns a result dict or None if the record should be skipped.
    """
    ticker = settlement.get("market_ticker") or settlement.get("ticker", "")
    if not ticker:
        return None

    asset  = _asset_from_ticker(ticker)
    side   = _side_from_order(settlement)
    price  = _price_from_order(settlement, side)
    ttc    = _ttc_hours_from_order(settlement)
    result = str(settlement.get("result", "")).lower()
    outcome = _outcome_from_settlement(settlement, side)

    if outcome == -1:
        return None   # void / unknown settlement

    # Build pick dict
    pick  = {"asset": asset, "side": side, "yes_price": price, "ttc": ttc}
    mom   = _mock_momentum(asset)

    # Use neutral model probs (no live model at backtest time)
    # These represent what a naive pre-ensemble estimate would be
    model_probs = {
        "diffusion":   0.50 + (price - 0.50) * 0.5,    # anchor near market price
        "monte_carlo": 0.50 + (price - 0.50) * 0.4,
        "neural":      0.50 + (price - 0.50) * 0.6,
        "technical":   0.50,
    }

    try:
        result_obj = tf.evaluate(pick, mom, model_probs, bankroll=_BANKROLL, fee_rate=_FEE_RATE)
    except Exception as exc:
        log.debug("evaluate() error for %s: %s", ticker, exc)
        return None

    edge = result_obj.calibrated_edge_pct
    if min_edge > 0 and edge < min_edge:
        return None

    return {
        "ticker":          ticker,
        "asset":           asset,
        "side":            side,
        "price":           round(price, 3),
        "ttc_hours":       round(ttc, 2),
        "outcome":         outcome,
        "approved":        result_obj.approved,
        "abstain_reason":  result_obj.abstain_reason.value if result_obj.abstain_reason else None,
        "cal_edge_pct":    round(edge * 100, 2),
        "cal_prob":        round(result_obj.calibrated_prob, 4),
        "regime_trend":    result_obj.regime.trend if result_obj.regime else "?",
        "regime_vol":      result_obj.regime.vol_regime if result_obj.regime else "?",
        "confidence":      round(result_obj.ensemble.confidence, 3) if result_obj.ensemble else 0.0,
        "recommended_stake": result_obj.recommended_stake,
    }


# ── Aggregate stats ───────────────────────────────────────────────────────────

def _compute_stats(records: list[dict], label: str = "ALL") -> dict:
    approved = [r for r in records if r["approved"]]
    if not approved:
        return {"label": label, "approved": 0, "abstained": len(records) - len(approved)}

    outcomes  = [r["outcome"] for r in approved]
    wins      = sum(outcomes)
    n         = len(approved)
    win_rate  = wins / n

    probs     = [r["cal_prob"] for r in approved]
    brier     = sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / n

    stakes    = [r["recommended_stake"] for r in approved]
    # Simplified P&L: win → (1/price - 1) * stake, lose → -stake
    pnls = []
    for r in approved:
        p = max(0.01, r["price"])
        payout = (1.0 / p - 1.0) * r["recommended_stake"]
        pnls.append(payout if r["outcome"] == 1 else -r["recommended_stake"])

    total_staked = sum(stakes)
    total_pnl    = sum(pnls)
    roi          = total_pnl / total_staked if total_staked > 0 else 0.0
    avg_edge     = sum(r["cal_edge_pct"] for r in approved) / n

    abstain_reasons: dict[str, int] = {}
    for r in records:
        if not r["approved"] and r["abstain_reason"]:
            abstain_reasons[r["abstain_reason"]] = abstain_reasons.get(r["abstain_reason"], 0) + 1

    return {
        "label":           label,
        "total_records":   len(records),
        "approved":        n,
        "abstained":       len(records) - n,
        "abstain_rate_pct": round((len(records) - n) / len(records) * 100, 1),
        "abstain_reasons": abstain_reasons,
        "win_rate":        round(win_rate * 100, 1),
        "brier_score":     round(brier, 4),
        "avg_edge_pct":    round(avg_edge, 2),
        "total_staked":    round(total_staked, 2),
        "total_pnl":       round(total_pnl, 2),
        "roi_pct":         round(roi * 100, 1),
    }


def _print_stats(s: dict):
    print(f"\n{'-' * 60}")
    print(f"  {s['label']}")
    print(f"{'-' * 60}")
    print(f"  Records      : {s['total_records']}")
    print(f"  Approved bets: {s['approved']}")
    print(f"  Abstained    : {s['abstained']} ({s.get('abstain_rate_pct', 0):.1f}%)")
    if s['approved']:
        print(f"  Win rate     : {s['win_rate']:.1f}%")
        print(f"  Brier score  : {s['brier_score']:.4f}  (lower is better)")
        print(f"  Avg edge     : {s['avg_edge_pct']:+.2f}%")
        print(f"  Total staked : ${s['total_staked']:.2f}")
        print(f"  Total P&L    : ${s['total_pnl']:+.2f}  (ROI {s['roi_pct']:+.1f}%)")
    if s.get("abstain_reasons"):
        print(f"  Abstain breakdown:")
        for r, count in sorted(s["abstain_reasons"].items(), key=lambda x: -x[1]):
            print(f"    {r:35s}: {count}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args):
    settlements = await _load_settlements(args.from_file)
    if not settlements:
        print("No settlements found. Pass --from-file or ensure Kalshi API credentials.")
        return

    tf = TradeFilter()
    records: list[dict] = []

    for i, s in enumerate(settlements):
        if args.dry_run and i >= 5:
            break
        rec = _evaluate_one(tf, s, min_edge=args.min_edge)
        if rec is not None:
            records.append(rec)

    if not records:
        print("No valid records after filtering.")
        return

    # Overall stats
    overall = _compute_stats(records)
    _print_stats(overall)

    # Per-asset breakdown
    print("\n  Per-asset:")
    assets = sorted({r["asset"] for r in records})
    for a in assets:
        sub = [r for r in records if r["asset"] == a]
        s = _compute_stats(sub, label=a)
        if s["approved"]:
            print(f"    {a:5s}  win={s['win_rate']:.0f}%  "
                  f"edge={s['avg_edge_pct']:+.1f}%  "
                  f"n={s['approved']}  roi={s['roi_pct']:+.1f}%")

    # Per-regime breakdown
    print("\n  Per trend regime:")
    trends = sorted({r["regime_trend"] for r in records})
    for t in trends:
        sub = [r for r in records if r["regime_trend"] == t]
        s = _compute_stats(sub, label=t)
        if s["approved"]:
            print(f"    {t:15s}  win={s['win_rate']:.0f}%  "
                  f"Brier={s['brier_score']:.4f}  "
                  f"n={s['approved']}  roi={s['roi_pct']:+.1f}%")

    # Save detailed results
    _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_RESULTS_PATH, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "overall":      overall,
            "records":      records,
        }, f, indent=2)
    print(f"\n  Full results written to {_RESULTS_PATH}\n")


def _parse_args():
    p = argparse.ArgumentParser(description="Backtest Kalshi edge engine against settled history")
    p.add_argument("--from-file", metavar="PATH",
                   help="Load settlements from local JSON file instead of API")
    p.add_argument("--min-edge", type=float, default=0.0,
                   help="Only include records with edge >= this threshold (e.g. 0.05)")
    p.add_argument("--dry-run", action="store_true",
                   help="Process only first 5 records and print")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(_parse_args()))
