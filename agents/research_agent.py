"""
research_agent.py — Autonomous study, learning, and execution agent
===================================================================
The Research Agent runs continuously and does four things in each cycle:

  STUDY   — scans ALL Kalshi markets, scores them with the strategy library,
              records snapshots for historical analysis, generates a report.

  LEARN   — queries the learning tracker for unresolved bets, settles them
              via the Kalshi API, updates strategy weights, identifies which
              signals actually predict wins.

  ACT     — for opportunities above the confidence/edge threshold, places bets
              automatically (respecting per-category daily limits).

  REPORT  — writes a JSON + text report to db/research_report.json so the
              MCP server and dashboard can display it.

Timeframe schedule:
  Every 15 minutes : 15-min crypto scan + act
  Every 60 minutes : full scan of all categories + act
  Every 6 hours    : deep learning analysis + report generation
  Daily (midnight) : write_daily_summary, update econ consensus cache

Config (environment variables or config/betting_config.json):
  RESEARCH_INTERVAL_MIN    (default: 60)
  RESEARCH_MIN_EDGE        (default: 0.05)
  RESEARCH_MIN_CONFIDENCE  (default: 0.50)
  RESEARCH_MAX_DAILY_USD   (default: 3.00)   — $ per category per day
  RESEARCH_BANKROLL        (default: BANKROLL from .env)
  RESEARCH_EXECUTE         (default: false)  — set true to place real bets
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from research.market_scanner import scan_all, snapshot_market_prices
from research.learning_tracker import (
    get_all_stats, get_strategy_weights, get_best_strategies,
    get_daily_pnl, get_signal_correlations, record_bet, auto_settle_open_bets,
    write_daily_summary,
)

# ─── Config ───────────────────────────────────────────────────────────────────

_INTERVAL_MIN        = int(os.getenv("RESEARCH_INTERVAL_MIN", "60"))
_MIN_EDGE            = float(os.getenv("RESEARCH_MIN_EDGE", "0.05"))
_MIN_CONFIDENCE      = float(os.getenv("RESEARCH_MIN_CONFIDENCE", "0.50"))
_MAX_DAILY_USD       = float(os.getenv("RESEARCH_MAX_DAILY_USD", "3.00"))  # per category
_BANKROLL            = float(os.getenv("RESEARCH_BANKROLL", os.getenv("BANKROLL", "10")))
_EXECUTE             = os.getenv("RESEARCH_EXECUTE", "false").lower() == "true"
_REPORT_PATH         = _PROJECT_ROOT / "db" / "research_report.json"
_CONSENSUS_CACHE     = _PROJECT_ROOT / "db" / "econ_consensus.json"

_CATEGORY_LIMITS = {
    "crypto":   _MAX_DAILY_USD * 1.5,   # crypto gets more allocation
    "econ":     _MAX_DAILY_USD,
    "political":_MAX_DAILY_USD * 0.5,   # political = higher variance
    "weather":  _MAX_DAILY_USD * 0.5,
    "sports":   _MAX_DAILY_USD,
    "misc":     _MAX_DAILY_USD * 0.3,
}

# Daily spend tracker (resets at midnight)
_daily_spent: dict[str, float] = {}
_last_reset_day: str = ""


def _check_reset_daily():
    global _daily_spent, _last_reset_day
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != _last_reset_day:
        _daily_spent = {k: 0.0 for k in _CATEGORY_LIMITS}
        _last_reset_day = today


def _can_spend(category: str, amount: float) -> bool:
    _check_reset_daily()
    spent = _daily_spent.get(category, 0.0)
    limit = _CATEGORY_LIMITS.get(category, _MAX_DAILY_USD)
    return spent + amount <= limit


def _register_spend(category: str, amount: float):
    _check_reset_daily()
    _daily_spent[category] = _daily_spent.get(category, 0.0) + amount


# ─── Execution helper ──────────────────────────────────────────────────────

async def _execute_opportunity(opp: dict, dry_run: bool = True) -> Optional[dict]:
    """Route an opportunity through the Kalshi executor."""
    try:
        from agents.kalshi_executor import (
            contracts_for_spend, potential_profit, MAX_CONTRACTS
        )
        import httpx
        from data.feeds.kalshi_intraday import _headers, _BASE

        side       = opp["side"]
        price_c    = int(opp["yes_ask_cents"] if side == "yes" else (100 - opp.get("yes_ask_cents", 50)))
        our_prob   = opp["our_prob"]
        edge_pct   = abs(opp["edge_pct"])
        bankroll   = _BANKROLL

        # Kelly sizing (fractional)
        kelly_f    = edge_pct / (our_prob * (1 - our_prob) + 1e-9) * 0.10
        spend      = round(max(1.0, min(bankroll * kelly_f, _MAX_DAILY_USD)), 2)
        contracts  = contracts_for_spend(spend, price_c)
        contracts  = min(contracts, MAX_CONTRACTS)
        spend      = round(contracts * price_c / 100.0, 2)

        order_id   = None
        if not dry_run and spend >= 0.50:
            payload = {
                "ticker": opp["ticker"],
                "action": "buy",
                "type": "market",
                "side": side,
                "count": contracts,
                "client_order_id": f"ra_{int(time.time())}",
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
            except Exception as e:
                print(f"[ResearchAgent] Order error: {e}")
                return None

        # Record in learning tracker
        bet_id = record_bet(
            ticker       = opp["ticker"],
            side         = side,
            price_cents  = price_c,
            contracts    = contracts,
            spend_usd    = spend,
            our_prob     = our_prob,
            edge_pct     = edge_pct,
            strategy     = opp.get("strategy", "research"),
            signals      = opp.get("signals", {}),
            order_id     = order_id,
            market_type  = opp.get("category", opp.get("market_type", "misc")),
            timeframe    = opp.get("timeframe", "daily"),
            series       = opp.get("series"),
            asset        = opp.get("asset"),
            notes        = opp.get("reason", ""),
        )
        _register_spend(opp.get("category", "misc"), spend)

        return {
            "bet_id":    bet_id,
            "order_id":  order_id,
            "ticker":    opp["ticker"],
            "side":      side,
            "contracts": contracts,
            "spend":     spend,
            "edge_pct":  round(edge_pct * 100, 2),
            "dry_run":   dry_run,
        }
    except Exception as e:
        print(f"[ResearchAgent] Execute error for {opp.get('ticker')}: {e}")
        return None


# ─── Research cycle ───────────────────────────────────────────────────────────

async def study_cycle(execute: bool = None) -> dict:
    """
    One complete research cycle:
      1. Settle open bets
      2. Scan all markets
      3. Score & filter opportunities
      4. Execute top picks (if enabled)
      5. Snapshot prices for future OI-delta enrichment
      6. Return report dict
    """
    if execute is None:
        execute = _EXECUTE
    dry_run = not execute

    ts = datetime.now(timezone.utc).isoformat()
    print(f"\n[ResearchAgent] === Study cycle started {ts} ===")

    # 1. Settle open bets
    print("[ResearchAgent] Settling open bets...")
    newly_settled = await auto_settle_open_bets()
    if newly_settled:
        print(f"[ResearchAgent] Settled {len(newly_settled)} bets")

    # 2. Scan all markets
    print("[ResearchAgent] Scanning all markets...")
    try:
        opportunities = await scan_all(min_edge=_MIN_EDGE, top_n=100)
    except Exception as e:
        print(f"[ResearchAgent] Scan error: {e}")
        opportunities = []
    print(f"[ResearchAgent] Found {len(opportunities)} opportunities with edge >= {_MIN_EDGE*100:.0f}%")

    # 3. Filter by confidence
    filtered = [o for o in opportunities if o.get("confidence", 0) >= _MIN_CONFIDENCE]

    # 4. Execute top picks per category
    executed = []
    if filtered:
        # Group by category, take best per category
        by_cat: dict[str, list] = {}
        for o in filtered:
            by_cat.setdefault(o["category"], []).append(o)

        for cat, picks in by_cat.items():
            best = picks[0]  # already sorted by edge
            estimated_spend = max(1.0, _BANKROLL * abs(best["edge_pct"]) * 0.10)
            if not _can_spend(cat, estimated_spend):
                print(f"[ResearchAgent] {cat} daily limit reached, skipping")
                continue
            print(f"[ResearchAgent] Executing: {best['ticker']} {best['side'].upper()} "
                  f"edge={best['edge_pct']*100:.1f}% strategy={best['strategy']}")
            result = await _execute_opportunity(best, dry_run=dry_run)
            if result:
                executed.append(result)

    # 5. Snapshot top 20 tickers for future OI-delta analysis
    tickers = [o["ticker"] for o in opportunities[:20]]
    if tickers:
        await snapshot_market_prices(tickers)

    # 6. Build stats
    stats = get_all_stats()
    best_strats = get_best_strategies(min_bets=3)
    signal_corr = get_signal_correlations()

    # 7. Generate report
    report = {
        "timestamp":            ts,
        "opportunities_found":  len(opportunities),
        "opportunities_filtered": len(filtered),
        "executed":             len(executed),
        "dry_run":              dry_run,
        "newly_settled_bets":   len(newly_settled),
        "top_opportunities":    opportunities[:10],
        "executed_bets":        executed,
        "performance": {
            "total_bets":   stats["total_bets"],
            "win_rate":     round(stats["win_rate"] * 100, 1),
            "roi_pct":      stats["roi_pct"],
            "gross_pnl":    stats["gross_pnl"],
            "open_exposure":stats["open_exposure"],
        },
        "best_strategies": best_strats[:5],
        "top_signal_correlations": signal_corr[:10],
        "daily_pnl": get_daily_pnl(7),
        "daily_spend_tracker": dict(_daily_spent),
    }

    # Write report to disk
    try:
        _REPORT_PATH.parent.mkdir(exist_ok=True)
        with open(str(_REPORT_PATH), "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"[ResearchAgent] Report written to {_REPORT_PATH}")
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
    """
    interval = (interval_minutes or _INTERVAL_MIN) * 60
    if execute is None:
        execute = _EXECUTE

    print(f"[ResearchAgent] Starting research loop")
    print(f"  Interval:    {interval // 60} min")
    print(f"  Min edge:    {_MIN_EDGE*100:.0f}%")
    print(f"  Min conf:    {_MIN_CONFIDENCE*100:.0f}%")
    print(f"  Max $/cat:   ${_MAX_DAILY_USD:.2f}/day")
    print(f"  Execute:     {execute}")

    cycle_count = 0
    last_deep   = 0.0

    while True:
        try:
            await study_cycle(execute=execute)
            cycle_count += 1

            # Deep analysis every 6 hours
            if time.time() - last_deep > 6 * 3600:
                await deep_analysis()
                last_deep = time.time()

            # Daily summary at "midnight UTC" window (within any cycle after 00:00)
            hour = datetime.now(timezone.utc).hour
            if hour == 0 and cycle_count % 4 == 0:
                write_daily_summary(bankroll_end=_BANKROLL)

        except Exception as e:
            print(f"[ResearchAgent] Cycle error: {e}")
            import traceback
            traceback.print_exc()

        if run_once:
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
