"""Find liquid Kalshi markets with the updated normalizer."""
import asyncio
import httpx
import os
import sys

sys.path.insert(0, r"C:\Users\Kevan\kalishi-edge")
from dotenv import load_dotenv
load_dotenv()


async def scan(series_ticker="", pages=5):
    from data.feeds.kalshi import get_active_markets, normalize_kalshi_market
    # Add series_ticker support inline via direct HTTP
    key = os.getenv("KALSHI_API_KEY")
    headers = {"Authorization": f"Bearer {key}"}
    all_raw = []
    cursor = None
    async with httpx.AsyncClient(timeout=20) as c:
        for _ in range(pages):
            params = {"status": "open", "limit": 200}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            r = await c.get("https://api.elections.kalshi.com/trade-api/v2/markets",
                            headers=headers, params=params)
            data = r.json()
            page = data.get("markets", [])
            all_raw.extend(page)
            cursor = data.get("cursor")
            if not cursor or len(page) < 200:
                break

    liquid = []
    for m in all_raw:
        n = normalize_kalshi_market(m)
        yes_c = round(n.get("yes_prob", 0) * 100)
        oi = n.get("open_interest", 0) or 0
        vol = n.get("volume", 0) or 0
        if yes_c >= 5 and yes_c <= 95 and (oi > 0 or vol > 0):
            liquid.append({
                "ticker": n["ticker"],
                "title": (n.get("title") or "")[:70],
                "yes_c": yes_c,
                "oi": oi,
                "vol": vol,
                "liq": n.get("liquidity", 0),
            })
    return all_raw, liquid


async def main():
    print("=== All categories (20 pages) ===")
    raw, liquid = await scan(pages=20)
    print(f"Fetched {len(raw)} → {len(liquid)} liquid")
    for m in sorted(liquid, key=lambda x: x["oi"], reverse=True)[:30]:
        print(f"  {m['ticker'][:50]:<50} yes={m['yes_c']:>3}¢  oi={m['oi']:>8.0f}  vol={m['vol']:>8.0f}")

    print()
    print("=== Specific series ===")
    for s in ["KXNBA", "KXMLB", "KXGOLF", "KXBTC", "KXETH"]:
        raw2, liq2 = await scan(s, pages=2)
        if raw2:
            print(f"  {s}: {len(raw2)} raw → {len(liq2)} liquid")
            for m in liq2[:3]:
                print(f"    {m['ticker'][:50]} yes={m['yes_c']}¢ oi={m['oi']:.0f}")


asyncio.run(main())

