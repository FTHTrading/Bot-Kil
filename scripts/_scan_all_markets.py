"""Scan ALL Kalshi live markets — find tradeable opportunities across categories.
API fields (2026-07): yes_ask_dollars, no_ask_dollars, volume_fp, volume_24h_fp, liquidity_dollars (all strings).
"""
import asyncio, sys, os, json, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data.feeds.kalshi import _get_headers, KALSHI_BASE_URL
import httpx

def _f(m, key, default=0.0):
    """Parse Kalshi dollar-string field to float."""
    v = m.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default

async def main():
    async with httpx.AsyncClient(timeout=15) as c:
        all_markets = []
        cursor = ""
        pages = 0

        while pages < 25:
            h = _get_headers("GET", "/markets")
            params = {"status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = await c.get(f"{KALSHI_BASE_URL}/markets", headers=h, params=params)
            data = r.json()
            batch = data.get("markets", [])
            all_markets.extend(batch)
            cursor = data.get("cursor", "")
            pages += 1
            if not cursor or not batch:
                break

        print(f"Total active markets fetched: {len(all_markets)}")

        # Filter: skip multivariate combos, keep markets with real activity
        real = []
        mve_count = 0
        for m in all_markets:
            ticker = m.get("ticker", "")
            if "KXMVE" in ticker:
                mve_count += 1
                continue
            vol = _f(m, "volume_fp")
            vol24 = _f(m, "volume_24h_fp")
            liq = _f(m, "liquidity_dollars")
            yes_ask = _f(m, "yes_ask_dollars")
            if vol == 0 and vol24 == 0 and liq == 0 and yes_ask == 0:
                continue
            real.append(m)

        print(f"Filtered out {mve_count} multivariate combos")
        print(f"Real markets with activity: {len(real)}")

        # Group by series prefix (event_ticker before the date/matchup segment)
        categories = {}
        for m in real:
            evt = m.get("event_ticker", m.get("ticker", ""))
            # KXNBAGAME-26APR06DETORL → KXNBAGAME
            match = re.match(r"([A-Z0-9]+?(?:GAME|SPREAD|TOTAL|PROPS?|15M|D|DAILY)?)-", evt)
            series = match.group(1) if match else re.match(r"([A-Z]+)", evt).group(1) if re.match(r"([A-Z]+)", evt) else "OTHER"
            if series not in categories:
                categories[series] = []
            categories[series].append(m)

        print(f"\n{'='*90}")
        print(f"TRADEABLE SERIES — {len(categories)} categories (sorted by total volume)")
        print(f"{'='*90}")

        for series in sorted(categories, key=lambda s: sum(_f(m, "volume_fp") for m in categories[s]), reverse=True):
            mkts = categories[series]
            total_vol = sum(_f(m, "volume_fp") for m in mkts)
            total_liq = sum(_f(m, "liquidity_dollars") for m in mkts)
            total_v24 = sum(_f(m, "volume_24h_fp") for m in mkts)

            # Show everything with volume > 0 OR 3+ markets with liquidity
            if total_vol == 0 and total_liq == 0:
                continue

            print(f"\n{series}: {len(mkts)} mkts | vol=${total_vol:>12,.0f} | vol24h=${total_v24:>10,.0f} | liq=${total_liq:>10,.0f}")

            mkts.sort(key=lambda m: _f(m, "volume_fp"), reverse=True)
            for m in mkts[:8]:
                tk = m.get("ticker", "?")[:50]
                title = (m.get("title") or m.get("yes_sub_title") or "?")[:40]
                ya = _f(m, "yes_ask_dollars")
                na = _f(m, "no_ask_dollars")
                yb = _f(m, "yes_bid_dollars")
                nb = _f(m, "no_bid_dollars")
                vol = _f(m, "volume_fp")
                liq = _f(m, "liquidity_dollars")
                close = (m.get("close_time") or "?")[:16]
                spread = ya - yb if yb > 0 else 0
                print(f"  {tk:50s} yes=${ya:.2f} no=${na:.2f} spr={spread:.2f} vol=${vol:>10,.0f} liq=${liq:>6,.0f}  {title}")

asyncio.run(main())
