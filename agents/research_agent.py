"""
research_agent.py — Autonomous study, learning, and execution agent
===================================================================
The Research Agent runs continuously and does four things in each cycle:

  STUDY   — scans ALL Kalshi markets through the conviction engine,
              records snapshots, generates a full report.

  LEARN   — auto-settles open bets, updates strategy weights, identifies
              signal correlations over time.

  ACT     — three tiers of execution:
              LOCK    (4+ independent evidence groups, ~85-90% win rate)
              JACKPOT (asymmetric high-payout plays, 5:1+ return)
              SIGNAL  (2+ strategies agree, normal confidence)

  ALERT   — separate 2-minute monitoring loop that checks for LOCK or
              JACKPOT plays between scheduled cycles and fires immediately
              the moment a qualifying opportunity appears. Bets anytime
              the system is sure — not just on the hour.

Win-rate target: 80-90% by only executing LOCK-level plays and only
supplementing with JACKPOT or SIGNAL plays when the system confirms.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from research.market_scanner import scan_all, scan_for_locks, scan_for_jackpots, scan_highest_value, snapshot_market_prices
from research.learning_tracker import (
    get_all_stats, get_strategy_weights, get_best_strategies,
    get_daily_pnl, get_signal_correlations, record_bet, auto_settle_open_bets,
    write_daily_summary,
)

# ─── Config ───────────────────────────────────────────────────────────────────

_INTERVAL_MIN        = int(os.getenv("RESEARCH_INTERVAL_MIN", "60"))
_MIN_EDGE            = float(os.getenv("RESEARCH_MIN_EDGE", "0.05"))
_MIN_CONFIDENCE      = float(os.getenv("RESEARCH_MIN_CONFIDENCE", "0.55"))
_MAX_DAILY_USD       = float(os.getenv("RESEARCH_MAX_DAILY_USD", "3.00"))  # per category
_BANKROLL            = float(os.getenv("RESEARCH_BANKROLL", os.getenv("BANKROLL", "10")))
_EXECUTE             = os.getenv("RESEARCH_EXECUTE", "false").lower() == "true"
_ALERT_INTERVAL_SEC  = int(os.getenv("RESEARCH_ALERT_INTERVAL_SEC", "120"))  # 2-min alert monitor
_REPORT_PATH         = _PROJECT_ROOT / "db" / "research_report.json"
_CONSENSUS_CACHE     = _PROJECT_ROOT / "db" / "econ_consensus.json"

# Per-tier daily limits (LOCK gets larger allocation — we want these)
_TIER_LIMITS = {
    "lock":    _MAX_DAILY_USD * 3.0,   # locks get 3× — most confident bet
    "jackpot": _MAX_DAILY_USD * 1.5,   # jackpots: small bets but high payout
    "signal":  _MAX_DAILY_USD * 1.0,   # regular signals
}

_CATEGORY_LIMITS = {
    "crypto":   _MAX_DAILY_USD * 1.5,
    "econ":     _MAX_DAILY_USD,
    "political":_MAX_DAILY_USD * 0.5,
    "weather":  _MAX_DAILY_USD * 0.5,
    "sports":   _MAX_DAILY_USD,
    "misc":     _MAX_DAILY_USD * 0.3,
}

# Deduplicate tickers we've already bet on this session (resets at midnight)
_session_bets: set[str] = set()
_daily_spent: dict[str, float] = {}
_tier_spent: dict[str, float] = {}
_last_reset_day: str = ""


def _check_reset_daily():
    global _daily_spent, _tier_spent, _session_bets, _last_reset_day
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != _last_reset_day:
        _daily_spent  = {k: 0.0 for k in _CATEGORY_LIMITS}
        _tier_spent   = {k: 0.0 for k in _TIER_LIMITS}
        _session_bets = set()
        _last_reset_day = today


def _can_spend(category: str, tier: str, amount: float) -> bool:
    _check_reset_daily()
    cat_ok  = _daily_spent.get(category, 0.0) + amount <= _CATEGORY_LIMITS.get(category, _MAX_DAILY_USD)
    tier_ok = _tier_spent.get(tier, 0.0)      + amount <= _TIER_LIMITS.get(tier, _MAX_DAILY_USD)
    return cat_ok and tier_ok


def _register_spend(category: str, tier: str, amount: float, ticker: str):
    _check_reset_daily()
    _daily_spent[category] = _daily_spent.get(category, 0.0) + amount
    _tier_spent[tier]      = _tier_spent.get(tier, 0.0)      + amount
    _session_bets.add(ticker)


# ─── Execution helpers ────────────────────────────────────────────────────────

async def _execute_conviction(opp: dict, tier: str, dry_run: bool = True) -> Optional[dict]:
    """
    Execute a bet from the conviction engine's result dict.
    Uses Kelly sizing from conviction_engine.kelly_for_conviction().
    """
    try:
        from research.conviction_engine import ConvictionResult, ConvictionLevel, kelly_for_conviction
        import httpx
        from data.feeds.kalshi_intraday import _headers, _BASE
        from agents.kalshi_executor import MAX_CONTRACTS

        ticker   = opp["ticker"]
        side     = opp["side"]
        mkt_price = float(opp.get("market_price", 0.50))
        price_c  = int(round(mkt_price * 100))
        our_prob = float(opp.get("avg_our_prob", opp.get("our_prob", 0.55)))
        edge_pct = float(opp.get("avg_edge_pct", opp.get("edge_pct", 0.05)))

        # Kelly sizing — use payout and conviction level
        payout = float(opp.get("expected_payout", 1.0 / max(0.01, mkt_price)))
        b = payout - 1.0
        if b <= 0:
            return None

        # Fractional Kelly by tier
        kelly_scales = {"lock": 0.25, "jackpot": 0.10, "signal": 0.12}
        kelly_f = kelly_scales.get(tier, 0.12)
        raw_kelly = (b * our_prob - (1 - our_prob)) / b
        spend = max(1.0, _BANKROLL * max(0.0, raw_kelly) * kelly_f)
        spend = min(spend, _TIER_LIMITS.get(tier, 3.0) * 0.5)  # never more than half daily tier limit
        spend = round(spend, 2)

        contracts = max(1, int(spend * 100 / max(1, price_c)))
        contracts = min(contracts, MAX_CONTRACTS)
        actual_spend = round(contracts * price_c / 100.0, 2)

        order_id = None
        if not dry_run and actual_spend >= 0.50:
            payload = {
                "ticker":           ticker,
                "action":           "buy",
                "type":             "market",
                "side":             side,
                "count":            contracts,
                "client_order_id":  f"{tier[:2]}_{int(time.time())}",
            }
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(
                        f"{_BASE}/portfolio/orders",
                        headers=_headers("POST", "/portfolio/orders"),
                        json=payload,
                    )
                    if resp.status_code in (200, 201):
                        order_id = resp.json().get("order", {}).get("order_id")
                    else:
                        print(f"[ResearchAgent] Order rejected {resp.status_code}: {resp.text[:200]}")
                        return None
            except Exception as e:
                print(f"[ResearchAgent] Order error ({ticker}): {e}")
                return None

        # Record every trade (real or dry-run) in learning tracker
        signals = opp.get("signals", opp.get("all_signals", {}))
        strategy_name = (opp.get("strategies") or [opp.get("strategy", tier)])
        if isinstance(strategy_name, list):
            strategy_name = "+".join(strategy_name[:3])

        bet_id = record_bet(
            ticker      = ticker,
            side        = side,
            price_cents = price_c,
            contracts   = contracts,
            spend_usd   = actual_spend,
            our_prob    = our_prob,
            edge_pct    = edge_pct,
            strategy    = strategy_name,
            signals     = signals,
            order_id    = order_id,
            market_type = opp.get("category", "misc"),
            timeframe   = opp.get("timeframe", "daily"),
            series      = opp.get("series"),
            asset       = opp.get("asset"),
            notes       = f"[{tier.upper()}] {opp.get('reason', opp.get('best_reason', ''))}",
        )
        category = opp.get("category", "misc")
        _register_spend(category, tier, actual_spend, ticker)

        level = opp.get("conviction", tier.upper())
        print(f"[ResearchAgent] {'DRY' if dry_run else 'LIVE'} {level} bet: "
              f"{ticker} {side.upper()} {contracts}×{price_c}¢ = ${actual_spend:.2f} "
              f"| edge={edge_pct*100:.1f}% | strategies={opp.get('strategy_count', '?')}"
              f"{' | ORDER=' + order_id if order_id else ''}")

        return {
            "bet_id":    bet_id,
            "order_id":  order_id,
            "ticker":    ticker,
            "tier":      tier,
            "side":      side,
            "contracts": contracts,
            "spend":     actual_spend,
            "edge_pct":  round(edge_pct * 100, 2),
            "conviction":opp.get("conviction", tier.upper()),
            "dry_run":   dry_run,
        }
    except Exception as e:
        print(f"[ResearchAgent] Execute error for {opp.get('ticker')}: {e}")
        import traceback; traceback.print_exc()
        return None


async def _execute_opportunity(opp: dict, dry_run: bool = True) -> Optional[dict]:
    """Backwards-compatible wrapper — auto-detects tier from opp dict."""
    conv = opp.get("conviction", "SIGNAL").upper()
    tier = "lock" if conv == "LOCK" else ("jackpot" if opp.get("is_jackpot") else "signal")
    return await _execute_conviction(opp, tier=tier, dry_run=dry_run)


# ─── Alert monitor — fires IMMEDIATELY on locks / jackpots ───────────────────

async def alert_monitor(execute: bool = None) -> None:
    """
    Runs every ALERT_INTERVAL_SEC (default: 2 minutes) independent of the
    main hourly study cycle.  The ONLY goal: find LOCK or JACKPOT level
    plays and execute them immediately without waiting for the next study.

    This is what enables "place bets anytime the system is sure".
    """
    if execute is None:
        execute = _EXECUTE
    dry_run = not execute

    print(f"[AlertMonitor] Starting (interval={_ALERT_INTERVAL_SEC}s, execute={execute})")

    while True:
        try:
            await asyncio.sleep(_ALERT_INTERVAL_SEC)

            # Scan for locks first (fastest path to high-confidence bets)
            locks    = await scan_for_locks()
            jackpots = await scan_for_jackpots()

            fired = []

            # ─── Execute LOCK plays ───────────────────────────────────────
            for lock in locks:
                ticker = lock["ticker"]
                if ticker in _session_bets:
                    continue  # already bet on this today
                est_spend = max(1.0, _BANKROLL * float(lock.get("avg_edge_pct", 0.05)) * 0.25)
                category  = lock.get("category", "misc")
                if not _can_spend(category, "lock", est_spend):
                    continue

                print(f"[AlertMonitor] 🔒 LOCK DETECTED: {ticker} {lock['side'].upper()} "
                      f"({lock.get('independent_groups',0)} groups, "
                      f"edge={float(lock.get('avg_edge_pct',0))*100:.1f}%)")

                result = await _execute_conviction(lock, tier="lock", dry_run=dry_run)
                if result:
                    fired.append(result)
                    break  # one lock bet per alert cycle to control exposure

            # ─── Execute JACKPOT plays (up to 2 per cycle) ───────────────
            jackpot_count = 0
            for jk in jackpots[:3]:
                ticker = jk["ticker"]
                if ticker in _session_bets or jackpot_count >= 2:
                    continue
                est_spend = max(1.0, _BANKROLL * 0.05)
                category  = jk.get("category", "misc")
                if not _can_spend(category, "jackpot", est_spend):
                    continue

                ev = float(jk.get("ev_per_dollar", 0))
                print(f"[AlertMonitor] 💰 JACKPOT: {ticker} {jk['side'].upper()} "
                      f"price={float(jk.get('market_price',0))*100:.0f}¢ "
                      f"EV/$ = {ev:.2f}")

                result = await _execute_conviction(jk, tier="jackpot", dry_run=dry_run)
                if result:
                    fired.append(result)
                    jackpot_count += 1

            if fired:
                print(f"[AlertMonitor] Placed {len(fired)} alert bet(s)")
            # else: silent — no spam when nothing qualifies

        except asyncio.CancelledError:
            print("[AlertMonitor] Cancelled.")
            break
        except Exception as e:
            print(f"[AlertMonitor] Error: {e}")


# ─── Main research cycle ──────────────────────────────────────────────────────

async def study_cycle(execute: bool = None) -> dict:
    """
    Full hourly research cycle:
      1. Auto-settle open bets
      2. Run conviction engine across ALL markets
      3. Execute: locks first, then jackpots, then signal plays
      4. Snapshot prices for OI-delta analysis
      5. Write report
    """
    if execute is None:
        execute = _EXECUTE
    dry_run = not execute

    ts = datetime.now(timezone.utc).isoformat()
    print(f"\n[ResearchAgent] === Study cycle {ts} ===")

    # 1. Settle open bets
    newly_settled = await auto_settle_open_bets()
    if newly_settled:
        print(f"[ResearchAgent] Settled {len(newly_settled)} bets")

    # 2. Run conviction engine
    print("[ResearchAgent] Running conviction engine scan...")
    try:
        from research.conviction_engine import scan_all_tiers
        tiers = await scan_all_tiers()
    except Exception as e:
        print(f"[ResearchAgent] Conviction scan error: {e}")
        tiers = {"locks": [], "jackpots": [], "strong": [], "signals": [], "summary": {}}

    locks    = tiers.get("locks", [])
    jackpots = tiers.get("jackpots", [])
    strong   = tiers.get("strong", [])
    summary  = tiers.get("summary", {})
    print(f"[ResearchAgent] Results: {summary.get('lock_count',0)} locks, "
          f"{summary.get('jackpot_count',0)} jackpots, "
          f"{summary.get('strong_count',0)} strong")

    # 3. Execute in priority order
    executed = []
    weights  = get_strategy_weights()

    # ── LOCKS (highest priority, target 85-90% win rate) ──────────────────
    for lock in locks[:3]:  # top 3 locks per cycle
        ticker = lock["ticker"]
        if ticker in _session_bets:
            continue
        cat = lock.get("category", "misc")
        est = max(1.0, _BANKROLL * float(lock.get("avg_edge_pct", 0.05)) * 0.25)
        if not _can_spend(cat, "lock", est):
            continue
        r = await _execute_conviction(lock, tier="lock", dry_run=dry_run)
        if r:
            executed.append(r)

    # ── JACKPOTS (high payout, small bet) ─────────────────────────────────
    for jk in jackpots[:2]:
        ticker = jk["ticker"]
        if ticker in _session_bets:
            continue
        cat = jk.get("category", "misc")
        est = max(1.0, _BANKROLL * 0.05)
        if not _can_spend(cat, "jackpot", est):
            continue
        r = await _execute_conviction(jk, tier="jackpot", dry_run=dry_run)
        if r:
            executed.append(r)

    # ── STRONG (normal confidence, 3+ strategies) ─────────────────────────
    for play in strong[:2]:
        ticker = play["ticker"]
        if ticker in _session_bets:
            continue
        cat  = play.get("category", "misc")
        edge = float(play.get("avg_edge_pct", play.get("edge_pct", 0.05)))
        conf = float(play.get("avg_confidence", play.get("confidence", 0)))
        if conf < _MIN_CONFIDENCE:
            continue
        est = max(1.0, _BANKROLL * edge * 0.12)
        if not _can_spend(cat, "signal", est):
            continue
        r = await _execute_conviction(play, tier="signal", dry_run=dry_run)
        if r:
            executed.append(r)

    # 4. Snapshot top-25 market prices for OI-delta enrichment
    all_plays = tiers.get("signals", [])
    tickers   = list({p["ticker"] for p in (locks + jackpots + strong + all_plays)})[:25]
    if tickers:
        await snapshot_market_prices(tickers)

    # 5. Build report
    stats       = get_all_stats()
    best_strats = get_best_strategies(min_bets=3)
    signal_corr = get_signal_correlations()

    report = {
        "timestamp":              ts,
        "conviction_summary":     summary,
        "executed":               len(executed),
        "dry_run":                dry_run,
        "newly_settled_bets":     len(newly_settled),
        "top_locks":              locks[:5],
        "top_jackpots":           jackpots[:5],
        "top_strong":             strong[:5],
        "executed_bets":          executed,
        "performance": {
            "total_bets":    stats["total_bets"],
            "win_rate_pct":  round(stats["win_rate"] * 100, 1),
            "roi_pct":       stats["roi_pct"],
            "gross_pnl":     stats["gross_pnl"],
            "open_exposure": stats["open_exposure"],
        },
        "best_strategies":            best_strats[:5],
        "top_signal_correlations":    signal_corr[:10],
        "daily_pnl":                  get_daily_pnl(7),
        "daily_spend":                dict(_daily_spent),
        "tier_spend":                 dict(_tier_spent),
    }

    try:
        _REPORT_PATH.parent.mkdir(exist_ok=True)
        with open(str(_REPORT_PATH), "w") as f:
            json.dump(report, f, indent=2, default=str)
    except Exception as e:
        print(f"[ResearchAgent] Report write error: {e}")

    return report


async def deep_analysis() -> dict:
    """
    Extended analysis run every 6 hours:
    - Compute full signal correlations
    - Identify best/worst strategies by ROI
    - Update econ consensus cache from external sources
    - Generate insights text
    """
    print("[ResearchAgent] === Deep analysis ===")
    stats    = get_all_stats()
    best     = get_best_strategies(min_bets=5)
    corr     = get_signal_correlations()
    pnl      = get_daily_pnl(30)

    insights = []

    # Strategy insights
    for s in best[:3]:
        roi_pct = round((s["total_pnl"] / max(0.01, s["total_wagered"])) * 100, 1)
        insights.append(
            f"  BEST: {s['strategy']} — {s['wins']}W/{s['losses']}L, {roi_pct}% ROI"
        )

    # Signal insights
    for c in corr[:3]:
        insights.append(
            f"  SIGNAL: {c['signal']} r={c['correlation']:+.3f} on {c['n']} bets"
        )

    # Update econ consensus cache
    await _update_econ_consensus_cache()

    analysis = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "best_strategies": best,
        "signal_correlations": corr,
        "monthly_pnl": pnl,
        "insights": insights,
    }
    print("[ResearchAgent] Deep analysis insights:")
    for ins in insights:
        print(ins)
    return analysis


async def _update_econ_consensus_cache() -> None:
    """
    Try to update the econ consensus cache from public sources.
    Falls back silently if unavailable.
    """
    try:
        import httpx
        # Try to get consensus from a public economic calendar API
        # Using Trading Economics public summary as a data source
        updated = {}
        async with httpx.AsyncClient(timeout=10) as client:
            # Try to fetch recent NFP estimate from stlouisfed FRED
            # PAYEMS latest trend as proxy for NFP consensus
            resp = await client.get(
                "https://fred.stlouisfed.org/graph/fredgraph.json?id=PAYEMS&vintage_date=&limit=2",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200:
                data = resp.json()
                obs = data.get("observations", [])
                if len(obs) >= 2:
                    latest = float(obs[-1].get("value", 0))
                    prev   = float(obs[-2].get("value", 0))
                    # Monthly change in thousands
                    change = (latest - prev) * 1000
                    updated["NFP"] = {
                        "estimate": round(change, 0),
                        "std": 50,
                        "source": "FRED trend",
                    }

        if updated:
            _CONSENSUS_CACHE.parent.mkdir(exist_ok=True)
            existing = {}
            if _CONSENSUS_CACHE.exists():
                with open(str(_CONSENSUS_CACHE)) as f:
                    existing = json.load(f)
            existing.update(updated)
            with open(str(_CONSENSUS_CACHE), "w") as f:
                json.dump(existing, f, indent=2)
            print(f"[ResearchAgent] Updated consensus cache: {list(updated.keys())}")
    except Exception as e:
        print(f"[ResearchAgent] Consensus cache update error: {e}")


# ─── Main loop ────────────────────────────────────────────────────────────────

async def run_research_loop(
    interval_minutes: int = None,
    execute: bool = None,
    run_once: bool = False,
) -> None:
    """
    Continuous research loop.

    interval_minutes: how often to run a full scan (default from env)
    execute: place real bets if True (default from env)
    run_once: run a single cycle and exit (useful for testing)

    Launches alert_monitor() as a background task that fires every
    ALERT_INTERVAL_SEC regardless of the main cycle interval — this is
    what ensures we never miss a LOCK or JACKPOT-level play.
    """
    interval = (interval_minutes or _INTERVAL_MIN) * 60
    if execute is None:
        execute = _EXECUTE

    print(f"[ResearchAgent] Starting research loop")
    print(f"  Interval:      {interval // 60} min")
    print(f"  Alert monitor: every {_ALERT_INTERVAL_SEC}s")
    print(f"  Min edge:      {_MIN_EDGE*100:.0f}%")
    print(f"  Min conf:      {_MIN_CONFIDENCE*100:.0f}%")
    print(f"  Max $/tier:    ${_MAX_DAILY_USD:.2f}/day")
    print(f"  Bankroll:      ${_BANKROLL:.2f}")
    print(f"  Execute:       {execute}")
    print(f"  Lock limit:    ${_TIER_LIMITS['lock']:.2f}/day    (targets 85-90% win rate)")
    print(f"  Jackpot limit: ${_TIER_LIMITS['jackpot']:.2f}/day  (high-payout asymmetric plays)")

    cycle_count   = 0
    last_deep     = 0.0
    alert_task    = None

    if not run_once:
        # Start the alert monitor as a concurrent background task
        alert_task = asyncio.create_task(alert_monitor(execute=execute))
        print(f"[ResearchAgent] Alert monitor started (task={alert_task.get_name()})")

    while True:
        try:
            await study_cycle(execute=execute)
            cycle_count += 1

            # Deep analysis every 6 hours
            if time.time() - last_deep > 6 * 3600:
                await deep_analysis()
                last_deep = time.time()

            # Daily summary at midnight UTC window
            hour = datetime.now(timezone.utc).hour
            if hour == 0 and cycle_count % 4 == 0:
                write_daily_summary(bankroll_end=_BANKROLL)

        except Exception as e:
            print(f"[ResearchAgent] Cycle error: {e}")
            import traceback
            traceback.print_exc()

        if run_once:
            if alert_task and not alert_task.done():
                alert_task.cancel()
            break

        print(f"[ResearchAgent] Sleeping {interval // 60} min until next scan...\n")
        await asyncio.sleep(interval)


# ─── Quick query tools for MCP server ────────────────────────────────────────

def get_latest_report() -> dict:
    """Return the most recent research report (from disk cache)."""
    try:
        if _REPORT_PATH.exists():
            with open(str(_REPORT_PATH)) as f:
                return json.load(f)
    except Exception:
        pass
    return {"error": "No report yet — run a study cycle first"}


def get_performance_summary() -> dict:
    """Quick performance summary for MCP tools and dashboard."""
    stats  = get_all_stats()
    best   = get_best_strategies(min_bets=3)
    pnl_7d = get_daily_pnl(7)

    total_pnl_7d = sum(d.get("pnl", 0) for d in pnl_7d)
    return {
        "overall": {
            "total_bets":    stats["total_bets"],
            "win_rate_pct":  round(stats["win_rate"] * 100, 1),
            "roi_pct":       stats["roi_pct"],
            "total_pnl":     stats["gross_pnl"],
            "open_exposure": stats["open_exposure"],
        },
        "last_7_days_pnl": round(total_pnl_7d, 4),
        "top_strategies":  best[:3],
        "daily_breakdown": pnl_7d,
    }
