"""Signing in to Claude from the console, without ever opening a terminal.

``claude setup-token`` is not a line-oriented program. It is a full-screen **Ink TUI**: it
paints with ANSI colour and cursor moves, repaints a spinner every frame, line-wraps at the
terminal width, and refuses to run without a TTY. What it actually does, captured from a
real pty rather than assumed:

1. it prints a verification URL on **claude.com** — ``https://claude.com/cai/oauth/authorize
   ?code=true&client_id=…&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode
   %2Fcallback&…&state=…`` — usually wrapped across two or three lines, and repainted;
2. the operator approves it in a browser, and because ``redirect_uri`` is the
   *platform.claude.com* callback page rather than a loopback port, that page ends by
   **showing an authorization code**;
3. the CLI sits at a prompt waiting for that code to be pasted back into it;
4. only then does it print the long-lived ``sk-ant-oat…`` token.

Step 3 is a human step that cannot be skipped, so the console has to carry it: the modal
asks for the code, and this module writes it to the child's stdin. A flow that stops at
step 1 waits forever, which is exactly the hang this module was rebuilt to fix.

The whole module is built around one rule, the same one the rest of the secret surface
obeys: **the token never leaves this process except into the ``.env`` writer**.

* the reader thread keeps a short rolling window of CLI output *only* to find the URL, the
  code prompt and the token in it, and that window is dropped the moment the session ends;
* every status the UI can see (:meth:`SignInManager.status`, every SSE frame) is built
  from :class:`SignInSession`, which has no field that can hold a token — only a phase, a
  verification URL, a human sentence, and ``last4``;
* the pasted authorization code is held only long enough to write it to the pty, and it is
  masked out of the transcript, because the TUI echoes what it is given;
* the optional transcript file exists for a failed sign-in and is written **redacted**
  through :func:`console.security.redact`, at 0600, and unlinked on every exit path —
  success, failure, cancel and timeout alike;
* the captured token goes straight to :func:`console.services.secrets_service.set_secret`,
  which writes ``.env`` at 0600 and files the audit row.

The state machine is small and total. Each phase is distinct on the wire, because the UI
polls ``GET /api/llm/claude/signin`` once a second and a spinner that cannot say *what* it
is waiting for is how a hang goes unnoticed::

    idle ─start─▶ starting ─url─▶ url_ready ─prompt─▶ awaiting_code ─code─▶ exchanging ─▶ done
                     │               │                    │   ▲                 │
                     │               │                    └───┘ rejected        │
                     └───────────────┴────────────┬─────────────────────────────┘
                                                  ▼
                                    failed:<reason> │ cancelled

:meth:`SignInSession.phase` renders a failure as ``failed:<reason>`` so one poll says both
that it stopped and why. Reasons are a closed set (:data:`REASONS`) so the UI can explain
each one without parsing English.

Only one session may be live at a time — two browser tabs racing two CLI processes at one
``~/.claude`` is not a thing an operator can reason about — and a live session is replaced
only after it is cancelled.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from console import security

__all__ = [
    "CLI_ENV_VAR",
    "MAX_CODE_ATTEMPTS",
    "REASONS",
    "STATES",
    "TERMINAL_STATES",
    "TOKEN_SECRET_NAME",
    "SignInError",
    "SignInManager",
    "SignInSession",
    "clean_code",
    "find_token",
    "find_url",
    "manager",
    "rejected_code",
    "strip_ansi",
    "wants_code",
]

#: The secret the flow ends up writing. The one name in the catalogue for a Claude Max
#: subscription credential.
TOKEN_SECRET_NAME = "CLAUDE_CODE_OAUTH_TOKEN"

#: Override the CLI for a test (or an unusual install). Absolute path or a name on PATH.
CLI_ENV_VAR = "EARN_CLAUDE_CLI"
DEFAULT_CLI = "claude"
SETUP_ARGS: tuple[str, ...] = ("setup-token",)

#: The SSE topic every status change is published on (``console.sse.TOPICS``).
TOPIC = "claude_auth"

STATES: tuple[str, ...] = (
    "idle",           # nothing has ever run
    "starting",       # the CLI is spawning and has not printed a link yet
    "url_ready",      # the link is parsed and the operator has to approve it
    "awaiting_code",  # the CLI is at its prompt, waiting for the browser's code
    "exchanging",     # the code has been written to the CLI; it is redeeming it
    "done", "failed", "cancelled",
)
TERMINAL_STATES: frozenset[str] = frozenset({"done", "failed", "cancelled"})

REASONS: tuple[str, ...] = (
    "cli_missing",     # no `claude` on PATH
    "unsupported",     # no pseudo-terminal on this platform
    "no_link",         # the CLI never printed a verification URL
    "not_approved",    # the operator never got as far as the code prompt
    "no_code",         # the CLI asked for the code and nobody pasted one
    "bad_code",        # the code was pasted and the CLI would not take it
    "cli_failed",      # the CLI exited non-zero
    "no_token",        # it exited 0 but printed nothing that looks like a token
    "cancelled",       # a human pressed cancel
    "store_failed",    # the .env writer refused
)

#: How long the CLI gets to print its verification URL before we give up on it.
LINK_TIMEOUT_S = 90.0
#: How long the operator gets in the browser — approving, then pasting the code back.
#: `claude setup-token` itself eventually times out; this bound is ours, so a forgotten tab
#: cannot hold a pty forever. It is refreshed when the code prompt appears, because the
#: approval and the paste are two separate human waits.
APPROVAL_TIMEOUT_S = 600.0
#: How long the CLI gets to turn an accepted code into a token.
EXCHANGE_TIMEOUT_S = 120.0
#: Grace after the CLI prints the token for it to exit on its own.
EXIT_GRACE_S = 10.0
#: How many codes an operator may paste before we stop and say so. Three is the usual
#: "typo, typo, wrong browser account" budget; past that the code is not the problem.
MAX_CODE_ATTEMPTS = 3
#: A repainting TUI redraws its prompt for a frame or two after the code goes in. Only a
#: prompt seen *after* this grace counts as the CLI asking again.
REPROMPT_GRACE_S = 2.5
#: How much CLI output is kept while parsing. A rolling window, never a log.
SCAN_WINDOW_CHARS = 16_000
#: The pty we hand the CLI. Wide on purpose: Ink wraps at the terminal width, and a URL
#: that never wraps is a URL that cannot be reassembled wrongly.
PTY_COLUMNS = 200
PTY_ROWS = 50

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_EOL = re.compile(r"\r+\n")

#: Registrable domains a verification link may sit under, with their subdomains. The real
#: CLI uses ``claude.com`` (``platform.claude.com`` for the callback); older builds used
#: ``claude.ai`` and ``console.anthropic.com``. Anything else in the output is not our link,
#: and following a URL the CLI did not mean is how an operator gets phished by a log line.
LINK_DOMAINS: tuple[str, ...] = ("claude.com", "claude.ai", "anthropic.com")
_URL = re.compile(r"https://([A-Za-z0-9.\-]+)(/[^\s'\"<>()\\\]]*)?")

#: The credential shape `claude setup-token` prints. Deliberately narrow: it must not
#: match the `sk-ant-api03-…` of a metered key pasted into the same terminal.
_TOKEN = re.compile(r"\bsk-ant-oat[0-9A-Za-z_\-]{16,}")

#: "Paste code here if prompted >" and the phrasings around it. Written not to fire on the
#: ``?code=true`` / ``&code_challenge=`` inside the verification URL itself.
_CODE_PROMPT = re.compile(
    r"paste\s+(?:the\s+|your\s+)?(?:authorization\s+|auth\s+)?code"
    r"|enter\s+(?:the\s+|your\s+)?(?:authorization\s+|auth\s+)?code"
    r"|authorization\s+code\s*[:>]"
    r"|^\s*(?:auth(?:orization)?\s+)?code\s*[>:]\s*$",
    re.IGNORECASE | re.MULTILINE,
)

#: The CLI's own way of saying the code was no good.
_CODE_REJECTED = re.compile(
    r"invalid\s+(?:authorization\s+)?code"
    r"|incorrect\s+code"
    r"|code\s+(?:is\s+)?(?:invalid|incorrect|expired|not\s+valid)"
    r"|(?:authentication|authorization|login)\s+failed"
    r"|failed\s+to\s+(?:exchange|authenticate|authorize)",
    re.IGNORECASE,
)

#: Characters that can appear inside a URL and are legal in a pasted code.
_CODE_CHARS = re.compile(r"^[!-~]{1,1024}$")

# A wrapped URL's continuation line: URL-legal characters running from column zero.
_URL_TAIL = re.compile(r"https://\S*$")
_CONT_TOKEN = re.compile(r"[A-Za-z0-9%._~:/?#\[\]@!$&'()*+,;=\-]+")
_STRUCTURAL = frozenset("%&=?/:-_+.#")
#: A continuation that carries no URL punctuation at all has to be long enough that prose
#: is implausible — a mid-token wrap of a base64 ``state`` looks like nothing else.
_CONT_MIN_OPAQUE = 12


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_ansi(text: str) -> str:
    """Drop terminal control sequences so the parsers see plain characters.

    ``\\r\\r\\n`` is one line break, not two: the CLI writes ``\\r\\n`` and the pty's
    ``ONLCR`` turns the ``\\n`` into another ``\\r\\n``. Collapsing that first is what keeps
    a wrapped URL's halves adjacent instead of a phantom blank line apart. A lone ``\\r``
    is a repaint, and becomes a break.
    """
    return _ANSI.sub("", _EOL.sub("\n", text).replace("\r", "\n"))


def _unwrap(text: str) -> str:
    """Re-join lines a terminal split mid-URL.

    Ink wraps at the terminal width, so the verification URL arrives as two or three lines
    with the break anywhere — after ``&``, inside ``code_challenge``, halfway through the
    ``state``. Joining blindly would glue the next sentence onto the URL (the prompt line
    sits right under it), so a line counts as a continuation only when the break looks like
    a break:

    * the last non-blank line ends *in* a URL, as a wrap leaves it (blank lines in between
      are the repaint's, not the URL's);
    * this line is URL characters and nothing else — a wrapped URL never shares its line
      with prose, while "Paste code here if prompted >" does;
    * and it carries URL punctuation, continues from punctuation, or is a long opaque run
      (a wrap through the middle of a base64 ``state`` looks like nothing else).
    """
    out: list[str] = []
    tail = -1  # index in `out` of the last non-blank line
    for raw in text.split("\n"):
        line = raw.rstrip()
        if line and tail >= 0 and _URL_TAIL.search(out[tail]) \
                and _CONT_TOKEN.fullmatch(line) \
                and (any(c in _STRUCTURAL for c in line)
                     or out[tail][-1] in _STRUCTURAL
                     or len(line) >= _CONT_MIN_OPAQUE):
            out[tail] += line
            continue
        out.append(line)
        if line:
            tail = len(out) - 1
    return "\n".join(out)


def _known_host(host: str) -> bool:
    """``claude.com``, ``claude.ai``, ``anthropic.com`` and their subdomains."""
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in LINK_DOMAINS)


def _url_rank(url: str | None) -> tuple[int, int]:
    """How complete a candidate looks: has a ``state``, then how much of it we have.

    The CLI repaints, so the same link arrives over and over — sometimes truncated by a
    narrow terminal, sometimes whole. Ranking rather than taking the first match is what
    makes a fragment lose to the URL it is a fragment of.
    """
    if not url:
        return (-1, -1)
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - urlsplit is forgiving
        return (-1, -1)
    if not parts.netloc:
        return (-1, -1)
    state = parse_qs(parts.query).get("state") or []
    return (1 if any(s.strip() for s in state) else 0, len(url))


def _candidates(text: str) -> Iterator[str]:
    for match in _URL.finditer(text):
        if not _known_host(match.group(1)):
            continue
        url = match.group(0).rstrip(".,);:'\"")
        if url:
            yield url


def find_url(text: str) -> str | None:
    """The verification link in CLI output, or ``None``.

    Only ``https`` and only Anthropic's own domains; a trailing ``.``/``,``/``)`` is prose
    punctuation, not part of the URL. Both the text as printed and the text with wrapped
    lines re-joined are searched, and the most complete candidate wins — the first match in
    a repainting TUI is usually half a link.
    """
    plain = strip_ansi(text)
    best: str | None = None
    best_rank = (-1, -1)
    seen: set[str] = set()
    for url in (*_candidates(plain), *_candidates(_unwrap(plain))):
        if url in seen:
            continue
        seen.add(url)
        rank = _url_rank(url)
        if rank > best_rank:
            best, best_rank = url, rank
    return best


def find_token(text: str) -> str | None:
    """The ``sk-ant-oat…`` credential in CLI output, or ``None``."""
    match = _TOKEN.search(strip_ansi(text))
    return match.group(0) if match else None


def wants_code(text: str) -> bool:
    """Whether the CLI is sitting at its "paste the code" prompt."""
    return bool(_CODE_PROMPT.search(strip_ansi(text)))


def rejected_code(text: str) -> bool:
    """Whether the CLI said the code it was given was no good."""
    return bool(_CODE_REJECTED.search(strip_ansi(text)))


def clean_code(code: str) -> str:
    """The pasted code, or :class:`SignInError`. Never widened to "whatever they typed".

    Whatever this returns is written to a child process's stdin, so it is one line of
    printable characters and nothing else: no newline to inject a second answer with, no
    control character to drive the TUI with.
    """
    cleaned = (code or "").strip()
    if not cleaned:
        raise SignInError("paste the code the browser showed you", reason="invalid")
    if not _CODE_CHARS.match(cleaned):
        raise SignInError(
            "that does not look like the code from the browser — it is one line of "
            "letters, digits and punctuation, with no spaces",
            reason="invalid")
    return cleaned


class SignInError(Exception):
    """A refused sign-in request. The message never contains a token or a code."""

    def __init__(self, message: str, *, reason: str = "invalid") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class SignInSession:
    """Everything the UI is allowed to know about one sign-in attempt.

    There is deliberately no field a token could be assigned to, and none a pasted code
    could be assigned to either. ``last4`` is the only part of the credential that ever
    appears, and it is derived after the write.
    """

    id: str
    state: str = "starting"
    message: str = ""
    url: str | None = None
    reason: str | None = None
    actor: str = ""
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    finished_at: str | None = None
    last4: str | None = None
    #: Wall-clock deadline for the current wait, so the UI can count down.
    deadline_at: str | None = None
    exit_code: int | None = None
    #: How many codes have been handed to the CLI, and how many are left.
    attempts: int = 0
    attempts_left: int = MAX_CODE_ATTEMPTS

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def phase(self) -> str:
        """The phase as one string — ``failed:no_code`` rather than ``failed``.

        The modal polls once a second and this is what it renders, so a stuck flow names
        the step it is stuck on instead of spinning.
        """
        if self.state in {"failed", "cancelled"} and self.reason:
            return f"{self.state}:{self.reason}"
        return self.state

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "phase": self.phase,
            "message": self.message,
            "url": self.url,
            "reason": self.reason,
            "actor": self.actor,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "last4": self.last4,
            "deadline_at": self.deadline_at,
            "exit_code": self.exit_code,
            "attempts": self.attempts,
            "attempts_left": self.attempts_left,
            "terminal": self.terminal,
        }


def cli_path(environ: Mapping[str, str] | None = None) -> str | None:
    """Where ``claude`` is, honouring :data:`CLI_ENV_VAR`. ``None`` when not installed."""
    env = environ if environ is not None else os.environ
    configured = (env.get(CLI_ENV_VAR) or "").strip()
    if configured:
        p = Path(configured)
        if p.is_absolute() or os.sep in configured:
            return str(p) if p.exists() and os.access(p, os.X_OK) else None
        return shutil.which(configured)
    found = shutil.which(DEFAULT_CLI)
    if found:
        return found
    # The documented install location, which a non-login shell's PATH often misses.
    fallback = Path.home() / ".local" / "bin" / DEFAULT_CLI
    return str(fallback) if fallback.exists() and os.access(fallback, os.X_OK) else None


def cli_version(path: str | None, *, timeout_s: float = 5.0) -> str | None:
    """``claude --version``, or ``None``. Never raises, never blocks for long."""
    if not path:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - the path came from shutil.which/our own env
            [path, "--version"], capture_output=True, text=True, timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (out.stdout or out.stderr or "").strip().splitlines()
    return line[0][:120] if line else None


class _Transcript:
    """A redacted, 0600 scratch file for one session; unlinked on every exit path."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._fh: Any = None
        self._masks: list[str] = []
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("w", encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - a read-only state root is not fatal here
            self._fh, self.path = None, None

    def mask(self, value: str) -> None:
        """Never write this string. The TUI echoes the pasted code straight back."""
        if value and value not in self._masks:
            self._masks.append(value)

    def write(self, chunk: str) -> None:
        if self._fh is None:
            return
        text = security.redact(strip_ansi(chunk))
        for value in self._masks:
            text = text.replace(value, "«code»")
        try:
            self._fh.write(text)
            self._fh.flush()
        except (OSError, ValueError):  # pragma: no cover
            pass

    def discard(self) -> None:
        """Close and delete. Idempotent — cleanup runs from several places."""
        self._masks.clear()
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:  # pragma: no cover
                pass
            self._fh = None
        if self.path is not None:
            try:
                self.path.unlink()
            except OSError:  # pragma: no cover
                pass
            self.path = None


class SignInManager:
    """One live ``claude setup-token`` at a time, supervised in a thread.

    Every collaborator is injected, so the tests drive the real pty path against a fake
    CLI script and never reach Anthropic:

    ``store``      what to do with the captured token — the ``.env`` writer by default.
    ``bus``        the SSE bus; ``publish(topic, payload)`` is the only method used.
    ``audit``      ``(action, target, result, detail) -> None``.
    ``credential`` reports presence/last4 of the stored token for :meth:`status`.
    """

    def __init__(
        self,
        *,
        store: Callable[[str, str], dict[str, Any]],
        bus: Any = None,
        audit: Callable[..., None] | None = None,
        credential: Callable[[], dict[str, Any]] | None = None,
        transcript_dir: Path | None = None,
        environ: Mapping[str, str] | None = None,
        link_timeout_s: float = LINK_TIMEOUT_S,
        approval_timeout_s: float = APPROVAL_TIMEOUT_S,
        exchange_timeout_s: float = EXCHANGE_TIMEOUT_S,
        reprompt_grace_s: float = REPROMPT_GRACE_S,
    ) -> None:
        self._store = store
        self._bus = bus
        self._audit = audit or (lambda **_kwargs: None)
        self._credential = credential or (lambda: {"present": False, "last4": None})
        self._transcript_dir = transcript_dir
        self._environ = dict(environ if environ is not None else os.environ)
        self.link_timeout_s = link_timeout_s
        self.approval_timeout_s = approval_timeout_s
        self.exchange_timeout_s = exchange_timeout_s
        self.reprompt_grace_s = reprompt_grace_s

        self._lock = threading.RLock()
        self._session: SignInSession | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._transcript: _Transcript | None = None
        self._pending_code: str | None = None
        self._version_cache: tuple[str | None, str | None] | None = None

    # -- public surface --------------------------------------------------------

    def start(self, *, actor: str, replace: bool = False) -> dict[str, Any]:
        """Launch the CLI. Raises :class:`SignInError` rather than starting a second one."""
        with self._lock:
            live = self._session
            if live is not None and not live.terminal:
                raise SignInError(
                    "a sign-in is already running; cancel it before starting another",
                    reason="conflict")
            existing = self._credential()
            if existing.get("present") and not replace:
                raise SignInError(
                    f"{TOKEN_SECRET_NAME} is already set (…{existing.get('last4')}); "
                    "confirm replace to sign in again",
                    reason="conflict")
            path = cli_path(self._environ)
            session = SignInSession(id=os.urandom(6).hex(), actor=actor,
                                    state="starting",
                                    message="starting `claude setup-token`…")
            self._session = session
            self._cancel = threading.Event()
            self._pending_code = None
            if path is None:
                self._finish(session, "failed", "cli_missing",
                             "The Claude CLI is not installed on this host. Install it in "
                             "WSL with `npm i -g @anthropic-ai/claude-code`, then try again.")
                return session.as_dict()
            if not _pty_available():
                self._finish(session, "failed", "unsupported",
                             "`claude setup-token` needs a pseudo-terminal, which this "
                             "platform does not provide. Run it in WSL instead.")
                return session.as_dict()
            self._audit(action="claude.signin.start", target=session.id, result="ok",
                        detail={"replace": bool(replace)})
            self._emit(session)
            thread = threading.Thread(target=self._run, args=(session, path),
                                      name=f"claude-signin-{session.id}", daemon=True)
            self._thread = thread
        thread.start()
        return session.as_dict()

    def submit_code(self, code: str, *, actor: str = "") -> dict[str, Any]:
        """Hand the CLI the authorization code the browser showed.

        The code is validated here, queued for the worker (which owns the pty), and then
        forgotten: it is not stored on the session, not published, not audited as a value.
        """
        cleaned = clean_code(code)
        with self._lock:
            session = self._session
            if session is None or session.terminal:
                raise SignInError("no sign-in is waiting for a code", reason="not_found")
            if session.state != "awaiting_code":
                raise SignInError(
                    f"the sign-in is not waiting for a code (it is {session.phase})",
                    reason="conflict")
            if self._pending_code is not None:  # pragma: no cover - a double-click
                raise SignInError("that code is still being checked", reason="conflict")
            self._pending_code = cleaned
            session.attempts += 1
            session.attempts_left = max(0, MAX_CODE_ATTEMPTS - session.attempts)
            attempt = session.attempts
            transcript = self._transcript
        if transcript is not None:
            transcript.mask(cleaned)
        cleaned = ""
        self._audit(action="claude.signin.code", target=session.id, result="ok",
                    detail={"attempt": attempt, "actor": actor or None})
        self._update(session, state="exchanging",
                     message="Checking the code with Claude…",
                     deadline_in_s=self.exchange_timeout_s)
        return self.status()

    def cancel(self, *, actor: str = "") -> dict[str, Any]:
        """Stop a running sign-in. Idempotent; a finished session is returned unchanged."""
        with self._lock:
            session = self._session
            if session is None:
                raise SignInError("no sign-in is running", reason="not_found")
            if session.terminal:
                return session.as_dict()
            self._cancel.set()
            self._pending_code = None
            self._kill_process()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        with self._lock:
            session = self._session
            if session is not None and not session.terminal:
                # The worker did not get there (already exiting, or wedged): close it out
                # here so no session can be left mid-flight.
                self._finish(session, "cancelled", "cancelled", "Sign-in cancelled.")
            self._audit(action="claude.signin.cancel",
                        target=session.id if session else None, result="ok",
                        detail={"actor": actor} if actor else None)
            return session.as_dict() if session else {}

    def status(self) -> dict[str, Any]:
        """The full picture the modal renders: session, CLI presence, credential."""
        with self._lock:
            session = self._session
        path = cli_path(self._environ)
        if self._version_cache is None or self._version_cache[0] != path:
            self._version_cache = (path, cli_version(path))
        return {
            "session": session.as_dict() if session else None,
            "state": session.state if session else "idle",
            "phase": session.phase if session else "idle",
            "cli": {
                "present": path is not None,
                "path": path,
                "version": self._version_cache[1],
                "pty": _pty_available(),
            },
            "credential": {"name": TOKEN_SECRET_NAME, **self._credential()},
        }

    def shutdown(self) -> None:
        """Stop a live session and clean up. Called from the app's lifespan."""
        try:
            self.cancel(actor="shutdown")
        except SignInError:
            pass

    # -- the worker ------------------------------------------------------------

    def _run(self, session: SignInSession, path: str) -> None:  # noqa: C901, PLR0912, PLR0915
        """Drive one CLI process from spawn to a terminal state. Never raises out."""
        transcript = _Transcript(self._transcript_path(session))
        with self._lock:
            self._transcript = transcript
        buffer = ""       # everything recent, for the URL and the token
        since_code = ""   # only what arrived after the last code went in
        submitted_at: float | None = None
        deadline = time.monotonic() + self.link_timeout_s
        proc = master = None
        try:
            proc, master = _spawn(path, self._environ)
            with self._lock:
                self._proc = proc
            self._update(session, state="starting",
                         message="`claude setup-token` is starting…",
                         deadline_in_s=self.link_timeout_s)
            while True:
                if self._cancel.is_set():
                    self._finish(session, "cancelled", "cancelled", "Sign-in cancelled.")
                    return

                code = self._take_pending_code()
                if code:
                    _write_line(master, code)
                    code = ""
                    submitted_at = time.monotonic()
                    since_code = ""
                    deadline = time.monotonic() + self.exchange_timeout_s

                chunk = _read(master, timeout_s=0.5)
                if chunk:
                    transcript.write(chunk)
                    buffer = (buffer + chunk)[-SCAN_WINDOW_CHARS:]
                    if submitted_at is not None:
                        since_code = (since_code + chunk)[-SCAN_WINDOW_CHARS:]
                    token = find_token(buffer)
                    if token:
                        self._capture(session, token, proc)
                        return

                    url = find_url(buffer)
                    if url and _url_rank(url) > _url_rank(session.url):
                        # A repaint can carry a more complete link than the first frame did;
                        # the operator gets the better one rather than a truncated fragment.
                        first = session.url is None
                        session.url = url
                        if first:
                            deadline = time.monotonic() + self.approval_timeout_s
                            self._update(
                                session, state="url_ready",
                                message="Open the link in your Windows browser and "
                                        "approve it. The CLI is waiting in WSL.",
                                deadline_in_s=self.approval_timeout_s)
                        else:
                            self._update(session)

                    if session.state == "url_ready" and wants_code(buffer):
                        deadline = time.monotonic() + self.approval_timeout_s
                        self._update(
                            session, state="awaiting_code",
                            message="Approve the link, then paste the code the browser "
                                    "shows you into the box below.",
                            deadline_in_s=self.approval_timeout_s)

                # Checked every turn, not only when output arrives: a CLI that redraws its
                # prompt and then says nothing at all is precisely the silent hang.
                if session.state == "exchanging" and submitted_at is not None:
                    # A repaint can echo the prompt for a frame or two after the code goes
                    # in, so only the *current* screen counts, and only after the grace.
                    reprompt = (time.monotonic() - submitted_at > self.reprompt_grace_s
                                and wants_code(since_code[-400:]))
                    if rejected_code(since_code) or reprompt:
                        submitted_at, since_code = None, ""
                        if session.attempts_left <= 0:
                            self._finish(
                                session, "failed", "bad_code",
                                "The CLI would not accept that code. Nothing was written. "
                                "Start again and copy the code from the browser page in "
                                "one go.")
                            return
                        deadline = time.monotonic() + self.approval_timeout_s
                        self._update(
                            session, state="awaiting_code",
                            message="That code was not accepted. Copy it again from the "
                                    f"browser page — {session.attempts_left} attempt(s) "
                                    "left.",
                            deadline_in_s=self.approval_timeout_s)

                exited = proc.poll()
                if exited is not None and not chunk:
                    if self._cancel.is_set():
                        # We are the ones who killed it. Reading its exit code back as
                        # `cli_failed` would tell the operator their own Cancel button was
                        # a crash; cancel is checked here, not only at the top of the
                        # loop, because the kill and the EOF arrive together.
                        self._finish(session, "cancelled", "cancelled",
                                     "Sign-in cancelled.")
                        return
                    # Drain whatever the pty still holds before judging the exit.
                    tail = _drain(master)
                    if tail:
                        transcript.write(tail)
                        buffer = (buffer + tail)[-SCAN_WINDOW_CHARS:]
                    token = find_token(buffer)
                    if token:
                        self._capture(session, token, proc)
                        return
                    session.exit_code = exited
                    self._finish(session, "failed", *self._ending(session, buffer, exited))
                    return

                if time.monotonic() > deadline:
                    if self._cancel.is_set():
                        self._finish(session, "cancelled", "cancelled",
                                     "Sign-in cancelled.")
                        return
                    self._finish(session, "failed", *self._timeout(session))
                    return
        except Exception as e:  # noqa: BLE001 - a spawn failure is a failed sign-in
            self._finish(session, "failed", "cli_failed",
                         f"could not run the Claude CLI: {type(e).__name__}")
        finally:
            buffer = since_code = ""
            self._cleanup(master, proc, transcript)

    def _ending(self, session: SignInSession, buffer: str,
                exited: int) -> tuple[str, str]:
        """Why a CLI that has exited did not produce a token."""
        if exited != 0:
            if session.attempts and rejected_code(buffer):
                return ("bad_code",
                        "The CLI rejected the code and stopped. Nothing was written — "
                        "start again and copy the code from the browser page in one go.")
            return ("cli_failed",
                    f"`claude setup-token` exited with code {exited}. Run it once in a "
                    "WSL terminal to see what it is asking for.")
        return ("no_token",
                "The CLI finished without printing a token. Nothing was written.")

    def _timeout(self, session: SignInSession) -> tuple[str, str]:
        """Which wait ran out, named so the operator knows which step to redo."""
        if session.state == "starting":
            return ("no_link",
                    "The CLI did not print a verification link in "
                    f"{int(self.link_timeout_s)}s. Nothing was written.")
        if session.state == "awaiting_code":
            return ("no_code",
                    "The CLI asked for the code from the browser and never got one. "
                    "Nothing was written — start again with the browser page open.")
        if session.state == "exchanging":
            return ("no_token",
                    "The CLI took the code but never printed a token. Nothing was "
                    "written.")
        return ("not_approved",
                "Nobody approved the link in time. Nothing was written — start again "
                "when the browser is ready.")

    def _take_pending_code(self) -> str:
        with self._lock:
            code, self._pending_code = self._pending_code, None
        return code or ""

    def _capture(self, session: SignInSession, token: str,
                 proc: subprocess.Popen[bytes] | None) -> None:
        """Store the token and close the session. ``token`` dies with this frame."""
        self._update(session, state="exchanging", message="Storing the credential…")
        if proc is not None:
            _wait_briefly(proc, EXIT_GRACE_S)
            session.exit_code = proc.poll()
        try:
            written = self._store(TOKEN_SECRET_NAME, token)
        except Exception as e:  # noqa: BLE001 - the writer's refusal is the failure
            self._audit(action="claude.signin", target=session.id, result="failed",
                        detail={"reason": "store_failed"})
            self._finish(session, "failed", "store_failed",
                         f"the credential could not be written: {security.redact(str(e))}")
            return
        session.last4 = written.get("last4")
        self._audit(action="claude.signin", target=TOKEN_SECRET_NAME, result="ok",
                    detail={"session": session.id, "last4": session.last4,
                            "source": "setup-token"})
        self._finish(session, "done", None,
                     f"Signed in. {TOKEN_SECRET_NAME} is set (…{session.last4}).")

    # -- state transitions -----------------------------------------------------

    def _update(self, session: SignInSession, *, state: str | None = None,
                message: str | None = None, deadline_in_s: float | None = None) -> None:
        with self._lock:
            if session.terminal:
                return
            if state is not None:
                session.state = state
            if message is not None:
                session.message = message
            if deadline_in_s is not None:
                session.deadline_at = datetime.fromtimestamp(
                    time.time() + deadline_in_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            session.updated_at = _now()
        self._emit(session)

    def _finish(self, session: SignInSession, state: str, reason: str | None,
                message: str) -> None:
        with self._lock:
            if session.terminal:
                return
            session.state = state
            session.reason = reason
            session.message = message
            session.deadline_at = None
            session.updated_at = session.finished_at = _now()
            self._pending_code = None
            if state != "done":
                self._audit(action="claude.signin", target=session.id,
                            result="denied" if state == "cancelled" else "failed",
                            detail={"reason": reason})
        self._emit(session)

    def _emit(self, session: SignInSession) -> None:
        """Publish one status frame. The payload is the session, which holds no token."""
        if self._bus is None:
            return
        try:
            self._bus.publish(TOPIC, session.as_dict())
        except Exception:  # noqa: BLE001 - a full bus must not break the sign-in
            pass

    # -- process handling ------------------------------------------------------

    def _transcript_path(self, session: SignInSession) -> Path | None:
        if self._transcript_dir is None:
            return None
        return Path(self._transcript_dir) / f"claude-signin-{session.id}.log"

    def _kill_process(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except (OSError, AttributeError, ProcessLookupError):
            try:
                proc.terminate()
            except OSError:  # pragma: no cover
                pass

    def _cleanup(self, master: int | None, proc: subprocess.Popen[bytes] | None,
                 transcript: _Transcript) -> None:
        """Close the pty, reap the child, delete the transcript. Runs on every path."""
        if proc is not None and proc.poll() is None:
            with self._lock:
                self._kill_process()
            if not _wait_briefly(proc, 3.0):
                try:
                    os.killpg(os.getpgid(proc.pid), 9)
                except (OSError, AttributeError, ProcessLookupError):  # pragma: no cover
                    try:
                        proc.kill()
                    except OSError:
                        pass
                _wait_briefly(proc, 2.0)
        if master is not None:
            try:
                os.close(master)
            except OSError:  # pragma: no cover
                pass
        transcript.discard()
        with self._lock:
            self._proc = None
            self._transcript = None
            self._pending_code = None


def _pty_available() -> bool:
    try:
        import pty  # noqa: F401,PLC0415
    except Exception:  # noqa: BLE001 - Windows has no pty module
        return False
    return hasattr(os, "openpty") and hasattr(os, "setsid")


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Tell the pty how wide it is, so Ink has no reason to wrap the URL."""
    try:
        import fcntl  # noqa: PLC0415
        import struct  # noqa: PLC0415
        import termios  # noqa: PLC0415

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:  # noqa: BLE001 - a narrower terminal only means we reassemble
        pass


def _spawn(path: str, environ: Mapping[str, str]) -> tuple[subprocess.Popen[bytes], int]:
    """``claude setup-token`` under a wide pseudo-terminal, in its own process group."""
    import pty  # noqa: PLC0415

    master, slave = pty.openpty()
    _set_winsize(slave, PTY_ROWS, PTY_COLUMNS)
    _set_winsize(master, PTY_ROWS, PTY_COLUMNS)
    env = {k: v for k, v in environ.items() if k != CLI_ENV_VAR}
    # The CLI must not find a credential in its own environment: setup-token is how we
    # obtain one, and a stale value in the console's process would change what it does.
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(name, None)
    env.setdefault("TERM", "xterm-256color")
    # Belt and braces with the winsize: Ink reads COLUMNS when it is set.
    env["COLUMNS"], env["LINES"] = str(PTY_COLUMNS), str(PTY_ROWS)
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell, path from which()
            [path, *SETUP_ARGS], stdin=slave, stdout=slave, stderr=slave, env=env,
            close_fds=True, start_new_session=True,
        )
    finally:
        os.close(slave)
    return proc, master


def _read(master: int, *, timeout_s: float) -> str:
    """One bounded read off the pty. ``""`` on timeout or EOF; never raises."""
    import select  # noqa: PLC0415

    try:
        ready, _, _ = select.select([master], [], [], timeout_s)
    except (OSError, ValueError):
        return ""
    if not ready:
        return ""
    try:
        data = os.read(master, 4096)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _write_line(master: int | None, line: str) -> None:
    """Type one line into the child, as a human would. Never raises, never logs it."""
    if master is None:  # pragma: no cover - the worker always has one here
        return
    payload = (line + "\n").encode("utf-8", errors="ignore")
    while payload:
        try:
            written = os.write(master, payload)
        except OSError:  # pragma: no cover - the child went away mid-write
            return
        if written <= 0:  # pragma: no cover
            return
        payload = payload[written:]


def _drain(master: int | None, *, limit: int = 64) -> str:
    """Whatever is still buffered after the child exited."""
    if master is None:
        return ""
    out: list[str] = []
    for _ in range(limit):
        chunk = _read(master, timeout_s=0.05)
        if not chunk:
            break
        out.append(chunk)
    return "".join(out)


def _wait_briefly(proc: subprocess.Popen[bytes], timeout_s: float) -> bool:
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False
    except OSError:  # pragma: no cover
        return True
    return True


# --------------------------------------------------------------------------- app wiring

_APP_ATTR = "claude_signin"


def manager(app: Any, *, cfg: Any = None) -> SignInManager:
    """The app's one :class:`SignInManager`, built on first use.

    Built here rather than in ``create_app`` so the seam stays inside this feature: the
    router asks for it, the tests replace ``app.state.claude_signin`` with their own.
    """
    existing = getattr(app.state, _APP_ATTR, None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]

    from console.deps import audit_event, journal_db
    from console.services import secrets_service
    from ops.lib import envfile

    env_file = getattr(app.state, "env_file", None)

    def store(name: str, value: str) -> dict[str, Any]:
        journal = journal_db(cfg)
        return secrets_service.set_secret(
            name, value, actor="human:console:signin", path=env_file,
            journal=journal if journal.exists() else None,
        )

    def credential() -> dict[str, Any]:
        info = envfile.info(TOKEN_SECRET_NAME,
                            path=env_file or secrets_service.env_path())
        return {"present": info.present, "last4": info.last4,
                "updated_at": info.updated_at}

    def audit(**kwargs: Any) -> None:
        try:
            audit_event(actor="human:console:signin", cfg=cfg, **kwargs)
        except Exception:  # noqa: BLE001 - auditing never vetoes the action
            pass

    from ops.lib import paths

    built = SignInManager(
        store=store, bus=getattr(app.state, "bus", None), audit=audit,
        credential=credential, transcript_dir=paths.state_root() / "runtime",
    )
    # The CLI runs in its own session (`start_new_session=True`), so killing the console
    # would otherwise leave an orphaned `claude setup-token` holding a pty and a transcript
    # until its own timeout. `console.app.shutdown` cannot do this — it is passed a custom
    # lifespan, and adding the hook there would mean editing a module this feature does not
    # own — so the manager cleans itself up on interpreter exit. `shutdown` is idempotent
    # and a no-op when nothing is running.
    atexit.register(built.shutdown)
    setattr(app.state, _APP_ATTR, built)
    return built
