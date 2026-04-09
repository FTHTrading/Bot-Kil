import asyncio, sys
sys.path.insert(0, '.')
from data.feeds.btc_momentum import get_momentum_signals

async def main():
    sigs = await get_momentum_signals(['BTC','ETH','XRP','SOL'])
    for asset, s in sigs.items():
        live = s.get("current", 0)
        m5   = s.get("mom_5m", 0)
        m15  = s.get("mom_15m", 0)
        trnd = s.get("trend", "?")
        src  = s.get("spot_source", "?")
        cls  = [round(c, 4) for c in s.get("closes", [])]
        print(f"{asset}  live={live:>10.4f}  mom_5m={m5:+.4f}  mom_15m={m15:+.4f}  trend={trnd}  src={src}")
        print(f"       closes={cls}")

asyncio.run(main())
