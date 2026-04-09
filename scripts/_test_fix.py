"""Test the fixed model with synthetic market data to verify picks are generated."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine.intraday_ev import intraday_edge_picks

# Simulate markets similar to what we saw earlier:
# ETH floor=2146.26, current=2142.47, YES=0.05, NO=0.97, 6min
# SOL floor=81.75, current=81.57, YES=0.05, NO=0.97, 6min
# XRP floor=1.3393, current=1.3368, YES=0.10, NO=0.92, 6min
# DOGE floor=0.091589, current=0.0915, YES=0.15, NO=0.86, 6min

markets = [
    {"ticker": "TEST-ETH", "title": "ETH 15m", "asset": "ETH", "floor_strike": 2146.26,
     "yes_ask": 0.05, "no_ask": 0.97, "minutes_remaining": 6.0, "open_interest": 5000, "close_time": ""},
    {"ticker": "TEST-SOL", "title": "SOL 15m", "asset": "SOL", "floor_strike": 81.75,
     "yes_ask": 0.05, "no_ask": 0.97, "minutes_remaining": 6.0, "open_interest": 1400, "close_time": ""},
    {"ticker": "TEST-XRP", "title": "XRP 15m", "asset": "XRP", "floor_strike": 1.3393,
     "yes_ask": 0.10, "no_ask": 0.92, "minutes_remaining": 6.0, "open_interest": 500, "close_time": ""},
    {"ticker": "TEST-DOGE", "title": "DOGE 15m", "asset": "DOGE", "floor_strike": 0.091589,
     "yes_ask": 0.15, "no_ask": 0.86, "minutes_remaining": 6.0, "open_interest": 400, "close_time": ""},
    # Also test an early-window scenario (12 min left, prices near 50/50)
    {"ticker": "TEST-BTC-EARLY", "title": "BTC 15m early", "asset": "BTC", "floor_strike": 69500.0,
     "yes_ask": 0.48, "no_ask": 0.54, "minutes_remaining": 12.0, "open_interest": 50000, "close_time": ""},
    # And a mid-window with moderate gap
    {"ticker": "TEST-BTC-MID", "title": "BTC 15m mid", "asset": "BTC", "floor_strike": 69500.0,
     "yes_ask": 0.30, "no_ask": 0.72, "minutes_remaining": 8.0, "open_interest": 40000, "close_time": ""},
]

momentum = {
    "BTC":  {"current": 69486.01, "mom_5m": -0.00032, "mom_15m": -0.00229, "mom_1m": 0.0, "mom_3m": 0.0, "trend": "down", "realized_vol": 0.000530},
    "ETH":  {"current": 2141.41,  "mom_5m": -0.00042, "mom_15m": -0.00145, "mom_1m": 0.0, "mom_3m": 0.0, "trend": "down", "realized_vol": 0.000397},
    "SOL":  {"current": 81.56,    "mom_5m": -0.00049, "mom_15m": -0.00196, "mom_1m": 0.0, "mom_3m": 0.0, "trend": "down", "realized_vol": 0.000519},
    "DOGE": {"current": 0.09150,  "mom_5m": -0.00120, "mom_15m": -0.00098, "mom_1m": 0.0, "mom_3m": 0.0, "trend": "down", "realized_vol": 0.000673},
    "XRP":  {"current": 1.3367,   "mom_5m": -0.00030, "mom_15m": -0.00134, "mom_1m": 0.0, "mom_3m": 0.0, "trend": "down", "realized_vol": 0.000700},
}

bankroll = 9.18
picks = intraday_edge_picks(markets, momentum, bankroll)

print(f"\n=== {len(picks)} picks generated ===")
for p in picks:
    meta = p["intraday_meta"]
    print(f"  {p['market']:20s}  {p['side'].upper():3s}  edge={p['edge_pct']:+6.1f}%  "
          f"our_prob={p['our_prob']:4.0f}%  imp={p['implied_prob']:4.0f}%  "
          f"stake=${p['recommended_stake']:.2f}  conf={meta.get('confidence','?')}  "
          f"verdict={p['verdict']}")

if not picks:
    print("  STILL NO PICKS — need further investigation")
