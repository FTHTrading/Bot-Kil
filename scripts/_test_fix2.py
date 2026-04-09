"""Test early-window and mid-window scenarios."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine.intraday_ev import intraday_edge_picks

# Scenario 1: BTC slightly above floor, early window, prices near 50/50
# Scenario 2: BTC slightly below floor, mid window
markets = [
    {"ticker": "T-BTC-EARLY", "title": "BTC 15m", "asset": "BTC", "floor_strike": 69500.0,
     "yes_ask": 0.45, "no_ask": 0.57, "minutes_remaining": 13.0, "open_interest": 50000, "close_time": ""},
    {"ticker": "T-BTC-MID", "title": "BTC 15m mid", "asset": "BTC", "floor_strike": 69500.0,
     "yes_ask": 0.35, "no_ask": 0.67, "minutes_remaining": 8.0, "open_interest": 40000, "close_time": ""},
    # Scenario 3: ETH right at floor, very early
    {"ticker": "T-ETH-FLAT", "title": "ETH 15m", "asset": "ETH", "floor_strike": 2145.0,
     "yes_ask": 0.50, "no_ask": 0.52, "minutes_remaining": 14.0, "open_interest": 3000, "close_time": ""},
]
momentum = {
    "BTC": {"current": 69535.0, "mom_5m": 0.0003, "mom_15m": 0.0005, "mom_1m": 0.0, "mom_3m": 0.0, "trend": "up", "realized_vol": 0.0005},
    "ETH": {"current": 2145.5, "mom_5m": 0.0001, "mom_15m": 0.0002, "mom_1m": 0.0, "mom_3m": 0.0, "trend": "flat", "realized_vol": 0.0004},
}
picks = intraday_edge_picks(markets, momentum, 9.18)
print(f"{len(picks)} picks from early/mid/flat tests:")
for p in picks:
    m = p["intraday_meta"]
    print(f"  {p['market']:15s} {p['side'].upper():3s}  edge={p['edge_pct']:+.1f}%  prob={p['our_prob']:.0f}%  stake=${p['recommended_stake']:.2f}  conf={m['confidence']}")
if not picks:
    print("  (none — model correctly finds no edge in well-priced markets)")
