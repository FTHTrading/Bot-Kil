"""Find all intraday / short-duration BTC and crypto series on Kalshi."""
import asyncio
import sys
sys.path.insert(0, ".")
from data.feeds.kalshi_crypto import _headers, _BASE
import httpx


async def main():
    async with httpx.AsyncClient(timeout=20) as c:
        # Paginate through all series
        all_series = []
        cursor = None
        for _ in range(20):
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = await c.get(_BASE + "/series", headers=_headers("GET", "/series"), params=params)
            data = r.json()
            batch = data.get("series", [])
            all_series.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break

        print(f"Total series found: {len(all_series)}")

        # Find crypto/price related
        keywords = ["BTC", "ETH", "XBT", "COIN", "CRYPTO", "KXBTC", "KXETH"]
        crypto = [s for s in all_series if any(k in s.get("ticker", "").upper() for k in keywords)]
        print(f"\n=== CRYPTO SERIES ({len(crypto)}) ===")
        for s in sorted(crypto, key=lambda x: x.get("ticker","")):
            print(f"  {s.get('ticker','?'):<30}  {s.get('title','')[:70]}")

        # Look for anything with 15, min, hour in title (intraday)
        intraday = [s for s in all_series if any(k in s.get("title","").lower() for k in ["15 min", "15min", "30 min", "1 hour", "hourly", "intraday", "hour", "minute"])]
        print(f"\n=== INTRADAY / SHORT-DURATION SERIES ({len(intraday)}) ===")
        for s in sorted(intraday, key=lambda x: x.get("ticker","")):
            print(f"  {s.get('ticker','?'):<30}  {s.get('title','')[:70]}")

asyncio.run(main())
