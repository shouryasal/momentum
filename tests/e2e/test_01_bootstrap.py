"""Step 1 — the host bootstrap, run for real against the sandbox.

``ops/setup.sh --check`` reports, ``ops.init_dbs`` is idempotent, and both drift checks
agree with ``config/earn.yaml``. These are the four commands the README's week-1 gate
opens with, so if any of them is wrong nothing downstream is worth believing.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.e2e.conftest import Sandbox

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


class TestRuntimeDirs:
    def test_setup_check_reports_and_changes_nothing(self, sandbox: Sandbox) -> None:
        before = sorted(p.name for p in sandbox.repo.iterdir())
        result = sandbox.run("bash", "ops/setup.sh", "--check")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "runtime directories" in result.stdout
        assert "summary" in result.stdout
        # --check is report-only: nothing in the checkout moved.
        assert sorted(p.name for p in sandbox.repo.iterdir()) == before

    def test_setup_check_refuses_nothing_it_cannot_see(self, sandbox: Sandbox) -> None:
        """Every directory it names is either ok or a warn, never an unexplained FAIL."""
        result = sandbox.run("bash", "ops/setup.sh", "--check")
        fails = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("FAIL")]
        assert fails == [], fails

    def test_the_state_root_carries_the_runtime_layout(self, sandbox: Sandbox) -> None:
        """``$EARN_STATE_ROOT`` is where ``ops.lib.paths`` puts the runtime tree."""
        for rel in ("var/state", "var/runtime", "ops/locks", "ops/killdir", "logs"):
            assert (sandbox.state / rel).is_dir(), f"missing {rel} under the state root"

    def test_setup_checks_the_runtime_tree_where_the_code_reads_it(
        self, sandbox: Sandbox
    ) -> None:
        """setup.sh used to resolve every runtime directory against the checkout, so on a
        split-root host it reported ok for a tree nothing reads — ``ops/killdir`` above
        all, which is the kill switch. It now follows ``$EARN_STATE_ROOT``."""
        result = sandbox.run("bash", "ops/setup.sh", "--check")
        assert f"state root: {sandbox.state}" in result.stdout, result.stdout
        for rel in ("var/state", "var/runtime", "ops/locks", "ops/killdir", "logs"):
            assert f"ok    {sandbox.state / rel}" in result.stdout, \
                f"setup.sh did not check {rel} under the state root:\n{result.stdout}"

    def test_setup_check_points_at_a_missing_state_tree(self, sandbox: Sandbox) -> None:
        """Pointed at an empty state root it warns about the real locations, in full."""
        empty = sandbox.home / "empty-state"
        empty.mkdir(parents=True, exist_ok=True)
        result = sandbox.run("bash", "ops/setup.sh", "--check",
                             env={"EARN_STATE_ROOT": str(empty)})
        for rel in ("var/state", "ops/killdir", "logs"):
            assert f"warn  {empty / rel} missing" in result.stdout, result.stdout
        assert not (empty / "var").exists(), "--check created something"


class TestDatabases:
    def test_init_dbs_is_idempotent(self, sandbox: Sandbox) -> None:
        first = sandbox.py("-m", "ops.init_dbs")
        assert first.returncode == 0, first.stderr
        second = sandbox.py("-m", "ops.init_dbs")
        assert second.returncode == 0, second.stderr
        assert first.stdout == second.stdout, "a second init_dbs reported something different"

    def test_both_databases_land_under_the_state_root(self, sandbox: Sandbox) -> None:
        sandbox.py("-m", "ops.init_dbs")
        journal = sandbox.state / "journal" / "journal.db"
        knowledge = sandbox.state / "knowledge" / "earn.db"
        assert journal.exists() and knowledge.exists()
        # ...and nowhere near the checkout.
        assert not (sandbox.repo / "journal" / "journal.db").exists()

    def test_the_schema_is_at_version_three(self, sandbox: Sandbox) -> None:
        sandbox.py("-m", "ops.init_dbs")
        conn = sqlite3.connect(sandbox.state / "journal" / "journal.db")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert version == 3, f"journal user_version is {version}"
        for table in ("audit_log", "config_audit", "mode_transitions", "sleeve_runs",
                      "nav_points", "signals"):
            assert table in tables, f"{table} missing from a freshly initialised journal"


class TestGeneratedFiles:
    def test_freqtrade_config_has_no_drift(self, sandbox: Sandbox) -> None:
        result = sandbox.py("-m", "ops.gen_freqtrade_config", "--check")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "match" in result.stdout

    def test_ops_files_have_no_drift(self, sandbox: Sandbox) -> None:
        result = sandbox.py("-m", "ops.gen_ops_files", "--check")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "match" in result.stdout

    def test_a_tampered_config_is_caught_by_the_drift_check(self, sandbox: Sandbox) -> None:
        """The check is worth running only if it can fail. Prove it, then put it back."""
        target = sandbox.repo / "config" / "riskgate.json"
        original = target.read_text()
        try:
            target.write_text(original.replace('"phase"', '"phase_tampered"', 1))
            result = sandbox.py("-m", "ops.gen_freqtrade_config", "--check")
            assert result.returncode != 0, "the drift check passed a tampered riskgate.json"
        finally:
            target.write_text(original)
        assert sandbox.py("-m", "ops.gen_freqtrade_config", "--check").returncode == 0
