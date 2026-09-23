"""``/api/prompts`` — families, versions, immutability, render preview, activation.

The rule under test throughout: **a version a snapshot has used is immutable.** The replay
harness rebuilds the exact prompt that produced each recorded decision to check for builder
drift, so rewriting an old version would quietly invalidate the whole evidence chain. Saving
therefore creates the next version; only an unused version may be edited in place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ops import db
from ops.config import load_config

from .conftest import step_up

V1 = "# research v1\n\nState:\n\n{{STATE}}\n"
V2 = "# research v2\n\nState:\n\n{{STATE}}\n\nFlags:\n\n{{FLAGS}}\n"


@pytest.fixture
def repo(env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = env / "repo"
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "research.v1.md").write_text(V1, encoding="utf-8")
    (root / "prompts" / "research.v2.md").write_text(V2, encoding="utf-8")
    (root / "config").mkdir()
    journal, _ = db.init_all(load_config(), root=env)
    with db.opened(journal) as conn:
        conn.execute(
            "INSERT INTO proposals(run_id, shadow, ts_utc, valid, module, prompt_version,"
            " model) VALUES ('2026-09-21T08:30+04:00',0,'2026-09-21T04:30:00Z',1,'trend',"
            "'research.v1','claude-opus-5')")
        conn.commit()
    for module in ("console.routers.prompts", "console.services.prompts_service"):
        monkeypatch.setattr(f"{module}._root", lambda root=root: root)
    return root


def _families(client: TestClient) -> dict:
    response = client.get("/api/prompts")
    assert response.status_code == 200, response.text
    return {f["family"]: f for f in response.json()["families"]}


def test_the_listing_groups_versions_and_marks_the_used_one_immutable(
        auth_client: TestClient, repo: Path):
    families = _families(auth_client)
    assert "research" in families
    versions = {v["version_id"]: v for v in families["research"]["versions"]}
    assert set(versions) == {"research.v1", "research.v2"}
    assert versions["research.v1"]["snapshots"] == 1
    assert versions["research.v1"]["immutable"] is True
    assert versions["research.v2"]["immutable"] is False
    assert versions["research.v2"]["placeholders"] == ["FLAGS", "STATE"]


def test_reading_a_version_lists_the_snapshots_that_used_it(auth_client: TestClient,
                                                            repo: Path):
    response = auth_client.get("/api/prompts/research.v1.md")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version_id"] == "research.v1"
    assert body["immutable"] is True
    assert [row["run_id"] for row in body["snapshots_using"]] == \
        ["2026-09-21T08:30+04:00"]


def test_saving_creates_the_next_version_and_never_rewrites_the_old_one(
        auth_client: TestClient, repo: Path):
    before = (repo / "prompts" / "research.v2.md").read_text()
    response = auth_client.put("/api/prompts/research.v2.md",
                               json={"content": V2 + "\nExtra guidance.\n",
                                     "as_new_version": True, "base_sha": None})
    assert response.status_code == 200, response.text
    assert response.json()["version_id"] == "research.v3"
    assert (repo / "prompts" / "research.v3.md").exists()
    assert (repo / "prompts" / "research.v2.md").read_text() == before


def test_an_unused_version_may_be_edited_in_place(auth_client: TestClient, repo: Path):
    current = auth_client.get("/api/prompts/research.v2.md").json()
    response = auth_client.put("/api/prompts/research.v2.md",
                               json={"content": V2 + "\ntightened\n",
                                     "as_new_version": False,
                                     "base_sha": current["sha"]})
    assert response.status_code == 200, response.text
    assert "tightened" in (repo / "prompts" / "research.v2.md").read_text()


def test_a_used_version_cannot_be_edited_in_place(auth_client: TestClient, repo: Path):
    before = (repo / "prompts" / "research.v1.md").read_text()
    response = auth_client.put("/api/prompts/research.v1.md",
                               json={"content": "# rewritten\n",
                                     "as_new_version": False, "base_sha": None})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "immutable"
    assert (repo / "prompts" / "research.v1.md").read_text() == before


def test_a_stale_base_sha_is_a_conflict(auth_client: TestClient, repo: Path):
    response = auth_client.put("/api/prompts/research.v2.md",
                               json={"content": "# x\n", "as_new_version": False,
                                     "base_sha": "0" * 32})
    assert response.status_code == 409


def test_an_empty_prompt_is_refused(auth_client: TestClient, repo: Path):
    response = auth_client.post("/api/prompts/research/versions", json={"content": "   "})
    assert response.status_code in (400, 422)


def test_activation_writes_the_tier1_overlay_and_needs_step_up(
        auth_client: TestClient, repo: Path, token: str):
    body = {"family": "research", "version": "2"}
    assert auth_client.put("/api/prompts/active", json=body).status_code == 403

    step_up(auth_client, token)
    response = auth_client.put("/api/prompts/active", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["version_id"] == "research.v2"

    overlay = yaml.safe_load((repo / "config" / "prompts-auto.yaml").read_text())
    assert overlay["active"] == {"research": "research.v2"}
    assert _families(auth_client)["research"]["active"] == "research.v2"


def test_activating_a_version_that_does_not_exist_is_a_404(auth_client: TestClient,
                                                           repo: Path, token: str):
    step_up(auth_client, token)
    response = auth_client.put("/api/prompts/active",
                               json={"family": "research", "version": "9"})
    assert response.status_code == 404


def test_the_diff_between_two_versions_is_a_unified_diff(auth_client: TestClient,
                                                         repo: Path):
    response = auth_client.get("/api/prompts/diff",
                               params={"left": "research.v1.md", "right": "research.v2.md"})
    assert response.status_code == 200, response.text
    diff = response.json()["diff"]
    assert "--- research.v1.md" in diff
    assert "+Flags:" in diff


def test_render_substitutes_placeholders_and_estimates_tokens(auth_client: TestClient,
                                                              repo: Path):
    response = auth_client.post("/api/prompts/research.v2.md/render",
                                json={"context": {"state": "regime: trend_up"}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "regime: trend_up" in body["text"]
    assert "{{STATE}}" not in body["text"]
    assert body["unresolved_placeholders"] == ["FLAGS"]
    assert body["tokens"] > 0


def test_an_unknown_prompt_is_a_404(auth_client: TestClient, repo: Path):
    assert auth_client.get("/api/prompts/nope.v1.md").status_code == 404


def test_a_path_cannot_escape_the_prompts_folder(repo: Path):
    from console.services import prompts_service
    from console.services.prompts_service import PromptError

    with pytest.raises(PromptError) as excinfo:
        prompts_service.read_prompt("../config/earn.yaml", root=repo)
    assert excinfo.value.code == "forbidden"


def test_every_route_needs_a_session(client: TestClient, repo: Path):
    assert client.get("/api/prompts").status_code == 401
    assert client.put("/api/prompts/active",
                      json={"family": "research", "version": "2"}).status_code in (401, 403)
