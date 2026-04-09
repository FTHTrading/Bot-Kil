"""
paper_settle.py — Resolve paper trades against actual Kalshi settlements
========================================================================
Reads logs/paper_trades.jsonl, checks each unsettled entry against the
Kalshi settlement API, and writes results + running stats.

Usage:
    python scripts/paper_settle.py          # check once
    python scripts/paper_settle.py --loop   # poll every 2 minutes
"""
from __future__ import annotations

import asyncio
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from data.feeds import kalshi

_PAPER_LOG = _PROJECT_ROOT / "logs" / "paper_trades.jsonl"
_PAPER_RESULTS = _PROJECT_ROOT / "logs" / "paper_results.json"


def _load_paper_trades() -> list[dict]:
    """Load all paper trade entries."""
    if not _PAPER_LOG.exists():
        return []
    entries = []
    for line in _PAPER_LOG.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _save_paper_trades(entries: list[dict]):
    """Rewrite the entire paper trades file with updated results."""
    _PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_PAPER_LOG, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


async def settle_paper_trades():
    """Check Kalshi settlements and resolve paper trades."""
    entries = _load_paper_trades()
    if not entries:
        print("No paper trades to settle.")
        return

    unsettled = [e for e in entries if e.get("result") is None]
    if not unsettled:
        print("All paper trades already settled.")
        _print_stats(entries)
        return

    print(f"Checking {len(unsettled)} unsettled paper trade(s)...")

    # Fetch settlements from Kalshi
    settlements = await kalshi.get_settlements()
    settled_map = {}
    for s in settlements:
        ticker = s.get("ticker", "")
        settled_map[ticker] = s.get("market_result", "unknown")

    resolved = 0
    for e in entries:
        if e.get("result") is not None:
            continue
        ticker = e.get("ticker", "")
        if ticker in settled_map:
            market_result = settled_map[ticker]
            side = e.get("side", "").lower()
            cost = e.get("cost_usd", 0)
            contracts = e.get("contracts", 0)

            if market_result == side:
                # WIN: payout = $1 per contract, minus cost
                payout = contracts * 1.0
                fee = round(contracts * 0.02, 2)  # ~2% fee estimate
                profit = round(payout - cost - fee, 2)
                e["result"] = "WIN"
                e["payout"] = payout
                e["profit"] = profit
            elif market_result in ("yes", "no"):
                # LOSS: cost is gone
                e["result"] = "LOSS"
                e["payout"] = 0
                e["profit"] = round(-cost, 2)
            else:
                continue  # not yet settled

            e["settled_time"] = datetime.now(timezone.utc).isoformat()
            e["market_result"] = market_result
            resolved += 1

    if resolved > 0:
        _save_paper_trades(entries)
        print(f"Resolved {resolved} paper trade(s).")
    else:
        print("No new settlements found yet.")

    _print_stats(entries)


def _print_stats(entries: list[dict]):
    """Print running paper trade performance."""
    settled = [e for e in entries if e.get("result") is not None]
    unsettled = [e for e in entries if e.get("result") is None]

    if not settled:
        print(f"\nNo settled paper trades yet. {len(unsettled)} pending.")
        return

    wins = [e for e in settled if e["result"] == "WIN"]
    losses = [e for e in settled if e["result"] == "LOSS"]
    total_cost = sum(e.get("cost_usd", 0) for e in settled)
    total_profit = sum(e.get("profit", 0) for e in settled)
    win_rate = len(wins) / len(settled) * 100 if settled else 0

    print(f"\n{'='*60}")
    print(f"  PAPER TRADE SCORECARD")
    print(f"{'='*60}")
    print(f"  Settled:  {len(settled)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Pending:  {len(unsettled)}")
    print(f"  Win Rate: {win_rate:.1f}%")
    print(f"  Capital:  ${total_cost:.2f}")
    print(f"  Net P&L:  ${total_profit:+.2f}")
    if total_cost > 0:
        roi = total_profit / total_cost * 100
        print(f"  ROI:      {roi:+.1f}%")

    # Breakdown by asset
    asset_stats = {}
    for e in settled:
        asset = e.get("asset", "?")
        if asset not in asset_stats:
            asset_stats[asset] = {"wins": 0, "losses": 0, "profit": 0}
        if e["result"] == "WIN":
            asset_stats[asset]["wins"] += 1
        else:
            asset_stats[asset]["losses"] += 1
        asset_stats[asset]["profit"] += e.get("profit", 0)

    if asset_stats:
        print(f"\n  By Asset:")
        for asset in sorted(asset_stats):
            s = asset_stats[asset]
            total = s["wins"] + s["losses"]
            wr = s["wins"] / total * 100 if total else 0
            print(f"    {asset:6s}  {s['wins']}W/{s['losses']}L  ({wr:.0f}%)  P&L=${s['profit']:+.2f}")

    # Edge accuracy: compare predicted edge to actual outcome
    edge_hits = [(e["edge_pct"], e["result"] == "WIN") for e in settled if "edge_pct" in e]
    if edge_hits:
        avg_edge_wins = sum(e for e, w in edge_hits if w) / max(1, sum(1 for _, w in edge_hits if w))
        avg_edge_losses = sum(e for e, w in edge_hits if not w) / max(1, sum(1 for _, w in edge_hits if not w))
        print(f"\n  Edge Accuracy:")
        print(f"    Avg edge on WINS:   {avg_edge_wins:+.1f}%")
        print(f"    Avg edge on LOSSES: {avg_edge_losses:+.1f}%")

    # Verdict: is the model ready for live trading?
    print(f"\n  {'='*58}")
    if len(settled) < 20:
        print(f"  VERDICT: Need {20 - len(settled)} more settled trades for valid assessment")
    elif win_rate >= 55 and total_profit > 0:
        print(f"  VERDICT: MODEL VALIDATED — ready for live trading ✓")
    elif win_rate >= 45:
        print(f"  VERDICT: MARGINAL — needs tuning before live trading")
    else:
        print(f"  VERDICT: MODEL FAILING — do NOT go live ({win_rate:.0f}% win rate)")
    print(f"  {'='*58}")


async def _loop():
    import time
    print("Paper trade settlement monitor — checking every 2 minutes")
    while True:
        try:
            await settle_paper_trades()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  [error] {e}")
        print(f"\n  Next check in 120s... (Ctrl+C to stop)\n")
        await asyncio.sleep(120)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        asyncio.run(_loop())
    else:
        asyncio.run(settle_paper_trades())
