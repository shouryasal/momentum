"""Signing in to Claude from the console — the parser, the state machine, the storage.

Nothing here reaches Anthropic. Every test drives the **real** pseudo-terminal path against
fake ``claude`` scripts, and those scripts are written from a capture of the real CLI rather
than from what a line-oriented program would do:

* it paints with ANSI — colour, ``[2J``, cursor hiding — and **repaints a spinner** between
  the interesting lines, so the same region arrives many times;
* the verification URL is on **claude.com** (the old parser accepted only ``claude.ai`` and
  ``anthropic.com``, which is why the flow never left "starting");
* the URL is **line-wrapped** by the terminal, and one repaint carries only the first 80
  columns of it, so the first match in the stream is half a link;
* after the browser approval, ``redirect_uri`` lands on *platform.claude.com*, which shows
  an **authorization code**, and the CLI sits at a prompt until that code is typed into it;
* only then does it print the ``sk-ant-oat…`` token.

Five properties are asserted over and over, because they are the ones that would hurt:

* the parser recovers the **complete** URL — claude.com, reassembled across the wrap, not
  the truncated repaint that came first;
* the pasted code reaches the CLI's stdin, a rejected one can be retried, and the code
  appears in no frame, status, audit row or transcript;
* the token reaches ``.env`` and **nothing else** — no response body, no SSE frame, no
  audit row, no transcript, no exception message;
* every path ends in a terminal state with a reason from the closed set, and every phase on
  the way is distinct, because the modal polls this once a second;
* the pty, the child process and the transcript file are gone afterwards, on every path.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from console import security
from console.services import claude_signin_service as signin
from ops import db
from ops.config import load_config
from ops.lib import envfile

from .conftest import step_up

#: What the fake CLI prints. Not a credential: it has never existed anywhere.
FAKE_TOKEN = "sk-ant-oat01-fake00000000000000000000000000ABCD"

#: The shape captured from a real ``claude setup-token``: claude.com, the platform callback
#: as ``redirect_uri`` (which is *why* there is a code to paste back), and a ``state``.
FAKE_URL = (
    "https://claude.com/cai/oauth/authorize?code=true"
    "&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    "&response_type=code"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&scope=user%3Ainference"
    "&code_challenge=Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZg"
    "&code_challenge_method=S256"
    "&state=8c1f0b7a-2d3e-4f5a-9b6c-7d8e9f0a1b2c"
)
#: Where a 200-column-ish terminal breaks it, and what an 80-column repaint got.
URL_HEAD, URL_TAIL = FAKE_URL[:90], FAKE_URL[90:]
URL_TRUNCATED = FAKE_URL[:80]

#: What the callback page shows and the CLI waits for.
FAKE_CODE = "4e17f3aa9c2b8d10#8c1f0b7a-2d3e-4f5a-9b6c-7d8e9f0a1b2c"

pytestmark = pytest.mark.skipif(
    not signin._pty_available(), reason="the sign-in flow needs a pseudo-terminal"
)


# --------------------------------------------------------------------------- fixtures


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)
    return path


#: The TUI preamble every fake shares: escapes, a truncated repaint of the link, a spinner
#: that redraws, then the link wrapped across two lines exactly as the terminal delivers it.
_PREAMBLE = f"""
esc=$(printf '\\033')
printf '%s[?25l%s[2J%s[H' "$esc" "$esc" "$esc"
printf '%s[1mClaude Code%s[0m\\r\\n\\r\\n' "$esc" "$esc"
printf "Browser didn't open? Use the url below to sign in (c to copy)\\r\\n"
printf '%s\\r\\n' '{URL_TRUNCATED}'
for _ in 1 2 3; do
  printf '%s[2K\\r%s[36m*%s[0m Waiting for the browser' "$esc" "$esc" "$esc"
  sleep 0.05
done
printf '\\r\\n'
printf '%s\\r\\n' '{URL_HEAD}'
printf '%s\\r\\n' '{URL_TAIL}'
printf '\\r\\n'
"""

_PROMPT = "printf 'Paste code here if prompted > '\n"
_TOKEN_LINE = (
    f"""printf '\\r\\n%s[32m{FAKE_TOKEN}%s[0m\\r\\n' "$esc" "$esc"\n"""
)


@pytest.fixture
def tui_cli(tmp_path: Path) -> Path:
    """The whole real flow: wrapped claude.com link, code prompt, token, exit 0."""
    return _script(tmp_path, "claude-tui", _PREAMBLE + _PROMPT + f"""
read -r code
if [ "$code" = '{FAKE_CODE}' ]; then
  printf '\\r\\nLogging in\\r\\n'
  sleep 0.1
""" + _TOKEN_LINE + """
  exit 0
fi
printf '\\r\\nInvalid code. Nothing was written.\\r\\n'
exit 4
""")


@pytest.fixture
def retry_cli(tmp_path: Path) -> Path:
    """Rejects the first code out loud, then takes the second — the typo case."""
    return _script(tmp_path, "claude-retry", _PREAMBLE + _PROMPT + """
read -r first
printf '\\r\\nInvalid code. Try again.\\r\\n'
""" + _PROMPT + f"""
read -r second
if [ "$second" = '{FAKE_CODE}' ]; then
""" + _TOKEN_LINE + """
  exit 0
fi
exit 5
""")


@pytest.fixture
def stubborn_cli(tmp_path: Path) -> Path:
    """Never accepts anything, and keeps asking — the budget has to end somewhere."""
    return _script(tmp_path, "claude-stubborn", _PREAMBLE + """
while true; do
  printf 'Paste code here if prompted > '
  read -r code
  printf '\\r\\nInvalid code. Try again.\\r\\n'
done
""")


@pytest.fixture
def quiet_prompt_cli(tmp_path: Path) -> Path:
    """Swallows the code and silently re-draws its prompt, saying nothing at all."""
    return _script(tmp_path, "claude-quiet", _PREAMBLE + _PROMPT + """
read -r code
""" + _PROMPT + """
read -r ignored
sleep 5
""")


@pytest.fixture
def happy_cli(tmp_path: Path) -> Path:
    """An older build that needs no code: prints a link, waits, prints the token."""
    return _script(tmp_path, "claude-happy", f"""
echo "Claude Code will open a browser to authorize this machine."
echo "Visit: {FAKE_URL}"
sleep 0.6
echo ""
echo "Success! Your token:"
echo "{FAKE_TOKEN}"
sleep 0.1
exit 0
""")


@pytest.fixture
def hanging_cli(tmp_path: Path) -> Path:
    """Prints the link and then waits forever — the operator never approves."""
    return _script(tmp_path, "claude-hanging", f"""
echo "Visit: {FAKE_URL}"
sleep 300
""")


@pytest.fixture
def silent_cli(tmp_path: Path) -> Path:
    """Never prints a link at all."""
    return _script(tmp_path, "claude-silent", """
echo "thinking about it"
sleep 300
""")


@pytest.fixture
def failing_cli(tmp_path: Path) -> Path:
    return _script(tmp_path, "claude-failing", f"""
echo "Visit: {FAKE_URL}"
sleep 0.2
echo "error: this machine is not eligible" >&2
exit 3
""")


@pytest.fixture
def tokenless_cli(tmp_path: Path) -> Path:
    return _script(tmp_path, "claude-tokenless", f"""
echo "Visit: {FAKE_URL}"
sleep 0.2
echo "bye"
exit 0
""")


class Recorder:
    """Collects everything the manager is allowed to emit, so tests can search it."""

    def __init__(self, *, present: bool = False, last4: str | None = None) -> None:
        self.frames: list[dict] = []
        self.audits: list[dict] = []
        self.stored: list[str] = []
        self.present = present
        self.last4 = last4

    # the SSE bus
    def publish(self, topic: str, payload: dict) -> None:
        assert topic == signin.TOPIC
        self.frames.append(payload)

    # the .env writer
    def store(self, name: str, value: str) -> dict:
        self.stored.append(name)
        self.value = value
        return {"present": True, "last4": value[-4:]}

    def credential(self) -> dict:
        return {"present": self.present, "last4": self.last4}

    def audit(self, **kwargs: object) -> None:
        self.audits.append(dict(kwargs))

    def text(self) -> str:
        return json.dumps({"frames": self.frames, "audits": self.audits},
                          default=str)


def manager_for(cli: Path, rec: Recorder, tmp_path: Path, **kwargs: float) -> signin.SignInManager:
    return signin.SignInManager(
        store=rec.store, bus=rec, audit=rec.audit, credential=rec.credential,
        transcript_dir=tmp_path / "runtime",
        environ={signin.CLI_ENV_VAR: str(cli), "PATH": os.environ.get("PATH", "")},
        link_timeout_s=kwargs.get("link_timeout_s", 5.0),
        approval_timeout_s=kwargs.get("approval_timeout_s", 5.0),
        exchange_timeout_s=kwargs.get("exchange_timeout_s", 5.0),
        reprompt_grace_s=kwargs.get("reprompt_grace_s", 0.3),
    )


def settle(manager: signin.SignInManager, *, timeout: float = 20.0) -> dict:
    """Wait for the session to reach a terminal state and return it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = manager.status()["session"]
        if session and session["terminal"]:
            return session
        time.sleep(0.05)
    raise AssertionError(f"sign-in never finished: {manager.status()}")


def reach(manager: signin.SignInManager, state: str, *, timeout: float = 15.0) -> dict:
    """Wait for one phase. A terminal state on the way is the failure, not a timeout."""
    deadline = time.monotonic() + timeout
    session: dict | None = None
    while time.monotonic() < deadline:
        session = manager.status()["session"]
        if session and session["state"] == state:
            return session
        if session and session["terminal"]:
            raise AssertionError(f"ended as {session['phase']} before {state}: {session}")
        time.sleep(0.05)
    raise AssertionError(f"never reached {state}: {session}")


# --------------------------------------------------------------------------- the parsers


@pytest.mark.parametrize("text,expected", [
    # The bug that hung the flow: the real CLI's link is on claude.com, and the parser
    # accepted only claude.ai/anthropic.com, so `url` stayed None until the timeout.
    (f"Use the url below to sign in\n{FAKE_URL}", FAKE_URL),
    ("go to https://platform.claude.com/oauth/code/callback",
     "https://platform.claude.com/oauth/code/callback"),
    ("Visit: https://claude.ai/oauth/authorize?code=1&state=x",
     "https://claude.ai/oauth/authorize?code=1&state=x"),
    ("open https://claude.ai/oauth/authorize?code=1.", "https://claude.ai/oauth/authorize?code=1"),
    ("\x1b[1mVisit:\x1b[0m https://claude.ai/go", "https://claude.ai/go"),
    ("see https://console.anthropic.com/oauth", "https://console.anthropic.com/oauth"),
    ("docs at https://example.com/claude.ai", None),
    ("evil https://claude.com.attacker.io/oauth/authorize?state=x", None),
    ("http://claude.com/insecure", None),
    ("nothing here", None),
])
def test_only_an_anthropic_https_link_is_offered_to_the_operator(text: str, expected):  # noqa: ANN001
    """A console that turns any URL in CLI output into a button is a phishing surface."""
    assert signin.find_url(text) == expected


def test_a_wrapped_url_is_reassembled_rather_than_truncated():
    """The terminal breaks the link at its width; the operator still gets a whole link."""
    printed = (
        "Browser didn't open? Use the url below to sign in (c to copy)\n"
        f"{URL_HEAD}\n{URL_TAIL}\n"
    )
    assert signin.find_url(printed) == FAKE_URL


def test_the_complete_url_beats_the_fragment_an_earlier_repaint_left():
    """A repainting TUI prints the link many times, and the first one can be cut off."""
    stream = (
        f"\x1b[2J\x1b[H{URL_TRUNCATED}\n"
        "\x1b[2K\r\x1b[36m*\x1b[0m Waiting for the browser\n"
        f"{URL_HEAD}\n{URL_TAIL}\n"
        "\nPaste code here if prompted > "
    )
    assert signin.find_url(stream) == FAKE_URL


def test_prose_under_the_link_is_not_glued_onto_it():
    """Reassembly is the fix; swallowing the next line would be a new bug."""
    stream = f"{FAKE_URL}\nPaste code here if prompted > \n"
    assert signin.find_url(stream) == FAKE_URL


@pytest.mark.parametrize("text,expected", [
    ("Paste code here if prompted > ", True),
    ("\x1b[1mPaste code here if prompted >\x1b[0m ", True),
    ("Enter the authorization code:", True),
    ("Authorization code >", True),
    # the link itself is full of the word "code" and must not look like a prompt
    (FAKE_URL, False),
    ("Browser didn't open? Use the url below to sign in (c to copy)", False),
    ("", False),
])
def test_the_code_prompt_is_recognised_without_firing_on_the_url(text: str, expected: bool):
    assert signin.wants_code(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("Invalid code. Try again.", True),
    ("that code is expired", True),
    ("Authentication failed", True),
    ("Logging in", False),
])
def test_a_rejected_code_is_recognised(text: str, expected: bool):
    assert signin.rejected_code(text) is expected


@pytest.mark.parametrize("raw,expected", [
    (f"  {FAKE_CODE}\n", FAKE_CODE),
    ("abc123", "abc123"),
])
def test_a_pasted_code_is_trimmed(raw: str, expected: str):
    assert signin.clean_code(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "two words", "line\nbreak", "bell\x07", "x" * 2000])
def test_a_code_that_could_drive_the_terminal_is_refused(raw: str):
    """Whatever this returns is typed into a child process; it is one printable line."""
    with pytest.raises(signin.SignInError) as caught:
        signin.clean_code(raw)
    assert caught.value.reason == "invalid"


@pytest.mark.parametrize("text,expected", [
    (f"token: {FAKE_TOKEN}", FAKE_TOKEN),
    (f"\x1b[32m{FAKE_TOKEN}\x1b[0m", FAKE_TOKEN),
    ("sk-ant-api03-this-is-a-metered-key-not-a-subscription-token", None),
    ("sk-ant-oat-short", None),
    ("", None),
])
def test_only_a_subscription_token_shape_is_captured(text: str, expected):  # noqa: ANN001
    assert signin.find_token(text) == expected


def test_a_transcript_is_redacted_and_deletes_itself(tmp_path: Path):
    """The only file this flow writes must survive neither the token nor the session."""
    path = tmp_path / "t.log"
    transcript = signin._Transcript(path)
    transcript.mask(FAKE_CODE)
    transcript.write(f"Visit: {FAKE_URL}\n\x1b[32m{FAKE_TOKEN}\x1b[0m\n")
    transcript.write(f"Paste code here if prompted > {FAKE_CODE}\n")
    body = path.read_text()
    assert FAKE_TOKEN not in body
    assert FAKE_CODE not in body, "the TUI echoes what it is given; the echo is masked"
    assert FAKE_URL in body, "the link is not a secret; hiding it would hide the fix"
    assert oct(path.stat().st_mode)[-3:] == "600"
    transcript.discard()
    assert not path.exists()
    transcript.discard()  # idempotent: cleanup runs from several places


# --------------------------------------------------------------------------- state machine


def test_the_real_flow_walks_every_phase_and_ends_signed_in(tui_cli: Path, tmp_path: Path):
    rec = Recorder()
    manager = manager_for(tui_cli, rec, tmp_path, approval_timeout_s=15.0)
    manager.start(actor="human:console")

    waiting = reach(manager, "awaiting_code")
    assert waiting["url"] == FAKE_URL, "the operator got the whole link, not a fragment"
    assert "paste the code" in waiting["message"].lower()

    manager.submit_code(FAKE_CODE, actor="human:console")
    session = settle(manager)

    assert session["state"] == "done"
    assert session["reason"] is None
    assert session["phase"] == "done"
    assert session["last4"] == FAKE_TOKEN[-4:]
    assert session["attempts"] == 1
    assert rec.stored == [signin.TOKEN_SECRET_NAME]

    phases = [f["phase"] for f in rec.frames]
    for phase in ("starting", "url_ready", "awaiting_code", "exchanging", "done"):
        assert phase in phases, f"the modal never saw {phase}: {phases}"
    assert phases.index("url_ready") < phases.index("awaiting_code") < phases.index("done")


def test_the_url_is_upgraded_when_a_later_repaint_carries_all_of_it(tui_cli: Path,
                                                                    tmp_path: Path):
    """The first frame may hold a truncated link; the operator must not click that one."""
    rec = Recorder()
    manager = manager_for(tui_cli, rec, tmp_path, approval_timeout_s=15.0)
    manager.start(actor="human:console")
    reach(manager, "awaiting_code")
    manager.cancel()

    urls = [f["url"] for f in rec.frames if f["url"]]
    assert urls[-1] == FAKE_URL
    assert all(u == FAKE_URL or FAKE_URL.startswith(u) for u in urls), \
        "a frame carried a link that is not part of the real one"


def test_the_code_actually_reaches_the_cli_and_no_frame_carries_it(tui_cli: Path,
                                                                   tmp_path: Path):
    rec = Recorder()
    manager = manager_for(tui_cli, rec, tmp_path, approval_timeout_s=15.0)
    manager.start(actor="human:console")
    reach(manager, "awaiting_code")
    manager.submit_code(FAKE_CODE)
    # The fake CLI exits 4 unless it read exactly this code off its own stdin, so a `done`
    # here is proof the bytes arrived through the pty.
    assert settle(manager)["state"] == "done"

    blob = rec.text() + repr(manager.status())
    assert FAKE_CODE not in blob
    assert "4e17f3aa9c2b8d10" not in blob
    assert [a["action"] for a in rec.audits].count("claude.signin.code") == 1
    entry = next(a for a in rec.audits if a["action"] == "claude.signin.code")
    assert entry["detail"]["attempt"] == 1
    assert FAKE_CODE not in json.dumps(entry, default=str)


def test_a_code_cannot_be_submitted_before_the_cli_asks_for_one(hanging_cli: Path,
                                                                tmp_path: Path):
    rec = Recorder()
    manager = manager_for(hanging_cli, rec, tmp_path, approval_timeout_s=300.0)
    with pytest.raises(signin.SignInError) as idle:
        manager.submit_code(FAKE_CODE)
    assert idle.value.reason == "not_found"

    manager.start(actor="human:console")
    reach(manager, "url_ready")
    # `url_ready` is not `awaiting_code`: this CLI never reaches its prompt, and writing a
    # line into one that is not asking would be swallowed by the TUI.
    with pytest.raises(signin.SignInError) as early:
        manager.submit_code(FAKE_CODE)
    assert early.value.reason == "conflict"
    assert manager.status()["session"]["attempts"] == 0
    manager.cancel()


def test_a_rejected_code_comes_back_as_a_retry_not_a_dead_end(retry_cli: Path,
                                                              tmp_path: Path):
    rec = Recorder()
    manager = manager_for(retry_cli, rec, tmp_path, approval_timeout_s=15.0)
    manager.start(actor="human:console")
    reach(manager, "awaiting_code")

    manager.submit_code("not-the-code")
    again = reach(manager, "awaiting_code")
    assert again["attempts"] == 1
    assert again["attempts_left"] == signin.MAX_CODE_ATTEMPTS - 1
    assert "not accepted" in again["message"]
    assert not again["terminal"], "a typo must not end the sign-in"

    manager.submit_code(FAKE_CODE)
    session = settle(manager)
    assert session["state"] == "done"
    assert session["attempts"] == 2
    assert rec.stored == [signin.TOKEN_SECRET_NAME]


def test_a_silent_reprompt_is_treated_as_a_rejection(quiet_prompt_cli: Path,
                                                     tmp_path: Path):
    """Some builds just redraw the prompt. A modal that sits in `exchanging` forever is
    the hang this whole rebuild is about."""
    rec = Recorder()
    manager = manager_for(quiet_prompt_cli, rec, tmp_path, approval_timeout_s=15.0,
                          reprompt_grace_s=0.3)
    manager.start(actor="human:console")
    reach(manager, "awaiting_code")
    manager.submit_code(FAKE_CODE)
    again = reach(manager, "awaiting_code")
    assert again["attempts"] == 1
    manager.cancel()


def test_three_bad_codes_stop_the_flow_with_bad_code(stubborn_cli: Path, tmp_path: Path):
    rec = Recorder()
    manager = manager_for(stubborn_cli, rec, tmp_path, approval_timeout_s=15.0)
    manager.start(actor="human:console")
    for attempt in range(signin.MAX_CODE_ATTEMPTS):
        reach(manager, "awaiting_code")
        manager.submit_code(f"wrong-{attempt}")
    session = settle(manager)
    assert (session["state"], session["reason"]) == ("failed", "bad_code")
    assert session["phase"] == "failed:bad_code"
    assert session["attempts"] == signin.MAX_CODE_ATTEMPTS
    assert rec.stored == []
    assert list((tmp_path / "runtime").glob("*")) == []


def test_a_code_that_is_never_pasted_times_out_as_no_code(stubborn_cli: Path,
                                                          tmp_path: Path):
    rec = Recorder()
    manager = manager_for(stubborn_cli, rec, tmp_path, approval_timeout_s=1.2)
    manager.start(actor="human:console")
    # The wait it dies in is the code wait, not the approval wait: this CLI reaches its
    # prompt straight away and then nobody types anything.
    session = settle(manager)
    assert (session["state"], session["reason"]) == ("failed", "no_code")
    assert session["phase"] == "failed:no_code"
    assert "Nothing was written" in session["message"]
    assert rec.stored == []


def test_no_frame_no_status_and_no_audit_row_can_carry_the_token(tui_cli: Path,
                                                                 tmp_path: Path):
    rec = Recorder()
    manager = manager_for(tui_cli, rec, tmp_path, approval_timeout_s=15.0)
    manager.start(actor="human:console")
    reach(manager, "awaiting_code")
    manager.submit_code(FAKE_CODE)
    settle(manager)

    blob = rec.text() + repr(manager.status())
    assert FAKE_TOKEN not in blob
    assert "sk-ant-oat" not in blob
    assert "last4" in blob and FAKE_TOKEN[-4:] in blob, "…last4 is the one part that shows"
    # The session dataclass has no field a token or a code could be assigned to at all.
    fields = signin.SignInSession("x").as_dict()
    assert "token" not in fields
    assert "code" not in fields


def test_the_happy_path_of_an_older_cli_still_works(happy_cli: Path, tmp_path: Path):
    """A build that prints the token without asking for a code must not need one."""
    rec = Recorder()
    manager = manager_for(happy_cli, rec, tmp_path)
    manager.start(actor="human:console")
    session = settle(manager)

    assert session["state"] == "done"
    assert session["last4"] == FAKE_TOKEN[-4:]
    phases = [f["phase"] for f in rec.frames]
    assert "url_ready" in phases, "the operator was never shown a link to approve"
    assert phases.index("url_ready") < phases.index("done")
    link = next(f for f in rec.frames if f["phase"] == "url_ready")
    assert link["url"] == FAKE_URL
    assert "windows browser" in link["message"].lower()


def test_everything_is_cleaned_up_after_a_successful_sign_in(tui_cli: Path, tmp_path: Path):
    rec = Recorder()
    manager = manager_for(tui_cli, rec, tmp_path, approval_timeout_s=15.0)
    manager.start(actor="human:console")
    reach(manager, "awaiting_code")
    manager.submit_code(FAKE_CODE)
    settle(manager)
    time.sleep(0.2)

    assert list((tmp_path / "runtime").glob("claude-signin-*")) == []
    assert manager._proc is None
    assert manager._transcript is None
    assert manager._pending_code is None


def test_a_missing_cli_fails_immediately_with_the_install_command(tmp_path: Path):
    rec = Recorder()
    manager = signin.SignInManager(
        store=rec.store, bus=rec, audit=rec.audit, credential=rec.credential,
        environ={signin.CLI_ENV_VAR: str(tmp_path / "nope"), "PATH": str(tmp_path)},
    )
    session = manager.start(actor="human:console")
    assert session["state"] == "failed"
    assert session["reason"] == "cli_missing"
    assert session["phase"] == "failed:cli_missing"
    assert "npm i -g @anthropic-ai/claude-code" in session["message"]
    assert rec.stored == []
    assert manager.status()["cli"]["present"] is False


def test_a_link_that_never_arrives_times_out_without_writing_anything(silent_cli: Path,
                                                                     tmp_path: Path):
    rec = Recorder()
    manager = manager_for(silent_cli, rec, tmp_path, link_timeout_s=0.8)
    manager.start(actor="human:console")
    session = settle(manager)
    assert (session["state"], session["reason"]) == ("failed", "no_link")
    assert rec.stored == []


def test_an_operator_who_never_approves_times_out_and_is_told_so(hanging_cli: Path,
                                                                tmp_path: Path):
    rec = Recorder()
    manager = manager_for(hanging_cli, rec, tmp_path, approval_timeout_s=1.0)
    manager.start(actor="human:console")
    session = settle(manager)
    assert (session["state"], session["reason"]) == ("failed", "not_approved")
    assert session["url"] == FAKE_URL
    assert "Nothing was written" in session["message"]
    assert rec.stored == []
    assert list((tmp_path / "runtime").glob("*")) == []


def test_a_cli_that_exits_non_zero_reports_its_code(failing_cli: Path, tmp_path: Path):
    rec = Recorder()
    manager = manager_for(failing_cli, rec, tmp_path)
    manager.start(actor="human:console")
    session = settle(manager)
    assert (session["state"], session["reason"]) == ("failed", "cli_failed")
    assert session["exit_code"] == 3
    assert rec.stored == []


def test_a_clean_exit_with_no_token_is_a_failure_not_a_success(tokenless_cli: Path,
                                                              tmp_path: Path):
    rec = Recorder()
    manager = manager_for(tokenless_cli, rec, tmp_path)
    manager.start(actor="human:console")
    session = settle(manager)
    assert (session["state"], session["reason"]) == ("failed", "no_token")
    assert rec.stored == []


def test_a_cli_that_dies_on_a_bad_code_says_so(tui_cli: Path, tmp_path: Path):
    """The fake exits 4 on a wrong code; "exited with code 4" would send the operator
    looking for a broken CLI instead of re-copying the code."""
    rec = Recorder()
    manager = manager_for(tui_cli, rec, tmp_path, approval_timeout_s=15.0)
    manager.start(actor="human:console")
    reach(manager, "awaiting_code")
    manager.submit_code("definitely-not-it")
    session = settle(manager)
    assert (session["state"], session["reason"]) == ("failed", "bad_code")
    assert rec.stored == []


def test_cancel_kills_the_cli_and_leaves_no_pty_behind(hanging_cli: Path, tmp_path: Path):
    rec = Recorder()
    manager = manager_for(hanging_cli, rec, tmp_path, approval_timeout_s=300.0)
    manager.start(actor="human:console")
    reach(manager, "url_ready")

    session = manager.cancel(actor="human:console")
    assert (session["state"], session["reason"]) == ("cancelled", "cancelled")
    assert session["phase"] == "cancelled:cancelled"
    assert rec.stored == []
    assert list((tmp_path / "runtime").glob("*")) == []
    assert manager._proc is None
    assert [a["action"] for a in rec.audits].count("claude.signin.cancel") == 1


def test_cancelling_while_it_waits_for_a_code_still_cleans_up(stubborn_cli: Path,
                                                              tmp_path: Path):
    rec = Recorder()
    manager = manager_for(stubborn_cli, rec, tmp_path, approval_timeout_s=300.0)
    manager.start(actor="human:console")
    reach(manager, "awaiting_code")
    session = manager.cancel(actor="human:console")
    assert session["state"] == "cancelled"
    assert manager._proc is None
    assert list((tmp_path / "runtime").glob("*")) == []


def test_cancelling_nothing_is_a_not_found_not_a_crash(tmp_path: Path):
    rec = Recorder()
    manager = signin.SignInManager(store=rec.store, credential=rec.credential,
                                   environ={signin.CLI_ENV_VAR: ""})
    with pytest.raises(signin.SignInError) as caught:
        manager.cancel()
    assert caught.value.reason == "not_found"


def test_two_sign_ins_cannot_race_one_credentials_file(hanging_cli: Path, tmp_path: Path):
    rec = Recorder()
    manager = manager_for(hanging_cli, rec, tmp_path, approval_timeout_s=300.0)
    manager.start(actor="human:console")
    with pytest.raises(signin.SignInError) as caught:
        manager.start(actor="human:console")
    assert caught.value.reason == "conflict"
    manager.cancel()


def test_an_existing_credential_is_not_replaced_without_saying_so(happy_cli: Path,
                                                                 tmp_path: Path):
    rec = Recorder(present=True, last4="9999")
    manager = manager_for(happy_cli, rec, tmp_path)
    with pytest.raises(signin.SignInError) as caught:
        manager.start(actor="human:console")
    assert caught.value.reason == "conflict"
    assert "9999" in str(caught.value)
    assert rec.stored == []

    manager.start(actor="human:console", replace=True)
    assert settle(manager)["state"] == "done"


def test_a_writer_that_refuses_fails_the_sign_in_loudly(happy_cli: Path, tmp_path: Path):
    rec = Recorder()

    def refuse(_name: str, value: str) -> dict:
        raise RuntimeError(f"disk full while writing {value}")

    manager = manager_for(happy_cli, rec, tmp_path)
    manager._store = refuse
    manager.start(actor="human:console")
    session = settle(manager)
    assert (session["state"], session["reason"]) == ("failed", "store_failed")
    assert FAKE_TOKEN not in session["message"], "the writer's message is redacted first"
    assert security.redact(FAKE_TOKEN) in session["message"]


def test_shutdown_stops_a_live_sign_in(hanging_cli: Path, tmp_path: Path):
    rec = Recorder()
    manager = manager_for(hanging_cli, rec, tmp_path, approval_timeout_s=300.0)
    manager.start(actor="human:console")
    manager.shutdown()
    assert manager.status()["session"]["terminal"] is True
    manager.shutdown()  # idempotent


# --------------------------------------------------------------------------- the API


@pytest.fixture
def env_file(app, env: Path) -> Path:  # noqa: ANN001
    path = env / ".env"
    path.write_text("# comment kept\n")
    app.state.env_file = path
    return path


@pytest.fixture
def journal(env: Path) -> Path:
    journal_path, _ = db.init_all(load_config(), root=env)
    return journal_path


def test_the_status_route_reports_cli_and_credential_presence_only(
    auth_client: TestClient, env_file: Path, monkeypatch: pytest.MonkeyPatch,
    happy_cli: Path,
):
    monkeypatch.setenv(signin.CLI_ENV_VAR, str(happy_cli))
    body = auth_client.get("/api/llm/claude/signin").json()
    assert body["state"] == "idle"
    assert body["phase"] == "idle"
    assert body["session"] is None
    assert body["cli"]["present"] is True
    assert body["credential"] == {
        "name": signin.TOKEN_SECRET_NAME, "present": False, "last4": None,
        "updated_at": None,
    }
    assert "sk-ant" not in str(body)


def test_starting_a_sign_in_needs_step_up(auth_client: TestClient, env_file: Path,
                                          monkeypatch: pytest.MonkeyPatch,
                                          happy_cli: Path):
    monkeypatch.setenv(signin.CLI_ENV_VAR, str(happy_cli))
    response = auth_client.post("/api/llm/claude/signin", json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "step_up_required"


def test_a_session_is_required(client: TestClient):
    assert client.get("/api/llm/claude/signin").status_code == 401
    # A write without a session never gets as far as the dependency: the CSRF middleware
    # turns it away first. Either way the code route is unreachable from outside.
    posted = client.post("/api/llm/claude/signin/code", json={"code": "x"})
    assert posted.status_code == 403
    assert posted.json()["error"]["code"] == "csrf_failed"


def test_the_whole_flow_through_the_api_writes_env_at_0600_and_audits_it(
    auth_client: TestClient, token: str, env_file: Path, journal: Path,
    monkeypatch: pytest.MonkeyPatch, tui_cli: Path,
):
    monkeypatch.setenv(signin.CLI_ENV_VAR, str(tui_cli))
    step_up(auth_client, token)
    started = auth_client.post("/api/llm/claude/signin", json={})
    assert started.status_code == 200, started.text
    # The worker runs in its own thread, so by the time the response is serialised the
    # session may already have moved on; what matters is that it started without a reason.
    assert started.json()["session"]["reason"] is None

    bodies: list[dict] = []

    def poll(until: str, *, timeout: float = 25.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = auth_client.get("/api/llm/claude/signin").json()
            bodies.append(body)
            if body["session"]["state"] == until:
                return body
            assert not body["session"]["terminal"], body
            time.sleep(0.05)
        raise AssertionError(f"never reached {until}: {bodies[-1] if bodies else None}")

    waiting = poll("awaiting_code")
    assert waiting["phase"] == "awaiting_code"
    assert waiting["session"]["url"] == FAKE_URL, "the browser needs the whole link"

    handed = auth_client.post("/api/llm/claude/signin/code", json={"code": FAKE_CODE})
    assert handed.status_code == 200, handed.text
    assert FAKE_CODE not in handed.text, "the code must not come back out"

    deadline = time.monotonic() + 25
    body: dict = handed.json()
    while time.monotonic() < deadline:
        body = auth_client.get("/api/llm/claude/signin").json()
        bodies.append(body)
        if body["session"]["terminal"]:
            break
        time.sleep(0.05)

    assert body["session"]["state"] == "done", body
    assert body["credential"]["present"] is True
    assert body["credential"]["last4"] == FAKE_TOKEN[-4:]
    assert {b["session"]["phase"] for b in bodies} >= {"awaiting_code", "done"}

    info = envfile.info(signin.TOKEN_SECRET_NAME, path=env_file)
    assert info.present and info.last4 == FAKE_TOKEN[-4:]
    assert oct(env_file.stat().st_mode)[-3:] == "600"
    assert FAKE_TOKEN in env_file.read_text(), "the one place it is allowed to be"

    # …and nowhere else: not in any response body this test ever saw.
    assert FAKE_TOKEN not in json.dumps(bodies)
    assert FAKE_CODE not in json.dumps(bodies)

    with db.opened(journal, readonly=True) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action LIKE 'claude.signin%' ORDER BY id")]
    assert [r["action"] for r in rows] == [
        "claude.signin.start", "claude.signin.code", "claude.signin"]
    assert FAKE_TOKEN not in str(rows)
    assert FAKE_CODE not in str(rows)
    assert rows[-1]["result"] == "ok"

    # the secrets page agrees, presence-only
    listed = auth_client.get("/api/secrets").json()["secrets"]
    row = next(r for r in listed if r["name"] == signin.TOKEN_SECRET_NAME)
    assert row["present"] is True and row["last4"] == FAKE_TOKEN[-4:]
    assert FAKE_TOKEN not in str(listed)


def test_posting_a_code_when_nothing_is_waiting_is_a_404(auth_client: TestClient,
                                                         env_file: Path):
    response = auth_client.post("/api/llm/claude/signin/code", json={"code": "abc"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_code_that_is_not_one_line_is_refused_without_echoing_it(
    auth_client: TestClient, token: str, env_file: Path, journal: Path,
    monkeypatch: pytest.MonkeyPatch, stubborn_cli: Path,
):
    monkeypatch.setenv(signin.CLI_ENV_VAR, str(stubborn_cli))
    step_up(auth_client, token)
    auth_client.post("/api/llm/claude/signin", json={})
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if auth_client.get("/api/llm/claude/signin").json()["session"]["state"] \
                == "awaiting_code":
            break
        time.sleep(0.05)

    bad = auth_client.post("/api/llm/claude/signin/code",
                           json={"code": "paste me\nand also this"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid"
    assert "and also this" not in bad.text
    auth_client.delete("/api/llm/claude/signin")


def test_the_sse_frames_the_browser_would_receive_carry_no_token(
    auth_client: TestClient, token: str, env_file: Path, journal: Path,
    monkeypatch: pytest.MonkeyPatch, tui_cli: Path, app,  # noqa: ANN001
):
    monkeypatch.setenv(signin.CLI_ENV_VAR, str(tui_cli))
    step_up(auth_client, token)
    auth_client.post("/api/llm/claude/signin", json={})
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        state = auth_client.get("/api/llm/claude/signin").json()["session"]["state"]
        if state == "awaiting_code":
            auth_client.post("/api/llm/claude/signin/code", json={"code": FAKE_CODE})
        if auth_client.get("/api/llm/claude/signin").json()["session"]["terminal"]:
            break
        time.sleep(0.05)

    events = app.state.bus.replay([signin.TOPIC], "0")
    assert events, "the sign-in published nothing; the modal would show a spinner forever"
    phases = {e.payload["phase"] for e in events}
    assert phases >= {"starting", "url_ready", "awaiting_code", "done"}
    assert FAKE_TOKEN not in str([e.payload for e in events])
    assert FAKE_CODE not in str([e.payload for e in events])


def test_starting_twice_over_the_api_is_a_409(auth_client: TestClient, token: str,
                                              env_file: Path, journal: Path,
                                              monkeypatch: pytest.MonkeyPatch,
                                              hanging_cli: Path):
    monkeypatch.setenv(signin.CLI_ENV_VAR, str(hanging_cli))
    step_up(auth_client, token)
    assert auth_client.post("/api/llm/claude/signin", json={}).status_code == 200
    step_up(auth_client, token)
    second = auth_client.post("/api/llm/claude/signin", json={})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"
    assert auth_client.delete("/api/llm/claude/signin").json()["session"]["state"] == \
        "cancelled"


def test_the_app_manager_cleans_itself_up_when_the_console_exits(
    app, env_file: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
):
    """The CLI runs in its own session, so process exit alone would orphan it."""
    from console.services import claude_signin_service

    registered: list[object] = []
    monkeypatch.setattr("atexit.register", lambda fn: registered.append(fn) or fn)
    built = claude_signin_service.manager(app)
    assert built.shutdown in registered
    # …and the app keeps exactly one, so a second route call does not stack handlers.
    assert claude_signin_service.manager(app) is built
    assert len(registered) == 1


def test_cancelling_nothing_over_the_api_is_a_404(auth_client: TestClient, env_file: Path):
    response = auth_client.delete("/api/llm/claude/signin")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
