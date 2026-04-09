"""
tests/test_portfolio_guard.py — Unit tests for engine/portfolio_guard.py
"""
import pytest
from engine.portfolio_guard import (
    PortfolioGuard,
    PortfolioVerdict,
    _MAX_SAME_ASSET_BETS,
    _MAX_TOTAL_OPEN_BETS,
    _MAX_OPEN_EXPOSURE_PCT,
    _MAX_BTC_CORRELATED_BETS,
    _BTC_CORRELATED_ASSETS,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pos(asset="BTC", status="open", cost=5.0):
    return {"asset": asset, "status": status, "cost": cost}


def _check(candidate_asset, positions, bankroll=200.0):
    pg = PortfolioGuard()
    return pg.check({"asset": candidate_asset}, positions, bankroll=bankroll)


# ── Happy path ────────────────────────────────────────────────────────────────

def test_empty_portfolio_allowed():
    v = _check("BTC", [])
    assert v.allowed


def test_single_open_allowed():
    v = _check("BTC", [_pos("ETH")])
    assert v.allowed


def test_verdict_to_dict():
    v = _check("BTC", [])
    d = v.to_dict()
    assert "allowed" in d


# ── Gate 1: Total open bet count ─────────────────────────────────────────────

def test_total_open_limit_blocks():
    positions = [_pos() for _ in range(_MAX_TOTAL_OPEN_BETS)]
    v = _check("SOL", positions)
    assert not v.allowed
    assert str(_MAX_TOTAL_OPEN_BETS) in v.reason


def test_one_below_total_limit_allowed():
    positions = [_pos() for _ in range(_MAX_TOTAL_OPEN_BETS - 1)]
    v = _check("SOL", positions)
    # Should not be blocked by TOTAL gate (may be blocked by other gates)
    assert "total open" not in (v.reason or "").lower()


def test_settled_positions_not_counted():
    """Settled / closed positions must not count toward the total limit."""
    positions = (
        [_pos(status="settled") for _ in range(_MAX_TOTAL_OPEN_BETS + 2)]
        + [_pos(status="open")]
    )
    v = _check("ETH", positions)
    # Only 1 active — should be allowed
    assert v.allowed


# ── Gate 2: Same-asset concentration ─────────────────────────────────────────

def test_same_asset_concentration_blocks():
    positions = [_pos("BTC") for _ in range(_MAX_SAME_ASSET_BETS)]
    v = _check("BTC", positions)
    assert not v.allowed
    assert "BTC" in v.reason


def test_one_below_same_asset_limit_allowed():
    positions = [_pos("BTC") for _ in range(_MAX_SAME_ASSET_BETS - 1)]
    v = _check("BTC", positions)
    # Not blocked by same-asset gate
    assert "already" not in (v.reason or "")


def test_different_asset_allowed_even_if_full():
    positions = [_pos("BTC") for _ in range(_MAX_SAME_ASSET_BETS)]
    v = _check("SOL", positions)   # adding SOL, not BTC
    # Not blocked by same-asset gate for BTC
    assert v.allowed or "BTC" not in (v.reason or "")


# ── Gate 3: BTC-correlated cluster ───────────────────────────────────────────

def test_btc_cluster_blocks_eth():
    """ETH is in the BTC cluster; _MAX_BTC_CORRELATED_BETS already open BTC/ETH."""
    positions = [_pos("BTC"), _pos("BTC"), _pos("ETH")]
    # 3 BTC-correlated assets already open → blocked
    v = _check("ETH", positions)
    assert not v.allowed
    assert "cluster" in v.reason.lower()


def test_btc_cluster_blocks_btc():
    positions = [_pos("BTC"), _pos("BTC"), _pos("ETH")]
    v = _check("BTC", positions)
    # Blocked by same-asset or cluster
    assert not v.allowed


def test_sol_not_in_btc_cluster():
    """SOL is NOT in the BTC cluster."""
    positions = [_pos("BTC"), _pos("BTC"), _pos("ETH")]
    v = _check("SOL", positions)
    # Should not be blocked by the BTC cluster gate
    # (may be blocked by total exposure if high-cost, but not cluster)
    if not v.allowed:
        assert "cluster" not in v.reason.lower()


def test_btc_correlated_assets_constants():
    assert "BTC" in _BTC_CORRELATED_ASSETS
    assert "ETH" in _BTC_CORRELATED_ASSETS


# ── Gate 4: Capital exposure ──────────────────────────────────────────────────

def test_high_exposure_blocks():
    bankroll  = 100.0
    threshold = bankroll * _MAX_OPEN_EXPOSURE_PCT   # 40.0
    positions = [_pos("SOL", cost=threshold + 1)]   # 41.0 / 100 = 41%
    v = _check("SOL", positions, bankroll=bankroll)
    assert not v.allowed
    assert "exposure" in v.reason.lower()


def test_low_exposure_allowed():
    bankroll  = 200.0
    positions = [_pos("SOL", cost=5.0)]   # 2.5% exposure
    v = _check("SOL", positions, bankroll=bankroll)
    assert v.allowed


def test_zero_bankroll_exposure_check_skipped():
    """Division by zero guard — zero bankroll should not crash."""
    positions = [_pos("BTC", cost=999.0)]
    v = _check("ETH", positions, bankroll=0.0)
    # Just must not raise


# ── summary() ────────────────────────────────────────────────────────────────

def test_summary_empty_portfolio():
    pg = PortfolioGuard()
    s = pg.summary([], bankroll=200.0)
    assert s["open_bets"] == 0
    assert s["btc_cluster"] == 0


def test_summary_counts_assets():
    positions = [_pos("BTC"), _pos("BTC"), _pos("ETH"), _pos("SOL")]
    pg = PortfolioGuard()
    s = pg.summary(positions, bankroll=200.0)
    assert s["open_bets"] == 4
    assert s["asset_breakdown"]["BTC"] == 2
    assert s["btc_cluster"] == 3   # BTC(2) + ETH(1)


def test_summary_excludes_settled():
    positions = [_pos("BTC", status="settled"), _pos("ETH", status="open")]
    pg = PortfolioGuard()
    s = pg.summary(positions, bankroll=200.0)
    assert s["open_bets"] == 1


def test_summary_exposure_pct():
    positions = [_pos("BTC", cost=20.0), _pos("ETH", cost=20.0)]
    pg = PortfolioGuard()
    s = pg.summary(positions, bankroll=200.0)
    assert s["total_at_risk"] == pytest.approx(40.0)
    assert s["exposure_pct"] == pytest.approx(20.0)


def test_summary_contains_limits():
    pg = PortfolioGuard()
    s = pg.summary([], bankroll=200.0)
    assert "limits" in s
    limits = s["limits"]
    assert "max_total_open" in limits
    assert "max_same_asset" in limits
    assert "max_btc_cluster" in limits
