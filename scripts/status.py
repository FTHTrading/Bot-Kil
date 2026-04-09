"""
status.py — Kalishi Edge operator health dashboard
===================================================
Prints a comprehensive go/no-go report covering:
  - GPU / torch environment
  - Neural model state (device, schema, training meta)
  - Paper-trade evidence (win rate, neural vs math, last 10)
  - Pre-flight checklist
  - Go / No-Go recommendation for live execution

Usage:
    python scripts/status.py              # one-shot report
    python scripts/status.py --settle     # also run settlement check first
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

_PAPER_LOG  = _ROOT / "logs" / "paper_trades_multi.jsonl"
_PRED_LOG   = _ROOT / "logs" / "predictions.jsonl"
_MODEL_PATH = _ROOT / "models" / "kalshi_net.pt"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check(cond: bool, label: str, ok_msg: str = "", fail_msg: str = "") -> bool:
    icon = "✓" if cond else "✗"
    msg  = ok_msg if cond else fail_msg
    print(f"    [{icon}] {label}  {msg}")
    return cond


def _load_trades() -> list[dict]:
    if not _PAPER_LOG.exists():
        return []
    trades = []
    for ln in _PAPER_LOG.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                trades.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return trades


def _load_model_meta() -> dict:
    if not _MODEL_PATH.exists():
        return {}
    try:
        import torch
        ckpt = torch.load(_MODEL_PATH, map_location="cpu", weights_only=False)
        return ckpt.get("meta", {})
    except Exception as e:
        return {"load_error": str(e)}


# ── Sections ──────────────────────────────────────────────────────────────────

def _section_gpu() -> dict:
    print("\n  GPU / TORCH")
    print("  " + "─" * 58)
    info = {
        "torch_ok": False,
        "cuda_ok":  False,
        "device":   "cpu",
        "vram_gb":  0.0,
    }
    try:
        import torch
        info["torch_ok"]      = True
        info["torch_version"] = torch.__version__
        info["cuda_ok"]       = torch.cuda.is_available()
        print(f"    torch:       {torch.__version__}")
        print(f"    cuda:        {info['cuda_ok']}")
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            info["device"]     = torch.cuda.get_device_name(0)
            info["vram_gb"]    = round(props.total_memory / 1e9, 1)
            info["device_count"] = torch.cuda.device_count()
            print(f"    devices:     {info['device_count']}")
            print(f"    device[0]:   {info['device']}")
            print(f"    VRAM:        {info['vram_gb']} GB")
        else:
            print(f"    [!] CUDA not available \u2014 inference will run on CPU")
    except ImportError:
        print(f"    [!] torch not installed")
    return info


def _section_model() -> dict:
    print("\n  MODEL")
    print("  " + "─" * 58)
    info = {
        "file_ok":    False,
        "schema_ok":  False,
        "meta":       {},
    }
    if not _MODEL_PATH.exists():
        print(f"    [✗] models/kalshi_net.pt NOT FOUND")
        print(f"        Run: python scripts/train_neural_model.py")
        return info

    info["file_ok"] = True
    print(f"    file:        {_MODEL_PATH.name}  [EXISTS]")

    meta = _load_model_meta()
    info["meta"] = meta
    if "load_error" in meta:
        print(f"    [!] load error: {meta['load_error']}")
        return info

    td = meta.get("training_date", "unknown")
    va = meta.get("val_acc", "?")
    nt = meta.get("n_train", "?")
    ep = meta.get("best_epoch", "?")
    dev = meta.get("device", "?")
    sh  = meta.get("schema_hash", "none")
    print(f"    trained:     {td}")
    print(f"    val_acc:     {va}%  (epoch {ep},  n={nt})")
    print(f"    device:      {dev}")
    print(f"    schema_hash: {sh}")

    # Check schema match
    try:
        from engine.neural_model import SCHEMA_HASH
        if sh == SCHEMA_HASH:
            print(f"    schema:      ✓ matches current code")
            info["schema_ok"] = True
        elif sh == "none":
            print(f"    schema:      ? old model, no hash stored  (retrain recommended)")
            info["schema_ok"] = True  # don't block on old models
        else:
            print(f"    schema:      ✗ MISMATCH  saved={sh}  current={SCHEMA_HASH}")
            print(f"                 Retrain: python scripts/train_neural_model.py")
    except ImportError:
        info["schema_ok"] = True

    # Param count
    try:
        import torch
        from engine.neural_model import KalshiNet
        m = KalshiNet()
        params = sum(p.numel() for p in m.parameters())
        print(f"    params:      {params:,}")
    except Exception:
        pass

    return info


def _section_paper() -> dict:
    print("\n  PAPER TRADING  [paper_trades_multi.jsonl]")
    print("  " + "─" * 58)
    info = {
        "total": 0, "settled": 0, "pending": 0,
        "wins": 0, "losses": 0, "win_pct": 0.0,
        "neural_wins": 0, "neural_total": 0,
        "math_wins": 0, "math_total": 0,
    }

    trades = _load_trades()
    if not trades:
        print(f"    No paper trades yet. Start with:")
        print(f"    python scripts/run_multi.py --paper --loop --loop-seconds 60")
        return info

    info["total"]   = len(trades)
    settled  = [t for t in trades if t.get("result") in ("win", "loss")]
    pending  = [t for t in trades if not t.get("result")]
    wins     = [t for t in settled if t.get("result") == "win"]
    losses   = [t for t in settled if t.get("result") == "loss"]

    info["settled"] = len(settled)
    info["pending"] = len(pending)
    info["wins"]    = len(wins)
    info["losses"]  = len(losses)
    info["win_pct"] = len(wins) / len(settled) * 100 if settled else 0.0

    # Neural vs math
    for t in settled:
        src = (t.get("prob_source") or "")
        if "neural" in src:
            info["neural_total"] += 1
            if t.get("result") == "win":
                info["neural_wins"] += 1
        else:
            info["math_total"] += 1
            if t.get("result") == "win":
                info["math_wins"] += 1

    print(f"    total picks: {info['total']}  ({info['settled']} settled,  {info['pending']} pending)")
    if settled:
        print(f"    win rate:    {info['win_pct']:.1f}%  ({info['wins']}W / {info['losses']}L)")
        # Neural vs math
        def _wr(w, t):
            return f"{w}/{t} = {w/t*100:.0f}%" if t else "n/a"
        print(f"    neural:      {_wr(info['neural_wins'], info['neural_total'])}")
        print(f"    math:        {_wr(info['math_wins'], info['math_total'])}")
    else:
        print(f"    No settled picks yet  ({info['pending']} awaiting settlement)")

    # Last 10 settled
    last10 = settled[-10:]
    if last10:
        print(f"\n    Last {len(last10)} settled picks:")
        for t in last10:
            ts   = t.get("ts", "")[:16].replace("T", " ")
            tkr  = (t.get("ticker") or "")[:38]
            side = (t.get("side") or "").upper()[:3]
            price = t.get("price", 0)
            src  = (t.get("prob_source") or "?")[:10]
            res  = t.get("result", "?")
            icon = "WIN " if res == "win" else "LOSS"
            print(f"      {icon}  {ts}  {tkr:<38}  {side}  @{price:.2f}  [{src}]")

    # Last prediction from audit log
    if _PRED_LOG.exists():
        try:
            lines = _PRED_LOG.read_text(encoding="utf-8").splitlines()
            if lines:
                last = json.loads(lines[-1])
                ts   = last.get("ts", "")[:19].replace("T", " ")
                print(f"\n    Last prediction: {ts}  {last.get('asset')}  "
                      f"{last.get('side')}  prob={last.get('prob'):.0%}  "
                      f"edge={last.get('edge', 0)*100:.1f}%  "
                      f"{'neural' if last.get('used_neural') else 'math'}")
        except Exception:
            pass

    return info


async def _check_api() -> dict:
    """Quick Kalshi API and live-data health check."""
    result = {"api_ok": False, "balance": None, "momentum_ok": False}
    try:
        from data.feeds.kalshi import get_balance
        result["balance"] = await get_balance()
        result["api_ok"] = True
    except Exception as e:
        result["api_error"] = str(e)[:60]

    try:
        from data.feeds.btc_momentum import get_momentum_signals
        sigs = await get_momentum_signals(["BTC"])
        btc = sigs.get("BTC", {})
        result["momentum_ok"] = bool(btc.get("current") and btc.get("current") > 0)
        result["btc_live"]    = round(btc.get("current", 0), 0)
        result["btc_spot"]    = btc.get("spot_live", False)
    except Exception as e:
        result["momentum_error"] = str(e)[:60]

    return result


def _section_preflight(gpu: dict, model: dict, paper: dict, api: dict, balance_floor: float = 5.50) -> dict:
    print("\n  PRE-FLIGHT CHECKLIST")
    print("  " + "─" * 58)
    GATE_PICKS  = 10
    GATE_WINPCT = 60.0

    checks = {}
    checks["gpu"]       = _check(gpu.get("cuda_ok", False),   "GPU (CUDA)",
                                   f"  {gpu.get('device', '?')}",
                                   "  CUDA not available \u2014 inference on CPU")
    checks["model"]     = _check(model.get("file_ok", False),  "Model file",
                                   "  models/kalshi_net.pt exists",
                                   "  model file missing \u2014 run train script")
    checks["schema"]    = _check(model.get("schema_ok", False), "Schema hash",
                                   "  training schema matches inference code",
                                   "  schema mismatch \u2014 retrain required")
    checks["api"]       = _check(api.get("api_ok", False),     "Kalshi API",
                                   f"  balance=${api.get('balance', '?')}",
                                   f"  {api.get('api_error', 'API error')}")
    checks["live_data"] = _check(api.get("momentum_ok", False),"Live price feed",
                                   f"  BTC=${api.get('btc_live', '?')}  live={api.get('btc_spot')}",
                                   "  price feed unavailable")
    bal = api.get("balance") or 0.0
    checks["floor"]     = _check(bal >= balance_floor,          "Balance floor",
                                   f"  ${bal:.2f} ≥ ${balance_floor:.2f}",
                                   f"  ${bal:.2f} < floor ${balance_floor:.2f} \u2014 STOP")
    settled  = paper.get("settled", 0)
    win_pct  = paper.get("win_pct", 0.0)
    checks["picks"]     = _check(settled >= GATE_PICKS,         "Settled picks gate",
                                   f"  {settled}/{GATE_PICKS} \u2713",
                                   f"  {settled}/{GATE_PICKS} \u2014 need {GATE_PICKS - settled} more")
    checks["winrate"]   = _check(win_pct >= GATE_WINPCT,        "Win rate gate",
                                   f"  {win_pct:.1f}% \u2265 {GATE_WINPCT:.0f}% \u2713",
                                   f"  {win_pct:.1f}% < {GATE_WINPCT:.0f}% \u2014 not ready")
    return checks


def _section_verdict(checks: dict, paper: dict):
    GATE_PICKS  = 10
    GATE_WINPCT = 60.0
    settled  = paper.get("settled", 0)
    win_pct  = paper.get("win_pct", 0.0)

    infra_ok = checks.get("gpu") and checks.get("model") and checks.get("schema") and \
               checks.get("api") and checks.get("live_data") and checks.get("floor")
    gate_ok  = checks.get("picks") and checks.get("winrate")

    print("\n  GO / NO-GO RECOMMENDATION")
    print("  " + "═" * 58)
    if not infra_ok:
        print(f"  ✘  BLOCKED  \u2014 infrastructure issues above must be resolved first")
    elif not gate_ok:
        needed = max(0, GATE_PICKS - settled)
        print(f"  ✘  NO-GO  \u2014 paper evidence gate not yet met")
        print(f"     Need  : {GATE_PICKS}+ settled picks at ≥{GATE_WINPCT:.0f}% win rate")
        print(f"     Have  : {settled} settled  |  {win_pct:.1f}% win rate")
        if needed > 0:
            print(f"     Action: Keep paper bot running. Need {needed} more settled picks.")
        else:
            print(f"     Action: Win rate {win_pct:.1f}% is below threshold. Continue monitoring.")
        print(f"\n  PAPER BOT COMMAND:")
        print(f"    cd C:\\Users\\Kevan\\kalishi-edge")
        print(f"    python scripts\\run_multi.py --paper --loop --loop-seconds 60")
    else:
        print(f"  ✔  GO  \u2014 all gates met: {settled} picks, {win_pct:.1f}% win rate")
        print(f"\n  EXECUTE BOT COMMAND (review once more before running):")
        print(f"    cd C:\\Users\\Kevan\\kalishi-edge")
        print(f"    python scripts\\run_multi.py --execute --loop --loop-seconds 30 \\")
        print(f"      --wait-minutes 3.0 --min-edge 12 --max-contracts 2 \\")
        print(f"      --balance-floor 5.50 --crypto-only")
    print("  " + "═" * 58)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print()
    print("═" * 64)
    print(f"   KALISHI EDGE  \u2014  System Status  [{now}]")
    print("═" * 64)

    if args.settle:
        print("\n  Running settlement check first...")
        try:
            from scripts.settle_paper import settle_file
            stats = await settle_file(_PAPER_LOG)
            print(f"  Settlement complete: +{stats['updated']} resolved  "
                  f"({stats['wins']} wins, {stats['losses']} losses)")
        except Exception as e:
            print(f"  Settlement check failed: {e}")

    gpu    = _section_gpu()
    model  = _section_model()
    paper  = _section_paper()
    api    = await _check_api()

    # Show API results inline
    print("\n  LIVE DATA")
    print("  " + "─" * 58)
    if api.get("api_ok"):
        print(f"    Kalshi API:  OK  (balance=${api['balance']:.2f})")
    else:
        print(f"    Kalshi API:  FAIL  ({api.get('api_error', '?')})")
    if api.get("momentum_ok"):
        print(f"    Price feed:  OK  (BTC=${api['btc_live']:.0f}  live={api['btc_spot']})")
    else:
        print(f"    Price feed:  FAIL  ({api.get('momentum_error', '?')})")

    checks = _section_preflight(gpu, model, paper, api)
    _section_verdict(checks, paper)
    print()


def run():
    ap = argparse.ArgumentParser(description="Kalishi Edge operator health dashboard")
    ap.add_argument("--settle", action="store_true", help="Run settlement check first")
    ap.add_argument("--balance-floor", type=float, default=5.50)
    args = ap.parse_args()
    asyncio.run(main(args))


if __name__ == "__main__":
    run()
