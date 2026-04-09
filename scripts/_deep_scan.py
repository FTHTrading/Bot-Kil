"""Deep scan: find ALL Kalshi markets with real money, any category."""
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
        # Fetch ALL open markets
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
        
        # Count types
        mve = sum(1 for m in all_markets if "KXMVE" in m.get("ticker",""))
        print(f"MVE (multivariate combos): {mve}")
        print(f"Non-MVE: {len(all_markets) - mve}")
        
        # ALL non-MVE markets, dump key fields
        non_mve = [m for m in all_markets if "KXMVE" not in m.get("ticker","")]
        
        print(f"\n{'='*100}")
        print(f"ALL {len(non_mve)} NON-MVE MARKETS (sorted by volume)")
        print(f"{'='*100}")
        
        # Sort by volume descending
        non_mve.sort(key=lambda m: _f(m, "volume_fp"), reverse=True)
        
        for m in non_mve:
            tk = m.get("ticker","")[:55]
            title = (m.get("title") or m.get("yes_sub_title") or "")[:40]
            ya = _f(m, "yes_ask_dollars")
            na = _f(m, "no_ask_dollars")
            yb = _f(m, "yes_bid_dollars")
            nb = _f(m, "no_bid_dollars")
            vol = _f(m, "volume_fp")
            v24 = _f(m, "volume_24h_fp")
            liq = _f(m, "liquidity_dollars")
            oi = _f(m, "open_interest") or m.get("open_interest", "")
            close = (m.get("close_time") or "?")[:16]
            cat = m.get("category", "?")
            
            print(f"{tk:55s} ya=${ya:.2f} na=${na:.2f} vol=${vol:>10,.0f} v24=${v24:>8,.0f} liq=${liq:>8,.0f} cat={cat:12s} {title}")
        
        # Also show raw JSON of a high-volume non-MVE market
        if non_mve:
            print(f"\n{'='*60}")
            print("RAW JSON — highest volume non-MVE market:")
            print(json.dumps(non_mve[0], indent=2))

asyncio.run(main())
