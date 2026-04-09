"""Quick diagnostic: run the full edge model on current live markets."""
import asyncio, sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from data.feeds.kalshi_intraday import get_intraday_markets
from data.feeds.btc_momentum import get_momentum_signals
from engine.intraday_ev import intraday_edge_picks, _MIN_EDGE

async def main():
    markets = await get_intraday_markets()
    momentum = await get_momentum_signals()
    bankroll = 9.18
    min_edge = _MIN_EDGE

    print(f"\n=== {len(markets)} live markets ===")
    for m in markets:
        print(f"  {m['ticker']}  asset={m['asset']}  yes={m['yes_ask']:.2f}  no={m['no_ask']:.2f}  {m['minutes_remaining']:.1f}min  OI={m['open_interest']:.0f}  floor={m.get('floor_strike',0)}")

    print(f"\n=== Momentum keys: {list(momentum.keys())} ===")
    for asset, s in momentum.items():
        print(f"  {asset}: price={s.get('current',0):.4f}  5m={s.get('mom_5m',0)*100:+.3f}%  15m={s.get('mom_15m',0)*100:+.3f}%  1m={s.get('mom_1m',0)*100:+.3f}%  trend={s.get('trend','?')}")

    print(f"\n=== Running model (bankroll={bankroll}, min_edge={min_edge}) ===")
    picks = intraday_edge_picks(markets, momentum, bankroll, min_edge)
    print(f"  → {len(picks)} picks returned")
    for p in picks:
        meta = p.get("intraday_meta", {})
        print(f"  {p['market']}  side={p['side']}  edge={p['edge_pct']:+.1f}%  our_prob={p['our_prob']:.0f}%  imp={p['implied_prob']:.0f}%  gap={meta.get('gap_pct',0):+.3f}%  conf={meta.get('confidence','?')}")

    if not picks:
        # Run with min_edge=0 to see what model WOULD produce
        print(f"\n=== Re-run with min_edge=0 to see raw model output ===")
        raw = intraday_edge_picks(markets, momentum, bankroll, 0.0)
        print(f"  → {len(raw)} raw picks")
        for p in raw:
            meta = p.get("intraday_meta", {})
            v = meta.get("verdict", "?")
            print(f"  {p['market']}  side={p['side']}  edge={p['edge_pct']:+.1f}%  our_prob={p['our_prob']:.0f}%  imp={p['implied_prob']:.0f}%  gap={meta.get('gap_pct',0):+.3f}%  trend={meta.get('trend','?')}  verdict={v}")

asyncio.run(main())
