"""Signed mode state: what makes a sleeve live, and every way that can fail.

The invariant under test is one sentence: **nothing but a correctly signed file makes a
sleeve live.** Missing file, truncated file, bad JSON, unknown shape, edited field, wrong
secret, absent secret — all of them read back as TEST for every sleeve, and the generator
refuses to render a live overlay from any of them.
"""

from __future__ import annotations

import json
import os

import pytest

from ops.config import load_config, seed_for
from ops.gen_freqtrade_config import build_mode_overlay, build_sleeve_runtime
from ops.lib import mode_state as ms
from ops.lib import paths, signing

SECRET = "console-secret-for-tests-0123456789"


@pytest.fixture(autouse=True)
def state_root(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.STATE_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(paths.AUTOMATED_RUN_ENV, raising=False)
    monkeypatch.setenv(signing.SECRET_ENV, SECRET)
    return tmp_path


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _write_live(path=None, *, secret=SECRET, sleeve="b", state="LIVE_PROPOSE", seed=500.0):
    built = ms.build(
        {
            "a": ms.SleeveState(),
            sleeve: ms.SleeveState(state=state, submode="propose",
                                   run_id="live-b-20270201-01", seed_usdt=seed),
        },
        set_by="human:console:sid-1",
        transition_id=12,
    )
    return ms.write(built, secret=secret, path=path)


# --------------------------------------------------------------------------- happy path


def test_round_trip_keeps_every_field():
    p = _write_live()
    loaded = ms.load()
    assert loaded.path == p == paths.mode_state_path()
    assert loaded.verified is True and loaded.reason == ms.REASON_OK
    b = loaded.sleeve("b")
    assert (b.state, b.submode, b.run_id, b.seed_usdt) == (
        "LIVE_PROPOSE", "propose", "live-b-20270201-01", 500.0)
    assert loaded.sleeve("a").state == "TEST"
    assert loaded.set_by == "human:console:sid-1" and loaded.transition_id == 12


def test_file_is_private_inside_a_private_directory(state_root):
    _write_live()
    f = paths.mode_state_path()
    assert oct(f.stat().st_mode)[-3:] == "600"
    assert oct(f.parent.stat().st_mode)[-3:] == "700"


def test_phase_is_derived_from_the_sleeves():
    _write_live(state="LIVE_PROPOSE")
    assert ms.load().phase == "live_propose"
    _write_live(state="LIVE_EXECUTE")
    assert ms.load().phase == "live_execute"


def test_seed_for_prefers_the_pinned_live_seed(cfg):
    _write_live(seed=500.0)
    state = ms.load()
    assert seed_for(cfg, "b", state=state) == 500.0
    assert seed_for(cfg, "a", state=state) == cfg.modes.test.seed_usdt["a"]


def test_describe_is_one_readable_line():
    _write_live()
    assert ms.describe(ms.load()) == "a=TEST b=LIVE_PROPOSE (verified)"


# --------------------------------------------------------------------------- fail closed


def test_missing_file_is_test_for_every_sleeve():
    state = ms.load()
    assert state.verified is False and state.reason == ms.REASON_MISSING
    assert [state.state_of(s) for s in ("a", "b")] == ["TEST", "TEST"]
    assert state.phase == "paper"
    assert state.any_live() is False


def test_bad_json_is_test():
    paths.write_private(paths.mode_state_path(), "{not json")
    assert ms.load().reason == ms.REASON_BAD_JSON
    assert ms.load().is_live("b") is False


def test_tampered_field_is_test():
    _write_live(state="LIVE_PROPOSE")
    raw = json.loads(paths.mode_state_path().read_text())
    raw["sleeves"]["b"]["state"] = "LIVE_EXECUTE"      # signature no longer matches
    paths.write_private(paths.mode_state_path(), json.dumps(raw))
    state = ms.load()
    assert state.reason == ms.REASON_BAD_SIGNATURE
    assert state.state_of("b") == "TEST" and state.phase == "paper"


def test_tampered_seed_is_test():
    _write_live(seed=500.0)
    raw = json.loads(paths.mode_state_path().read_text())
    raw["sleeves"]["b"]["seed_usdt"] = 500000.0
    paths.write_private(paths.mode_state_path(), json.dumps(raw))
    assert ms.load().reason == ms.REASON_BAD_SIGNATURE


def test_stripped_signature_is_test():
    _write_live()
    raw = json.loads(paths.mode_state_path().read_text())
    raw.pop("sig")
    paths.write_private(paths.mode_state_path(), json.dumps(raw))
    assert ms.load().reason == ms.REASON_BAD_SIGNATURE


def test_signature_from_another_secret_is_test():
    _write_live(secret="a-different-secret-0123456789")
    assert ms.load().reason == ms.REASON_BAD_SIGNATURE


def test_absent_secret_is_test(monkeypatch):
    _write_live()
    monkeypatch.delenv(signing.SECRET_ENV)
    state = ms.load()
    assert state.reason == ms.REASON_NO_SECRET
    assert state.state_of("b") == "TEST"


def test_unknown_state_name_is_test():
    _write_live()
    raw = json.loads(paths.mode_state_path().read_text())
    raw["sleeves"]["b"]["state"] = "SUPER_LIVE"
    paths.write_private(paths.mode_state_path(),
                        json.dumps(signing.sign_payload({k: v for k, v in raw.items()
                                                         if k != "sig"}, SECRET)))
    assert ms.load().reason == ms.REASON_BAD_SHAPE


def test_future_version_is_test():
    _write_live()
    raw = {k: v for k, v in json.loads(paths.mode_state_path().read_text()).items()
           if k != "sig"}
    raw["version"] = 99
    paths.write_private(paths.mode_state_path(), json.dumps(signing.sign_payload(raw, SECRET)))
    assert ms.load().reason == ms.REASON_BAD_SHAPE


def test_unlisted_sleeve_reads_as_test():
    _write_live()
    assert ms.load().sleeve("zzz").state == "TEST"


# --------------------------------------------------------------------------- writing


def test_write_refuses_under_an_automated_run(monkeypatch):
    monkeypatch.setenv(paths.AUTOMATED_RUN_ENV, "1")
    with pytest.raises(ms.ModeStateError, match="EARN_AUTOMATED_RUN"):
        _write_live()


def test_write_without_a_secret_is_refused(monkeypatch):
    monkeypatch.delenv(signing.SECRET_ENV)
    with pytest.raises(signing.SigningError, match=signing.SECRET_ENV):
        ms.write(ms.build({}, set_by="human:cli"))


def test_short_secret_is_refused(monkeypatch):
    monkeypatch.setenv(signing.SECRET_ENV, "short")
    with pytest.raises(signing.SigningError, match="shorter"):
        ms.write(ms.build({}, set_by="human:cli"))


def test_console_secret_never_leaks_into_the_written_file():
    _write_live()
    assert SECRET not in paths.mode_state_path().read_text()


# --------------------------------------------------------------------------- generator


def test_live_overlay_needs_a_verified_state(cfg):
    _write_live(state="LIVE_EXECUTE")
    overlay = build_mode_overlay(cfg, "b", ms.load())
    assert overlay["dry_run"] is False

    raw = json.loads(paths.mode_state_path().read_text())
    raw["sleeves"]["b"]["state"] = "LIVE_EXECUTE"
    raw["sig"] = "hmac-sha256:" + "0" * 64
    paths.write_private(paths.mode_state_path(), json.dumps(raw))
    overlay = build_mode_overlay(cfg, "b", ms.load())
    assert overlay["dry_run"] is True                 # fell back to TEST, not to live
    assert build_sleeve_runtime(cfg, "b", ms.load())["mode"] == "test"


# --------------------------------------------------------------------------- signing


def test_canonical_form_ignores_key_order_and_the_signature():
    a = {"b": 2, "a": 1, "sig": "x"}
    b = {"a": 1, "b": 2}
    assert signing.canonical(a) == signing.canonical(b)


def test_verify_is_false_without_a_secret_or_signature():
    payload = {"a": 1}
    sig = signing.sign(payload, SECRET)
    assert signing.verify(payload, sig, SECRET) is True
    assert signing.verify(payload, sig, None) is False
    assert signing.verify(payload, None, SECRET) is False
    assert signing.verify({"a": 2}, sig, SECRET) is False


def test_secret_is_read_from_the_environment_and_trimmed(monkeypatch):
    monkeypatch.setenv(signing.SECRET_ENV, "  padded-secret-value-123456  ")
    assert signing.get_secret() == "padded-secret-value-123456"
    monkeypatch.setenv(signing.SECRET_ENV, "   ")
    assert signing.get_secret() is None


def test_console_secret_is_not_in_any_envwrap_allowlist():
    """Spec section 1: the console secret is in NO job allowlist, so an automated run can
    never mint a live mode file."""
    envwrap = (paths.REPO_ROOT / "ops" / "envwrap.sh").read_text()
    assert signing.SECRET_ENV not in envwrap
    assert "EARN_CONSOLE_TOKEN" not in envwrap


def test_new_secret_is_long_enough_to_sign_with():
    secret = signing.new_secret()
    assert len(secret) >= signing.MIN_SECRET_LEN
    assert signing.verify_payload(signing.sign_payload({"x": 1}, secret), secret)


def test_state_root_env_moves_the_whole_var_layout(tmp_path):
    assert paths.state_root() == tmp_path.resolve()
    assert paths.mode_state_path() == tmp_path / "var" / "state" / "mode.json"
    assert paths.runtime_dir() == tmp_path / "var" / "runtime"
    assert paths.ops_lock_path() == tmp_path / "ops" / "locks" / "ops.lock"
    assert paths.data_path("journal/journal.db") == tmp_path / "journal" / "journal.db"
    assert paths.data_path("/abs/elsewhere.db") == type(tmp_path)("/abs/elsewhere.db")
    assert paths.in_worktree() is True
    assert os.environ[paths.STATE_ROOT_ENV] == str(tmp_path)
