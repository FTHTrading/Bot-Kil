"""
run_multi.py — Unified multi-category Kalshi trading bot
========================================================
Scans ALL tradeable Kalshi market categories on each cycle:
  - Crypto 15-min directional (existing V6 model)
  - Sports moneyline (ESPN spread/Log5 model)

Paper-trade mode logs picks to logs/paper_trades_multi.jsonl.
Live mode routes through kalshi_executor.

Usage:
    python scripts/run_multi.py --paper --loop                 # paper-trade loop
    python scripts/run_multi.py --paper --loop --loop-seconds 60
    python scripts/run_multi.py --execute --loop               # live (careful!)
    python scripts/run_multi.py                                # single scan, dry-run
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from data.feeds.kalshi_all_markets import get_all_markets
from data.feeds.btc_momentum import get_momentum_signals
from data.feeds.kalshi import get_balance as kalshi_get_balance
from engine.neural_ev import neural_edge_picks as intraday_edge_picks
from engine.sports_ev import get_espn_games, evaluate_sports_edge, SPORT_MAP


def _print_startup_banner(mode: str):
    """Print GPU + model status once at startup."""
    try:
        from engine.neural_model import gpu_startup_banner, get_model, MODEL_PATH
        gpu_startup_banner()
        m = get_model()
        if m is not None:
            import torch
            dev = next(m.parameters()).device
            print(f"  [startup] Neural model loaded  device={dev}")
        else:
            print(f"  [startup] WARNING: neural model not loaded \u2014 math fallback active")
    except Exception as e:
        print(f"  [startup] model check skipped: {e}")
    print(f"  [startup] Mode: {mode}")

# ── Ledger (persistent bet tracking) ─────────────────────────────────────
_LEDGER_PATH      = _ROOT / "logs" / "bet_ledger_multi.json"
_EXEC_LEDGER_PATH = _ROOT / "logs" / "bet_ledger_exec.json"  # separate for live trading
_LEDGER_TTL  = 60 * 60 * 6   # 6 hours — sports games can last a while
_PAPER_LOG   = _ROOT / "logs" / "paper_trades_multi.jsonl"

# ── Daily loss limit ─────────────────────────────────────────────────────
_DAILY_PNL_PATH = _ROOT / "logs" / "daily_pnl_multi.json"
_DAILY_LOSS_LIMIT = -2.50  # stop at -$2.50


def _event_key(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def _load_ledger(path: Path = None) -> dict:
    p = path or _LEDGER_PATH
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    now = _time_mod.time()
    return {k: v for k, v in data.items() if now - v.get("ts", 0) < _LEDGER_TTL}


def _save_ledger(ledger: dict, path: Path = None):
    p = path or _LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    tmp.replace(p)


def _ledger_allows(ledger: dict, ticker: str) -> bool:
    return _event_key(ticker) not in ledger


def _ledger_record(ledger: dict, ticker: str, side: str, amount: float):
    key = _event_key(ticker)
    ledger[key] = {"ticker": ticker, "side": side, "amount": amount, "ts": _time_mod.time()}


def _load_daily_pnl() -> dict:
    if not _DAILY_PNL_PATH.exists():
        return {"date": "", "spent": 0.0, "trades": 0}
    try:
        data = json.loads(_DAILY_PNL_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"date": "", "spent": 0.0, "trades": 0}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("date") != today:
        return {"date": today, "spent": 0.0, "trades": 0}
    return data


def _save_daily_pnl(pnl: dict):
    pnl.setdefault("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    _DAILY_PNL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DAILY_PNL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(pnl, indent=2), encoding="utf-8")
    tmp.replace(_DAILY_PNL_PATH)


# ── Display ───────────────────────────────────────────────────────────────
def _print_header():
    now = datetime.now()
    print()
    print("=" * 72)
    print("    KALISHI EDGE — Multi-Category Scanner")
    print(f"    {now.strftime('%a %b %d %Y  %H:%M:%S')}")
    print(f"    Categories: Crypto 15m + MLB + NBA + NHL + NCAAB")
    print("=" * 72)


def _print_sports_picks(picks: list[dict]):
    if not picks:
        print("  SPORTS: No edge found in current moneyline markets.")
        return
    print(f"\n  SPORTS PICKS ({len(picks)} with edge):")
    print(f"  {'#':<3} {'Ticker':<45} {'Side':<4} {'Price':>6} {'TrueP':>6} {'Edge':>6} {'Stake':>7} {'Source':<20} {'Game'}")
    print("  " + "-" * 120)
    for i, p in enumerate(picks, 1):
        print(f"  {i:<3} {p['ticker']:<45} {p['side']:<4} ${p['price']:.2f}"
              f"  {p['true_prob']:>4.0%}  {p['edge_pct']:>6s}"
              f"  ${p['suggested_stake']:>5.2f}  {p['prob_source']:<20s} {p['espn_game']}")
    print()


def _print_crypto_picks(picks: list[dict]):
    if not picks:
        print("  CRYPTO: No edge in current 15-min window.")
        return
    print(f"\n  CRYPTO 15M PICKS ({len(picks)} with edge):")
    for p in picks:
        meta = p.get("intraday_meta", {})
        print(f"    {meta.get('asset','?'):<5} {p['side'].upper():<4} "
              f"mkt={p['implied_prob']:.0f}% mdl={p['our_prob']:.0f}% "
              f"edge={p['edge_pct']:+.1f}%  stake=${p['recommended_stake']:.2f}  "
              f"{p['minutes_remaining']:.1f}min left")
    print()


# ── Core scan ─────────────────────────────────────────────────────────────
async def scan_once(args, ledger: dict, loop_mode: bool = False, ledger_path: Path = None) -> dict:
    """Run one full multi-category scan. Returns summary dict."""
    if not loop_mode:
        _print_header()

    now_str = datetime.now().strftime("%H:%M:%S")

    # Daily loss check — only applies in live execute mode, not paper
    daily_pnl = _load_daily_pnl()
    if args.execute:
        net = daily_pnl.get("won", 0) - daily_pnl.get("spent", 0)
        if net <= _DAILY_LOSS_LIMIT:
            print(f"  [{now_str}] DAILY LOSS LIMIT — ${net:.2f} (limit ${_DAILY_LOSS_LIMIT:.2f}). Paused.")
            return {"crypto_picks": [], "sports_picks": []}

    # Balance floor — hard stop if balance too low
    if args.execute and getattr(args, "balance_floor", None):
        try:
            cur_bal = await kalshi_get_balance()
            if cur_bal < args.balance_floor:
                print(f"  [{now_str}] BALANCE FLOOR — ${cur_bal:.2f} < ${args.balance_floor:.2f}. Stopped.")
                return {"crypto_picks": [], "sports_picks": []}
        except Exception:
            pass

    # Bankroll
    try:
        bankroll = await kalshi_get_balance()
        if bankroll <= 0:
            bankroll = float(os.getenv("BANKROLL", "10"))
    except Exception:
        bankroll = float(os.getenv("BANKROLL", "10"))

    # Fetch ALL markets + ESPN data concurrently
    print(f"  [{now_str}] Scanning all markets + ESPN data...")

    mkt_data, momentum, *espn_data = await asyncio.gather(
        get_all_markets(),
        get_momentum_signals(),
        *(get_espn_games(sport) for sport in SPORT_MAP.keys()),
        return_exceptions=True,
    )

    if isinstance(mkt_data, Exception):
        print(f"  ERROR fetching markets: {mkt_data}")
        return {"crypto_picks": [], "sports_picks": []}
    if isinstance(momentum, Exception):
        momentum = {}

    crypto_mkts = mkt_data.get("crypto", [])
    sports_mkts = mkt_data.get("sports", [])

    # Flatten ESPN games
    all_espn = []
    for i, sport in enumerate(SPORT_MAP.keys()):
        games = espn_data[i]
        if isinstance(games, list):
            all_espn.extend(games)

    print(f"  [{now_str}] Found {len(crypto_mkts)} crypto + {len(sports_mkts)} sports markets, "
          f"{len(all_espn)} ESPN games")

    # ── CRYPTO edge (existing V6 model) ──
    crypto_picks = []
    if crypto_mkts:
        try:
            crypto_picks = intraday_edge_picks(crypto_mkts, momentum, bankroll)
            # Filter: require real price data (non-zero momentum)
            crypto_picks = [p for p in crypto_picks
                           if not (p["intraday_meta"]["mom_5m_pct"] == 0.0
                                   and p["intraday_meta"]["mom_15m_pct"] == 0.0
                                   and p["intraday_meta"]["gap_pct"] == 0.0)]
            # --wait-minutes: only fire in last N minutes of window
            if getattr(args, "wait_minutes", None):
                crypto_picks = [p for p in crypto_picks
                                if p["minutes_remaining"] <= args.wait_minutes]
            # --min-edge: override threshold
            if getattr(args, "min_edge", None):
                crypto_picks = [p for p in crypto_picks
                                if p["edge_pct"] >= args.min_edge]
            # Recoup mode extra filter: no flat-trend picks (only strong directional)
            if getattr(args, "wait_minutes", None) or getattr(args, "min_edge", None):
                crypto_picks = [p for p in crypto_picks
                                if p["intraday_meta"].get("trend", "flat") != "flat"]
        except Exception as e:
            print(f"  Crypto model error: {e}")

    # ── SPORTS edge (ESPN model) ──
    sports_picks = []
    if sports_mkts and all_espn and not getattr(args, "crypto_only", False):
        try:
            sports_picks = evaluate_sports_edge(sports_mkts, all_espn, bankroll)
            if getattr(args, "min_edge", None):
                sports_picks = [p for p in sports_picks
                                if p["edge"] * 100 >= args.min_edge]
        except Exception as e:
            print(f"  Sports model error: {e}")

    if loop_mode:
        crypto_picks = [p for p in crypto_picks if _ledger_allows(ledger, p["market"])]
        sports_picks = [p for p in sports_picks if _ledger_allows(ledger, p["ticker"])]
    # Display results
    _print_crypto_picks(crypto_picks)
    _print_sports_picks(sports_picks)

    total_picks = len(crypto_picks) + len(sports_picks)
    if total_picks == 0:
        print(f"  [{now_str}] No edge found. Bankroll=${bankroll:.2f}")
    else:
        print(f"  [{now_str}] {total_picks} total pick(s). Bankroll=${bankroll:.2f}")

    # ── Execute / Paper / Dry-run ──
    all_actions = []

    for p in sports_picks:
        action = {
            "type": "sports",
            "ticker": p["ticker"],
            "side": p["side"],
            "price": p["price"],
            "edge": p["edge"],
            "true_prob": p["true_prob"],
            "stake": p["suggested_stake"],
            "prob_source": p["prob_source"],
            "espn_game": p["espn_game"],
            "category": p["category"],
        }
        all_actions.append(action)

    for p in crypto_picks:
        meta = p.get("intraday_meta", {})
        price = p.get("implied_prob", 50) / 100.0
        action = {
            "type": "crypto",
            "ticker": p["market"],
            "side": p["side"].upper(),
            "price": price,
            "edge": p["edge_pct"] / 100.0,
            "true_prob": p["our_prob"] / 100.0,
            "stake": p["recommended_stake"],
            "prob_source": f"neural({meta.get('trend','?')})" if meta.get("used_neural") else f"math({meta.get('trend','?')})",
            "espn_game": f"{meta.get('asset','?')} 15m",
            "category": "CRYPTO_15M",
        }
        all_actions.append(action)

    if all_actions and args.paper:
        _PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
        print(f"\n  PAPER TRADE — logging {len(all_actions)} pick(s)")
        for a in all_actions:
            contracts = max(1, min(5, int(max(a["stake"], 1.0) / max(a["price"], 0.01))))
            cost = round(contracts * a["price"], 2)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": a["type"],
                "ticker": a["ticker"],
                "side": a["side"],
                "price": a["price"],
                "contracts": contracts,
                "cost_usd": cost,
                "edge": round(a["edge"], 3),
                "true_prob": round(a["true_prob"], 3),
                "prob_source": a["prob_source"],
                "game": a["espn_game"],
                "category": a["category"],
                "result": None,
            }
            with open(_PAPER_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            _ledger_record(ledger, a["ticker"], a["side"], cost)
            _save_ledger(ledger, ledger_path)
            daily_pnl["spent"] = daily_pnl.get("spent", 0) + cost
            daily_pnl["trades"] = daily_pnl.get("trades", 0) + 1
            _save_daily_pnl(daily_pnl)
            print(f"    PAPER: {a['ticker']}  {a['side']}@${a['price']:.2f}  "
                  f"x{contracts}  ${cost:.2f}  edge={a['edge']*100:.1f}%  "
                  f"[{a['prob_source']}]  {a['espn_game']}")
        print(f"  Logged to {_PAPER_LOG}")

    elif all_actions and args.execute:
        print(f"\n  LIVE EXECUTION — {len(all_actions)} order(s)")
        max_c = getattr(args, "max_contracts", None) or 5
        from agents.kalshi_executor import _execute_crypto_pick
        for a in all_actions:
            pick = {
                "market": a["ticker"],
                "side": a["side"].lower(),
                "edge_pct": a["edge"] * 100,
                "our_prob": a["true_prob"] * 100,
                "recommended_stake": a["stake"],
                "crypto_meta": {"side": a["side"], "asset": a["category"]},
                "_max_contracts_override": max_c,
            }
            result = await _execute_crypto_pick(pick, bankroll, dry_run=False)

            status = result.get("status", "?")
            if status in ("PLACED", "ok"):
                _ledger_record(ledger, a["ticker"], a["side"],
                              result.get("spend_usd", 0))
                _save_ledger(ledger, ledger_path)
                daily_pnl["spent"] = daily_pnl.get("spent", 0) + result.get("spend_usd", 0)
                daily_pnl["trades"] = daily_pnl.get("trades", 0) + 1
                _save_daily_pnl(daily_pnl)
            print(f"    {status}: {a['ticker']}  {a['side']}  ${result.get('spend_usd',0):.2f}  {result.get('reason', '')}")

    elif all_actions:
        print(f"\n  DRY-RUN — {len(all_actions)} pick(s) found. Add --paper or --execute.")

    return {"crypto_picks": crypto_picks, "sports_picks": sports_picks}


# ── Loop ──────────────────────────────────────────────────────────────────
async def _loop(args):
    interval = args.loop_seconds
    ledger_path = _EXEC_LEDGER_PATH if args.execute else _LEDGER_PATH
    ledger = _load_ledger(ledger_path)
    _print_header()
    mode = "PAPER" if args.paper else ("LIVE" if args.execute else "DRY-RUN")
    _print_startup_banner(mode)
    print(f"  LOOP MODE [{mode}] — scanning every {interval}s. Ctrl+C to stop.\n")

    while True:
        try:
            ledger = _load_ledger(ledger_path)
            await scan_once(args, ledger, loop_mode=True, ledger_path=ledger_path)
        except KeyboardInterrupt:
            print("\n  Loop stopped.")
            break
        except Exception as e:
            print(f"  [loop error] {type(e).__name__}: {e}")
        print(f"\n  Sleeping {interval}s... (Ctrl+C to stop)")
        await asyncio.sleep(interval)


def run():
    parser = argparse.ArgumentParser(description="Multi-category Kalshi trading bot")
    parser.add_argument("--execute",       action="store_true", help="Place real orders")
    parser.add_argument("--paper",         action="store_true", help="Paper-trade mode")
    parser.add_argument("--loop",          action="store_true", help="Continuous polling loop")
    parser.add_argument("--loop-seconds",  type=int,   default=60,   help="Seconds between polls (default 60)")
    # Recoup / precision mode flags
    parser.add_argument("--wait-minutes",  type=float, default=None, help="Only fire when ≤N min remain in window (e.g. 3.0)")
    parser.add_argument("--min-edge",      type=float, default=None, help="Override min edge %% (e.g. 15)")
    parser.add_argument("--max-contracts", type=int,   default=None, help="Cap contracts per order (e.g. 2)")
    parser.add_argument("--balance-floor", type=float, default=None, help="Hard stop if balance falls below this (e.g. 5.50)")
    parser.add_argument("--crypto-only",   action="store_true",      help="Skip sports picks (crypto 15m only)")
    args = parser.parse_args()

    if args.loop:
        asyncio.run(_loop(args))
    else:
        ledger_path = _EXEC_LEDGER_PATH if args.execute else _LEDGER_PATH
        asyncio.run(scan_once(args, _load_ledger(ledger_path), ledger_path=ledger_path))


if __name__ == "__main__":
    run()
