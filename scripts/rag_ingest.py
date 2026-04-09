"""
scripts/rag_ingest.py — Populate ChromaDB with trade history and session summaries
==================================================================================
Two public entry points:

  ingest_settled_trades(n_days_back=7)
      Reads logs/trade_history.jsonl, filters for records with a
      `settled` status (or non-null `result`), builds a rich text
      document per trade and upserts into the `bet_history` collection.
      Use-case: night_cycle.py Phase 8, or manual bootstrapping.
      Returns: number of documents upserted.

  ingest_session_summary(session_result: dict)
      Serialises an autonomous-agent session result into a concise
      plaintext narrative and upserts into `daily_picks`.
      Use-case: called at the end of every autonomous session.
      Returns: 1 if successful, 0 on error.

ChromaDB's upsert() is idempotent on id — safe to call repeatedly.
IDs are derived from order_id / session fingerprint, so re-ingestion
just refreshes the document rather than creating duplicates.

Both functions degrade gracefully if ChromaDB / sentence-transformers
are not installed — they log a warning and return 0 rather than raising.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent

# Lazy import — silences ImportError when ChromaDB is absent
_store: Optional[object] = None   # EmbeddingStore singleton

def _get_store():
    global _store
    if _store is None:
        try:
            sys.path.insert(0, str(_ROOT))
            from rag.embeddings import EmbeddingStore
            _store = EmbeddingStore()
        except Exception as exc:
            log.warning("[rag_ingest] Cannot load EmbeddingStore: %s", exc)
            return None
    return _store


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> list[dict]:
    """Read a .jsonl file and return all valid JSON objects."""
    if not path.exists():
        log.warning("[rag_ingest] File not found: %s", path)
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _trade_to_text(rec: dict) -> str:
    """
    Convert a trade_history.jsonl record into a natural-language document
    suitable for semantic retrieval.

    Example output:
        Date: 2025-04-09 | Market: KXBTCD-26APR0907-T71299.99
        Bet: NO ×2 @ 85¢  Edge: +18.9%  Asset: BTC  Type: daily_diffusion
        Regime: trending_bull | Vol regime: normal
        Model confidence: 73.2%  Calibrated prob: 0.312
        Result: WIN  PnL: +$1.70  Settlement: YES=4¢
        Setup: breakout_continuation (A-tier)
        Reasoning snippet: BTC pump exhaustion near 71300 resistance...
    """
    ts = rec.get("ts", rec.get("timestamp", ""))[:10]
    ticker    = rec.get("ticker", "?")
    side      = rec.get("side", "?").upper()
    qty       = rec.get("qty", rec.get("quantity", 1))
    price     = rec.get("price", rec.get("entry_price_cents", "?"))
    edge      = rec.get("edge_pct", rec.get("edge", 0.0))
    asset     = rec.get("asset", rec.get("entry_meta", {}).get("asset", "?"))
    mtype     = rec.get("market_type", rec.get("entry_meta", {}).get("market_type", "?"))
    regime    = rec.get("regime", rec.get("entry_meta", {}).get("regime", "?"))
    vol_reg   = rec.get("vol_regime", "?")
    conf      = rec.get("model_confidence", rec.get("entry_meta", {}).get("model_confidence", "?"))
    result    = rec.get("result", rec.get("outcome", "?"))
    pnl       = rec.get("pnl", rec.get("net_pnl", "?"))
    settle_p  = rec.get("settlement_price", "?")
    setup     = rec.get("entry_meta", {}).get("setup_class", "?")
    setup_gr  = rec.get("entry_meta", {}).get("setup_grade", "?")
    reasoning = rec.get("reasoning", rec.get("entry_meta", {}).get("reasoning", ""))[:200]

    lines = [
        f"Date: {ts} | Market: {ticker}",
        f"Bet: {side} ×{qty} @ {price}¢  Edge: +{edge}%  Asset: {asset}  Type: {mtype}",
        f"Regime: {regime} | Vol regime: {vol_reg}",
        f"Model confidence: {conf}  ",
        f"Result: {result}  PnL: {pnl}  Settlement price: {settle_p}",
        f"Setup: {setup} ({setup_gr})",
    ]
    if reasoning:
        lines.append(f"Reasoning: {reasoning}")
    return "\n".join(lines)


def _trade_metadata(rec: dict) -> dict:
    """Flat metadata dict (scalar values only) for ChromaDB `where` filtering."""
    meta = {
        "date":        rec.get("ts", rec.get("timestamp", ""))[:10],
        "ticker":      rec.get("ticker", ""),
        "asset":       rec.get("asset", rec.get("entry_meta", {}).get("asset", "")),
        "side":        rec.get("side", ""),
        "result":      str(rec.get("result", rec.get("outcome", ""))),
        "market_type": rec.get("market_type", rec.get("entry_meta", {}).get("market_type", "")),
        "regime":      str(rec.get("regime", rec.get("entry_meta", {}).get("regime", ""))),
        "setup_class": str(rec.get("entry_meta", {}).get("setup_class", "")),
        "source":      "trade_history",
    }
    # Numeric fields — convert to strings so ChromaDB doesn't choke on None
    for k in ("edge_pct", "pnl", "net_pnl", "model_confidence"):
        val = rec.get(k)
        if val is None:
            val = rec.get("entry_meta", {}).get(k)
        meta[k] = str(val) if val is not None else ""
    return meta


# ── Public API ────────────────────────────────────────────────────────────────

def ingest_settled_trades(n_days_back: int = 7) -> int:
    """
    Read trade_history.jsonl and ingest settled records into `bet_history`.

    A record is considered "settled" if it has a non-empty `result` field,
    or a `status` of "settled" / "closed" / "won" / "lost".

    Returns number of documents upserted.
    """
    store = _get_store()
    if store is None:
        log.warning("[rag_ingest] Skipping ingest — no vector store available")
        return 0

    path = _ROOT / "logs" / "trade_history.jsonl"
    records = _load_jsonl(path)

    if not records:
        log.info("[rag_ingest] No trade history found at %s", path)
        return 0

    # Cutoff date
    cutoff = datetime.now(timezone.utc) - timedelta(days=n_days_back)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    _SETTLED_STATUSES = {"settled", "closed", "won", "lost", "win", "loss"}

    settled = []
    for r in records:
        ts = r.get("ts", r.get("timestamp", ""))[:10]
        if ts < cutoff_str:
            continue
        result = str(r.get("result", r.get("outcome", ""))).strip().lower()
        status = str(r.get("status", "")).strip().lower()
        if result and result not in ("none", "", "?", "unknown"):
            settled.append(r)
        elif status in _SETTLED_STATUSES:
            settled.append(r)

    if not settled:
        log.info("[rag_ingest] %d trade(s) found but none settled in last %d days",
                 len(records), n_days_back)
        return 0

    texts     = [_trade_to_text(r) for r in settled]
    metadatas = [_trade_metadata(r) for r in settled]
    # Use order_id as stable id; fall back to ticker+date hash
    ids = []
    for r in settled:
        oid = r.get("order_id") or r.get("id")
        if oid:
            ids.append(f"trade_{oid}")
        else:
            from hashlib import sha256
            key = (r.get("ticker", "") + r.get("ts", r.get("timestamp", ""))[:16])
            ids.append("trade_" + sha256(key.encode()).hexdigest()[:12])

    try:
        n = store.upsert("bet_history", texts, metadatas, ids)
        log.info("[rag_ingest] Upserted %d settled trades → bet_history", n)
        return n
    except Exception as exc:
        log.error("[rag_ingest] bet_history upsert failed: %s", exc)
        return 0


def ingest_session_summary(session_result: dict) -> int:
    """
    Write a session summary to the `daily_picks` ChromaDB collection.

    Expected keys in session_result (all optional — function is defensive):
        session_id    : str
        date          : str  YYYY-MM-DD
        bets_placed   : list[dict]  (subset of trade records)
        session_spend : float
        session_pnl   : float
        market_summary: str   (free-text LLM summary for the session)
        decisions_skipped : int
        top_setup     : str

    Returns 1 on success, 0 on any error.
    """
    store = _get_store()
    if store is None:
        return 0

    try:
        date       = session_result.get("date",         datetime.now().strftime("%Y-%m-%d"))
        sess_id    = session_result.get("session_id",   date)
        bets       = session_result.get("bets_placed",  [])
        spend      = session_result.get("session_spend", 0.0)
        pnl        = session_result.get("session_pnl",   0.0)
        summary    = session_result.get("market_summary", "")
        skipped    = session_result.get("decisions_skipped", "?")
        top_setup  = session_result.get("top_setup", "?")

        bet_lines = []
        for b in bets[:5]:   # cap at 5 bets per summary for token budget
            ticker = b.get("ticker", "?")
            side   = b.get("side",   "?").upper()
            price  = b.get("price",  b.get("entry_price_cents", "?"))
            edge   = b.get("edge_pct", "?")
            result = b.get("result", "pending")
            bet_lines.append(f"  {ticker}  {side} @ {price}¢  edge={edge}%  result={result}")

        bet_block = "\n".join(bet_lines) if bet_lines else "  (no bets placed)"

        doc = (
            f"Session: {date}  id={sess_id}\n"
            f"Spend: ${spend:.2f}  PnL: ${pnl:.2f}\n"
            f"Bets placed:\n{bet_block}\n"
            f"Decisions skipped: {skipped}  Top setup: {top_setup}\n"
            f"Summary: {summary[:500]}"
        )

        meta = {
            "date":        date,
            "session_id":  str(sess_id),
            "n_bets":      str(len(bets)),
            "spend":       str(round(spend, 2)),
            "pnl":         str(round(pnl, 2)),
            "top_setup":   str(top_setup),
            "source":      "session_summary",
        }

        n = store.upsert("daily_picks", [doc], [meta], [f"session_{sess_id}"])
        log.info("[rag_ingest] Session summary upserted → daily_picks  session=%s", sess_id)
        return n
    except Exception as exc:
        log.error("[rag_ingest] daily_picks upsert failed: %s", exc)
        return 0


# ── CLI bootstrap ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="Populate ChromaDB from trade history")
    ap.add_argument("--days", type=int, default=30,
                    help="Look back N days for settled trades (default 30)")
    args = ap.parse_args()

    print(f"Ingesting settled trades from last {args.days} days …")
    n = ingest_settled_trades(n_days_back=args.days)
    print(f"Done — {n} document(s) upserted to bet_history.")
