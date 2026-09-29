"""The profit & gap ledger (``runs/profit_gaps.py``).

The owner asked to "keep checking what is likely profit we are getting and what are gaps
making us miss profit". The review of 2026-09-29 measured the gaps once by hand; this
suite seeds a temp root with the same shapes — two run databases per bot (a loss in the
old one, a gain in the new one), gate rows that are 5-second retries of one refusal, a
model run that cost money and returned nothing, a market state with ``assets={}``, a
heartbeat log with a six-hour hole, a ledger tick that reset to the seed, a hand flatten
in the audit log — and asserts every ledger line, the two windows, the three sentences,
and that nothing in it ever raises on a fresh checkout.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from console.services import pot_service
from ops import db
from ops.config import REPO_ROOT, load_config
from runs import profit_gaps as pg

NOW = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ft(dt: datetime) -> str:
    """Freqtrade's naive UTC stamp."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.000000")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


FT_DDL = """
CREATE TABLE trades (
  id INTEGER PRIMARY KEY, pair TEXT, is_open INTEGER, open_date TEXT, close_date TEXT,
  open_rate REAL, close_rate REAL, amount REAL, stake_amount REAL,
  fee_open REAL, fee_close REAL, fee_open_cost REAL, fee_close_cost REAL,
  close_profit REAL, close_profit_abs REAL, exit_reason TEXT, strategy TEXT, enter_tag TEXT
);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY, ft_trade_id INTEGER, ft_order_side TEXT, order_id TEXT,
  status TEXT, side TEXT, price REAL, average REAL, amount REAL, filled REAL, cost REAL,
  order_filled_date TEXT, ft_order_tag TEXT
);
"""


class FtDb:
    """A hand-built Freqtrade database with the columns the reader uses."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(FT_DDL)

    def trade(self, trade_id: int, pair: str, *, open_rate: float, amount: float,
              opened: datetime, closed: datetime | None = None,
              close_rate: float | None = None, exit_reason: str | None = None,
              enter_tag: str = "fast_breakout", fee: float = 0.001) -> float:
        """Insert a trade with its orders. Returns Freqtrade's realised profit (exit net of
        its fee minus the entry gross of its fee), 0 for an open trade."""
        stake = open_rate * amount
        self.conn.execute(
            "INSERT INTO orders(ft_trade_id, ft_order_side, order_id, status, side, price,"
            " average, amount, filled, cost, order_filled_date, ft_order_tag)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "buy", f"buy_{pair}_{trade_id}", "closed", "buy", open_rate, open_rate,
             amount, amount, stake, _ft(opened), enter_tag))
        realised = None
        if closed is not None and close_rate is not None:
            cost = amount * close_rate
            self.conn.execute(
                "INSERT INTO orders(ft_trade_id, ft_order_side, order_id, status, side, price,"
                " average, amount, filled, cost, order_filled_date, ft_order_tag)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade_id, "sell", f"sell_{pair}_{trade_id}", "closed", "sell", close_rate,
                 close_rate, amount, amount, cost, _ft(closed), exit_reason))
            realised = cost * (1 - fee) - stake * (1 + fee)
        self.conn.execute(
            "INSERT INTO trades(id, pair, is_open, open_date, close_date, open_rate, close_rate,"
            " amount, stake_amount, fee_open, fee_close, fee_open_cost, fee_close_cost,"
            " close_profit, close_profit_abs, exit_reason, strategy, enter_tag)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, pair, 0 if closed else 1, _ft(opened), _ft(closed) if closed else None,
             open_rate, close_rate, amount, stake, fee, fee, stake * fee,
             None if realised is None else amount * close_rate * fee,
             None if realised is None else realised / stake, realised, exit_reason,
             "SleeveFast", enter_tag))
        self.conn.commit()
        return realised or 0.0

    def close(self) -> None:
        self.conn.close()


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated state root: databases, run databases, logs and state files."""
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def seeded(cfg, root: Path) -> dict:
    """The review's week, in miniature, with every number the ledger reads."""
    journal, knowledge = db.init_all(cfg, root=root)
    jdb, kdb = db.connect(journal), db.connect(knowledge)
    old_close = datetime(2026, 9, 23, 21, 58, 52, tzinfo=UTC)
    old_open = datetime(2026, 9, 23, 13, 37, 37, tzinfo=UTC)

    # --- run databases: a loss in the old one, a gain in the new one, one open trade.
    legacy_a = FtDb(root / "ft_userdata" / "a" / "tradesv3.sqlite")
    loss_a = legacy_a.trade(1, "BTC/USDT", open_rate=85761.87, amount=0.0173, opened=old_open,
                            closed=old_close, close_rate=84441.33, exit_reason="force_exit",
                            enter_tag="dca")
    legacy_a.close()
    legacy_b = FtDb(root / "ft_userdata" / "b" / "tradesv3.sqlite")
    loss_b = legacy_b.trade(1, "ETH/USDT", open_rate=2718.13, amount=1.306, opened=old_open,
                            closed=old_open + timedelta(minutes=15), close_rate=2712.8,
                            exit_reason="target_zero", enter_tag="proposal")
    legacy_b.close()
    new_a = FtDb(root / "ft_userdata" / "a" / "runs" / "test-a-000.sqlite")
    gain_a = new_a.trade(1, "AVAX/USDT", open_rate=20.0, amount=25.0, opened=NOW - timedelta(
        hours=5), closed=NOW - timedelta(hours=3), close_rate=21.0, exit_reason="roi")
    new_a.trade(2, "SOL/USDT", open_rate=200.0, amount=2.5, opened=NOW - timedelta(hours=1))
    new_a.close()
    new_b = FtDb(root / "ft_userdata" / "b" / "runs" / "test-b-000.sqlite")
    new_b.close()

    # --- gate: five 5-second retries of ONE refusal, one other refusal, one allowed entry.
    t0 = NOW - timedelta(hours=2)
    for i in range(5):
        jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, side, intent, callback,"
                    " allowed, reason, severity) VALUES (?,?,?,?,?,?,?,?,?)",
                    (_iso(t0 + timedelta(seconds=5 * i)), "a", "PEPE/USDT", "buy", "entry",
                     "confirm_trade_entry", 0, "beta_cap", "reject"))
    jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, side, intent, callback,"
                " allowed, reason, severity) VALUES (?,?,?,?,?,?,?,?,?)",
                (_iso(t0), "b", "XRP/USDT", "buy", "entry", "confirm_trade_entry", 0,
                 "staleness", "reject"))
    jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, side, intent, callback,"
                " allowed, reason, severity) VALUES (?,?,?,?,?,?,?,?,?)",
                (_iso(NOW - timedelta(hours=5)), "a", "AVAX/USDT", "buy", "entry",
                 "confirm_trade_entry", 1, "ok", "allow"))

    # --- model spend: a dead validate run, two useful calls, one uncertain-with-error.
    for ts, task, ref, status, cost in (
            (t0, "validate", "sig-1", "error", 0.5),
            (t0, "classify", "cls-1", "ok", 0.02),
            (t0, "scan", "scan-1", "ok", 0.1)):
        jdb.execute("INSERT INTO llm_calls(ts_utc, task, run_ref, stage, provider, model,"
                    " attempt, status, cost_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                    (_iso(ts), task, ref, task, "claude:subscription", "m", 1, status, cost))
    jdb.execute("INSERT INTO runs(run_id, stage, kind, started_utc, status, cost_usd)"
                " VALUES (?,?,?,?,?,?)", ("daily-x", "daily_review", "review", _iso(t0),
                                          "failed", 3.0))

    # --- the funnel: four signals, one validation that errored, two abstaining proposals.
    ts = NOW - timedelta(hours=3)
    for sid, status, reason, prop in (
            ("sig-0", "screened_out", "screener:keep=false", None),
            ("sig-1", "expired", "expire_after_min", None),
            ("sig-2", "error", "unknown feature_key: ['market_state.data_fresh']", None),
            ("sig-3", "acted", None, "r1")):
        jdb.execute("INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector, pair,"
                    " strength, features_json, dedupe_key, status, status_reason,"
                    " proposal_run_id, updated_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, _iso(ts), "scan-1", "detector", "move", "HBAR/USDT", 1.0, "{}",
                     sid, status, reason, prop, _iso(ts)))
    jdb.execute("INSERT INTO signal_validations(signal_id, ts_utc, provider, model, verdict,"
                " confidence, error, cost_usd) VALUES (?,?,?,?,?,?,?,?)",
                ("sig-2", _iso(t0), "claude", "m", "uncertain", 0.0,
                 "unknown feature_key: ['market_state.data_fresh']", 0.6))
    for rid, when, why in (("r1", ts, '["assets={} and asof_candle_utc=null: abstain"]'),
                           ("r2", NOW - timedelta(hours=1), '["asof_candle_utc null"]')):
        jdb.execute("INSERT INTO proposals(run_id, shadow, ts_utc, valid, abstain, module,"
                    " rationale_json) VALUES (?,0,?,1,1,'hold',?)", (rid, _iso(when), why))

    # --- the ledger that reset: 15-minute ticks, sleeve a snaps to the seed at NOW-10h.
    for k in range(97):
        when = NOW - timedelta(hours=24) + timedelta(minutes=15 * k)
        reset = when >= NOW - timedelta(hours=10)
        for sleeve, nav, realised in (("a", 10000.0 if reset else 9957.79,
                                       0.0 if reset else -42.21), ("b", 10000.0, 0.0)):
            jdb.execute("INSERT OR REPLACE INTO nav_points(ts_utc, sleeve, run_id, mode,"
                        " nav_usdt, cash_usdt, realized_pnl, open_trades)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (_iso(when), sleeve, None, "test", nav, nav, realised, 0))

    # --- the hand flatten, in the audit log, two seconds after the force_exit fill.
    jdb.execute("INSERT INTO audit_log(ts_utc, actor, action, target, result)"
                " VALUES (?,?,?,?,?)", (_iso(old_close + timedelta(seconds=1)),
                                        "human:console:uAoHXwho3Z1dcwBA", "autonomy.flatten",
                                        "a", "ok"))
    jdb.commit()

    # --- knowledge: candles for the yardsticks, the data_stale flag, two incidents.
    for pair, p0, p1 in (("BTC/USDT", 80000.0, 82000.0), ("ETH/USDT", 3000.0, 3000.0),
                         ("BNB/USDT", 600.0, 900.0)):
        for when, close in ((NOW - timedelta(hours=25), p0), (NOW - timedelta(hours=1), p1)):
            kdb.execute("INSERT OR REPLACE INTO candles(pair, tf, open_time, open, high, low,"
                        " close, volume, close_time, is_closed) VALUES (?,?,?,?,?,?,?,?,?,1)",
                        (pair, "1h", _ms(when), close, close, close, close, 1.0,
                         _ms(when + timedelta(hours=1)) - 1))
    # A set row and its clear row, in the audit table's own shape; plus an OLD-format pair
    # (the clear row stamped with the clear time, 2026-09-24 style) further back, which
    # must pair by order and not run open to now.
    for active, set_utc, cleared in (
            (1, NOW - timedelta(hours=20), None),
            (0, NOW - timedelta(hours=19), NOW - timedelta(hours=19)),
            (1, NOW - timedelta(hours=3), None),
            (0, NOW - timedelta(hours=3), NOW - timedelta(hours=2))):
        kdb.execute("INSERT INTO flags(name, active, set_utc, cleared_utc, source, severity,"
                    " scope) VALUES (?,?,?,?,?,?,?)",
                    ("data_stale", active, _iso(set_utc), None if cleared is None
                     else _iso(cleared), "healthcheck", "block_entries", "ALL"))
    kdb.execute("INSERT INTO ops_incidents(opened_at, kind, detail) VALUES (?,?,?)",
                (_iso(NOW - timedelta(hours=6)), "host_suspended", json.dumps(
                    {"from": _iso(NOW - timedelta(hours=12)), "to": _iso(NOW - timedelta(
                        hours=6)), "hours": 6.0})))
    kdb.execute("INSERT INTO ops_incidents(opened_at, kind, detail) VALUES (?,?,?)",
                (_iso(NOW - timedelta(hours=3)), "stale_data", "data 200 min old"))
    kdb.commit()

    # --- state files: empty inputs, a fresh sidecar.
    state = root / "knowledge" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "latest.json").write_text(json.dumps({
        "asof_candle_utc": None, "assets": {}, "computed_utc": _iso(NOW), "data_fresh": True,
        "newest_data_age_min": 3, "portfolio": {"regime": "unknown"}}))
    (state / "freshness.json").write_text(json.dumps({
        "version": 1, "updated_at": _iso(NOW),
        "sources": {"book_snapshots": {"latest_utc": _iso(NOW - timedelta(minutes=5))},
                    "candles_1h": {"latest_utc": _iso(NOW - timedelta(minutes=30))}},
        "advisory": {"news": {"latest_utc": _iso(NOW - timedelta(hours=1))}}}))

    # --- heartbeat logs with a six-hour hole, both bots.
    lines = []
    for k in range(24 * 60 + 1):
        when = NOW - timedelta(hours=24) + timedelta(minutes=k)
        if NOW - timedelta(hours=12) < when < NOW - timedelta(hours=6):
            continue
        lines.append(f"{when.strftime('%Y-%m-%d %H:%M:%S')},000 - freqtrade.worker - INFO -"
                     " Bot heartbeat. PID=1, version='2026.8', state='RUNNING'")
    for sleeve in ("a", "b"):
        log = root / "ft_userdata" / sleeve / "logs" / "freqtrade.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join(lines) + "\n")

    # --- the watcher: 0 holdings while AVAX was open, 1 later, 0 when nothing was open.
    (root / "logs").mkdir(exist_ok=True)
    cycles = [(NOW - timedelta(hours=4), 0), (NOW - timedelta(minutes=30), 1),
              (NOW - timedelta(hours=10), 0)]
    (root / "logs" / "watch.log").write_text("".join(
        json.dumps({"cycle_id": f"watch-{t.strftime('%Y%m%dT%H%M%S')}Z", "holdings": h,
                    "checked": h, "notes": []}) + "\n" for t, h in cycles)
        + "/home/x/.venv/bin/python: No module named ops.autonomy\n")

    yield {"jdb": jdb, "kdb": kdb, "loss_a": loss_a, "loss_b": loss_b, "gain_a": gain_a,
           "old_open": old_open}
    jdb.close()
    kdb.close()


def _gaps(ledger: pg.Ledger) -> dict[str, pg.Gap]:
    return {g.key: g for g in ledger.gaps}


def _line(gap: pg.Gap, key: str):
    return next(ln for ln in gap.lines if ln.key == key).value


# --------------------------------------------------------------------------- the windows


def test_both_windows_are_reported_and_since_start_is_the_first_row_in_any_run_db(cfg, root,
                                                                                    seeded):
    report = pg.compute_report(cfg, seeded["jdb"], seeded["kdb"], root, now=NOW)
    assert set(report.windows) == {"last_24h", "since_start"}
    day = report.windows["last_24h"].window
    start = report.windows["since_start"].window
    assert day["since_utc"] == _iso(NOW - timedelta(hours=24)) and day["hours"] == 24.0
    assert start["since_utc"] == _iso(seeded["old_open"])
    assert start["hours"] == pytest.approx(
        (NOW - seeded["old_open"]).total_seconds() / 3600, abs=1e-3)
    assert report.generated_utc == _iso(NOW)
    assert report.profile == (cfg.profiles.active or None)


# --------------------------------------------------------------------------- A. expected


def test_expected_is_declared_not_measured_and_says_so(cfg, root, seeded):
    led = pg.compute(cfg, seeded["jdb"], seeded["kdb"], root, since=NOW - timedelta(hours=24),
                     until=NOW)
    e = led.expected
    assert e["profile"] == (cfg.profiles.active or "shipped")
    assert isinstance(e["expected_per_30d_pct"], float)
    assert isinstance(e["planned_max_drawdown_pct"], float) and e["planned_max_drawdown_pct"] < 0
    assert e["expected_this_window_pct"] == pytest.approx(e["expected_per_30d_pct"] * 24 / 720)
    assert "cannot confirm or refute" in e["note"]
    keys = {ln.key for ln in e["lines"]}
    assert {"expected_per_30d_pct", "planned_max_drawdown_pct",
            "fees_pct_of_balance_per_30d"} <= keys
    assert all(ln.unit and ln.query for ln in e["lines"])


def test_the_fast_test_profile_reads_its_own_evidence_block(root):
    fake = SimpleNamespace(profiles=SimpleNamespace(active="fast-test", dir="config/profiles"))
    assert (REPO_ROOT / "config" / "profiles" / "fast-test.yaml").exists()
    parsed = pg._profile_evidence(fake, root)
    assert parsed is not None
    assert parsed["expected_per_30d_pct"] == -1.29
    assert parsed["fees_pct_of_balance_per_30d"] == 1.35
    assert parsed["measured_max_drawdown_30d_pct"] == -1.46
    assert parsed["planned_max_drawdown_pct"] == -26.8


def test_the_shipped_configuration_uses_the_dip_strategy_numbers(root):
    fake = SimpleNamespace(profiles=SimpleNamespace(active=None, dir="config/profiles"))
    e = pg._expected(fake, root, pg._Window("x", NOW - timedelta(hours=24), NOW))
    assert e["profile"] == "shipped"
    assert e["planned_max_drawdown_pct"] == -45.0
    by_key = {ln.key: ln.value for ln in e["lines"]}
    assert by_key["median_annual_pct"] == 24.8 and by_key["p25_annual_pct"] == 2.2
    assert by_key["p10_annual_pct"] == -15.0 and by_key["worst_annual_pct"] == -38.9


# --------------------------------------------------------------------------- B. realised


def test_realised_counts_the_window_and_the_cumulative_pot_by_import(cfg, root, seeded):
    jdb, kdb = seeded["jdb"], seeded["kdb"]
    day = pg.compute(cfg, jdb, kdb, root, since=NOW - timedelta(hours=24), until=NOW)
    r = day.realised
    assert r["trades"] == 1 and r["wins"] == 1 and r["win_rate"] == 1.0
    assert r["realised_net_usdt"] == pytest.approx(seeded["gain_a"], abs=1e-6)
    assert r["fees_usdt"] == pytest.approx(500 * 0.001 + 525 * 0.001, abs=1e-6)
    assert r["gross_usdt"] == pytest.approx(r["realised_net_usdt"] + r["fees_usdt"], abs=1e-6)
    assert r["fee_gross_ratio"] == pytest.approx(r["fees_usdt"] / r["gross_usdt"], abs=1e-4)
    assert r["exit_reasons"] == {"roi": {"trades": 1, "net_usdt": round(seeded["gain_a"], 4),
                                         "wins": 1}}
    # The cumulative pot is the console's own function, imported, not re-derived here.
    pot_a = pot_service.sleeve_pot(cfg, "a", conn=jdb, root=root)
    assert r["per_sleeve"]["a"]["cumulative_net_usdt"] == pot_a["cumulative_net_usdt"]
    assert r["per_sleeve"]["a"]["realised_all_runs_usdt"] == pytest.approx(
        seeded["loss_a"] + seeded["gain_a"], abs=1e-6)
    assert r["per_sleeve"]["b"]["realised_all_runs_usdt"] == pytest.approx(seeded["loss_b"],
                                                                           abs=1e-6)
    assert r["seed_total_usdt"] == 20000.0
    # BTC 80,000 -> 82,000 on 20,000, costed at 15 bps a side; BNB is data-only and left out
    # of the basket, so the basket is BTC (+2.5%) and ETH (0%).
    cost = (1 - pg.BENCHMARK_COST_PER_SIDE) ** 2
    assert r["benchmark"]["btc_hold_usdt"] == pytest.approx(20000 * (1.025 * cost - 1), abs=0.01)
    assert r["benchmark"]["basket_pairs"] == 2
    assert r["benchmark"]["basket_hold_usdt"] == pytest.approx(
        20000 * (1.0125 * cost - 1), abs=0.01)
    assert set(r["definitions"]) == {"realised_net_usdt", "fees_usdt", "btc_hold_usdt"}
    assert all(ln.unit and ln.query for ln in r["lines"])

    start = pg.compute(cfg, jdb, kdb, root, since=seeded["old_open"], until=NOW)
    assert start.realised["trades"] == 3
    assert start.realised["realised_net_usdt"] == pytest.approx(
        seeded["loss_a"] + seeded["loss_b"] + seeded["gain_a"], abs=1e-3)
    assert start.realised["exit_reasons"]["force_exit"]["trades"] == 1
    assert start.realised["exit_reasons"]["target_zero"]["trades"] == 1


# --------------------------------------------------------------------------- C. the gaps


def test_every_gap_line_in_the_last_24h(cfg, root, seeded):
    led = pg.compute(cfg, seeded["jdb"], seeded["kdb"], root, since=NOW - timedelta(hours=24),
                     until=NOW)
    g = _gaps(led)
    assert list(g) == ["uptime", "refused_entries", "funnel", "model_spend", "decision_inputs",
                       "data_staleness", "holdings_watcher", "fee_drag", "ledger_integrity",
                       "operator_events"]
    assert all(gap.error is None for gap in g.values()), [gap.error for gap in g.values()]
    for gap in g.values():
        for ln in gap.lines:
            assert ln.unit and ln.query, (gap.key, ln.key)

    # 1 uptime: the six-hour hole in both logs, and the incident that explains it.
    up = g["uptime"]
    assert up.size == pytest.approx(6.0, abs=0.02) and up.unit == "hours"
    assert _line(up, "dark_hours") == pytest.approx(6.0, abs=0.02)
    assert _line(up, "nav_dark_hours") == 0.0
    assert _line(up, "uptime_pct") == pytest.approx(75.0, abs=0.1)
    assert _line(up, "host_suspended_incidents") == 1
    assert _line(up, "host_suspended_hours") == 6.0
    assert up.severity == pytest.approx(25.0, abs=0.1)
    assert "not trading 6.0 of the last 24 hours" in up.sentence
    assert "suspended" in up.sentence

    # 2 refused entries: six rows are two refusals.
    rf = g["refused_entries"]
    assert _line(rf, "refused_rows") == 6 and _line(rf, "refused_distinct") == 2
    assert _line(rf, "allowed_entries") == 1 and rf.size == 2.0
    assert rf.detail["by_reason"]["beta_cap"] == {
        "rows": 5, "distinct": 1, "pairs": ["PEPE/USDT"], "cause": pg.REFUSAL_CAUSES["beta_cap"]}
    assert rf.detail["by_reason"]["staleness"]["distinct"] == 1
    assert "2 distinct entries were refused" in rf.sentence and "6 rows" in rf.sentence

    # 3 the funnel, stage by stage.
    fn = g["funnel"]
    want = {"candidates": 4, "screened": 3, "screened_out": 1, "validated": 1,
            "with_verdict": 0, "validator_errors": 1, "expired_unvalidated": 1,
            "error_status": 1, "blocked": 0, "proposed": 2, "abstained": 2, "gate_allowed": 1,
            "filled": 2, "skipped_capability": 0, "on_all_failed": 0, "model_errors": 1}
    assert {k: _line(fn, k) for k in want} == want
    assert fn.size == 3.0 and fn.severity == 100.0
    assert "3 of 3 signals that passed the screen never got a verdict" in fn.sentence
    assert "unknown feature_key" in fn.sentence
    assert [s["stage"] for s in fn.detail["stages"]] == [
        "candidates", "screened", "validated", "verdict", "proposed", "gate_allowed", "filled"]

    # 4 model spend for nothing: the dead validate run plus the uncertain-with-error one.
    ms = g["model_spend"]
    assert _line(ms, "dead_run_usd") == 0.5 and _line(ms, "uncertain_error_usd") == 0.6
    assert _line(ms, "failed_runs_usd") == 3.0 and _line(ms, "total_usd") == 0.62
    assert ms.size == pytest.approx(1.1) and ms.unit == "USD"
    assert ms.detail["dead_by_task"] == {"validate": {"runs": 1, "usd": 0.5}}
    assert "1.10 USD of model spend bought nothing" in ms.sentence

    # 5 decision-stage inputs: assets={} and both proposals abstained on it.
    di = g["decision_inputs"]
    assert _line(di, "latest_assets") == 0 and _line(di, "proposals") == 2
    assert _line(di, "abstained_on_empty_inputs") == 2 and di.severity == 100.0
    assert di.sentence.startswith("The AI bot's decision stage abstained 2 of 2 times")
    assert di.detail["run_ids_on_empty"] == ["r1", "r2"]

    # 6 data staleness: an hour of data_stale, and the ages now.
    st = g["data_staleness"]
    assert _line(st, "data_stale_minutes") == 120.0 and st.size == 120.0
    assert [s["from"] for s in st.detail["spans"]] == [_iso(NOW - timedelta(hours=20)),
                                                       _iso(NOW - timedelta(hours=3))]
    assert _line(st, "stale_data_incidents") == 1
    assert _line(st, "age:book_snapshots") == pytest.approx(5.0, abs=0.1)
    assert _line(st, "age:candles_1h") == pytest.approx(30.0, abs=0.1)
    assert "blocked for 2.0 hours by the data-stale flag" in st.sentence
    assert "allowance" not in st.sentence  # nothing is past its allowance in this fixture

    # 7 the holdings watcher: 0 holdings while AVAX was open.
    hw = g["holdings_watcher"]
    assert _line(hw, "cycles") == 3 and _line(hw, "cycles_with_open_trade") == 2
    assert _line(hw, "cycles_zero_while_open") == 1 and hw.severity == 50.0
    assert "no holdings in 1 of 2 cycles while a position was open" in hw.sentence

    # 8 fee drag, pro-rated: 1% a month is 0.0333% a day.
    fd = g["fee_drag"]
    assert _line(fd, "budget_pct_prorated") == pytest.approx(100 * 0.01 * 24 / 720, abs=1e-6)
    assert _line(fd, "fees_usdt") == pytest.approx(1.025, abs=1e-6)
    assert _line(fd, "fees_pct_of_seed") == pytest.approx(100 * 1.025 / 20000, abs=1e-6)
    assert "Fees took" in fd.sentence and "round trip" in fd.sentence

    # 9 ledger integrity: the reset to the seed, and what it hid.
    li = g["ledger_integrity"]
    assert _line(li, "resets") == 1 and _line(li, "hidden_usdt") == -42.21
    assert li.detail["resets"][0]["sleeve"] == "a"
    assert li.detail["resets"][0]["ts_utc"] == _iso(NOW - timedelta(hours=10))
    assert li.detail["resets"][0]["jump_usdt"] == pytest.approx(42.21, abs=1e-6)
    assert "reset to the seed 1 time(s)" in li.sentence and "42.21" in li.sentence

    # 10 operator and code events: none in the last 24 hours.
    ev = g["operator_events"]
    assert ev.size == 0.0 and _line(ev, "events") == 0 and ev.sentence == ""


def test_operator_events_since_the_start_name_the_hand_and_the_code(cfg, root, seeded):
    led = pg.compute(cfg, seeded["jdb"], seeded["kdb"], root, since=seeded["old_open"],
                     until=NOW)
    ev = _gaps(led)["operator_events"]
    assert _line(ev, "events") == 2 and _line(ev, "audit_rows") == 1
    assert ev.size == pytest.approx(seeded["loss_a"] + seeded["loss_b"], abs=1e-4)
    by_pair = {e["pair"]: e for e in ev.detail["events"]}
    assert by_pair["BTC/USDT"]["actor"] == "human:console"
    assert by_pair["BTC/USDT"]["exit_reason"] == "force_exit"
    assert by_pair["ETH/USDT"]["actor"] == "code"
    assert "a hand-typed flatten on the rules bot" in ev.sentence
    assert "a sell on no mandate on the AI bot" in ev.sentence


# --------------------------------------------------------------------------- D. top three


def test_the_top_three_are_three_plain_sentences_ranked_by_severity(cfg, root, seeded):
    led = pg.compute(cfg, seeded["jdb"], seeded["kdb"], root, since=NOW - timedelta(hours=24),
                     until=NOW)
    top = led.top_three
    assert len(top) == 3
    assert [t["score"] for t in top] == sorted((t["score"] for t in top), reverse=True)
    for t in top:
        assert t["sentence"].endswith(".") and len(t["sentence"]) > 30
        assert "sleeve" not in t["sentence"].lower()   # a builder word Home never prints
        assert t["key"] in {g.key for g in led.gaps}
        assert t["score"] == pytest.approx(t["severity"] * pg.GAP_WEIGHTS[t["key"]], abs=0.01)
    assert {t["key"] for t in top} <= {"funnel", "decision_inputs", "holdings_watcher",
                                       "uptime", "model_spend", "ledger_integrity"}
    # A whole day dark outranks a 100%-of-scope gap in the AI path: severity × weight.
    dark = pg.compute(cfg, seeded["jdb"], seeded["kdb"], root / "nowhere",
                      since=NOW - timedelta(hours=24), until=NOW)
    assert dark.top_three[0]["key"] == "uptime"
    assert set(pg.GAP_WEIGHTS) == {g.key for g in led.gaps}


# --------------------------------------------------------------------------- write


def test_write_produces_the_markdown_and_the_state_json(cfg, root, seeded):
    report = pg.compute_report(cfg, seeded["jdb"], seeded["kdb"], root, now=NOW)
    md, js = pg.write(report, root)
    assert md == root / "reports" / "profit-gaps" / "2026-09-29.md"
    assert js == root / "knowledge" / "state" / "profit_gaps.json"
    text = md.read_text(encoding="utf-8")
    assert "## Window `last_24h`" in text and "## Window `since_start`" in text
    for head in ("### A. Expected", "### B. Realised", "### C. Gaps", "### D. The top three"):
        assert text.count(head) == 2
    assert "cannot confirm or refute" in text
    assert "| Hours with no bot heartbeat | 6 | hours |" in text
    assert "gate_decisions" in text and "llm_calls" in text  # the queries are in the file
    data = json.loads(js.read_text(encoding="utf-8"))
    assert set(data["windows"]) == {"last_24h", "since_start"}
    day = data["windows"]["last_24h"]
    assert day["gaps"][0]["key"] == "uptime" and day["gaps"][0]["size"] == pytest.approx(6.0,
                                                                                         abs=0.02)
    assert len(day["top_three"]) == 3 and day["expected"]["lines"][0]["unit"] == "%"
    # Idempotent: a rerun rewrites the same files whole.
    assert pg.write(report, root) == (md, js)


def test_appendix_line_never_raises(cfg, root, seeded, monkeypatch):
    line = pg.appendix_line(cfg, seeded["jdb"], seeded["kdb"], root, now=NOW)
    assert line.startswith("- profit & gap ledger: `reports/profit-gaps/2026-09-29.md` — ")
    assert (root / "reports" / "profit-gaps" / "2026-09-29.md").exists()

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pg, "compute_report", boom)
    assert pg.appendix_line(cfg, seeded["jdb"], seeded["kdb"], root, now=NOW) == (
        "- profit & gap ledger: failed (RuntimeError: disk full)")


def test_a_fresh_root_renders_a_ledger_of_zeros_and_notes_not_an_exception(cfg, tmp_path,
                                                                            monkeypatch):
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    led = pg.compute(cfg, None, None, tmp_path, since=NOW - timedelta(hours=24), until=NOW)
    assert led.realised["trades"] == 0 and led.realised["realised_net_usdt"] == 0.0
    assert led.realised["benchmark"]["btc_hold_usdt"] is None
    assert all(g.error is None for g in led.gaps)
    up = _gaps(led)["uptime"]
    assert up.size == 24.0  # no log at all is a whole dark window, never "fine"
    assert any("freqtrade.log" in e for e in led.errors)
    assert json.dumps(led.to_json())  # JSON-serialisable, inf and NaN included
    report = pg.compute_report(cfg, None, None, tmp_path, now=NOW)
    assert report.windows["since_start"].window["hours"] == 24.0
    md, js = pg.write(report, tmp_path)
    assert md.exists() and js.exists()
