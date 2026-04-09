"""Discover Kalshi series by category + find BTC/crypto markets."""
import httpx, base64, time, json, sys
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
    try:
        return r.json()
    except Exception as e:
        print(f"  JSON error: {e} | body: {r.text[:200]}")
        return None

with httpx.Client(timeout=15) as c:

    # ------------------------------------------------------------------
    # 1. List series (paginate first few pages to find crypto/financials)
    # ------------------------------------------------------------------
    print("=== SERIES ENDPOINT ===")
    data = get(c, "/series", {"limit": 200})
    if data:
        print(f"  Keys in response: {list(data.keys())}")
        series_list = data.get("series", [])
        print(f"  Total series returned: {len(series_list)}")
        if series_list:
            print(f"  Sample[0]: {json.dumps(series_list[0], indent=2)[:400]}")
            # Filter interesting ones
            interesting = [s for s in series_list
                           if any(k in (s.get("title","") + s.get("category","") + s.get("ticker","")).upper()
                                  for k in ["BTC","BITCOIN","ETH","CRYPTO","NASDAQ","S&P","SPX","FED","GOLD","OIL","NFT","TRUMP","ELECTION"])]
            print(f"\n  Interesting series ({len(interesting)}):")
            for s in interesting[:30]:
                print(f"    [{s.get('ticker')}] cat={s.get('category')} {s.get('title','')[:60]}")

    # ------------------------------------------------------------------
    # 2. Try markets with series_ticker filter (use known financial patterns)
    # ------------------------------------------------------------------
    print("\n=== MARKETS BY SERIES_TICKER ===")
    guesses = ["KXBTC", "KXBTCD", "KXETH", "KXBTCW", "KXBTCM",
               "KXNAMSPX", "KXNAZNASDAQ", "KXFED", "KXGOLD"]
    for st in guesses:
        data = get(c, "/markets", {"series_ticker": st, "status": "open", "limit": 5})
        if data:
            mkts = data.get("markets", [])
            if mkts:
                print(f"  {st}: {len(mkts)} markets | e.g. [{mkts[0].get('ticker')}] {mkts[0].get('title','')[:60]}")

    # ------------------------------------------------------------------
    # 3. Events with keyword in title
    # ------------------------------------------------------------------
    print("\n=== EVENTS ENDPOINT ===")
    data = get(c, "/events", {"limit": 5})
    if data:
        print(f"  Keys: {list(data.keys())}")
        events = data.get("events", [])
        if events:
            print(f"  Sample event: {json.dumps(events[0], indent=2)[:400]}")

    # ------------------------------------------------------------------
    # 4. Raw market sample — what fields exist?
    # ------------------------------------------------------------------
    print("\n=== MARKET FIELD SAMPLE ===")
    data = get(c, "/markets", {"limit": 1, "status": "open"})
    if data:
        mkts = data.get("markets", [])
        if mkts:
            print(json.dumps(mkts[0], indent=2))

    # ------------------------------------------------------------------
    # 5. Check portfolio / orderbook endpoints
    # ------------------------------------------------------------------
    print("\n=== PORTFOLIO ENDPOINTS ===")
    for ep in ["/portfolio/positions", "/portfolio/orders"]:
        data = get(c, ep)
        if data:
            print(f"  {ep}: {list(data.keys())}")
