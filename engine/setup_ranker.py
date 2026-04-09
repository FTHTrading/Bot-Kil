"""
engine/setup_ranker.py — Setup class registry + expectancy tracker
==================================================================
Classifies every entry attempt into a named setup class and tracks
per-setup expectancy from the trade journal.  The agent uses the
ranking to decide which setups are live and at what size.

Setup classes
-------------
breakout_continuation    — momentum in direction of attempted break, healthy vol
reversion_after_exhaustion — trend stalled, spread compressed, reversal signal
momentum_follow_through  — established trend, pick is riding confirmed wave
news_spike_fade         — rapid price spike, fading back toward anchor

Each settled trade gets a label.  The ranker reads trade_history.jsonl
nightly and persists per-setup stats to logs/setup_stats.json.

Usage (gate layer):
    from engine.setup_ranker import SetupRanker, classify_setup, SetupClass
    ranker = SetupRanker()
    ranker.load()
    sc = classify_setup(entry_meta, regime_snap)
    rating = ranker.rating(sc)
    if not rating.tradeable:
        return REJECTED

Usage (night cycle):
    ranker = SetupRanker()
    ranker.rebuild_from_journal()   # recomputes expectancy from settled trades
    ranker.save()
"""
from __future__ import annotations

import json
import math
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_STATS_PATH   = Path(__file__).parent.parent / "logs" / "setup_stats.json"
_JOURNAL_PATH = Path(__file__).parent.parent / "logs" / "trade_history.jsonl"

# Minimum settled trades before a setup can be disabled or ranked below neutral
_MIN_SAMPLE_TO_DISABLE    = 8
_MIN_SAMPLE_TO_RANK_UP    = 5
# Expectancy below this forces the setup to "watch_only" (disabled)
_EXPECTANCY_DISABLE_FLOOR = -0.04   # -4 cents per dollar staked
# Expectancy above this allows full size
_EXPECTANCY_FULL_SIZE     = 0.06    # +6 cents per dollar staked
# Expectancy above this allows half size (watchlist only otherwise)
_EXPECTANCY_HALF_SIZE     = 0.01    # +1 cent


# ── Setup taxonomy ────────────────────────────────────────────────────────────

class SetupClass:
    BREAKOUT_CONTINUATION      = "breakout_continuation"
    REVERSION_AFTER_EXHAUSTION = "reversion_after_exhaustion"
    MOMENTUM_FOLLOW_THROUGH    = "momentum_follow_through"
    NEWS_SPIKE_FADE            = "news_spike_fade"
    UNKNOWN                    = "unknown"

    ALL = [
        BREAKOUT_CONTINUATION,
        REVERSION_AFTER_EXHAUSTION,
        MOMENTUM_FOLLOW_THROUGH,
        NEWS_SPIKE_FADE,
        UNKNOWN,
    ]


@dataclass
class SetupStats:
    setup:          str
    n_bets:         int   = 0
    n_wins:         int   = 0
    total_staked:   float = 0.0
    total_pnl:      float = 0.0
    expectancy:     float = 0.0   # E[pnl per dollar staked]
    win_rate:       float = 0.0
    avg_edge_entry: float = 0.0   # mean entry edge_pct at time of bet
    avg_edge_result: float = 0.0  # mean realized edge (pnl / stake)
    last_updated:   str   = ""

    def update(self, stake: float, pnl: float, entry_edge: float):
        self.n_bets       += 1
        self.total_staked += stake
        self.total_pnl    += pnl
        if pnl > 0:
            self.n_wins  += 1
        if self.total_staked > 0:
            self.expectancy = self.total_pnl / self.total_staked
        self.win_rate = self.n_wins / self.n_bets if self.n_bets else 0.0
        # running avg
        self.avg_edge_entry  = (self.avg_edge_entry  * (self.n_bets - 1) + entry_edge) / self.n_bets
        if self.total_staked > 0:
            self.avg_edge_result = self.total_pnl / self.total_staked
        self.last_updated     = datetime.now(timezone.utc).isoformat()


@dataclass
class SetupRating:
    setup:      str
    tradeable:  bool        # True → can place live bets
    size_mult:  float       # 0.0 / 0.5 / 1.0 multiplier on base stake
    expectancy: float
    n_bets:     int
    reason:     str

    @classmethod
    def default(cls, setup: str) -> "SetupRating":
        """New setups get half-size tradeable while sample accumulates."""
        return cls(
            setup=setup, tradeable=True, size_mult=0.5,
            expectancy=0.0, n_bets=0,
            reason="new_setup: half-size until sample >= 5",
        )


# ── Classifier ────────────────────────────────────────────────────────────────

def classify_setup(entry_meta: dict, regime: Optional[object] = None) -> str:
    """
    Classify an entry into a setup class using entry_meta and regime signals.
    entry_meta keys used: trend, realized_vol, edge_pct, market_type, minutes_to_close
    regime attrs used:    vol_regime, trend (from RegimeSnapshot)
    """
    trend   = (entry_meta.get("trend") or "unknown").lower()
    rv      = float(entry_meta.get("realized_vol") or 0)
    edge    = float(entry_meta.get("edge_pct")     or 0)
    mtype   = (entry_meta.get("market_type")       or "daily").lower()
    min_rem = float(entry_meta.get("minutes_to_close") or 999)

    # Determine regime vol bucket
    reg_vol = "normal"
    if regime is not None:
        reg_vol = getattr(regime, "vol_regime", "normal")

    # NEWS_SPIKE_FADE: very high vol short-time-remaining and fading
    if rv > 0.004 and min_rem < 30 and trend in ("down", "flat"):
        return SetupClass.NEWS_SPIKE_FADE

    # REVERSION_AFTER_EXHAUSTION: trend stalled/reverting, edge is positive on the OTHER side
    if trend == "flat" and edge > 0 and mtype == "intraday":
        return SetupClass.REVERSION_AFTER_EXHAUSTION

    # BREAKOUT_CONTINUATION: clear trend, edge confirms direction, vol is normal/high
    if trend in ("up", "down") and reg_vol in ("normal", "high") and edge > 2.0:
        return SetupClass.BREAKOUT_CONTINUATION

    # MOMENTUM_FOLLOW_THROUGH: established trend across multiple timeframes
    mom5  = entry_meta.get("mom_5m",  None)
    mom15 = entry_meta.get("mom_15m", None)
    if mom5 is not None and mom15 is not None:
        if abs(float(mom5)) > 0 and abs(float(mom15)) > 0:
            # Both timeframes agree in direction
            if (float(mom5) > 0) == (float(mom15) > 0):
                return SetupClass.MOMENTUM_FOLLOW_THROUGH

    if trend in ("up", "down"):
        return SetupClass.MOMENTUM_FOLLOW_THROUGH

    return SetupClass.UNKNOWN


# ── Ranker ────────────────────────────────────────────────────────────────────

class SetupRanker:
    """
    Loads/saves per-setup stats and produces tradeable ratings.
    """

    def __init__(self):
        self._stats: dict[str, SetupStats] = {
            s: SetupStats(setup=s) for s in SetupClass.ALL
        }

    # ── Persistence ──────────────────────────────────────────────────────────

    def load(self) -> "SetupRanker":
        try:
            if _STATS_PATH.exists():
                raw = json.loads(_STATS_PATH.read_text(encoding="utf-8"))
                for setup, d in raw.items():
                    self._stats[setup] = SetupStats(**d)
                log.info("[setup_ranker] Loaded stats for %d setups", len(raw))
        except Exception as e:
            log.warning("[setup_ranker] Could not load stats: %s (using defaults)", e)
        return self

    def save(self) -> None:
        try:
            _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
            out = {s: asdict(v) for s, v in self._stats.items()}
            _STATS_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
            log.info("[setup_ranker] Stats saved to %s", _STATS_PATH)
        except Exception as e:
            log.error("[setup_ranker] Save failed: %s", e)

    # ── Rebuild from journal ──────────────────────────────────────────────────

    def rebuild_from_journal(self) -> dict[str, SetupStats]:
        """
        Scan trade_history.jsonl, match executions to settlements, compute
        per-setup expectancy.  Idempotent — rebuilds from scratch each night.
        """
        if not _JOURNAL_PATH.exists():
            log.warning("[setup_ranker] No trade journal at %s", _JOURNAL_PATH)
            return self._stats

        executions: list[dict] = []
        settlements: list[dict] = []
        try:
            for raw in _JOURNAL_PATH.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                rec = json.loads(raw)
                t = rec.get("type", "")
                if t in ("execution", "placed", "dry_run"):
                    executions.append(rec)
                elif t in ("settlement", "settled"):
                    settlements.append(rec)
        except Exception as e:
            log.error("[setup_ranker] Journal read error: %s", e)
            return self._stats

        # Index settlements by ticker
        settle_by_ticker: dict[str, list[dict]] = {}
        for s in settlements:
            t = s.get("ticker", "")
            settle_by_ticker.setdefault(t, []).append(s)

        # Reset stats
        self._stats = {s: SetupStats(setup=s) for s in SetupClass.ALL}

        matched = 0
        for ex in executions:
            ticker = ex.get("ticker", "")
            entry_meta = ex.get("entry_meta") or {}
            setup = ex.get("setup_class") or classify_setup(entry_meta)
            stake = float(ex.get("cost_usd") or ex.get("stake", 0))
            if stake <= 0:
                continue

            # Find matching settlement
            matched_settle = None
            for s in settle_by_ticker.get(ticker, []):
                # Match by side
                if s.get("side", "").lower() == ex.get("side", "").lower():
                    matched_settle = s
                    break

            if matched_settle is None:
                # Unsettled — skip
                continue

            pnl = float(matched_settle.get("pnl") or
                        matched_settle.get("net_pnl") or
                        (float(matched_settle.get("revenue", 0) or 0) - stake))
            entry_edge = float(entry_meta.get("edge_pct") or ex.get("edge_pct", 0))

            if setup not in self._stats:
                self._stats[setup] = SetupStats(setup=setup)
            self._stats[setup].update(stake, pnl, entry_edge)
            matched += 1

        log.info("[setup_ranker] Rebuilt: %d executions matched, %d setups active", matched, len(self._stats))
        return self._stats

    # ── Rating ────────────────────────────────────────────────────────────────

    def rating(self, setup: str) -> SetupRating:
        stats = self._stats.get(setup)
        if stats is None or stats.n_bets == 0:
            return SetupRating.default(setup)

        n = stats.n_bets
        exp = stats.expectancy

        if n >= _MIN_SAMPLE_TO_DISABLE and exp < _EXPECTANCY_DISABLE_FLOOR:
            return SetupRating(
                setup=setup, tradeable=False, size_mult=0.0,
                expectancy=exp, n_bets=n,
                reason=f"disabled: expectancy={exp:.3f} < floor {_EXPECTANCY_DISABLE_FLOOR:.3f} on {n} bets",
            )

        if n >= _MIN_SAMPLE_TO_RANK_UP and exp >= _EXPECTANCY_FULL_SIZE:
            return SetupRating(
                setup=setup, tradeable=True, size_mult=1.0,
                expectancy=exp, n_bets=n,
                reason=f"full_size: expectancy={exp:.3f} on {n} bets",
            )

        if exp >= _EXPECTANCY_HALF_SIZE:
            return SetupRating(
                setup=setup, tradeable=True, size_mult=0.5,
                expectancy=exp, n_bets=n,
                reason=f"half_size: expectancy={exp:.3f} on {n} bets",
            )

        return SetupRating(
            setup=setup, tradeable=True, size_mult=0.5,
            expectancy=exp, n_bets=n,
            reason=f"watch: marginal expectancy={exp:.3f} on {n} bets",
        )

    def ranked_list(self) -> list[SetupRating]:
        """Return all setups sorted by expectancy descending."""
        ratings = [self.rating(s) for s in SetupClass.ALL]
        return sorted(ratings, key=lambda r: r.expectancy, reverse=True)

    def summary(self) -> dict:
        return {
            s: {
                "n_bets":      st.n_bets,
                "win_rate":    round(st.win_rate, 3),
                "expectancy":  round(st.expectancy, 4),
                "tradeable":   self.rating(s).tradeable,
                "size_mult":   self.rating(s).size_mult,
            }
            for s, st in self._stats.items()
        }
