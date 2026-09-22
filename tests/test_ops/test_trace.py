"""trace.py renders the whole decision story from a seeded journal; every
section is None-tolerant; snapshot outputs/ never break sha verification."""

import json

from evals import snapshot as snapshotlib
from runs.trace import build_trace, write_trace

RUN_ID = "2026-09-22T08:30+04:00"


def _snapshot(root, run_id=RUN_ID):
    return snapshotlib.write_snapshot(
        run_id, inputs={"state": '{"regime": "trend_up"}', "brief": "calm day"},
        limits="max_weight: {BTC: 0.40, default: 0.30}\n",
        fewshot="", rendered_prompt="PROMPT",
        meta=snapshotlib.SnapshotMeta(run_id=run_id, created_at="x",
                                      prompt_version="research.v1",
                                      model="claude-opus-5", git_commit="abc123",
                                      token_budget=20000),
        root=root)


def _seed_story(jdb, root):
    _snapshot(root)
    jdb.execute(
        "INSERT INTO runs(run_id, stage, kind, started_utc, requested_model,"
        " served_model, prompt_version, escalated, escalation_reasons, cost_usd,"
        " num_turns, effort, auth_source, trigger_reason, status)"
        " VALUES (?,?,?,?,?,?,?,1,?,0.9,7,'max','none','news:hack','success')",
        (RUN_ID, "decide", "research", "2026-09-22T04:30:00Z", "claude-fable-5-1",
         "claude-fable-5-1", "research.v1", '["trigger:news:hack"]'))
    jdb.execute(
        "INSERT INTO proposals(run_id, shadow, ts_utc, path, prompt_version, model,"
        " module, targets_json, exposure_scale, confidence, abstain, horizon_days,"
        " rationale_json, invalidation, hard_case_flags_json, valid,"
        " consumed_status, consumed_at, consumed_reason)"
        " VALUES (?,0,'2026-09-22T04:30:00Z','proposals/x.json','research.v1',"
        "'claude-fable-5-1','trend','{\"BTC\":0.4,\"ETH\":0.25,\"USDT\":0.35}',"
        "0.8,0.6,0,7,'[\"BTC above 200d\"]','BTC below 200d','[]',1,"
        "'consumed','2026-09-22T04:35:00Z','fresh')", (RUN_ID,))
    jdb.execute(
        "INSERT INTO gate_decisions(id, ts_utc, sleeve, pair, side, intent,"
        " callback, allowed, reason, severity) VALUES"
        " (7,'2026-09-22T04:36:00Z','b','BTC/USDT','buy','entry',"
        "'confirm_trade_entry',1,'ok','allow')")
    jdb.execute(
        "INSERT INTO orders(id, gate_decision_id, ts_utc, sleeve, pair, side,"
        " order_type, amount, price, status, proposal_run_id) VALUES"
        " (1,7,'2026-09-22T04:36:00Z','b','BTC/USDT','buy','limit',0.01,60000,"
        "'filled',?)", (RUN_ID,))
    jdb.execute(
        "INSERT INTO fills(id, order_id, ts_utc, sleeve, pair, side, fill_amount,"
        " fill_price) VALUES (11,1,'2026-09-22T04:37:00Z','b','BTC/USDT','buy',"
        "0.01,60010)")
    jdb.execute(
        "INSERT INTO tca_fill_costs(fill_id, total_bps, status) VALUES (11,12.5,'ok')")
    jdb.execute(
        "INSERT INTO decision_grades(run_id, graded_at, review_week, process_grade,"
        " process_rubric_json, outcome_vs_rules_bps, outcome_vs_btc_bps,"
        " outcome_grade, outcome_resolved_at, grader_model, grader_run_id)"
        " VALUES (?,'2026-09-23T00:00:00Z','2026-W39',85,"
        "'{\"cites_numbers\": true}',30.0,-12.0,'better','x','g','r')", (RUN_ID,))
    jdb.execute(
        "INSERT INTO root_cause_events(event_id, review_week, kind, ref, cause,"
        " recurrence_key, fix_path, learn_eligible, eligibility_rule, evidence_json)"
        " VALUES ('e1','2026-W39','invalid_proposal',?, 'reasoning',"
        "'late-brief','prompts/research','1','r','{}')", (RUN_ID,))
    jdb.execute(
        "INSERT INTO whatif_nav(date_utc, nav_usdt, last_proposal_run_id, turnover,"
        " cost_usdt) VALUES ('2026-09-22',10120.5,?,0.15,3.2)", (RUN_ID,))
    jdb.commit()
    (root / "lessons.md").write_text(
        "# lessons\n\n## L-2026-W39-01 — trust the 200d line\n"
        f"  date: 2026-09-23  evidence: {RUN_ID}\n")


def test_full_story_renders_every_section(cfg, dbs):
    root, jdb, _ = dbs
    _seed_story(jdb, root)
    snapshotlib.write_output(RUN_ID, "response.json", '{"raw": true}', root=root)
    text = build_trace(cfg, jdb, RUN_ID, root=root)
    for needle in (
        "sha256-verified snapshot", "rendered_prompt.md", "abc123",
        "claude-fable-5-1", "effort: max", "auth: none", "news:hack",
        "trigger:news:hack", "response.json",
        '"BTC": 0.4', "BTC above 200d", "BTC below 200d",
        "consumed", "2026-09-22T04:35:00Z",
        "gate #7 ALLOWED", "order #1", "fill #11", "12.5 bps",
        "30.0 bps", "-12.0 bps", "better", "85/100", "cites_numbers",
        "late-brief", "L-2026-W39-01",
        "10120.50 USDT",
    ):
        assert needle in text, needle


def test_empty_journal_is_none_tolerant(cfg, dbs):
    root, jdb, _ = dbs
    text = build_trace(cfg, jdb, "2026-01-05T08:30+04:00", root=root)
    assert "(not recorded)" in text and "not graded yet" in text
    assert "none recorded" in text  # root causes
    p = write_trace(cfg, jdb, "2026-01-05T08:30+04:00", root=root)
    assert p == root / "reports" / "trace" / "20260105-0830.md"
    assert p.exists()


def test_outputs_exempt_from_sha_manifest(cfg, dbs):
    root, _, _ = dbs
    _snapshot(root)
    snapshotlib.read_snapshot(RUN_ID, root=root)  # verifies clean
    snapshotlib.write_output(RUN_ID, "response.json", "x" * 1000, root=root)
    snapshotlib.write_output(RUN_ID, "response_shadow.json", "{}", root=root)
    snap = snapshotlib.read_snapshot(RUN_ID, root=root)  # still verifies
    assert snap.rendered_prompt == "PROMPT"
    manifest = json.loads((snap.path / "manifest.json").read_text())
    assert not any("outputs" in f or "response" in f for f in manifest["files"])
