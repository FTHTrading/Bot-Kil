import asyncio, sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data.feeds.kalshi import _get_headers, KALSHI_BASE_URL
import httpx

async def main():
    async with httpx.AsyncClient(timeout=15) as c:
        # Get a settled non-MVE market raw JSON
        h = _get_headers("GET", "/markets")
        r = await c.get(f"{KALSHI_BASE_URL}/markets", headers=h, params={"status": "settled", "limit": 50})
        mkts = r.json().get("markets", [])
        for m in mkts:
            if "KXMVE" not in m.get("ticker", ""):
                print("=== SETTLED NON-MVE RAW ===")
                print(json.dumps(m, indent=2))
                break
        
        # Also check: what does a crypto 15m settled market look like?
        # Search settled by event_ticker containing "15M"
        for m in mkts:
            tk = m.get("ticker", "")
            if "15M" in tk and "KXMVE" not in tk:
                print("\n=== CRYPTO 15M SETTLED ===")
                print(json.dumps(m, indent=2))
                break
        
        # Fetch the actual crypto 15m series
        h2 = _get_headers("GET", "/events")
        r2 = await c.get(f"{KALSHI_BASE_URL}/events", headers=h2, params={"series_ticker": "KXBTC15M", "limit": 5})
        events = r2.json().get("events", [])
        print(f"\n=== KXBTC15M events: {len(events)} ===")
        for e in events[:3]:
            print(f"  {e.get('event_ticker')} - {e.get('title', '?')[:60]}")
        
        # Also check series endpoint
        h3 = _get_headers("GET", "/series/KXBTC15M")
        r3 = await c.get(f"{KALSHI_BASE_URL}/series/KXBTC15M", headers=h3)
        print(f"\n=== Series KXBTC15M: status={r3.status_code} ===")
        if r3.status_code == 200:
            print(json.dumps(r3.json(), indent=2)[:500])

asyncio.run(main())
