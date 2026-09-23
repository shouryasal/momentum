"""The operator's backtest runner: rows, argv, window walking and the queue."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from console.services import backtest_service
from ops.lib import compose as composelib
from runs import backtest_job as bj

NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)


def _result_zip(path: Path, *, strategy: str = "SleeveA", profit: float = 0.42) -> Path:
    payload = {
        "strategy": {
            strategy: {
                "profit_total": profit,
                "max_drawdown_account": 0.18,
                "total_trades": 37,
                "backtest_start": "2024-01-01 00:00:00",
                "backtest_end": "2024-07-01 00:00:00",
            }
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("backtest-result-2026-10-27.json", json.dumps(payload))
        zf.writestr("backtest-result-2026-10-27_config.json", "{}")
    return path


@pytest.fixture
def request_a() -> bj.BacktestRequest:
    return bj.BacktestRequest(sleeve="a", timerange="20240101-20241231")


class TestValidation:
    def test_a_bad_timerange_is_refused(self, cfg):
        with pytest.raises(bj.BacktestError, match="timerange"):
            bj.BacktestRequest(sleeve="a", timerange="last year").validate(cfg)

    def test_an_open_ended_timerange_is_allowed(self, cfg):
        bj.BacktestRequest(sleeve="a", timerange="20240101-").validate(cfg)

    def test_an_unknown_kind_is_refused(self, cfg):
        with pytest.raises(bj.BacktestError, match="kind"):
            bj.BacktestRequest(kind="hyperopt", sleeve="a", timerange="20240101-").validate(cfg)

    def test_an_unknown_sleeve_is_refused(self, cfg):
        with pytest.raises(bj.BacktestError, match="sleeve"):
            bj.BacktestRequest(sleeve="c", timerange="20240101-").validate(cfg)

    def test_the_strategy_defaults_to_the_sleeve(self, cfg, request_a):
        assert request_a.strategy_name(cfg) == cfg.sleeves.a.strategy
        assert bj.BacktestRequest(
            sleeve="a", timerange="20240101-", strategy="SleeveB"
        ).strategy_name(cfg) == "SleeveB"


class TestRows:
    def test_the_row_exists_before_anything_runs(self, cfg, jdb, request_a):
        bt_id = bj.create(jdb, cfg, request_a, actor="human:console:1", now=NOW)
        row = bj.get(jdb, bt_id)
        assert row is not None
        assert row["status"] == bj.STATUS_QUEUED
        assert row["strategy"] == cfg.sleeves.a.strategy
        assert row["actor"] == "human:console:1"

    def test_costs_default_to_the_measured_numbers(self, cfg, jdb, request_a):
        bt_id = bj.create(jdb, cfg, request_a, actor="human:cli", now=NOW)
        fee, slip = bj.default_costs()
        row = bj.get(jdb, bt_id)
        assert row is not None
        assert (row["fee_bps"], row["slippage_bps"]) == (fee, slip)

    def test_explicit_costs_win(self, cfg, jdb):
        bt_id = bj.create(
            jdb, cfg,
            bj.BacktestRequest(sleeve="a", timerange="20240101-", fee_bps=7.5, slippage_bps=2.5),
            actor="human:cli", now=NOW,
        )
        row = bj.get(jdb, bt_id)
        assert row is not None and row["fee_bps"] == 7.5

    def test_cancel_only_works_while_it_could_still_run(self, cfg, jdb, request_a):
        bt_id = bj.create(jdb, cfg, request_a, actor="human:cli", now=NOW)
        assert bj.cancel(jdb, bt_id, now=NOW) is True
        assert bj.get(jdb, bt_id)["status"] == bj.STATUS_CANCELLED  # type: ignore[index]
        assert bj.cancel(jdb, bt_id, now=NOW) is False
        assert bj.cancel(jdb, "nope", now=NOW) is False

    def test_listing_is_newest_first(self, cfg, jdb, request_a):
        older = bj.create(jdb, cfg, request_a, actor="human:cli",
                          now=datetime(2026, 10, 26, tzinfo=UTC))
        newer = bj.create(jdb, cfg, request_a, actor="human:cli", now=NOW)
        assert [r["id"] for r in bj.listing(jdb)] == [newer, older]


class TestArgv:
    def test_the_docker_invocation(self, cfg, request_a, state_root):
        argv = bj.backtest_argv(
            cfg, request_a, timerange="20240101-20241231", fee=0.0015, patch=None,
            root=state_root,
        )
        assert argv[:2] == ["docker", "compose"]
        assert "run" in argv and "--rm" in argv and "backtesting" in argv
        assert argv[argv.index("--strategy") + 1] == cfg.sleeves.a.strategy
        assert argv[argv.index("--timerange") + 1] == "20240101-20241231"
        assert argv[argv.index("--fee") + 1] == "0.00150000"
        assert "--enable-protections" in argv
        assert str(composelib.base_path()) in argv

    def test_a_config_patch_becomes_a_second_config(self, cfg, state_root):
        request = bj.BacktestRequest(
            sleeve="b", timerange="20240101-", config_patch={"stake_amount": 100}
        )
        patch = bj.patch_file("bt-1", request.config_patch, root=state_root)
        assert patch is not None and json.loads(patch.read_text())["stake_amount"] == 100
        argv = bj.backtest_argv(
            cfg, request, timerange="20240101-", fee=0.001, patch=patch, root=state_root
        )
        assert argv.count("--config") == 2
        assert f"/freqtrade/user_data/backtests/{patch.name}" in argv

    def test_an_empty_patch_writes_no_file(self, cfg, state_root):
        assert bj.patch_file("bt-2", {}, root=state_root) is None


class TestExecute:
    def _runner(self, ok=True):
        calls: list[list[str]] = []

        def runner(argv, cwd, timeout_s):
            calls.append(list(argv))
            return composelib.CommandResult(
                list(argv), 0 if ok else 2, "done" if ok else "", "" if ok else "boom"
            )

        return runner, calls

    def test_a_single_window_backtest(self, cfg, jdb, request_a, state_root):
        bt_id = bj.create(jdb, cfg, request_a, actor="human:cli", now=NOW)
        zip_path = _result_zip(state_root / "results" / "backtest-result-1.zip")
        runner, calls = self._runner()
        events: list[tuple[str, dict]] = []

        result = bj.execute(
            jdb, cfg, bt_id, request_a, runner=runner, root=state_root, now=lambda: NOW,
            progress=lambda e, p: events.append((e, p)), newest_zip=lambda _d: zip_path,
        )
        assert result["status"] == bj.STATUS_OK
        assert result["windows_run"] == 1
        assert result["profit_total_pct"] == pytest.approx(42.0)
        assert len(calls) == 1
        row = bj.get(jdb, bt_id)
        assert row is not None and row["status"] == bj.STATUS_OK and row["finished_utc"]
        assert json.loads(row["metrics_json"])["windows"][0]["trades"] == 37
        assert [e for e, _ in events] == ["started", "window", "finished"]

    def test_a_walk_forward_runs_every_window(self, cfg, jdb, state_root):
        request = bj.BacktestRequest(
            kind="walk_forward", sleeve="a", timerange="20220101-20240101", oos_months=6
        )
        bt_id = bj.create(jdb, cfg, request, actor="human:cli", now=NOW)
        zip_path = _result_zip(state_root / "results" / "wf.zip")
        runner, calls = self._runner()
        result = bj.execute(
            jdb, cfg, bt_id, request, runner=runner, root=state_root, now=lambda: NOW,
            newest_zip=lambda _d: zip_path,
        )
        assert result["status"] == bj.STATUS_OK
        assert result["windows_run"] == len(calls) > 1
        # compounding, not averaging: two +42% windows are more than +84%
        assert result["profit_total_pct"] > 42.0 * result["windows_run"]

    def test_a_docker_failure_fails_the_row_with_the_output(self, cfg, jdb, request_a, state_root):
        bt_id = bj.create(jdb, cfg, request_a, actor="human:cli", now=NOW)
        runner, _calls = self._runner(ok=False)
        result = bj.execute(
            jdb, cfg, bt_id, request_a, runner=runner, root=state_root, now=lambda: NOW
        )
        assert result["status"] == bj.STATUS_FAILED
        row = bj.get(jdb, bt_id)
        assert row is not None and "boom" in row["error"]

    def test_a_missing_result_archive_is_recorded_not_crashed(
        self, cfg, jdb, request_a, state_root
    ):
        bt_id = bj.create(jdb, cfg, request_a, actor="human:cli", now=NOW)
        runner, _calls = self._runner()
        result = bj.execute(
            jdb, cfg, bt_id, request_a, runner=runner, root=state_root, now=lambda: NOW,
            newest_zip=lambda _d: None,
        )
        assert result["status"] == bj.STATUS_OK
        assert result["windows"][0]["parse_error"] == "no result archive produced"

    def test_cancelling_stops_between_windows(self, cfg, jdb, state_root):
        request = bj.BacktestRequest(
            kind="walk_forward", sleeve="a", timerange="20220101-20240101", oos_months=6
        )
        bt_id = bj.create(jdb, cfg, request, actor="human:cli", now=NOW)
        zip_path = _result_zip(state_root / "results" / "wf.zip")
        calls: list[list[str]] = []

        def runner(argv, cwd, timeout_s):
            calls.append(list(argv))
            bj.cancel(jdb, bt_id, now=NOW)  # the operator hits cancel mid-run
            return composelib.CommandResult(list(argv), 0, "done")

        result = bj.execute(
            jdb, cfg, bt_id, request, runner=runner, root=state_root, now=lambda: NOW,
            newest_zip=lambda _d: zip_path,
        )
        assert result["status"] == bj.STATUS_CANCELLED
        assert len(calls) == 1

    def test_an_unknown_id_raises(self, cfg, jdb, request_a, state_root):
        with pytest.raises(bj.BacktestError, match="unknown backtest"):
            bj.execute(
                jdb, cfg, "bt-nope", request_a, runner=self._runner()[0], root=state_root
            )

    def test_the_parser_handles_a_sleeve_b_archive(self, state_root):
        from runs import walk_forward

        zip_path = _result_zip(state_root / "results" / "b.zip", strategy="SleeveB")
        parsed = walk_forward.parse_backtest_zip(zip_path)
        assert parsed["trades"] == 37


class TestSummarise:
    def test_compounding_and_worst_window(self):
        out = bj.summarise(
            [
                {"window": "a", "profit_total_pct": 10.0, "max_drawdown_pct": 5.0, "trades": 3},
                {"window": "b", "profit_total_pct": -5.0, "max_drawdown_pct": 9.0, "trades": 2},
            ]
        )
        assert out["profit_total_pct"] == pytest.approx(4.5)
        assert out["profit_mean_pct"] == pytest.approx(2.5)
        assert out["worst_window_pct"] == -5.0
        assert out["max_drawdown_pct"] == 9.0
        assert out["trades"] == 5

    def test_unparsed_windows_do_not_poison_the_summary(self):
        out = bj.summarise([{"window": "a", "parse_error": "no archive"}])
        assert out["windows_run"] == 1 and out["profit_total_pct"] is None


class TestQueue:
    def test_submit_creates_a_row_and_queues_it(self, cfg, jdb, state_root, request_a):
        from ops import db as opsdb

        events: list[dict] = []
        queue = backtest_service.BacktestQueue(
            cfg, journal_path=opsdb.journal_path(cfg), root=state_root,
            runner=lambda argv, cwd, t: composelib.CommandResult(list(argv), 0),
            publish=lambda topic, payload: events.append(payload), now=lambda: NOW,
        )
        try:
            bt_id = queue.submit(request_a, actor="human:cli", conn=jdb)
            assert bj.get(jdb, bt_id) is not None
            assert events and events[0]["event"] == "queued"
        finally:
            queue.stop(timeout=1.0)

    def test_a_backtest_cancelled_while_queued_never_runs(self, cfg, jdb, state_root,
                                                          request_a):
        """Cancel writes the row; the ``_Item`` stays in the in-process queue.

        ``backtest_job.execute`` sets ``running`` unconditionally before its first
        per-window cancellation check, so the cancel was overwritten and could never fire:
        the run went to completion — up to 1800 s of docker per window — and finished with
        a ``finished_utc`` stamped *before* it started (``set_status`` COALESCEs it).
        The worker must not start a row that already reached a terminal status.
        """
        ran: list[list[str]] = []
        # The row is created directly so the worker thread never starts: `run_now` is
        # exactly what the worker calls once it pops the item, which is the path at issue.
        bt_id = bj.create(jdb, cfg, request_a, actor="human:cli", now=NOW)
        queue = self._queue(cfg, state_root, ran)
        assert queue.cancel(bt_id) is True
        assert bj.get(jdb, bt_id)["status"] == bj.STATUS_CANCELLED   # still queued, cancelled
        assert queue.run_now(backtest_service._Item(bt_id, request_a)) == {
            "status": bj.STATUS_CANCELLED, "windows": []}
        assert ran == [], "a cancelled backtest still spawned docker"
        row = bj.get(jdb, bt_id)
        assert row is not None and row["status"] == bj.STATUS_CANCELLED

    def test_a_queued_backtest_that_was_not_cancelled_still_runs(self, cfg, jdb,
                                                                 state_root, request_a):
        """The terminal-status guard must not become "never run anything"."""
        ran: list[list[str]] = []
        bt_id = bj.create(jdb, cfg, request_a, actor="human:cli", now=NOW)
        out = self._queue(cfg, state_root, ran).run_now(
            backtest_service._Item(bt_id, request_a))
        assert out["status"] == bj.STATUS_OK and len(ran) == 1

    @staticmethod
    def _queue(cfg, state_root, ran: list[list[str]]):
        from ops import db as opsdb

        def runner(argv, cwd, timeout_s):
            ran.append(list(argv))
            return composelib.CommandResult(list(argv), 0)

        return backtest_service.BacktestQueue(
            cfg, journal_path=opsdb.journal_path(cfg), root=state_root,
            runner=runner, now=lambda: NOW,
        )

    def test_an_interrupted_queue_is_failed_at_start_up(self, cfg, jdb, request_a):
        bt_id = bj.create(jdb, cfg, request_a, actor="human:cli", now=NOW)
        bj.set_status(jdb, bt_id, bj.STATUS_RUNNING, now=NOW)
        assert backtest_service.mark_interrupted(jdb, now=NOW) == [bt_id]
        row = bj.get(jdb, bt_id)
        assert row is not None and row["status"] == bj.STATUS_FAILED
        assert "console restarted" in row["error"]

    def test_get_decodes_the_json_columns(self, cfg, jdb):
        bt_id = bj.create(
            jdb, cfg,
            bj.BacktestRequest(sleeve="a", timerange="20240101-", config_patch={"x": 1}),
            actor="human:cli", now=NOW,
        )
        bj.set_status(jdb, bt_id, bj.STATUS_OK, metrics={"profit_total_pct": 1.0}, now=NOW)
        row = backtest_service.get(jdb, bt_id)
        assert row is not None
        assert row["metrics"]["profit_total_pct"] == 1.0
        assert row["config_patch"] == {"x": 1}

    def test_get_of_an_unknown_id_is_none(self, cfg, jdb):
        assert backtest_service.get(jdb, "bt-nope") is None
