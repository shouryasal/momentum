"""``docker compose run`` REPLACES the service command — so every caller passes --config.

ops/docker-compose.yml is the only place ``--config`` appears for the bots: it is part of
the service ``command:``. ``docker compose run <service> download-data ...`` throws that
command away and keeps only the image entrypoint, so freqtrade received no config at all,
fell back to its own discovery, found nothing and died validating an empty configuration
("'enabled' is a required property"). ops/bootstrap_data.sh did exactly that, which is why
the week-1 data gate could not pass; ops/refresh_backtest_data.sh had the same defect.

The shape of the bug is textual — a missing flag on a command line we never execute in a
unit test — so it is asserted textually here, and the *consequence* (freqtrade rejecting a
config-less utility run) is asserted against the real validator in
tests/test_ops/test_freqtrade_config_schema.py.
"""

from __future__ import annotations

import re

import pytest

from ops.config import REPO_ROOT

OPS = REPO_ROOT / "ops"

#: Every script that starts a freqtrade command through ``docker compose run``.
SCRIPTS = ("bootstrap_data.sh", "backtest.sh", "refresh_backtest_data.sh")

#: The in-container path of the config the compose service would have passed.
CONTAINER_CONFIG = "/freqtrade/earn-config/freqtrade-a.json"

#: ``compose run [flags] SERVICE COMMAND ...`` — capture the freqtrade sub-command and the
#: rest of the invocation, which bash continues across backslash-newlines.
_RUN = re.compile(
    r"docker\s+compose\s+run\s+(?:--\S+\s+)*freqtrade-[ab]\s+(?P<cmd>[\w-]+)"
    r"(?P<rest>(?:\\\n|[^\n])*)"
)


def _invocations(name: str) -> list[re.Match[str]]:
    text = (OPS / name).read_text()
    # Strip comments so a documented counter-example cannot satisfy the assertion.
    body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    return list(_RUN.finditer(body))


def test_the_scripts_still_drive_freqtrade_through_compose_run():
    """Guard the guard: if the scripts stop matching, the tests below pass vacuously."""
    assert {name: len(_invocations(name)) for name in SCRIPTS} == {
        "bootstrap_data.sh": 2,          # download-data, list-data
        "backtest.sh": 1,                # backtesting
        "refresh_backtest_data.sh": 1,   # download-data
    }


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_compose_run_passes_the_config_explicitly(name):
    for match in _invocations(name):
        rest = match.group("rest")
        assert "--config" in rest, (
            f"{name}: `docker compose run ... {match.group('cmd')}` passes no --config; "
            f"compose run replaces the service command, so the config is simply lost"
        )
        assert CONTAINER_CONFIG in rest, (
            f"{name}: {match.group('cmd')} must use the in-container config path "
            f"{CONTAINER_CONFIG}, not a host path"
        )


def test_bootstrap_downloads_into_the_mounted_data_directory():
    """The config is also what points the download at data/binance/: user_data_dir."""
    text = (OPS / "bootstrap_data.sh").read_text()
    assert "--timerange 20210101-" in text
    assert "-t 1h 4h 1d" in text
    assert "ops.check_gaps" in text          # the gate the download feeds
