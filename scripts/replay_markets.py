#!/usr/bin/env python
"""
scripts/replay_markets.py — Replay saved market snapshots through the full
engine pipeline to audit what decisions the agent *would have* made.

Snapshot format (JSON list of objects)
---------------------------------------
[
  {
    "timestamp":   "2025-05-01T12:00:00Z",
    "ticker":      "KXETH-25MAY12-T3485",
    "asset":       "ETH",
    "side":        "yes",
    "yes_price":   0.42,
    "ttc_hours":   3.0,
    "snapshot": {
      "mom_5m":       0.0005,
      "mom_15m":      0.0002,
      "realized_vol": 0.0035,
      "trend":        "trending",
      "closes":       [3480, 3482, 3483, 3485, 3488],
      "current":      3488.0
    },
    "model_probs": {
      "diffusion":   0.56,
      "monte_carlo": 0.54,
      "neural":      0.60,
      "technical":   0.52
    },
    "actual_outcome": 1
  },
  ...
]

If no "model_probs" are present the script synthesises plausible neutral
estimates centred on the market YES price.

Usage
-----
    python scripts/replay_markets.py logs/market_snapshots.json
    python scripts/replay_markets.py logs/market_snapshots.json --verbose
    python scripts/replay_markets.py logs/market_snapshots.json --approved-only
    python scripts/replay_markets.py logs/market_snapshots.json --abstain-only
    python scripts/replay_markets.py logs/market_snapshots.json --out logs/replay_out.json
    python scripts/replay_markets.py --demo   # generate synthetic demo data and replay it
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Project root on path ──────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.trade_filter import TradeFilter, TradeFilterResult
from engine.abstain      import NoTradeReason
from engine.explain      import explain_decision

log = logging.getLogger("replay")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ── Constants ─────────────────────────────────────────────────────────────────
_BANKROLL = 200.0
_FEE_RATE = 0.02
_DEMO_ASSETS = ["BTC", "ETH", "SOL", "DOGE", "XRP"]


# ── Model probs synthesiser ───────────────────────────────────────────────────

def _synthetic_model_probs(yes_price: float, trend: str = "flat") -> dict:
    """
    Generate plausible model probabilities centred on the market YES price.
    Used when a snapshot doesn't include recorded model outputs.
    """
    base   = max(0.05, min(0.95, yes_price))
    jitter = 0.04
    rng    = random.Random(int(yes_price * 1000))
    probs  = {
        "diffusion":   base + rng.uniform(-jitter, jitter),
        "monte_carlo": base + rng.uniform(-jitter, jitter),
        "neural":      base + rng.uniform(-jitter * 1.5, jitter * 1.5),
        "technical":   base + rng.uniform(-jitter, jitter),
    }
    # Trending: nudge neural higher
    if trend in ("trending", "strong_trend"):
        probs["neural"] += 0.03
    return {k: round(max(0.01, min(0.99, v)), 4) for k, v in probs.items()}


# ── Demo data generator ───────────────────────────────────────────────────────

def _generate_demo_snapshots(n: int = 25, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    snaps = []
    trends = ["flat", "trending", "mean_reverting"]
    vols   = [0.0015, 0.003, 0.005, 0.007]
    for i in range(n):
        asset   = rng.choice(_DEMO_ASSETS)
        trend   = rng.choice(trends)
        vol     = rng.choice(vols)
        price   = rng.uniform(0.20, 0.80)
        ttc     = rng.choice([1.0, 3.0, 6.0, 24.0])
        outcome = 1 if rng.random() < price else 0   # biased towards price being correct
        base_close = {"BTC": 60000, "ETH": 3000, "SOL": 150, "DOGE": 0.12, "XRP": 0.55}
        bc  = base_close.get(asset, 100.0)
        mom_5m  = rng.uniform(-0.002, 0.002)
        closes  = [bc * (1 + rng.gauss(0, vol)) for _ in range(5)]
        snaps.append({
            "timestamp":      "2025-05-01T{:02d}:00:00Z".format(i % 24),
            "ticker":         f"KX{asset}-DEMO{i:03d}",
            "asset":          asset,
            "side":           "yes",
            "yes_price":      round(price, 3),
            "ttc_hours":      ttc,
            "snapshot": {
                "mom_5m":        round(mom_5m, 6),
                "mom_15m":       round(mom_5m * 0.7, 6),
                "realized_vol":  round(vol, 5),
                "trend":         trend,
                "closes":        [round(c, 4) for c in closes],
                "current":       round(closes[-1], 4),
            },
            "model_probs":    _synthetic_model_probs(price, trend),
            "actual_outcome": outcome,
        })
    return snaps


# ── Load snapshots ────────────────────────────────────────────────────────────

def _load_snapshots(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        log.error("File not found: %s", path)
        sys.exit(1)
    with open(p) as f:
        data = json.load(f)
    records = data if isinstance(data, list) else data.get("snapshots", [])
    log.info("Loaded %d snapshots from %s", len(records), p)
    return records


# ── Replay one snapshot ───────────────────────────────────────────────────────

def _replay_one(
    tf: TradeFilter,
    snap: dict,
    verbose: bool = False,
) -> dict:
    asset    = snap.get("asset", "BTC")
    side     = snap.get("side", "yes")
    price    = float(snap.get("yes_price", 0.50))
    ttc      = float(snap.get("ttc_hours", 3.0))
    ticker   = snap.get("ticker", "UNKNOWN")
    ts       = snap.get("timestamp", "")
    outcome  = snap.get("actual_outcome", -1)
    mom_data = snap.get("snapshot") or {}
    trend    = mom_data.get("trend", "flat")

    probs = snap.get("model_probs") or _synthetic_model_probs(price, trend)

    pick = {
        "asset":     asset,
        "side":      side,
        "yes_price": price,
        "ttc":       ttc,
        "ticker":    ticker,
    }

    try:
        result: TradeFilterResult = tf.evaluate(
            pick, mom_data, probs,
            bankroll=_BANKROLL,
            fee_rate=_FEE_RATE,
        )
    except Exception as exc:
        log.warning("evaluate() raised for %s: %s", ticker, exc)
        return {
            "ticker":  ticker,
            "ts":      ts,
            "error":   str(exc),
        }

    decision  = "APPROVE" if result.approved else "ABSTAIN"
    reason    = result.abstain_reason.value if result.abstain_reason else ""
    conf      = round(result.ensemble.confidence, 3) if result.ensemble else 0.0
    edge      = round(result.calibrated_edge_pct * 100, 2)
    cal_prob  = round(result.calibrated_prob, 4)

    # Outcome assessment
    if outcome == -1:
        outcome_label = "UNKNOWN"
        correct       = None
    else:
        outcome_label = "WIN" if outcome == 1 else "LOSS"
        correct = (result.approved and outcome == 1) or (not result.approved)

    # Print line
    flag  = "OK" if correct else ("XX" if correct is not None else " ?")
    print(
        f"  {flag}  {ts[:19]:19s}  {asset:4s}  {side:3s}  "
        f"p={price:.2f}  ttc={ttc:5.1f}h  "
        f"{decision:7s}  {reason:35s}  "
        f"edge={edge:+6.1f}%  conf={conf:.2f}  "
        f"stake=${result.recommended_stake:.2f}  "
        f"→{outcome_label}"
    )

    if verbose:
        try:
            explanation = explain_decision(result, pick=pick, verbose=True)
            for line in explanation.splitlines():
                print(f"         {line}")
        except Exception:
            pass

    return {
        "ticker":           ticker,
        "timestamp":        ts,
        "asset":            asset,
        "side":             side,
        "price":            round(price, 4),
        "ttc_hours":        ttc,
        "decision":         decision,
        "abstain_reason":   reason or None,
        "confidence":       conf,
        "cal_prob":         cal_prob,
        "cal_edge_pct":     edge,
        "recommended_stake": result.recommended_stake,
        "actual_outcome":   outcome,
        "correct":          correct,
        "regime_trend":     result.regime.trend if result.regime else None,
        "regime_vol":       result.regime.vol_regime if result.regime else None,
    }


# ── Summary stats ─────────────────────────────────────────────────────────────

def _summarise(results: list[dict]):
    valid   = [r for r in results if "error" not in r]
    approved = [r for r in valid if r["decision"] == "APPROVE"]
    abstained = [r for r in valid if r["decision"] == "ABSTAIN"]
    with_outcome = [r for r in approved if r["actual_outcome"] != -1]

    print(f"\n{'=' * 70}")
    print(f"  REPLAY SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total snapshots : {len(valid)}")
    print(f"  Approved        : {len(approved)} ({len(approved)/len(valid)*100:.0f}%)" if valid else "")
    print(f"  Abstained       : {len(abstained)}")

    # Abstain breakdown
    if abstained:
        reasons: dict[str, int] = {}
        for r in abstained:
            k = r.get("abstain_reason") or "unknown"
            reasons[k] = reasons.get(k, 0) + 1
        print("  Abstain reasons:")
        for rk, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {rk:40s}: {cnt}")

    # Outcome stats for approved bets
    if with_outcome:
        wins    = sum(1 for r in with_outcome if r["actual_outcome"] == 1)
        n       = len(with_outcome)
        brier   = sum((r["cal_prob"] - r["actual_outcome"]) ** 2 for r in with_outcome) / n
        avg_e   = sum(r["cal_edge_pct"] for r in with_outcome) / n

        stakes  = [r["recommended_stake"] for r in with_outcome]
        pnls    = []
        for r in with_outcome:
            p = max(0.01, r["price"])
            payout = (1.0 / p - 1.0) * r["recommended_stake"]
            pnls.append(payout if r["actual_outcome"] == 1 else -r["recommended_stake"])

        print(f"\n  Approved bets with known outcome: {n}")
        print(f"  Win rate     : {wins/n*100:.1f}%")
        print(f"  Brier score  : {brier:.4f}  (lower is better)")
        print(f"  Avg edge     : {avg_e:+.2f}%")
        print(f"  Total staked : ${sum(stakes):.2f}")
        print(f"  Total P&L    : ${sum(pnls):+.2f}  "
              f"(ROI {sum(pnls)/sum(stakes)*100:+.1f}%)" if sum(stakes) else "")

    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main(args):
    if args.demo:
        log.info("Generating %d synthetic demo snapshots…", args.demo_n)
        snapshots = _generate_demo_snapshots(n=args.demo_n)
    else:
        if not args.input_file:
            print("Provide a snapshot file path or use --demo. See --help.")
            sys.exit(1)
        snapshots = _load_snapshots(args.input_file)

    tf = TradeFilter()
    results: list[dict] = []

    print(f"\n  {'TS':19s}  {'ASSET':4s}  {'SIDE':3s}  "
          f"{'PRICE':6s}  {'TTC':7s}  {'DECISION':7s}  "
          f"{'ABSTAIN REASON':35s}  "
          f"{'EDGE':9s}  {'CONF':6s}  {'STAKE':9s}  OUTCOME")
    print(f"  {'-' * 150}")

    for snap in snapshots:
        decision_label = snap.get("decision")
        if args.approved_only and decision_label == "ABSTAIN":
            continue
        if args.abstain_only and decision_label == "APPROVE":
            continue
        rec = _replay_one(tf, snap, verbose=args.verbose)
        results.append(rec)

    _summarise(results)

    # Write output
    out_path = Path(args.out) if args.out else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "results":      results,
            }, f, indent=2)
        print(f"  Full results written to {out_path}\n")


def _parse_args():
    p = argparse.ArgumentParser(
        description="Replay market snapshots through the engine pipeline"
    )
    p.add_argument("input_file", nargs="?", metavar="SNAPSHOTS_JSON",
                   help="Path to market_snapshots.json")
    p.add_argument("--demo", action="store_true",
                   help="Use a synthetic demo dataset instead of a file")
    p.add_argument("--demo-n", type=int, default=25,
                   help="Number of demo snapshots to generate (default 25)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print full engine explanation for each record")
    p.add_argument("--approved-only", action="store_true",
                   help="Only show APPROVED decisions")
    p.add_argument("--abstain-only", action="store_true",
                   help="Only show ABSTAIN decisions")
    p.add_argument("--out", metavar="PATH",
                   help="Write full JSON results to this file")
    return p.parse_args()


if __name__ == "__main__":
    main(_parse_args())
