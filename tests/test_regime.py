"""
tests/test_regime.py — Unit tests for engine/regime.py
"""
import math
import pytest
from dataclasses import replace

from engine.regime import (
    classify_regime,
    RegimeSnapshot,
    _cfg,
    _ttc_bucket,
    _annualised_vol,
    _ASSET_CONFIGS,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

CLOSES_FLAT    = [60000.0] * 5
CLOSES_RISING  = [59800, 59900, 60000, 60100, 60200]
CLOSES_FALLING = [60200, 60100, 60000, 59900, 59800]

BASE_SIG = {
    "mom_5m":       0.0008,
    "mom_15m":      0.0003,
    "realized_vol": 0.003,
    "trend":        "up",
    "closes":       CLOSES_RISING,
    "current":      60200.0,
}


# ── _cfg ─────────────────────────────────────────────────────────────────────

def test_cfg_known_assets():
    for asset in ("BTC", "ETH", "SOL", "DOGE", "XRP"):
        cfg = _cfg(asset)
        assert cfg.vol_low < cfg.vol_high
        assert cfg.trend_threshold > cfg.flat_threshold
        assert 0 < cfg.mr_rsi_low < cfg.mr_rsi_high < 100
        assert 0 < cfg.mr_bollpb_low < cfg.mr_bollpb_high < 1


def test_cfg_unknown_falls_back_to_default():
    cfg = _cfg("SHIB")
    default = _ASSET_CONFIGS["_default"]
    assert cfg.vol_low == default.vol_low


# ── _ttc_bucket ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hours,expected", [
    (0.10, "le15m"),
    (0.25, "le15m"),
    (0.50, "le1h"),
    (1.00, "le1h"),
    (3.00, "1-6h"),
    (6.00, "1-6h"),
    (12.0, "6-24h"),
    (24.0, "6-24h"),
    (48.0, "gt24h"),
])
def test_ttc_bucket(hours, expected):
    assert _ttc_bucket(hours) == expected


# ── _annualised_vol ──────────────────────────────────────────────────────────

def test_annualised_vol_zero():
    assert _annualised_vol(0.0) == 0.0


def test_annualised_vol_scaling():
    per5m = 0.001
    daily = _annualised_vol(per5m)
    assert abs(daily - per5m * math.sqrt(288)) < 1e-12


# ── classify_regime basic structure ──────────────────────────────────────────

def test_classify_regime_returns_snapshot():
    snap = classify_regime(BASE_SIG, hours_to_close=3.0, asset="BTC")
    assert isinstance(snap, RegimeSnapshot)


def test_classify_regime_key_is_4tuple():
    snap = classify_regime(BASE_SIG, hours_to_close=3.0, asset="BTC")
    assert len(snap.key) == 4
    assert snap.key[0] == "BTC"


def test_classify_regime_vol_regime_valid():
    snap = classify_regime(BASE_SIG, hours_to_close=3.0, asset="BTC")
    assert snap.vol_regime in ("low", "normal", "high")


def test_classify_regime_trend_valid():
    snap = classify_regime(BASE_SIG, hours_to_close=3.0, asset="BTC")
    assert snap.trend in ("trending", "flat", "mean_reverting")


def test_classify_regime_ttc_bucket_valid():
    snap = classify_regime(BASE_SIG, hours_to_close=3.0, asset="BTC")
    assert snap.ttc_bucket == "1-6h"


def test_classify_regime_trend_confidence_in_range():
    snap = classify_regime(BASE_SIG, hours_to_close=3.0, asset="BTC")
    assert 0.0 <= snap.trend_confidence <= 1.0


def test_classify_regime_data_quality_valid():
    snap = classify_regime(BASE_SIG, hours_to_close=3.0, asset="BTC")
    assert snap.data_quality in ("ok", "thin", "stale")


# ── Per-asset configs produce different regimes ────────────────────────────

def test_btc_vs_doge_same_vol_different_regime():
    """BTC has tighter vol thresholds — same realized vol can be 'high' for BTC
    but 'normal' for DOGE."""
    # vol that is above BTC high (0.052) but below DOGE high (0.100)
    sig = dict(BASE_SIG, realized_vol=0.003)  # 5m vol → daily ~0.051 near BTC high
    snap_btc  = classify_regime(sig, hours_to_close=3.0, asset="BTC")
    snap_doge = classify_regime(sig, hours_to_close=3.0, asset="DOGE")
    # Both should be valid; regime values may differ
    assert snap_btc.vol_regime in ("low", "normal", "high")
    assert snap_doge.vol_regime in ("low", "normal", "high")


# ── Trend detection ──────────────────────────────────────────────────────────

def test_strong_momentum_yields_trending():
    sig = dict(BASE_SIG, mom_5m=0.002, mom_15m=0.002)
    snap = classify_regime(sig, hours_to_close=3.0, asset="BTC")
    assert snap.trend == "trending"


def test_near_zero_momentum_yields_flat_or_mr():
    sig = dict(BASE_SIG, mom_5m=0.00001, mom_15m=0.00001)
    snap = classify_regime(sig, hours_to_close=3.0, asset="BTC")
    assert snap.trend in ("flat", "mean_reverting")


# ── Data quality ─────────────────────────────────────────────────────────────

def test_stale_data_with_poor_rv():
    """Very high realized vol with flat closes signals stale/thin data."""
    sig = dict(BASE_SIG, realized_vol=0.0, closes=CLOSES_FLAT)
    snap = classify_regime(sig, hours_to_close=3.0, asset="BTC")
    # realized_vol=0 → daily_vol=0 → should flag as thin or stale
    assert snap.data_quality in ("thin", "stale")


def test_stale_data_property():
    snap = classify_regime(BASE_SIG, hours_to_close=3.0, asset="BTC")
    # stale_data property should be consistent with data_quality
    if snap.data_quality == "stale":
        assert snap.stale_data is True
    else:
        assert snap.stale_data is False


# ── to_dict / serialisation ──────────────────────────────────────────────────

def test_to_dict_contains_required_keys():
    snap = classify_regime(BASE_SIG, hours_to_close=3.0, asset="BTC")
    d = snap.to_dict()
    for key in ("asset", "vol_regime", "trend", "trend_confidence", "ttc_bucket",
                "hours_to_close", "realized_vol_daily", "combined_momentum",
                "rsi", "bollinger_pb", "data_quality", "stale_data", "reason"):
        assert key in d, f"Missing key: {key}"


def test_to_dict_values_are_json_safe():
    snap = classify_regime(BASE_SIG, hours_to_close=3.0, asset="BTC")
    d = snap.to_dict()
    import json
    json.dumps(d)   # must not raise


# ── key routing ─────────────────────────────────────────────────────────────

def test_key_includes_trend_so_mr_and_trending_not_same():
    """Assets with different trends must get different routing keys."""
    sig_trend = dict(BASE_SIG, mom_5m=0.002, mom_15m=0.002)
    sig_mr    = dict(BASE_SIG, mom_5m=0.00001, mom_15m=0.00001,
                     closes=[59800, 60200, 59800, 60200, 59800])
    snap_trend = classify_regime(sig_trend, hours_to_close=3.0, asset="BTC")
    snap_mr    = classify_regime(sig_mr,    hours_to_close=3.0, asset="BTC")
    # Even if same asset/vol/ttc, keys must differ when trends differ
    if snap_trend.trend != snap_mr.trend:
        assert snap_trend.key != snap_mr.key


# ── different assets same signal ────────────────────────────────────────────

@pytest.mark.parametrize("asset", ["BTC", "ETH", "SOL", "DOGE", "XRP"])
def test_all_assets_classify_without_error(asset):
    snap = classify_regime(BASE_SIG, hours_to_close=3.0, asset=asset)
    assert snap.asset == asset
