"""
learning_tracker.py — SQLite-backed bet journal & strategy intelligence
=======================================================================
Every bet placed by any system (daily, intraday, research) is recorded here
with its FULL signal state.  After settlement the outcome is written back.
Over time this builds a database of:

  • Which strategies actually win (by ROI, win-rate, Sharpe)
  • Which signals correlate with wins vs losses
  • Real-time P&L, bankroll curve, drawdown stats
  • Per-timeframe and per-asset performance

The research_agent reads these stats at startup to weight its strategies.

DB:  db/learning.db   (auto-created, git-ignored logs dir equivalent)
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_PROJECT_ROOT, "db", "learning.db")


# ─── DB bootstrap ─────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS bets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    placed_at       TEXT    NOT NULL,           -- ISO-8601 UTC
    ticker          TEXT    NOT NULL,           -- Kalshi market ticker
    series          TEXT,                       -- e.g. KXBTC15M, KXCPI, KXEPL
    market_type     TEXT    NOT NULL,           -- crypto_15m | crypto_daily | econ | political | weather | sports
    timeframe       TEXT    NOT NULL DEFAULT 'daily', -- 15min | 1hr | 4hr | daily
    asset           TEXT,                       -- BTC, ETH, SOL, CPI, etc.
    side            TEXT    NOT NULL,           -- yes | no
    price_cents     INTEGER NOT NULL,           -- 1–99
    contracts       INTEGER NOT NULL DEFAULT 1,
    spend_usd       REAL    NOT NULL,
    our_prob        REAL    NOT NULL,           -- our modelled win probability
    market_prob     REAL    NOT NULL,           -- implied from price_cents/100
    edge_pct        REAL    NOT NULL,           -- our_prob - market_prob
    strategy        TEXT    NOT NULL DEFAULT 'unknown',  -- which strategy flagged it
    signals         TEXT,                       -- JSON blob of all signal values
    order_id        TEXT,                       -- Kalshi order UUID
    settled         INTEGER NOT NULL DEFAULT 0, -- 0=open, 1=settled
    won             INTEGER,                    -- 1=win, 0=loss, NULL=open
    pnl             REAL,                       -- actual P&L after settlement
    settled_at      TEXT,                       -- ISO-8601 UTC
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT    NOT NULL,
    ticker      TEXT    NOT NULL,
    yes_price   INTEGER,
    no_price    INTEGER,
    open_interest INTEGER,
    volume      INTEGER,
    minutes_remaining REAL
);

CREATE TABLE IF NOT EXISTS strategy_stats (
    strategy    TEXT PRIMARY KEY,
    market_type TEXT,
    wins        INTEGER NOT NULL DEFAULT 0,
    losses      INTEGER NOT NULL DEFAULT 0,
    total_wagered  REAL NOT NULL DEFAULT 0,
    total_pnl      REAL NOT NULL DEFAULT 0,
    last_updated   TEXT
);

CREATE TABLE IF NOT EXISTS signal_weights (
    signal_name     TEXT PRIMARY KEY,
    weight          REAL NOT NULL DEFAULT 1.0,  -- multiplier applied to edge estimate
    win_correlation REAL,                        -- Pearson r vs outcome
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS daily_summaries (
    date         TEXT PRIMARY KEY,  -- YYYY-MM-DD
    bets_placed  INTEGER DEFAULT 0,
    bets_won     INTEGER DEFAULT 0,
    bets_lost    INTEGER DEFAULT 0,
    bets_open    INTEGER DEFAULT 0,
    gross_wagered REAL   DEFAULT 0,
    gross_pnl    REAL    DEFAULT 0,
    roi_pct      REAL,
    bankroll_end REAL
);

CREATE INDEX IF NOT EXISTS idx_bets_ticker    ON bets(ticker);
CREATE INDEX IF NOT EXISTS idx_bets_strategy  ON bets(strategy);
CREATE INDEX IF NOT EXISTS idx_bets_settled   ON bets(settled);
CREATE INDEX IF NOT EXISTS idx_bets_placed_at ON bets(placed_at);
CREATE INDEX IF NOT EXISTS idx_snaps_ticker   ON market_snapshots(ticker);
"""


@contextmanager
def _db():
    """Thread-safe SQLite connection context manager."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── Core write operations ─────────────────────────────────────────────────────

def record_bet(
    ticker: str,
    side: str,
    price_cents: int,
    contracts: int,
    spend_usd: float,
    our_prob: float,
    edge_pct: float,
    strategy: str,
    signals: Optional[dict] = None,
    order_id: Optional[str] = None,
    market_type: str = "unknown",
    timeframe: str = "daily",
    series: str = None,
    asset: str = None,
    notes: str = None,
) -> int:
    """
    Record a placed bet.  Returns the new row id (bet_id).
    """
    now = datetime.now(timezone.utc).isoformat()
    market_prob = price_cents / 100.0
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO bets
               (placed_at, ticker, series, market_type, timeframe, asset,
                side, price_cents, contracts, spend_usd,
                our_prob, market_prob, edge_pct, strategy,
                signals, order_id, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now, ticker, series, market_type, timeframe, asset,
                side.lower(), price_cents, contracts, spend_usd,
                our_prob, market_prob, edge_pct, strategy,
                json.dumps(signals or {}), order_id, notes,
            ),
        )
        bet_id = cur.lastrowid

    # upsert into strategy_stats
    _touch_strategy(strategy, market_type, spend_usd)
    return bet_id


def record_outcome(bet_id: int, won: bool, pnl: float) -> None:
    """Write back the outcome after a market settles."""
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            """UPDATE bets SET settled=1, won=?, pnl=?, settled_at=?
               WHERE id=?""",
            (1 if won else 0, pnl, now, bet_id),
        )
        row = conn.execute("SELECT strategy, market_type FROM bets WHERE id=?", (bet_id,)).fetchone()

    if row:
        _update_strategy_stats(row["strategy"], row["market_type"], won, pnl)


def record_snapshot(
    ticker: str,
    yes_price: int,
    no_price: int,
    open_interest: int = 0,
    volume: int = 0,
    minutes_remaining: float = 0,
) -> None:
    """Capture a market price snapshot for historical analysis."""
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            """INSERT INTO market_snapshots
               (captured_at, ticker, yes_price, no_price, open_interest, volume, minutes_remaining)
               VALUES (?,?,?,?,?,?,?)""",
            (now, ticker, yes_price, no_price, open_interest, volume, minutes_remaining),
        )


# ─── Settlement scanner ────────────────────────────────────────────────────────

async def auto_settle_open_bets() -> list[dict]:
    """
    Query Kalshi API for all open bets that may have settled.
    Returns list of newly settled bets with outcome info.
    """
    import asyncio
    results = []
    try:
        from data.feeds.kalshi_intraday import _headers, _BASE
        import httpx
        headers = _headers("GET", "/portfolio/orders")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{_BASE}/portfolio/orders", headers=headers,
                                    params={"status": "settled", "limit": 100})
            if resp.status_code != 200:
                return results
            settled = resp.json().get("orders", [])

        # Match against our open bets
        with _db() as conn:
            open_bets = conn.execute(
                "SELECT id, order_id, spend_usd, contracts, price_cents, side FROM bets WHERE settled=0 AND order_id IS NOT NULL"
            ).fetchall()

        order_map = {o["order_id"]: o for o in settled}
        for bet in open_bets:
            order = order_map.get(bet["order_id"])
            if not order:
                continue
            status = order.get("status", "")
            if status not in ("settled", "filled"):
                continue

            # Calculate P&L from Kalshi order data
            pnl = _calc_pnl(bet, order)
            won = pnl > 0
            record_outcome(bet["id"], won, pnl)
            results.append({"bet_id": bet["id"], "order_id": bet["order_id"], "won": won, "pnl": pnl})
    except Exception as e:
        print(f"[LearningTracker] Auto-settle error: {e}")
    return results


def _calc_pnl(bet_row, order_data: dict) -> float:
    """Estimate P&L from Kalshi order response."""
    try:
        side = bet_row["side"]
        contracts = bet_row["contracts"]
        price_cents = bet_row["price_cents"]
        # Kalshi pays $1/contract to winners
        # 'yes' winner: profit = contracts * (1 - price/100) — cost already spent
        # 'yes' loser:  loss = -spend_usd
        filled = order_data.get("contracts_filled", contracts)
        outcome = order_data.get("outcome", "")  # 'yes' | 'no'
        if not outcome:
            return 0.0
        won = (side == outcome)
        if won:
            cost_per = price_cents / 100.0 if side == "yes" else (100 - price_cents) / 100.0
            return round(filled * (1.0 - cost_per), 4)
        else:
            cost_per = price_cents / 100.0 if side == "yes" else (100 - price_cents) / 100.0
            return round(-filled * cost_per, 4)
    except Exception:
        return 0.0


# ─── Strategy stats helpers ────────────────────────────────────────────────────

def _touch_strategy(strategy: str, market_type: str, wagered: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            """INSERT INTO strategy_stats (strategy, market_type, total_wagered, last_updated)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(strategy) DO UPDATE SET
                 total_wagered = total_wagered + excluded.total_wagered,
                 last_updated  = excluded.last_updated""",
            (strategy, market_type, wagered, now),
        )


def _update_strategy_stats(strategy: str, market_type: str, won: bool, pnl: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        if won:
            conn.execute(
                """UPDATE strategy_stats SET wins=wins+1, total_pnl=total_pnl+?, last_updated=?
                   WHERE strategy=?""",
                (pnl, now, strategy),
            )
        else:
            conn.execute(
                """UPDATE strategy_stats SET losses=losses+1, total_pnl=total_pnl+?, last_updated=?
                   WHERE strategy=?""",
                (pnl, now, strategy),
            )


# ─── Query / analytics ────────────────────────────────────────────────────────

def get_all_stats() -> dict:
    """
    Return comprehensive statistics for the research agent and MCP tools.
    """
    with _db() as conn:
        bets = conn.execute("SELECT * FROM bets ORDER BY placed_at DESC LIMIT 500").fetchall()
        stats = conn.execute("SELECT * FROM strategy_stats ORDER BY total_pnl DESC").fetchall()

    bets = [dict(b) for b in bets]
    stats = [dict(s) for s in stats]

    settled = [b for b in bets if b["settled"]]
    open_bets = [b for b in bets if not b["settled"]]
    wins = [b for b in settled if b["won"]]
    losses = [b for b in settled if not b["won"]]

    gross_wagered = sum(b["spend_usd"] for b in settled)
    gross_pnl = sum(b["pnl"] or 0 for b in settled)
    roi = (gross_pnl / gross_wagered * 100) if gross_wagered > 0 else 0.0

    return {
        "total_bets": len(bets),
        "settled": len(settled),
        "open": len(open_bets),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(settled) if settled else 0.0,
        "gross_wagered": round(gross_wagered, 2),
        "gross_pnl": round(gross_pnl, 4),
        "roi_pct": round(roi, 2),
        "open_exposure": round(sum(b["spend_usd"] for b in open_bets), 2),
        "strategy_breakdown": stats,
        "recent_bets": bets[:20],
    }


def get_strategy_weights() -> dict[str, float]:
    """
    Return a dict of strategy_name -> weight multiplier.
    Weight = 1.0 baseline; boosted when strategy is performing well (ROI > 10%),
    penalised when losing (ROI < -5%).
    """
    with _db() as conn:
        rows = conn.execute("SELECT * FROM strategy_stats").fetchall()

    weights = {}
    for row in rows:
        total = row["wins"] + row["losses"]
        if total < 5:
            weights[row["strategy"]] = 1.0  # not enough data
            continue
        wagered = row["total_wagered"] or 1.0
        roi = row["total_pnl"] / wagered
        # Sigmoid-like weight: ranges from 0.5 to 1.8
        weights[row["strategy"]] = max(0.5, min(1.8, 1.0 + roi * 5))
    return weights


def get_best_strategies(min_bets: int = 5) -> list[dict]:
    """Return strategies ranked by ROI with at least min_bets settled bets."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT strategy, market_type, wins, losses,
                      total_wagered, total_pnl,
                      CASE WHEN total_wagered > 0 THEN total_pnl / total_wagered ELSE 0 END as roi,
                      wins + losses as total_settled
               FROM strategy_stats
               WHERE wins + losses >= ?
               ORDER BY roi DESC""",
            (min_bets,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_daily_pnl(days: int = 30) -> list[dict]:
    """Return per-day P&L for the last N days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _db() as conn:
        rows = conn.execute(
            """SELECT date(placed_at) as date,
                      COUNT(*) as bets,
                      SUM(spend_usd) as wagered,
                      SUM(CASE WHEN won=1 THEN pnl ELSE 0 END) as pnl,
                      SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                      SUM(CASE WHEN won=0 THEN 1 ELSE 0 END) as losses
               FROM bets
               WHERE placed_at >= ? AND settled=1
               GROUP BY date(placed_at)
               ORDER BY date DESC""",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_signal_correlations() -> list[dict]:
    """
    Compute per-signal correlation with winning outcomes.
    Only uses settled bets that have a JSON signals blob.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT signals, won FROM bets WHERE settled=1 AND signals IS NOT NULL AND signals != '{}'"
        ).fetchall()

    # Collect (signal_name, value, won) triples
    signal_data: dict[str, list[tuple[float, int]]] = {}
    for row in rows:
        try:
            sigs = json.loads(row["signals"])
        except Exception:
            continue
        for name, val in sigs.items():
            try:
                fval = float(val)
                signal_data.setdefault(name, []).append((fval, row["won"] or 0))
            except (TypeError, ValueError):
                pass

    results = []
    for name, pairs in signal_data.items():
        if len(pairs) < 5:
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        corr = _pearson(xs, ys)
        results.append({"signal": name, "correlation": round(corr, 4), "n": len(pairs)})

    results.sort(key=lambda x: abs(x["correlation"]), reverse=True)
    return results


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def write_daily_summary(bankroll_end: float) -> None:
    """Upsert today's summary row."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _db() as conn:
        conn.execute(
            """INSERT INTO daily_summaries (date, bets_placed, bets_won, bets_lost, bets_open,
               gross_wagered, gross_pnl, roi_pct, bankroll_end)
               SELECT ?, COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN won=0 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN settled=0 THEN 1 ELSE 0 END),
                      SUM(spend_usd),
                      SUM(COALESCE(pnl,0)),
                      CASE WHEN SUM(spend_usd) > 0 THEN SUM(COALESCE(pnl,0))/SUM(spend_usd)*100 ELSE 0 END,
                      ?
               FROM bets WHERE date(placed_at)=?
               ON CONFLICT(date) DO UPDATE SET
                 bets_placed=excluded.bets_placed, bets_won=excluded.bets_won,
                 bets_lost=excluded.bets_lost, bets_open=excluded.bets_open,
                 gross_wagered=excluded.gross_wagered, gross_pnl=excluded.gross_pnl,
                 roi_pct=excluded.roi_pct, bankroll_end=excluded.bankroll_end""",
            (today, bankroll_end, today),
        )
