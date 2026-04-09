"""Probe ALL Kalshi markets by paginating and checking series for crypto/finance."""
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

async def fetch_all_markets():
    all_markets = []
    cursor = None
    async with httpx.AsyncClient(timeout=15) as c:
        while True:
            path = "/trade-api/v2/markets"
            params = {"limit": 200, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            r = await c.get(BASE + "/markets", headers=mkhdrs("GET", path), params=params)
            data = r.json()
            batch = data.get("markets", [])
            all_markets.extend(batch)
            cursor = data.get("cursor")
            print(f"  Fetched {len(all_markets)} markets so far (cursor={'...' if cursor else 'END'})")
            if not cursor or len(batch) == 0:
                break
    return all_markets

async def main():
    print("Paginating all open Kalshi markets...")
    mkts = await fetch_all_markets()
    print(f"\nTotal: {len(mkts)}\n")

    # Categories
    cats = {}
    for m in mkts:
        cat = m.get("category", "unknown")
        cats[cat] = cats.get(cat, 0) + 1
    print("=== CATEGORIES ===")
    for k, v in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")

    # Print ALL unique categories + sample ticker per category
    print("\n=== SAMPLE RAW MARKET FIELDS ===")
    print(json.dumps(mkts[0], indent=2)[:600] if mkts else "none")

    # Keyword scan
    keywords = ["BTC", "BITCOIN", "ETH", "CRYPTO", "SOLANA", "NASDAQ", "S&P", "SPX",
                "TRUMP", "FED", "RATE", "OIL", "GOLD", "INFL", "CPI", "NFP", "JOBS",
                "NBA", "NFL", "MLB", "NHL", "SOCCER"]
    print("\n=== KEYWORD SCAN (all markets) ===")
    for kw in keywords:
        hits = [m for m in mkts if kw in (m.get("title","") + m.get("ticker","") + m.get("event_ticker","")).upper()]
        if hits:
            sample = hits[0]
            print(f"  {kw}: {len(hits)} markets | e.g. [{sample.get('ticker')}] {sample.get('title','')[:60]}")

asyncio.run(main())
