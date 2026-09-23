"""Written instructions that a session or an operator would act on, checked against code.

Two of them had gone stale, and stale instructions fail *open*: the reader believes them.

* ``prompts/review.v{1,2}.md`` told the weekly session to run pytest. No allowlist admits
  it (``runs.decision_core.AUTOMATED_BASH_ALLOWLIST``) and the tier-2 PreToolUse hook
  denies it outright (``.claude/hooks/tier2_paths.py``), so a session that obeys is refused
  mid-protocol and either drops the candidate or, worse, reports a step it never ran.
* ``docs/contracts.md`` and ``README.md`` said the ``subscription`` auth mode *drops* a
  present API key and that the protected rename belongs to ``auto``. ``ops/envwrap.sh``
  renames it unconditionally in both. An operator reading the old text would conclude the
  key is unavailable to a subscription-mode job and stop looking for metered spend.
"""

from __future__ import annotations

import re

import pytest

from ops.config import REPO_ROOT
from runs.decision_core import AUTOMATED_BASH_ALLOWLIST

PROMPTS = ("review.v1.md", "review.v2.md")


def _body(text: str) -> str:
    """The instruction body, with HTML comments (reviewer notes) stripped."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


class TestTheWeeklySessionIsToldOnlyWhatItMayDo:
    @pytest.mark.parametrize("name", PROMPTS)
    def test_the_prompt_does_not_tell_the_session_to_run_pytest(self, name):
        text = _body((REPO_ROOT / "prompts" / name).read_text(encoding="utf-8"))
        instructions = [ln for ln in text.splitlines()
                        if re.search(r"(?<!not )\bpytest\b", ln) and "not**" not in ln]
        offending = [ln for ln in instructions
                     if not re.search(r"do \*\*not\*\* run pytest|never run pytest", ln,
                                      re.I)]
        assert not offending, (
            f"{name} still instructs the session to run pytest: {offending}. "
            "No allowlist admits it and the tier-2 hook denies it; the change gate "
            "recomputes the suite itself."
        )

    @pytest.mark.parametrize("name", PROMPTS)
    def test_every_command_the_prompt_names_is_one_the_session_is_granted(self, name):
        """A `python3 -m <mod>` in the instructions must appear in the Bash allowlist."""
        text = _body((REPO_ROOT / "prompts" / name).read_text(encoding="utf-8"))
        granted = {re.sub(r"[()*]", "", rule).replace("Bash", "").strip()
                   for rule in AUTOMATED_BASH_ALLOWLIST}
        for module in set(re.findall(r"python3 -m ([\w.]+)", text)):
            assert any(f"python3 -m {module}".startswith(g.rstrip()) or
                       g.startswith(f"python3 -m {module}")
                       for g in granted), \
                f"{name} names `python3 -m {module}`, which no allowlist rule grants"

    @pytest.mark.parametrize("name", PROMPTS)
    def test_the_prompt_still_names_the_gate_that_recomputes_the_evidence(self, name):
        text = _body((REPO_ROOT / "prompts" / name).read_text(encoding="utf-8"))
        assert "verify_change" in text or "change gate" in text, \
            f"{name} drops pytest without saying who checks the change instead"


#: Ways a document can claim the subscription mode throws a present API key away. It does
#: not: ``ops/envwrap.sh`` renames it, in ``subscription`` exactly as in ``auto``.
_DROP_CLAIMS = (
    r"drops? a present (`?ANTHROPIC_)?API[ _]?KEY`?",
    r"a present (`?ANTHROPIC_)?API[ _]?KEY`? is \*{0,2}dropp?ed",
    r"present API key is dropped",
)


@pytest.fixture
def envwrap() -> str:
    return (REPO_ROOT / "ops" / "envwrap.sh").read_text(encoding="utf-8")


class TestTheAuthModeTextMatchesEnvwrap:
    """``ops/envwrap.sh`` is the only thing that decides which variable a job sees."""

    def test_subscription_mode_really_renames_rather_than_drops(self, envwrap):
        """The fact the docs describe: `subscription` calls the same rename `auto` does."""
        case = envwrap.split("case \"$AUTH_MODE\" in", 1)[1]
        subscription = case.split("subscription)", 1)[1].split(";;", 1)[0]
        assert "rename_key" in subscription
        assert "EARN_FALLBACK_ANTHROPIC_API_KEY" in envwrap

    @pytest.mark.parametrize("doc", ["docs/contracts.md", "README.md"])
    def test_no_document_still_says_subscription_drops_the_key(self, doc):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        for pattern in _DROP_CLAIMS:
            hit = re.search(pattern, text, re.I)
            assert hit is None, (
                f"{doc} still says a present API key is dropped ({hit.group(0)!r}). "
                "ops/envwrap.sh renames it to EARN_FALLBACK_ANTHROPIC_API_KEY "
                "unconditionally in subscription mode too."
            )

    def test_the_claim_the_documents_must_not_make_is_actually_detectable(self):
        """The guard above is only worth anything if it catches the old wording."""
        stale = ("| `subscription` | `CLAUDE_CODE_OAUTH_TOKEN` only (a present API key is"
                 " dropped, since it would preempt subscription auth) |")
        assert any(re.search(p, stale, re.I) for p in _DROP_CLAIMS)

    @pytest.mark.parametrize("doc", ["docs/contracts.md", "README.md"])
    def test_the_rename_is_documented_as_unconditional_in_both_modes(self, doc):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8").lower()
        assert "unconditional" in text
        window = text.split("unconditional", 1)[1][:400]
        assert "subscription" in window and "auto" in window, \
            f"{doc} still scopes the unconditional rename to auto mode alone"
