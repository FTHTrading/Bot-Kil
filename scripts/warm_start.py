"""
scripts/warm_start.py — 4:55 AM ET pre-open health checks and reopen arming
============================================================================
Runs immediately before the trading day opens.  Verifies every subsystem,
loads the pre-open watchlist, arms reopen mode in daily_state.json so
the autonomous agent starts the session with stricter thresholds for the
first 10-15 minutes.

Usage
-----
    python scripts/warm_start.py [--no-arm]

Flags
-----
    --no-arm    Run health checks and watchlist load but do NOT arm
                reopen mode (useful for testing mid-day).

Output
------
    logs/warm_start_YYYY-MM-DD.json — health report
    logs/daily_state.json           — updated with reopen_mode fields
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

_ROOT        = Path(__file__).parent.parent
_LOGS        = _ROOT / "logs"
_MODELS      = _ROOT / "models"
_STATE_FILE  = _LOGS / "daily_state.json"
_MODEL_FILE  = _MODELS / "kalshi_net.pt"

# Reopen-mode durations
_REOPEN_WINDOW_SECS = 15 * 60   # 15 min until reopen_mode auto-expires
_NO_TRADE_SECS      = 10 * 60   # first 10 min: no trades placed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [warm_start] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("warm_start")


# ── Health checks ─────────────────────────────────────────────────────────────

def _check_api_auth() -> dict:
    try:
        from data.feeds.kalshi import get_balance
        bal = get_balance()
        available = bal.get("available", None)
        return {"status": "ok", "balance": available}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def _check_model_load() -> dict:
    try:
        if not _MODEL_FILE.exists():
            return {"status": "fail", "error": f"model file missing: {_MODEL_FILE}"}
        from engine.neural_model import get_model, SCHEMA_HASH
        model = get_model()
        if model is None:
            return {"status": "fail", "error": "get_model() returned None"}
        return {"status": "ok", "schema_hash": SCHEMA_HASH, "path": str(_MODEL_FILE)}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def _check_state_file() -> dict:
    try:
        today = date.today().isoformat()
        if not _STATE_FILE.exists():
            return {"status": "ok", "note": "no state file yet (first run)"}
        raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        file_date = raw.get("date", "")
        if file_date != today:
            return {"status": "ok", "note": f"state is from {file_date} (will reset)"}
        cooldowns  = raw.get("cooldowns", {})
        daily_spend = raw.get("daily_spend", 0.0)
        return {
            "status":      "ok",
            "date":        file_date,
            "daily_spend": daily_spend,
            "cooldowns":   len(cooldowns),
        }
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def _check_orderbook_endpoint() -> dict:
    """Spot-check orderbook connectivity with the first available ticker."""
    try:
        from data.feeds.kalshi import get_active_markets, get_market_orderbook
        markets = get_active_markets("BTC")
        if not markets:
            return {"status": "warn", "note": "no active BTC markets"}
        ticker = markets[0]["ticker"]
        ob = get_market_orderbook(ticker)
        n_yes = len(ob.get("yes", []))
        n_no  = len(ob.get("no",  []))
        return {"status": "ok", "ticker": ticker, "yes_levels": n_yes, "no_levels": n_no}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def _check_settlement_endpoint() -> dict:
    try:
        from data.feeds.kalshi import get_settlements
        settlements = get_settlements()
        return {"status": "ok", "settlements_today": len(settlements)}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def _load_watchlist(today: str) -> Optional[dict]:
    path = _LOGS / f"preopen_watchlist_{today}.json"
    if not path.exists():
        log.warning("[warm_start] No watchlist for %s at %s", today, path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("[warm_start] Watchlist load failed: %s", e)
        return None


# ── Arm reopen mode ───────────────────────────────────────────────────────────

def _arm_reopen_mode(today: str) -> bool:
    """
    Write reopen_mode fields into daily_state.json.
    The autonomous agent reads these on the next run_agent() cycle.
    """
    now = time.time()
    try:
        # Load existing state (if same date)
        state: dict = {}
        if _STATE_FILE.exists():
            try:
                raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                if raw.get("date") == today:
                    state = raw
            except Exception:
                pass

        state["date"]                 = today
        state["reopen_mode"]          = True
        state["reopen_mode_expires"]  = now + _REOPEN_WINDOW_SECS
        state["reopen_no_trade_until"] = now + _NO_TRADE_SECS

        _LOGS.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        log.info(
            "[warm_start] Reopen mode armed — no-trade until +%d min, expires +%d min",
            _NO_TRADE_SECS // 60, _REOPEN_WINDOW_SECS // 60,
        )
        return True
    except Exception as e:
        log.error("[warm_start] Failed to arm reopen mode: %s", e)
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pre-open warm-start health check")
    parser.add_argument("--no-arm", action="store_true",
                        help="Run checks but do NOT arm reopen mode")
    args = parser.parse_args()

    today    = date.today().isoformat()
    t_start  = time.time()

    log.info("=" * 60)
    log.info("Warm start  date=%s  arm=%s", today, not args.no_arm)
    log.info("=" * 60)

    checks = {}

    log.info("Checking API authentication …")
    checks["api_auth"] = _check_api_auth()

    log.info("Checking model load …")
    checks["model_load"] = _check_model_load()

    log.info("Checking state file …")
    checks["state_file"] = _check_state_file()

    log.info("Checking orderbook endpoint …")
    checks["orderbook_endpoint"] = _check_orderbook_endpoint()

    log.info("Checking settlement endpoint …")
    checks["settlement_endpoint"] = _check_settlement_endpoint()

    # Report check results
    n_fail = sum(1 for v in checks.values() if v.get("status") == "fail")
    n_warn = sum(1 for v in checks.values() if v.get("status") == "warn")
    for name, res in checks.items():
        s = res.get("status", "?")
        marker = "✓" if s == "ok" else ("⚠" if s == "warn" else "✗")
        log.info("  %s %s: %s", marker, name, json.dumps(res))

    # Load watchlist
    watchlist = _load_watchlist(today)
    watchlist_candidates = len((watchlist or {}).get("candidates", []))
    log.info("Watchlist: %d candidates loaded", watchlist_candidates)

    # Arm reopen mode
    armed = False
    if not args.no_arm:
        if n_fail > 2:
            log.error(
                "[warm_start] %d critical checks failed — NOT arming reopen mode", n_fail
            )
        else:
            armed = _arm_reopen_mode(today)

    elapsed = round(time.time() - t_start, 1)
    report = {
        "date":            today,
        "started_at":      datetime.now(timezone.utc).isoformat(),
        "elapsed_secs":    elapsed,
        "checks":          checks,
        "n_fail":          n_fail,
        "n_warn":          n_warn,
        "watchlist_candidates": watchlist_candidates,
        "reopen_mode_armed":    armed,
        "overall_status":  "ok" if n_fail == 0 else ("warn" if n_fail <= 1 else "fail"),
    }

    out = _LOGS / f"warm_start_{today}.json"
    _LOGS.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info("Report written to %s", out)
    log.info("Warm start done in %.1f s — %d failures, %d warnings", elapsed, n_fail, n_warn)

    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
