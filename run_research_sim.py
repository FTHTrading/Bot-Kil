"""
Kalishi Edge — Full Research Intelligence Simulation
Runs the conviction engine, strategy library, and learning tracker
against synthetic market data to demonstrate the full pipeline.
"""
import sys, os
sys.path.insert(0, r"C:\Users\Kevan\kalishi-edge")

import json
from datetime import datetime, timezone, timedelta
import random

random.seed(42)

from research.conviction_engine import analyze_conviction, find_locks, find_jackpots, find_best_value
from research.strategy_library import score_market
from research import learning_tracker

print("=" * 65)
print("  KALISHI EDGE — Research Intelligence Simulation")
print("=" * 65)

# ── Generate synthetic markets across categories ──

def make_market(ticker, category, yes_ask, volume, oi, hours_left, **kw):
    return {
        "ticker": ticker,
        "category": category,
        "yes_ask": yes_ask,
        "yes_bid": yes_ask - random.uniform(0.01, 0.03),
        "no_ask": round(1.0 - yes_ask + random.uniform(0.01, 0.02), 3),
        "no_bid": round(1.0 - yes_ask - random.uniform(0.01, 0.03), 3),
        "volume": volume,
        "volume_24h": int(volume * random.uniform(0.3, 0.8)),
        "open_interest": oi,
        "open_interest_delta": int(oi * random.uniform(-0.15, 0.25)),
        "close_time": (datetime.now(timezone.utc) + timedelta(hours=hours_left)).isoformat(),
        "minutes_remaining": hours_left * 60,
        "status": "active",
        **kw,
    }


# Crypto markets — need asset, floor_strike
crypto_markets = [
    make_market("KXBTC-15M-68000", "crypto", 0.52, 15000, 8000, 0.25,
                asset="BTC", floor_strike=68000, series="KXBTC15M", timeframe="15m"),
    make_market("KXBTC-1H-67500", "crypto", 0.62, 25000, 12000, 1.0,
                asset="BTC", floor_strike=67500, series="KXBTC1H", timeframe="1h"),
    make_market("KXETH-15M-3830", "crypto", 0.55, 8000, 4000, 0.25,
                asset="ETH", floor_strike=3830, series="KXETH15M", timeframe="15m"),
    make_market("KXSOL-1H-140", "crypto", 0.38, 5000, 2500, 1.0,
                asset="SOL", floor_strike=140, series="KXSOL1H", timeframe="1h"),
    make_market("KXDOGE-15M-0.170", "crypto", 0.62, 3000, 1500, 0.25,
                asset="DOGE", floor_strike=0.170, series="KXDOGE15M", timeframe="15m"),
]

# Econ markets — need series with ECON_SERIES keyword, floor_strike
econ_markets = [
    make_market("KXCPI-APR-3.5", "econ", 0.35, 50000, 30000, 48,
                series="KXCPI-APR", floor_strike=3.5),
    make_market("KXNFP-APR-200K", "econ", 0.60, 40000, 25000, 72,
                series="KXNFP-APR", floor_strike=200000),
    make_market("KXGDP-Q1-2.5", "econ", 0.45, 30000, 18000, 120,
                series="KXGDP-Q1", floor_strike=2.5),
]

# Fed Watch markets — need series with FOMC/FED
fed_markets = [
    make_market("KXFOMC-MAY-HOLD", "econ", 0.72, 80000, 50000, 168,
                series="KXFOMC-MAY", outcome="hold"),
]

# Political markets — need series with polling data
political_markets = [
    make_market("KXEPL-SENATE-DEM", "political", 0.15, 200000, 120000, 2400,
                series="KXEPL-SENATE-DEM"),
]

# Weather markets — need series, location fields
weather_markets = [
    make_market("KXWEATHER-NYC-85", "weather", 0.22, 5000, 2000, 24,
                series="KXWEATHER-NYC", floor_strike=85, location="NYC"),
]

# Jackpot candidates
jackpot_markets = [
    make_market("KXBTC-100K-24H", "crypto", 0.08, 200000, 80000, 24,
                asset="BTC", floor_strike=100000, series="KXBTC24H"),
    make_market("KXSOL-FLIP-ETH", "crypto", 0.12, 50000, 25000, 720,
                asset="SOL", floor_strike=5000, series="KXSOL720H"),
]

all_markets = crypto_markets + econ_markets + fed_markets + political_markets + weather_markets + jackpot_markets

# Context — needs momentum dict with asset sub-dicts, econ_consensus, fedwatch, polls
context = {
    "momentum": {
        "BTC": {"current": 68100, "mom_5m": 0.0044, "mom_15m": 0.0089, "realized_vol": 0.0035,
                "sma_20": 67500, "rsi": 62},
        "ETH": {"current": 3845, "mom_5m": 0.0065, "mom_15m": 0.012, "realized_vol": 0.0045,
                "sma_20": 3780, "rsi": 58},
        "SOL": {"current": 145, "mom_5m": 0.021, "mom_15m": 0.051, "realized_vol": 0.008,
                "sma_20": 138, "rsi": 71},
        "DOGE": {"current": 0.172, "mom_5m": 0.030, "mom_15m": 0.077, "realized_vol": 0.012,
                 "sma_20": 0.158, "rsi": 68},
    },
    "econ_consensus": {
        "CPI": {"estimate": 3.2, "std": 0.15},
        "NFP": {"estimate": 185000, "std": 25000},
        "GDP": {"estimate": 2.3, "std": 0.4},
    },
    "fedwatch": {
        "hold": 0.78, "cut_25": 0.18, "cut_50": 0.04,
    },
    "polls": {
        "SENATE-DEM": {"poll_avg": 47.8, "margin_of_error": 3.2, "n_polls": 12},
    },
    "weather": {
        "NYC": {"noaa_high": 87, "noaa_low": 78, "noaa_mean": 82, "precip_pct": 20},
    },
    "time_utc": datetime.now(timezone.utc).isoformat(),
    "vix": 18.5,
    "dxy": 104.2,
}


# ── 1. Score all markets with strategy library ──

print(f"\n{'─'*65}")
print(f"  PHASE 1: Strategy Library Scoring ({len(all_markets)} markets)")
print(f"{'─'*65}")

all_scores = []
for m in all_markets:
    scores = score_market(m, context)
    if scores:
        all_scores.extend(scores)
        best = max(scores, key=lambda s: abs(s["edge_pct"]))
        print(f"  {m['ticker']:25s} │ {len(scores)} strategies │ "
              f"best: {best['strategy']:20s} edge={best['edge_pct']:+.1%} "
              f"side={best['side']:3s} conf={best['confidence']:.2f}")
    else:
        print(f"  {m['ticker']:25s} │ 0 strategies firing")

print(f"\n  Total signals: {len(all_scores)} across {len(all_markets)} markets")


# ── 2. Conviction engine ──

print(f"\n{'─'*65}")
print(f"  PHASE 2: Conviction Engine Analysis")
print(f"{'─'*65}")

convictions = []
for m in all_markets:
    r = analyze_conviction(m, context)
    if r:
        convictions.append(r)
        emoji = "🔒" if r.level.name == "LOCK" else ("💪" if r.level.name == "STRONG" else ("📡" if r.level.name == "SIGNAL" else "👁"))
        jp = " 🎰 JACKPOT" if r.is_jackpot else ""
        print(f"  {emoji} {r.ticker:25s} │ {r.level.name:7s} │ "
              f"{r.independent_groups} indep groups │ edge={r.avg_edge_pct:+.1%} │ "
              f"EV/$ = {r.ev_per_dollar:+.3f}{jp}")

if not convictions:
    print("  No conviction signals detected (strategies may not be applicable to synthetic data)")


# ── 3. Find LOCKs, JACKPOTS, best value ──

print(f"\n{'─'*65}")
print(f"  PHASE 3: Advanced Filters")
print(f"{'─'*65}")

locks = find_locks(all_markets, context)
jackpots = find_jackpots(all_markets, context)
best = find_best_value(all_markets, context, top_n=10)

# scan_all_tiers is async + fetches from live Kalshi API — skip for synthetic data
# Build equivalent tier breakdown manually
from research.conviction_engine import ConvictionLevel
tier_labels = {"locks": [], "strong": [], "signals": [], "noise": []}
for m in all_markets:
    r = analyze_conviction(m, context)
    if r:
        if r.level >= ConvictionLevel.LOCK:
            tier_labels["locks"].append(r)
        elif r.level >= ConvictionLevel.STRONG:
            tier_labels["strong"].append(r)
        elif r.level >= ConvictionLevel.SIGNAL:
            tier_labels["signals"].append(r)
        else:
            tier_labels["noise"].append(r)

print(f"  LOCKs found:    {len(locks)}")
print(f"  JACKPOTs found: {len(jackpots)}")
print(f"  Best value:     {len(best)}")
print(f"  Tier breakdown: {json.dumps({k: len(v) for k, v in tier_labels.items()})}")

for lock in locks:
    print(f"    LOCK: {lock.ticker} │ {lock.independent_groups} groups │ "
          f"edge={lock.avg_edge_pct:+.1%} │ strategies={lock.strategies}")

for jp in jackpots:
    print(f"    JACKPOT: {jp.ticker} │ payout={jp.expected_payout:.1f}x │ "
          f"EV/$ = {jp.ev_per_dollar:+.3f} │ our_prob={jp.avg_our_prob:.1%}")


# ── 4. Learning Tracker simulation ──

print(f"\n{'─'*65}")
print(f"  PHASE 4: Learning Tracker — Simulated Bet History")
print(f"{'─'*65}")

# Simulate 100 resolved bets via the learning tracker module
strategies_list = ["crypto_momentum", "crypto_vol_misprice", "timedecay_exploit",
              "mean_reversion_fade", "econ_consensus", "fedwatch_arb",
              "polling_arb", "weather_forecast", "volume_breakout", "calendar_effect"]

win_rates = {
    "crypto_momentum": 0.58, "crypto_vol_misprice": 0.62,
    "timedecay_exploit": 0.72, "mean_reversion_fade": 0.55,
    "econ_consensus": 0.65, "fedwatch_arb": 0.70,
    "polling_arb": 0.52, "weather_forecast": 0.60,
    "volume_breakout": 0.56, "calendar_effect": 0.48,
}

bet_results = []  # track locally since module writes to SQLite
for i in range(100):
    strat = random.choice(strategies_list)
    won = random.random() < win_rates[strat]
    edge = random.uniform(0.04, 0.15)
    payout = random.uniform(1.5, 4.0)
    stake = random.uniform(50, 500)
    pnl = stake * (payout - 1) if won else -stake
    price_cents = int(random.uniform(20, 80))

    try:
        bid = learning_tracker.record_bet(
            ticker=f"SIM-{i}",
            series=f"SIM-SERIES",
            market_type="crypto_15m",
            timeframe="15min",
            asset=strat.split('_')[0].upper(),
            side="yes",
            price_cents=price_cents,
            contracts=max(1, int(stake / price_cents)),
            spend_usd=round(stake, 2),
            our_prob=round(edge + price_cents/100, 3),
            market_prob=round(price_cents/100, 3),
            edge_pct=round(edge, 4),
            strategy=strat,
            signals={"sim": True},
        )
        if bid:
            learning_tracker.record_outcome(bid, won, round(pnl, 2))
    except Exception:
        pass

    bet_results.append({"strategy": strat, "won": won, "pnl": round(pnl, 2), "stake": round(stake, 2)})

# Get weights and aggregate performance locally
try:
    weights = learning_tracker.get_strategy_weights()
except Exception:
    weights = {}

# Build performance summary from our local data
from collections import defaultdict
strat_stats = defaultdict(lambda: {"bets": 0, "wins": 0, "total_pnl": 0.0})
for b in bet_results:
    s = strat_stats[b["strategy"]]
    s["bets"] += 1
    s["wins"] += 1 if b["won"] else 0
    s["total_pnl"] += b["pnl"]

perf = [{"strategy": k, **v, "win_rate": v["wins"]/v["bets"] if v["bets"] else 0} for k, v in strat_stats.items()]

print(f"\n  Strategy Performance (100 simulated bets):")
print(f"  {'Strategy':25s} │ {'Bets':>5s} │ {'Win%':>6s} │ {'PnL':>10s} │ {'Weight':>7s}")
print(f"  {'─'*25}─┼──{'─'*4}─┼──{'─'*5}─┼──{'─'*9}─┼──{'─'*6}")
for s in perf:
    w = weights.get(s["strategy"], 1.0)
    print(f"  {s['strategy']:25s} │ {s['bets']:5d} │ {s['win_rate']:5.1%} │ "
          f"${s['total_pnl']:>9.2f} │ {w:7.3f}")

total_pnl = sum(s["total_pnl"] for s in perf)
total_bets = sum(s["bets"] for s in perf)
total_wins = sum(s["wins"] for s in perf)
print(f"\n  TOTAL: {total_bets} bets, {total_wins} wins ({total_wins/total_bets:.1%}), PnL: ${total_pnl:+,.2f}")


# ── 5. Re-run conviction with learned weights ──

print(f"\n{'─'*65}")
print(f"  PHASE 5: Conviction Re-Scoring with Learned Weights")
print(f"{'─'*65}")

weighted_convictions = []
for m in all_markets:
    r = analyze_conviction(m, context, weights=weights)
    if r:
        weighted_convictions.append(r)

print(f"  Without weights: {len(convictions)} signals")
print(f"  With weights:    {len(weighted_convictions)} signals")

for r in weighted_convictions:
    print(f"    {r.ticker:25s} │ {r.level.name:7s} │ EV/$ = {r.ev_per_dollar:+.3f}")


print(f"\n{'='*65}")
print(f"  INTELLIGENCE PIPELINE COMPLETE")
print(f"  Markets scanned: {len(all_markets)}")
print(f"  Signals generated: {len(all_scores)}")
print(f"  Conviction plays: {len(convictions)}")
print(f"  Learning history: {total_bets} bets simulated")
print(f"{'='*65}")
