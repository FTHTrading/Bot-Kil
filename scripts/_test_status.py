import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data.feeds.kalshi import _get_headers, KALSHI_BASE_URL
import httpx

async def test():
    async with httpx.AsyncClient(timeout=15) as c:
        for status in ["active", "open", "closed", "settled", ""]:
            h = _get_headers("GET", "/markets")
            params = {"limit": 5}
            if status:
                params["status"] = status
            r = await c.get(f"{KALSHI_BASE_URL}/markets", headers=h, params=params)
            d = r.json()
            mkts = d.get("markets", [])
            n = len(mkts)
            print(f"status={status!r:10s} => {n} markets")
            if mkts:
                m = mkts[0]
                print(f"  sample: ticker={m.get('ticker')}, status={m.get('status')}, result={m.get('result')}")

asyncio.run(test())
