"""
tracker.py — Persistent trade tracking, P&L, and performance analytics
=======================================================================
Central source of truth for all bot trades.  Records:
  - Every pick the model generates (with full signal snapshot)
  - Every execution attempt (placed / rejected / dry-run)
  - Every settlement result (win / loss / push)
  - Rolling performance metrics

Storage: logs/trade_history.jsonl  (append-only, one JSON object per line)
         logs/performance.json    (rolling analytics, rebuilt on read)
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).parent.parent
_HISTORY_PATH = _PROJECT_ROOT / "logs" / "trade_history.jsonl"
_PERF_PATH    = _PROJECT_ROOT / "logs" / "performance.json"
_SIGNAL_PATH  = _PROJECT_ROOT / "logs" / "signal_log.jsonl"

# ── Trade record helpers ────────────────────────────────────────────────────

def record_pick(pick: dict, momentum: dict, bankroll: float, verdict: str = "WAIT"):
    """Log a model pick with full signal snapshot.  Called every time the model
    surfaces a pick, whether it fires or waits."""
    meta = pick.get("intraday_meta", {})
    entry = {
        "type":       "pick",
        "ts":         datetime.now(timezone.utc).isoformat(),
        "ticker":     pick.get("market", ""),
        "asset":      meta.get("asset", ""),
        "side":       pick.get("side", ""),
        "edge_pct":   round(pick.get("edge_pct", 0), 2),
        "ev_pct":     round(pick.get("ev_pct", 0), 2),
        "our_prob":   round(pick.get("our_prob", 0), 2),
        "implied":    round(pick.get("implied_prob", 0), 2),
        "confidence": meta.get("confidence", 0),
        "gap_pct":    round(meta.get("gap_pct", 0), 5),
        "mom_5m":     round(meta.get("mom_5m_pct", 0), 4),
        "mom_15m":    round(meta.get("mom_15m_pct", 0), 4),
        "trend":      meta.get("trend", ""),
        "min_left":   round(pick.get("minutes_remaining", 0), 1),
        "stake":      round(pick.get("recommended_stake", 0), 2),
        "bankroll":   round(bankroll, 2),
        "verdict":    verdict,  # WAIT, FIRE, SKIP, CACHE_HIT
    }
    _append(_HISTORY_PATH, entry)
    return entry


def record_execution(pick: dict, exec_result: dict, bankroll: float):
    """Log an execution attempt (PLACED, DRY_RUN, SKIP, ERROR)."""
    meta = pick.get("intraday_meta", {})
    entry = {
        "type":        "execution",
        "ts":          datetime.now(timezone.utc).isoformat(),
        "ticker":      pick.get("market", ""),
        "asset":       meta.get("asset", ""),
        "side":        pick.get("side", ""),
        "status":      exec_result.get("status", "UNKNOWN"),
        "reason":      exec_result.get("reason", ""),
        "order_id":    exec_result.get("order_id", ""),
        "contracts":   exec_result.get("contracts", 0),
        "price_cents": exec_result.get("price_cents", 0),
        "spend_usd":   round(exec_result.get("spend_usd", 0), 2),
        "edge_pct":    round(pick.get("edge_pct", 0), 2),
        "our_prob":    round(pick.get("our_prob", 0), 2),
        "bankroll":    round(bankroll, 2),
    }
    _append(_HISTORY_PATH, entry)
    return entry


def record_settlement(ticker: str, side: str, won: bool, payout: float,
                      cost: float, fees: float = 0.0):
    """Log a settlement (market resolved)."""
    entry = {
        "type":    "settlement",
        "ts":      datetime.now(timezone.utc).isoformat(),
        "ticker":  ticker,
        "side":    side,
        "won":     won,
        "payout":  round(payout, 2),
        "cost":    round(cost, 2),
        "fees":    round(fees, 3),
        "net":     round(payout - cost - fees, 2),
    }
    _append(_HISTORY_PATH, entry)
    return entry


def record_signal_snapshot(momentum: dict, markets: list, bankroll: float):
    """Log a full signal snapshot for later backtesting."""
    entry = {
        "ts":       datetime.now(timezone.utc).isoformat(),
        "bankroll": round(bankroll, 2),
        "n_markets": len(markets),
        "signals":  {},
    }
    for asset, sig in momentum.items():
        entry["signals"][asset] = {
            "price":    sig.get("current", 0),
            "mom_5m":   round(sig.get("mom_5m", 0), 6),
            "mom_15m":  round(sig.get("mom_15m", 0), 6),
            "mom_1m":   round(sig.get("mom_1m", 0), 6),
            "vol_5m":   round(sig.get("realized_vol", 0), 6),
            "trend":    sig.get("trend", ""),
        }
    _append(_SIGNAL_PATH, entry)


# ── History reading ─────────────────────────────────────────────────────────

def load_history(days: int = 7) -> list[dict]:
    """Load trade history, optionally filtering to last N days."""
    if not _HISTORY_PATH.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    entries = []
    for line in _HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get("ts", "") >= cutoff:
                entries.append(obj)
        except json.JSONDecodeError:
            continue
    return entries


def load_executions(days: int = 7) -> list[dict]:
    """Load only execution records."""
    return [e for e in load_history(days) if e.get("type") == "execution"]


def load_settlements(days: int = 7) -> list[dict]:
    """Load only settlement records."""
    return [e for e in load_history(days) if e.get("type") == "settlement"]


def load_picks(days: int = 7) -> list[dict]:
    """Load only pick records."""
    return [e for e in load_history(days) if e.get("type") == "pick"]


# ── Performance analytics ───────────────────────────────────────────────────

def compute_performance(days: int = 7) -> dict:
    """Compute rolling performance metrics from trade history."""
    history = load_history(days)
    picks = [e for e in history if e["type"] == "pick"]
    execs = [e for e in history if e["type"] == "execution"]
    settlements = [e for e in history if e["type"] == "settlement"]

    placed = [e for e in execs if e["status"] == "PLACED"]
    rejected = [e for e in execs if e["status"] not in ("PLACED", "DRY_RUN")]
    dry_runs = [e for e in execs if e["status"] == "DRY_RUN"]

    wins = [s for s in settlements if s.get("won")]
    losses = [s for s in settlements if not s.get("won")]
    total_cost = sum(s.get("cost", 0) for s in settlements)
    total_payout = sum(s.get("payout", 0) for s in settlements)
    total_fees = sum(s.get("fees", 0) for s in settlements)
    total_net = total_payout - total_cost - total_fees

    # Edge accuracy: compare predicted edge vs actual outcome
    edge_accuracy = _compute_edge_accuracy(picks, settlements)

    # Win rate by confidence bucket
    conf_buckets = _compute_confidence_buckets(picks, settlements)

    # Asset breakdown
    asset_stats = _compute_asset_stats(picks, settlements)

    # Streak tracking
    streak = _compute_streak(settlements)

    # Hourly performance
    hourly = _compute_hourly_stats(settlements)

    perf = {
        "updated":        datetime.now(timezone.utc).isoformat(),
        "period_days":    days,
        "total_picks":    len(picks),
        "total_placed":   len(placed),
        "total_rejected": len(rejected),
        "total_dry_runs": len(dry_runs),
        "total_settled":  len(settlements),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(len(wins) / max(len(settlements), 1) * 100, 1),
        "total_wagered":  round(total_cost, 2),
        "total_payout":   round(total_payout, 2),
        "total_fees":     round(total_fees, 3),
        "net_pnl":        round(total_net, 2),
        "roi_pct":        round(total_net / max(total_cost, 0.01) * 100, 1),
        "avg_edge":       round(sum(p.get("edge_pct", 0) for p in picks) / max(len(picks), 1), 2),
        "avg_confidence": round(sum(p.get("confidence", 0) for p in picks) / max(len(picks), 1), 1),
        "edge_accuracy":  edge_accuracy,
        "conf_buckets":   conf_buckets,
        "asset_stats":    asset_stats,
        "streak":         streak,
        "hourly_stats":   hourly,
    }

    # Persist
    _PERF_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PERF_PATH.write_text(json.dumps(perf, indent=2), encoding="utf-8")
    return perf


def _compute_edge_accuracy(picks: list, settlements: list) -> dict:
    """How accurate are our edge predictions?"""
    # Map ticker → best pick edge
    pick_edges = {}
    for p in picks:
        t = p.get("ticker", "")
        if t and p.get("edge_pct", 0) > pick_edges.get(t, {}).get("edge_pct", 0):
            pick_edges[t] = p

    matched = 0
    correct = 0
    edge_errors = []
    for s in settlements:
        t = s.get("ticker", "")
        if t in pick_edges:
            matched += 1
            pred_edge = pick_edges[t].get("edge_pct", 0)
            actual = 100.0 if s.get("won") else -100.0  # simplified
            if (pred_edge > 0 and s.get("won")) or (pred_edge <= 0 and not s.get("won")):
                correct += 1
            edge_errors.append(abs(pred_edge))

    return {
        "matched_trades":  matched,
        "direction_correct": correct,
        "direction_pct":   round(correct / max(matched, 1) * 100, 1),
        "avg_predicted_edge": round(sum(edge_errors) / max(len(edge_errors), 1), 2),
    }


def _compute_confidence_buckets(picks: list, settlements: list) -> dict:
    """Win rate grouped by confidence score."""
    # Map ticker → settlement outcome
    outcomes = {}
    for s in settlements:
        outcomes[s.get("ticker", "")] = s.get("won", False)

    buckets = {"0-25": [0, 0], "25-50": [0, 0], "50-75": [0, 0], "75-100": [0, 0]}
    for p in picks:
        t = p.get("ticker", "")
        if t not in outcomes:
            continue
        conf = p.get("confidence", 0)
        if conf < 25:
            key = "0-25"
        elif conf < 50:
            key = "25-50"
        elif conf < 75:
            key = "50-75"
        else:
            key = "75-100"
        buckets[key][0] += 1  # total
        if outcomes[t]:
            buckets[key][1] += 1  # wins

    return {k: {"trades": v[0], "wins": v[1],
                "win_rate": round(v[1] / max(v[0], 1) * 100, 1)}
            for k, v in buckets.items()}


def _compute_asset_stats(picks: list, settlements: list) -> dict:
    """Performance breakdown by asset."""
    outcomes = {}
    for s in settlements:
        outcomes[s.get("ticker", "")] = s

    asset_data: dict[str, dict] = {}
    for p in picks:
        asset = p.get("asset", "?")
        if asset not in asset_data:
            asset_data[asset] = {"picks": 0, "wins": 0, "losses": 0,
                                  "net": 0.0, "edges": []}
        asset_data[asset]["picks"] += 1
        asset_data[asset]["edges"].append(p.get("edge_pct", 0))

        t = p.get("ticker", "")
        if t in outcomes:
            s = outcomes[t]
            if s.get("won"):
                asset_data[asset]["wins"] += 1
            else:
                asset_data[asset]["losses"] += 1
            asset_data[asset]["net"] += s.get("net", 0)

    result = {}
    for asset, d in asset_data.items():
        total = d["wins"] + d["losses"]
        result[asset] = {
            "picks":     d["picks"],
            "trades":    total,
            "wins":      d["wins"],
            "losses":    d["losses"],
            "win_rate":  round(d["wins"] / max(total, 1) * 100, 1),
            "net_pnl":   round(d["net"], 2),
            "avg_edge":  round(sum(d["edges"]) / max(len(d["edges"]), 1), 2),
        }
    return result


def _compute_streak(settlements: list) -> dict:
    """Current and max win/loss streaks."""
    if not settlements:
        return {"current": 0, "current_type": "none", "max_win": 0, "max_loss": 0}

    sorted_s = sorted(settlements, key=lambda x: x.get("ts", ""))
    current = 0
    current_type = "none"
    max_win = 0
    max_loss = 0
    streak = 0
    last_won = None

    for s in sorted_s:
        won = s.get("won", False)
        if won == last_won:
            streak += 1
        else:
            streak = 1
            last_won = won
        if won:
            max_win = max(max_win, streak)
        else:
            max_loss = max(max_loss, streak)

    return {
        "current": streak,
        "current_type": "win" if last_won else "loss",
        "max_win": max_win,
        "max_loss": max_loss,
    }


def _compute_hourly_stats(settlements: list) -> dict:
    """Win rate by hour of day (UTC)."""
    hourly: dict[int, list] = {h: [0, 0] for h in range(24)}
    for s in settlements:
        try:
            ts = datetime.fromisoformat(s["ts"].replace("Z", "+00:00"))
            h = ts.hour
            hourly[h][0] += 1
            if s.get("won"):
                hourly[h][1] += 1
        except (KeyError, ValueError):
            continue

    return {str(h): {"trades": v[0], "wins": v[1],
                      "win_rate": round(v[1] / max(v[0], 1) * 100, 1)}
            for h, v in hourly.items() if v[0] > 0}


# ── Tuning suggestions ─────────────────────────────────────────────────────

def suggest_tuning(days: int = 7) -> list[str]:
    """Analyze performance and return tuning suggestions."""
    perf = compute_performance(days)
    suggestions = []

    # Not enough data
    if perf["total_settled"] < 5:
        suggestions.append(f"Only {perf['total_settled']} settled trades — need ≥5 for meaningful analysis")
        return suggestions

    # Win rate check
    wr = perf["win_rate"]
    if wr < 45:
        suggestions.append(f"Win rate {wr}% is low — consider raising MIN_EDGE from 5% to 7%")
    elif wr > 70:
        suggestions.append(f"Win rate {wr}% is strong — could lower MIN_EDGE to 4% to catch more setups")

    # ROI check
    roi = perf["roi_pct"]
    if roi < -10:
        suggestions.append(f"ROI is {roi}% — tighten filters: raise MIN_GAP_PCT or MIN_EDGE")
    elif roi > 20:
        suggestions.append(f"ROI is {roi}% — system is printing.  Consider increasing Kelly from 10% to 12%")

    # Confidence calibration
    buckets = perf.get("conf_buckets", {})
    low_conf = buckets.get("0-25", {})
    high_conf = buckets.get("75-100", {})
    if low_conf.get("trades", 0) >= 3 and low_conf.get("win_rate", 50) < 40:
        suggestions.append("Low-confidence trades (0-25) underperforming — consider MIN_CONFIDENCE=30 filter")
    if high_conf.get("trades", 0) >= 3 and high_conf.get("win_rate", 50) > 65:
        suggestions.append("High-confidence trades (75-100) outperforming — increase stake for conf>75")

    # Asset performance
    for asset, stats in perf.get("asset_stats", {}).items():
        if stats["trades"] >= 3 and stats["win_rate"] < 35:
            suggestions.append(f"{asset} win rate only {stats['win_rate']}% over {stats['trades']} trades — consider excluding")
        if stats["trades"] >= 3 and stats["win_rate"] > 70:
            suggestions.append(f"{asset} win rate {stats['win_rate']}% — strong asset, prioritize it")

    # Streak warning
    streak = perf.get("streak", {})
    if streak.get("current_type") == "loss" and streak.get("current", 0) >= 4:
        suggestions.append(f"On a {streak['current']}-loss streak — consider pausing or reducing stake 50%")

    # Rejection rate
    total_attempts = perf["total_placed"] + perf["total_rejected"]
    if total_attempts > 0:
        reject_rate = perf["total_rejected"] / total_attempts * 100
        if reject_rate > 50:
            suggestions.append(f"{reject_rate:.0f}% of execution attempts rejected — check executor limits vs model thresholds")

    if not suggestions:
        suggestions.append("No issues detected — system performing within expected parameters")

    return suggestions


# ── Internal ────────────────────────────────────────────────────────────────

def _append(path: Path, entry: dict):
    """Append a JSON line to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
