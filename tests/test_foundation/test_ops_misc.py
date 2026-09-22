"""Kill switch, locks, tg sender, compose pin, Scaffold import-safety."""

from pathlib import Path

import pytest
import yaml

from ops import db
from ops.config import REPO_ROOT, load_config
from ops.lib import kill, locks, tg


def test_kill_switch_roundtrip(tmp_path):
    cfg = load_config()
    assert not kill.is_engaged(cfg, root=tmp_path)
    p = kill.engage(cfg, "test incident", root=tmp_path)
    assert p == tmp_path / "ops" / "killdir" / "KILL"
    assert kill.is_engaged(cfg, root=tmp_path)
    assert kill.reason(cfg, root=tmp_path) == "test incident"
    p.unlink()
    assert not kill.is_engaged(cfg, root=tmp_path)


def test_lock_excludes_second_holder(tmp_path):
    with locks.acquire("job", locks_dir=tmp_path):
        with pytest.raises(locks.LockBusy):
            with locks.acquire("job", locks_dir=tmp_path):
                pass
    with locks.acquire("job", locks_dir=tmp_path):
        pass  # released cleanly


def test_tg_send_dedupe_and_record(tmp_path):
    cfg = load_config()
    _, knowledge = db.init_all(cfg, root=tmp_path)
    sent = []

    def transport(token, chat, text):
        sent.append(text)
        return True

    with db.connect(knowledge) as conn:
        assert tg.send("hello", "warn", dedupe_key="k1", conn=conn,
                       token="t", chat_id="c", transport=transport)
        assert tg.send("hello again", "warn", dedupe_key="k1", conn=conn,
                       token="t", chat_id="c", transport=transport)
        rows = conn.execute("SELECT * FROM ops_alerts").fetchall()
    assert len(sent) == 1  # second suppressed by dedupe
    assert sent[0].startswith("⚠️ ")
    assert rows[0]["delivered"] == 1


def test_tg_send_never_raises_without_creds(tmp_path):
    cfg = load_config()
    _, knowledge = db.init_all(cfg, root=tmp_path)
    with db.connect(knowledge) as conn:
        ok = tg.send("no creds", "critical", conn=conn, token="", chat_id="")
    assert ok is False  # recorded undelivered, no exception


def test_compose_pins_and_killdir():
    compose = yaml.safe_load((REPO_ROOT / "ops" / "docker-compose.yml").read_text())
    for svc in ("freqtrade-a", "freqtrade-b"):
        s = compose["services"][svc]
        assert s["image"] == "freqtradeorg/freqtrade:2026.8"
        assert s["restart"] == "unless-stopped"
        ports = s["ports"]
        assert all(p.startswith("127.0.0.1:") for p in ports)
        vols = s["volumes"]
        assert "./killdir:/freqtrade/killdir:ro" in vols
        assert not any(v.startswith("../ops:") for v in vols)  # never mount all of ops/
        assert s["environment"]["EARN_RISKGATE"] == "/freqtrade/earn-config/riskgate.json"
    a_ports = compose["services"]["freqtrade-a"]["ports"]
    b_ports = compose["services"]["freqtrade-b"]["ports"]
    assert a_ports == ["127.0.0.1:8080:8080"] and b_ports == ["127.0.0.1:8081:8080"]


def test_scaffold_source_has_no_signals():
    src = (REPO_ROOT / "strategies" / "Scaffold.py").read_text()
    assert '"enter_long"] = 0' in src and '"exit_long"] = 0' in src


def test_journal_module_is_stdlib_only():
    import ast

    tree = ast.parse(Path(REPO_ROOT / "strategies" / "_journal.py").read_text())
    stdlib_ok = {"json", "os", "sqlite3", "sys", "datetime", "pathlib", "__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] in stdlib_ok, a.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in stdlib_ok, node.module
