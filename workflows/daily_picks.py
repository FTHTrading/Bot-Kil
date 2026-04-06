"""
Daily Picks Workflow
=====================
Scheduled automation that runs every morning:
1. Pulls today's odds from all books
2. Runs all sport models
3. Outputs ranked picks with Kelly sizing
4. Scans for arbitrage opportunities
5. Writes to dashboard and logs

Run: python workflows/daily_picks.py
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

LOG_DIR = Path("./db/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

BANKROLL = float(os.getenv("BANKROLL_TOTAL", "10000"))


async def run_daily_workflow():
    """Full daily picks pipeline."""
    now = datetime.now()
    date_str = date.today().isoformat()
    
    print(f"\n{'='*60}")
    print(f"  KALISHI EDGE — Daily Picks Run")
    print(f"  {now.strftime('%A, %B %d, %Y %I:%M %p')}")
    print(f"  Bankroll: ${BANKROLL:,.2f}")
    print(f"{'='*60}\n")
    
    from agents.orchestrator import run_daily_picks
    
    # Run all agents
    print("[1/4] Running pick analysis across all sports...")
    picks_data = await run_daily_picks()
    
    # Display results
    print(f"\n[2/4] Results:")
    print(f"  Total value picks:  {picks_data['total_picks']}")
    print(f"  Arb opportunities:  {picks_data['total_arbs']}")
    print(f"  Sports covered:     {', '.join(picks_data['sports_covered']).upper()}")
    
    # Print top picks
    print(f"\n{'─'*60}")
    print("  TOP PICKS TODAY (by edge %)")
    print(f"{'─'*60}")
    
    for i, pick in enumerate(picks_data.get("top_picks", [])[:10], 1):
        print(f"\n  {i}. [{pick['sport']}] {pick['event']}")
        print(f"     Pick:   {pick['pick']} ({pick['market']})")
        print(f"     Odds:   {pick['american_odds']:+d}  (decimal: {pick['decimal_odds']:.3f})")
        print(f"     Book:   {pick['book'].upper()}")
        print(f"     Edge:   +{pick['edge_pct']:.2f}%  |  EV: +{pick['ev_pct']:.2f}%")
        print(f"     Prob:   Ours {pick['our_prob']}% vs Implied {pick['implied_prob']}%")
        print(f"     Stake:  ${pick['recommended_stake']:,.2f}  ({pick['kelly_pct']:.2f}% Kelly)")
        print(f"     Grade:  {pick['verdict']}")
    
    # Print arb opportunities
    if picks_data.get("arbitrage_opportunities"):
        print(f"\n{'─'*60}")
        print("  ARBITRAGE OPPORTUNITIES (guaranteed profit)")
        print(f"{'─'*60}")
        for arb in picks_data["arbitrage_opportunities"][:5]:
            print(f"\n  [{arb['sport']}] {arb['event']}")
            print(f"  Profit: {arb['profit_pct']:.2f}%  |  ${arb['guaranteed_profit']:.2f} guaranteed")
            print(f"  Leg A: {arb['leg_a']['side']} @ {arb['leg_a']['odds']:.3f} ({arb['leg_a']['book'].upper()})")
            print(f"  Leg B: {arb['leg_b']['side']} @ {arb['leg_b']['odds']:.3f} ({arb['leg_b']['book'].upper()})")
    
    # ── Kalshi dry-run execution ────────────────────────────────────────────────
    print(f"\n[3/5] Running Kalshi DRY-RUN (no real orders placed)...")
    top_picks = picks_data.get("top_picks", [])
    try:
        from agents.kalshi_executor import auto_execute_picks
        kalshi_result = await auto_execute_picks(
            top_picks,
            BANKROLL,
            min_edge=0.04,
            dry_run=True,          # ← safe: no real money
        )
        placed      = kalshi_result.get("placed", 0)
        skipped_cnt = kalshi_result.get("skipped_below_edge", 0)
        spend       = kalshi_result.get("total_spend_usd", 0.0)
        placed_dets = [
            r for r in kalshi_result.get("results", [])
            if r.get("status") in ("PLACED", "DRY_RUN")
        ]
        print(f"  Kalshi DRY-RUN complete:")
        print(f"    Contracts matched & would be placed: {placed}")
        print(f"    Picks skipped (no market / failed gate): {skipped_cnt}")
        print(f"    Total would-spend: ${spend:,.2f}")
        if placed_dets:
            print(f"\n  Matches:")
            for p in placed_dets[:5]:
                print(
                    f"    {p.get('market_ticker','?')}  YES@{p.get('yes_price_cents',0)}c  "
                    f"x{p.get('contracts',0)} contracts  (${p.get('spend_usd',0):.2f})"
                )
    except Exception as e:
        print(f"  [Kalshi] DRY-RUN skipped: {e}")

    # Summary
    summary = picks_data.get("summary", {})
    print(f"\n{'─'*60}".replace('─', '-'))
    print("  DAILY SUMMARY")
    print('-' * 60)
    print(f"  Strong value bets: {summary.get('value_bets', 0)}")
    print(f"  Arb profit avail:  ${summary.get('arb_profit_available', 0):.2f}")

    # Save to log
    log_path = LOG_DIR / f"picks_{date_str}.json"
    with open(log_path, "w") as f:
        json.dump(picks_data, f, indent=2)
    print(f"\n[4/5] Saved to {log_path}")

    # Also save latest.json for dashboard
    latest_path = LOG_DIR / "picks_latest.json"
    with open(latest_path, "w") as f:
        json.dump(picks_data, f, indent=2)

    print("[5/5] Done. Dashboard will reflect latest picks.\n")
    return picks_data


async def run_arb_scanner():
    """
    Continuous arbitrage scanner — runs every 5 minutes.
    For live betting arbs before they close.
    """
    from data.feeds.odds_api import scan_for_arb_opportunities
    
    print("[ArbScanner] Starting continuous arbitrage scan...")
    
    while True:
        try:
            arbs = await scan_for_arb_opportunities(BANKROLL)
            if arbs:
                print(f"\n🎯 [{datetime.now().strftime('%H:%M:%S')}] {len(arbs)} ARB OPPORTUNITIES FOUND!")
                for arb in arbs[:3]:
                    print(f"  {arb.get('sport')} | {arb.get('event')} | {arb.get('profit_margin_pct', 0):.2f}% guaranteed")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanning... no arbs found")
        except Exception as e:
            print(f"[ArbScanner] Error: {e}")
        
        await asyncio.sleep(300)  # 5 minutes


if __name__ == "__main__":
    asyncio.run(run_daily_workflow())
