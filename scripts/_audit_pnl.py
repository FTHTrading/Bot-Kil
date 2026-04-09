"""Quick P&L audit — run once to see exactly where money went."""
import asyncio
import json
from data.feeds.kalshi import get_orders, get_portfolio, get_balance, get_settlements

async def main():
    balance = await get_balance()
    print(f"Current balance: ${balance:.2f}")
    print()

    # Get all executed orders
    orders = await get_orders("executed")
    print(f"Total executed orders: {len(orders)}")
    total_cost = 0.0
    for o in orders:
        t = o.get("ticker", "?")
        side = o.get("side", "?")
        fill_cost_maker = float(o.get("maker_fill_cost_dollars", "0"))
        fill_cost_taker = float(o.get("taker_fill_cost_dollars", "0"))
        cost = fill_cost_maker + fill_cost_taker
        yes_price = o.get("yes_price_dollars", "0")
        no_price = o.get("no_price_dollars", "0")
        fill = o.get("fill_count_fp", "0")
        fees_maker = float(o.get("maker_fees_dollars", "0"))
        fees_taker = float(o.get("taker_fees_dollars", "0"))
        created = o.get("created_time", "?")[:19]
        total_cost += cost
        print(f"  {created}  {t:<42}  {side:<3}  fill={fill}  cost=${cost:.2f}  fees=${fees_maker+fees_taker:.3f}  yes=${yes_price}  no=${no_price}")
    print(f"\nTotal spent on fills: ${total_cost:.2f}")
    print()

    # Get positions
    port = await get_portfolio()
    positions = port.get("positions", [])
    print(f"Active positions: {len(positions)}")
    for p in positions[:30]:
        print(json.dumps(p, indent=2, default=str))
    print()

    # Get settlements
    settlements = await get_settlements()
    print(f"Settlements: {len(settlements)}")
    total_payout = 0.0
    for s in settlements[:30]:
        print(json.dumps(s, indent=2, default=str))
        total_payout += float(s.get("revenue", 0))
    if settlements:
        print(f"\nTotal settlement revenue: ${total_payout:.2f}")

asyncio.run(main())
