"""
engine/metrics.py — Live performance tracking
==============================================
Tracks bet outcomes, accuracy, edge, and Brier score in a rolling
in-memory store.  Persists to a JSON file so stats survive agent restarts.

Designed for low-overhead use inside the agent loop: every settled
position calls `record_outcome()` and the store updates itself.

Usage:
    from engine.metrics import MetricsStore
    m = MetricsStore()
    m.record_outcome(asset="BTC", regime_key=("BTC","normal","1-6h","trending"),
                     predicted_prob=0.62, outcome=1, edge_pct=0.14, stake=5.0,
                     pnl=4.80)
    print(m.summary())
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_METRICS_PATH = Path(__file__).parent.parent / "logs" / "metrics.json"
_ROLLING_N    = 50    # window for rolling accuracy / Brier


# ── Single trade record ──────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    asset:          str
    regime_key:     tuple         # (asset, vol, bucket, trend) from RegimeSnapshot.key
    predicted_prob: float
    outcome:        int           # 1 = won, 0 = lost
    edge_pct:       float         # calibrated edge used to approve the bet
    stake:          float
    pnl:            float         # net P&L in dollars
    timestamp:      str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["regime_key"] = list(self.regime_key)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TradeRecord":
        d = dict(d)
        d["regime_key"] = tuple(d.get("regime_key", []))
        return cls(**d)


# ── Metrics store ────────────────────────────────────────────────────────────

class MetricsStore:
    """
    Append-only record store with rolling and all-time statistics.
    Thread-safe for single-process use (no locking needed for asyncio).
    """

    def __init__(self, path: Optional[Path] = None):
        self._path    = path or _METRICS_PATH
        self._records: list[TradeRecord] = []
        self._load()

    # ── Recording ────────────────────────────────────────────────────────────

    def record_outcome(
        self,
        asset: str,
        regime_key: tuple,
        predicted_prob: float,
        outcome: int,
        edge_pct: float,
        stake: float,
        pnl: float,
        timestamp: str = "",
    ):
        """Append a settled trade record and persist."""
        import datetime
        record = TradeRecord(
            asset=asset,
            regime_key=regime_key,
            predicted_prob=predicted_prob,
            outcome=outcome,
            edge_pct=edge_pct,
            stake=stake,
            pnl=pnl,
            timestamp=timestamp or datetime.datetime.utcnow().isoformat(),
        )
        self._records.append(record)
        self._save()
        log.debug("[metrics] recorded %s outcome=%d pnl=$%.2f", asset, outcome, pnl)

    # ── Statistics ────────────────────────────────────────────────────────────

    def summary(self, last_n: Optional[int] = None) -> dict:
        """Return aggregate statistics.  Pass last_n to restrict to recent trades."""
        recs = self._records[-last_n:] if last_n else self._records
        if not recs:
            return {"n": 0}

        n         = len(recs)
        wins      = sum(r.outcome for r in recs)
        total_pnl = sum(r.pnl for r in recs)
        avg_pred  = sum(r.predicted_prob for r in recs) / n
        avg_act   = wins / n
        brier     = sum((r.predicted_prob - r.outcome) ** 2 for r in recs) / n
        avg_edge  = sum(r.edge_pct for r in recs) / n
        avg_stake = sum(r.stake for r in recs) / n
        roi       = total_pnl / sum(r.stake for r in recs) if sum(r.stake for r in recs) > 0 else 0.0

        return {
            "n":           n,
            "wins":        wins,
            "losses":      n - wins,
            "win_rate":    round(avg_act, 3),
            "avg_pred":    round(avg_pred, 3),
            "calibration_gap": round(avg_pred - avg_act, 3),  # positive = overconfident
            "brier_score": round(brier, 4),
            "avg_edge_pct": round(avg_edge * 100, 2),
            "avg_stake":   round(avg_stake, 2),
            "total_pnl":   round(total_pnl, 2),
            "roi_pct":     round(roi * 100, 2),
        }

    def by_regime(self) -> dict[str, dict]:
        """Break down stats by regime key (asset, vol, bucket, trend)."""
        groups: dict[str, list[TradeRecord]] = {}
        for r in self._records:
            k = "/".join(str(x) for x in r.regime_key)
            groups.setdefault(k, []).append(r)
        return {k: self._stats_for(v) for k, v in groups.items()}

    def by_asset(self) -> dict[str, dict]:
        """Break down stats by asset."""
        groups: dict[str, list[TradeRecord]] = {}
        for r in self._records:
            groups.setdefault(r.asset, []).append(r)
        return {k: self._stats_for(v) for k, v in groups.items()}

    def rolling(self, n: int = _ROLLING_N) -> dict:
        """Rolling window stats for the last N trades."""
        return self.summary(last_n=n)

    def recent(self, n: int = 10) -> list[dict]:
        """Return the N most recent trade records as dicts."""
        return [r.to_dict() for r in self._records[-n:]]

    # ── Persistence ──────────────────────────────────────────────────────────

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump([r.to_dict() for r in self._records], f, indent=2)
        except Exception as e:
            log.warning("[metrics] save failed: %s", e)

    def _load(self):
        if not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            self._records = [TradeRecord.from_dict(d) for d in raw]
            log.info("[metrics] loaded %d records from %s", len(self._records), self._path)
        except Exception as e:
            log.warning("[metrics] load failed: %s", e)
            self._records = []

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _stats_for(recs: list[TradeRecord]) -> dict:
        n = len(recs)
        if n == 0:
            return {"n": 0}
        wins = sum(r.outcome for r in recs)
        pnl  = sum(r.pnl for r in recs)
        invested = sum(r.stake for r in recs) or 1.0
        brier = sum((r.predicted_prob - r.outcome) ** 2 for r in recs) / n
        return {
            "n":           n,
            "win_rate":    round(wins / n, 3),
            "total_pnl":   round(pnl, 2),
            "roi_pct":     round(pnl / invested * 100, 2),
            "brier_score": round(brier, 4),
        }
