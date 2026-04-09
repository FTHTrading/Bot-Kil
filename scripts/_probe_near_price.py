"""Find BTC markets near current price."""
import httpx, base64, time, re
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_pad

KEY_ID = "79cf3d4a-b413-4cdb-b309-dc7cba59b762"
BASE   = "https://api.elections.kalshi.com/trade-api/v2"

def mkhdrs(method, path):
    with open("keys/kalshi_private.pem", "rb") as f:
        priv = load_pem_private_key(f.read(), password=None)
    ts = str(int(time.time() * 1000))
    sig = priv.sign(f"{ts}{method}{path}".encode(),
        asym_pad.PSS(mgf=asym_pad.MGF1(hashes.SHA256()), salt_length=asym_pad.PSS.DIGEST_LENGTH),
        hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KEY_ID, "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

BTC_PRICE = 69666
ETH_PRICE = 2158

all_btc = []
cursor = None
with httpx.Client(timeout=15) as c:
    for _ in range(30):
        params = {"series_ticker": "KXBTCD", "status": "open", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        path = "/trade-api/v2/markets"
        r = c.get(BASE + "/markets", headers=mkhdrs("GET", path), params=params)
        data = r.json()
        mkts = data.get("markets", [])
        all_btc.extend(mkts)
        cursor = data.get("cursor")
        if not cursor or not mkts:
            break

    print(f"Total KXBTCD markets paged: {len(all_btc)}")

    # Extract threshold from ticker like KXBTCD-26APR0611-T69999.99
    def thresh(ticker):
        m = re.search(r"-T([\d.]+)$", ticker)
        return float(m.group(1)) if m else None

    lo, hi = BTC_PRICE * 0.90, BTC_PRICE * 1.10
    near = [(thresh(m["ticker"]), m) for m in all_btc if thresh(m["ticker"])]
    near = [(t, m) for t, m in near if lo <= t <= hi]
    near.sort(key=lambda x: x[0])

    print(f"\nBTC ${BTC_PRICE:,} — markets in ±10% range ({len(near)} total):")
    for t, m in near:
        ya = m.get("yes_ask_dollars", "?")
        na = m.get("no_ask_dollars", "?")
        tk = m["ticker"]
        diff_pct = ((t - BTC_PRICE) / BTC_PRICE) * 100
        print(f"  ${t:>9,.2f}  ({diff_pct:+.1f}%)  YES=${ya}  NO=${na}  {tk}")

    print("\n--- All unique ask prices seen ---")
    from collections import Counter
    yes_prices = Counter(m.get("yes_ask_dollars") for m in all_btc)
    print("YES ask distribution:", dict(yes_prices.most_common(10)))

    # Also check ETH range markets
    print(f"\n--- ETH ${ETH_PRICE} markets ---")
    all_eth = []
    cursor = None
    for _ in range(5):
        params = {"series_ticker": "KXETH", "status": "open", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = c.get(BASE + "/markets", headers=mkhdrs("GET", "/trade-api/v2/markets"), params=params)
        data = r.json()
        mkts = data.get("markets", [])
        all_eth.extend(mkts)
        cursor = data.get("cursor")
        if not cursor or not mkts:
            break
    print(f"Total KXETH: {len(all_eth)}")
    for m in all_eth:
        ya = m.get("yes_ask_dollars", "?")
        na = m.get("no_ask_dollars", "?")
        print(f"  {m['ticker']}  YES=${ya}  NO=${na}  {m.get('title','')[:60]}")
