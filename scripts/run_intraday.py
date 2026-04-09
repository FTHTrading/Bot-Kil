"""
run_intraday.py — Find and execute edge bets on current 15-minute crypto markets
=================================================================================
Run at ANY time to analyse the current 15-min window for each crypto asset.
New windows open every 15 minutes on the clock (11:00, 11:15, 11:30, etc.)

Usage:
    python scripts/run_intraday.py              # dry-run, all assets
    python scripts/run_intraday.py --execute    # place real orders
    python scripts/run_intraday.py --asset BTC  # single asset

Algorithm:
    - Position signal: current price vs floor_strike (opening BRTI reference)
    - Momentum signal: 5-min & 15-min Binance candle momentum
    - Blended by time remaining (momentum-heavy early, position-heavy late)
    - Min edge: 4%  |  Kelly: 10%  |  Max $150/trade
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time as _time_mod
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 output on Windows (cp1252 default can't handle ₿, ≥, → etc.)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from data.feeds.kalshi_intraday import get_intraday_markets
from data.feeds.btc_momentum import get_momentum_signals
from data.feeds.kalshi import get_balance as kalshi_get_balance
from engine.intraday_ev import intraday_edge_picks, _MIN_EDGE
from engine.tracker import record_pick, record_execution, record_signal_snapshot


# ---------------------------------------------------------------------------
# Persistent bet ledger — survives process restarts (PS1 auto-restart loop)
# Tracks by EVENT (asset+window), not by ticker, to prevent betting multiple
# strikes on the same event. Also locks side to prevent YES→NO hedging.
# ---------------------------------------------------------------------------
_LEDGER_PATH = _PROJECT_ROOT / "logs" / "bet_ledger.json"
_LEDGER_TTL  = 25 * 60   # 25 min — covers any 15-min window + safe buffer

# Max bets per poll — keep positions concentrated on small bankrolls
_MAX_PICKS_PER_POLL = 2

# Daily loss limit — stop trading if cumulative daily P&L exceeds this
_DAILY_LOSS_LIMIT = -2.50   # USD — stop at -$2.50 for small accounts
_DAILY_LOSS_PATH  = _PROJECT_ROOT / "logs" / "daily_pnl.json"

# Pick cache for wait-mode — remember good picks so they survive model re-runs
_PICK_CACHE: dict[str, dict] = {}   # ticker → pick dict (in-memory, resets on restart)


def _event_key(ticker: str) -> str:
    """Strip strike suffix → event-level key (e.g. KXBTC15M-26APR061230)."""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def _load_ledger() -> dict:
    """Load persistent bet ledger, pruning expired entries."""
    if not _LEDGER_PATH.exists():
        return {}
    try:
        data = json.loads(_LEDGER_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    now = _time_mod.time()
    return {k: v for k, v in data.items() if now - v.get("ts", 0) < _LEDGER_TTL}


def _save_ledger(ledger: dict):
    """Atomically persist the ledger."""
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    tmp.replace(_LEDGER_PATH)


def _ledger_allows(ledger: dict, ticker: str, side: str) -> bool:
    """Return True if the ledger permits this bet (no dup event, no side-flip)."""
    key = _event_key(ticker)
    return key not in ledger   # one bet per event, period


def _ledger_record(ledger: dict, ticker: str, side: str, amount: float):
    """Record a placed bet in the ledger."""
    key = _event_key(ticker)
    ledger[key] = {"ticker": ticker, "side": side, "amount": amount, "ts": _time_mod.time()}


# ---------------------------------------------------------------------------
# Daily P&L tracking — hard stop-loss per day
# ---------------------------------------------------------------------------
def _load_daily_pnl() -> dict:
    """Load daily P&L record. Resets at midnight UTC."""
    if not _DAILY_LOSS_PATH.exists():
        return {"date": "", "spent": 0.0, "won": 0.0, "trades": 0}
    try:
        data = json.loads(_DAILY_LOSS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"date": "", "spent": 0.0, "won": 0.0, "trades": 0}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("date") != today:
        return {"date": today, "spent": 0.0, "won": 0.0, "trades": 0}
    return data


def _save_daily_pnl(pnl: dict):
    """Persist daily P&L."""
    pnl.setdefault("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    _DAILY_LOSS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DAILY_LOSS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(pnl, indent=2), encoding="utf-8")
    tmp.replace(_DAILY_LOSS_PATH)


def _daily_loss_ok(pnl: dict) -> bool:
    """Return True if we haven't hit the daily loss limit."""
    net = pnl.get("won", 0.0) - pnl.get("spent", 0.0)
    return net > _DAILY_LOSS_LIMIT


def _record_spend(pnl: dict, amount: float):
    """Record a trade spend in daily P&L."""
    pnl["spent"] = pnl.get("spent", 0.0) + amount
    pnl["trades"] = pnl.get("trades", 0) + 1
    _save_daily_pnl(pnl)


# ---------------------------------------------------------------------------
# Executor integration — reuses existing kalshi_executor infrastructure
# ---------------------------------------------------------------------------
async def _execute_intraday_pick(pick: dict, bankroll: float = None, dry_run: bool = True) -> dict:
    """Route an intraday pick through the Kalshi crypto executor."""
    from agents.kalshi_executor import _execute_crypto_pick
    if bankroll is None:
        bankroll = float(os.getenv("BANKROLL", "10000"))
    # _execute_crypto_pick reads side/asset from crypto_meta — bridge intraday pick format
    intraday_meta = pick.get("intraday_meta", {})
    if "crypto_meta" not in pick:
        pick = dict(pick)  # copy — don't mutate caller's dict
        pick["crypto_meta"] = {
            "side":  pick.get("side", "yes").upper(),
            "asset": intraday_meta.get("asset", "CRYPTO"),
        }
    return await _execute_crypto_pick(pick, bankroll, dry_run)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

_ASSET_EMOJI = {
    "BTC":  "₿",
    "ETH":  "Ξ",
    "SOL":  "◎",
    "DOGE": "Ð",
    "XRP":  "✕",
    "BNB":  "B",
}

_BARS = "█" * 10

def _edge_bar(edge: float) -> str:
    filled = max(1, min(10, int(edge / 2)))
    return "█" * filled + "░" * (10 - filled)


def _print_header():
    now = datetime.now()
    print()
    print("=" * 68)
    print("    KALISHI EDGE — 15-Min Intraday Crypto Picks")
    print(f"    {now.strftime('%a %b %d %Y  %H:%M:%S')}    Algo: Momentum + Position Blend")
    print("=" * 68)


def _print_momentum_table(momentum: dict, assets: list[str]):
    print()
    print("  LIVE MOMENTUM SIGNALS")
    print(f"  {'Asset':<6}  {'Price':>13}  {'5-min':>8}  {'15-min':>8}  {'Trend':<6}  {'Vol(5m)':>8}")
    print("  " + "-" * 58)
    for asset in assets:
        s = momentum.get(asset, {})
        if not s:
            print(f"  {asset:<6}  {'(no data)':>13}")
            continue
        emoji = _ASSET_EMOJI.get(asset, " ")
        mom5  = s.get("mom_5m", 0.0) * 100
        mom15 = s.get("mom_15m", 0.0) * 100
        vol   = s.get("realized_vol", 0.0) * 100
        trend = s.get("trend", "flat")
        print(
            f"  {emoji}{asset:<5}  {s['current']:>13,.4f}  "
            f"{mom5:>+7.3f}%  {mom15:>+7.3f}%  {trend:<6}  {vol:>6.4f}%"
        )
    print()


def _print_picks_table(picks: list[dict]):
    if not picks:
        print("  No edge found in current 15-min window.")
        print("  (min edge 4% — markets may be efficiently priced or no open window)")
        return

    print(f"  {'#':<3}  {'Asset':<5}  {'Side':<4}  "
          f"{'Mkt%':>5}  {'Mdl%':>5}  {'Edge':>6}  {'EV':>7}  "
          f"{'Stake':>6}  {'Min left':>8}  {'Signals'}")
    print("  " + "-" * 95)
    for i, p in enumerate(picks, 1):
        meta = p.get("intraday_meta", {})
        print(
            f"  {i:<3}  {meta.get('asset','?'):<5}  {p['side'].upper():<4}  "
            f"{p['implied_prob']:>4.0f}%  {p['our_prob']:>4.0f}%  "
            f"{p['edge_pct']:>+5.1f}%  {p['ev_pct']:>+6.1f}%  "
            f"${p['recommended_stake']:>5.0f}  "
            f"  {p['minutes_remaining']:>5.1f}m  "
            f"gap={meta.get('gap_pct',0):+.3f}%  5m={meta.get('mom_5m_pct',0):+.3f}%  trend={meta.get('trend','?')}"
        )
    print()
    total_stake = sum(p["recommended_stake"] for p in picks)
    print(f"  Total suggested stake: ${total_stake:.2f}  |  {len(picks)} trade(s)")
    print()


def _print_execution_results(results: list[dict]):
    print("  EXECUTION RESULTS:")
    for r in results:
        status = r.get("status", "?")
        ticker = r.get("market_ticker") or r.get("ticker", "?")
        reason = r.get("reason") or r.get("note", "")
        price  = r.get("price_cents", "?")
        qty    = r.get("contracts") or r.get("quantity", "?")
        spend  = r.get("spend_usd") or r.get("estimated_spend") or 0
        side   = r.get("side", "?").upper()
        if status == "PLACED":
            order_id = r.get("order_id", "")
            print(f"    ✓ PLACED  {ticker}  {side}@{price}c  x{qty}  ${spend:.2f}  order={order_id}")
        elif status == "DRY_RUN":
            print(f"    DRY-RUN  {ticker}  {side}@{price}c  x{qty}  ${spend:.2f}")
        elif status in ("ok",):
            print(f"    OK  {ticker}  {side}@{price}c  x{qty}  ${spend:.2f}")
        else:
            print(f"    SKIP [{status}]  {ticker}  — {reason}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace, loop_mode: bool = False, ledger: dict = None):
    if ledger is None:
        ledger = _load_ledger()
    if not loop_mode:
        _print_header()

    assets_filter = [a.upper() for a in args.asset] if args.asset else None

    # 1. Fetch market data and momentum signals in parallel
    print("  Fetching markets + momentum signals…")
    markets_task  = get_intraday_markets()
    momentum_task = get_momentum_signals(assets_filter)
    markets, momentum = await asyncio.gather(markets_task, momentum_task)

    # Filter by asset if requested
    if assets_filter:
        markets = [m for m in markets if m["asset"] in assets_filter]

    # 2. Print momentum table
    active_assets = sorted({m["asset"] for m in markets}) or (assets_filter or list(momentum.keys()))
    _print_momentum_table(momentum, active_assets)

    # 3. Run model — use live Kalshi balance as bankroll (dynamic)
    if args.bankroll:
        bankroll = args.bankroll
    else:
        try:
            bankroll = await kalshi_get_balance()
            if bankroll <= 0:
                bankroll = float(os.getenv("BANKROLL", "10"))
        except Exception:
            bankroll = float(os.getenv("BANKROLL", "10"))
    min_edge = float(os.getenv("MIN_EDGE_INTRADAY", str(_MIN_EDGE)))

    # Log signal snapshot (after bankroll is available)
    record_signal_snapshot(momentum, markets, bankroll)
    picks_raw = intraday_edge_picks(markets, momentum, bankroll, min_edge)

    # ── SMART FILTERS ──────────────────────────────────────────────────────
    # 1. Remove any pick where we have NO real price data
    # 2. In --wait mode: skip picks with > max_wait minutes remaining
    # 3. Check daily loss limit before allowing execution
    # 4. Cache good picks from wait-mode so they survive model re-runs
    market_map = {m["ticker"]: m for m in markets}

    # Daily loss check
    daily_pnl = _load_daily_pnl()
    if not _daily_loss_ok(daily_pnl):
        net = daily_pnl.get("won", 0) - daily_pnl.get("spent", 0)
        print(f"  DAILY LOSS LIMIT HIT — net P&L today: ${net:.2f} (limit: ${_DAILY_LOSS_LIMIT:.2f})")
        print(f"  Trading paused until tomorrow. {daily_pnl.get('trades', 0)} trades today.")
        return

    picks = []
    waiting = []
    rejected_no_data = []
    for p in picks_raw:
        meta = p["intraday_meta"]
        has_price_data = not (meta["mom_5m_pct"] == 0.0 and meta["mom_15m_pct"] == 0.0
                              and meta["gap_pct"] == 0.0)
        t_min = p["minutes_remaining"]

        # Never bet without real price data
        if not has_price_data:
            rejected_no_data.append(p)
            continue

        if args.wait and t_min > args.wait_minutes:
            # Cache this pick for later — store the pick with its edge/confidence
            _PICK_CACHE[p["market"]] = p
            waiting.append(p)
            record_pick(p, momentum, bankroll, verdict="WAIT")
            continue

        # In loop mode: skip events we already bet on (side-locked, persistent)
        if loop_mode and not _ledger_allows(ledger, p["market"], p["side"]):
            continue

        picks.append(p)

    # Recover cached picks that are now in the firing window
    # If a ticker was cached from a previous poll and the current model
    # didn't produce it (e.g. market adjusted), use the cached version
    # as long as it's still in the time window.
    if args.wait and loop_mode:
        active_tickers = {p["market"] for p in picks}
        for cache_ticker, cached_pick in list(_PICK_CACHE.items()):
            if cache_ticker in active_tickers:
                continue  # already in picks
            # Check if this market is still open and in firing window
            if cache_ticker in market_map:
                live_m = market_map[cache_ticker]
                t_remaining = live_m["minutes_remaining"]
                if t_remaining <= args.wait_minutes and t_remaining > 0.3:
                    # Re-validate V6 trend gate against CURRENT momentum
                    cached_asset = cached_pick.get("intraday_meta", {}).get("asset", "?")
                    current_trend = momentum.get(cached_asset, {}).get("trend", "flat")
                    cached_side = cached_pick.get("side", "").lower()
                    if current_trend == "up" and cached_side == "no":
                        del _PICK_CACHE[cache_ticker]
                        print(f"  CACHE STALE: {cache_ticker} — trend now {current_trend}, {cached_side.upper()} blocked")
                        continue
                    if current_trend == "down" and cached_side == "yes":
                        del _PICK_CACHE[cache_ticker]
                        print(f"  CACHE STALE: {cache_ticker} — trend now {current_trend}, {cached_side.upper()} blocked")
                        continue
                    # Update time in cached pick
                    cached_pick = dict(cached_pick)
                    cached_pick["minutes_remaining"] = round(t_remaining, 1)
                    if _ledger_allows(ledger, cache_ticker, cached_pick["side"]):
                        picks.append(cached_pick)
                        record_pick(cached_pick, momentum, bankroll, verdict="CACHE_HIT")
                        print(f"  CACHE HIT: {cache_ticker} — firing cached pick from earlier scan")
            else:
                # Market no longer listed — expired or settled; purge cache
                del _PICK_CACHE[cache_ticker]

    if rejected_no_data:
        print(f"  Skipped {len(rejected_no_data)} pick(s) — no live price data (would be betting blind)")
    if waiting:
        import math
        max_rem = max(p['minutes_remaining'] for p in waiting)
        print(f"  WAIT MODE: {len(waiting)} pick(s) held — re-run in ~{math.ceil(max_rem - args.wait_minutes + 0.5):.0f} min when ≤{args.wait_minutes:.0f} min remain")
        for p in waiting:
            meta = p["intraday_meta"]
            m = market_map.get(p["market"], {})
            price_cents = round(m.get("yes_ask", 0.5) * 100) if p["side"] == "yes" else round(m.get("no_ask", 0.5) * 100)
            conf = meta.get("confidence", "?")
            print(f"    WAITING: {p['market']}  {p['side'].upper()}@{price_cents}c  edge={p['edge_pct']:+.1f}%  {p['minutes_remaining']:.1f}min left  gap={meta['gap_pct']:+.3f}%  conf={conf}")
        print()
    # end smart filters

    # 4. Print picks
    if not loop_mode or picks or waiting:
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"  [{now_str}] PICKS (min edge {min_edge*100:.0f}% | 15-min window | {len(markets)} markets scanned | bankroll=${bankroll:.2f})")
        print()
        _print_picks_table(picks)

    if not picks:
        if not loop_mode:
            print("  Tip: Re-run at the start of the next 15-min window for fresh opportunities.")
        return

    # 5. Paper-trade mode — log picks as if we traded, verify at settlement
    if args.paper and picks:
        _paper_log = _PROJECT_ROOT / "logs" / "paper_trades.jsonl"
        _paper_log.parent.mkdir(parents=True, exist_ok=True)
        market_map_exec = {m["ticker"]: m for m in markets}
        print(f"  📝 PAPER TRADE — logging {len(picks)} pick(s) (not executing)")
        for p in picks:
            m = market_map_exec.get(p["market"], {})
            price_cents = round(m.get("yes_ask", 0.5) * 100) if p["side"] == "yes" else round(m.get("no_ask", 0.5) * 100)
            contracts = max(1, min(5, int(max(p["recommended_stake"], 1.0) / max(price_cents / 100, 0.01))))
            cost = contracts * price_cents / 100
            paper_entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "ticker": p["market"],
                "asset": p.get("intraday_meta", {}).get("asset", "?"),
                "side": p["side"],
                "price_cents": price_cents,
                "contracts": contracts,
                "cost_usd": round(cost, 2),
                "edge_pct": p["edge_pct"],
                "model_prob": p["our_prob"],
                "market_prob": p["implied_prob"],
                "confidence": p.get("intraday_meta", {}).get("confidence", 0),
                "minutes_remaining": p["minutes_remaining"],
                "momentum_5m": p.get("intraday_meta", {}).get("mom_5m_pct", 0),
                "trend": p.get("intraday_meta", {}).get("trend", "?"),
                "gap_pct": p.get("intraday_meta", {}).get("gap_pct", 0),
                "result": None,  # filled by settle_sync later
            }
            with open(_paper_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(paper_entry) + "\n")
            record_pick(p, momentum, bankroll, verdict="PAPER")
            _ledger_record(ledger, p["market"], p["side"], cost)
            _save_ledger(ledger)
            print(f"    PAPER: {p['market']}  {p['side'].upper()}@{price_cents}c  x{contracts}  ${cost:.2f}  edge={p['edge_pct']:+.1f}%")
        print(f"  Logged to {_paper_log}")
        return

    # 5b. Dry-run gate
    if not args.execute:
        print("  DRY-RUN — no orders placed. Add --execute to trade, or --paper to paper-trade.")
        print()
        print("  Simulated orders:")
        # Build a ticker → market lookup
        market_map = {m["ticker"]: m for m in markets}
        for p in picks:
            m = market_map.get(p["market"], {})
            price_cents = round(m.get("yes_ask", 0.5) * 100) if p["side"] == "yes" else round(m.get("no_ask", 0.5) * 100)
            contracts = max(1, min(5, int(p["recommended_stake"] / max(price_cents / 100, 0.01))))
            cost = contracts * price_cents / 100
            print(f"    Would place: {p['market']}  {p['side'].upper()}@{price_cents}c  x{contracts}  ${cost:.2f}")
        return

    # 6. Execute — V6 hard safety gates (prevent rogue execution)
    _V6_MIN_PRICE = 0.10   # never bet below 10¢
    _V6_MAX_CONTRACTS = 5  # never more than 5 contracts per trade
    safe_picks = []
    for p in picks:
        mkt = {m["ticker"]: m for m in markets}.get(p["market"], {})
        ask = mkt.get("yes_ask", 0.5) if p["side"] == "yes" else mkt.get("no_ask", 0.5)
        if ask < _V6_MIN_PRICE:
            print(f"  [V6 BLOCKED] {p['market']} — price {ask:.2f} < {_V6_MIN_PRICE:.2f} floor")
            continue
        contracts = max(1, min(_V6_MAX_CONTRACTS, int(p["recommended_stake"] / max(ask, 0.01))))
        if contracts > _V6_MAX_CONTRACTS:
            print(f"  [V6 CAPPED] {p['market']} — contracts {contracts} → {_V6_MAX_CONTRACTS}")
            contracts = _V6_MAX_CONTRACTS
        safe_picks.append(p)
    picks = safe_picks
    if not picks:
        print("  All picks blocked by V6 safety gates. Not executing.")
        return

    if bankroll < 50:
        picks = picks[:_MAX_PICKS_PER_POLL]
    print(f"  Placing {len(picks)} real orders…")
    if not args.loop and not args.yes:
        confirm = input("  Type YES to confirm real money orders: ").strip()
        if confirm.upper() != "YES":
            print("  Cancelled.")
            return

    results = []
    for pick in picks:
        record_pick(pick, momentum, bankroll, verdict="FIRE")
        result = await _execute_intraday_pick(pick, bankroll=bankroll, dry_run=False)
        record_execution(pick, result, bankroll)
        results.append(result)
        if result.get("status") in ("PLACED", "ok"):
            _ledger_record(ledger, pick["market"], pick["side"],
                           result.get("spend_usd", 0))
            _save_ledger(ledger)
            _record_spend(daily_pnl, result.get("spend_usd", 0))

    _print_execution_results(results)
    print()
    print("  Done. Orders placed — check kalshi.com for status.")
    print("  Winnings auto-settle at expiry → available in Kalshi balance immediately.")


def run():
    parser = argparse.ArgumentParser(description="15-min intraday crypto picks")
    parser.add_argument("--execute",      action="store_true", help="Place real orders")
    parser.add_argument("--asset",        nargs="*",           help="Filter assets e.g. --asset BTC ETH")
    parser.add_argument("--min-edge",     type=float, default=None, help="Override min edge %% (e.g. 3)")
    parser.add_argument("--bankroll",     type=float, default=None, help="Override bankroll (default: BANKROLL env or 10000)")
    parser.add_argument("--wait",         action="store_true", help="Hold picks until ≤N min remain (max confidence)")
    parser.add_argument("--wait-minutes", type=float, default=3.0, help="Minutes-remaining threshold for --wait (default 3)")
    parser.add_argument("--loop",         action="store_true", help="Poll every N seconds until edge found, then auto-execute")
    parser.add_argument("--loop-seconds", type=int, default=30, help="Seconds between polls in --loop mode (default 30)")
    parser.add_argument("--yes",          action="store_true", help="Skip confirmation prompt for live orders")
    parser.add_argument("--paper",        action="store_true", help="Paper-trade mode: log picks as if real but don't execute. Validates model accuracy.")
    args = parser.parse_args()

    if args.min_edge is not None:
        os.environ["MIN_EDGE_INTRADAY"] = str(args.min_edge / 100)

    if args.loop:
        asyncio.run(_loop(args))
    else:
        asyncio.run(main(args))


async def _loop(args: argparse.Namespace):
    """continuously poll for edge, fire when found (used with --execute for live bets)."""
    interval = args.loop_seconds
    ledger = _load_ledger()   # persistent — survives restarts via file

    print()
    print("  LOOP MODE — scanning every", interval, "seconds. Ctrl+C to stop.")
    print("  Will auto-execute when edge >= threshold." if args.execute else "  Dry-run -- add --execute to place live bets.")
    print(f"  Persistent ledger: {_LEDGER_PATH}  ({len(ledger)} active entries)")
    print()

    while True:
        try:
            # Reload ledger each poll to prune expired entries
            ledger = _load_ledger()
            await main(args, loop_mode=True, ledger=ledger)
        except KeyboardInterrupt:
            print("\n  Loop stopped.")
            break
        except Exception as e:
            print(f"  [loop error] {e}")
        print(f"  Sleeping {interval}s…  (Ctrl+C to stop)")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    run()
