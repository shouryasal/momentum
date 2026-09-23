"""The two skills the system uses to do its OWN research, and the `discover` stage row.

Every strategy finding this repo holds was produced by a build-time agent and handed over as
finished config. That tests the builders, not the product. `research-scout` (where to look,
and what has already been killed) and `hypothesis-lab` (falsifier first, costs on, both
baselines, walk-forward, honest negative) are what the system runs instead — so these tests
pin the two things that would quietly hollow them out: a skill drifting past the tier-1 tool
allowlist, and the `discover` row buying itself a cheap model or a cheap effort.
"""

from __future__ import annotations

import re

import pytest
import yaml

from ops.config import REPO_ROOT
from ops.models_config import ModelsConfig, load_models_cfg
from runs import router

SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
NEW_SKILLS = ("research-scout", "hypothesis-lab")
DISCOVER_PROMPT = REPO_ROOT / "prompts" / "stages" / "discover.v1.md"


@pytest.fixture(scope="module")
def mc() -> ModelsConfig:
    return load_models_cfg()


def frontmatter(name: str) -> tuple[dict, str]:
    text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, f"{name}: no frontmatter"
    return yaml.safe_load(m.group(1)), m.group(2)


# --------------------------------------------------------------------------- layout


@pytest.mark.parametrize("name", NEW_SKILLS)
def test_the_skill_ships_the_whole_contract(name):
    d = SKILLS_DIR / name
    assert (d / "SKILL.md").is_file()
    assert any((d / "tests").glob("test_*.py")), f"{name}: no tests/"
    assert (d / "evals" / "cases.yaml").is_file(), f"{name}: no eval cases"
    assert any((d / "references").glob("*.md")), f"{name}: no references/"
    assert any((d / "scripts").glob("*.py")), f"{name}: no scripts/"


@pytest.mark.parametrize("name", NEW_SKILLS)
def test_frontmatter_follows_the_conventions(name):
    fm, body = frontmatter(name)
    assert fm["name"] == name
    desc = fm["description"]
    assert "Triggers on" in desc
    assert len(desc) >= 40
    assert not desc.lower().startswith(("you ", "i ")), "description must be third person"
    assert " you " not in desc.lower() and " i " not in desc.lower()
    assert len(body.splitlines()) < 500


@pytest.mark.parametrize("name", NEW_SKILLS)
def test_it_lints_clean_under_the_strict_change_gate(name):
    """`strict_tools=True` is the setting apply_changes uses: a tool outside the tier-1
    allowlist is an ERROR there, because that allowlist is what a model may grant itself."""
    from evals.skill_lint import lint_skill

    d = SKILLS_DIR / name
    names = {p.name for p in d.parent.iterdir() if p.is_dir()}
    result = lint_skill(d, existing_names=names, strict_tools=True)
    assert result.ok, [f.as_dict() for f in result.errors]


@pytest.mark.parametrize("name", NEW_SKILLS)
def test_it_asks_for_no_network_and_no_wildcard_bash(name):
    fm, _ = frontmatter(name)
    tools = str(fm.get("allowed-tools", ""))
    assert "WebFetch" not in tools and "WebSearch" not in tools
    assert "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)" in tools
    assert "Write(knowledge/**)" in tools and "Write(reports/**)" in tools


@pytest.mark.parametrize("name", NEW_SKILLS)
def test_it_declares_enough_deterministic_cases_to_be_scored(name):
    """An `ask:` case is SKIPPED by the code-only gate and a skipped case is not a pass, so
    a skill whose evals are all `ask:` can never clear skills.eval.min_pass_rate."""
    from evals.skill_eval import load_cases

    cases = load_cases(SKILLS_DIR / name)
    runnable = [c for c in cases if "run" in c]
    assert len(runnable) >= 4, f"{name}: only {len(runnable)} runnable cases"


@pytest.mark.parametrize("name", NEW_SKILLS)
def test_the_body_marks_the_tier_boundary(name):
    _, body = frontmatter(name)
    assert "TIER 1" in body
    assert "scripts/**" in body and "tier 2" in body.lower()


# --------------------------------------------------------- what each skill is for


def test_the_scout_carries_the_standing_rejections():
    """The whole reason the ledger exists: a run must not spend a trial rediscovering a
    dead end. Losing one of these is losing the point of the skill."""
    _, body = frontmatter("research-scout")
    lowered = body.lower()
    for phrase in ("rejection ledger", "verdict=rejected", "observe_only"):
        assert phrase in lowered, phrase
    page = (SKILLS_DIR / "research-scout" / "references"
            / "rejected-ledger.md").read_text(encoding="utf-8")
    for topic in ("onchain-flows", "hash-ribbons", "etf-flow-proxy", "unlock-calendar",
                  "execution-timing"):
        assert topic in page, topic


def test_the_scout_never_measures_and_never_points():
    _, body = frontmatter("research-scout")
    lowered = body.lower()
    assert "never measures anything" in lowered
    assert "never emit a direction" in lowered
    assert "hypothesis-lab" in lowered, "the scout must hand off, not conclude"


def test_the_lab_states_the_discipline_and_delegates_the_statistics():
    _, body = frontmatter("hypothesis-lab")
    lowered = body.lower()
    for phrase in ("falsifier", "costs", "both baselines", "regime", "walk forward",
                   "out-of-sample", "honest negative"):
        assert phrase in lowered, phrase
    assert "edge-audit" in lowered, "effective N, purged CV and the hurdle live there"
    assert "expected_max_sharpe(" not in body, "do not restate edge-audit's formula"


def test_the_lab_requires_both_baselines_by_name():
    body = (SKILLS_DIR / "hypothesis-lab" / "SKILL.md").read_text(encoding="utf-8")
    assert "btc_buy_and_hold" in body and "strategy_baseline" in body


def test_the_two_skills_do_not_shadow_anything():
    existing = {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()}
    for name in NEW_SKILLS:
        assert name in existing
        assert name not in {"read", "write", "bash", "glob", "grep", "skill", "task"}


# --------------------------------------------------------------------------- prompt


def test_the_discover_prompt_exists_and_is_tier_one():
    text = DISCOVER_PROMPT.read_text(encoding="utf-8")
    assert "TIER 1" in text
    assert len(text.splitlines()) > 40


def test_every_placeholder_is_substituted_exactly_once():
    """The scan prompt once rendered every data block TWICE because the header comment
    spelled its placeholders WITH braces, making the comment a second substitution site.
    That silently doubled the token bill, so it is pinned here too."""
    text = DISCOVER_PROMPT.read_text(encoding="utf-8")
    header = text.split("-->", 1)[0]
    assert "{{" not in header, "the header comment must not name placeholders with braces"
    found = re.findall(r"\{\{(\w+)\}\}", text)
    assert sorted(found) == sorted(set(found)), f"duplicate placeholder: {found}"
    assert set(found) == {"RUN_ID", "SLOT", "OPEN_QUESTIONS", "LEDGER_SUMMARY",
                          "TRIAL_STATE", "BASELINES", "RECENT_STUDIES", "BUDGET"}


def test_the_prompt_names_both_skills_and_the_statistics_owner():
    text = DISCOVER_PROMPT.read_text(encoding="utf-8")
    assert "research-scout" in text and "hypothesis-lab" in text
    assert "edge-audit" in text


def test_the_prompt_forbids_inventing_a_number_and_proposing_a_change():
    lowered = DISCOVER_PROMPT.read_text(encoding="utf-8").lower()
    assert "may not invent a number" in lowered
    assert "propose nothing here" in lowered
    assert "risk gate" in lowered
    assert "honest negative" in lowered


def test_the_prompt_asks_for_the_audited_outcome_not_the_hoped_one():
    text = DISCOVER_PROMPT.read_text(encoding="utf-8")
    assert "not the one you hoped for" in text
    assert '"audit_problems"' in text


# ------------------------------------------------------------------- the matrix row


def test_the_discover_row_exists_and_explains_itself(mc: ModelsConfig):
    task = mc.task("discover")
    assert task.chain and task.effort
    assert task.why and len(task.why) > 40, "the console shows `why:` beside the row"


def test_discover_sits_where_the_header_documents(mc: ModelsConfig):
    task = mc.task("discover")
    assert task.chain[0] == "sonnet"
    assert task.effort == "high"
    assert task.escalation_effort == "max", "the cheap lever first"
    assert task.escalation == "opus"


def test_a_local_model_may_never_author_a_discovery(mc: ModelsConfig):
    """The standing invariant: a local model authors no proposal and no validation. A
    discovery becomes a proposal, so the same floor binds it."""
    task = mc.task("discover")
    assert task.min_tier >= 3
    assert task.allow_local is False
    assert "local_small" not in task.chain


def test_discover_cannot_be_configured_below_the_authoring_floor():
    """`discover` is deliberately absent from INPUT_TASKS, so effort_floor_for fails closed
    at `high` — adding the row cannot quietly buy a cheap floor."""
    assert "discover" not in router.INPUT_TASKS
    assert router.effort_floor_for("discover") == router.EFFORT_FLOOR == "high"
    assert router.clamp_effort("low", "discover") == "high"


def test_discover_gets_tools_because_it_reads_the_repos_own_data(mc: ModelsConfig):
    task = mc.task("discover")
    assert task.tools == "skill_rw"
    assert task.max_turns and task.max_turns >= 20, "a real study is not a one-turn call"


def test_discover_is_budgeted_under_the_review_row(mc: ModelsConfig):
    """It runs more often than the weekly review and must not outspend it."""
    assert mc.task("discover").max_usd_per_run <= mc.task("review").max_usd_per_run
    assert mc.task("discover").monthly_budget_usd <= mc.task("review").monthly_budget_usd
