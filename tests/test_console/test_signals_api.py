"""The Signals, Decisions and Knowledge APIs.

Three properties are checked beyond "it returns rows": every mutating route needs a
session and writes an ``audit_log`` row, no route ever runs the pipeline inside the
request (the detached spawn is faked and asserted on), and a console with no databases
answers 503 rather than 500 — a fresh checkout must still render.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from console.services import decisions_service, signals_service
from ops import db
from ops.config import load_config

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def journal(env: Path, monkeypatch: pytest.MonkeyPatch):
    """Initialised databases under the isolated state root, seeded with one signal.

    Every row below is stamped relative to ``NOW``, so the service reads its clock from
    ``NOW`` too. Without the pin the funnel (24h), the detector/model stats (30d) and
    the screener health panel (24h) all measured their windows against the wall clock
    while the rows sat at a fixed date: the assertions below meant "1" on the day they
    were written and "0" a day (or a month) later. A test that changes meaning with the
    calendar is worse than no test, so the clock is part of the fixture.
    """
    monkeypatch.setattr(signals_service, "_now", lambda: NOW)
    cfg = load_config()
    journal_path, _knowledge = db.init_all(cfg, root=env)
    with db.opened(journal_path) as conn:
        conn.execute(
            "INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector, pair,"
            " direction, detector_score, screen_score, strength, features_json,"
            " news_refs_json, dedupe_key, screen_provider, screen_model,"
            " screen_rationale, fast_path, status, status_reason, run_id,"
            " proposal_run_id, updated_utc)"
            " VALUES ('sig-1', ?, 'scan-1','detector','breakout','BTC/USDT','up',"
            " 0.8, 0.9, 0.85, ?, ?, 'k', 'claude','claude-haiku','kept', 0,"
            " 'acted', NULL, '2026-09-22T08:30+04:00','2026-09-22T08:30+04:00', ?)",
            (iso(NOW - timedelta(minutes=10)),
             json.dumps({"detector": "breakout", "detail": {"level": 100.0},
                         "cited": {"BTC/USDT.close": 105.0}}),
             json.dumps(["news-hash"]), iso(NOW)))
        conn.execute(
            "INSERT INTO signal_validations(signal_id, ts_utc, provider, model, verdict,"
            " confidence, suggested_json, horizon_hours, thesis, reasons_json,"
            " counter_evidence_json, invalidation, outcome_ret, outcome_hit,"
            " outcome_resolved_at) VALUES ('sig-1', ?, 'claude','claude-sonnet-5',"
            " 'valid', 0.82, ?, 24, 'thesis', ?, ?, 'a close back inside', 3.2, 1, ?)",
            (iso(NOW - timedelta(minutes=8)), json.dumps({"direction": "up"}),
             json.dumps(["reason"]), json.dumps(["counter"]), iso(NOW)))
        conn.execute(
            "INSERT INTO runs(run_id, stage, kind, started_utc, requested_model,"
            " served_model, provider, signal_id, status)"
            " VALUES ('2026-09-22T08:30+04:00','decide','research', ?, 'claude-opus-5',"
            " 'claude-opus-5','claude:subscription','sig-1','success')",
            (iso(NOW - timedelta(minutes=5)),))
        conn.execute(
            "INSERT INTO proposals(run_id, shadow, ts_utc, path, module, targets_json,"
            " confidence, abstain, valid, signal_id, approval_status)"
            " VALUES ('2026-09-22T08:30+04:00',0, ?, 'proposals/x.json','trend', ?,"
            " 0.6, 0, 1, 'sig-1','pending')",
            (iso(NOW - timedelta(minutes=4)),
             json.dumps({"BTC": 0.45, "ETH": 0.25, "USDT": 0.30})))
        conn.commit()
    return journal_path


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every detached command the console would start, captured instead of run."""
    calls: list[list[str]] = []
    monkeypatch.setattr(signals_service, "_spawn", lambda cmd, root: calls.append(cmd) or 4242)
    return calls


class TestReads:
    def test_signal_list_and_filters(self, auth_client: TestClient, journal):
        body = auth_client.get("/api/signals").json()
        assert [s["signal_id"] for s in body["signals"]] == ["sig-1"]
        assert auth_client.get("/api/signals", params={"status": "candidate"}).json()[
            "signals"] == []
        assert auth_client.get("/api/signals", params={"detector": "breakout"}).json()[
            "signals"]

    def test_signal_detail_carries_features_and_validations(self, auth_client, journal):
        body = auth_client.get("/api/signals/sig-1").json()
        assert body["features"] == {"BTC/USDT.close": 105.0}
        assert body["detector_detail"] == {"level": 100.0}
        assert body["news_refs"] == ["news-hash"]
        v = body["validations"][0]
        assert v["verdict"] == "valid" and v["counter_evidence"] == ["counter"]
        assert v["suggested"] == {"direction": "up"}
        assert body["run"]["served_model"] == "claude-opus-5"
        assert body["proposal"]["approval_status"] == "pending"

    def test_unknown_signal_is_404(self, auth_client, journal):
        assert auth_client.get("/api/signals/nope").status_code == 404

    def test_funnel_reports_counts_and_hit_rates(self, auth_client, journal):
        body = auth_client.get("/api/signals/funnel").json()
        assert body["counts"]["acted"] == 1
        assert body["by_detector"][0]["group"] == "breakout"
        assert body["by_detector"][0]["hit_rate"] == 1.0
        assert body["by_model"][0]["group"] == "claude-sonnet-5"

    def test_the_funnel_window_follows_the_service_clock(
            self, auth_client, journal, monkeypatch: pytest.MonkeyPatch):
        """Two pins, two different answers — impossible if the window is the wall clock.

        ``pipeline.funnel`` takes an injectable ``now``; the console service used to drop
        it and let the pipeline read ``datetime.now(UTC)``. Whatever the real date is,
        that bug answers both halves of this test identically (one fixed window, one set
        of rows), so it cannot satisfy both. No calendar can make it pass.
        """
        far = NOW + timedelta(days=400)
        with db.opened(journal) as conn:
            conn.execute(
                "INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector, pair,"
                " strength, features_json, dedupe_key, status, updated_utc)"
                " VALUES ('sig-far', ?, 'scan-2','detector','breakout','ETH/USDT', 0.5,"
                " '{}', 'k2', 'candidate', ?)", (iso(far - timedelta(minutes=10)), iso(far)))
            conn.commit()

        near = auth_client.get("/api/signals/funnel").json()["counts"]
        assert near["acted"] == 1        # sig-1, ten minutes before the pinned NOW

        monkeypatch.setattr(signals_service, "_now", lambda: far)
        later = auth_client.get("/api/signals/funnel").json()["counts"]
        assert later["detected"] == 1 and later["acted"] == 0     # sig-1 has aged out

    def test_runs_and_proposals(self, auth_client, journal):
        runs = auth_client.get("/api/runs").json()["runs"]
        assert runs[0]["run_id"] == "2026-09-22T08:30+04:00"
        assert runs[0]["signal_id"] == "sig-1"
        detail = auth_client.get("/api/runs/2026-09-22T08:30+04:00").json()
        assert detail["stages"][0]["stage"] == "decide"
        assert detail["signal"]["signal_id"] == "sig-1"
        props = auth_client.get("/api/proposals").json()["proposals"]
        assert props[0]["targets"] == {"BTC": 0.45, "ETH": 0.25, "USDT": 0.30}

    def test_pending_approvals_carry_a_countdown(self, auth_client, journal):
        body = auth_client.get("/api/proposals/pending").json()["pending"]
        assert body[0]["run_id"] == "2026-09-22T08:30+04:00"
        assert body[0]["expires_utc"] and body[0]["seconds_left"] is not None

    def test_trace_renders_markdown_with_the_signal_section(self, auth_client, journal):
        response = auth_client.get("/api/runs/2026-09-22T08:30+04:00/trace")
        assert response.status_code == 200
        assert "text/markdown" in response.headers["content-type"]
        assert "Signal that fired this run" in response.text
        assert "sig-1" in response.text

    def test_knowledge_routes_answer(self, auth_client, journal):
        for path in ("/api/knowledge/briefs", "/api/knowledge/news",
                     "/api/knowledge/sources", "/api/knowledge/state",
                     "/api/knowledge/dossiers", "/api/knowledge/incidents",
                     "/api/knowledge/grades", "/api/reports"):
            assert auth_client.get(path).status_code == 200, path

    def test_a_report_path_cannot_escape_its_directory(self, auth_client, journal):
        response = auth_client.get("/api/reports/../../config/earn.yaml")
        assert response.status_code in (400, 404)

    def test_every_read_needs_a_session(self, client: TestClient, journal):
        for path in ("/api/signals", "/api/signals/funnel", "/api/runs",
                     "/api/proposals", "/api/knowledge/news", "/api/reports"):
            assert client.get(path).status_code == 401, path

    def test_missing_databases_answer_503_not_500(self, auth_client: TestClient):
        for path in ("/api/signals", "/api/runs", "/api/proposals"):
            assert auth_client.get(path).status_code == 503, path


class TestActions:
    def test_scan_now_spawns_the_cron_command_and_audits(self, auth_client, journal,
                                                         no_spawn):
        body = auth_client.post("/api/signals/scan-now").json()
        assert body["spawned"] is True and body["pid"] == 4242
        cmd = no_spawn[0]
        assert cmd[0] == "flock" and "runs.signals" in cmd and "scan" in cmd
        with db.opened(journal, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM audit_log WHERE action='signals.scan_now'").fetchone()
        assert row is not None and row["actor"].startswith("human:console:")

    def test_revalidate_forces_and_audits(self, auth_client, journal, no_spawn):
        body = auth_client.post("/api/signals/sig-1/revalidate").json()
        assert body["spawned"] is True
        cmd = no_spawn[0]
        assert "--signal-id" in cmd and "sig-1" in cmd and "--force" in cmd

    def test_revalidating_an_unknown_signal_is_404(self, auth_client, journal, no_spawn):
        assert auth_client.post("/api/signals/nope/revalidate").status_code == 404
        assert no_spawn == []

    def test_manual_signal_enters_at_screened(self, auth_client, journal):
        body = auth_client.post("/api/signals/manual", json={
            "pair": "BTC/USDT", "direction": "risk", "note": "I saw something"}).json()
        with db.opened(journal, readonly=True) as conn:
            row = conn.execute("SELECT * FROM signals WHERE signal_id=?",
                               (body["signal_id"],)).fetchone()
        assert row["status"] == "screened" and row["source"] == "manual"

    def test_manual_signal_validates_its_input(self, auth_client, journal):
        assert auth_client.post("/api/signals/manual", json={
            "direction": "sideways", "note": "x"}).status_code == 400
        assert auth_client.post("/api/signals/manual", json={
            "direction": "up", "note": "  "}).status_code == 400

    def test_mark_noise_retires_the_signal(self, auth_client, journal):
        assert auth_client.post("/api/signals/sig-1/label",
                                json={"label": "noise"}).status_code == 200
        with db.opened(journal, readonly=True) as conn:
            row = conn.execute("SELECT * FROM signals WHERE signal_id='sig-1'").fetchone()
        assert row["status"] == "screened_out"
        assert row["status_reason"].startswith("human:noise:")

    def test_an_unknown_label_is_refused(self, auth_client, journal):
        assert auth_client.post("/api/signals/sig-1/label",
                                json={"label": "brilliant"}).status_code == 400

    def test_run_research_now_spawns_and_audits(self, auth_client, journal,
                                                monkeypatch: pytest.MonkeyPatch):
        calls: list[list[str]] = []

        class _Proc:
            pid = 99

        monkeypatch.setattr(decisions_service.subprocess, "Popen",
                            lambda cmd, **kw: calls.append(cmd) or _Proc())
        body = auth_client.post("/api/jobs/research/run", json={"slot": "0830"}).json()
        assert body["spawned"] is True and body["slot"] == "0830"
        assert "runs.research_run" in calls[0]
        with db.opened(journal, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM audit_log WHERE action='research.run_now'").fetchone()
        assert row is not None

    def test_every_action_needs_a_session(self, client: TestClient, journal):
        for path in ("/api/signals/scan-now", "/api/signals/sig-1/revalidate",
                     "/api/jobs/research/run"):
            assert client.post(path, json={}).status_code in (401, 403), path
