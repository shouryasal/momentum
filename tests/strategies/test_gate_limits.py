"""One test per spec-§9 sizing/counting control (weight cap, gross cap, USDT floor,
min notional, trades/day). Values are read from the committed riskgate.json so the
tests break if the config drifts from the spec."""

from datetime import timedelta

from strategies.riskgate import MemoryStateStore

from .conftest import ENTRY, NOW, benign_gate, ps


class TestWeightCap:
    def test_btc_40pct_cap_rejects_past_cap(self, gate):
        # BTC at 35% of 10k NAV; +600 pushes past 40%
        d = gate.check_entry("BTC/USDT", 600.0, ps(btc=3500))
        assert not d.allowed and d.reason == "weight_cap:BTC/USDT"

    def test_eth_uses_default_30pct(self, gate):
        d = gate.check_entry("ETH/USDT", 600.0, ps(eth=2500))
        assert not d.allowed and d.reason == "weight_cap:ETH/USDT"

    def test_below_cap_passes(self, gate):
        assert gate.check_entry("BTC/USDT", 400.0, ps(btc=3500)).allowed

    def test_cap_stake_shrinks_to_exact_headroom(self, gate):
        # headroom = 40%*10000 - 3500 = 500
        assert gate.cap_stake("BTC/USDT", 2000.0, ps(btc=3500)) == 500.0


class TestGrossExposureAndFloor:
    # With the 2-asset universe the per-asset caps sum to 70% < the 80% gross cap, so
    # gross/floor can only bind after positions have DRIFTED above their caps (price
    # appreciation) or when free USDT is tied up in open orders. The tests model that.

    def test_gross_80pct_rejected_after_drift(self, gate):
        # ETH drifted to 76% of NAV; BTC entry passes its own cap but breaks gross
        d = gate.check_entry("BTC/USDT", ENTRY, ps(btc=500, eth=7600))
        assert not d.allowed and d.reason == "gross_cap:BTC/USDT"

    def test_usdt_floor_20pct_rejected(self, gate):
        # free is low despite small positions (funds tied in open orders):
        # 2100 - 200 = 1900 < 20% of NAV; weight (12%) and gross (22%) both fine
        d = gate.check_entry("BTC/USDT", 200.0, ps(free=2100, btc=1000, eth=1000))
        assert not d.allowed and d.reason == "usdt_floor"

    def test_weight_cap_binds_before_gross_normally(self, gate):
        # 1900 is inside max_order_notional_pct (20% of 10k) but past BTC's 40% cap
        d = gate.check_entry("BTC/USDT", 1900.0, ps(btc=2500, eth=1000))
        assert not d.allowed and d.reason == "weight_cap:BTC/USDT"
        d = gate.check_entry("ETH/USDT", 1900.0, ps(btc=1000, eth=1000))
        assert d.allowed  # 29% ETH, gross 49%, free left 6100 > 2000

    def test_cap_stake_respects_floor(self, gate):
        # free 8000, floor 2000 -> floor headroom 6000; weight headroom 3000;
        # order_notional headroom 20% of 10k = 2000 is now the tightest
        assert gate.cap_stake("BTC/USDT", 9000.0, ps(btc=1000, eth=1000)) == 2000.0

    def test_cap_stake_floor_binds_when_order_notional_is_loose(self, gate_cfg, tmp_path):
        from .conftest import gate_cfg_with

        def loosen(raw):
            raw["risk"]["max_order_notional_pct"] = 1.0

        gate = benign_gate(gate_cfg_with(tmp_path, loosen))
        # now the USDT floor is the binding constraint: free 2500 - floor 2000 = 500
        assert gate.cap_stake("BTC/USDT", 9000.0, ps(free=2500, btc=1000, eth=1000)) == 500.0


class TestMinNotional:
    def test_below_min_rejected(self, gate):
        d = gate.check_entry("BTC/USDT", 24.0, ps())
        assert not d.allowed and d.reason == "min_notional"

    def test_cap_stake_returns_zero_below_min(self, gate):
        # headroom shrinks the stake under 25 -> 0 (trade skipped)
        assert gate.cap_stake("BTC/USDT", 30.0, ps(btc=3990)) == 0.0


class TestTradesPerDay:
    def test_fifth_entry_rejected_and_gulf_reset(self, gate_cfg):
        store = MemoryStateStore()
        gate = benign_gate(gate_cfg, store)
        for _ in range(4):
            assert gate.check_entry("BTC/USDT", ENTRY, ps()).allowed
            gate.record_entry_fill(NOW)
        d = gate.check_entry("BTC/USDT", ENTRY, ps())
        assert not d.allowed and d.reason == "trades_per_day"
        # next Gulf day (Gulf midnight = 20:00 UTC): counter resets
        tomorrow = NOW + timedelta(hours=13)
        assert gate.check_entry("BTC/USDT", ENTRY, ps(now=tomorrow)).allowed

    def test_counter_survives_restart_via_store(self, gate_cfg):
        store = MemoryStateStore()
        gate1 = benign_gate(gate_cfg, store)
        for _ in range(4):
            gate1.record_entry_fill(NOW)
        gate2 = benign_gate(gate_cfg, store)  # "restarted" bot, same store
        assert not gate2.check_entry("BTC/USDT", ENTRY, ps()).allowed
