"""Check EVENTS (not individual markets) for high volume across Kalshi."""
import asyncio, sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data.feeds.kalshi import _get_headers, KALSHI_BASE_URL
import httpx

def _f(m, key, default=0.0):
    v = m.get(key)
    if v is None: return default
    try: return float(v)
    except: return default

async def main():
    async with httpx.AsyncClient(timeout=15) as c:
        # 1. Check events with volume
        all_events = []
        cursor = ""
        for _ in range(10):
            h = _get_headers("GET", "/events")
            params = {"status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = await c.get(f"{KALSHI_BASE_URL}/events", headers=h, params=params)
            data = r.json()
            batch = data.get("events", [])
            all_events.extend(batch)
            cursor = data.get("cursor", "")
            if not cursor or not batch:
                break

        print(f"Total open events: {len(all_events)}")
        
        # Sort by volume (check what fields events have)
        if all_events:
            print(f"\nEvent fields: {list(all_events[0].keys())}")
            print(f"\nSample event:\n{json.dumps(all_events[0], indent=2)[:1500]}")
        
        # Group events by category
        by_cat = {}
        for e in all_events:
            cat = e.get("category", "unknown")
            if cat not in by_cat:
                by_cat[cat] = []
            by_cat[cat].append(e)
        
        print(f"\n{'='*80}")
        print("EVENT CATEGORIES")
        for cat in sorted(by_cat, key=lambda c: len(by_cat[c]), reverse=True):
            evts = by_cat[cat]
            print(f"  {cat:25s}: {len(evts)} events")
            for e in evts[:3]:
                tk = e.get("event_ticker", "?")
                title = (e.get("title") or "?")[:50]
                vol = _f(e, "volume") or _f(e, "volume_fp") or _f(e, "total_volume")
                print(f"    {tk:40s} vol={vol:>12,.0f}  {title}")

        # 2. Now search for specific high-value market types
        print(f"\n{'='*80}")
        print("SEARCHING FOR HIGH-VALUE MARKETS BY TICKER PREFIX")
        
        prefixes = [
            "KXBTC", "KXETH", "KXSOL",  # crypto
            "KXNCAAMB",  # NCAA March Madness (user showed this)
            "INX", "CPI", "GDP", "ECON",  # economics
            "PRES", "TRUMP", "DEM", "REP",  # politics
            "KXNBA", "KXNHL",  # major sports
        ]
        
        for prefix in prefixes:
            h = _get_headers("GET", "/markets")
            params = {"status": "open", "limit": 10, "ticker": prefix}
            r = await c.get(f"{KALSHI_BASE_URL}/markets", headers=h, params=params)
            data = r.json()
            mkts = data.get("markets", [])
            if mkts:
                print(f"\n  {prefix}: {len(mkts)} markets found")
                for m in mkts[:3]:
                    tk = m.get("ticker","")[:50]
                    vol = _f(m, "volume_fp")
                    liq = _f(m, "liquidity_dollars")
                    ya = _f(m, "yes_ask_dollars")
                    print(f"    {tk:50s} vol=${vol:>10,.0f} liq=${liq:>8,.0f} ya=${ya:.2f}")
            else:
                # Try event search
                h2 = _get_headers("GET", "/events")
                r2 = await c.get(f"{KALSHI_BASE_URL}/events", headers=h2, params={"status": "open", "limit": 5, "series_ticker": prefix})
                evts = r2.json().get("events", [])
                if evts:
                    print(f"\n  {prefix}: {len(evts)} events (via series)")
                    for e in evts[:2]:
                        print(f"    {e.get('event_ticker','?'):40s} {(e.get('title') or '?')[:50]}")
                else:
                    print(f"\n  {prefix}: nothing found")

asyncio.run(main())
