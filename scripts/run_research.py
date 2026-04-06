# -*- coding: utf-8 -*-
"""
run_research.py — Continuous research system runner
====================================================
Wraps the ResearchAgent in an outer fault-tolerant loop with:
  - Dynamic scheduling (15 min during crypto sessions, 60 min general)
  - Timestamped log file (logs/research_YYYY-MM-DD.log)
  - Auto-restart on crash with exponential back-off
  - Reads interval + execute flag from config/betting_config.json

Usage:
  python scripts/run_research.py                 # study only, no real bets
  python scripts/run_research.py --execute       # place real bets
  python scripts/run_research.py --once          # one cycle then exit

Config file: config/betting_config.json
  systems.research_daily.interval_minutes   (default 60)
  systems.research_daily.enabled            (must be true)
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

# ─── Parse CLI args ──────────────────────────────────────────────────────────
_EXECUTE  = "--execute" in sys.argv
_RUN_ONCE = "--once"    in sys.argv

# ─── Load config ─────────────────────────────────────────────────────────────
_CFG_PATH = _ROOT / "config" / "betting_config.json"
_cfg: dict = {}
if _CFG_PATH.exists():
    with open(str(_CFG_PATH)) as f:
        _cfg = json.load(f)

_INTERVAL  = _cfg.get("systems", {}).get("research_daily", {}).get("interval_minutes", 60)
_ENABLED   = _cfg.get("systems", {}).get("research_daily", {}).get("enabled", True)

# ─── Logging ──────────────────────────────────────────────────────────────────
_LOG_DIR = _ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_today   = datetime.datetime.now().strftime("%Y-%m-%d")
_log_path = _LOG_DIR / f"research_{_today}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(_log_path), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("run_research")

# ─── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    if not _ENABLED:
        log.warning("research_daily system is disabled in config. Set enabled=true to run.")
        return

    log.info("=" * 60)
    log.info("Kalshi Edge — Research System Starting")
    log.info(f"  Interval  : {_INTERVAL} min")
    log.info(f"  Execute   : {_EXECUTE}")
    log.info(f"  Run once  : {_RUN_ONCE}")
    log.info(f"  Log file  : {_log_path}")
    log.info("=" * 60)

    from agents.research_agent import run_research_loop, study_cycle

    if _RUN_ONCE:
        report = await study_cycle(execute=_EXECUTE)
        log.info(f"Single cycle complete. Found {report['opportunities_found']} opps, "
                 f"executed {report['executed']}.")
        return

    # Outer crash-restart loop with exponential back-off
    back_off = 5
    while True:
        try:
            await run_research_loop(
                interval_minutes = _INTERVAL,
                execute          = _EXECUTE,
            )
        except KeyboardInterrupt:
            log.info("Research system stopped by user.")
            break
        except Exception as exc:
            log.exception(f"Research loop crashed: {exc}")
            log.info(f"Restarting in {back_off}s...")
            await asyncio.sleep(back_off)
            back_off = min(back_off * 2, 300)  # cap at 5 minutes
        else:
            break  # clean exit


if __name__ == "__main__":
    asyncio.run(main())
