"""
dashboard.py — Live performance dashboard & settlement sync
===========================================================
Run anytime:  python scripts/dashboard.py
              python scripts/dashboard.py --sync     (fetch settlements from Kalshi first)
              python scripts/dashboard.py --suggest   (show tuning suggestions)
              python scripts/dashboard.py --full      (all of the above)

Reads from:  logs/trade_history.jsonl  (picks + executions)
             Kalshi API                (live balance, settlements, positions)
             logs/performance.json     (cached analytics)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# Force UTF-8 output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from engine.tracker import (
    compute_performance, load_history, load_picks, load_executions,
    load_settlements as load_local_settlements, suggest_tuning,
    record_settlement,
)
from data.feeds.kalshi import (
    get_balance, get_orders, get_settlements as kalshi_get_settlements,
    get_portfolio,
)


_ASSET_EMOJI = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "DOGE": "Ð", "XRP": "✕", "BNB": "B"}


async def sync_settlements():
    """Pull settlement data from Kalshi and record any we don't have yet."""
    settlements = await kalshi_get_settlements()
    local = {s.get("ticker", "") for s in load_local_settlements(days=30)}
    new_count = 0
    for s in settlements:
        ticker = s.get("market_ticker", s.get("ticker", ""))
        if ticker in local:
            continue
        revenue = float(s.get("revenue", 0))
        # Settlement revenue > 0 means we won (got paid back)
        # Cost info needs order cross-reference—we estimate from yes_price
        yes_price = float(s.get("yes_price", 50)) / 100.0
        side = "yes" if revenue > 0 else "no"  # rough heuristic
        won = revenue > 0
        record_settlement(
            ticker=ticker,
            side=side,
            won=won,
            payout=revenue,
            cost=yes_price,
            fees=0.0,
        )
        new_count += 1

    if new_count:
        print(f"  Synced {new_count} new settlement(s) from Kalshi")
    else:
        print("  Settlements up to date — no new records")
    return new_count


async def show_dashboard(sync: bool = False, show_suggestions: bool = False):
    """Print the full dashboard."""
    now = datetime.now()
    print()
    print("=" * 72)
    print("    KALISHI EDGE — Performance Dashboard")
    print(f"    {now.strftime('%a %b %d %Y  %H:%M:%S')}")
    print("=" * 72)

    # Sync if requested
    if sync:
        print()
        await sync_settlements()

    # Live balance
    balance = await get_balance()
    print(f"\n  💰 Live Kalshi Balance: ${balance:.2f}")

    # Active positions
    port = await get_portfolio()
    positions = port.get("event_positions", port.get("positions", []))
    if positions:
        print(f"  📊 Active Positions: {len(positions)}")
        for p in positions[:10]:
            ticker = p.get("ticker", p.get("event_ticker", "?"))
            # Handle different position formats
            yes_count = p.get("total_traded", 0)
            resting = p.get("resting_orders_count", 0)
            print(f"     {ticker}  contracts={yes_count}  resting={resting}")
    else:
        print("  📊 Active Positions: 0")

    # Performance summary
    perf = compute_performance(days=7)
    print(f"\n  ── 7-Day Performance ──")
    print(f"  Picks generated:  {perf['total_picks']}")
    print(f"  Orders placed:    {perf['total_placed']}")
    print(f"  Rejected:         {perf['total_rejected']}")
    print(f"  Settled:          {perf['total_settled']}")
    print(f"  Wins / Losses:    {perf['wins']}W / {perf['losses']}L")
    print(f"  Win Rate:         {perf['win_rate']}%")
    print(f"  Total Wagered:    ${perf['total_wagered']:.2f}")
    print(f"  Net P&L:          ${perf['net_pnl']:+.2f}")
    print(f"  ROI:              {perf['roi_pct']:+.1f}%")
    print(f"  Avg Edge:         {perf['avg_edge']:+.1f}%")
    print(f"  Avg Confidence:   {perf['avg_confidence']:.0f}")

    # Streak
    streak = perf.get("streak", {})
    if streak.get("current_type") != "none":
        print(f"  Current Streak:   {streak['current']} {streak['current_type']}(s)")
        print(f"  Max Win Streak:   {streak['max_win']}  |  Max Loss Streak: {streak['max_loss']}")

    # Confidence buckets
    buckets = perf.get("conf_buckets", {})
    has_bucket_data = any(v.get("trades", 0) > 0 for v in buckets.values())
    if has_bucket_data:
        print(f"\n  ── Win Rate by Confidence ──")
        for label in ["0-25", "25-50", "50-75", "75-100"]:
            b = buckets.get(label, {})
            if b.get("trades", 0) > 0:
                bar = "█" * min(10, int(b["win_rate"] / 10)) + "░" * max(0, 10 - int(b["win_rate"] / 10))
                print(f"  conf {label:>5}:  {bar}  {b['win_rate']:>5.1f}%  ({b['wins']}/{b['trades']})")

    # Asset breakdown
    asset_stats = perf.get("asset_stats", {})
    if asset_stats:
        print(f"\n  ── Asset Breakdown ──")
        print(f"  {'Asset':<6}  {'Picks':>5}  {'W':>3}  {'L':>3}  {'WR%':>5}  {'Net':>8}  {'Avg Edge':>8}")
        print(f"  {'-'*48}")
        for asset in sorted(asset_stats.keys()):
            s = asset_stats[asset]
            emoji = _ASSET_EMOJI.get(asset, " ")
            print(
                f"  {emoji}{asset:<5}  {s['picks']:>5}  {s['wins']:>3}  {s['losses']:>3}  "
                f"{s['win_rate']:>4.0f}%  ${s['net_pnl']:>+7.2f}  {s['avg_edge']:>+6.1f}%"
            )

    # Edge accuracy
    ea = perf.get("edge_accuracy", {})
    if ea.get("matched_trades", 0) > 0:
        print(f"\n  ── Edge Accuracy ──")
        print(f"  Matched trades:     {ea['matched_trades']}")
        print(f"  Direction correct:  {ea['direction_correct']} ({ea['direction_pct']}%)")
        print(f"  Avg predicted edge: {ea['avg_predicted_edge']:.1f}%")

    # Recent activity (last 10 picks)
    picks = load_picks(days=1)
    if picks:
        print(f"\n  ── Recent Picks (last 24h: {len(picks)} total) ──")
        for p in picks[-10:]:
            ts = p.get("ts", "")[:19].replace("T", " ")
            emoji = _ASSET_EMOJI.get(p.get("asset", ""), " ")
            verdict = p.get("verdict", "?")
            print(
                f"  {ts}  {emoji}{p.get('asset','?'):<4}  {p.get('side','?').upper():<3}  "
                f"edge={p.get('edge_pct',0):>+5.1f}%  conf={p.get('confidence',0):>2}  "
                f"gap={p.get('gap_pct',0):>+.3f}%  {p.get('min_left',0):>4.1f}m  → {verdict}"
            )

    # Tuning suggestions
    if show_suggestions:
        suggestions = suggest_tuning(days=7)
        print(f"\n  ── Tuning Suggestions ──")
        for i, s in enumerate(suggestions, 1):
            print(f"  {i}. {s}")

    # Recent executions
    execs = load_executions(days=1)
    if execs:
        print(f"\n  ── Recent Executions (last 24h) ──")
        for e in execs[-10:]:
            ts = e.get("ts", "")[:19].replace("T", " ")
            status = e.get("status", "?")
            icon = "✓" if status == "PLACED" else "⊘" if status == "DRY_RUN" else "✗"
            print(
                f"  {ts}  {icon} {status:<10}  {e.get('ticker','?'):<35}  "
                f"{e.get('side','?').upper():<3}@{e.get('price_cents',0)}c  "
                f"${e.get('spend_usd',0):.2f}  {e.get('reason','')[:50]}"
            )

    # Bot status
    print(f"\n  ── Bot Status ──")
    import subprocess
    result = subprocess.run(
        ["powershell", "-c", "Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, @{L='CPU(s)';E={[math]::Round($_.CPU,1)}}, @{L='Mem(MB)';E={[math]::Round($_.WorkingSet64/1MB,0)}} | Format-Table -AutoSize"],
        capture_output=True, text=True, timeout=10
    )
    bot_output = result.stdout.strip()
    if bot_output and "Id" in bot_output:
        print(f"  Python process(es) running:")
        for line in bot_output.split("\n"):
            if line.strip():
                print(f"    {line.rstrip()}")
    else:
        print("  ⚠ No python process detected — run run_24_7.ps1 to start")

    # Latest log
    log_dir = _PROJECT_ROOT / "logs"
    v4_logs = sorted(log_dir.glob("intraday_V4_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if v4_logs:
        latest = v4_logs[0]
        size_kb = latest.stat().st_size / 1024
        mtime = datetime.fromtimestamp(latest.stat().st_mtime)
        age_min = (datetime.now() - mtime).total_seconds() / 60
        print(f"  Latest log: {latest.name} ({size_kb:.1f}KB, updated {age_min:.0f}m ago)")

    print()
    print("=" * 72)
    print()


def main():
    parser = argparse.ArgumentParser(description="Performance dashboard")
    parser.add_argument("--sync", action="store_true", help="Sync settlements from Kalshi")
    parser.add_argument("--suggest", action="store_true", help="Show tuning suggestions")
    parser.add_argument("--full", action="store_true", help="Full dashboard with sync + suggestions")
    args = parser.parse_args()

    do_sync = args.sync or args.full
    do_suggest = args.suggest or args.full
    asyncio.run(show_dashboard(sync=do_sync, show_suggestions=do_suggest))


if __name__ == "__main__":
    main()
