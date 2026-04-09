"""Full settlement audit — all positions settled today."""
import asyncio, sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data.feeds import kalshi

async def main():
    bal = await kalshi.get_balance()
    print(f"=== Balance: ${bal:.2f} ===\n")
    
    settlements = await kalshi.get_settlements()
    
    total_cost = 0
    total_revenue = 0
    wins = 0
    losses = 0
    
    print(f"All settlements ({len(settlements)} total):")
    print(f"{'Ticker':50s} {'Result':6s} {'Side':4s} {'Qty':>4s} {'Cost':>8s} {'Revenue':>8s} {'P&L':>8s} {'Fee':>7s}")
    print("-" * 110)
    
    for s in settlements:
        ticker = s.get("ticker", "?")
        result = s.get("market_result", "?")
        yes_count = float(s.get("yes_count_fp", 0))
        no_count = float(s.get("no_count_fp", 0))
        yes_cost = float(s.get("yes_total_cost_dollars", 0))
        no_cost = float(s.get("no_total_cost_dollars", 0))
        revenue_raw = s.get("revenue", 0)
        fee = float(s.get("fee_cost", 0))
        
        if yes_count > 0:
            side = "YES"
            qty = int(yes_count)
            cost = yes_cost
        elif no_count > 0:
            side = "NO"
            qty = int(no_count)
            cost = no_cost
        else:
            side = "---"
            qty = 0
            cost = 0
        
        # Revenue from Kalshi: cents integer or dollars float
        if isinstance(revenue_raw, (int, float)):
            if revenue_raw > 100:  # probably cents
                revenue = revenue_raw / 100
            else:
                revenue = revenue_raw
        else:
            revenue = 0
        
        pnl = revenue - cost - fee
        total_cost += cost
        total_revenue += revenue
        
        if cost > 0:
            if revenue > cost:
                wins += 1
                marker = "W"
            else:
                losses += 1
                marker = "L"
        else:
            marker = "-"
        
        if cost > 0 or revenue > 0:
            print(f"{ticker:50s} {result:6s} {side:4s} {qty:4d} ${cost:7.2f} ${revenue:7.2f} ${pnl:+7.2f} ${fee:6.3f} {marker}")
    
    print("-" * 110)
    print(f"TOTALS: Cost=${total_cost:.2f}  Revenue=${total_revenue:.2f}  Net=${total_revenue-total_cost:.2f}")
    print(f"Record: {wins}W / {losses}L")
    
    # Check for resting orders (open positions)
    try:
        resting = await kalshi.get_orders("resting")
        if resting:
            print(f"\n=== OPEN/RESTING ORDERS ({len(resting)}) ===")
            for o in resting:
                print(f"  {o.get('ticker','?')} {o.get('side','?')} x{o.get('count',0)} price={o.get('yes_price','?')}c status={o.get('status','?')}")
    except Exception as e:
        print(f"Resting orders: {e}")

asyncio.run(main())
