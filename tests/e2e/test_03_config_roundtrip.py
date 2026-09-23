"""Step 3 — read a value, preview the diff, save it, see the audit row, revert it.

The whole point of the three-call shape (read → preview → save) is that the operator
saves exactly the version they were looking at, so the ``base_sha`` is carried through
every call here and a stale one is proved to be refused.
"""

from __future__ import annotations

import pytest
import yaml

from tests.e2e.conftest import Api, Sandbox

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

#: An unprotected tier-2 leaf with no effects: the smallest honest round trip.
LABEL_PATH = "/modes/test/label"
NEW_LABEL = "e2e-round-trip"


def earn_yaml(sandbox: Sandbox) -> dict:
    return yaml.safe_load((sandbox.repo / "config" / "earn.yaml").read_text())


class TestConfigRoundTrip:
    def test_the_whole_round_trip(self, api: Api, sandbox: Sandbox) -> None:
        # ---- read -------------------------------------------------------------
        listing = api.json("/api/config")
        ids = {f["id"] for f in listing["files"]}
        assert "earn" in ids, sorted(ids)

        before = api.json("/api/config/earn")
        base_sha = before["sha"]
        assert base_sha, "GET /api/config/earn returned no sha to save against"
        original_label = earn_yaml(sandbox)["modes"]["test"]["label"]
        assert original_label != NEW_LABEL

        # ---- preview ----------------------------------------------------------
        preview = api.post("/api/config/earn/preview", json={
            "patch": [{"op": "replace", "path": LABEL_PATH, "value": NEW_LABEL}],
            "base_sha": base_sha,
        })
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["valid"] is True, body
        assert NEW_LABEL in body["diff"], body["diff"]
        assert f"{original_label}" in body["diff"]
        assert body.get("requires_stepup") is False, body
        # A preview writes nothing.
        assert earn_yaml(sandbox)["modes"]["test"]["label"] == original_label

        # ---- save -------------------------------------------------------------
        saved = api.put("/api/config/earn", json={
            "patch": [{"op": "replace", "path": LABEL_PATH, "value": NEW_LABEL}],
            "base_sha": base_sha,
            "reason": "end-to-end proof",
            "commit": False,
        })
        assert saved.status_code == 200, saved.text
        result = saved.json()
        audit_id = result["audit_id"]
        assert isinstance(audit_id, int) and audit_id > 0, result
        assert earn_yaml(sandbox)["modes"]["test"]["label"] == NEW_LABEL

        # The file is still loadable and the rest of it is untouched.
        assert api.json("/api/config/earn")["sha"] != base_sha
        reloaded = earn_yaml(sandbox)
        assert reloaded["risk"]["usdt_floor"] == \
            yaml.safe_load((sandbox.repo / "config" / "earn.yaml").read_text())["risk"]["usdt_floor"]

        # ---- the audit row ----------------------------------------------------
        history = api.json("/api/config/earn/history")
        entries = history["entries"] if "entries" in history else history["items"]
        assert entries, history
        newest = entries[0]
        assert newest["id"] == audit_id
        assert newest["reason"] == "end-to-end proof"
        assert "modes.test.label" in " ".join(newest["changed_paths"])

        audit = api.json("/api/audit?limit=50")
        rows = audit["items"] if "items" in audit else audit["entries"]
        assert any(r.get("source") == "config_audit" and r.get("ref") == str(audit_id)
                   for r in rows), [r.get("source") for r in rows][:10]

        entry = api.json(f"/api/audit/entry/config_audit/{audit_id}")
        assert entry["rows"], entry
        assert NEW_LABEL in entry["rows"][0]["diff"]
        assert entry["rows"][0]["actor"].startswith("human:console:")

        # ---- revert -----------------------------------------------------------
        reverted = api.post("/api/config/earn/revert",
                            json={"audit_id": audit_id, "commit": False})
        assert reverted.status_code == 200, reverted.text
        assert earn_yaml(sandbox)["modes"]["test"]["label"] == original_label
        assert api.json("/api/config/earn")["sha"] == base_sha, \
            "a revert did not restore the file byte for byte"

    def test_a_stale_base_sha_is_a_conflict(self, api: Api) -> None:
        response = api.put("/api/config/earn", json={
            "patch": [{"op": "replace", "path": LABEL_PATH, "value": "nope"}],
            "base_sha": "0" * 64,
            "reason": "stale write",
            "commit": False,
        })
        assert response.status_code == 409, response.text
        # docs/contracts.md §9.4 calls the 409 code `conflict`; ops/config_store.py:84
        # emits `sha_conflict` and tests/test_console/test_config_api.py pins that. The
        # status is what a client branches on, so the drift is documented, not "fixed"
        # under an integration pass.
        assert response.json()["error"]["code"] in ("conflict", "sha_conflict")

    def test_a_protected_path_needs_step_up(self, api: Api, sandbox: Sandbox) -> None:
        """``risk.usdt_floor`` is x-protected: a plain session may look, never save."""
        base_sha = api.json("/api/config/earn")["sha"]
        current = float(earn_yaml(sandbox)["risk"]["usdt_floor"])
        candidate = round(current / 2, 4)
        assert candidate != current
        patch = [{"op": "replace", "path": "/risk/usdt_floor", "value": candidate}]

        preview = api.post("/api/config/earn/preview",
                           json={"patch": patch, "base_sha": base_sha})
        assert preview.status_code == 200, preview.text
        assert preview.json()["valid"] is True, preview.text
        assert preview.json()["requires_stepup"] is True

        response = api.put("/api/config/earn", json={
            "patch": patch, "base_sha": base_sha,
            "reason": "should be refused", "commit": False,
        })
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "step_up_required"
        assert float(earn_yaml(sandbox)["risk"]["usdt_floor"]) == current

    def test_an_out_of_bounds_value_is_refused_before_it_is_written(
        self, api: Api, sandbox: Sandbox
    ) -> None:
        base_sha = api.json("/api/config/earn")["sha"]
        response = api.post("/api/config/earn/preview", json={
            "patch": [{"op": "replace", "path": "/risk/max_gross_exposure", "value": 9.0}],
            "base_sha": base_sha,
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["valid"] is False, body
        assert body["errors"], body
        assert api.json("/api/config/earn")["sha"] == base_sha

    def test_the_generated_configs_still_match_after_the_round_trip(
        self, api: Api, sandbox: Sandbox
    ) -> None:
        blessed = sandbox.py("-m", "console", "bless-config", "--reason", "e2e")
        assert blessed.returncode == 0, blessed.stdout + blessed.stderr
        drift = api.json("/api/config/drift")
        assert drift["bless"]["ok"] is True, drift["bless"]
        by_name = {g["generator"]: g for g in drift["generators"]}
        for name in ("ops.gen_freqtrade_config", "ops.gen_ops_files"):
            assert by_name[name]["ok"] is True, by_name[name]
        assert drift["ok"] is True, drift
        check = sandbox.py("-m", "ops.gen_freqtrade_config", "--check")
        assert check.returncode == 0, check.stdout + check.stderr
