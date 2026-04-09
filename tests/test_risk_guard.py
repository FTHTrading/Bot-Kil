"""
tests/test_risk_guard.py — Unit tests for engine/risk_guard.py
"""
import pytest
from engine.risk_guard import (
    RiskGuard,
    RiskVerdict,
    _MAX_BET_DOLLARS,
    _MIN_BET_DOLLARS,
    _MAX_BET_PCT_BANKROLL,
    _MAX_SESSION_LOSS_PCT,
    _MAX_DAILY_BETS,
    _MAX_CONSECUTIVE_LOSSES,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _guard(bankroll=200.0, **kwargs) -> RiskGuard:
    return RiskGuard(starting_bankroll=bankroll, **kwargs)


# ── Happy path ────────────────────────────────────────────────────────────────

def test_happy_path_allowed():
    g = _guard()
    v = g.check(proposed_stake=5.0, current_bankroll=200.0)
    assert v.allowed
    assert v.reason is None
    assert v.clamped_stake == pytest.approx(5.0)


def test_verdict_to_dict():
    v = _guard().check(5.0, 200.0)
    d = v.to_dict()
    assert "allowed" in d and "clamped_stake" in d


# ── Dollar cap ────────────────────────────────────────────────────────────────

def test_stake_clamped_to_max_dollars():
    g = _guard()
    v = g.check(proposed_stake=1000.0, current_bankroll=200.0)
    assert v.allowed
    assert v.clamped_stake <= _MAX_BET_DOLLARS


def test_stake_clamped_to_bankroll_pct():
    """If bankroll is tiny, the % cap kicks in before the dollar cap."""
    bankroll = 50.0
    pct_cap = bankroll * _MAX_BET_PCT_BANKROLL  # 4.0
    g = _guard(bankroll=bankroll)
    v = g.check(proposed_stake=20.0, current_bankroll=bankroll)
    assert v.allowed
    assert v.clamped_stake <= pct_cap + 0.01


def test_stake_below_minimum_blocked():
    g = _guard()
    v = g.check(proposed_stake=0.50, current_bankroll=200.0)
    assert not v.allowed


def test_exact_minimum_stake_allowed():
    g = _guard()
    v = g.check(proposed_stake=_MIN_BET_DOLLARS, current_bankroll=200.0)
    assert v.allowed


# ── Session bet limit ─────────────────────────────────────────────────────────

def test_session_bet_count_blocks_at_limit():
    g = _guard(max_daily_bets=3)
    for _ in range(3):
        g.record_bet_placed()
    v = g.check(5.0, 200.0)
    assert not v.allowed
    assert "limit" in v.reason.lower()


def test_session_bet_count_allows_below_limit():
    g = _guard(max_daily_bets=5)
    for _ in range(4):
        g.record_bet_placed()
    v = g.check(5.0, 200.0)
    assert v.allowed


def test_reset_session_clears_bet_count():
    g = _guard(max_daily_bets=2)
    for _ in range(2):
        g.record_bet_placed()
    g.reset_session()
    v = g.check(5.0, 200.0)
    assert v.allowed


# ── Consecutive loss circuit breaker ─────────────────────────────────────────

def test_consecutive_losses_block_after_limit():
    g = _guard(max_consec_losses=4)
    for _ in range(4):
        g.record_outcome(won=False)
    v = g.check(5.0, 200.0)
    assert not v.allowed
    assert "consecutive" in v.reason.lower()


def test_win_resets_consecutive_loss_counter():
    g = _guard(max_consec_losses=4)
    for _ in range(3):
        g.record_outcome(won=False)
    g.record_outcome(won=True)   # win resets the counter
    v = g.check(5.0, 200.0)
    assert v.allowed


def test_reset_consecutive_losses_re_enables():
    g = _guard(max_consec_losses=4)
    for _ in range(4):
        g.record_outcome(won=False)
    g.reset_consecutive_losses()
    v = g.check(5.0, 200.0)
    assert v.allowed


# ── Session drawdown limit ────────────────────────────────────────────────────

def test_drawdown_blocks_at_limit():
    g = _guard(bankroll=200.0, max_session_loss_pct=0.15)  # limit = $30
    v = g.check(5.0, current_bankroll=200.0, session_pnl=-30.01)
    assert not v.allowed
    assert "loss" in v.reason.lower()


def test_drawdown_allows_below_limit():
    g = _guard(bankroll=200.0, max_session_loss_pct=0.15)
    v = g.check(5.0, current_bankroll=200.0, session_pnl=-29.0)
    assert v.allowed


def test_zero_pnl_never_drawdown_blocked():
    g = _guard()
    v = g.check(5.0, 200.0, session_pnl=0.0)
    assert v.allowed


# ── status() ─────────────────────────────────────────────────────────────────

def test_status_returns_dict():
    g = _guard()
    s = g.status()
    assert "bets_this_session" in s
    assert "consec_losses" in s


def test_status_reflects_recorded_bets():
    g = _guard()
    g.record_bet_placed()
    g.record_bet_placed()
    assert g.status()["bets_this_session"] == 2


def test_status_reflects_consecutive_losses():
    g = _guard()
    g.record_outcome(won=False)
    g.record_outcome(won=False)
    assert g.status()["consec_losses"] == 2


# ── Gate ordering: session limit checked before consecutive losses ─────────────

def test_session_limit_checked_first():
    g = _guard(max_daily_bets=2, max_consec_losses=2)
    for _ in range(2):
        g.record_bet_placed()
    for _ in range(2):
        g.record_outcome(won=False)
    v = g.check(5.0, 200.0)
    # Both limits are breached; session count fires first
    assert not v.allowed
    assert "limit" in v.reason.lower()
