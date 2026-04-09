"""Quick check: what 15-min crypto markets are open on Kalshi right now?"""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.feeds.kalshi_intraday import get_intraday_markets

async def main():
    ms = await get_intraday_markets()
    print(f"\n=== {len(ms)} markets open ===")
    for m in ms:
        t = m.get("ticker", "?")
        ya = m.get("yes_ask", 0)
        na = m.get("no_ask", 0)
        mins = m.get("minutes_remaining", 0)
        oi = m.get("open_interest", 0)
        floor = m.get("floor_strike", 0)
        print(f"  {t:40s}  YES={ya:.2f}  NO={na:.2f}  {mins:.1f}min  OI={oi:.0f}  floor={floor}")
    if not ms:
        print("  (no markets returned — may be outside operating hours)")

asyncio.run(main())
