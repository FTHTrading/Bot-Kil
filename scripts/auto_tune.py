"""
auto_tune.py — Analyze trade history and suggest/apply parameter adjustments
============================================================================
Run:  python scripts/auto_tune.py           (analyze only — show suggestions)
      python scripts/auto_tune.py --apply   (write tuned values to intraday_ev.py)

Reads all trade history and settlements, computes what parameter changes
would have improved results, and can auto-apply them.

Safety: --apply writes to a backup first, and all changes are bounded
within safe ranges (no YOLO parameters).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# Force UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from engine.tracker import compute_performance, load_picks, suggest_tuning

_EV_PATH = _PROJECT_ROOT / "engine" / "intraday_ev.py"

# Safe ranges for parameters
_SAFE_RANGES = {
    "_MIN_EDGE":         (0.03, 0.12),    # 3% to 12%
    "_MIN_BET_PRICE":    (0.03, 0.20),    # 3¢ to 20¢
    "_MIN_GAP_PCT_EARLY": (0.03, 0.15),   # 0.03% to 0.15%
    "_MIN_GAP_PCT_LATE":  (0.005, 0.03),  # 0.005% to 0.03%
    "_KELLY_FRACTION":   (0.05, 0.15),    # 5% to 15%
    "_MOMENTUM_K":       (0.10, 0.25),    # momentum factor
    "_PROB_CAP":         (0.75, 0.90),    # max probability
    "_FEE_RATE":         (0.01, 0.03),    # fee estimate
}


def analyze_and_suggest() -> list[dict]:
    """Return list of {param, current, suggested, reason} dicts."""
    perf = compute_performance(days=7)
    picks = load_picks(days=7)
    adjustments = []

    n_settled = perf["total_settled"]
    win_rate = perf["win_rate"]
    roi = perf["roi_pct"]
    n_picks = perf["total_picks"]
    n_placed = perf["total_placed"]
    avg_edge = perf["avg_edge"]

    # Read current parameters from file
    current = _read_current_params()

    # 1. MIN_EDGE tuning
    cur_edge = current.get("_MIN_EDGE", 0.05)
    if n_settled >= 5:
        if win_rate > 65 and roi > 10:
            new_edge = max(cur_edge - 0.01, _SAFE_RANGES["_MIN_EDGE"][0])
            if new_edge < cur_edge:
                adjustments.append({
                    "param": "_MIN_EDGE", "current": cur_edge, "suggested": new_edge,
                    "reason": f"Win rate {win_rate}% and ROI {roi}% are strong — lower edge to catch more trades"
                })
        elif win_rate < 45 or roi < -15:
            new_edge = min(cur_edge + 0.02, _SAFE_RANGES["_MIN_EDGE"][1])
            if new_edge > cur_edge:
                adjustments.append({
                    "param": "_MIN_EDGE", "current": cur_edge, "suggested": new_edge,
                    "reason": f"Win rate {win_rate}% / ROI {roi}% too low — raise edge threshold"
                })

    # 2. KELLY tuning based on realized edge
    cur_kelly = current.get("_KELLY_FRACTION", 0.10)
    if n_settled >= 10 and roi > 15:
        new_kelly = min(cur_kelly + 0.01, _SAFE_RANGES["_KELLY_FRACTION"][1])
        if new_kelly > cur_kelly:
            adjustments.append({
                "param": "_KELLY_FRACTION", "current": cur_kelly, "suggested": new_kelly,
                "reason": f"ROI {roi}% over {n_settled} trades — safe to increase Kelly fraction"
            })
    elif n_settled >= 5 and roi < -10:
        new_kelly = max(cur_kelly - 0.02, _SAFE_RANGES["_KELLY_FRACTION"][0])
        if new_kelly < cur_kelly:
            adjustments.append({
                "param": "_KELLY_FRACTION", "current": cur_kelly, "suggested": new_kelly,
                "reason": f"Negative ROI {roi}% — reduce Kelly to limit drawdown"
            })

    # 3. Gap filter tuning
    cur_gap = current.get("_MIN_GAP_PCT_EARLY", 0.05)
    # If lots of picks generated but few fire → gap may be too tight
    if n_picks > 20 and n_placed < 3:
        new_gap = max(cur_gap - 0.01, _SAFE_RANGES["_MIN_GAP_PCT_EARLY"][0])
        if new_gap < cur_gap:
            adjustments.append({
                "param": "_MIN_GAP_PCT_EARLY", "current": cur_gap, "suggested": new_gap,
                "reason": f"{n_picks} picks but only {n_placed} placed — loosen early gap filter"
            })
    elif n_settled >= 5 and win_rate < 40:
        new_gap = min(cur_gap + 0.02, _SAFE_RANGES["_MIN_GAP_PCT_EARLY"][1])
        if new_gap > cur_gap:
            adjustments.append({
                "param": "_MIN_GAP_PCT_EARLY", "current": cur_gap, "suggested": new_gap,
                "reason": f"Win rate {win_rate}% — tighten gap filter to improve quality"
            })

    # 4. Confidence analysis — check if low-conf picks are dragging down results
    conf_buckets = perf.get("conf_buckets", {})
    low = conf_buckets.get("0-25", {})
    if low.get("trades", 0) >= 3 and low.get("win_rate", 50) < 35:
        adjustments.append({
            "param": "_MIN_CONFIDENCE",
            "current": 0,
            "suggested": 25,
            "reason": f"Low-confidence trades (0-25) only winning {low['win_rate']}% — add confidence floor"
        })

    # 5. Asset exclusion suggestions
    asset_stats = perf.get("asset_stats", {})
    for asset, stats in asset_stats.items():
        if stats["trades"] >= 5 and stats["win_rate"] < 30:
            adjustments.append({
                "param": f"EXCLUDE_{asset}",
                "current": "included",
                "suggested": "excluded",
                "reason": f"{asset} win rate only {stats['win_rate']}% over {stats['trades']} trades"
            })

    return adjustments


def _read_current_params() -> dict:
    """Read current parameter values from intraday_ev.py."""
    if not _EV_PATH.exists():
        return {}
    text = _EV_PATH.read_text(encoding="utf-8")
    params = {}
    for param in _SAFE_RANGES:
        match = re.search(rf'^{re.escape(param)}\s*=\s*([0-9.]+)', text, re.MULTILINE)
        if match:
            params[param] = float(match.group(1))
    return params


def apply_adjustments(adjustments: list[dict]):
    """Apply parameter adjustments to intraday_ev.py (with backup)."""
    if not _EV_PATH.exists():
        print("  ERROR: intraday_ev.py not found")
        return

    # Only apply params that exist in the safe ranges (skip EXCLUDE_ etc.)
    applicable = [a for a in adjustments if a["param"] in _SAFE_RANGES]
    if not applicable:
        print("  No applicable parameter changes to write")
        return

    # Backup
    backup = _EV_PATH.with_suffix(f".py.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(_EV_PATH, backup)
    print(f"  Backup: {backup.name}")

    text = _EV_PATH.read_text(encoding="utf-8")
    for adj in applicable:
        param = adj["param"]
        new_val = adj["suggested"]
        lo, hi = _SAFE_RANGES[param]
        new_val = max(lo, min(hi, new_val))

        pattern = rf'^({re.escape(param)}\s*=\s*)[0-9.]+'
        replacement = rf'\g<1>{new_val}'
        new_text = re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
        if new_text != text:
            text = new_text
            print(f"  Updated {param}: {adj['current']} → {new_val}")
        else:
            print(f"  SKIP {param}: pattern not found in file")

    _EV_PATH.write_text(text, encoding="utf-8")
    print("  Changes written to intraday_ev.py")


def main():
    parser = argparse.ArgumentParser(description="Auto-tune model parameters")
    parser.add_argument("--apply", action="store_true",
                        help="Apply suggested changes (with backup)")
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  KALISHI EDGE — Auto-Tuner")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Current parameters
    current = _read_current_params()
    print("\n  Current Parameters:")
    for p, v in sorted(current.items()):
        lo, hi = _SAFE_RANGES.get(p, (0, 1))
        print(f"    {p:<22} = {v}  (safe range: {lo}–{hi})")

    # Performance summary
    perf = compute_performance(days=7)
    print(f"\n  7-Day Stats: {perf['total_settled']} settled, "
          f"{perf['win_rate']}% WR, {perf['roi_pct']:+.1f}% ROI, "
          f"${perf['net_pnl']:+.2f} net")

    # Suggestions
    adjustments = analyze_and_suggest()
    if not adjustments:
        print("\n  No adjustments suggested — parameters look good for current performance")
    else:
        print(f"\n  Suggested Adjustments ({len(adjustments)}):")
        for i, adj in enumerate(adjustments, 1):
            print(f"\n  {i}. {adj['param']}")
            print(f"     Current:   {adj['current']}")
            print(f"     Suggested: {adj['suggested']}")
            print(f"     Reason:    {adj['reason']}")

    # Tuning suggestions from tracker
    suggestions = suggest_tuning(days=7)
    print(f"\n  General Suggestions:")
    for s in suggestions:
        print(f"    • {s}")

    # Apply if requested
    if args.apply and adjustments:
        print(f"\n  Applying {len(adjustments)} adjustment(s)…")
        apply_adjustments(adjustments)
    elif args.apply:
        print("\n  Nothing to apply.")

    print()


if __name__ == "__main__":
    main()
