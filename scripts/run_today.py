"""
run_today.py — CLI pick runner + optional Kalshi execution
==========================================================
Usage:
    python scripts/run_today.py              # show picks, dry-run Kalshi
    python scripts/run_today.py --execute    # LIVE execution (real money)
    python scripts/run_today.py --top 5      # show top N picks only
    python scripts/run_today.py --min-edge 0.05  # stricter edge filter
    python scripts/run_today.py --crypto-only    # only crypto/finance picks
    python scripts/run_today.py --sports-only    # only sports picks
"""
from __future__ import annotations
import asyncio
import argparse
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import os
BANKROLL = float(os.getenv("BANKROLL_TOTAL", "10000"))


def _american(dec: float) -> str:
    """Format decimal odds as American string."""
    if dec >= 2.0:
        return f"+{int((dec - 1) * 100)}"
    return f"{int(-100 / (dec - 1))}"


def _bar(pct: float, width: int = 20) -> str:
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


async def main(args: argparse.Namespace) -> None:
    from agents.orchestrator import run_daily_picks
    from agents.kalshi_executor import auto_execute_picks

    dry_run = not args.execute
    mode_label = "DRY-RUN (no orders placed)" if dry_run else "⚠  LIVE EXECUTION — REAL MONEY"

    print()
    print("=" * 56)
    print("         KALISHI EDGE -- Daily Pick Runner")
    print(f"  {datetime.now().strftime('%a %b %d %Y  %H:%M')}       Bankroll: ${BANKROLL:>9,.2f}")
    print(f"  Mode: {mode_label}")
    print("=" * 56)
    print()

    # ── Generate picks ─────────────────────────────────────────────────────────
    print("Fetching odds and generating picks…")
    try:
        data = await run_daily_picks()
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    picks  = data.get("top_picks", [])
    arbs   = data.get("arbitrage_opportunities", [])
    crypto = data.get("crypto_picks", [])

    # Apply mode filters
    if args.crypto_only:
        picks = [p for p in picks if p["sport"] == "CRYPTO"]
    elif args.sports_only:
        picks = [p for p in picks if p["sport"] != "CRYPTO"]

    # Filter by edge
    picks = [p for p in picks if p["edge_pct"] >= args.min_edge * 100]
    picks = picks[: args.top]

    sports_picks  = [p for p in picks if p["sport"] != "CRYPTO"]
    crypto_picks  = [p for p in picks if p["sport"] == "CRYPTO"]

    print(
        f"Found {len(sports_picks)} sports + {len(crypto_picks)} crypto/finance picks  "
        f"|  {len(arbs)} arb(s)  |  "
        f"Sports: {', '.join(data.get('sports_covered', []) or []).upper() or 'MOCK'}"
    )
    print()

    if not picks:
        print("No picks meet the current edge threshold "
              f"(>={args.min_edge*100:.1f}%).  Lower --min-edge to see more.")
        return

    # ── Print sports picks table ─────────────────────────────────────────────
    sports_only = [p for p in picks if p["sport"] != "CRYPTO"]
    if sports_only:
        print(f"{'#':<3} {'Sport':<5} {'Pick':<28} {'ML':>6} {'Edge':>7} {'EV':>6} {'Stake':>8}  Confidence")
        print("-" * 82)
        for i, p in enumerate(sports_only, 1):
            pick_label = f"{p['pick'][:26]}"
            ml = _american(p["decimal_odds"])
            print(
                f"{i:<3} {p['sport']:<5} {pick_label:<28} {ml:>6}  "
                f"+{p['edge_pct']:.1f}%  +{p['ev_pct']:.1f}%  "
                f"${p['recommended_stake']:>7,.0f}  {p['verdict']}"
            )
        print()

    # ── Print crypto/finance picks table ─────────────────────────────────────
    crypto_only = [p for p in picks if p["sport"] == "CRYPTO"]
    if crypto_only:
        print("CRYPTO / FINANCE MARKETS:")
        print(f"{'#':<3} {'Asset':<5} {'Side':<4} {'Threshold':>13} {'Mkt%':>5} {'Mdl%':>5} {'Edge':>6} {'EV':>6} {'Stake':>8}  Closes")
        print("-" * 84)
        for i, p in enumerate(crypto_only, 1):
            meta      = p.get("crypto_meta", {})
            asset     = meta.get("asset", p["sport"])
            side      = meta.get("side", "YES")
            threshold = meta.get("threshold", 0)
            mkt_p     = meta.get("market_prob", 0)
            mdl_p     = meta.get("model_prob", 0)
            hrs       = meta.get("hours_to_close", 0)
            stake     = p["recommended_stake"]
            ev        = p["ev_pct"]
            edge      = p["edge_pct"]
            thr_str   = f"${threshold:>11,.2f}" if threshold >= 100 else f"{threshold:>11.2f}%"
            print(
                f"{i:<3} {asset:<5} {side:<4} {thr_str}  {mkt_p:>4.0f}%  {mdl_p:>4.0f}%  "
                f"+{edge:.1f}%  +{ev:.1f}%  ${stake:>7,.0f}  {hrs:.1f}h"
            )
        print()


    if arbs:
        print("ARBITRAGE (guaranteed profit):")
        for a in arbs[:3]:
            print(
                f"  {a['sport']:<5} {a['event']:<40} "
                f"+{a['profit_pct']:.2f}%  ${a['guaranteed_profit']:.2f}"
            )
        print()

    # ── Kalshi execution ──────────────────────────────────────────────────────
    if not dry_run:
        confirm = input("⚠  You are about to place REAL orders on Kalshi. "
                        "Type YES to confirm: ").strip()
        if confirm != "YES":
            print("Aborted.")
            return

    print(f"Running Kalshi {'dry-run' if dry_run else 'LIVE execution'}…")
    try:
        result = await auto_execute_picks(
            picks,
            BANKROLL,
            min_edge=args.min_edge,
            dry_run=dry_run,
        )
    except Exception as exc:
        print(f"Kalshi error: {exc}")
        return

    placed_count  = result.get("placed", 0)
    skipped_count = result.get("skipped_below_edge", 0)
    spend         = result.get("total_spend_usd", 0.0)
    placed_details = [
        r for r in result.get("results", [])
        if r.get("status") in ("PLACED", "DRY_RUN")
    ]
    failed_details = [
        r for r in result.get("results", [])
        if r.get("status") not in ("PLACED", "DRY_RUN")
    ]

    verb = "Would place" if dry_run else "Placed"
    print(f"  {verb}: {placed_count} contract(s)  |  Skipped below edge: {skipped_count}  |  "
          f"{'Simulated' if dry_run else 'Actual'} spend: ${spend:,.2f}")

    if placed_details:
        print()
        for p in placed_details:
            side_disp  = p.get('side', 'yes').upper()
            price_disp = p.get('price_cents', p.get('yes_price_cents', 0))
            print(
                f"    OK {p.get('market_ticker','?')}  {side_disp}@{price_disp}c  "
                f"x{p.get('contracts',0)}  ${p.get('spend_usd',0):.2f}"
            )

    if failed_details:
        print()
        print(f"  Gates failed ({len(failed_details)}):")
        for f in failed_details[:5]:
            print(f"    -- {f.get('team','?')} - {f.get('reason','?')}")

    print()
    if dry_run:
        print("To place real orders, re-run with --execute (requires KALSHI_API_KEY in .env)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalishi Edge — daily pick runner")
    parser.add_argument("--execute",      action="store_true",
                        help="Place LIVE orders on Kalshi (real money)")
    parser.add_argument("--top",          type=int, default=15,
                        help="Max picks to show/execute (default: 15)")
    parser.add_argument("--min-edge",     type=float, default=0.03,
                        help="Minimum edge fraction (default: 0.03 = 3%%)")
    parser.add_argument("--crypto-only",  action="store_true",
                        help="Show only crypto/finance markets (BTC, ETH, FED, etc.)")
    parser.add_argument("--sports-only",  action="store_true",
                        help="Show only sports markets (NBA, MLB, NFL, etc.)")
    asyncio.run(main(parser.parse_args()))
