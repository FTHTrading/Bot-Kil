"""Detailed trade audit — dump raw API responses."""
import asyncio, sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data.feeds import kalshi

async def main():
    bal = await kalshi.get_balance()
    print(f"=== Balance: ${bal/100:.2f} ===\n")
    
    # Get ALL orders (any status)
    for status in ["resting", "settled", "canceled", "pending"]:
        try:
            orders = await kalshi.get_orders(status)
            if orders:
                print(f"\n--- Orders status={status} ({len(orders)}) ---")
                for o in orders[:5]:
                    print(json.dumps(o, indent=2, default=str)[:500])
                    print()
        except Exception as e:
            print(f"  get_orders({status}): {e}")
    
    # Raw settlements
    settlements = await kalshi.get_settlements()
    if settlements:
        print(f"\n--- Raw settlement [0] keys ---")
        print(json.dumps(settlements[0], indent=2, default=str)[:800])
        print(f"\n--- Raw settlement [1] ---")
        if len(settlements) > 1:
            print(json.dumps(settlements[1], indent=2, default=str)[:800])
    
    # Try portfolio/positions
    try:
        portfolio = await kalshi.get_portfolio()
        print(f"\n--- Portfolio ({len(portfolio)} items) ---")
        if portfolio:
            for p in portfolio[:5]:
                print(json.dumps(p, indent=2, default=str)[:500])
    except Exception as e:
        print(f"Portfolio: {e}")

asyncio.run(main())
