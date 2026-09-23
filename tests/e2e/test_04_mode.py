"""Step 4 — mode: preflight in TEST, a seed change, a real test-run reset, fail-closed.

Nothing here goes live and nothing can: ``ops.modes.transition`` needs a passing
preflight, and a host with no Docker, no exchange key and no track record fails seven of
the thirteen items. What is proved instead is that the refusal is *informative* (every
blocking item named with its evidence) rather than a traceback, that the TEST path
actually works end to end, and that a tampered signature costs the sleeve its mode.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.conftest import Api, Sandbox

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def mode_file(sandbox: Sandbox):
    return sandbox.state / "var" / "state" / "mode.json"


class TestPreflight:
    def test_preflight_in_test_lists_blocking_items_rather_than_crashing(
        self, api: Api, sandbox: Sandbox
    ) -> None:
        response = api.post("/api/mode/preflight", json={
            "sleeve": "a", "target": "LIVE_PROPOSE", "submode": "propose",
            "seed_usdt": 100.0,
        })
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["ok"] is False, "a host with no docker and no track record passed preflight"
        assert body["preflight_id"], body
        assert body["confirm_phrase"].startswith("GO LIVE"), body["confirm_phrase"]

        items = body["items"]
        assert len(items) >= 13, f"only {len(items)} preflight items"
        blocking_failures = [i for i in items if i["blocking"] and i["status"] == "fail"]
        assert blocking_failures, "nothing blocked a go-live from a bare sandbox"
        for item in blocking_failures:
            assert item["detail"], f"{item['id']} failed with no explanation"
        ids = {i["id"] for i in items}
        # The items spec §8.3 makes blocking, and that this host cannot satisfy.
        assert ids & {"stoploss_on_exchange", "track_record", "exchange_account"}, sorted(ids)

    def test_preflight_is_read_only(self, api: Api, sandbox: Sandbox) -> None:
        before = mode_file(sandbox).read_text() if mode_file(sandbox).exists() else None
        api.post("/api/mode/preflight", json={"sleeve": "b", "target": "LIVE_EXECUTE",
                                              "submode": "execute", "seed_usdt": 50.0})
        after = mode_file(sandbox).read_text() if mode_file(sandbox).exists() else None
        assert after == before, "a preflight wrote the mode file"

    def test_a_transition_without_step_up_is_refused(self, api: Api) -> None:
        response = api.post("/api/mode/transition", json={
            "sleeve": "a", "target": "LIVE_PROPOSE", "submode": "propose",
            "confirm_phrase": "GO LIVE a 100 USDT",
        })
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "step_up_required"


class TestSeedAndReset:
    def test_a_seed_change_and_a_reset_run_end_to_end(self, api: Api, sandbox: Sandbox) -> None:
        # ---- the seed change, through the config API --------------------------
        base_sha = api.json("/api/config/earn")["sha"]
        new_seed = 12345.0
        preview = api.post("/api/config/earn/preview", json={
            "patch": [{"op": "replace", "path": "/modes/test/seed_usdt/a", "value": new_seed}],
            "base_sha": base_sha,
        })
        assert preview.status_code == 200, preview.text
        assert preview.json()["valid"] is True
        assert "reset_required" in preview.json()["effects"], preview.json()["effects"]

        saved = api.put("/api/config/earn", json={
            "patch": [{"op": "replace", "path": "/modes/test/seed_usdt/a", "value": new_seed}],
            "base_sha": base_sha, "reason": "e2e seed change", "commit": False,
        })
        assert saved.status_code == 200, saved.text
        seed_audit_id = saved.json()["audit_id"]

        # `reset_required` is never auto-applied — it is a banner, not an action.
        effects = api.json("/api/config/effects")
        assert effects["needs_reset"] is True, effects

        # ---- the reset, through the API ---------------------------------------
        before_calls = len(sandbox.docker_calls())
        api.step_up()
        reset = api.post("/api/testruns/a/reset", json={
            "confirm_phrase": "RESET", "label": "e2e", "notes": "end-to-end proof",
        })
        assert reset.status_code == 200, reset.text
        result = reset.json()
        run_id = result["run_id"]
        assert run_id.startswith("test-a-"), result
        steps = {s["step"]: s["status"] for s in result["steps"]}
        assert steps.get("write_mode") == "ok", steps
        assert steps.get("regen") == "ok", steps
        assert steps.get("compose") == "ok", steps
        assert steps.get("open_run") == "ok", steps

        # The compose step really ran `docker compose ... up ... freqtrade-a`.
        calls = sandbox.docker_calls()[before_calls:]
        assert calls, "the reset never invoked docker"
        up = [c for c in calls if " up " in f" {c} "]
        assert up, calls
        assert "freqtrade-a" in up[-1], up[-1]

        # ---- what the reset left behind ---------------------------------------
        state = json.loads(mode_file(sandbox).read_text())
        assert state["sleeves"]["a"]["state"] == "TEST"
        assert state["sleeves"]["a"]["run_id"] == run_id
        assert state["sleeves"]["a"]["seed_usdt"] == new_seed
        assert state["sig"].startswith("hmac-sha256:")

        runtime = sandbox.state / "var" / "runtime" / "runtime-a.json"
        assert runtime.exists(), "the reset rendered no runtime file for the strategy"
        assert json.loads(runtime.read_text())["mode"] == "test"

        mode = api.json("/api/mode")
        assert mode["verified"] is True and mode["reason"] == "ok"
        sleeve_a = next(s for s in mode["sleeves"] if s["sleeve"] == "a")
        assert sleeve_a["run_id"] == run_id
        assert sleeve_a["seed_usdt"] == new_seed

        summary = api.json("/api/testruns/summary/a")
        assert summary["run"]["run_id"] == run_id
        assert summary["pending"]["reset_required"] is False, summary["pending"]

        rows = api.json("/api/mode/runs?sleeve=a")
        assert any(r["run_id"] == run_id and r["status"] == "active" for r in rows), rows

        # ---- put the config back ----------------------------------------------
        reverted = api.post("/api/config/earn/revert",
                            json={"audit_id": seed_audit_id, "commit": False})
        assert reverted.status_code == 200, reverted.text
        assert api.json("/api/config/earn")["sha"] == base_sha

    def test_a_reset_without_the_typed_word_is_refused(self, api: Api) -> None:
        api.step_up()
        response = api.post("/api/testruns/a/reset", json={"confirm_phrase": "reset"})
        assert response.status_code == 400, response.text
        assert "RESET" in response.json()["error"]["message"]

    def test_a_reset_without_step_up_is_refused(self, server, console_token: str) -> None:
        session = Api(server.base_url, console_token)
        session.login()
        try:
            response = session.post("/api/testruns/a/reset", json={"confirm_phrase": "RESET"})
            assert response.status_code == 403, response.text
        finally:
            session.close()


class TestFailClosed:
    def test_a_tampered_signature_drops_every_sleeve_to_test(
        self, api: Api, sandbox: Sandbox
    ) -> None:
        path = mode_file(sandbox)
        assert path.exists(), "run the reset test first: there is no signed mode file"
        good = path.read_text()
        state = json.loads(good)
        assert state["sleeves"]["a"]["run_id"], state

        try:
            # A live-looking state with a signature that does not cover it.
            state["sleeves"]["a"]["state"] = "LIVE_EXECUTE"
            state["sleeves"]["a"]["submode"] = "execute"
            path.write_text(json.dumps(state))

            body = api.json("/api/mode")
            assert body["verified"] is False, body
            assert body["reason"] == "bad_signature", body
            assert body["phase"] == "paper", body
            assert all(s["state"] == "TEST" for s in body["sleeves"]), body["sleeves"]

            # And the same file read by the code the containers use.
            probe = sandbox.py("-c",
                               "from ops.lib import mode_state as m; s = m.load();"
                               " print(s.verified, s.reason, s.sleeve('a').state)")
            assert probe.returncode == 0, probe.stderr
            assert probe.stdout.split() == ["False", "bad_signature", "TEST"], probe.stdout
        finally:
            path.write_text(good)

        assert api.json("/api/mode")["verified"] is True

    def test_a_missing_secret_also_fails_closed(self, sandbox: Sandbox) -> None:
        """No ``EARN_CONSOLE_SECRET`` means nothing can be verified, so nothing is live."""
        probe = sandbox.py(
            "-c",
            "from ops.lib import mode_state as m; s = m.load();"
            " print(s.verified, s.reason, s.sleeve('a').state, s.sleeve('b').state)",
            env={"EARN_CONSOLE_SECRET": ""},
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.split() == ["False", "no_secret", "TEST", "TEST"], probe.stdout
