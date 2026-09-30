"""A fee counter must not be able to veto a full de-risk. Found 2026-09-30.

`confirm_trade_exit` routes **every** exit through `check_discretionary_exit`, and the only
exemption used to be `is_risk_exit(exit_reason)` — a name match against `RISK_EXIT_REASONS`
plus the prefixes `risk_stop`/`stop_loss`/`trailing_stop`/`emergency`.

`exit_signal` is in neither. It is the literal value freqtrade passes for a
`populate_exit_trend` sell (checked against `freqtrade.enums.ExitType.EXIT_SIGNAL`), and it is
SleeveA's **only** signal-driven exit: the 200-day MA regime flip, a 100% close. So the one sell
this sleeve makes on its own opinion was refusable by `orders_per_day`, `turnover_day` or
`fee_budget`.

Three things made the incentive backwards at once:

* `abs(stake)` is in the turnover numerator, so the **bigger** the de-risk the likelier the veto;
* `ACTION_PRIORITY` ranks `add` above `rebalance`, so risk-*increasing* orders spend the budget
  the de-risk later needs;
* `fee_budget` is keyed to the Gulf month, so once tripped it refused the close on **every
  candle until the month turned** — leaving only the per-position stop, in exactly the month
  expensive enough to exhaust the budget.

The fix exempts by SIZE and direction rather than by name, so the next new sell reason inherits
the protection instead of silently missing it. These tests pin both halves: a large reduction
always clears, and an ordinary trim is still budgeted (or the budget means nothing).
"""

from __future__ import annotations

from strategies.riskgate import MemoryStateStore

from .conftest import NOW, benign_gate, ps


def _spent_gate(gate_cfg):
    """A gate whose churn counters are past every limit, driven the way production drives them.

    Through ``record_order_fill`` rather than by writing store keys: the gate wraps its store in
    ``NamespacedStateStore`` (keys become ``run:<run_id>:...``), so a test that sets raw keys
    sets keys nothing reads and then proves nothing. Getting that wrong is how
    ``check_exit`` — allowed unconditionally, zero production callers — came to be asserted in
    two tests as though it were the guarantee.
    """
    gate = benign_gate(gate_cfg, MemoryStateStore())
    nav = 10_000.0
    for _ in range(gate.cfg.max_orders_per_day + 5):
        gate.record_order_fill(NOW, notional=nav * gate.cfg.max_turnover_pct_per_day,
                               fee_usdt=nav * gate.cfg.max_fee_pct_per_month)
    return gate


def test_the_budget_really_is_exhausted_in_this_fixture(gate_cfg):
    """The control. If the fixture does not actually refuse anything, nothing below means much."""
    gate = _spent_gate(gate_cfg)
    state = ps(nav=10_000, btc=3000)
    small = gate.check_discretionary_exit("BTC/USDT", 50.0, state, "tp1")
    assert not small.allowed
    assert small.reason in ("orders_per_day", "turnover_day", "fee_budget")


def test_a_full_core_close_is_never_refused_by_the_fee_budget(gate_cfg):
    """The MA200 flip. 30% of NAV out of BTC, on the sleeve's own signal, must go through."""
    gate = _spent_gate(gate_cfg)
    state = ps(nav=10_000, btc=3000)
    d = gate.check_discretionary_exit("BTC/USDT", 3000.0, state, "exit_signal")
    assert d.allowed, f"a full core close was refused on {d.reason}"
    assert d.checks.get("large_derisk") is True


def test_the_exemption_is_by_size_not_by_the_name_of_the_reason(gate_cfg):
    """The point of the fix: a reason nobody has thought of yet still gets the protection."""
    gate = _spent_gate(gate_cfg)
    state = ps(nav=10_000, btc=3000)
    for invented in ("exit_signal", "trend_flip", "a_reason_from_2027", "", "TRIM_BREACH"):
        d = gate.check_discretionary_exit("BTC/USDT", 2500.0, state, invented)
        assert d.allowed, f"{invented!r} was refused on {d.reason}"


def test_an_ordinary_band_trim_is_still_budgeted(gate_cfg):
    """The other half. If small trims bypass the budget too, the budget is decoration.

    A rebalance trim is bounded by `rebalance_band` (0.05 of NAV) and a take-profit rung is
    smaller still, so both sit well under the 0.10 exemption and stay refusable.
    """
    gate = _spent_gate(gate_cfg)
    state = ps(nav=10_000, btc=3000)
    for stake in (100.0, 400.0, 500.0):          # 1%, 4%, 5% of NAV
        d = gate.check_discretionary_exit("BTC/USDT", stake, state, "rebalance")
        assert not d.allowed, f"a {stake / 100:.0f}% trim bypassed the budget"


def test_the_threshold_is_read_from_config_not_hardcoded(gate_cfg):
    """It is a limit, so it comes from earn.yaml -- CLAUDE.md forbids restating one by hand."""
    gate = _spent_gate(gate_cfg)
    pct = gate.cfg.derisk_exempt_pct
    assert 0.05 <= pct <= 0.50, pct
    state = ps(nav=10_000, btc=3000)
    just_under = gate.check_discretionary_exit("BTC/USDT", pct * 10_000 * 0.99, state, "x")
    just_over = gate.check_discretionary_exit("BTC/USDT", pct * 10_000 * 1.01, state, "x")
    assert not just_under.allowed
    assert just_over.allowed


def test_a_risk_exit_is_still_exempt_at_any_size(gate_cfg):
    """The name-based exemption has to survive: a stop is exempt even when it is tiny."""
    gate = _spent_gate(gate_cfg)
    state = ps(nav=10_000, btc=3000)
    for reason in ("stop_loss", "trailing_stop_loss", "emergency_exit", "risk_stop_daily"):
        d = gate.check_discretionary_exit("BTC/USDT", 5.0, state, reason)
        assert d.allowed, f"{reason} was refused on {d.reason}"
        assert d.checks.get("risk_exit") is True


def test_a_negative_magnitude_is_treated_as_its_size(gate_cfg):
    """`abs(stake)` is what the turnover numerator uses, so the exemption must agree with it."""
    gate = _spent_gate(gate_cfg)
    state = ps(nav=10_000, btc=3000)
    assert gate.check_discretionary_exit("BTC/USDT", -3000.0, state, "exit_signal").allowed
