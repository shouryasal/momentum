"""Skill lint (all 5 research skills) + compute_state behavior via the live repo."""

import importlib.util
import re

import pytest
import yaml

from ops.config import REPO_ROOT

RESEARCH_SKILLS = ("crypto-brief", "market-state", "decide", "exchange-ops", "reg-watch")


def frontmatter(name: str) -> tuple[dict, str]:
    text = (REPO_ROOT / ".claude" / "skills" / name / "SKILL.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, f"{name}: no frontmatter"
    return yaml.safe_load(m.group(1)), m.group(2)


@pytest.mark.parametrize("name", RESEARCH_SKILLS)
def test_frontmatter_and_body(name):
    fm, body = frontmatter(name)
    assert fm["name"] == name
    desc = fm["description"]
    assert "Triggers on" in desc or "consulted when" in desc
    assert not desc.lower().startswith(("you ", "i ")), "description must be third person"
    assert len(body.splitlines()) < 500
    assert "allowed-tools" in fm


def test_exchange_ops_background_only():
    fm, _ = frontmatter("exchange-ops")
    assert fm.get("user-invocable") is False
    assert fm["allowed-tools"] == "Read"


@pytest.mark.parametrize("name", RESEARCH_SKILLS)
def test_referenced_files_exist(name):
    d = REPO_ROOT / ".claude" / "skills" / name
    _, body = frontmatter(name)
    for rel in re.findall(r"references/[\w.-]+\.(?:md|csv)", body):
        assert (d / rel).exists(), f"{name}: {rel} referenced but missing"
    for rel in re.findall(r"scripts/([\w.-]+\.py)", body):
        assert (d / "scripts" / rel).exists(), f"{name}: scripts/{rel} missing"


@pytest.mark.parametrize("name", RESEARCH_SKILLS)
def test_every_skill_has_tests(name):
    tests = REPO_ROOT / ".claude" / "skills" / name / "tests"
    assert tests.exists() and any(tests.rglob("test_*.py")), f"{name}: no tests/"


def test_decide_checklist_mirrors_prompt_template():
    _, body = frontmatter("decide")
    template = (REPO_ROOT / "prompts" / "research.v1.md").read_text()
    for anchor in ("Freshness", "Flags", "invalidation", "Reasons not to trade"):
        assert anchor.lower() in body.lower(), anchor
        assert anchor.lower() in template.lower(), anchor


def _load_compute_state():
    p = REPO_ROOT / ".claude/skills/market-state/scripts/compute_state.py"
    spec = importlib.util.spec_from_file_location("cs_live", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestComputeState:
    def test_empty_db_reports_unknown_and_stale(self, tmp_path):
        from ops import db
        from ops.config import load_config

        cs = _load_compute_state()
        cfg = load_config()
        _, kpath = db.init_all(cfg, root=tmp_path)
        from datetime import UTC, datetime

        with db.connect(kpath) as kdb:
            state = cs.compute_and_write(cfg, kdb, tmp_path,
                                         datetime(2026, 9, 22, tzinfo=UTC))
        assert state["portfolio"]["regime"] == "unknown"
        assert state["data_fresh"] is False

    def test_disagreement_flag(self):
        from datetime import UTC, datetime

        cs = _load_compute_state()
        assets = {"BTC": {"trend": "up", "rvol_20d": 0.9},
                  "ETH": {"trend": "up", "rvol_20d": 0.5}}
        p = cs.portfolio_state(assets, None, datetime(2026, 9, 22, tzinfo=UTC))
        assert p["regime"] == "high_vol"
        assert p["modules"]["disagreement"] is True


# --------------------------------------------------------------------------- v2 skills

TIER1_SKILLS = ("post-mortem", "strategy-lab", "tca", "risk-gate", "skill-smith",
                "asset-dossier", "crypto-brief", "reg-watch", "market-state", "decide")


@pytest.mark.parametrize("name", TIER1_SKILLS)
def test_no_skill_asks_for_the_network(name):
    fm, _ = frontmatter(name)
    tools = str(fm.get("allowed-tools", ""))
    assert "WebFetch" not in tools and "WebSearch" not in tools, name


def test_the_template_exists_and_is_complete():
    d = REPO_ROOT / ".claude" / "skills" / "_template"
    assert (d / "SKILL.md").exists()
    assert (d / "references" / "checklist.md").exists()
    assert any((d / "tests").glob("test_*.py"))
    assert (d / "evals" / "cases.yaml").exists()
    body = (d / "SKILL.md").read_text()
    assert "{{SKILL_NAME}}" in body and "{{SKILL_TITLE}}" in body


def test_the_template_lints_clean_once_its_placeholders_are_filled(tmp_path):
    from evals.skill_lint import lint_skill

    src = REPO_ROOT / ".claude" / "skills" / "_template"
    dst = tmp_path / "example-skill"
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        # Running the template's scripts once leaves a ``__pycache__`` beside them, and a
        # ``.pyc`` read as UTF-8 raises — the test would then fail on a machine state that
        # says nothing about the template.
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        if path.is_dir():
            (dst / rel).mkdir(parents=True, exist_ok=True)
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_text(
            path.read_text(encoding="utf-8")
            .replace("{{SKILL_NAME}}", "example-skill")
            .replace("{{SKILL_TITLE}}", "Example skill"),
            encoding="utf-8")
    result = lint_skill(dst)
    assert result.ok, [f.as_dict() for f in result.errors]


def test_skill_smith_lints_clean_under_the_strict_gate():
    from evals.skill_lint import lint_skill

    d = REPO_ROOT / ".claude" / "skills" / "skill-smith"
    names = {p.name for p in d.parent.iterdir() if p.is_dir()}
    result = lint_skill(d, existing_names=names)
    assert result.ok, [f.as_dict() for f in result.errors]


def test_skill_smith_declares_deterministic_eval_cases():
    from evals.skill_eval import load_cases

    cases = load_cases(REPO_ROOT / ".claude" / "skills" / "skill-smith")
    assert cases, "skill-smith must declare eval cases"
    # at least one runnable case, or the gate could never score it
    assert any("run" in case for case in cases)


def test_skill_smith_names_both_triggers_and_the_scripts_stop():
    _, body = frontmatter("skill-smith")
    lowered = body.lower()
    assert "two" in lowered and "three" in lowered
    assert "held for the human" in lowered
    assert "incubating" in lowered


def test_strategy_lab_tells_the_session_its_numbers_are_claims():
    _, body = frontmatter("strategy-lab")
    lowered = body.lower()
    assert "recompute" in lowered
    assert "worktree" in lowered
    assert "op" in lowered


def test_skill_smith_clears_its_own_eval_floor(tmp_path):
    """Self-referential on purpose: one case checks skill-smith against its own rules."""
    from evals.skill_eval import evaluate
    from ops.config import load_config

    cfg = load_config()
    result = evaluate(REPO_ROOT / ".claude" / "skills" / "skill-smith",
                      cwd=REPO_ROOT, repo_root=REPO_ROOT, timeout_s=120)
    assert result.ok(float(cfg.skills.eval.min_pass_rate)), \
        [case.as_dict() for case in result.cases]
