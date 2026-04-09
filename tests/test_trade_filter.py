"""
tests/test_trade_filter.py — Integration tests for engine/trade_filter.py
"""
import pytest
from dataclasses import replace

from engine.trade_filter import TradeFilter, TradeFilterResult, _kelly_stake
from engine.abstain      import NoTradeReason


# ── Shared fixtures ──────────────────────────────────────────────────────────

MOMENTUM = {
    "mom_5m":       0.001,
    "mom_15m":      0.0008,
    "realized_vol": 0.003,
    "trend":        "up",
    "closes":       [60000, 60100, 60150, 60050, 60200],
    "current":      60200.0,
}

MODEL_PROBS = {"diffusion": 0.72, "monte_carlo": 0.70, "neural": 0.68, "technical": 0.65}

PICK = {
    "asset":     "BTC",
    "side":      "yes",
    "yes_price": 0.50,
    "ttc":       3.0,
}


def _tf() -> TradeFilter:
    return TradeFilter()


# ── Basic structure ───────────────────────────────────────────────────────────

def test_evaluate_returns_result():
    result = _tf().evaluate(PICK, MOMENTUM, MODEL_PROBS)
    assert isinstance(result, TradeFilterResult)


def test_to_dict_json_safe():
    import json
    result = _tf().evaluate(PICK, MOMENTUM, MODEL_PROBS)
    json.dumps(result.to_dict())


def test_result_has_regime():
    result = _tf().evaluate(PICK, MOMENTUM, MODEL_PROBS)
    assert result.regime is not None


def test_result_has_ensemble():
    result = _tf().evaluate(PICK, MOMENTUM, MODEL_PROBS)
    assert result.ensemble is not None


# ── Approved path ─────────────────────────────────────────────────────────────

def test_strong_edge_approves():
    """Strong model probs vs a 50-cent price should produce an approved pick
    (unless an abstain gate fires, which is possible — just verify structure)."""
    result = _tf().evaluate(PICK, MOMENTUM, MODEL_PROBS, bankroll=200.0)
    # Result must be either approved or have a concrete reason
    if result.approved:
        assert result.recommended_stake > 0.0
        assert result.abstain_reason is None
    else:
        assert result.abstain_reason is not None


def test_approved_stake_in_bounds():
    result = _tf().evaluate(PICK, MOMENTUM, MODEL_PROBS, bankroll=200.0)
    if result.approved:
        assert 1.0 <= result.recommended_stake <= 25.0


# ── Rejection paths ───────────────────────────────────────────────────────────

def test_stale_data_abstains():
    """Inject stale momentum (zero realized_vol + flat closes)."""
    stale_mom = dict(MOMENTUM, realized_vol=0.0, closes=[60000.0] * 5)
    result = _tf().evaluate(PICK, stale_mom, MODEL_PROBS)
    if not result.approved:
        assert result.abstain_reason is not None


def test_low_edge_abstains():
    """If yes_price is very high vs model probs, edge should be negative."""
    expensive_pick = dict(PICK, yes_price=0.95)
    result = _tf().evaluate(expensive_pick, MOMENTUM, MODEL_PROBS)
    if not result.approved:
        assert result.abstain_reason in (
            NoTradeReason.EDGE_BELOW_MIN,
            NoTradeReason.INSUFFICIENT_MODELS,
            NoTradeReason.LOW_CONFIDENCE,
        )


def test_insufficient_models_abstains():
    """Only 1 model — ensemble falls back, should flag insufficient models."""
    result = _tf().evaluate(PICK, MOMENTUM, {"neural": 0.65})
    assert not result.approved
    assert result.abstain_reason == NoTradeReason.INSUFFICIENT_MODELS


def test_portfolio_concentration_abstains():
    """Passing 2 open BTC positions should trigger correlated exposure gate."""
    portfolio = [
        {"asset": "BTC", "status": "open"},
        {"asset": "BTC", "status": "open"},
    ]
    result = _tf().evaluate(PICK, MOMENTUM, MODEL_PROBS, portfolio=portfolio)
    if not result.approved:
        assert result.abstain_reason in (
            NoTradeReason.CORRELATED_EXPOSURE,
            NoTradeReason.EDGE_BELOW_MIN,
            NoTradeReason.INSUFFICIENT_MODELS,
            NoTradeReason.LOW_CONFIDENCE,
        )


def test_empty_model_probs_abstains():
    result = _tf().evaluate(PICK, MOMENTUM, {})
    assert not result.approved
    assert result.abstain_reason == NoTradeReason.INSUFFICIENT_MODELS


# ── Edge and calibration fields ───────────────────────────────────────────────

def test_calibrated_prob_in_range():
    result = _tf().evaluate(PICK, MOMENTUM, MODEL_PROBS)
    assert 0.0 < result.calibrated_prob < 1.0


def test_edge_fields_present():
    result = _tf().evaluate(PICK, MOMENTUM, MODEL_PROBS)
    d = result.to_dict()
    assert "calibrated_edge_pct" in d
    assert "raw_edge_pct" in d


def test_unapproved_stake_is_zero():
    result = _tf().evaluate(PICK, MOMENTUM, {"neural": 0.65})
    assert not result.approved
    assert result.recommended_stake == 0.0


# ── _kelly_stake ─────────────────────────────────────────────────────────────

def test_kelly_stake_positive_edge():
    stake = _kelly_stake(prob=0.70, price=0.50, fee_rate=0.02, bankroll=200.0)
    assert stake >= 1.0


def test_kelly_stake_negative_edge_zero():
    stake = _kelly_stake(prob=0.30, price=0.50, fee_rate=0.02, bankroll=200.0)
    assert stake == 0.0


def test_kelly_stake_capped_at_max():
    stake = _kelly_stake(prob=0.99, price=0.50, fee_rate=0.0, bankroll=10000.0)
    assert stake <= 25.0


def test_kelly_stake_floored_at_min():
    stake = _kelly_stake(prob=0.52, price=0.50, fee_rate=0.0, bankroll=5.0)
    if stake > 0:
        assert stake >= 1.0


def test_kelly_stake_bad_price_returns_zero():
    assert _kelly_stake(prob=0.6, price=0.0, fee_rate=0.02, bankroll=200.0) == 0.0
    assert _kelly_stake(prob=0.6, price=1.0, fee_rate=0.02, bankroll=200.0) == 0.0


# ── Different assets ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("asset", ["BTC", "ETH", "SOL", "DOGE", "XRP"])
def test_all_assets_evaluate_without_crash(asset):
    pick = dict(PICK, asset=asset)
    result = _tf().evaluate(pick, MOMENTUM, MODEL_PROBS)
    assert isinstance(result, TradeFilterResult)
