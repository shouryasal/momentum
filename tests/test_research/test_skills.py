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
