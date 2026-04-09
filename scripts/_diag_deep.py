"""Deep diagnostic: step through model per-market to see exactly where picks get filtered."""
import asyncio, sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from data.feeds.kalshi_intraday import get_intraday_markets
from data.feeds.btc_momentum import get_momentum_signals
from engine.intraday_ev import (
    _position_prob, _momentum_prob, _blend_prob,
    _MIN_GAP_PCT_EARLY, _MIN_GAP_PCT_LATE, _MIN_EDGE, _MIN_BET_PRICE,
    _FEE_RATE, _DAILY_VOL, _PROB_CAP, _PROB_FLOOR
)

async def main():
    markets = await get_intraday_markets()
    momentum = await get_momentum_signals()
    bankroll = 9.18

    for m in markets:
        asset = m["asset"]
        floor = m["floor_strike"]
        yes_ask = m["yes_ask"]
        no_ask = m["no_ask"]
        t_min = m["minutes_remaining"]
        sig = momentum.get(asset, {})
        current = sig.get("current", floor)

        print(f"\n{'='*70}")
        print(f"  {m['ticker']}  asset={asset}")
        print(f"  floor={floor}  current={current}  YES={yes_ask:.2f}  NO={no_ask:.2f}  {t_min:.1f}min")

        if not current or current <= 0:
            print("  REJECTED: no current price")
            continue

        # Realized vol → daily vol
        realized_vol_5m = sig.get("realized_vol", 0.0)
        if realized_vol_5m and realized_vol_5m > 0.00005:
            daily_vol = realized_vol_5m * (288 ** 0.5)
            print(f"  daily_vol = {daily_vol:.6f} (from realized_vol_5m={realized_vol_5m:.6f})")
        else:
            daily_vol = _DAILY_VOL.get(asset, 0.05)
            print(f"  daily_vol = {daily_vol:.6f} (DEFAULT — no realized vol)")
        default_daily_vol = _DAILY_VOL.get(asset, 0.05)
        print(f"  default_daily_vol = {default_daily_vol:.4f}  ratio = {daily_vol/default_daily_vol:.2f}x")

        # Position prob
        p_pos = _position_prob(current, floor, t_min, daily_vol)
        p_pos_default = _position_prob(current, floor, t_min, default_daily_vol)
        print(f"  p_pos = {p_pos:.6f}  (with default vol: {p_pos_default:.4f})")

        # Momentum
        mom_5m = sig.get("mom_5m", 0.0)
        mom_15m = sig.get("mom_15m", 0.0)
        mom_1m = sig.get("mom_1m", 0.0)
        trend = sig.get("trend", "flat")
        p_mom = _momentum_prob(mom_5m, mom_15m, trend, t_min)
        print(f"  mom_5m={mom_5m*100:+.3f}%  mom_15m={mom_15m*100:+.3f}%  mom_1m={mom_1m*100:+.3f}%  trend={trend}")
        print(f"  p_mom = {p_mom:.4f}")

        # Gap gate
        gap_pct = (current - floor) / floor * 100 if floor > 0 else 0.0
        if t_min <= 2.0:
            gap_thresh = _MIN_GAP_PCT_LATE
        elif t_min >= 5.0:
            gap_thresh = _MIN_GAP_PCT_EARLY
        else:
            frac = (t_min - 2.0) / 3.0
            gap_thresh = _MIN_GAP_PCT_LATE + frac * (_MIN_GAP_PCT_EARLY - _MIN_GAP_PCT_LATE)
        passes_gap = abs(gap_pct) >= gap_thresh
        print(f"  gap = {gap_pct:+.4f}%  thresh = {gap_thresh:.4f}%  {'PASS' if passes_gap else '*** FILTERED BY GAP ***'}")

        if not passes_gap:
            continue

        # Blend
        model_prob = _blend_prob(p_pos, p_mom, t_min)
        model_prob_default = _blend_prob(p_pos_default, p_mom, t_min)
        print(f"  model_prob = {model_prob:.4f}  (with default vol: {model_prob_default:.4f})")

        # Edge (fee-adjusted)
        edge_yes = model_prob - yes_ask - _FEE_RATE
        edge_no = (1.0 - model_prob) - no_ask - _FEE_RATE
        print(f"  edge_yes = {edge_yes:+.4f}  edge_no = {edge_no:+.4f}")

        edge_yes_d = model_prob_default - yes_ask - _FEE_RATE
        edge_no_d = (1.0 - model_prob_default) - no_ask - _FEE_RATE
        print(f"  (default vol) edge_yes = {edge_yes_d:+.4f}  edge_no = {edge_no_d:+.4f}")

        # Side selection
        if edge_yes >= edge_no and edge_yes >= _MIN_EDGE:
            side, edge, bet_price = "YES", edge_yes, yes_ask
        elif edge_no > edge_yes and edge_no >= _MIN_EDGE:
            side, edge, bet_price = "NO", edge_no, no_ask
        else:
            print(f"  *** FILTERED BY MIN_EDGE ({_MIN_EDGE}) ***")
            # Show what would happen with default vol
            if edge_yes_d >= edge_no_d and edge_yes_d >= _MIN_EDGE:
                print(f"  (default vol WOULD give: YES edge={edge_yes_d:+.4f} → PASS)")
            elif edge_no_d >= _MIN_EDGE:
                print(f"  (default vol WOULD give: NO edge={edge_no_d:+.4f} → PASS)")
            continue

        print(f"  → side={side}  edge={edge:+.4f}  bet_price={bet_price:.2f}")

        # MIN_BET_PRICE gate
        if bet_price < _MIN_BET_PRICE:
            print(f"  *** FILTERED BY MIN_BET_PRICE ({_MIN_BET_PRICE}) — bet_price {bet_price:.2f} < {_MIN_BET_PRICE} ***")
            continue

        # Signal agreement
        if trend == "up" and side == "NO":
            print(f"  *** FILTERED BY SIGNAL AGREEMENT — trend=up but side=NO ***")
            continue
        if trend == "down" and side == "YES":
            print(f"  *** FILTERED BY SIGNAL AGREEMENT — trend=down but side=YES ***")
            continue

        print(f"  ✓ PICK WOULD BE GENERATED")

asyncio.run(main())
