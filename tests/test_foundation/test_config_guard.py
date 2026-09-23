"""The config bless: preflight only lets a sleeve go live when the working config is
exactly what a human signed off.

Fail closed in every direction — no bless file, no secret, a forged signature, an edited
file, a missing file — all of them are ``ok=False`` with a reason a human can act on.
"""

from __future__ import annotations

import json

import pytest

from ops.lib import config_guard as cg
from ops.lib import paths, signing

SECRET = "console-secret-for-tests-0123456789"
ACTOR = "human:console:sid-1"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A miniature checkout holding just the protected files, plus its own var/."""
    monkeypatch.setenv(paths.STATE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(signing.SECRET_ENV, SECRET)
    (tmp_path / "config").mkdir()
    for rel in cg.BLESSED_FILES:
        (tmp_path / rel).write_text(f"# {rel}\ncontent: 1\n")
    return tmp_path


def test_unblessed_config_is_refused(repo):
    result = cg.verify(root=repo)
    assert not result
    assert result.reason == cg.REASON_MISSING
    assert "not blessed" in result.summary()


def test_bless_then_verify_passes(repo):
    payload = cg.bless(ACTOR, reason="initial setup", root=repo)
    assert payload["sig"].startswith("hmac-sha256:")
    assert set(payload["files"]) == set(cg.BLESSED_FILES)

    result = cg.verify(root=repo)
    assert result and result.reason == cg.REASON_OK
    assert result.blessed_by == ACTOR
    assert "config blessed" in result.summary()


def test_edited_file_is_detected(repo):
    cg.bless(ACTOR, root=repo)
    (repo / "config" / "earn.yaml").write_text("# tampered\ncontent: 2\n")
    result = cg.verify(root=repo)
    assert not result and result.reason == cg.REASON_DRIFT
    assert result.changed == ["config/earn.yaml"]
    assert cg.changed_files(root=repo) == ["config/earn.yaml"]


def test_deleted_file_is_detected(repo):
    cg.bless(ACTOR, root=repo)
    (repo / "config" / "riskgate.json").unlink()
    result = cg.verify(root=repo)
    assert not result and result.reason == cg.REASON_DRIFT
    assert result.missing == ["config/riskgate.json"]


def test_forged_signature_is_refused(repo):
    cg.bless(ACTOR, root=repo)
    raw = json.loads(paths.bless_path().read_text())
    raw["files"]["config/earn.yaml"] = "0" * 64      # pretend the edited file was blessed
    paths.write_private(paths.bless_path(), json.dumps(raw))
    assert cg.verify(root=repo).reason == cg.REASON_BAD_SIGNATURE


def test_resigning_with_another_secret_is_refused(repo):
    cg.bless(ACTOR, root=repo, secret="a-completely-different-secret-1")
    assert cg.verify(root=repo).reason == cg.REASON_BAD_SIGNATURE


def test_absent_secret_is_refused(repo, monkeypatch):
    cg.bless(ACTOR, root=repo)
    monkeypatch.delenv(signing.SECRET_ENV)
    assert cg.verify(root=repo).reason == cg.REASON_NO_SECRET


def test_corrupt_bless_file_is_refused(repo):
    cg.bless(ACTOR, root=repo)
    paths.write_private(paths.bless_path(), "{oops")
    assert cg.verify(root=repo).reason == cg.REASON_BAD_JSON


def test_unknown_version_is_refused(repo):
    cg.bless(ACTOR, root=repo)
    raw = {k: v for k, v in json.loads(paths.bless_path().read_text()).items() if k != "sig"}
    raw["version"] = 99
    paths.write_private(paths.bless_path(), json.dumps(signing.sign_payload(raw, SECRET)))
    assert cg.verify(root=repo).reason == cg.REASON_BAD_VERSION


def test_bless_file_is_private(repo):
    cg.bless(ACTOR, root=repo)
    p = paths.bless_path()
    assert oct(p.stat().st_mode)[-3:] == "600"
    assert oct(p.parent.stat().st_mode)[-3:] == "700"
    assert SECRET not in p.read_text()


def test_rebless_after_an_intended_edit_passes(repo):
    cg.bless(ACTOR, root=repo)
    (repo / "config" / "earn.yaml").write_text("# deliberate change\ncontent: 3\n")
    assert not cg.verify(root=repo)
    cg.bless(ACTOR, reason="raised the daily stop", root=repo)
    assert cg.verify(root=repo)


def test_the_real_repo_config_is_covered():
    """The protected list must name the files a live sleeve actually depends on."""
    assert set(cg.BLESSED_FILES) == {
        "config/earn.yaml", "config/models.yaml", "config/freqtrade-a.json",
        "config/freqtrade-b.json", "config/riskgate.json",
    }
    digests = cg.digest()
    assert set(digests) == set(cg.BLESSED_FILES)
    assert all(len(v) == 64 for v in digests.values())
