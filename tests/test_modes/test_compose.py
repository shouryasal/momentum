"""The live compose overlay and the docker-compose invocation."""

from __future__ import annotations

import pytest

from ops.config import REPO_ROOT
from ops.lib import compose as composelib
from ops.lib import paths


class TestLiveTemplate:
    def test_the_template_is_committed(self):
        assert composelib.template_path().exists()

    def test_rendering_substitutes_the_data_root(self, cfg, state_root):
        text = composelib.render_live_file(cfg, root=state_root)
        assert composelib.ROOT_PLACEHOLDER not in text
        assert f"{state_root}/var/runtime:/freqtrade/earn-runtime:ro" in text
        assert f"name: {cfg.runtime.docker.compose_project}" in text

    def test_the_overlay_drops_db_url_so_the_mode_overlay_wins(self, cfg, state_root):
        """The base command's --db-url would override the per-run database."""
        base = composelib.base_path().read_text()
        assert "--db-url" in base  # the committed file still has it
        text = composelib.render_live_file(cfg, root=state_root)
        directives = "\n".join(
            ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
        )
        assert "--db-url" not in directives
        for sleeve in paths.SLEEVES:
            assert f"--config /freqtrade/earn-runtime/freqtrade-{sleeve}.mode.json" in text

    def test_each_sleeve_gets_its_own_runtime_file(self, cfg, state_root):
        text = composelib.render_live_file(cfg, root=state_root)
        for sleeve in paths.SLEEVES:
            assert f"EARN_RUNTIME: /freqtrade/earn-runtime/runtime-{sleeve}.json" in text

    def test_no_credential_ever_appears_in_the_template(self):
        text = composelib.template_path().read_text()
        for needle in ("BINANCE_KEY", "BINANCE_SECRET", "FREQTRADE__EXCHANGE__KEY"):
            assert needle not in text, f"{needle} belongs in the compose override, not here"

    def test_writing_is_private_and_idempotent(self, cfg, state_root):
        first = composelib.write_live_file(cfg, root=state_root)
        assert first == composelib.live_file_path()
        content = first.read_text()
        assert composelib.write_live_file(cfg, root=state_root).read_text() == content

    def test_a_missing_template_refuses_rather_than_renders_empty(self, cfg, tmp_path):
        with pytest.raises(composelib.ComposeError, match="template unreadable"):
            composelib.render_live_file(cfg, root=tmp_path, source_root=tmp_path)


class TestComposeInvocation:
    def test_layer_order_is_base_then_live_then_credentials(self, cfg, state_root):
        composelib.write_live_file(cfg, root=state_root)
        override = paths.runtime_dir() / "compose.override.yml"
        override.write_text("services: {}\n")
        files = composelib.compose_files(cfg)
        assert [f.name for f in files] == [
            "docker-compose.yml", "docker-compose.live.yml", "compose.override.yml"
        ]

    def test_absent_layers_are_skipped(self, cfg, state_root):
        assert [f.name for f in composelib.compose_files(cfg)] == ["docker-compose.yml"]

    def test_up_argv(self, cfg, state_root, docker):
        compose = composelib.Compose(cfg, root=state_root, runner=docker)
        compose.up(["freqtrade-a"])
        argv = docker.calls[-1]
        assert argv[:4] == ["docker", "compose", "-p", cfg.runtime.docker.compose_project]
        assert argv[-4:] == ["up", "-d", "--force-recreate", "freqtrade-a"]

    def test_up_without_recreate(self, cfg, state_root, docker):
        composelib.Compose(cfg, root=state_root, runner=docker).up(
            ["freqtrade-b"], force_recreate=False
        )
        assert "--force-recreate" not in docker.calls[-1]

    def test_it_runs_from_the_checkout_not_the_data_root(self, cfg, state_root):
        seen: list = []

        def runner(argv, cwd, timeout_s):
            seen.append(cwd)
            return composelib.CommandResult(list(argv), 0)

        composelib.Compose(cfg, root=state_root, runner=runner).ps()
        assert seen == [REPO_ROOT / "ops"]

    def test_a_failure_raises_with_the_output(self, cfg, state_root, docker):
        docker.ok = False
        with pytest.raises(composelib.ComposeError, match="no such service"):
            composelib.Compose(cfg, root=state_root, runner=docker).up(["freqtrade-a"])

    def test_unchecked_commands_return_the_failure(self, cfg, state_root, docker):
        docker.ok = False
        result = composelib.Compose(cfg, root=state_root, runner=docker).ps()
        assert not result.ok and "no such service" in result.output

    def test_service_names_come_from_the_config(self, cfg, state_root, docker):
        compose = composelib.Compose(cfg, root=state_root, runner=docker)
        assert compose.service_for("a") == cfg.ops.bots["a"].service
        assert compose.service_for("B") == cfg.ops.bots["b"].service

    def test_recreate_sleeve_renders_the_overlay_first(self, cfg, state_root, docker):
        composelib.recreate_sleeve(cfg, "a", root=state_root, runner=docker)
        assert composelib.live_file_path().exists()
        assert cfg.ops.bots["a"].service in docker.calls[-1]
