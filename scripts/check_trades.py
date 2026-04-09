"""Quick check: balance + recent settled orders."""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data.feeds import kalshi

async def main():
    bal = await kalshi.get_balance()
    print(f"Balance: ${bal/100:.2f}")
    
    # Check positions
    try:
        positions = await kalshi.get_portfolio()
        if positions:
            print(f"\nOpen positions ({len(positions)}):")
            for p in positions[:10]:
                print(f"  {p}")
    except Exception as e:
        print(f"Portfolio check: {e}")
    
    # Recent settled orders
    orders = await kalshi.get_orders("settled")
    print(f"\nRecent settled orders ({len(orders)} total, showing last 10):")
    for o in orders[:10]:
        ticker = o.get("ticker", "?")
        side = o.get("side", "?")
        count = o.get("count", 0)
        yes_price = o.get("yes_price", 0)
        no_price = o.get("no_price", 0)
        revenue = o.get("revenue", 0)
        cost = yes_price * count if side == "yes" else no_price * count
        pnl = revenue - cost
        order_id = o.get("order_id", "")[:8]
        print(f"  {ticker:45s} {side:3s} x{count:2d} @{yes_price:3d}c  cost={cost:4d}c rev={revenue:4d}c  P&L={pnl:+5d}c  [{order_id}]")
    
    # Recent settlements
    settlements = await kalshi.get_settlements()
    print(f"\nRecent settlements ({len(settlements)} total, showing last 10):")
    for s in settlements[:10]:
        ticker = s.get("market_ticker", s.get("ticker", "?"))
        result = s.get("result", "?")
        yes_price = s.get("yes_price", 0)
        settled = s.get("settled_time", s.get("settlement_time", "?"))
        print(f"  {ticker:45s} result={result:5s} yes={yes_price:3d}c  {settled}")

asyncio.run(main())
