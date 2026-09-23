"""Prompt assembly: limits byte-equal earn.yaml values, static prefix stable,
budgets enforced, no P&L, snapshot round-trip byte-identical."""

import json
from datetime import UTC, datetime

import pytest
import yaml

from evals import snapshot as snapshotlib
from ops import db
from ops.config import load_config
from runs import build_prompt

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
RUN_ID = "2026-09-22T08:30+04:00"


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    journal, _ = db.init_all(cfg, root=tmp_path)
    jdb = db.connect(journal)
    # scaffold the template + minimal inputs in the tmp root
    from ops.config import REPO_ROOT

    (tmp_path / "prompts" / "stages").mkdir(parents=True)
    for p in (REPO_ROOT / "prompts" / "stages").iterdir():
        (tmp_path / "prompts" / "stages" / p.name).write_text(p.read_text())
    for v in ("research.v1.md", "research.v2.md", "research.v3.md",
              "research.v4.md"):
        (tmp_path / "prompts" / v).write_text(
            (REPO_ROOT / "prompts" / v).read_text())
    (tmp_path / "lessons.md").write_text("## L-1\nlesson text\n")
    sp = tmp_path / cfg.paths.state_latest
    sp.parent.mkdir(parents=True)
    sp.write_text('{"data_fresh": true}')
    yield cfg, jdb, tmp_path
    jdb.close()


def _build(cfg, jdb, root, reasons=None):
    return build_prompt.build_research_prompt(cfg, jdb, RUN_ID, reasons or [],
                                              NOW, root=root)


def test_limits_block_byte_equal_to_earn_yaml(env):
    cfg, jdb, root = env
    bp, _, limits, _ = _build(cfg, jdb, root)
    parsed = yaml.safe_load(limits)
    assert parsed["max_weight"] == cfg.risk.max_weight
    assert parsed["max_gross_exposure"] == cfg.risk.max_gross_exposure
    assert parsed["usdt_floor"] == cfg.risk.usdt_floor
    assert parsed["max_trades_per_day"] == cfg.risk.max_trades_per_day
    assert limits.rstrip("\n") in bp.text  # copied VERBATIM into the prompt


def test_static_prefix_identical_across_builds(env):
    cfg, jdb, root = env
    bp1, *_ = _build(cfg, jdb, root)
    jdb.execute("INSERT INTO nav_daily(date_utc, sleeve, nav_usdt) VALUES"
                " ('2026-09-22','a',12345)")
    jdb.commit()
    bp2, *_ = _build(cfg, jdb, root)
    assert bp1.static_prefix_len == bp2.static_prefix_len > 1000
    assert bp1.text[:bp1.static_prefix_len] == bp2.text[:bp2.static_prefix_len]
    assert "12345" in bp2.text and "12345" not in bp1.text  # dynamic part differs


def test_escalation_reasons_rendered(env):
    cfg, jdb, root = env
    bp, *_ = _build(cfg, jdb, root, ["near_stop"])
    assert "escalation: near_stop" in bp.text


def test_truncation_keeps_newest(env):
    cfg, jdb, root = env
    (root / "lessons.md").write_text("OLD-MARKER\n" + ("x" * 30000) + "\nNEW-MARKER\n")
    bp, *_ = _build(cfg, jdb, root)
    assert "NEW-MARKER" in bp.text and "OLD-MARKER" not in bp.text
    assert "[...truncated...]" in bp.text


def test_hard_cap_raises(env):
    cfg, jdb, root = env
    active = build_prompt.prompt_version_for(cfg, root)
    template = (root / "prompts" / f"{active}.md").read_text()
    (root / "prompts" / f"{active}.md").write_text(template + "P" * 100000)
    with pytest.raises(build_prompt.PromptBudgetExceeded):
        _build(cfg, jdb, root)


def test_pnl_never_in_graded_block(env):
    cfg, jdb, root = env
    inputs = build_prompt.gather_inputs(cfg, jdb, root)
    assert "pnl" not in inputs["graded"].lower()
    # and the guard actually trips if someone reintroduces it
    with pytest.raises(AssertionError):
        build_prompt._forbid_pnl("run x PnL +5%")


def test_fewshot_missing_degrades(env):
    cfg, jdb, root = env
    bp, *_ = _build(cfg, jdb, root)
    assert "No graded examples yet" in bp.text


def test_snapshot_roundtrip_byte_identical(env):
    cfg, jdb, root = env
    bp, inputs, limits, fewshot = _build(cfg, jdb, root)
    meta = snapshotlib.SnapshotMeta(
        run_id=RUN_ID, created_at="2026-09-22T04:30:00Z", prompt_version="research.v4",
        model="claude-opus-5", git_commit="abc", token_budget=20000)
    d = snapshotlib.write_snapshot(RUN_ID, inputs=inputs, limits=limits,
                                   fewshot=fewshot, rendered_prompt=bp.text,
                                   meta=meta, jdb=jdb, root=root)
    # rebuilding from the snapshot must byte-equal the stored rendered prompt
    # (template read comes from the live repo -> copy matches since we scaffolded it)
    import runs.build_prompt as bpmod

    orig = bpmod.REPO_ROOT
    bpmod.REPO_ROOT = root
    try:
        rebuilt = build_prompt.build_from_snapshot(d)
    finally:
        bpmod.REPO_ROOT = orig
    assert rebuilt.text == bp.text
    row = jdb.execute("SELECT * FROM snapshot_index WHERE run_id=?", (RUN_ID,)).fetchone()
    assert row is not None and row["prompt_version"] == "research.v4"


def test_snapshot_write_once_and_tamper_detected(env):
    cfg, jdb, root = env
    bp, inputs, limits, fewshot = _build(cfg, jdb, root)
    meta = snapshotlib.SnapshotMeta(RUN_ID, "2026-09-22T04:30:00Z", "research.v1",
                                    "claude-opus-5", "abc", 20000)
    d = snapshotlib.write_snapshot(RUN_ID, inputs=inputs, limits=limits,
                                   fewshot=fewshot, rendered_prompt=bp.text,
                                   meta=meta, root=root)
    with pytest.raises(snapshotlib.SnapshotError, match="write-once"):
        snapshotlib.write_snapshot(RUN_ID, inputs=inputs, limits=limits,
                                   fewshot=fewshot, rendered_prompt=bp.text,
                                   meta=meta, root=root)
    (d / "state.json").write_text("{tampered}")
    with pytest.raises(snapshotlib.SnapshotError, match="tampered"):
        snapshotlib.read_snapshot(d)


def test_pre_v2_snapshot_rebuilds_byte_identical(env):
    """A snapshot written BEFORE the dossier inputs existed (no dossiers.md /
    event_stats.json, manifest without them) must still rebuild byte-identically
    under its own v1 template."""
    import hashlib
    import json

    cfg, jdb, root = env
    bp, inputs, limits, fewshot = build_prompt.build_research_prompt(
        cfg, jdb, RUN_ID, [], NOW, root=root, prompt_version="research.v1")
    d = root / "journal" / "snapshots" / "20260922-0830"
    d.mkdir(parents=True)
    old_files = {"state": "state.json", "brief": "brief.md",
                 "positions": "positions.json", "graded": "graded_recent.txt",
                 "lessons": "lessons.md", "flags": "flags.json"}
    files = {}
    for key, fname in old_files.items():
        (d / fname).write_text(inputs[key])
        files[fname] = hashlib.sha256(inputs[key].encode()).hexdigest()
    for fname, content in (("limits.yaml", limits), ("fewshot.txt", fewshot),
                           ("rendered_prompt.md", bp.text)):
        (d / fname).write_text(content)
        files[fname] = hashlib.sha256(content.encode()).hexdigest()
    manifest = {"meta": {"run_id": RUN_ID, "created_at": "x",
                         "prompt_version": "research.v1", "model": "m",
                         "git_commit": "c", "token_budget": 20000,
                         "escalation_reasons": []}, "files": files}
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    snap = snapshotlib.read_snapshot(d)  # tolerant of the missing v2 files
    assert snap.inputs["dossiers"] == "" and snap.inputs["event_stats"] == ""
    import runs.build_prompt as bpmod

    orig = bpmod.REPO_ROOT
    bpmod.REPO_ROOT = root
    try:
        rebuilt = build_prompt.build_from_snapshot(d)
    finally:
        bpmod.REPO_ROOT = orig
    assert rebuilt.text == bp.text and rebuilt.prompt_version == "research.v1"


def test_v2_renders_dossier_summaries_and_event_stats(env):
    cfg, jdb, root = env
    assets = root / "knowledge" / "assets"
    assets.mkdir(parents=True)
    (assets / "BTC.md").write_text(
        "# BTC dossier\n\n## Summary\ncurrent vol rank 0.82\n\n## History\nlong tail\n")
    (root / "knowledge" / "state").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "state" / "event_stats.json").write_text(
        '{"events": {"hack": {"mean_1d_pct": -3.1}}}')
    bp, inputs, *_ = _build(cfg, jdb, root)
    assert bp.prompt_version == "research.v4"
    assert "current vol rank 0.82" in bp.text     # the Summary section is in
    assert "long tail" not in bp.text             # History stays out
    assert '"mean_1d_pct": -3.1' in bp.text
    assert inputs["dossiers"].startswith("### BTC")


def test_v2_without_dossiers_degrades(env):
    cfg, jdb, root = env
    bp, *_ = _build(cfg, jdb, root)
    assert "(no dossiers yet)" in bp.text and "{{DOSSIERS}}" not in bp.text
    assert "{{EVENT_STATS}}" not in bp.text


# ------------------------------------------------------------- v4 (wide universe)

WIDE = ["BTC", "ETH", "SOL", "AVAX", "LINK", "DOT", "ATOM", "NEAR", "OP", "ARB"]
SNAP = {"date": "2026-09-20", "sha256": "f" * 64}


def widen(monkeypatch, cfg, assets=WIDE, snapshot=SNAP):
    """A wide tradeable tier without a resolver snapshot on disk (they are computed fields)."""
    u = type(cfg.universe)
    monkeypatch.setattr(u, "assets", property(lambda self: list(assets)), raising=False)
    monkeypatch.setattr(u, "pairs",
                        property(lambda self: [f"{a}/USDT" for a in assets]), raising=False)
    monkeypatch.setattr(u, "watchlist_pairs",
                        property(lambda self: [f"{a}/USDT" for a in assets] +
                                 [f"W{i}/USDT" for i in range(90)]), raising=False)
    monkeypatch.setattr(u, "snapshot_ref", property(lambda self: dict(snapshot)),
                        raising=False)
    monkeypatch.setattr(u, "tier_of",
                        lambda self, a: "core" if a in ("BTC", "ETH") else "satellite",
                        raising=False)


def test_the_universe_block_names_the_snapshot_and_every_tradeable_tier(env, monkeypatch):
    cfg, jdb, root = env
    widen(monkeypatch, cfg)
    bp, inputs, *_ = _build(cfg, jdb, root)
    payload = json.loads(inputs["universe"])
    assert payload["snapshot"] == SNAP          # what the proposal must quote back
    assert payload["core"] == ["BTC", "ETH"]
    assert [r["asset"] for r in payload["tradeable"]] == WIDE
    tiers = {r["asset"]: r["tier"] for r in payload["tradeable"]}
    assert tiers["BTC"] == "core" and tiers["SOL"] == "satellite"
    caps = {r["asset"]: r["max_weight"] for r in payload["tradeable"]}
    assert caps["BTC"] > caps["SOL"]            # a satellite is not a core position
    assert payload["watchlist_pairs"] == 100    # looked at, not tradeable
    assert payload["max_assets_per_proposal"] == cfg.risk.max_open_positions
    assert inputs["universe"] in bp.text and "{{UNIVERSE}}" not in bp.text


def test_dossiers_cover_core_and_held_not_the_whole_tradeable_tier(env, monkeypatch):
    """A dossier is ~300 tokens; ten of them would eat the whole dossier budget."""
    cfg, jdb, root = env
    widen(monkeypatch, cfg)
    assets = root / "knowledge" / "assets"
    assets.mkdir(parents=True)
    for a in WIDE:
        (assets / f"{a}.md").write_text(f"# {a}\n\n## Summary\n{a} summary line\n")
    jdb.execute("INSERT INTO nav_daily(date_utc, sleeve, nav_usdt, positions_json)"
                " VALUES ('2026-09-22','b',1000,?)",
                (json.dumps({"SOL": 4.0, "AVAX": 0.0}),))
    jdb.commit()
    _, inputs, *_ = _build(cfg, jdb, root)
    assert "BTC summary line" in inputs["dossiers"]
    assert "ETH summary line" in inputs["dossiers"]
    assert "SOL summary line" in inputs["dossiers"]      # held
    assert "AVAX summary line" not in inputs["dossiers"]  # zero position
    assert "LINK summary line" not in inputs["dossiers"]  # tradeable but not held


def test_the_limits_block_carries_the_tier_caps_not_a_hundred_names(env, monkeypatch):
    cfg, jdb, root = env
    widen(monkeypatch, cfg)
    _, _, limits, _ = _build(cfg, jdb, root)
    parsed = yaml.safe_load(limits)
    assert parsed["universe"]["core"] == ["BTC", "ETH"]
    assert parsed["universe"]["tradeable_assets"] == len(WIDE)
    assert "assets" not in parsed["universe"]        # not a ten-line list of names
    assert parsed["tier_caps"]["satellite"] == cfg.risk.tier_caps.satellite
    assert parsed["max_open_positions"] == cfg.risk.max_open_positions


def test_the_decide_floor_is_untouched_by_the_wide_universe():
    """U3 widens what a model may say, never who is allowed to say it."""
    from runs.llm.types import MIN_TIER_FLOOR, ModelRef, chain_for

    assert MIN_TIER_FLOOR["decide"] == 4 and MIN_TIER_FLOOR["validate"] == 3
    local = ModelRef("local", "ollama", "llama3.1:8b", 4)
    assert chain_for("decide", [local], min_tier=1, allow_local=True) == []
