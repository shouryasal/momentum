"""The deterministic half: every number checked against arithmetic done by hand.

If these are wrong nothing downstream can be right, and no model can rescue it — which is
the whole reason the numbers are computed here rather than asked for.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from runs.watch import positions as pos
from tests.test_watch.conftest import (
    NOW,
    seed_book,
    seed_candles,
    seed_proposal,
    seed_trade,
    seed_wallet,
)


@pytest.fixture
def one(cfg, root, dbs):
    jdb, kdb = dbs
    seed_wallet(root, "a", 10_000.0)
    seed_trade(root, "a", pair="BTC/USDT", amount=0.02, open_rate=80_000.0,
               stop_loss=72_000.0, max_rate=88_000.0, min_rate=79_000.0, hours_ago=6.0)
    seed_book(kdb, "BTC/USDT", mid=84_000.0)
    seed_candles(kdb, "BTC/USDT", "1h", n=30, close=83_000.0)
    seed_proposal(jdb, targets={"BTC": 0.10, "USDT": 0.90})
    return pos.open_holdings(cfg, kdb=kdb, jdb=jdb, root=root, now=NOW)


def test_one_open_position_is_found(one):
    assert [h.pair for h in one] == ["BTC/USDT"]


def test_the_mark_is_the_freshest_of_book_and_candle(one):
    h = one[0]
    assert h.mark.source == "book_mid"
    assert h.mark.price == 84_000.0
    assert h.mark.age_min == pytest.approx(2.0, abs=0.01)


def test_pnl_and_value(one):
    h = one[0]
    assert h.value_usdt == pytest.approx(0.02 * 84_000.0)          # 1680
    assert h.pnl_pct == pytest.approx((84_000 - 80_000) / 80_000 * 100)   # +5%
    assert h.pnl_usdt == pytest.approx(0.02 * 4_000.0)             # 80


def test_time_held(one):
    assert one[0].age_hours == pytest.approx(6.0, abs=0.001)


def test_distance_to_the_stop(one):
    h = one[0]
    assert h.stop == 72_000.0
    assert h.dist_to_stop_pct == pytest.approx((84_000 - 72_000) / 84_000 * 100)
    assert h.through_stop is False
    assert h.stop_pct_from_entry == pytest.approx(-10.0)


def test_drawdown_from_the_high_since_entry(one):
    """Freqtrade's own max_rate is the high-water mark; nothing is re-derived."""
    assert one[0].drawdown_from_peak_pct == pytest.approx(
        (84_000 - 88_000) / 88_000 * 100)


def test_nav_weight_target_and_cap(cfg, one):
    h = one[0]
    # NAV = 10,000 start + 0 realised + (1,680 value - 1,600 cost) = 10,080
    assert h.nav_usdt == pytest.approx(10_080.0)
    assert h.nav_basis == "freqtrade-a.json:dry_run_wallet"
    assert h.weight_now == pytest.approx(1_680.0 / 10_080.0)
    assert h.target_weight == 0.10
    assert h.weight_gap == pytest.approx(h.weight_now - 0.10)
    assert h.weight_cap == cfg.risk.max_weight["BTC"]
    assert h.headroom_to_cap == pytest.approx(h.weight_cap - h.weight_now)


def test_the_gate_limits_travel_with_the_holding(cfg, one):
    h = one[0]
    assert h.gross_cap == cfg.risk.max_gross_exposure
    assert h.usdt_floor == cfg.risk.usdt_floor
    assert h.gross_exposure == pytest.approx(1_680.0 / 10_080.0)
    assert h.usdt_share == pytest.approx((10_080.0 - 1_680.0) / 10_080.0)


def test_a_position_through_its_stop_is_flagged(cfg, root, dbs):
    jdb, kdb = dbs
    seed_wallet(root, "a")
    seed_trade(root, "a", amount=0.02, open_rate=80_000.0, stop_loss=72_000.0)
    seed_book(kdb, "BTC/USDT", mid=71_500.0)
    h = pos.open_holdings(cfg, kdb=kdb, jdb=jdb, root=root, now=NOW)[0]
    assert h.through_stop is True
    assert h.dist_to_stop_pct < 0


def test_a_missing_number_is_none_and_named(cfg, root, dbs):
    """No book, no candle, no NAV basis: everything that depends on a price is None and
    the gaps say why. Nothing is defaulted to something convenient."""
    jdb, kdb = dbs
    seed_trade(root, "a", amount=0.02, open_rate=80_000.0)
    h = pos.open_holdings(cfg, kdb=kdb, jdb=jdb, root=root, now=NOW)[0]
    assert h.mark.price is None
    assert h.pnl_pct is None and h.weight_now is None and h.nav_usdt is None
    assert any("no mark" in g for g in h.gaps)
    assert any("NAV unresolved" in g for g in h.gaps)


def test_closed_trades_are_not_held(cfg, root, dbs):
    jdb, kdb = dbs
    seed_wallet(root, "a")
    seed_trade(root, "a", trade_id=1, is_open=0, close_profit_abs=-13.5)
    assert pos.open_holdings(cfg, kdb=kdb, jdb=jdb, root=root, now=NOW) == []


def test_realised_profit_from_closed_trades_moves_nav(cfg, root, dbs):
    jdb, kdb = dbs
    seed_wallet(root, "a", 10_000.0)
    seed_trade(root, "a", trade_id=1, is_open=0, close_profit_abs=-500.0)
    seed_trade(root, "a", trade_id=2, amount=0.02, open_rate=80_000.0)
    seed_book(kdb, "BTC/USDT", mid=80_000.0)
    h = pos.open_holdings(cfg, kdb=kdb, jdb=jdb, root=root, now=NOW)[0]
    assert h.nav_usdt == pytest.approx(9_500.0)


def test_both_sleeves_are_watched(cfg, root, dbs):
    jdb, kdb = dbs
    seed_wallet(root, "a")
    seed_wallet(root, "b")
    seed_trade(root, "a", pair="BTC/USDT", amount=0.01, open_rate=80_000.0)
    seed_trade(root, "b", pair="ETH/USDT", amount=1.0, open_rate=2_700.0)
    seed_book(kdb, "BTC/USDT", mid=80_000.0)
    seed_book(kdb, "ETH/USDT", mid=2_700.0)
    held = pos.open_holdings(cfg, kdb=kdb, jdb=jdb, root=root, now=NOW)
    assert {(h.sleeve, h.pair) for h in held} == {("a", "BTC/USDT"), ("b", "ETH/USDT")}


def test_a_sleeve_with_no_ledger_is_not_an_error(cfg, root, dbs):
    jdb, kdb = dbs
    assert pos.open_holdings(cfg, kdb=kdb, jdb=jdb, root=root, now=NOW) == []


def test_facts_feed_the_invalidation_check(one):
    facts = one[0].facts()
    assert facts["price"] == 84_000.0
    assert facts["through_stop"] is False
    assert facts["pnl_pct"] == pytest.approx(5.0)
    assert "drawdown_pct" in facts


def test_the_record_is_json_safe(one):
    import json

    json.dumps(one[0].as_dict())


def test_a_stale_mark_reports_its_age(cfg, root, dbs):
    jdb, kdb = dbs
    seed_wallet(root, "a")
    seed_trade(root, "a", amount=0.02, open_rate=80_000.0)
    seed_book(kdb, "BTC/USDT", mid=84_000.0, minutes_ago=200.0)
    later = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
    h = pos.open_holdings(cfg, kdb=kdb, jdb=jdb, root=root, now=later)[0]
    assert h.mark.age_min == pytest.approx(200.0, abs=0.01)


def test_a_candle_still_in_progress_is_not_a_mark(cfg, root, dbs):
    """A bar in progress has a close_time in the FUTURE. Taking that as its timestamp made
    a 25-minute-old price look zero minutes old and beat a genuinely fresh book snapshot."""
    jdb, kdb = dbs
    seed_wallet(root, "a")
    seed_trade(root, "a", amount=0.02, open_rate=80_000.0)
    seed_book(kdb, "BTC/USDT", mid=84_000.0, minutes_ago=3.0)
    seed_candles(kdb, "BTC/USDT", "1h", n=1, close=70_000.0, bars_back=0)  # the only bar
    # is still open: it opened at `now` and will not close for another hour.
    h = pos.open_holdings(cfg, kdb=kdb, jdb=jdb, root=root, now=NOW)[0]
    assert h.mark.source == "book_mid"
    assert h.mark.price == 84_000.0
    assert h.mark.age_min == pytest.approx(3.0, abs=0.01)


def test_a_closed_candle_is_used_when_it_is_the_freshest_thing(cfg, root, dbs):
    jdb, kdb = dbs
    seed_wallet(root, "a")
    seed_trade(root, "a", amount=0.02, open_rate=80_000.0)
    seed_book(kdb, "BTC/USDT", mid=84_000.0, minutes_ago=180.0)
    seed_candles(kdb, "BTC/USDT", "1h", n=3, close=83_000.0, bars_back=1)
    h = pos.open_holdings(cfg, kdb=kdb, jdb=jdb, root=root, now=NOW)[0]
    assert h.mark.source == "candle_1h_close"
    assert h.mark.price == 83_000.0
    assert 0 < h.mark.age_min < 61
