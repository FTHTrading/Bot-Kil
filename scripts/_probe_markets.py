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

async def main():
    async with httpx.AsyncClient(timeout=15) as c:
        path = "/trade-api/v2/markets"
        r = await c.get(BASE + "/markets", headers=mkhdrs("GET", path),
                        params={"limit": 200, "status": "open"})
        mkts = r.json().get("markets", [])
        print(f"Total open markets fetched: {len(mkts)}")

        cats = {}
        for m in mkts:
            cat = m.get("category", "unknown")
            cats[cat] = cats.get(cat, 0) + 1
        print("\n=== CATEGORIES ===")
        for k, v in sorted(cats.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}")

        crypto_keys = ["BTC", "BITCOIN", "ETH", "ETHEREUM", "CRYPTO", "SOLANA", "SOL"]
        btc = [m for m in mkts if any(x in (m.get("title","") + m.get("ticker","")).upper() for x in crypto_keys)]
        print(f"\n=== CRYPTO MARKETS ({len(btc)}) ===")
        for m in btc[:30]:
            print(f"  {m.get('ticker')} | yes_ask={m.get('yes_ask')}c | {m.get('title','')[:70]}")

        # Also look for any series with cursor
        path2 = "/trade-api/v2/series"
        r2 = await c.get(BASE + "/series", headers=mkhdrs("GET", path2), params={"limit": 50})
        series = r2.json()
        print(f"\n=== SERIES (sample) ===")
        print(json.dumps(series, indent=2)[:1000])

asyncio.run(main())
