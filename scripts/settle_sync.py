"""
settle_sync.py — Background settlement checker
================================================
Run alongside the bot:  python scripts/settle_sync.py
                        python scripts/settle_sync.py --interval 120  (check every 2 min)
                        python scripts/settle_sync.py --once           (single check)

Polls Kalshi every N seconds for settled trades, records wins/losses,
updates the performance tracker, and prints a summary.
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from data.feeds.kalshi import get_balance, get_orders, get_settlements, get_portfolio
from engine.tracker import record_settlement, load_settlements as load_local, compute_performance

_KNOWN_PATH = _PROJECT_ROOT / "logs" / "known_settlements.json"


def _load_known() -> set:
    """Load set of already-processed settlement tickers."""
    if not _KNOWN_PATH.exists():
        return set()
    try:
        data = json.loads(_KNOWN_PATH.read_text(encoding="utf-8"))
        return set(data)
    except (json.JSONDecodeError, OSError):
        return set()


def _save_known(known: set):
    _KNOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KNOWN_PATH.write_text(json.dumps(sorted(known)), encoding="utf-8")


async def check_settlements() -> list[dict]:
    """Check for new settlements and record them."""
    known = _load_known()

    # Get executed orders (our trades)
    orders = await get_orders("executed")
    order_map = {}
    for o in orders:
        t = o.get("ticker", "")
        order_map[t] = {
            "side": o.get("side", "yes"),
            "fill_count": int(o.get("fill_count_fp", 0) or 0),
            "cost": float(o.get("taker_fill_cost_dollars", 0) or 0) + float(o.get("maker_fill_cost_dollars", 0) or 0),
            "fees": float(o.get("taker_fees_dollars", 0) or 0) + float(o.get("maker_fees_dollars", 0) or 0),
        }

    # Get settlements from Kalshi
    settlements = await get_settlements()
    new_records = []

    for s in settlements:
        ticker = s.get("market_ticker", s.get("ticker", ""))
        if not ticker or ticker in known:
            continue

        revenue = float(s.get("revenue", 0))
        order_info = order_map.get(ticker, {})
        side = order_info.get("side", "unknown")
        cost = order_info.get("cost", 0)
        fees = order_info.get("fees", 0)
        won = revenue > cost  # net positive = win

        record = record_settlement(
            ticker=ticker,
            side=side,
            won=won,
            payout=revenue,
            cost=cost,
            fees=fees,
        )
        new_records.append(record)
        known.add(ticker)

        icon = "✅" if won else "❌"
        net = revenue - cost - fees
        print(f"  {icon}  {ticker}  {side.upper()}  cost=${cost:.2f}  payout=${revenue:.2f}  net=${net:+.2f}")

    _save_known(known)
    return new_records


async def run_loop(interval: int = 60):
    """Continuously check for settlements."""
    print()
    print("  SETTLEMENT SYNC — checking Kalshi every", interval, "seconds")
    print("  Press Ctrl+C to stop")
    print()

    balance = await get_balance()
    print(f"  Starting balance: ${balance:.2f}")
    print()

    while True:
        try:
            new = await check_settlements()
            if new:
                balance = await get_balance()
                print(f"  {len(new)} new settlement(s) synced — balance: ${balance:.2f}")

                # Quick performance update
                perf = compute_performance(days=1)
                if perf["total_settled"] > 0:
                    print(f"  Today: {perf['wins']}W/{perf['losses']}L  WR={perf['win_rate']}%  net=${perf['net_pnl']:+.2f}")
                print()

        except KeyboardInterrupt:
            print("\n  Stopped.")
            break
        except Exception as e:
            print(f"  [sync error] {e}")

        await asyncio.sleep(interval)


async def run_once():
    """Single settlement check."""
    balance = await get_balance()
    print(f"\n  Balance: ${balance:.2f}")
    new = await check_settlements()
    if not new:
        print("  No new settlements found")
    else:
        print(f"\n  {len(new)} settlement(s) recorded")

    perf = compute_performance(days=7)
    if perf["total_settled"] > 0:
        print(f"  7-day: {perf['wins']}W/{perf['losses']}L  WR={perf['win_rate']}%  ${perf['net_pnl']:+.2f}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Settlement sync")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between checks (default 60)")
    parser.add_argument("--once", action="store_true", help="Single check, then exit")
    args = parser.parse_args()

    if args.once:
        asyncio.run(run_once())
    else:
        asyncio.run(run_loop(args.interval))


if __name__ == "__main__":
    main()
