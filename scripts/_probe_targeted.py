"""Targeted Kalshi market discovery - uses search/series instead of full pagination."""
import httpx, asyncio, base64, time, json
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_pad

KEY_ID = "79cf3d4a-b413-4cdb-b309-dc7cba59b762"
BASE   = "https://api.elections.kalshi.com/trade-api/v2"

def mkhdrs(method, path):
    with open("keys/kalshi_private.pem", "rb") as f:
        priv = load_pem_private_key(f.read(), password=None)
    ts  = str(int(time.time() * 1000))
    sig = priv.sign(
        f"{ts}{method}{path}".encode(),
        asym_pad.PSS(mgf=asym_pad.MGF1(hashes.SHA256()), salt_length=asym_pad.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    return {
        "KALSHI-ACCESS-KEY":       KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }

async def search_markets(c, query, limit=20):
    path = "/trade-api/v2/markets"
    r = await c.get(BASE + "/markets", headers=mkhdrs("GET", path),
                    params={"limit": limit, "status": "open", "search": query})
    return r.json().get("markets", [])

async def get_series_markets(c, series_ticker, limit=20):
    path = f"/trade-api/v2/series/{series_ticker}/markets"
    r = await c.get(BASE + f"/series/{series_ticker}/markets",
                    headers=mkhdrs("GET", path), params={"limit": limit, "status": "open"})
    return r.json().get("markets", [])

async def main():
    async with httpx.AsyncClient(timeout=15) as c:

        # 1. Search for BTC / crypto
        print("=== BTC / CRYPTO MARKETS (search) ===")
        for q in ["bitcoin", "BTC", "ethereum", "crypto price"]:
            mkts = await search_markets(c, q, 10)
            for m in mkts:
                print(f"  [{m.get('ticker')}] yes_ask={m.get('yes_ask')}c  liq={m.get('liquidity')}  {m.get('title','')[:70]}")
            if not mkts:
                print(f"  (no results for '{q}')")

        # 2. Known Kalshi financial series
        print("\n=== FINANCIAL / INDEX SERIES ===")
        known_series = ["KXBTC", "KXETH", "KXBTCD", "KXBTCW",  # Bitcoin series
                        "KXINXD", "KXNASDAQ", "KXSPX",           # Stock indices
                        "KXCPI", "KXFED", "KXUNRATE"]            # Macro
        for s in known_series:
            mkts = await get_series_markets(c, s, 5)
            if mkts:
                print(f"  Series {s}: {len(mkts)} markets")
                for m in mkts[:3]:
                    print(f"    [{m.get('ticker')}] yes_ask={m.get('yes_ask')}c  {m.get('title','')[:65]}")

        # 3. Search for today's date-based markets
        print("\n=== TODAY / THIS WEEK MARKETS ===")
        for q in ["April 6", "Apr 6", "tonight", "today"]:
            mkts = await search_markets(c, q, 10)
            if mkts:
                print(f"  Query '{q}': {len(mkts)} results")
                for m in mkts[:5]:
                    print(f"    [{m.get('ticker')}] yes={m.get('yes_ask')}c  {m.get('title','')[:65]}")

        # 4. Sports
        print("\n=== SPORTS MARKETS ===")
        for q in ["NBA", "MLB", "NHL", "soccer"]:
            mkts = await search_markets(c, q, 5)
            if mkts:
                print(f"  {q}: {len(mkts)} results | e.g. [{mkts[0].get('ticker')}] {mkts[0].get('title','')[:55]}")

        # 5. Raw field check
        print("\n=== RAW FIELD SAMPLE (first market) ===")
        first = await search_markets(c, "will", 1)
        if first:
            print(json.dumps(first[0], indent=2)[:800])

asyncio.run(main())
