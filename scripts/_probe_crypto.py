"""Deep-inspect BTC/ETH/crypto + financial markets for today."""
import httpx, base64, time, json
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

def get(client, path, params=None):
    fullpath = "/trade-api/v2" + path
    r = client.get(BASE + path, headers=mkhdrs("GET", fullpath), params=params or {})
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:200]}")
        return None
    return r.json()

with httpx.Client(timeout=15) as c:

    # ---- BTC markets today ----
    print("=== KXBTCD (Bitcoin price daily) ===")
    data = get(c, "/markets", {"series_ticker": "KXBTCD", "status": "open", "limit": 50})
    if data:
        mkts = data.get("markets", [])
        print(f"  Total open: {len(mkts)}")
        for m in mkts:
            print(f"  ticker={m['ticker']}")
            print(f"  title={m.get('title','')}")
            print(f"  yes_ask=${m.get('yes_ask_dollars')}  no_ask=${m.get('no_ask_dollars')}")
            print(f"  close_time={m.get('close_time')}  strike_type={m.get('strike_type')}")
            cs = m.get("custom_strike") or {}
            if cs:
                print(f"  custom_strike={json.dumps(cs)[:100]}")
            print()

    print("\n=== KXBTC (Bitcoin price range) ===")
    data = get(c, "/markets", {"series_ticker": "KXBTC", "status": "open", "limit": 50})
    if data:
        mkts = data.get("markets", [])
        print(f"  Total open: {len(mkts)}")
        for m in mkts[:10]:
            print(f"  [{m['ticker']}] yes=${m.get('yes_ask_dollars')} no=${m.get('no_ask_dollars')}  {m.get('title','')[:80]}")

    print("\n=== KXETH (Ethereum price) ===")
    data = get(c, "/markets", {"series_ticker": "KXETH", "status": "open", "limit": 20})
    if data:
        mkts = data.get("markets", [])
        print(f"  Total open: {len(mkts)}")
        for m in mkts[:10]:
            print(f"  [{m['ticker']}] yes=${m.get('yes_ask_dollars')} no=${m.get('no_ask_dollars')}  {m.get('title','')[:80]}")

    print("\n=== WTI OIL (WTIH) ===")
    data = get(c, "/markets", {"series_ticker": "WTIH", "status": "open", "limit": 10})
    if data:
        mkts = data.get("markets", [])
        for m in mkts:
            print(f"  [{m['ticker']}] yes=${m.get('yes_ask_dollars')}  {m.get('title','')[:80]}")

    print("\n=== FED RATE (KXFED) ===")
    data = get(c, "/markets", {"series_ticker": "KXFED", "status": "open", "limit": 5})
    if data:
        mkts = data.get("markets", [])
        for m in mkts:
            print(f"  [{m['ticker']}] yes=${m.get('yes_ask_dollars')}  {m.get('title','')[:80]}")

    # ---- Orderbook for one BTC market ----
    print("\n=== ORDERBOOK SAMPLE (first KXBTCD market) ===")
    data = get(c, "/markets", {"series_ticker": "KXBTCD", "status": "open", "limit": 1})
    if data and data.get("markets"):
        ticker = data["markets"][0]["ticker"]
        ob = get(c, f"/markets/{ticker}/orderbook")
        if ob:
            print(f"  ticker: {ticker}")
            print(json.dumps(ob, indent=2)[:600])
