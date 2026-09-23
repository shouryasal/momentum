"""``python -m console`` — the human entry point.

What a human needs a terminal for, and nothing else:

``serve``         run uvicorn on 127.0.0.1. ``--host`` exists only so that passing
                  anything else fails loudly instead of silently binding elsewhere.
``create-token``  mint (or rotate) the login token into ``~/.config/earn/console-token``
                  at 0600 and store only its salted hash under ``var/state/``. It prints
                  the path, never the token — ``cat`` it yourself.
``rotate-token``  ``create-token --rotate``.
``print-url``     print ``http://127.0.0.1:<port>/``, and say whether a token exists.
``bless-config``  re-sign the protected-config digest after editing a config by hand.
``set-mode``      the break-glass path **down** to TEST (docs/contracts.md §2).

Every subcommand refuses to run under ``EARN_AUTOMATED_RUN=1``: the console is human-only,
and an automated run has no business minting credentials, signing state or starting a
server. ``set-mode`` can only move a sleeve to TEST — there is deliberately no way to
reach a live state from a command line.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from console import __version__, security
from console.settings import BIND_HOST, ConsoleSettings
from ops.lib import audit, config_guard, mode_state, paths, signing

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3


class CliError(Exception):
    """A refusal with an exit code — printed to stderr, never a traceback."""

    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.code = code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m console",
        description="Earn console — local operator UI on 127.0.0.1.",
    )
    parser.add_argument("--version", action="version", version=f"earn-console {__version__}")
    parser.add_argument("--port", type=int, default=None, help="Override the configured port.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the console (127.0.0.1 only).")
    serve.add_argument(
        "--host",
        default=BIND_HOST,
        help=f"Accepted for clarity only; must be {BIND_HOST}.",
    )
    serve.add_argument("--log-level", default="info")

    token = sub.add_parser("create-token", help="Mint the login token (0600) and print its path.")
    token.add_argument("--rotate", action="store_true", help="Replace an existing token.")

    sub.add_parser("rotate-token", help="Alias for `create-token --rotate`.")
    sub.add_parser("print-url", help="Print the console URL.")

    bless = sub.add_parser("bless-config", help="Re-sign the protected-config digest.")
    bless.add_argument("--reason", default=None, help="Why the config changed.")

    mode = sub.add_parser("set-mode", help="Break-glass: put a sleeve back into TEST.")
    mode.add_argument("--sleeve", required=True, choices=list(paths.SLEEVES))
    mode.add_argument(
        "--test", action="store_true", required=True,
        help="The only supported target. Going live is a console transition, never a flag.",
    )
    return parser


def _settings(args: argparse.Namespace) -> ConsoleSettings:
    return ConsoleSettings.from_config(port=args.port)


def _refuse_automated() -> None:
    if security.automated_run():
        raise CliError("the console is human-only; refusing under EARN_AUTOMATED_RUN=1")


def cmd_serve(args: argparse.Namespace) -> int:
    host = str(args.host)
    if host != BIND_HOST:
        raise CliError(
            f"refusing to bind {host}: the console listens on {BIND_HOST} only", EXIT_USAGE
        )
    settings = _settings(args)
    if security.load_record(settings.auth_state_file) is None:
        raise CliError(
            "no login token yet — run `python -m console create-token` first",
            EXIT_REFUSED,
        )
    # imported late: serving is the only path that needs the app or the server
    import uvicorn

    from console.app import create_app

    print(f"earn console on {settings.base_url} (127.0.0.1 only)")
    uvicorn.run(
        create_app(settings), host=BIND_HOST, port=settings.port, log_level=str(args.log_level)
    )
    return EXIT_OK


def cmd_create_token(args: argparse.Namespace) -> int:
    settings = _settings(args)
    rotate = bool(getattr(args, "rotate", False)) or args.command == "rotate-token"
    if security.load_record(settings.auth_state_file) is not None and not rotate:
        raise CliError(
            f"a token already exists ({settings.token_file}); pass --rotate to replace it"
        )
    _token, record = security.create_token(
        token_file=settings.token_file,
        auth_state_file=settings.auth_state_file,
        rotated=rotate,
    )
    print(f"token written to {settings.token_file} (0600)")
    print(f"hash recorded in {settings.auth_state_file} at {record.rotated_at or record.created_at}")
    print(f"read it with: cat {settings.token_file}")
    print(f"console url:  {settings.base_url}")
    return EXIT_OK


def cmd_print_url(args: argparse.Namespace) -> int:
    settings = _settings(args)
    print(settings.base_url)
    has_token = security.load_record(settings.auth_state_file) is not None
    print(f"token: {'present' if has_token else 'missing — run create-token'}")
    return EXIT_OK


def cmd_bless_config(args: argparse.Namespace) -> int:
    """Record and sign the digest of the protected config files."""
    try:
        payload = config_guard.bless(audit.actor_cli(), reason=args.reason)
    except (config_guard.ConfigGuardError, signing.SigningError) as e:
        raise CliError(str(e)) from e
    files = payload.get("files", {})
    print(f"blessed {len(files)} file(s) at {payload.get('blessed_at')}")
    for rel in sorted(files):
        print(f"  {rel}")
    return EXIT_OK


def cmd_set_mode(args: argparse.Namespace) -> int:
    """Put one sleeve back into TEST by rewriting the signed mode file.

    Downward only, and deliberately dumb: it moves the authority file, nothing else. Run
    ``python -m ops.gen_freqtrade_config`` and restart the bot afterwards, or do the whole
    thing properly from the console's Mode page, which also flattens and reconciles.
    """
    sleeve = args.sleeve.lower()
    current = mode_state.load()
    sleeves = {s: current.sleeve(s) for s in paths.SLEEVES}
    sleeves[sleeve] = mode_state.SleeveState()
    try:
        state = mode_state.build(sleeves, set_by=audit.actor_cli())
        path = mode_state.write(state)
    except (mode_state.ModeStateError, signing.SigningError) as e:
        raise CliError(str(e)) from e
    print(f"sleeve {sleeve} is now TEST ({mode_state.describe(state)})")
    print(f"wrote {path}")
    print("next: python -m ops.gen_freqtrade_config && restart the bot")
    return EXIT_OK


COMMANDS = {
    "serve": cmd_serve,
    "create-token": cmd_create_token,
    "rotate-token": cmd_create_token,
    "print-url": cmd_print_url,
    "bless-config": cmd_bless_config,
    "set-mode": cmd_set_mode,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands first
        parser.error(f"unknown command: {args.command}")
        return EXIT_USAGE
    try:
        _refuse_automated()
        return handler(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
