"""Debug kalshi_intraday filter."""
import asyncio, sys
sys.path.insert(0, ".")
from data.feeds.kalshi_intraday import _headers, _BASE, _norm_intraday
import httpx
from datetime import datetime, timezone

async def main():
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            _BASE + "/markets",
            headers=_headers("GET", "/markets"),
            params={"series_ticker": "KXBTC15M", "status": "open", "limit": 5},
        )
        print(f"HTTP {r.status_code}")
        markets = r.json().get("markets", [])
        print(f"Got {len(markets)} markets")
        for m in markets:
            print(f"\nTicker: {m['ticker']}")
            print(f"  close_time: {m.get('close_time')}")
            print(f"  floor_strike: {m.get('floor_strike')}")
            print(f"  yes_ask_dollars: {m.get('yes_ask_dollars')}")
            print(f"  no_ask_dollars: {m.get('no_ask_dollars')}")
            print(f"  open_interest_fp: {m.get('open_interest_fp')}")
            # Compute minutes remaining
            from data.feeds.kalshi_intraday import _minutes_to_close
            t = _minutes_to_close(m.get("close_time", ""))
            print(f"  minutes_remaining: {t:.2f}")
            # Try normalise
            nm = _norm_intraday(m, "BTC", "KXBTC15M")
            print(f"  _norm_intraday => {nm is not None}")
            if nm is None:
                # Diagnose why
                try:
                    fs = float(m.get("floor_strike"))
                except: fs = None
                try:
                    ya = float(m.get("yes_ask_dollars"))
                except: ya = None
                try:
                    na = float(m.get("no_ask_dollars"))
                except: na = None
                oi = float(m.get("open_interest_fp") or 0)
                print(f"  REJECTED: floor={fs} ya={ya} na={na} oi={oi} t={t:.2f}")

asyncio.run(main())
