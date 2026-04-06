"""
Analytics Engine
================
Performance attribution, CLV tracking, edge bucket analysis,
ROI by segment, and Sharpe ratio calculations.

Beats Pikkit (CLV), Bet-Analytix (edge buckets, ROI), and
provides agent-level attribution unavailable in any commercial tool.
"""
from __future__ import annotations
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional


# ── Helpers ────────────────────────────────────────────────────────────────

def _edge_bucket(edge_pct: float) -> str:
    if edge_pct < 3:
        return "<3%"
    if edge_pct < 5:
        return "3-5%"
    if edge_pct < 8:
        return "5-8%"
    if edge_pct < 12:
        return "8-12%"
    return "12%+"


def _period_filter(bets: list, days: Optional[int]) -> list:
    if days is None:
        return bets
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    return [b for b in bets if b.get("placed_at", "9999") >= cutoff]


def _segment_stats(subset: list) -> dict:
    settled = [b for b in subset if b.get("result") in ("win", "loss", "push")]
    wins    = [b for b in settled if b.get("result") == "win"]
    losses  = [b for b in settled if b.get("result") == "loss"]
    wagered = sum(b.get("stake",   0) for b in settled)
    pnl     = sum(b.get("pnl",    0) for b in settled if b.get("pnl") is not None)
    clv_vals  = [b["clv"]      for b in settled if b.get("clv")      is not None]
    edge_vals = [b["edge_pct"] for b in subset  if b.get("edge_pct") is not None]
    return {
        "bets":      len(subset),
        "settled":   len(settled),
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  round(len(wins) / len(settled) * 100, 1) if settled else 0.0,
        "roi_pct":   round(pnl / wagered * 100, 2) if wagered > 0 else 0.0,
        "profit":    round(pnl, 2),
        "wagered":   round(wagered, 2),
        "clv_avg":   round(statistics.mean(clv_vals),  3) if clv_vals  else 0.0,
        "edge_avg":  round(statistics.mean(edge_vals), 2) if edge_vals else 0.0,
    }


def _sharpe(bets: list) -> float:
    """Betting Sharpe: mean(daily P&L) / std(daily P&L) * sqrt(252)."""
    daily: Dict[str, float] = defaultdict(float)
    for b in bets:
        if b.get("pnl") is not None and b.get("placed_at"):
            daily[b["placed_at"][:10]] += b["pnl"]
    if len(daily) < 2:
        return 0.0
    vals = list(daily.values())
    try:
        std = statistics.stdev(vals)
    except statistics.StatisticsError:
        return 0.0
    if std == 0:
        return 0.0
    return round(statistics.mean(vals) / std * math.sqrt(252), 2)


def _clean_sport(sport: str) -> str:
    return (sport.lower()
            .replace("americanfootball_", "")
            .replace("basketball_", "")
            .replace("baseball_", "")
            .replace("icehockey_", ""))


# ── Main Builder ───────────────────────────────────────────────────────────

def build_performance_report(raw_bets: list) -> dict:
    """
    Build full performance analytics from raw bet list.
    raw_bets items must have keys matching the Bet dataclass fields.
    """
    bets = []
    for b in raw_bets:
        edge = b.get("edge_pct", 0) or 0
        bets.append({
            "sport":     _clean_sport(b.get("sport", "other")),
            "market":    b.get("market", "moneyline"),
            "agent":     b.get("strategy", b.get("agent", "general")),
            "stake":     b.get("stake",    0) or 0,
            "pnl":       b.get("pnl"),
            "result":    b.get("result"),
            "edge_pct":  edge,
            "clv":       b.get("closing_odds"),
            "placed_at": b.get("placed_at", datetime.utcnow().isoformat()),
        })

    settled = [b for b in bets if b.get("result") in ("win", "loss", "push")]
    wins    = [b for b in settled if b["result"] == "win"]
    wagered = sum(b.get("stake", 0) for b in settled)
    pnl     = sum(b.get("pnl",   0) for b in settled if b.get("pnl") is not None)
    clv_vals  = [b["clv"]      for b in settled if b.get("clv")  is not None]
    edge_vals = [b["edge_pct"] for b in bets    if b.get("edge_pct") is not None]

    # Segmented breakdowns
    by_sport:  Dict[str, dict] = {}
    by_market: Dict[str, dict] = {}
    by_agent:  Dict[str, dict] = {}
    by_bucket: Dict[str, dict] = {}

    for seg_field, seg_dict in [("sport", by_sport), ("market", by_market), ("agent", by_agent)]:
        keys = {b.get(seg_field, "unknown") for b in bets}
        for k in keys:
            subset = [b for b in bets if b.get(seg_field) == k]
            seg_dict[k] = _segment_stats(subset)

    buckets: Dict[str, list] = defaultdict(list)
    for b in bets:
        buckets[_edge_bucket(b.get("edge_pct", 0))].append(b)
    for bucket, subset in buckets.items():
        by_bucket[bucket] = _segment_stats(subset)

    periods: Dict[str, dict] = {}
    for label, days in [("today", 1), ("7d", 7), ("30d", 30), ("all", None)]:
        periods[label] = _segment_stats(_period_filter(bets, days))

    return {
        "total_bets":      len(bets),
        "settled":         len(settled),
        "wins":            len(wins),
        "losses":          len([b for b in settled if b["result"] == "loss"]),
        "pushes":          len([b for b in settled if b["result"] == "push"]),
        "win_rate":        round(len(wins) / len(settled) * 100, 1) if settled else 0.0,
        "roi_pct":         round(pnl / wagered * 100, 2) if wagered > 0 else 0.0,
        "total_wagered":   round(wagered, 2),
        "total_profit":    round(pnl, 2),
        "clv_avg":         round(statistics.mean(clv_vals),  3) if clv_vals  else 0.0,
        "edge_avg":        round(statistics.mean(edge_vals), 2) if edge_vals else 0.0,
        "sharpe":          _sharpe(bets),
        "by_sport":        by_sport,
        "by_market":       by_market,
        "by_agent":        by_agent,
        "by_edge_bucket":  dict(sorted(by_bucket.items())),
        "periods":         periods,
    }


# ── Middle Finder ──────────────────────────────────────────────────────────

def _american_to_decimal(odds: int) -> float:
    if odds > 0:
        return odds / 100 + 1
    return 100 / abs(odds) + 1


def find_middles(events: list) -> list:
    """
    Scan events for middle opportunities.
    Each event: { id, home, away, books: { book_name: { spreads: [{side, line, odds}] } } }
    Returns list of middles with window, max_win, guaranteed_loss, ev_pct.
    """
    middles = []
    for ev in events:
        books = ev.get("books", {})
        # Collect all spread lines per side
        home_lines: list[dict] = []
        away_lines: list[dict] = []
        for bk_name, bk_data in books.items():
            for line in bk_data.get("spreads", []):
                entry = {"book": bk_name, "odds": line["odds"], "line": line["line"]}
                if line.get("side") == "home":
                    home_lines.append(entry)
                else:
                    away_lines.append(entry)

        # Find cross-book middles: home + X vs away + Y where X > Y (window > 0)
        for h in home_lines:
            for a in away_lines:
                # Home at a larger number than away: e.g. home +4.5 vs away -2.5
                # Middle window = h.line - abs(a.line) for same-side direction
                window = h["line"] + a["line"]  # both signed
                if window > 0:
                    stake = 100
                    h_dec = _american_to_decimal(h["odds"])
                    a_dec = _american_to_decimal(a["odds"])
                    total_risk = stake * 2
                    max_win    = stake * (h_dec - 1) + stake * (a_dec - 1)
                    # Worst case: one leg loses, one might push
                    guaranteed_loss = -(stake / h_dec + stake / a_dec - total_risk)
                    ev_pct = (window * 0.5 / 100) * 2 * 100  # rough approximation

                    middles.append({
                        "event":           ev.get("name", ""),
                        "sport":           ev.get("sport", ""),
                        "leg_a":           {"side": f"{ev.get('home','')} +{h['line']}", "odds": h["odds"], "book": h["book"], "stake": stake},
                        "leg_b":           {"side": f"{ev.get('away','')} {a['line']:+.1f}", "odds": a["odds"], "book": a["book"], "stake": stake},
                        "window":          round(window, 1),
                        "max_win":         round(max_win, 2),
                        "guaranteed_loss": round(guaranteed_loss, 2),
                        "ev_pct":          round(max(0.5, ev_pct), 1),
                    })
    return middles
