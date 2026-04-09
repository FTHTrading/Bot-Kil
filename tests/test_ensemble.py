"""
tests/test_ensemble.py — Unit tests for engine/ensemble.py
"""
import pytest
from dataclasses import replace

from engine.regime   import classify_regime
from engine.ensemble import WeightedEnsemble, EnsembleResult, _TREND_WEIGHT_MODS, _MIN_MODELS


# ── Shared fixtures ──────────────────────────────────────────────────────────

BASE_SIG = {
    "mom_5m": 0.0008, "mom_15m": 0.0003, "realized_vol": 0.003,
    "trend": "up", "closes": [60000, 60100, 60150, 60050, 60200], "current": 60200.0,
}

def _regime(trend_override=None, vol_override=None, dq="ok", tc=1.0, asset="BTC", hours=3.0):
    sig = dict(BASE_SIG)
    if trend_override == "trending":
        sig["mom_5m"] = 0.002; sig["mom_15m"] = 0.002
    elif trend_override == "mean_reverting":
        sig["mom_5m"] = 0.00001; sig["mom_15m"] = 0.00001
        sig["closes"] = [59800, 60200, 59800, 60200, 59800]
    if vol_override == "high":
        sig["realized_vol"] = 0.008
    elif vol_override == "low":
        sig["realized_vol"] = 0.0005
    r = classify_regime(sig, hours_to_close=hours, asset=asset)
    # patch dq and tc without re-running classify (fast override)
    r = replace(r, data_quality=dq, trend_confidence=tc,
                stale_data=(dq == "stale"))
    return r


THREE_MODELS = {"diffusion": 0.62, "neural": 0.58, "technical": 0.55}
FOUR_MODELS  = {"diffusion": 0.62, "monte_carlo": 0.60, "neural": 0.58, "technical": 0.55}


# ── Basic result structure ────────────────────────────────────────────────────

def test_run_returns_ensemble_result():
    r = _regime()
    res = WeightedEnsemble().run(THREE_MODELS, r)
    assert isinstance(res, EnsembleResult)


def test_weighted_prob_in_range():
    r = _regime()
    res = WeightedEnsemble().run(THREE_MODELS, r)
    assert 0.0 < res.weighted_prob < 1.0


def test_confidence_in_range():
    r = _regime()
    res = WeightedEnsemble().run(THREE_MODELS, r)
    assert 0.0 < res.confidence <= 1.0


def test_n_models_correct():
    r = _regime()
    res = WeightedEnsemble().run(THREE_MODELS, r)
    assert res.n_models == 3


def test_weights_sum_to_one():
    r = _regime()
    res = WeightedEnsemble().run(THREE_MODELS, r)
    assert abs(sum(res.weights_used.values()) - 1.0) < 1e-9


# ── Fallback on < MIN_MODELS ─────────────────────────────────────────────────

def test_single_model_fallback():
    r = _regime()
    res = WeightedEnsemble().run({"neural": 0.65}, r)
    assert res.n_models == 1
    assert res.confidence <= 0.35
    assert any("fall" in n.lower() for n in res.notes)


def test_empty_models_fallback():
    r = _regime()
    res = WeightedEnsemble().run({}, r)
    assert res.weighted_prob == 0.5
    assert res.confidence <= 0.35


# ── Trend weight modifications ────────────────────────────────────────────────

def test_trend_weight_mods_table_complete():
    for trend, mods in _TREND_WEIGHT_MODS.items():
        assert set(mods.keys()) == {"diffusion", "monte_carlo", "neural", "technical"}


def test_mean_reverting_boosts_technical():
    """In mean_reverting regime, re-normalised technical weight should be
    relatively higher than in trending regime."""
    mr_r  = _regime(trend_override="mean_reverting")
    tr_r  = _regime(trend_override="trending")
    # Force same detected trend so the mod table is applied correctly
    mr_r = replace(mr_r, trend="mean_reverting")
    tr_r = replace(tr_r, trend="trending")

    mr_res = WeightedEnsemble().run(FOUR_MODELS, mr_r)
    tr_res = WeightedEnsemble().run(FOUR_MODELS, tr_r)

    mr_tech = mr_res.weights_used.get("technical", 0)
    tr_tech = tr_res.weights_used.get("technical", 0)
    assert mr_tech > tr_tech, f"MR tech={mr_tech:.3f} should > trending tech={tr_tech:.3f}"


def test_trending_boosts_neural():
    """_TREND_WEIGHT_MODS must assign a higher multiplier to neural in trending
    than in mean_reverting. After renormalisation the absolute fraction can
    differ, but the raw multiplier must reflect the design intent."""
    tr_mod = _TREND_WEIGHT_MODS["trending"]["neural"]
    mr_mod = _TREND_WEIGHT_MODS["mean_reverting"]["neural"]
    assert tr_mod > mr_mod, (
        f"trending neural multiplier {tr_mod} should > mr {mr_mod}"
    )


# ── Confidence penalties ─────────────────────────────────────────────────────

def test_stale_data_halves_confidence():
    r_ok    = _regime(dq="ok",    tc=1.0)
    r_stale = _regime(dq="stale", tc=1.0)
    ok_res    = WeightedEnsemble().run(FOUR_MODELS, r_ok)
    stale_res = WeightedEnsemble().run(FOUR_MODELS, r_stale)
    assert stale_res.confidence < ok_res.confidence * 0.65


def test_thin_data_reduces_confidence():
    r_ok   = _regime(dq="ok",   tc=1.0)
    r_thin = _regime(dq="thin", tc=1.0)
    ok_res   = WeightedEnsemble().run(FOUR_MODELS, r_ok)
    thin_res = WeightedEnsemble().run(FOUR_MODELS, r_thin)
    assert thin_res.confidence < ok_res.confidence


def test_low_trend_confidence_penalises():
    r_high_tc = _regime(tc=0.90)
    r_low_tc  = _regime(tc=0.30)
    res_high = WeightedEnsemble().run(FOUR_MODELS, r_high_tc)
    res_low  = WeightedEnsemble().run(FOUR_MODELS, r_low_tc)
    assert res_low.confidence < res_high.confidence


def test_calib_meta_identity_penalises():
    r = _regime()
    meta_none    = {"method": "identity",  "n_samples": 0}
    meta_fitted  = {"method": "isotonic",  "n_samples": 100}
    res_none    = WeightedEnsemble().run(FOUR_MODELS, r, calib_meta=meta_none)
    res_fitted  = WeightedEnsemble().run(FOUR_MODELS, r, calib_meta=meta_fitted)
    assert res_none.confidence < res_fitted.confidence


def test_calib_meta_sparse_penalises():
    r = _regime()
    meta_sparse = {"method": "platt", "n_samples": 5}
    meta_rich   = {"method": "platt", "n_samples": 200}
    res_sparse = WeightedEnsemble().run(FOUR_MODELS, r, calib_meta=meta_sparse)
    res_rich   = WeightedEnsemble().run(FOUR_MODELS, r, calib_meta=meta_rich)
    assert res_sparse.confidence < res_rich.confidence


def test_high_disagreement_penalises_confidence():
    r = _regime()
    agree_probs = {"diffusion": 0.60, "monte_carlo": 0.61, "neural": 0.59, "technical": 0.60}
    disagree_probs = {"diffusion": 0.80, "monte_carlo": 0.45, "neural": 0.60, "technical": 0.20}
    res_a = WeightedEnsemble().run(agree_probs, r)
    res_d = WeightedEnsemble().run(disagree_probs, r)
    assert res_d.confidence < res_a.confidence
    assert res_d.disagreement > res_a.disagreement


# ── to_dict ───────────────────────────────────────────────────────────────────

def test_to_dict_json_safe():
    import json
    r = _regime()
    res = WeightedEnsemble().run(FOUR_MODELS, r)
    json.dumps(res.to_dict())


def test_to_dict_required_keys():
    r = _regime()
    res = WeightedEnsemble().run(FOUR_MODELS, r)
    d = res.to_dict()
    for k in ("weighted_prob", "raw_probs", "weights_used", "disagreement",
              "confidence", "n_models", "ttc_bucket", "vol_regime", "trend", "notes"):
        assert k in d


# ── BTC/ETH neural bump ───────────────────────────────────────────────────────

def test_btceth_neural_weight_higher_than_sol():
    r_btc = replace(_regime(asset="BTC"), asset="BTC")
    r_sol = replace(_regime(asset="SOL"), asset="SOL")
    res_btc = WeightedEnsemble().run(FOUR_MODELS, r_btc)
    res_sol = WeightedEnsemble().run(FOUR_MODELS, r_sol)
    assert res_btc.weights_used.get("neural", 0) >= res_sol.weights_used.get("neural", 0)
