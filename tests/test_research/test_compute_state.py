"""``compute_state`` hands the decision stage its indicators — or says, in the file, why not.

The defect these tests pin (docs/design/paper-trading-review-2026-09-29.md §2 #7, §6 item 4):
``asset_state`` needs 201 closed daily bars, ``knowledge/earn.db`` holds ~13, so
``knowledge/state/latest.json`` was written with ``assets: {}`` / ``asof_candle_utc: null``
and 7 of 7 research proposals abstained on empty inputs. The feather store has daily history
from 2017; ``runs.features.trend.daily_closes`` unions the two. This file must read through
that union, and must never write an empty ``assets`` block without a readable ``reason``.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ops import db
from ops.config import REPO_ROOT, load_config
from runs import build_prompt

NOW = datetime(2026, 9, 29, 5, 20, tzinfo=UTC)
DAY = timedelta(days=1)
DAY_MS = 86_400_000


def _load_compute_state():
    p = REPO_ROOT / ".claude/skills/market-state/scripts/compute_state.py"
    spec = importlib.util.spec_from_file_location("earn_compute_state_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cs():
    return _load_compute_state()


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def env(monkeypatch):
    # The feather store is resolved from ``root``; the machine's override must not leak in.
    monkeypatch.delenv("EARN_DATA_DIR", raising=False)


@pytest.fixture
def dbs(cfg, tmp_path, env):
    journal, knowledge = db.init_all(cfg, root=tmp_path)
    jdb, kdb = db.connect(journal), db.connect(knowledge)
    yield jdb, kdb
    jdb.close()
    kdb.close()


def _closes(n: int, end_open: datetime, drift: float = 0.002) -> pd.Series:
    idx = pd.date_range(end=end_open, periods=n, freq="D", tz="UTC")
    return pd.Series(100.0 * np.exp(np.cumsum(np.full(n, drift))), index=idx)


def _write_feather(root: Path, pair: str, closes: pd.Series) -> None:
    d = root / "data" / "binance"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "date": closes.index, "open": closes.values, "high": closes.values * 1.01,
        "low": closes.values * 0.99, "close": closes.values, "volume": 1000.0,
    }).to_feather(d / f"{pair.replace('/', '_')}-1d.feather")


def _seed_db(kdb, pair: str, closes: pd.Series, *, now: datetime = NOW) -> None:
    """The DB's daily bars exactly as ingest leaves them: today's bar present, not closed."""
    now_ms = int(now.timestamp() * 1000)
    rows = []
    for stamp, close in closes.items():
        open_ms = int(stamp.timestamp() * 1000)
        rows.append((pair, "1d", open_ms, float(close),
                     int(open_ms + DAY_MS - 1 <= now_ms)))
    kdb.executemany(
        "INSERT INTO candles(pair, tf, open_time, close, is_closed) VALUES (?,?,?,?,?)", rows)
    kdb.commit()


def _split_history(n_feather: int = 300) -> tuple[pd.Series, pd.Series]:
    """One continuous series to 09-29, split the way the two stores hold it on the runtime
    host: the feather ends 2026-09-22 (300 bars), the DB holds 13 bars (09-17 -> 09-29, the
    last one still open)."""
    full = _closes(n_feather + 7, datetime(2026, 9, 29, tzinfo=UTC))
    return full.loc[:"2026-09-22"], full.iloc[-13:]


# ---------------------------------------------------------------- (3) feather ∪ DB -> populated


def test_a_300_bar_feather_plus_a_13_bar_db_populates_both_core_assets(cs, cfg, dbs, tmp_path):
    _, kdb = dbs
    feather, dbbars = _split_history()
    assert len(feather) == 300 and len(dbbars) == 13
    for pair in ("BTC/USDT", "ETH/USDT"):
        _write_feather(tmp_path, pair, feather)
        _seed_db(kdb, pair, dbbars)
    # 13 rows in the DB, 12 of them closed — the runtime's exact shape on 2026-09-29.
    assert kdb.execute("SELECT COUNT(*) FROM candles WHERE pair='BTC/USDT' AND tf='1d'"
                       ).fetchone()[0] == 13

    state = cs.compute_and_write(cfg, kdb, tmp_path, NOW)

    written = json.loads((tmp_path / cfg.paths.state_latest).read_text())
    assert written == state
    assert set(written["assets"]) == {"BTC", "ETH"}
    assert written["asof_candle_utc"] == "2026-09-28T00:00:00Z"
    assert written["status"] == "ok" and written["reason"] is None and written["missing"] == {}
    btc = written["assets"]["BTC"]
    assert btc["source"] == "feather+kdb"
    assert btc["bars"] == 306                      # 300 feather + 6 DB bars past its end
    assert btc["asof_candle_utc"] == "2026-09-28T00:00:00Z"
    assert btc["asof_candle_close_utc"] == "2026-09-29T00:00:00Z"
    # The numbers are the union's, on the last CLOSED bar, the DB winning on the overlap.
    union = pd.concat([feather, dbbars])
    union = union[~union.index.duplicated(keep="last")].sort_index().loc[:"2026-09-28"]
    assert btc["close"] == pytest.approx(float(dbbars.loc["2026-09-28"]))
    assert btc["ma200"] == pytest.approx(float(union.iloc[-200:].mean()))
    assert btc["ma50"] == pytest.approx(float(union.iloc[-50:].mean()))
    assert btc["trend"] == "up"
    assert written["portfolio"]["regime"] in ("trend_up", "high_vol")
    assert written["portfolio"]["breadth_above_200d"] == 1.0
    # ...and the audit row carries the same as-of stamp.
    row = kdb.execute("SELECT asof_candle_utc, regime FROM state_snapshots"
                      " ORDER BY id DESC LIMIT 1").fetchone()
    assert row["asof_candle_utc"] == "2026-09-28T00:00:00Z"


def test_the_db_alone_at_13_bars_is_refused_with_the_reason_the_review_measured(
        cs, cfg, dbs, tmp_path, capsys):
    """Exactly the runtime's failure: no feather reachable, 13 daily bars in the DB."""
    _, kdb = dbs
    _, dbbars = _split_history()
    for pair in ("BTC/USDT", "ETH/USDT"):
        _seed_db(kdb, pair, dbbars)
    state = cs.compute_and_write(cfg, kdb, tmp_path, NOW)
    assert state["assets"] == {} and state["asof_candle_utc"] is None
    assert state["status"] == "empty"
    assert set(state["missing"]) == {"BTC", "ETH"}
    for asset in ("BTC", "ETH"):
        why = state["missing"][asset]
        assert why.startswith(f"{asset}: 12 closed daily bars (kdb")
        assert "< 201 needed" in why and "feather" in why
    assert state["reason"].startswith("assets block incomplete")
    # loud in the log, not only in the file
    assert "compute_state: status=empty" in capsys.readouterr().err


# ---------------------------------------------------------------- (4) no data -> reason -> prompt


def test_with_no_daily_data_at_all_the_file_says_why_and_the_decide_prompt_carries_it(
        cs, cfg, dbs, tmp_path):
    jdb, kdb = dbs
    state = cs.compute_and_write(cfg, kdb, tmp_path, NOW)
    assert state["assets"] == {} and state["status"] == "empty"
    reason = state["reason"]
    assert reason and "BTC: no closed daily bars:" in reason and "ETH: no closed daily bars:" in reason
    assert "feather" in reason and "candles(tf='1d')" in reason
    on_disk = json.loads((tmp_path / cfg.paths.state_latest).read_text())
    assert on_disk["reason"] == reason
    # ASCII only: json.dumps escapes anything else, and the prompt is the file verbatim.
    assert reason.isascii()
    # The decision stage is handed the file verbatim (research.v4 `{{STATE}}`), so the abstain
    # it writes can cite the cause instead of `assets={}`.
    (tmp_path / "lessons.md").write_text("none\n")
    inputs = build_prompt.gather_inputs(cfg, jdb, tmp_path)
    assert reason in inputs["state"]
    template = (REPO_ROOT / "prompts" / "research.v4.md").read_text()
    built = build_prompt.build(template, run_id="2026-09-29T08:30+04:00", limits="{}",
                               fewshot="", inputs=inputs, escalation_reasons=[],
                               prompt_version="research.v4")
    assert reason in built.text
    assert '"status": "empty"' in built.text


def test_a_partial_block_names_the_missing_asset_and_keeps_the_other(cs, cfg, dbs, tmp_path):
    _, kdb = dbs
    feather, dbbars = _split_history()
    _write_feather(tmp_path, "BTC/USDT", feather)
    for pair in ("BTC/USDT", "ETH/USDT"):
        _seed_db(kdb, pair, dbbars)
    state = cs.compute_and_write(cfg, kdb, tmp_path, NOW)
    assert set(state["assets"]) == {"BTC"} and set(state["missing"]) == {"ETH"}
    assert state["status"] == "partial"
    assert "ETH: 12 closed daily bars" in state["reason"]
    assert state["asof_candle_utc"] == "2026-09-28T00:00:00Z"


def test_a_hole_between_a_stale_feather_and_the_db_is_a_refusal_not_a_200_bar_average(
        cs, cfg, dbs, tmp_path):
    """The feather store refreshed 09-10, the DB starts 09-17: six days nobody has."""
    _, kdb = dbs
    feather, dbbars = _split_history()
    for pair in ("BTC/USDT", "ETH/USDT"):
        _write_feather(tmp_path, pair, feather.loc[:"2026-09-10"])
        _seed_db(kdb, pair, dbbars)
    state = cs.compute_and_write(cfg, kdb, tmp_path, NOW)
    assert state["assets"] == {} and state["status"] == "empty"
    assert "6 calendar day(s) missing (2026-09-10 -> 2026-09-17)" in state["missing"]["BTC"]
    assert "refresh data/binance" in state["missing"]["BTC"]


# ---------------------------------------------------------------- the prompt's state budget


def test_a_populated_state_fits_the_prompt_budget_even_with_a_31_pair_universe(
        cs, cfg, dbs, tmp_path):
    """``build_prompt`` gives the state 1000 tokens and keeps the TAIL on overflow. With a
    per-asset block for every tradeable pair, BTC and ETH (early in sort order) would be the
    first things truncated out of the decision prompt. Core gets blocks; the rest is counts."""
    _, kdb = dbs
    feather, dbbars = _split_history()
    pairs = list(cfg.universe.pairs)
    assert len(pairs) >= 2
    for pair in pairs:
        _write_feather(tmp_path, pair, feather)
        _seed_db(kdb, pair, dbbars)
    state = cs.compute_and_write(cfg, kdb, tmp_path, NOW)
    assert set(state["assets"]) == set(cfg.universe.core)
    wl = state["watchlist"]
    assert wl["n"] == len(pairs) - len(cfg.universe.core)
    assert wl["computed"] == wl["n"] and wl["missing"] == 0
    assert wl["above_200d"] == wl["n"]
    assert wl["share_above_200d"] == (1.0 if wl["n"] else None)
    text = (tmp_path / cfg.paths.state_latest).read_text()
    budget_chars = build_prompt.INPUT_BUDGETS["state"] * 3.5
    assert len(text) <= budget_chars, (len(text), budget_chars)
    assert build_prompt._truncate(text, build_prompt.INPUT_BUDGETS["state"]) == text


def test_regime_changed_utc_carries_forward_across_reruns_on_the_union(cs, cfg, dbs, tmp_path):
    _, kdb = dbs
    feather, dbbars = _split_history()
    for pair in ("BTC/USDT", "ETH/USDT"):
        _write_feather(tmp_path, pair, feather)
        _seed_db(kdb, pair, dbbars)
    first = cs.compute_and_write(cfg, kdb, tmp_path, NOW)
    second = cs.compute_and_write(cfg, kdb, tmp_path, NOW + timedelta(minutes=15))
    assert first["assets"] == second["assets"]
    assert second["portfolio"]["regime_changed_utc"] == first["portfolio"]["regime_changed_utc"]
    assert second["computed_utc"] != first["computed_utc"]
