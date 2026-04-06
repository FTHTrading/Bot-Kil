"""
run_intraday.py — Find and execute edge bets on current 15-minute crypto markets
=================================================================================
Run at ANY time to analyse the current 15-min window for each crypto asset.
New windows open every 15 minutes on the clock (11:00, 11:15, 11:30, etc.)

Usage:
    python scripts/run_intraday.py              # dry-run, all assets
    python scripts/run_intraday.py --execute    # place real orders
    python scripts/run_intraday.py --asset BTC  # single asset

Algorithm:
    - Position signal: current price vs floor_strike (opening BRTI reference)
    - Momentum signal: 5-min & 15-min Binance candle momentum
    - Blended by time remaining (momentum-heavy early, position-heavy late)
    - Min edge: 4%  |  Kelly: 10%  |  Max $150/trade
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 output on Windows (cp1252 default can't handle ₿, ≥, → etc.)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from data.feeds.kalshi_intraday import get_intraday_markets
from data.feeds.btc_momentum import get_momentum_signals
from engine.intraday_ev import intraday_edge_picks, _MIN_EDGE


# ---------------------------------------------------------------------------
# Executor integration — reuses existing kalshi_executor infrastructure
# ---------------------------------------------------------------------------
async def _execute_intraday_pick(pick: dict, bankroll: float = None, dry_run: bool = True) -> dict:
    """Route an intraday pick through the Kalshi crypto executor."""
    from agents.kalshi_executor import _execute_crypto_pick
    if bankroll is None:
        bankroll = float(os.getenv("BANKROLL", "10000"))
    # _execute_crypto_pick reads side/asset from crypto_meta — bridge intraday pick format
    intraday_meta = pick.get("intraday_meta", {})
    if "crypto_meta" not in pick:
        pick = dict(pick)  # copy — don't mutate caller's dict
        pick["crypto_meta"] = {
            "side":  pick.get("side", "yes").upper(),
            "asset": intraday_meta.get("asset", "CRYPTO"),
        }
    return await _execute_crypto_pick(pick, bankroll, dry_run)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

_ASSET_EMOJI = {
    "BTC":  "₿",
    "ETH":  "Ξ",
    "SOL":  "◎",
    "DOGE": "Ð",
    "XRP":  "✕",
    "BNB":  "B",
}

_BARS = "█" * 10

def _edge_bar(edge: float) -> str:
    filled = max(1, min(10, int(edge / 2)))
    return "█" * filled + "░" * (10 - filled)


def _print_header():
    now = datetime.now()
    print()
    print("=" * 68)
    print("    KALISHI EDGE — 15-Min Intraday Crypto Picks")
    print(f"    {now.strftime('%a %b %d %Y  %H:%M:%S')}    Algo: Momentum + Position Blend")
    print("=" * 68)


def _print_momentum_table(momentum: dict, assets: list[str]):
    print()
    print("  LIVE MOMENTUM SIGNALS")
    print(f"  {'Asset':<6}  {'Price':>13}  {'5-min':>8}  {'15-min':>8}  {'Trend':<6}  {'Vol(5m)':>8}")
    print("  " + "-" * 58)
    for asset in assets:
        s = momentum.get(asset, {})
        if not s:
            print(f"  {asset:<6}  {'(no data)':>13}")
            continue
        emoji = _ASSET_EMOJI.get(asset, " ")
        mom5  = s.get("mom_5m", 0.0) * 100
        mom15 = s.get("mom_15m", 0.0) * 100
        vol   = s.get("realized_vol", 0.0) * 100
        trend = s.get("trend", "flat")
        print(
            f"  {emoji}{asset:<5}  {s['current']:>13,.4f}  "
            f"{mom5:>+7.3f}%  {mom15:>+7.3f}%  {trend:<6}  {vol:>6.4f}%"
        )
    print()


def _print_picks_table(picks: list[dict]):
    if not picks:
        print("  No edge found in current 15-min window.")
        print("  (min edge 4% — markets may be efficiently priced or no open window)")
        return

    print(f"  {'#':<3}  {'Asset':<5}  {'Side':<4}  "
          f"{'Mkt%':>5}  {'Mdl%':>5}  {'Edge':>6}  {'EV':>7}  "
          f"{'Stake':>6}  {'Min left':>8}  {'Signals'}")
    print("  " + "-" * 95)
    for i, p in enumerate(picks, 1):
        meta = p.get("intraday_meta", {})
        print(
            f"  {i:<3}  {meta.get('asset','?'):<5}  {p['side'].upper():<4}  "
            f"{p['implied_prob']:>4.0f}%  {p['our_prob']:>4.0f}%  "
            f"{p['edge_pct']:>+5.1f}%  {p['ev_pct']:>+6.1f}%  "
            f"${p['recommended_stake']:>5.0f}  "
            f"  {p['minutes_remaining']:>5.1f}m  "
            f"gap={meta.get('gap_pct',0):+.3f}%  5m={meta.get('mom_5m_pct',0):+.3f}%  trend={meta.get('trend','?')}"
        )
    print()
    total_stake = sum(p["recommended_stake"] for p in picks)
    print(f"  Total suggested stake: ${total_stake:.2f}  |  {len(picks)} trade(s)")
    print()


def _print_execution_results(results: list[dict]):
    print("  EXECUTION RESULTS:")
    for r in results:
        status = r.get("status", "?")
        ticker = r.get("market_ticker") or r.get("ticker", "?")
        reason = r.get("reason") or r.get("note", "")
        price  = r.get("price_cents", "?")
        qty    = r.get("contracts") or r.get("quantity", "?")
        spend  = r.get("spend_usd") or r.get("estimated_spend") or 0
        side   = r.get("side", "?").upper()
        if status == "PLACED":
            order_id = r.get("order_id", "")
            print(f"    ✓ PLACED  {ticker}  {side}@{price}c  x{qty}  ${spend:.2f}  order={order_id}")
        elif status == "DRY_RUN":
            print(f"    DRY-RUN  {ticker}  {side}@{price}c  x{qty}  ${spend:.2f}")
        elif status in ("ok",):
            print(f"    OK  {ticker}  {side}@{price}c  x{qty}  ${spend:.2f}")
        else:
            print(f"    SKIP [{status}]  {ticker}  — {reason}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace, loop_mode: bool = False, already_placed: set = None):
    if already_placed is None:
        already_placed = set()
    if not loop_mode:
        _print_header()

    assets_filter = [a.upper() for a in args.asset] if args.asset else None

    # 1. Fetch market data and momentum signals in parallel
    print("  Fetching markets + momentum signals…")
    markets_task  = get_intraday_markets()
    momentum_task = get_momentum_signals(assets_filter)
    markets, momentum = await asyncio.gather(markets_task, momentum_task)

    # Filter by asset if requested
    if assets_filter:
        markets = [m for m in markets if m["asset"] in assets_filter]

    # 2. Print momentum table
    active_assets = sorted({m["asset"] for m in markets}) or (assets_filter or list(momentum.keys()))
    _print_momentum_table(momentum, active_assets)

    # 3. Run model — use --bankroll override or env
    bankroll = args.bankroll if args.bankroll else float(os.getenv("BANKROLL", "10000"))
    min_edge = float(os.getenv("MIN_EDGE_INTRADAY", str(_MIN_EDGE)))
    picks_raw = intraday_edge_picks(markets, momentum, bankroll, min_edge)

    # ── SMART FILTERS ──────────────────────────────────────────────────────
    # 1. Remove any pick where we have NO real price data (momentum gap/5m both 0
    #    and more than 2 min remaining — too risky to bet blind)
    # 2. In --wait mode: skip picks with > max_wait minutes remaining
    market_map = {m["ticker"]: m for m in markets}

    picks = []
    waiting = []
    rejected_no_data = []
    for p in picks_raw:
        meta = p["intraday_meta"]
        has_price_data = not (meta["mom_5m_pct"] == 0.0 and meta["mom_15m_pct"] == 0.0
                              and meta["gap_pct"] == 0.0)
        t_min = p["minutes_remaining"]

        # Never bet without real price data — if gap=0, mom=0, we're betting blind.
        # The market prices reflect real-time info we don't have.
        if not has_price_data:
            rejected_no_data.append(p)
            continue

        if args.wait and t_min > args.wait_minutes:
            waiting.append(p)
            continue

        # In loop mode: skip tickers we already bet this window
        if loop_mode and p["market"] in already_placed:
            continue

        picks.append(p)

    if rejected_no_data:
        print(f"  Skipped {len(rejected_no_data)} pick(s) — no live price data (would be betting blind)")
    if waiting:
        import math
        max_rem = max(p['minutes_remaining'] for p in waiting)
        print(f"  WAIT MODE: {len(waiting)} pick(s) held — re-run in ~{math.ceil(max_rem - args.wait_minutes + 0.5):.0f} min when ≤{args.wait_minutes:.0f} min remain")
        for p in waiting:
            meta = p["intraday_meta"]
            m = market_map.get(p["market"], {})
            price_cents = round(m.get("yes_ask", 0.5) * 100) if p["side"] == "yes" else round(m.get("no_ask", 0.5) * 100)
            print(f"    WAITING: {p['market']}  {p['side'].upper()}@{price_cents}c  edge={p['edge_pct']:+.1f}%  {p['minutes_remaining']:.1f}min left  gap={meta['gap_pct']:+.3f}%")
        print()
    # end smart filters

    # 4. Print picks
    if not loop_mode or picks or waiting:
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"  [{now_str}] PICKS (min edge {min_edge*100:.0f}% | 15-min window | {len(markets)} markets scanned)")
        print()
        _print_picks_table(picks)

    if not picks:
        if not loop_mode:
            print("  Tip: Re-run at the start of the next 15-min window for fresh opportunities.")
        return

    # 5. Dry-run gate
    if not args.execute:
        print("  DRY-RUN — no orders placed. Add --execute to trade.")
        print()
        print("  Simulated orders:")
        # Build a ticker → market lookup
        market_map = {m["ticker"]: m for m in markets}
        for p in picks:
            m = market_map.get(p["market"], {})
            price_cents = round(m.get("yes_ask", 0.5) * 100) if p["side"] == "yes" else round(m.get("no_ask", 0.5) * 100)
            contracts = max(1, min(500, int(p["recommended_stake"] / max(price_cents / 100, 0.01))))
            cost = contracts * price_cents / 100
            print(f"    Would place: {p['market']}  {p['side'].upper()}@{price_cents}c  x{contracts}  ${cost:.2f}")
        return

    # 6. Execute
    print(f"  Placing {len(picks)} real orders…")
    if not args.loop and not args.yes:
        confirm = input("  Type YES to confirm real money orders: ").strip()
        if confirm.upper() != "YES":
            print("  Cancelled.")
            return

    results = []
    for pick in picks:
        result = await _execute_intraday_pick(pick, bankroll=bankroll, dry_run=False)
        results.append(result)
        if result.get("status") in ("PLACED", "ok"):
            already_placed.add(pick["market"])

    _print_execution_results(results)
    print()
    print("  Done. Orders placed — check kalshi.com for status.")
    print("  Winnings auto-settle at expiry → available in Kalshi balance immediately.")


def run():
    parser = argparse.ArgumentParser(description="15-min intraday crypto picks")
    parser.add_argument("--execute",      action="store_true", help="Place real orders")
    parser.add_argument("--asset",        nargs="*",           help="Filter assets e.g. --asset BTC ETH")
    parser.add_argument("--min-edge",     type=float, default=None, help="Override min edge %% (e.g. 3)")
    parser.add_argument("--bankroll",     type=float, default=None, help="Override bankroll (default: BANKROLL env or 10000)")
    parser.add_argument("--wait",         action="store_true", help="Hold picks until ≤N min remain (max confidence)")
    parser.add_argument("--wait-minutes", type=float, default=3.0, help="Minutes-remaining threshold for --wait (default 3)")
    parser.add_argument("--loop",         action="store_true", help="Poll every N seconds until edge found, then auto-execute")
    parser.add_argument("--loop-seconds", type=int, default=30, help="Seconds between polls in --loop mode (default 30)")
    parser.add_argument("--yes",          action="store_true", help="Skip confirmation prompt for live orders")
    args = parser.parse_args()

    if args.min_edge is not None:
        os.environ["MIN_EDGE_INTRADAY"] = str(args.min_edge / 100)

    if args.loop:
        asyncio.run(_loop(args))
    else:
        asyncio.run(main(args))


async def _loop(args: argparse.Namespace):
    """continuously poll for edge, fire when found (used with --execute for live bets)."""
    import time as _time
    interval = args.loop_seconds
    bets_placed: set[str] = set()   # tickers already bet this window

    print()
    print("  LOOP MODE — scanning every", interval, "seconds. Ctrl+C to stop.")
    print("  Will auto-execute when edge >= threshold." if args.execute else "  Dry-run -- add --execute to place live bets.")
    print()

    while True:
        try:
            await main(args, loop_mode=True, already_placed=bets_placed)
        except KeyboardInterrupt:
            print("\n  Loop stopped.")
            break
        except Exception as e:
            print(f"  [loop error] {e}")
        print(f"  Sleeping {interval}s…  (Ctrl+C to stop)")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    run()
