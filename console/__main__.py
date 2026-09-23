"""``python -m console`` → :func:`console.cli.main`.

Kept to one line so there is exactly one implementation of the CLI and exactly one place
that decides the console may not bind anything but 127.0.0.1.
"""

from __future__ import annotations

from console.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
