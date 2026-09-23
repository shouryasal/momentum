"""Sleeve A over a wide book: core + rotated satellites, and the whitelist identity.

Two things are proven here that nothing else can prove:

1. **Sleeve A ranks and rotates rather than holding two fixed pairs**, and it does so from
   the point-in-time snapshot's scores with the §3.2 hysteresis band — so the selection
   the live bot makes is the selection a backtest replaying that snapshot would make.
2. **The backtest whitelist equals the snapshot whitelist** (design §5.3). Every dynamic
   pairlist in the installed freqtrade 2026.8 is ``SupportsBacktesting.NO``; if the bots
   chose their own universe, no backtest could reproduce the universe they traded and the
   whole change-control protocol would go blind exactly where the new risk lives.
"""

from __future__ import annotations

import json

import pytest

from ops.config import REPO_ROOT, load_config
from ops.gen_freqtrade_config import (
    build_bot_config,
    build_riskgate_json,
    latest_snapshot,
    snapshot_whitelist,
)

pytest.importorskip("freqtrade")

from .test_sleeves import _make  # noqa: E402

QUOTE = "USDT"

#: The gate-facing slice of a snapshot — what ``gate_universe_block`` extracts and what
#: ``riskgate.json`` carries. Keyed by base asset.
SNAPSHOT = {
    "date": "2026-09-21",
    "sha256": "a" * 64,
    "tiers": {"BTC": "core", "ETH": "core", "SOL": "major",
              "TIA": "satellite", "INJ": "satellite", "SEI": "satellite",
              "JUP": "satellite", "ONDO": "satellite", "DUST": "watchlist"},
    "caps": {},
    "scores": {"TIA": 0.91, "INJ": 0.84, "SEI": 0.77, "JUP": 0.70, "ONDO": 0.30},
    "exit_only": [],
    "filters": {},
}

#: A RESOLVER snapshot in the real shape ``ops/universe.py`` writes (U1): a ``pairs`` map
#: keyed by pair, each entry carrying its own tier, cap, score, filters and metrics.
RESOLVED = {
    "date": "2026-09-21",
    "sha256": "b" * 64,
    "quote": "USDT",
    "rules": {"core": ["BTC", "ETH"]},
    "pairs": {
        "BTC/USDT": {"base": "BTC", "tier": "core", "cap": 0.40, "rank": None,
                     "score": None, "min_notional": 5.0, "step_size": 1e-5,
                     "exit_only": False, "delisting_at": None,
                     "metrics": {"price": 86_208.56}},
        "ETH/USDT": {"base": "ETH", "tier": "core", "cap": 0.30, "rank": None,
                     "score": None, "min_notional": 5.0, "step_size": 1e-4,
                     "exit_only": False, "delisting_at": None,
                     "metrics": {"price": 2_800.0}},
        "SOL/USDT": {"base": "SOL", "tier": "major", "cap": 0.15, "rank": 1,
                     "score": 0.95, "min_notional": 5.0, "step_size": 0.001,
                     "exit_only": False, "delisting_at": None,
                     "metrics": {"price": 140.0}},
        "TIA/USDT": {"base": "TIA", "tier": "satellite", "cap": 0.05, "rank": 2,
                     "score": 0.91, "min_notional": 5.0, "step_size": 0.01,
                     "exit_only": False, "delisting_at": None,
                     "metrics": {"price": 3.0}},
        "OLD/USDT": {"base": "OLD", "tier": "satellite", "cap": 0.05, "rank": 3,
                     "score": 0.60, "min_notional": 5.0, "step_size": 0.1,
                     "exit_only": False, "delisting_at": "2026-10-01T00:00:00Z",
                     "metrics": {"price": 12.0}},
        "DUST/USDT": {"base": "DUST", "tier": "watchlist", "cap": 0.0, "rank": 40,
                      "score": 0.10, "min_notional": 5.0, "step_size": 1.0,
                      "exit_only": False, "delisting_at": None,
                      "metrics": {"price": 0.5}},
    },
}


def _state(pair, reg, vol):
    """What ``SleeveA._pair_state`` returns: (regime up, annualised vol, candle stamp)."""
    asset = pair.split("/")[0]
    return (bool(reg.get(asset, False)), float(vol.get(asset, 0.60)), "2026-09-21")


def _wide_sleeve_a(monkeypatch, tmp_path, *, regimes=None, vols=None, snapshot=None):
    """Sleeve A wired to a snapshot, with the per-pair 1d indicators stubbed.

    The indicators are stubbed rather than synthesised because what is under test is the
    cross-sectional selection and the book-level sizing, not the 200d MA — that is
    tests/strategies/test_sleeve_common.py's job, and stubbing keeps this file from
    passing for the wrong reason.
    """
    snap = SNAPSHOT if snapshot is None else snapshot

    def mutate(raw):
        raw["universe"]["snapshot"] = snap
        raw["universe"]["pairs"] = [f"{a}/{QUOTE}" for a in sorted(snap["tiers"])]

    s = _make(monkeypatch, tmp_path, "a", mutate=mutate)
    reg = dict(regimes or {a: True for a in snap["tiers"]})
    vol = dict(vols or {})
    monkeypatch.setattr(type(s), "_pair_state", lambda self, p: _state(p, reg, vol))
    s._params = {"base_weights": {"BTC": 0.60, "ETH": 0.40},
                 "vol": {"target_annual": 0.60}}
    return s


class TestSleeveARotates:
    def test_it_holds_the_top_four_satellites_alongside_the_core(self, monkeypatch, tmp_path):
        s = _wide_sleeve_a(monkeypatch, tmp_path)
        t = s._book_targets()
        assert t["BTC/USDT"] > 0 and t["ETH/USDT"] > 0
        held = {p for p, w in t.items() if w > 0 and p.split("/")[0] not in ("BTC", "ETH")}
        assert held == {"TIA/USDT", "INJ/USDT", "SEI/USDT", "JUP/USDT"}
        assert "ONDO/USDT" not in held        # 5th by score, and there are 4 seats
        assert "SOL/USDT" not in held         # a major is not a satellite
        assert "DUST/USDT" not in held        # watchlist only: never tradeable

    def test_the_satellite_sleeve_stays_inside_its_gross(self, monkeypatch, tmp_path):
        s = _wide_sleeve_a(monkeypatch, tmp_path)
        t = s._book_targets()
        sats = sum(w for p, w in t.items()
                   if s.gate_cfg.is_satellite(p))
        assert sats <= s.gate_cfg.max_satellite_gross + 1e-9

    def test_no_satellite_is_above_its_tier_cap(self, monkeypatch, tmp_path):
        s = _wide_sleeve_a(monkeypatch, tmp_path)
        for pair, w in s._book_targets().items():
            assert w <= s.gate_cfg.cap_for(pair) + 1e-9, pair

    def test_btc_below_its_200d_ma_takes_the_whole_satellite_sleeve_to_cash(
            self, monkeypatch, tmp_path):
        regimes = {a: True for a in SNAPSHOT["tiers"]}
        regimes["BTC"] = False
        s = _wide_sleeve_a(monkeypatch, tmp_path, regimes=regimes)
        t = s._book_targets()
        assert t["BTC/USDT"] == 0.0
        assert all(w == 0.0 for p, w in t.items() if s.gate_cfg.is_satellite(p))

    def test_a_satellite_below_its_own_ma_is_not_held(self, monkeypatch, tmp_path):
        regimes = {a: True for a in SNAPSHOT["tiers"]}
        regimes["TIA"] = False
        s = _wide_sleeve_a(monkeypatch, tmp_path, regimes=regimes)
        t = s._book_targets()
        assert t.get("TIA/USDT", 0.0) == 0.0
        # The seat is NOT backfilled inside the week. Selection is cross-sectional and
        # rotates weekly with hysteresis; letting a regime flip pull the next name in
        # would make the book churn on price rather than on rank. The sleeve's gross is
        # preserved across the names that are still eligible instead.
        assert t.get("ONDO/USDT", 0.0) == 0.0
        held = [p for p in ("INJ/USDT", "SEI/USDT", "JUP/USDT") if t.get(p, 0.0) > 0]
        assert len(held) == 3
        assert sum(t[p] for p in held) == pytest.approx(s.gate_cfg.max_satellite_gross)

    def test_incumbency_is_persisted_so_a_restart_does_not_churn_the_book(
            self, monkeypatch, tmp_path):
        s = _wide_sleeve_a(monkeypatch, tmp_path)
        s._book_targets()
        assert s.gate.store.get("satellites") == "TIA,INJ,SEI,JUP"

    def test_a_marginal_rank_change_does_not_rotate_an_incumbent(self, monkeypatch, tmp_path):
        # ONDO overtakes JUP by one rank. One rank of improvement is noise; the measured
        # weekly churn of the top 50 is 6%, and re-trading that buys no information.
        moved = dict(SNAPSHOT, scores=dict(SNAPSHOT["scores"], ONDO=0.71, JUP=0.70))
        s = _wide_sleeve_a(monkeypatch, tmp_path, snapshot=moved)
        s.gate.store.set("satellites", "TIA,INJ,SEI,JUP")
        held = {p for p, w in s._book_targets().items() if w > 0}
        assert "JUP/USDT" in held and "ONDO/USDT" not in held

    def test_a_decisive_rank_change_does_rotate(self, monkeypatch, tmp_path):
        # The incumbent has slipped to last; the challenger is three ranks better.
        moved = dict(SNAPSHOT, scores={"TIA": 0.91, "INJ": 0.84, "SEI": 0.77,
                                       "ONDO": 0.70, "NEW1": 0.65, "NEW2": 0.60,
                                       "JUP": 0.10})
        moved = dict(moved, tiers=dict(SNAPSHOT["tiers"], NEW1="satellite",
                                       NEW2="satellite"))
        s = _wide_sleeve_a(monkeypatch, tmp_path, snapshot=moved)
        s.gate.store.set("satellites", "TIA,INJ,SEI,JUP")
        held = {p for p, w in s._book_targets().items() if w > 0}
        assert "ONDO/USDT" in held and "JUP/USDT" not in held

    def test_without_a_snapshot_it_is_still_the_two_asset_sleeve(self, monkeypatch, tmp_path):
        s = _make(monkeypatch, tmp_path, "a")
        monkeypatch.setattr(type(s), "_pair_state",
                            lambda self, p: _state(p, {"BTC": True, "ETH": True}, {}))
        s._params = {"base_weights": {"BTC": 0.60, "ETH": 0.40},
                     "vol": {"target_annual": 0.60}}
        t = s._book_targets()
        assert set(t) == {"BTC/USDT", "ETH/USDT"}
        assert t["BTC/USDT"] == pytest.approx(0.40)   # its cap
        assert t["ETH/USDT"] == pytest.approx(0.30)   # its cap

    def test_the_book_is_computed_once_per_candle_not_once_per_pair(
            self, monkeypatch, tmp_path):
        s = _wide_sleeve_a(monkeypatch, tmp_path)
        calls = {"n": 0}
        inner = type(s)._pair_state

        def counted(self, pair):
            calls["n"] += 1
            return inner(self, pair)

        monkeypatch.setattr(type(s), "_pair_state", counted)
        first = s._book_targets()
        after_one = calls["n"]
        for pair in s.gate_cfg.pairs:
            s._target_weight(pair)
        # Each call still probes the pairs to read the candle stamp, but the cross-sectional
        # selection and the book vol targeting are done once.
        assert s._book_targets() is first
        assert calls["n"] > after_one   # stamps are cheap; the selection is not repeated


# ------------------------------------------------- whitelist / snapshot identity


class TestWhitelistEqualsTheSnapshot:
    def test_the_bot_whitelist_is_exactly_the_snapshots_tradeable_tier(self):
        cfg = load_config()
        conf = build_bot_config(cfg, "a", snapshot=RESOLVED)
        assert conf["exchange"]["pair_whitelist"] == snapshot_whitelist(RESOLVED, cfg)
        assert set(conf["exchange"]["pair_whitelist"]) == {
            "BTC/USDT", "ETH/USDT", "SOL/USDT", "TIA/USDT", "OLD/USDT"}
        # Watchlist-only: the scanner and the brief can see it; the bot cannot trade it.
        assert "DUST/USDT" not in conf["exchange"]["pair_whitelist"]

    def test_the_gates_universe_is_the_same_artefact(self):
        cfg = load_config()
        conf = build_bot_config(cfg, "a", snapshot=RESOLVED)
        rg = build_riskgate_json(cfg, snapshot=RESOLVED)
        assert rg["universe"]["pairs"] == conf["exchange"]["pair_whitelist"]
        assert rg["universe"]["snapshot"]["sha256"] == RESOLVED["sha256"]
        assert rg["universe"]["snapshot"]["tiers"]["TIA"] == "satellite"
        assert rg["universe"]["snapshot"]["caps"]["SOL"] == 0.15

    def test_a_delisting_notice_makes_the_gate_treat_the_name_as_exit_only(self):
        block = build_riskgate_json(load_config(), snapshot=RESOLVED)["universe"]["snapshot"]
        assert block["exit_only"] == ["OLD"]

    def test_the_gate_gets_one_lot_step_priced_in_usdt(self):
        block = build_riskgate_json(load_config(), snapshot=RESOLVED)["universe"]["snapshot"]
        assert block["filters"]["TIA"]["step_notional"] == pytest.approx(0.01 * 3.0)
        assert block["filters"]["TIA"]["min_notional"] == 5.0

    def test_freqtrade_always_runs_a_static_pairlist(self):
        conf = build_bot_config(load_config(), "a", snapshot=RESOLVED)
        assert conf["pairlists"] == [{"method": "StaticPairList"}]

    def test_max_open_trades_is_the_risk_limit_not_the_whitelist_length(self):
        cfg = load_config()
        conf = build_bot_config(cfg, "a", snapshot=RESOLVED)
        assert conf["max_open_trades"] == cfg.risk.max_open_positions == 8
        # len(pairs) was the old value and would be 5 here, and 107 on the live snapshot.
        assert conf["max_open_trades"] != len(conf["exchange"]["pair_whitelist"])

    def test_a_name_being_wound_down_stays_on_the_whitelist(self):
        # Dropping a pair freqtrade holds a position in is not an exit, it is an orphan.
        assert "OLD/USDT" in snapshot_whitelist(RESOLVED, load_config())

    def test_no_snapshot_falls_back_to_the_configured_pairs(self):
        cfg = load_config()
        assert snapshot_whitelist(None, cfg) == list(cfg.universe.pairs)

    def test_an_unreadable_snapshot_is_the_same_as_none(self, tmp_path):
        (tmp_path / "2026-09-21.json").write_text("{not json")
        assert latest_snapshot(tmp_path) is None

    def test_the_newest_readable_snapshot_wins(self, tmp_path):
        older = dict(RESOLVED, date="2026-09-14", sha256="c" * 64)
        (tmp_path / "2026-09-14.json").write_text(json.dumps(older))
        (tmp_path / "2026-09-21.json").write_text(json.dumps(RESOLVED))
        assert latest_snapshot(tmp_path)["sha256"] == RESOLVED["sha256"]

    def test_the_committed_riskgate_json_matches_the_generator(self):
        # Drift here means the bots are enforcing a different universe from the one
        # `python -m ops.gen_freqtrade_config` would render today.
        committed = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
        assert committed["universe"] == build_riskgate_json(load_config())["universe"]
        assert committed["risk"]["max_open_positions"] == 8
        assert "default" not in committed["risk"]["max_weight"]

    def test_the_committed_whitelist_is_the_live_snapshots_tradeable_tier(self):
        cfg = load_config()
        snap = latest_snapshot()
        if snap is None:
            pytest.skip("no universe snapshot committed yet (package U1)")
        committed = json.loads((REPO_ROOT / "config" / "freqtrade-a.json").read_text())
        assert committed["exchange"]["pair_whitelist"] == snapshot_whitelist(snap, cfg)
