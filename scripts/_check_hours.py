"""Check market availability and trading hours."""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
from data.feeds.kalshi_intraday import get_intraday_markets

async def main():
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    print("UTC:  ", now_utc.strftime(fmt))
    print("Local:", now_local.strftime(fmt), now_local.strftime("%A"))

    markets = await get_intraday_markets()
    print(f"\nParsed 15-min markets: {len(markets)}")
    if markets:
        for m in markets:
            print(f"  {m['ticker']}  {m['asset']}  yes={m['yes_ask']:.2f}  no={m['no_ask']:.2f}  t={m['minutes_remaining']:.1f}min")
    else:
        print("No markets — could be outside trading hours or between windows")
        print("Kalshi crypto 15-min: typically 24/7 but may have gaps")

asyncio.run(main())
