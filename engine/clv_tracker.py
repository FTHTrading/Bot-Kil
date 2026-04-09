"""
engine/clv_tracker.py — Closing Line Value recorder and analyser
================================================================
CLV is the gold standard for measuring whether a betting edge is real.
It compares the price you paid at entry to the final mid-price just
before settlement ("closing line").

  CLV (YES bet)  = close_price_cents − entry_price_cents
  CLV (NO bet)   = entry_price_cents − close_price_cents
  Positive CLV   = you beat the market's final estimate (sharp)
  Negative CLV   = market moved against you after entry  (fade target)

Extended interpretation:
  avg_clv > 0 on 55 %+ of bets → genuine model edge, keep the strategy
  avg_clv < 0 consistently     → re-examine entry timing or model calibration

Storage: logs/clv_history.jsonl  (one JSON object per order)

Usage
-----
    from engine.clv_tracker import CLVTracker

    tracker = CLVTracker()

    # Called immediately after Kalshi order is confirmed
    tracker.record_entry(order_id, ticker, side, entry_price_cents)

    # Called by night_cycle.py when settlement is fetched
    tracker.record_close(ticker, close_price_cents)

    # Called any time for current stats
    stats = tracker.compute_stats()
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ROOT    = Path(__file__).parent.parent
_CLV_PATH = _ROOT / "logs" / "clv_history.jsonl"

# Minimum number of closed records before we report meaningful stats
_MIN_CLOSED = 5


class CLVTracker:
    """
    Lightweight stateless façade over the clv_history.jsonl ledger.
    All methods are safe to call concurrently — each writes/reads
    the file independently. File locking is not implemented (single-process
    assumption).
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = path or _CLV_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ── Write helpers ─────────────────────────────────────────────────────────

    def record_entry(
        self,
        order_id:          str,
        ticker:            str,
        side:              str,   # "yes" or "no"
        entry_price_cents: int,
        edge_pct:          float = 0.0,
        asset:             str   = "",
        market_type:       str   = "",
    ) -> None:
        """
        Record the entry price immediately after an order is confirmed.
        Call this right after a PLACED response from Kalshi.
        """
        rec = {
            "order_id":          order_id,
            "ticker":            ticker,
            "side":              side.lower(),
            "entry_price_cents": entry_price_cents,
            "edge_pct_at_entry": round(edge_pct, 2),
            "asset":             asset,
            "market_type":       market_type,
            "entry_ts":          datetime.now(timezone.utc).isoformat(),
            "close_price_cents": None,   # filled in by record_close()
            "clv":               None,
            "close_ts":          None,
        }
        self._append(rec)
        log.debug(
            "[clv] entry recorded: %s %s @ %dc  order=%s",
            ticker, side, entry_price_cents, order_id,
        )

    def record_close(
        self,
        ticker:            str,
        close_price_cents: float,
    ) -> int:
        """
        Fill in the closing price for all open CLV entries matching *ticker*.
        Returns number of records updated.

        `close_price_cents` is the mid-price (yes_bid + yes_ask) / 2
        at the moment the market settles, sourced from the Kalshi orderbook
        snapshot taken during night_cycle.py.
        """
        updated = 0
        records = self._load_all()
        for rec in records:
            if rec.get("ticker") == ticker and rec.get("close_price_cents") is None:
                rec["close_price_cents"] = round(float(close_price_cents), 2)
                rec["close_ts"]          = datetime.now(timezone.utc).isoformat()
                # Compute CLV
                ep = float(rec.get("entry_price_cents", 50))
                cp = float(close_price_cents)
                if rec.get("side", "yes") == "yes":
                    rec["clv"] = round(cp - ep, 2)   # positive = price moved in our favour
                else:
                    rec["clv"] = round(ep - cp, 2)   # for NO bets, falling price = good
                updated += 1
        if updated:
            self._write_all(records)
            log.info("[clv] %d record(s) closed for %s  close_price=%.1fc", updated, ticker, close_price_cents)
        return updated

    # ── Analytics ─────────────────────────────────────────────────────────────

    def compute_stats(self) -> dict:
        """
        Compute aggregate CLV statistics from all closed records.

        Returns dict with:
            n_total         : total orders tracked
            n_closed        : orders with closing price filled in
            n_positive_clv  : count with CLV > 0
            pct_positive    : % positive CLV (target ≥ 55%)
            avg_clv         : mean CLV in cents (target > 0)
            median_clv      : median CLV in cents
            clv_by_asset    : {asset: {avg_clv, n}} breakdown
            clv_by_type     : {market_type: {avg_clv, n}} breakdown
            sufficient_data : True if n_closed >= _MIN_CLOSED
        """
        records = self._load_all()
        closed  = [r for r in records if r.get("clv") is not None]

        if not closed:
            return {
                "n_total": len(records), "n_closed": 0, "n_positive_clv": 0,
                "pct_positive": 0.0, "avg_clv": 0.0, "median_clv": 0.0,
                "clv_by_asset": {}, "clv_by_type": {},
                "sufficient_data": False,
            }

        clv_vals       = [float(r["clv"]) for r in closed]
        n_pos          = sum(1 for v in clv_vals if v > 0)
        avg_clv        = sum(clv_vals) / len(clv_vals)
        sorted_vals    = sorted(clv_vals)
        n              = len(sorted_vals)
        median_clv     = (sorted_vals[n // 2] if n % 2 else
                          (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2)

        # Per-asset breakdown
        by_asset: dict[str, dict] = {}
        by_type:  dict[str, dict] = {}
        for r in closed:
            for key, bucket in [(r.get("asset", "?"), by_asset),
                                (r.get("market_type", "?"), by_type)]:
                if key not in bucket:
                    bucket[key] = {"sum_clv": 0.0, "n": 0}
                bucket[key]["sum_clv"] += float(r["clv"])
                bucket[key]["n"]       += 1
        clv_by_asset = {k: {"avg_clv": round(v["sum_clv"] / v["n"], 2), "n": v["n"]}
                        for k, v in by_asset.items()}
        clv_by_type  = {k: {"avg_clv": round(v["sum_clv"] / v["n"], 2), "n": v["n"]}
                        for k, v in by_type.items()}

        return {
            "n_total":       len(records),
            "n_closed":      len(closed),
            "n_positive_clv": n_pos,
            "pct_positive":  round(n_pos / len(closed) * 100, 1),
            "avg_clv":       round(avg_clv, 2),
            "median_clv":    round(median_clv, 2),
            "clv_by_asset":  clv_by_asset,
            "clv_by_type":   clv_by_type,
            "sufficient_data": len(closed) >= _MIN_CLOSED,
        }

    # ── File I/O ──────────────────────────────────────────────────────────────

    def _append(self, rec: dict) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as exc:
            log.warning("[clv] append failed: %s", exc)

    def _load_all(self) -> list[dict]:
        if not self._path.exists():
            return []
        out: list[dict] = []
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
        except Exception as exc:
            log.warning("[clv] load failed: %s", exc)
        return out

    def _write_all(self, records: list[dict]) -> None:
        try:
            self._path.write_text(
                "\n".join(json.dumps(r) for r in records) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("[clv] write_all failed: %s", exc)
