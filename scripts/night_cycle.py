"""
scripts/night_cycle.py — 3 AM ET maintenance cycle
====================================================
Designed to run unattended in the maintenance window (3:00–5:00 AM ET).
Performs audit, retrain, recalibrate, setup re-rank, microstructure
cache, and pre-open watchlist generation.

Usage
-----
    python scripts/night_cycle.py [--dry-run] [--skip-train]

Flags
-----
    --dry-run     Log everything, write no model files.
    --skip-train  Skip the neural-model retrain step (faster for testing).

Output
------
    logs/night_cycle_YYYY-MM-DD.json  — comprehensive run report
    logs/night_audit_YYYY-MM-DD.json  — settlement audit with attribution
    logs/preopen_watchlist_YYYY-MM-DD.json — ranked watchlist for warm_start
    data/training_data.jsonl          — appended with newly settled trades
    models/kalshi_net.pt              — updated by train_neural_model.py
    models/calibrators/               — rebuilt by calibration module
    logs/setup_stats.json             — rebuilt by SetupRanker
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import subprocess
import sys
import time
from datetime import datetime, date, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
_ROOT            = Path(__file__).parent.parent
_LOGS            = _ROOT / "logs"
_DATA            = _ROOT / "data"
_MODELS          = _ROOT / "models"
_JOURNAL         = _LOGS / "trade_history.jsonl"
_TRAINING_DATA   = _DATA / "training_data.jsonl"
_CALIBRATORS_DIR = _MODELS / "calibrators"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [night_cycle] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("night_cycle")


# ── Phase 1: Settlement audit ─────────────────────────────────────────────────

def _run_audit(today: str, dry_run: bool) -> dict:
    """
    Fetch today's settled markets, attribute losses, compute P&L breakdowns
    by (asset, setup, hour-of-day, market_type).
    """
    log.info("[phase1] Starting settlement audit")
    report: dict = {
        "phase":          "audit",
        "settlements":    [],
        "pnl_by_asset":   {},
        "pnl_by_setup":   {},
        "pnl_by_hour":    {},
        "loss_attribution": {},
        "total_pnl":      0.0,
        "total_stake":    0.0,
        "errors":         [],
    }

    try:
        from data.feeds.kalshi import get_settlements
        settlements: list[dict] = get_settlements()
    except Exception as e:
        log.error("[phase1] get_settlements failed: %s", e)
        report["errors"].append(str(e))
        return report

    # Load executions from trade journal for matching
    executions = _load_journal_executions()
    ex_by_ticker: dict[str, dict] = {}
    for ex in executions:
        t = ex.get("ticker", "")
        ex_by_ticker.setdefault(t, ex)   # keep first (earliest) record

    for s in settlements:
        ticker     = s.get("ticker",      "")
        result     = s.get("market_result", "")     # "yes" / "no"
        yes_count  = float(s.get("yes_count_fp",          0) or 0)
        no_count   = float(s.get("no_count_fp",           0) or 0)
        yes_cost   = float(s.get("yes_total_cost_dollars", 0) or 0)
        no_cost    = float(s.get("no_total_cost_dollars",  0) or 0)
        revenue_raw = float(s.get("revenue",  0) or 0)
        fee        = float(s.get("fee_cost",  0) or 0)
        settled_time = s.get("settled_time", "")

        # Revenue normalisation: Kalshi sometimes returns cents
        revenue = revenue_raw / 100.0 if revenue_raw > 100 else revenue_raw

        stake = yes_cost + no_cost
        pnl   = revenue - fee - stake
        report["total_stake"] += stake
        report["total_pnl"]   += pnl

        # Match to a journal execution to get metadata
        ex = ex_by_ticker.get(ticker, {})
        asset      = ex.get("asset",      _guess_asset(ticker))
        setup      = ex.get("setup_class", "unknown")
        entry_edge = float(ex.get("entry_meta", {}).get("edge_pct", 0) or 0)
        side       = ex.get("side",       "yes")
        entry_prob = float(ex.get("entry_meta", {}).get("calibrated_prob", 0.5) or 0.5)

        # Hour of entry
        hour = _parse_hour(ex.get("ts", "") or settled_time)

        # Loss attribution
        attribution = "N/A"
        if pnl < 0:
            attribution = _attribute_loss(
                pnl=pnl,
                stake=stake,
                side=side,
                result=result,
                entry_prob=entry_prob,
                entry_edge=entry_edge,
                setup=setup,
            )

        rec = {
            "ticker":      ticker,
            "asset":       asset,
            "setup":       setup,
            "side":        side,
            "result":      result,
            "stake":       round(stake, 4),
            "revenue":     round(revenue, 4),
            "fee":         round(fee, 4),
            "pnl":         round(pnl, 4),
            "entry_edge":  round(entry_edge, 4),
            "hour":        hour,
            "attribution": attribution,
        }
        report["settlements"].append(rec)

        # Aggregate
        _add(report["pnl_by_asset"], asset,     pnl, stake)
        _add(report["pnl_by_setup"], setup,     pnl, stake)
        _add(report["pnl_by_hour"],  str(hour), pnl, stake)

        if attribution != "N/A":
            report["loss_attribution"].setdefault(attribution, 0)
            report["loss_attribution"][attribution] += 1

    log.info(
        "[phase1] Audit complete: %d settlements, total pnl %.2f, stake %.2f",
        len(report["settlements"]), report["total_pnl"], report["total_stake"],
    )

    if not dry_run:
        out_path = _LOGS / f"night_audit_{today}.json"
        _LOGS.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        log.info("[phase1] Audit written to %s", out_path)

    return report


def _attribute_loss(
    pnl: float,
    stake: float,
    side: str,
    result: str,
    entry_prob: float,
    entry_edge: float,
    setup: str,
) -> str:
    """Return a loss attribution label."""
    won_side = "yes" if result == "yes" else "no"
    if side != won_side:
        # We were on the wrong side — what caused it?
        if entry_edge > 8.0:
            return "wrong_direction"   # model was confident and wrong
        if entry_edge < 2.0:
            return "spread_trap"       # edge was eaten by spread
        return "wrong_direction"
    # We were on the right side but still lost — timing or fill quality
    if entry_prob < 0.45:
        return "wrong_timing"
    return "bad_fill"


def _add(agg: dict, key: str, pnl: float, stake: float):
    if key not in agg:
        agg[key] = {"pnl": 0.0, "stake": 0.0, "n": 0}
    agg[key]["pnl"]   += pnl
    agg[key]["stake"] += stake
    agg[key]["n"]     += 1


def _guess_asset(ticker: str) -> str:
    t = ticker.upper()
    if "ETH" in t:
        return "ETH"
    return "BTC"


def _parse_hour(ts: str) -> int:
    if not ts:
        return -1
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.hour
    except Exception:
        return -1


def _load_journal_executions() -> list[dict]:
    if not _JOURNAL.exists():
        return []
    out: list[dict] = []
    for line in _JOURNAL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if rec.get("type") in ("execution", "placed", "dry_run"):
                out.append(rec)
        except Exception:
            pass
    return out


# ── Phase 2: Append settled trades to training dataset ────────────────────────

def _append_training_data(audit_report: dict, dry_run: bool) -> int:
    """
    For each settled trade in audit_report, build a feature row and append
    to data/training_data.jsonl.  Uses the fields already in the entry_meta.
    Returns number of rows appended.
    """
    log.info("[phase2] Appending settled trades to training data")
    executions = _load_journal_executions()
    ex_by_ticker = {ex.get("ticker", ""): ex for ex in executions}

    appended = 0
    rows: list[str] = []
    for rec in audit_report.get("settlements", []):
        ticker  = rec.get("ticker", "")
        pnl     = rec.get("pnl", 0.0)
        ex      = ex_by_ticker.get(ticker, {})
        em      = ex.get("entry_meta", {}) or {}

        # Label: 1 = profitable, 0 = loss
        label = 1 if pnl > 0 else 0

        # Build feature row matching FEATURE_SCHEMA from neural_model.py
        row = {
            "ticker":          ticker,
            "asset":           rec.get("asset", "BTC"),
            "label":           label,
            "gap_pct":         float(em.get("gap_pct",         0) or 0),
            "mom_1m":          float(em.get("mom_1m",          0) or 0),
            "mom_3m":          float(em.get("mom_3m",          0) or 0),
            "mom_5m":          float(em.get("mom_5m",          0) or 0),
            "mom_15m":         float(em.get("mom_15m",         0) or 0),
            "realized_vol":    float(em.get("realized_vol",    0) or 0),
            "t_remaining_norm": float(em.get("t_remaining_norm", 0) or 0),
            "hour_sin":        float(em.get("hour_sin",        0) or 0),
            "hour_cos":        float(em.get("hour_cos",        0) or 0),
            "trend_up":        int(em.get("trend_up",          0) or 0),
            "trend_down":      int(em.get("trend_down",        0) or 0),
            "gap_pos":         int(em.get("gap_pos",           0) or 0),
            "gap_neg":         int(em.get("gap_neg",           0) or 0),
            "pnl":             round(float(pnl), 4),
            "settled_date":    date.today().isoformat(),
        }
        rows.append(json.dumps(row))
        appended += 1

    if not dry_run and rows:
        _TRAINING_DATA.parent.mkdir(parents=True, exist_ok=True)
        with _TRAINING_DATA.open("a", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        log.info("[phase2] Appended %d rows to %s", appended, _TRAINING_DATA)
    elif dry_run:
        log.info("[phase2] DRY RUN — would append %d rows", appended)

    return appended


# ── Phase 3: Retrain neural model ─────────────────────────────────────────────

def _retrain_model(dry_run: bool, skip_train: bool) -> bool:
    if skip_train:
        log.info("[phase3] Skipping retrain (--skip-train flag)")
        return True
    if dry_run:
        log.info("[phase3] DRY RUN — would run train_neural_model.py")
        return True

    log.info("[phase3] Launching train_neural_model.py")
    result = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "train_neural_model.py")],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        log.error("[phase3] Training failed:\n%s", result.stderr[-2000:])
        return False
    log.info("[phase3] Training complete")
    return True


# ── Phase 4: Rebuild calibrators ──────────────────────────────────────────────

def _rebuild_calibrators(dry_run: bool) -> bool:
    log.info("[phase4] Rebuilding calibrators")
    if dry_run:
        log.info("[phase4] DRY RUN — skipping calibrator rebuild")
        return True
    try:
        from engine.calibration import CalibrationStore
        store = CalibrationStore()
        store.rebuild_from_journal()
        log.info("[phase4] Calibrators rebuilt")
        return True
    except AttributeError:
        log.warning("[phase4] CalibrationStore.rebuild_from_journal not implemented — skipping")
        return True
    except Exception as e:
        log.error("[phase4] Calibration rebuild failed: %s", e)
        return False


# ── Phase 5: Rebuild setup expectancy ─────────────────────────────────────────

def _rebuild_setup_expectancy(dry_run: bool) -> dict:
    log.info("[phase5] Rebuilding setup expectancy")
    try:
        from engine.setup_ranker import SetupRanker
        ranker = SetupRanker()
        ranker.rebuild_from_journal()
        if not dry_run:
            ranker.save()
        summary = ranker.summary()
        log.info("[phase5] Setup expectancy rebuilt: %s", json.dumps(summary))
        return summary
    except Exception as e:
        log.error("[phase5] Setup expectancy rebuild failed: %s", e)
        return {}


# ── Phase 6: Microstructure cache ─────────────────────────────────────────────

def _cache_microstructure(today: str, dry_run: bool) -> dict:
    """
    Snapshot spread + open-interest for all live BTC/ETH Kalshi markets.
    Saves to logs/microstructure_cache_{today}.json.
    """
    log.info("[phase6] Caching microstructure data")
    cache: dict = {"markets": {}, "errors": [], "ts": datetime.now(timezone.utc).isoformat()}

    try:
        from data.feeds.kalshi import get_active_markets, get_market_orderbook, get_market
    except ImportError as e:
        log.error("[phase6] Kalshi import failed: %s", e)
        cache["errors"].append(str(e))
        return cache

    for asset in ("BTC", "ETH"):
        try:
            markets = get_active_markets(asset)
        except Exception as e:
            log.warning("[phase6] get_active_markets(%s) failed: %s", asset, e)
            cache["errors"].append(f"{asset}: {e}")
            continue

        for m in markets:
            ticker = m.get("ticker", "")
            if not ticker:
                continue
            entry: dict = {"asset": asset, "ticker": ticker}
            try:
                ob = get_market_orderbook(ticker)
                yes_levels = ob.get("yes", [])
                no_levels  = ob.get("no",  [])
                best_yes  = max((p for p, _ in yes_levels), default=0)
                best_no   = max((p for p, _ in no_levels),  default=0)
                entry["yes_bid"]     = best_yes
                entry["yes_ask"]     = 100 - best_no
                entry["spread"]      = max(0, (100 - best_no) - best_yes)
                entry["depth_yes"]   = sum(s for _, s in yes_levels[:3])
                entry["depth_no"]    = sum(s for _, s in no_levels[:3])
            except Exception as e:
                entry["orderbook_error"] = str(e)

            try:
                mkt = get_market(ticker)
                entry["open_interest"] = mkt.get("open_interest", 0)
                entry["volume"]        = mkt.get("volume", 0)
                entry["close_time"]    = mkt.get("close_time", "")
            except Exception as e:
                entry["market_error"] = str(e)

            cache["markets"][ticker] = entry

    if not dry_run:
        out = _LOGS / f"microstructure_cache_{today}.json"
        _LOGS.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        log.info("[phase6] Microstructure written to %s (%d tickers)", out, len(cache["markets"]))

    return cache


# ── Phase 7: Generate pre-open watchlist ──────────────────────────────────────

def _generate_watchlist(
    audit_report:    dict,
    setup_summary:   dict,
    micro_cache:     dict,
    today:           str,
    dry_run:         bool,
) -> dict:
    """
    Rank candidate markets for tomorrow's session.  Priority: spread quality,
    volume, setup history, OI.  Output to logs/preopen_watchlist_{today}.json.
    """
    log.info("[phase7] Generating pre-open watchlist")
    candidates: list[dict] = []

    for ticker, m in micro_cache.get("markets", {}).items():
        if m.get("orderbook_error") or m.get("market_error"):
            continue
        spread   = m.get("spread", 99)
        depth_y  = m.get("depth_yes", 0)
        depth_n  = m.get("depth_no",  0)
        oi       = m.get("open_interest", 0)
        volume   = m.get("volume", 0)
        asset    = m.get("asset", "BTC")

        # Simple priority score (lower spread = better)
        score = (
            max(0, 15.0 - spread) * 2.0   # spread (0-30)
            + min(depth_y + depth_n, 100) * 0.3   # depth (0-30)
            + min(oi,     500) * 0.02   # OI (0-10)
            + min(volume, 200) * 0.05   # volume (0-10)
        )

        candidates.append({
            "ticker":       ticker,
            "asset":        asset,
            "spread":       spread,
            "depth_yes":    depth_y,
            "depth_no":     depth_n,
            "open_interest": oi,
            "volume":       volume,
            "priority_score": round(score, 2),
        })

    # Sort by priority_score descending
    candidates.sort(key=lambda c: c["priority_score"], reverse=True)
    top20 = candidates[:20]

    watchlist = {
        "date":           today,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "setup_summary":  setup_summary,
        "candidates":     top20,
        "total_markets_scanned": len(candidates),
    }

    if not dry_run:
        out = _LOGS / f"preopen_watchlist_{today}.json"
        _LOGS.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(watchlist, indent=2), encoding="utf-8")
        log.info("[phase7] Watchlist written to %s (%d candidates)", out, len(top20))

    return watchlist


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Night-cycle maintenance runner")
    parser.add_argument("--dry-run",    action="store_true", help="Log only, write no files")
    parser.add_argument("--skip-train", action="store_true", help="Skip neural model retrain")
    args = parser.parse_args()

    today     = date.today().isoformat()
    t_start   = time.time()
    full_report: dict = {
        "date":       today,
        "dry_run":    args.dry_run,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "phases":     {},
        "errors":     [],
    }

    log.info("=" * 60)
    log.info("Night cycle starting  date=%s  dry_run=%s", today, args.dry_run)
    log.info("=" * 60)

    # Phase 1 — audit
    try:
        audit = _run_audit(today, args.dry_run)
        full_report["phases"]["audit"] = {
            "settlements": len(audit.get("settlements", [])),
            "total_pnl":   round(audit.get("total_pnl", 0), 4),
        }
    except Exception as e:
        log.error("[phase1] FATAL: %s", e)
        full_report["errors"].append(f"phase1: {e}")
        audit = {}

    # Phase 2 — append training data
    try:
        n_rows = _append_training_data(audit, args.dry_run)
        full_report["phases"]["append_training_data"] = {"rows_appended": n_rows}
    except Exception as e:
        log.error("[phase2] FATAL: %s", e)
        full_report["errors"].append(f"phase2: {e}")

    # Phase 3 — retrain
    try:
        ok = _retrain_model(args.dry_run, args.skip_train)
        full_report["phases"]["retrain"] = {"success": ok}
    except Exception as e:
        log.error("[phase3] FATAL: %s", e)
        full_report["errors"].append(f"phase3: {e}")

    # Phase 4 — recalibrate
    try:
        ok = _rebuild_calibrators(args.dry_run)
        full_report["phases"]["calibrate"] = {"success": ok}
    except Exception as e:
        log.error("[phase4] FATAL: %s", e)
        full_report["errors"].append(f"phase4: {e}")

    # Phase 5 — setup expectancy
    try:
        setup_summary = _rebuild_setup_expectancy(args.dry_run)
        full_report["phases"]["setup_expectancy"] = setup_summary
    except Exception as e:
        log.error("[phase5] FATAL: %s", e)
        full_report["errors"].append(f"phase5: {e}")
        setup_summary = {}

    # Phase 6 — microstructure cache
    try:
        micro = _cache_microstructure(today, args.dry_run)
        full_report["phases"]["microstructure"] = {
            "markets_cached": len(micro.get("markets", {}))
        }
    except Exception as e:
        log.error("[phase6] FATAL: %s", e)
        full_report["errors"].append(f"phase6: {e}")
        micro = {}

    # Phase 7 — watchlist
    try:
        watchlist = _generate_watchlist(audit, setup_summary, micro, today, args.dry_run)
        full_report["phases"]["watchlist"] = {"candidates": len(watchlist.get("candidates", []))}
    except Exception as e:
        log.error("[phase7] FATAL: %s", e)
        full_report["errors"].append(f"phase7: {e}")

    # Phase 8 — RAG ingestion + CLV close-updates
    try:
        from scripts.rag_ingest import ingest_settled_trades as _rag_ingest
        from engine.clv_tracker import CLVTracker as _CLVTracker
        _clv = _CLVTracker()
        n_ingested = 0
        if not args.dry_run:
            n_ingested = _rag_ingest(n_days_back=1)
        # Update CLV closing prices from settlement data in the audit
        _n_clv = 0
        for _trade in audit.get("settlements", []):
            _tkr = _trade.get("ticker", "")
            _close_p = _trade.get("close_price_cents") or _trade.get("settlement_price_cents")
            if _tkr and _close_p is not None:
                _n_clv += _clv.record_close(_tkr, float(_close_p))
        full_report["phases"]["rag_clv"] = {
            "docs_ingested": n_ingested,
            "clv_updated":   _n_clv,
        }
        log.info("[phase8] RAG ingested %d docs, CLV updated %d records", n_ingested, _n_clv)
    except Exception as e:
        log.error("[phase8] RAG/CLV phase failed: %s", e)
        full_report["errors"].append(f"phase8: {e}")

    # Write master report
    elapsed = round(time.time() - t_start, 1)
    full_report["finished_at"] = datetime.now(timezone.utc).isoformat()
    full_report["elapsed_secs"] = elapsed

    if not args.dry_run:
        _LOGS.mkdir(parents=True, exist_ok=True)
        out = _LOGS / f"night_cycle_{today}.json"
        out.write_text(json.dumps(full_report, indent=2), encoding="utf-8")
        log.info("Master report written to %s", out)

    n_errors = len(full_report["errors"])
    log.info("Night cycle done in %.1f s — %d errors", elapsed, n_errors)
    sys.exit(0 if n_errors == 0 else 1)


if __name__ == "__main__":
    main()
