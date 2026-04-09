"""
settle_paper.py — Check settlement results for paper trades.

Reads logs/paper_trades_multi.jsonl, queries Kalshi API for market status,
and updates result field to "win", "loss", or "push".

Usage:
    python scripts/settle_paper.py           # update all pending, print summary
    python scripts/settle_paper.py --watch   # loop every 60s
"""
from __future__ import annotations
import asyncio
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
_ROOT       = Path(__file__).resolve().parent.parent
_PAPER_LOG  = _ROOT / "logs" / "paper_trades_multi.jsonl"
_EXEC_LOG   = _ROOT / "logs" / "exec_trades.jsonl"

import sys
sys.path.insert(0, str(_ROOT))

from data.feeds.kalshi import get_market


# ── Settle one file ───────────────────────────────────────────────────────────

async def settle_file(path: Path) -> dict:
    """
    Check each unsettled paper/exec trade in path.
    Returns summary dict {checked, updated, wins, losses, pending}.
    """
    if not path.exists():
        return {"checked": 0, "updated": 0, "wins": 0, "losses": 0, "pending": 0}

    lines = path.read_text(encoding="utf-8").splitlines()
    trades = []
    for ln in lines:
        ln = ln.strip()
        if ln:
            try:
                trades.append(json.loads(ln))
            except json.JSONDecodeError:
                pass

    stats = {"checked": 0, "updated": 0, "wins": 0, "losses": 0, "pending": 0}
    updated_trades = []

    for trade in trades:
        if trade.get("result") not in (None, ""):
            # Already settled
            r = trade["result"]
            if r == "win":
                stats["wins"] += 1
            elif r == "loss":
                stats["losses"] += 1
            updated_trades.append(trade)
            stats["checked"] += 1
            continue

        # Check if the market window has plausibly expired
        # We attempt to fetch from API regardless — if still open, API will say so.
        ticker = trade.get("ticker", "")
        side   = trade.get("side", "yes").lower()

        stats["checked"] += 1

        mkt = await get_market(ticker)
        if not mkt:
            stats["pending"] += 1
            updated_trades.append(trade)
            continue

        status = mkt.get("status", "")
        result = mkt.get("result", "")   # "yes" or "no" when settled

        if status in ("closed", "finalized") and result in ("yes", "no"):
            won = (side == result)            # we backed the side that won
            trade["result"] = "win" if won else "loss"
            trade["settled_at"] = datetime.now(timezone.utc).isoformat()
            trade["market_result"] = result
            stats["updated"] += 1
            if won:
                stats["wins"] += 1
            else:
                stats["losses"] += 1
        else:
            # Market still open or result not yet available
            stats["pending"] += 1

        updated_trades.append(trade)

    # Write back updated file
    if stats["updated"] > 0:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for t in updated_trades:
                f.write(json.dumps(t) + "\n")
        tmp.replace(path)

    return stats


# ── Print summary ─────────────────────────────────────────────────────────────

def _print_summary(path: Path, stats: dict):
    label = path.name
    total = stats["wins"] + stats["losses"]
    winpct = (stats["wins"] / total * 100) if total > 0 else 0.0
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  Checked:  {stats['checked']}  |  Updated this run: {stats['updated']}")
    print(f"  WINS:  {stats['wins']}  LOSSES: {stats['losses']}  PENDING: {stats['pending']}")
    if total > 0:
        print(f"  Win rate: {winpct:.1f}%  ({stats['wins']}/{total} settled)")

    if not path.exists():
        return
    trades = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # ── Neural vs Math breakdown ──────────────────────────────────────────────
    settled = [t for t in trades if t.get("result") in ("win", "loss")]
    if settled:
        neural = [t for t in settled if "neural" in (t.get("prob_source") or "")]
        math_f = [t for t in settled if t not in neural]
        def _wr(lst):
            wins = sum(1 for t in lst if t.get("result") == "win")
            return f"{wins}/{len(lst)} = {wins/len(lst)*100:.0f}%" if lst else "n/a"
        print(f"\n  Model breakdown (settled):")
        print(f"    neural: {_wr(neural)}")
        print(f"    math:   {_wr(math_f)}")

    # ── Last 10 settled ───────────────────────────────────────────────────────
    last10 = [t for t in trades if t.get("result") in ("win", "loss")][-10:]
    if last10:
        print(f"\n  Last {len(last10)} settled picks:")
        for t in last10:
            ts    = t.get("ts", "")[:16].replace("T", " ")
            tkr   = t.get("ticker", "")
            side  = t.get("side", "").upper()
            price = t.get("price", 0)
            edge  = t.get("edge", 0)
            res   = t.get("result") or "pending"
            src   = (t.get("prob_source") or "?")[:12]
            icon  = "WIN " if res == "win" else "LOSS"
            print(f"    {icon}  {ts}  {tkr:<38}  {side:<4}  @{price:.2f}  "
                  f"edge={edge*100:.1f}%  [{src}]")

    # ── All pending ───────────────────────────────────────────────────────────
    pending = [t for t in trades if not t.get("result")]
    if pending:
        print(f"\n  Pending ({len(pending)}):")
        for t in pending[-5:]:
            ts  = t.get("ts", "")[:16].replace("T", " ")
            tkr = t.get("ticker", "")
            print(f"    ...  {ts}  {tkr}")

    # ── Go / No-Go gate ───────────────────────────────────────────────────────
    GATE_PICKS  = 10
    GATE_WINPCT = 60.0
    print(f"\n  {'='*58}")
    print(f"  GO / NO-GO GATE  (need {GATE_PICKS}+ settled, ≥{GATE_WINPCT:.0f}% win rate)")
    if total < GATE_PICKS:
        print(f"  STATUS: NO-GO  — only {total}/{GATE_PICKS} settled picks so far")
    elif winpct < GATE_WINPCT:
        print(f"  STATUS: NO-GO  — win rate {winpct:.1f}% < {GATE_WINPCT:.0f}% threshold")
    else:
        print(f"  STATUS: GATE MET  — {total} picks, {winpct:.1f}% win rate")
        print(f"  ACTION: Review results, then run execute bot if confident:")
        print(f"    python scripts\\run_multi.py --execute --loop --loop-seconds 30 \\")
        print(f"      --wait-minutes 3.0 --min-edge 12 --max-contracts 2 --balance-floor 5.50 --crypto-only")
    print(f"  {'='*58}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    ap = argparse.ArgumentParser(description="Settle paper/exec trades from Kalshi API")
    ap.add_argument("--watch", action="store_true", help="Loop every 60s")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()

    while True:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{now}] Checking settlements...")

        paper_stats = await settle_file(_PAPER_LOG)
        _print_summary(_PAPER_LOG, paper_stats)

        if _EXEC_LOG.exists():
            exec_stats = await settle_file(_EXEC_LOG)
            _print_summary(_EXEC_LOG, exec_stats)

        if not args.watch:
            break
        print(f"\nSleeping {args.interval}s...")
        time.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
