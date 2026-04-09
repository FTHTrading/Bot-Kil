"""Debug V6 model — show WHY picks get blocked at each gate."""
import asyncio, sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.feeds.kalshi_intraday import get_intraday_markets
from data.feeds.btc_momentum import get_momentum_signals
from engine.intraday_ev import (
    intraday_edge_picks, _DAILY_VOL, _MIN_EDGE, _MIN_BET_PRICE,
    _MAX_BET_PRICE, _MOMENTUM_K, _FEE_RATE,
    _position_prob, _momentum_prob, _blend_prob,
    _MIN_GAP_PCT_EARLY, _MIN_GAP_PCT_LATE,
)

async def main():
    markets = await get_intraday_markets()
    print(f"=== MARKETS: {len(markets)} ===")
    if not markets:
        print("NO MARKETS OPEN — check trading hours (Mon-Sun, varies)")
        return

    for m in markets:
        t = m["ticker"]
        print(f"  {t}  asset={m['asset']}  yes={m['yes_ask']:.2f}  no={m['no_ask']:.2f}  t={m['minutes_remaining']:.1f}min  floor={m['floor_strike']:.2f}")

    momentum = await get_momentum_signals()
    print(f"\n=== MOMENTUM: {list(momentum.keys())} ===")
    for asset, sig in momentum.items():
        cur = sig.get("current", 0)
        print(f"  {asset}: ${cur:,.2f}  5m={sig.get('mom_5m',0)*100:+.4f}%  1m={sig.get('mom_1m',0)*100:+.4f}%  15m={sig.get('mom_15m',0)*100:+.4f}%  trend={sig.get('trend','?')}  vol={sig.get('realized_vol',0)*100:.4f}%")

    print(f"\n=== V6 GATE-BY-GATE ANALYSIS ===")
    print(f"  Config: MIN_EDGE={_MIN_EDGE:.0%}  PRICE=[{_MIN_BET_PRICE:.0%}-{_MAX_BET_PRICE:.0%}]  FEE={_FEE_RATE:.0%}")

    for m in markets:
        asset = m["asset"]
        floor = m["floor_strike"]
        yes_ask = m["yes_ask"]
        no_ask = m["no_ask"]
        t_min = m["minutes_remaining"]
        sig = momentum.get(asset, {})
        current = sig.get("current", floor)

        print(f"\n  --- {m['ticker']} ({asset}) ---")
        if not current or current <= 0:
            print(f"    BLOCKED: no price data")
            continue

        # Vol calc
        default_vol = _DAILY_VOL.get(asset, 0.05)
        vol_floor = default_vol * 0.15
        realized_vol_5m = sig.get("realized_vol", 0.0)
        if realized_vol_5m and realized_vol_5m > 0.00005:
            daily_vol = max(realized_vol_5m * (288 ** 0.5), vol_floor)
        else:
            daily_vol = default_vol

        p_pos = _position_prob(current, floor, t_min, daily_vol)

        mom_5m = sig.get("mom_5m", 0.0)
        mom_15m = sig.get("mom_15m", 0.0)
        mom_1m = sig.get("mom_1m", 0.0)
        mom_3m = sig.get("mom_3m", 0.0)
        trend = sig.get("trend", "flat")

        if t_min <= 3.0 and (mom_1m != 0.0 or mom_3m != 0.0):
            eff_5m = 0.50 * mom_1m + 0.30 * mom_3m + 0.20 * mom_5m
            eff_15m = mom_5m
        else:
            eff_5m = mom_5m
            eff_15m = mom_15m

        p_mom = _momentum_prob(eff_5m, eff_15m, trend, t_min)
        model_prob = _blend_prob(p_pos, p_mom, t_min)

        gap_pct = (current - floor) / floor * 100 if floor > 0 else 0.0

        # Gap threshold
        if t_min <= 2.0:
            gap_thresh = _MIN_GAP_PCT_LATE
        elif t_min >= 5.0:
            gap_thresh = _MIN_GAP_PCT_EARLY
        else:
            frac = (t_min - 2.0) / 3.0
            gap_thresh = _MIN_GAP_PCT_LATE + frac * (_MIN_GAP_PCT_EARLY - _MIN_GAP_PCT_LATE)

        edge_yes = model_prob - yes_ask - _FEE_RATE
        edge_no = (1.0 - model_prob) - no_ask - _FEE_RATE
        best_side = "YES" if edge_yes >= edge_no else "NO"
        best_edge = max(edge_yes, edge_no)
        bet_price = yes_ask if best_side == "YES" else no_ask

        print(f"    price=${current:,.2f}  floor=${floor:,.2f}  gap={gap_pct:+.4f}%  (thresh={gap_thresh:.4f}%)")
        print(f"    p_pos={p_pos:.4f}  p_mom={p_mom:.4f}  blend={model_prob:.4f}  daily_vol={daily_vol:.6f}")
        print(f"    edge_yes={edge_yes:+.4f}  edge_no={edge_no:+.4f}  best={best_side}@{bet_price:.2f}  edge={best_edge:+.4f}")
        print(f"    trend={trend}  mom_5m={mom_5m*100:+.4f}%  mom_1m={mom_1m*100:+.4f}%")

        # Check each gate
        gates = []
        if abs(gap_pct) < gap_thresh:
            gates.append(f"GAP too small ({abs(gap_pct):.4f}% < {gap_thresh:.4f}%)")
        if best_edge < _MIN_EDGE:
            gates.append(f"EDGE too low ({best_edge:.4f} < {_MIN_EDGE})")
        if bet_price < _MIN_BET_PRICE:
            gates.append(f"PRICE too cheap ({bet_price:.2f} < {_MIN_BET_PRICE})")
        if bet_price > _MAX_BET_PRICE:
            gates.append(f"PRICE too expensive ({bet_price:.2f} > {_MAX_BET_PRICE})")
        if trend == "up" and best_side == "NO":
            gates.append(f"CONTRARIAN blocked (trend=up, side=NO)")
        if trend == "down" and best_side == "YES":
            gates.append(f"CONTRARIAN blocked (trend=down, side=YES)")
        if trend == "flat":
            combined = 0.70 * eff_5m + 0.30 * eff_15m
            if best_side == "YES" and combined < 0:
                gates.append(f"FLAT+MOMENTUM disagree (YES but mom={combined*100:+.4f}%)")
            if best_side == "NO" and combined > 0:
                gates.append(f"FLAT+MOMENTUM disagree (NO but mom={combined*100:+.4f}%)")

        if gates:
            print(f"    BLOCKED by: {' | '.join(gates)}")
        else:
            print(f"    >>> WOULD FIRE: {best_side}@{bet_price:.0%} edge={best_edge:+.1%}")

    # Run actual model
    picks = intraday_edge_picks(markets, momentum, bankroll=6.48)
    print(f"\n=== FINAL PICKS: {len(picks)} ===")
    for p in picks:
        print(f"  {p['market']}  {p['side']}@{p.get('implied_prob',0):.0f}%  edge={p['edge_pct']:+.1f}%  conf={p.get('intraday_meta',{}).get('confidence',0)}")

asyncio.run(main())
