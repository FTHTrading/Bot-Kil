"""
engine/session_context.py — Dynamic session context builder
============================================================
Assembles a live intelligence briefing injected into every agent session.
Reads from:
  - logs/daily_state.json          : today's spend, cooldowns, reopen mode
  - logs/performance.json          : rolling ROI, win-rate, expectancy
  - logs/setup_stats.json          : per-setup expectancy + active/disabled status
  - logs/trade_history.jsonl       : last N bets for recency context
  - logs/preopen_watchlist_*.json  : today's highest-priority markets
  - logs/clv_history.jsonl         : closing line value stats (if present)

Output: multi-section text block injected into run_agent() user message so
the LLM brain starts each session with full situational awareness.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ROOT  = Path(__file__).parent.parent
_LOGS  = _ROOT / "logs"

# How many recent bets to surface in the context block
_RECENT_BET_LIMIT = 5


# ── Readers ───────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _load_jsonl(path: Path, max_lines: int = 500) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
            if len(out) >= max_lines:
                break
    except Exception:
        pass
    return out   # newest-first after the reverse iteration


def _today_watchlist() -> dict:
    today = date.today().isoformat()
    path  = _LOGS / f"preopen_watchlist_{today}.json"
    return _load_json(path)


# ── Section builders ──────────────────────────────────────────────────────────

def _daily_state_section(state: dict) -> list[str]:
    lines: list[str] = []
    spend   = float(state.get("daily_spend", 0.0))
    cd      = state.get("cooldowns", {})
    reopen  = state.get("reopen_mode", False)
    lines.append(f"  Daily spend so far: ${spend:.2f}")

    now_ts = datetime.now(timezone.utc).timestamp()
    active_cds: list[str] = []
    for asset, exp in cd.items():
        rem = (float(exp) - now_ts) / 60
        if rem > 0:
            active_cds.append(f"{asset} ({rem:.0f} min)")
    if active_cds:
        lines.append(f"  Active cooldowns: {', '.join(active_cds)}")
    else:
        lines.append("  Active cooldowns: none")

    if reopen:
        no_trade = float(state.get("reopen_no_trade_until", 0.0))
        rem_nt   = max(0.0, (no_trade - now_ts) / 60)
        lines.append(f"  Reopen mode: ACTIVE (no-trade {rem_nt:.0f} min remaining)")
    return lines


def _performance_section(perf: dict) -> list[str]:
    if not perf:
        return ["  Performance: no data yet"]
    lines: list[str] = []
    roi   = perf.get("roi_pct",     perf.get("roi",   None))
    wr    = perf.get("win_rate",    None)
    bets  = perf.get("bets_placed", perf.get("n_bets", None))
    pnl   = perf.get("total_pnl",   perf.get("pnl",   None))
    clv   = perf.get("clv_avg",     None)
    if roi  is not None: lines.append(f"  ROI: {roi:+.1f}%")
    if wr   is not None: lines.append(f"  Win rate: {wr*100 if wr < 1.5 else wr:.0f}%")
    if bets is not None: lines.append(f"  Total bets placed: {bets}")
    if pnl  is not None: lines.append(f"  Cumulative P&L: ${float(pnl):+.2f}")
    if clv  is not None: lines.append(f"  Avg CLV: {float(clv):+.2f}¢  (>0 = beating closing line)")
    return lines if lines else ["  Performance: no data yet"]


def _setup_section(stats: dict) -> list[str]:
    if not stats:
        return []
    lines: list[str] = ["  Setup expectancy (from settled trades):"]
    setups = stats.get("setups", stats)   # handle both wrapped and flat formats
    for name, s in setups.items():
        if name == "metadata":
            continue
        if not isinstance(s, dict):
            continue
        n     = s.get("n_bets", 0)
        exp   = s.get("expectancy", 0.0)
        wr    = s.get("win_rate", 0.0)
        flag_map = {
            "full":       "✓ FULL SIZE",
            "half":       "½ HALF SIZE",
            "watch_only": "⚠ WATCH ONLY (disabled)",
        }
        status_key = s.get("status", "full")
        flag       = flag_map.get(status_key, status_key.upper())
        if n >= 1:
            lines.append(
                f"    {name}: n={n}  exp={exp:+.3f}  wr={wr*100:.0f}%  → {flag}"
            )
    return lines


def _recent_bets_section(records: list[dict]) -> list[str]:
    # records are newest-first; filter for placed/executed rows
    placed = [
        r for r in records
        if r.get("type") in ("execution", "placed") and
           r.get("status") in ("PLACED", "DRY_RUN", "placed")
    ][:_RECENT_BET_LIMIT]

    if not placed:
        return ["  Recent bets: none recorded yet"]

    lines: list[str] = [f"  Last {len(placed)} bets (newest first):"]
    for b in placed:
        ts      = b.get("ts", "")[:16]
        ticker  = b.get("ticker", "?")
        asset   = b.get("asset",  "?")
        side    = b.get("side",   "?")
        edge    = b.get("edge_pct", b.get("entry_meta", {}).get("edge_pct", 0))
        status  = b.get("status", "?")
        spend   = b.get("spend_usd", 0.0)
        lines.append(
            f"    [{ts}] {ticker} {side.upper()} "
            f"edge={edge:+.1f}% ${spend:.2f} → {status}"
        )
    return lines


def _recent_settlements_section(records: list[dict]) -> list[str]:
    settled = [r for r in records if r.get("type") == "settlement"][:5]
    if not settled:
        return []
    lines = ["  Recent settlements:"]
    for s in settled:
        ticker = s.get("ticker", "?")
        pnl    = s.get("pnl", 0.0)
        result = s.get("result", "?")
        sign   = "WIN" if float(pnl) > 0 else "LOSS"
        lines.append(f"    {ticker}: {sign} ${float(pnl):+.4f}  result={result}")
    return lines


def _watchlist_section(wl: dict) -> list[str]:
    candidates = wl.get("candidates", [])[:5]
    if not candidates:
        return []
    lines: list[str] = ["  Pre-open watchlist (top priority markets):"]
    for c in candidates:
        ticker = c.get("ticker", "?")
        spread = c.get("spread", "?")
        oi     = c.get("open_interest", "?")
        score  = c.get("priority_score", 0)
        lines.append(f"    {ticker}  spread={spread}¢  OI={oi}  score={score:.1f}")
    return lines


def _clv_section() -> list[str]:
    """Load CLV history and surface aggregate stats."""
    clv_path = _LOGS / "clv_history.jsonl"
    if not clv_path.exists():
        return []
    records = _load_jsonl(clv_path, max_lines=200)
    closed  = [r for r in records if r.get("close_price_cents") is not None]
    if len(closed) < 3:
        return []
    clv_vals = [
        float(r["close_price_cents"]) - float(r["entry_price_cents"])
        if r.get("side", "yes") == "yes" else
        float(r["entry_price_cents"]) - float(r["close_price_cents"])
        for r in closed
    ]
    avg_clv    = sum(clv_vals) / len(clv_vals)
    pct_pos    = sum(1 for v in clv_vals if v > 0) / len(clv_vals) * 100
    return [
        f"  CLV tracker (n={len(closed)}): avg_clv={avg_clv:+.2f}¢  "
        f"pct_positive={pct_pos:.0f}%  (target: avg>0, pct>55%)"
    ]


# ── Main entry point ──────────────────────────────────────────────────────────

def build_session_context(max_chars: int = 1800) -> str:
    """
    Build a live intelligence briefing as a multi-line string.
    Safe to call on every session — all I/O is best-effort, no exceptions raised.

    Parameters
    ----------
    max_chars : soft cap on output length to keep LLM context window clean.

    Returns
    -------
    Formatted multi-section string, or empty string if all sources are unavailable.
    """
    try:
        today   = date.today().isoformat()
        state   = _load_json(_LOGS / "daily_state.json")
        perf    = _load_json(_LOGS / "performance.json")
        stats   = _load_json(_LOGS / "setup_stats.json")
        history = _load_jsonl(_LOGS / "trade_history.jsonl", max_lines=100)
        wl      = _today_watchlist()

        # Guard: skip if state is from a different day
        if state.get("date") and state["date"] != today:
            state = {}

        sections: list[str] = []

        # ── Daily financials ──────────────────────────────────────────────────
        if state:
            sections.append("SESSION STATE:")
            sections.extend(_daily_state_section(state))

        # ── Rolling performance ───────────────────────────────────────────────
        if perf:
            sections.append("PERFORMANCE SUMMARY:")
            sections.extend(_performance_section(perf))

        # ── CLV ───────────────────────────────────────────────────────────────
        clv_lines = _clv_section()
        if clv_lines:
            sections.append("CLOSING LINE VALUE:")
            sections.extend(clv_lines)

        # ── Setup stats ───────────────────────────────────────────────────────
        setup_lines = _setup_section(stats)
        if setup_lines:
            sections.append("SETUP EXPECTANCY:")
            sections.extend(setup_lines)

        # ── Recent bets ───────────────────────────────────────────────────────
        sections.append("RECENT BETS:")
        sections.extend(_recent_bets_section(history))

        # ── Recent settlements ────────────────────────────────────────────────
        settle_lines = _recent_settlements_section(history)
        if settle_lines:
            sections.append("RECENT SETTLEMENTS:")
            sections.extend(settle_lines)

        # ── Watchlist ─────────────────────────────────────────────────────────
        wl_lines = _watchlist_section(wl)
        if wl_lines:
            sections.append(f"WATCHLIST ({today}):")
            sections.extend(wl_lines)

        if not sections:
            return ""

        header = f"═══ LIVE BRIEFING {datetime.now().strftime('%H:%M UTC')} ═══"
        footer = "═" * len(header)
        text   = f"{header}\n" + "\n".join(sections) + f"\n{footer}"

        # Trim if too long
        if len(text) > max_chars:
            text = text[:max_chars - 3] + "..."

        return text

    except Exception as exc:
        log.warning("[session_context] build failed (non-fatal): %s", exc)
        return ""
