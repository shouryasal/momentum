"""Offline brief assertions: pull_news output shape on a fixture DB, and the brief
format lints the replay harness applies to a written brief."""

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))


def lint_brief(text: str) -> list[str]:
    """The format rules a brief must satisfy (used here and by evals/replay)."""
    problems = []
    if len(text.split()) > 600:
        problems.append("over 600 words")
    for line in text.splitlines():
        if line.startswith("- ") and "http" not in line and "[unconfirmed]" not in line \
                and "nothing significant" not in line.lower():
            problems.append(f"claim without source: {line[:60]}")
    if re.search(r"will (rise|fall|reach|hit)", text, re.I):
        problems.append("price prediction")
    return problems


def test_lint_rules():
    good = ("# Brief\n## BTC\n- ETF inflows continue https://x/1\n"
            "## ETH\n- nothing significant\n")
    assert lint_brief(good) == []
    bad = "# Brief\n- BTC will rise to 200k\n"
    assert lint_brief(bad)


def test_pull_news_shape(tmp_path, monkeypatch):
    from ops import db
    from ops.config import load_config

    cfg = load_config()
    db.init_all(cfg, root=REPO_ROOT)  # ensure live DB exists for the script
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / ".claude/skills/crypto-brief/scripts/pull_news.py"),
         "--hours", "24"], capture_output=True, text=True, cwd=REPO_ROOT)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert "clusters" in data and "since" in data
