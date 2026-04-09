"""Find ALL Kalshi series and check which have volume. Also check current crypto 15m."""
import asyncio, sys, os, json
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data.feeds.kalshi import _get_headers, KALSHI_BASE_URL
import httpx

def _f(m, key, default=0.0):
    v = m.get(key)
    if v is None: return default
    try: return float(v)
    except: return default

CRYPTO_SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXXRP15M", "KXBNB15M", "KXHYPE15M"]
SPORTS_SERIES = ["KXMLBGAME", "KXNBAGAME", "KXNHLGAME", "KXNCAAMBGAME", "KXUFCGAME", "KXSOCCERGAME",
                 "KXMLBTOTAL", "KXNBATOTAL", "KXNHLTOTAL", "KXMLBSPREAD", "KXNBASPREAD", "KXNHLSPREAD"]
ESPORTS_SERIES = ["KXCS2GAME", "KXVALORANTGAME", "KXLOLGAME", "KXDOTAGAME"]
WEATHER_SERIES = ["KXTEMPNYCH"]
OTHER_SERIES = ["KXGASD", "KXGAS", "KXWTI"]

ALL_SERIES = CRYPTO_SERIES + SPORTS_SERIES + ESPORTS_SERIES + WEATHER_SERIES + OTHER_SERIES

async def check_series(c, series_ticker):
    """Get recent events for a series and check their markets."""
    h = _get_headers("GET", "/events")
    r = await c.get(f"{KALSHI_BASE_URL}/events", headers=h, params={"series_ticker": series_ticker, "limit": 5})
    events = r.json().get("events", [])
    if not events:
        return None
    
    # Check markets for the most recent event 
    event_ticker = events[0].get("event_ticker", "")
    h2 = _get_headers("GET", f"/events/{event_ticker}/markets")
    r2 = await c.get(f"{KALSHI_BASE_URL}/events/{event_ticker}/markets", headers=h2, params={"limit": 20})
    if r2.status_code != 200:
        markets = []
    else:
        try:
            markets = r2.json().get("markets", [])
        except Exception:
            markets = []
    
    total_vol = sum(_f(m, "volume_fp") for m in markets)
    total_liq = sum(_f(m, "liquidity_dollars") for m in markets)
    title = events[0].get("title", "?")
    
    return {
        "series": series_ticker,
        "event": event_ticker,
        "title": title,
        "num_events": len(events),
        "num_markets": len(markets),
        "total_vol": total_vol,
        "total_liq": total_liq,
        "markets": markets
    }

async def main():
    now = datetime.now(timezone.utc)
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    
    async with httpx.AsyncClient(timeout=15) as c:
        results = []
        for series in ALL_SERIES:
            result = await check_series(c, series)
            if result:
                results.append(result)
                status = "HAS DATA" if result["total_vol"] > 0 or result["total_liq"] > 0 else "no vol"
                print(f"  {series:20s} events={result['num_events']:2d} mkts={result['num_markets']:3d} vol=${result['total_vol']:>10,.0f} liq=${result['total_liq']:>8,.0f} [{status}] {result['title'][:50]}")
            else:
                print(f"  {series:20s} NO EVENTS")
        
        # Show detail for any series with actual volume or markets
        print(f"\n{'='*100}")
        print("DETAILED VIEW - Series with markets")
        for r in results:
            if r["num_markets"] > 0:
                print(f"\n--- {r['series']} ({r['title'][:50]}) ---")
                for m in r["markets"][:5]:
                    tk = m.get("ticker", "")[:55]
                    ya = _f(m, "yes_ask_dollars")
                    na = _f(m, "no_ask_dollars")
                    yb = _f(m, "yes_bid_dollars")
                    vol = _f(m, "volume_fp")
                    liq = _f(m, "liquidity_dollars")
                    status = m.get("status", "?")
                    close = (m.get("close_time") or "?")[:16]
                    title = (m.get("title") or m.get("yes_sub_title") or "")[:40]
                    print(f"  {tk:55s} ya=${ya:.2f} na=${na:.2f} vol=${vol:>8,.0f} liq=${liq:>6,.0f} st={status:10s} {title}")

asyncio.run(main())
