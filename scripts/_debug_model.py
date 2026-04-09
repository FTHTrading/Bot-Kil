"""Debug the intraday edge model against live market data."""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv; load_dotenv()

from data.feeds.kalshi_intraday import get_intraday_markets
from data.feeds.btc_momentum import get_momentum_signals
from engine.intraday_ev import _position_prob, _momentum_prob, _blend_prob, _DAILY_VOL

async def main():
    markets, momentum = await asyncio.gather(
        get_intraday_markets(),
        get_momentum_signals()
    )
    print(f"Markets fetched: {len(markets)},  Momentum assets: {list(momentum.keys())}\n")
    for m in markets:
        asset = m["asset"]
        floor = m["floor_strike"]
        yes_ask = m["yes_ask"]
        no_ask  = m["no_ask"]
        t = m["minutes_remaining"]
        sig = momentum.get(asset, {})
        current = sig.get("current", floor)
        gap_pct = (current - floor) / floor * 100 if floor else 0

        dv = _DAILY_VOL.get(asset, 0.05)
        p_pos = _position_prob(current, floor, t, dv)
        mom5  = sig.get("mom_5m", 0.0)
        mom15 = sig.get("mom_15m", 0.0)
        trend = sig.get("trend", "flat")
        p_mom = _momentum_prob(mom5, mom15, trend, t)
        blend = _blend_prob(p_pos, p_mom, t)

        edge_yes = blend - yes_ask
        edge_no  = (1 - blend) - no_ask

        print(f"{asset:5} floor={floor:.4f}  current={current:.4f}  gap={gap_pct:+.3f}%  t={t:.1f}m")
        print(f"       p_pos={p_pos:.3f}  p_mom={p_mom:.3f}  blend={blend:.3f}")
        print(f"       yes_ask={yes_ask:.2f}  no_ask={no_ask:.2f}")
        print(f"       edge_YES={edge_yes:+.3f}  edge_NO={edge_no:+.3f}  <- {('YES!' if edge_yes>0.04 else 'NO!' if edge_no>0.04 else 'no edge')}")
        print()

asyncio.run(main())
