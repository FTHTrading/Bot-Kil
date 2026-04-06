"""
Database setup — creates SQLite schema for bet tracking.
Run once: python db/setup.py
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "kalishi_edge.db"


def create_schema():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS bets (
        id              TEXT PRIMARY KEY,
        sport           TEXT NOT NULL,
        event           TEXT NOT NULL,
        market          TEXT NOT NULL,
        pick            TEXT NOT NULL,
        american_odds   INTEGER NOT NULL,
        decimal_odds    REAL NOT NULL,
        stake           REAL NOT NULL,
        ev_pct          REAL DEFAULT 0,
        edge_pct        REAL DEFAULT 0,
        strategy        TEXT DEFAULT 'kelly',
        result          TEXT,           -- 'win', 'loss', 'push', NULL=open
        pnl             REAL,
        closing_odds    INTEGER,
        clv             REAL,
        placed_at       TEXT NOT NULL,
        settled_at      TEXT,
        notes           TEXT
    );

    CREATE TABLE IF NOT EXISTS bankroll_snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date   TEXT NOT NULL,
        bankroll        REAL NOT NULL,
        daily_pnl       REAL DEFAULT 0,
        roi_pct         REAL DEFAULT 0,
        win_rate        REAL DEFAULT 0,
        total_bets      INTEGER DEFAULT 0,
        open_bets       INTEGER DEFAULT 0,
        clv_avg         REAL DEFAULT 0,
        created_at      TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS arb_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        event           TEXT NOT NULL,
        sport           TEXT,
        arb_type        TEXT,
        profit_pct      REAL,
        total_stake     REAL,
        leg_a_book      TEXT,
        leg_a_side      TEXT,
        leg_a_odds      REAL,
        leg_b_book      TEXT,
        leg_b_side      TEXT,
        leg_b_odds      REAL,
        guaranteed_profit REAL,
        scan_time       TEXT
    );

    CREATE TABLE IF NOT EXISTS line_movements (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        sport           TEXT,
        event           TEXT,
        book            TEXT,
        market          TEXT,
        side            TEXT,
        prev_decimal    REAL,
        curr_decimal    REAL,
        movement        REAL,
        significance    TEXT,
        ts              TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_bets_sport ON bets(sport);
    CREATE INDEX IF NOT EXISTS idx_bets_result ON bets(result);
    CREATE INDEX IF NOT EXISTS idx_bets_placed_at ON bets(placed_at);
    CREATE INDEX IF NOT EXISTS idx_snapshots_date ON bankroll_snapshots(snapshot_date);
    """)

    conn.commit()
    conn.close()
    print(f"[DB] Schema created at {DB_PATH}")


if __name__ == "__main__":
    create_schema()
