# -*- coding: utf-8 -*-
"""
run_all_systems.py — Multi-timeframe system coordinator
========================================================
Launches and coordinates all betting systems in parallel:

  System 1 — 15-min intraday crypto (BTC/ETH/SOL)
               Calls engine/intraday_ev.py every 15 min

  System 2 — Research agent (all categories, 60 min)
               Calls agents/research_agent.py every 60 min

  System 3 — Daily picks (optional, once per day at 9:00 AM ET)
               Calls agents/orchestrator.py

Configuration from config/betting_config.json.

Usage:
  python scripts/run_all_systems.py               # dry-run both systems
  python scripts/run_all_systems.py --execute     # live bets both
  python scripts/run_all_systems.py --no-intraday # skip 15-min crypto
  python scripts/run_all_systems.py --no-research # skip research
  python scripts/run_all_systems.py --no-daily    # skip daily picks

Logs: logs/all_systems.log + logs/research_DATE.log + logs/intraday_DATE.log
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ─── CLI flags ────────────────────────────────────────────────────────────────
_EXECUTE       = "--execute"      in sys.argv
_NO_INTRADAY   = "--no-intraday"  in sys.argv
_NO_RESEARCH   = "--no-research"  in sys.argv
_NO_DAILY      = "--no-daily"     in sys.argv

# ─── Config ───────────────────────────────────────────────────────────────────
_CFG_PATH = _ROOT / "config" / "betting_config.json"
_cfg: dict = {}
if _CFG_PATH.exists():
    with open(str(_CFG_PATH)) as f:
        _cfg = json.load(f)

_INTRADAY_INTERVAL = _cfg.get("systems", {}).get("crypto_15min", {}).get("interval_seconds", 900)
_RESEARCH_INTERVAL = _cfg.get("systems", {}).get("research_daily", {}).get("interval_minutes", 60) * 60

# ─── Logging ──────────────────────────────────────────────────────────────────
_LOG_DIR = _ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(_LOG_DIR / "all_systems.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("all_systems")


# ─── Bankroll coordinator ─────────────────────────────────────────────────────

class BankrollCoordinator:
    """
    Shared daily budget tracker across all systems.
    Prevents any single system from consuming the whole bankroll.
    """
    def __init__(self, daily_bankroll: float):
        self.daily_bankroll = daily_bankroll
        self._today: str = ""
        self._spent: dict[str, float] = {}

    def _reset_if_new_day(self):
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            self._spent = {}
            log.info(f"[Bankroll] New day {today} — daily limits reset")

    def can_spend(self, system: str, amount: float, limit: float) -> bool:
        self._reset_if_new_day()
        return self._spent.get(system, 0.0) + amount <= limit

    def record_spend(self, system: str, amount: float):
        self._reset_if_new_day()
        self._spent[system] = self._spent.get(system, 0.0) + amount

    def report(self) -> dict:
        self._reset_if_new_day()
        return {
            "date": self._today,
            "bankroll": self.daily_bankroll,
            "spent": dict(self._spent),
            "remaining": self.daily_bankroll - sum(self._spent.values()),
        }


_coordinator = BankrollCoordinator(
    daily_bankroll=_cfg.get("bankroll_usd", 10.0)
)


# ─── System 1: 15-min intraday crypto ────────────────────────────────────────

async def run_intraday_system():
    """
    Replicates run_intraday.py logic inline for coordinator.
    Runs every INTRADAY_INTERVAL seconds (default 900s = 15 min).
    """
    from data.feeds.kalshi_intraday import fetch_intraday_markets
    from engine.intraday_ev import score_all_markets
    from agents.kalshi_executor import execute_intraday_picks

    log.info("[Intraday] System starting")
    back_off = 30

    while True:
        try:
            log.info("[Intraday] Scanning crypto 15-min markets...")
            markets = await fetch_intraday_markets()
            picks   = score_all_markets(markets)
            if picks:
                log.info(f"[Intraday] {len(picks)} picks found, executing top 3...")
                results = await execute_intraday_picks(
                    picks[:3], dry_run=not _EXECUTE
                )
                for r in results:
                    log.info(f"[Intraday]   {r.get('ticker')} {r.get('side')} "
                             f"${r.get('spend', 0):.2f} id={r.get('order_id')}")
            else:
                log.info("[Intraday] No qualifying picks this cycle")
            back_off = 30
        except Exception as exc:
            log.exception(f"[Intraday] Error: {exc}")
            log.info(f"[Intraday] Back-off {back_off}s")
            await asyncio.sleep(back_off)
            back_off = min(back_off * 2, 300)
            continue

        log.info(f"[Intraday] Sleeping {_INTRADAY_INTERVAL // 60} min...")
        await asyncio.sleep(_INTRADAY_INTERVAL)


# ─── System 2: Research agent (all categories) ───────────────────────────────

async def run_research_system():
    """Wraps research_agent.run_research_loop()."""
    log.info("[Research] System starting")
    from agents.research_agent import run_research_loop
    await run_research_loop(
        interval_minutes=_RESEARCH_INTERVAL // 60,
        execute=_EXECUTE,
    )


# ─── System 3: Daily picks (once per day at ~9:05 AM UTC) ────────────────────

async def run_daily_picks_system():
    """
    Run orchestrator once per day at 9:05 AM UTC (market open vicinity).
    """
    log.info("[Daily] System starting")
    from agents.orchestrator import run_daily_picks

    _last_run_day = ""
    while True:
        now   = datetime.datetime.utcnow()
        today = now.strftime("%Y-%m-%d")

        # Target: 9:05 AM UTC
        if today != _last_run_day and now.hour == 9 and now.minute >= 5:
            log.info("[Daily] Running daily picks...")
            try:
                await asyncio.get_event_loop().run_in_executor(None, run_daily_picks)
                _last_run_day = today
                log.info("[Daily] Daily picks complete")
            except Exception as exc:
                log.exception(f"[Daily] Error: {exc}")

        # Check again in 5 minutes
        await asyncio.sleep(300)


# ─── Status reporter ──────────────────────────────────────────────────────────

async def status_reporter():
    """Logs a combined status report every 30 minutes."""
    while True:
        await asyncio.sleep(1800)
        try:
            from agents.research_agent import get_performance_summary
            perf = get_performance_summary()
            br   = _coordinator.report()
            log.info("=" * 50)
            log.info("[Status] 30-min system report")
            log.info(f"[Status] Bankroll: ${br['remaining']:.2f} remaining today")
            log.info(f"[Status] Bets: {perf['overall']['total_bets']} total, "
                     f"{perf['overall']['win_rate_pct']}% win rate, "
                     f"${perf['overall']['total_pnl']:.4f} P/L")
            log.info(f"[Status] 7-day P/L: ${perf['last_7_days_pnl']:.4f}")
            if perf.get("top_strategies"):
                top = perf["top_strategies"][0]
                log.info(f"[Status] Best strategy: {top.get('strategy')} "
                         f"({top.get('wins', 0)}W/{top.get('losses', 0)}L)")
            log.info("=" * 50)
        except Exception as e:
            log.debug(f"[Status] Report error: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 60)
    log.info("Kalshi Edge — All Systems Coordinator starting")
    log.info(f"  Execute     : {_EXECUTE}")
    log.info(f"  Intraday    : {'OFF' if _NO_INTRADAY else f'{_INTRADAY_INTERVAL // 60} min'}")
    log.info(f"  Research    : {'OFF' if _NO_RESEARCH else f'{_RESEARCH_INTERVAL // 60} min'}")
    log.info(f"  Daily picks : {'OFF' if _NO_DAILY else 'ON (9:05 AM UTC)'}")
    log.info(f"  Bankroll    : ${_coordinator.daily_bankroll:.2f}/day")
    log.info("=" * 60)

    tasks = []

    if not _NO_INTRADAY:
        tasks.append(asyncio.create_task(run_intraday_system(),  name="intraday"))
    if not _NO_RESEARCH:
        tasks.append(asyncio.create_task(run_research_system(),  name="research"))
    if not _NO_DAILY:
        tasks.append(asyncio.create_task(run_daily_picks_system(), name="daily"))

    tasks.append(asyncio.create_task(status_reporter(), name="status"))

    if not tasks:
        log.warning("All systems disabled — nothing to run. Check your --flags.")
        return

    log.info(f"[Main] Running {len(tasks)} system(s) in parallel...")
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION
        )
        for t in done:
            if t.exception():
                log.exception(f"Task {t.get_name()} raised: {t.exception()}")
        for t in pending:
            t.cancel()
    except KeyboardInterrupt:
        log.info("[Main] Stopped by user.")
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
