"""Inspect intraday market structure."""
import asyncio, sys, json
sys.path.insert(0, ".")
from data.feeds.kalshi_crypto import _headers, _BASE
import httpx

async def main():
    series_list = ["KXBTC15M","KXETH15M","KXSOL15M","KXDOGE15M","KXXRP15M","KXBNB15M","KXINXI","NASDAQ100I"]
    async with httpx.AsyncClient(timeout=15) as c:
        for series in series_list:
            r = await c.get(
                _BASE + "/markets", headers=_headers("GET", "/markets"),
                params={"series_ticker": series, "status": "open", "limit": 5},
            )
            markets = r.json().get("markets", [])
            if markets:
                m = markets[0]
                print(f"\n=== {series} ===")
                print(f"  Sample ticker: {m['ticker']}")
                print(f"  Title: {m.get('title','?')}")
                print(f"  close_time: {m.get('close_time','?')}")
                print(f"  yes_ask_dollars: {m.get('yes_ask_dollars')}  no_ask_dollars: {m.get('no_ask_dollars')}")
                print(f"  open_interest_fp: {m.get('open_interest_fp')}")
                print(f"  subtitle: {m.get('subtitle','?')}")
                # Show all keys on first series
                if series == "KXBTC15M":
                    print(f"  All fields:")
                    for k, v in sorted(m.items()):
                        print(f"    {k}: {v}")
            else:
                print(f"\n=== {series} === (no open markets)")

asyncio.run(main())
