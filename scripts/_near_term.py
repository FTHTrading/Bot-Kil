"""Find ALL Kalshi markets closing in the next 72h with real pricing."""
import asyncio, sys, os, json
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data.feeds.kalshi import _get_headers, KALSHI_BASE_URL
import httpx

def _f(m, key, default=0.0):
    v = m.get(key)
    if v is None: return default
    try: return float(v)
    except: return default

async def main():
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=72)
    
    async with httpx.AsyncClient(timeout=15) as c:
        # Fetch all open markets, filter to ones closing within 72h
        all_markets = []
        cursor = ""
        for _ in range(30):
            h = _get_headers("GET", "/markets")
            params = {"status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = await c.get(f"{KALSHI_BASE_URL}/markets", headers=h, params=params)
            data = r.json()
            batch = data.get("markets", [])
            all_markets.extend(batch)
            cursor = data.get("cursor", "")
            if not cursor or not batch:
                break
        
        print(f"Total open markets: {len(all_markets)}")
        
        # Filter: closes within 72h, not MVE
        near_term = []
        for m in all_markets:
            if "KXMVE" in m.get("ticker", ""):
                continue
            close_str = m.get("close_time", "")
            if not close_str:
                continue
            try:
                close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                if close_dt <= cutoff:
                    m["_close_dt"] = close_dt
                    near_term.append(m)
            except:
                continue
        
        print(f"Non-MVE markets closing within 72h: {len(near_term)}")
        near_term.sort(key=lambda m: m["_close_dt"])
        
        # Show by category/time
        print(f"\n{'='*100}")
        for m in near_term[:80]:
            tk = m.get("ticker","")[:55]
            title = (m.get("title") or m.get("yes_sub_title") or "")[:45]
            ya = _f(m, "yes_ask_dollars")
            na = _f(m, "no_ask_dollars")
            vol = _f(m, "volume_fp")
            liq = _f(m, "liquidity_dollars")
            ct = m["_close_dt"].strftime("%m/%d %H:%M")
            hrs = (m["_close_dt"] - now).total_seconds() / 3600
            series = m.get("event_ticker", "")[:30]
            print(f"{ct} ({hrs:5.1f}h) {tk:55s} ya=${ya:.2f} vol=${vol:>8,.0f} liq=${liq:>6,.0f}  {title}")
        
        # Also check: recently settled markets with highest volume
        print(f"\n{'='*100}")
        print("RECENTLY SETTLED (to see where volume WAS)")
        settled = []
        cursor = ""
        for _ in range(3):
            h = _get_headers("GET", "/markets")
            params = {"status": "settled", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = await c.get(f"{KALSHI_BASE_URL}/markets", headers=h, params=params)
            data = r.json()
            batch = data.get("markets", [])
            settled.extend(batch)
            cursor = data.get("cursor", "")
            if not cursor or not batch:
                break
        
        # Sort by volume
        settled.sort(key=lambda m: _f(m, "volume_fp"), reverse=True)
        print(f"Recently settled: {len(settled)} markets")
        for m in settled[:20]:
            tk = m.get("ticker","")[:55]
            vol = _f(m, "volume_fp")
            liq = _f(m, "liquidity_dollars")
            result = m.get("result", "?")
            title = (m.get("title") or "")[:45]
            print(f"  {tk:55s} vol=${vol:>12,.0f} result={result:3s}  {title}")

asyncio.run(main())
