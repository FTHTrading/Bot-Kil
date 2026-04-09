"""Debug: check specific event markets and see what's happening."""
import asyncio, sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data.feeds.kalshi import _get_headers, KALSHI_BASE_URL
import httpx

async def main():
    async with httpx.AsyncClient(timeout=15) as c:
        # 1. Get recent KXBTC15M events
        h = _get_headers("GET", "/events")
        r = await c.get(f"{KALSHI_BASE_URL}/events", headers=h, params={"series_ticker": "KXBTC15M", "limit": 10})
        events = r.json().get("events", [])
        print(f"KXBTC15M events: {len(events)}")
        for e in events:
            et = e.get("event_ticker", "?")
            title = e.get("title", "?")
            print(f"  {et}: {title}")
        
        # 2. Check markets for first event - try different approaches
        if events:
            et = events[0]["event_ticker"]
            
            # Approach A: /events/{et}/markets (what we've been doing)
            url_a = f"{KALSHI_BASE_URL}/events/{et}/markets"
            h_a = _get_headers("GET", f"/events/{et}/markets")
            r_a = await c.get(url_a, headers=h_a)
            print(f"\n[A] GET {url_a}")
            print(f"    Status: {r_a.status_code}")
            print(f"    Body: {r_a.text[:300]}")
            
            # Approach B: /markets?event_ticker=...
            url_b = f"{KALSHI_BASE_URL}/markets"
            h_b = _get_headers("GET", "/markets")
            r_b = await c.get(url_b, headers=h_b, params={"event_ticker": et, "limit": 10})
            print(f"\n[B] GET /markets?event_ticker={et}")
            print(f"    Status: {r_b.status_code}")
            mkts = r_b.json().get("markets", [])
            print(f"    Markets: {len(mkts)}")
            for m in mkts[:3]:
                print(f"    {m.get('ticker','?')} status={m.get('status','?')} vol={m.get('volume_fp','?')}")
        
        # 3. Also query for recently closed events (settled crypto)
        print(f"\n{'='*60}")
        for st in ["open", "closed", "settled"]:
            h2 = _get_headers("GET", "/events")
            r2 = await c.get(f"{KALSHI_BASE_URL}/events", headers=h2, params={"series_ticker": "KXBTC15M", "status": st, "limit": 3})
            evts = r2.json().get("events", [])
            print(f"\nKXBTC15M status={st}: {len(evts)} events")
            for e in evts[:2]:
                print(f"  {e.get('event_ticker','?')}")
        
        # 4. Check what the intraday fetcher endpoint sees
        print(f"\n{'='*60}")
        print("Checking what kalshi_intraday.py would see...")
        # The intraday code probably queries /markets with specific filters
        h3 = _get_headers("GET", "/markets")
        r3 = await c.get(f"{KALSHI_BASE_URL}/markets", headers=h3, params={"status": "open", "limit": 20, "series_ticker": "KXBTC15M"})
        mkts3 = r3.json().get("markets", [])
        print(f"Open KXBTC15M markets: {len(mkts3)}")
        for m in mkts3[:5]:
            print(f"  {m.get('ticker','?')} status={m.get('status','?')}")

asyncio.run(main())
