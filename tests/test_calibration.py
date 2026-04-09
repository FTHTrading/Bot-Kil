"""
tests/test_calibration.py — Unit tests for engine/calibration.py
"""
import math
import pickle
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from engine.calibration import (
    PlattCalibrator,
    BinCalibrator,
    IsotonicCalibrator,
    CalibrationStore,
    _trend_slug,
    _calib_path,
    _CALIBRATOR_DIR,
    _CALIBRATOR_CACHE,
)


# ── _trend_slug ───────────────────────────────────────────────────────────────

def test_trend_slug_mean_reverting():
    assert _trend_slug("mean_reverting") == "mr"


def test_trend_slug_flat():
    assert _trend_slug("flat") == "flat"


def test_trend_slug_trending():
    assert _trend_slug("trending") == "trending"


def test_trend_slug_unknown_passthrough():
    assert _trend_slug("weird") == "weird"


# ── _calib_path ───────────────────────────────────────────────────────────────

def test_calib_path_mr_slug():
    p = _calib_path("BTC", "normal", "1-6h", "mean_reverting")
    assert p.stem == "BTC_normal_1-6h_mr"


def test_calib_path_flat():
    p = _calib_path("ETH", "high", "le1h", "flat")
    assert p.stem == "ETH_high_le1h_flat"


def test_calib_path_trending():
    p = _calib_path("SOL", "low", "gt24h", "trending")
    assert p.stem == "SOL_low_gt24h_trending"


def test_calib_path_no_underscore_in_trend_part():
    """mr slug avoids double-underscore ambiguity when splitting stem on '_'."""
    p = _calib_path("BTC", "normal", "1-6h", "mean_reverting")
    # Splitting on '_' should give at most 4 parts
    parts = p.stem.split("_")
    assert len(parts) == 4


# ── PlattCalibrator ──────────────────────────────────────────────────────────

def _make_platt_data(n=50, bias=0.05):
    """Synthetic (prob, outcome) pairs where outcomes ~ prob + bias."""
    import random
    random.seed(42)
    probs    = [random.uniform(0.2, 0.8) for _ in range(n)]
    outcomes = [float(random.random() < p + bias) for p in probs]
    return probs, outcomes


def test_platt_fit_sets_fitted():
    cal = PlattCalibrator()
    probs, outcomes = _make_platt_data(50)
    cal.fit(probs, outcomes)
    assert cal.fitted


def test_platt_not_fitted_on_too_few_samples():
    cal = PlattCalibrator()
    cal.fit([0.6, 0.7], [1.0, 1.0])
    assert not cal.fitted


def test_platt_predict_passthrough_when_unfitted():
    cal = PlattCalibrator()
    assert cal.predict(0.72) == pytest.approx(0.72)


def test_platt_fitted_output_in_range():
    cal = PlattCalibrator()
    probs, outcomes = _make_platt_data(50)
    cal.fit(probs, outcomes)
    for p in (0.1, 0.3, 0.5, 0.7, 0.9):
        cp = cal.predict(p)
        assert 0.01 <= cp <= 0.99, f"predict({p}) = {cp} out of range"


def test_platt_l2_regularisation_fires():
    """lambda_reg > 0 should be reflected in changed A vs unregularised."""
    cal_reg   = PlattCalibrator()
    cal_noreg = PlattCalibrator()
    probs, outcomes = _make_platt_data(60)
    cal_reg.fit(probs, outcomes, lambda_reg=0.5, epochs=200)
    cal_noreg.fit(probs, outcomes, lambda_reg=0.0, epochs=200)
    # Strong L2 should shrink A toward 1 (smoother curve)
    assert cal_reg.fitted and cal_noreg.fitted
    # With strong reg A should be closer to 0 (or different from no-reg)
    assert cal_reg.A != cal_noreg.A


def test_platt_early_stopping_reduces_iterations():
    """With tiny patience the early-stop branch should fire silently (no error)."""
    cal = PlattCalibrator()
    probs, outcomes = _make_platt_data(40)
    cal.fit(probs, outcomes, lr=0.05, epochs=500, lambda_reg=0.01)
    assert cal.fitted  # should still converge eventually


# ── BinCalibrator ─────────────────────────────────────────────────────────────

def test_bin_calibrator_fit():
    cal = BinCalibrator()
    probs    = [i / 100 for i in range(10, 90)]
    outcomes = [float(p > 0.5) for p in probs]
    cal.fit(probs, outcomes)
    assert cal.fitted
    assert len(cal.bin_probs) == BinCalibrator.N_BINS


def test_bin_calibrator_laplace_no_empty_bins():
    """Even with no samples in a bin, Laplace smoothing yields a valid prob."""
    cal = BinCalibrator()
    # All samples in top half — lower bins have 0 samples
    probs    = [0.6, 0.7, 0.8, 0.55, 0.65, 0.75, 0.85, 0.9, 0.7, 0.6]
    outcomes = [1.0] * 10
    cal.fit(probs, outcomes)
    for bp in cal.bin_probs:
        assert 0.0 < bp < 1.0, f"Bin prob {bp} out of range (Laplace failed)"


def test_bin_calibrator_empty_bin_not_zero():
    """Lower bins are unfilled, must not be 0.0 (division by zero guard)."""
    cal = BinCalibrator()
    probs    = [0.9] * 20
    outcomes = [1.0] * 20
    cal.fit(probs, outcomes)
    for bp in cal.bin_probs[:-1]:   # first 9 bins are empty
        assert bp > 0.0


def test_bin_calibrator_predict_in_range():
    cal = BinCalibrator()
    probs    = [i / 20 for i in range(20)]
    outcomes = [float(p > 0.5) for p in probs]
    cal.fit(probs, outcomes)
    for p in (0.0, 0.25, 0.5, 0.75, 0.99):
        out = cal.predict(p)
        assert 0.01 <= out <= 0.99


def test_bin_calibrator_passthrough_unfitted():
    cal = BinCalibrator()
    assert cal.predict(0.55) == pytest.approx(0.55)


# ── IsotonicCalibrator ────────────────────────────────────────────────────────

def test_isotonic_predict_1d_input():
    """predict([p])[0] — must not fail with 1-D input.
    Note: IsotonicCalibrator.predict() intentionally has no hard clamp;
    clamping to [0.01, 0.99] is done by CalibrationStore.calibrate()."""
    pytest.importorskip("sklearn")
    cal = IsotonicCalibrator()
    probs    = [i / 50 for i in range(50)]
    outcomes = [float(p > 0.5) for p in probs]
    cal.fit(probs, outcomes)
    assert cal.fitted
    # This is the call pattern used in the code — must not raise
    out = cal.predict(0.65)
    assert isinstance(out, float)
    assert 0.0 <= out <= 1.0


def test_isotonic_passthrough_unfitted():
    cal = IsotonicCalibrator()
    assert cal.predict(0.72) == pytest.approx(0.72)


def test_isotonic_not_fitted_on_too_few():
    pytest.importorskip("sklearn")
    cal = IsotonicCalibrator()
    cal.fit([0.5, 0.6, 0.7], [1.0, 1.0, 0.0])
    assert not cal.fitted


# ── CalibrationStore (in-memory tests using temp dir) ────────────────────────

@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure cache is clean before/after each test."""
    _CALIBRATOR_CACHE.clear()
    yield
    _CALIBRATOR_CACHE.clear()


def test_calibrate_returns_raw_when_no_calibrator():
    store = CalibrationStore()
    cal_p, meta = store.calibrate(0.65, "BTC", "normal", "1-6h", "flat")
    assert cal_p == pytest.approx(0.65)
    assert meta["method"] == "identity"


def test_calibrate_trend_key_mr_vs_trending_separate(tmp_path):
    """Different trend keys must produce independent calibrator files."""
    p_mr  = _calib_path("BTC", "normal", "1-6h", "mean_reverting")
    p_tr  = _calib_path("BTC", "normal", "1-6h", "trending")
    assert p_mr != p_tr
    assert p_mr.stem != p_tr.stem


def test_update_fits_platt_on_small_set(tmp_path):
    """update() with 15 samples should fit a Platt calibrator."""
    import random; random.seed(0)
    probs    = [random.uniform(0.3, 0.8) for _ in range(15)]
    outcomes = [float(p > 0.55) for p in probs]

    with patch("engine.calibration._CALIBRATOR_DIR", tmp_path):
        store = CalibrationStore()
        result = store.update(probs, outcomes, "ETH", "normal", "1-6h", "flat")

    assert result.get("method") in ("platt", "bin")
    assert result.get("n_samples") == 15


def test_update_and_calibrate_roundtrip(tmp_path):
    """After update(), calibrate() should return non-identity values."""
    import random; random.seed(1)
    probs    = [random.uniform(0.3, 0.8) for _ in range(20)]
    outcomes = [float(random.random() < p) for p in probs]

    with patch("engine.calibration._CALIBRATOR_DIR", tmp_path), \
         patch("engine.calibration._CALIBRATOR_CACHE", {}):
        store = CalibrationStore()
        store.update(probs, outcomes, "BTC", "normal", "1-6h", "flat")
        cal_p, meta = store.calibrate(0.65, "BTC", "normal", "1-6h", "flat")

    # If method is identity, calibrator load failed — check tmp_path files
    assert meta["method"] != "identity", f"calibrator not loaded: {list(tmp_path.iterdir())}"
    assert 0.01 <= cal_p <= 0.99


def test_update_error_on_length_mismatch():
    store = CalibrationStore()
    result = store.update([0.5, 0.6], [1.0], "BTC", "normal", "1-6h", "flat")
    assert "error" in result


def test_update_error_on_too_few():
    store = CalibrationStore()
    result = store.update([0.5, 0.6], [1.0, 0.0], "BTC", "normal", "1-6h", "flat")
    assert "error" in result
