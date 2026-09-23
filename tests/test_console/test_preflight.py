"""The console side of the preflight: the TTL cache and the probe wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from console.services import preflight_service as ps
from ops import preflight as pf
from ops.config import load_config

NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)


def _result(sleeve: str = "a", *, ok: bool = True, now: datetime = NOW) -> pf.PreflightResult:
    status = pf.PASS if ok else pf.FAIL
    return pf.PreflightResult(
        preflight_id=pf.new_preflight_id(),
        request=pf.PreflightRequest(sleeve, "LIVE_PROPOSE", "propose", 500.0),
        items=[pf.Check("kill_clear", "Kill", True, status, "")],
        created_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_utc=(now + timedelta(minutes=pf.PREFLIGHT_TTL_MINUTES)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    )


class TestCache:
    def test_a_result_is_retrievable_by_id(self):
        cache = ps.PreflightCache()
        result = _result()
        cache.put(result, now=NOW)
        assert cache.get(result.preflight_id, now=NOW) is result

    def test_an_expired_result_is_gone(self):
        cache = ps.PreflightCache()
        result = _result()
        cache.put(result, now=NOW)
        later = NOW + timedelta(minutes=pf.PREFLIGHT_TTL_MINUTES + 1)
        assert cache.get(result.preflight_id, now=later) is None
        # and it is evicted, not just hidden
        assert cache.get(result.preflight_id, now=NOW) is None

    def test_an_unknown_id_is_none(self):
        assert ps.PreflightCache().get("pf-nope", now=NOW) is None

    def test_the_cache_is_bounded(self):
        cache = ps.PreflightCache(limit=3)
        ids = []
        for i in range(5):
            result = _result()
            ids.append(result.preflight_id)
            cache.put(result, now=NOW + timedelta(seconds=i))
        assert cache.get(ids[0], now=NOW) is None
        assert cache.get(ids[-1], now=NOW) is not None

    def test_clear(self):
        cache = ps.PreflightCache()
        result = _result()
        cache.put(result, now=NOW)
        cache.clear()
        assert cache.get(result.preflight_id, now=NOW) is None


class TestProbes:
    def test_bot_status_reports_a_down_bot(self):
        cfg = load_config()

        class Down:
            def ping(self):
                return False

        probe = ps.bot_status_probe(cfg, lambda _cfg, _s: Down())
        assert probe("a") == {"up": False}

    def test_bot_status_reads_show_config(self):
        cfg = load_config()

        class Up:
            def ping(self):
                return True

            def _get(self, path):
                return {"strategy": "SleeveA", "dry_run": True, "state": "running",
                        "bot_name": "earn-a-test"}

        info = ps.bot_status_probe(cfg, lambda _cfg, _s: Up())("a")
        assert info["up"] and info["strategy"] == "SleeveA" and info["dry_run"] is True

    def test_bot_status_survives_a_client_without_show_config(self):
        cfg = load_config()

        class Old:
            def ping(self):
                return True

            def _get(self, path):
                raise RuntimeError("404")

        info = ps.bot_status_probe(cfg, lambda _cfg, _s: Old())("a")
        assert info["up"] and info["strategy"] is None

    def test_exchange_probes_need_keys(self, monkeypatch):
        monkeypatch.delenv("BINANCE_KEY_A", raising=False)
        monkeypatch.delenv("BINANCE_SECRET_A", raising=False)
        probes = ps.exchange_probes(env={})
        with pytest.raises(Exception, match="BINANCE_KEY_A"):
            probes["exchange_account"]("a")

    def test_host_facts_without_p1_are_unknown_not_false(self, monkeypatch):
        """An absent host-check module must read as "cannot determine", not "fine"."""
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "console.services" and args and "host_checks" in str(args[2]):
                raise ImportError("not landed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        facts = ps.host_facts_probe()
        assert facts.filesystem is None and facts.docker_running is None

    def test_telegram_probe_reports_delivery(self):
        cfg = load_config()
        sent: list[tuple] = []

        def fake_send(text, severity, **kwargs):
            sent.append((text, severity))
            return True

        ok, detail = ps.telegram_probe(cfg, send=fake_send)()
        assert ok and "delivered" in detail and sent

    def test_telegram_probe_reports_failure(self):
        cfg = load_config()
        ok, detail = ps.telegram_probe(cfg, send=lambda *a, **k: False)()
        assert not ok and "not delivered" in detail


class TestRun:
    def test_injected_deps_bypass_the_real_probes_and_cache(self, env):
        cfg = load_config()
        cache = ps.PreflightCache()
        deps = pf.PreflightDeps(root=env, now=NOW)
        result = ps.run(
            cfg, pf.PreflightRequest("a", "LIVE_PROPOSE", "propose", 500.0),
            root=env, bot_factory=lambda *a: None, deps=deps, store=cache, now=NOW,
        )
        assert cache.get(result.preflight_id, now=NOW) is result
        # every probe was missing, so nothing passed by accident
        assert not result.ok

    def test_a_real_run_without_databases_still_answers(self, env):
        cfg = load_config()
        result = ps.run(
            cfg, pf.PreflightRequest("a", "LIVE_PROPOSE", "propose", 500.0),
            root=env, bot_factory=lambda *a: None, env={}, now=NOW,
            store=ps.PreflightCache(),
        )
        assert len(result.items) == len(pf.CHECK_ORDER)
        assert not result.ok


class TestHostFactsAdapter:
    """``host_facts_probe`` is the bridge from ``host_checks`` rows to ``HostFacts``.

    It had been looking for module-level names ``host_checks`` never exported, so every
    fact came back ``None`` and preflight check 13 failed with "docker is not running"
    on a host where docker was fine. These pin the mapping in both directions.
    """

    def _runner(self, table):
        def run(argv, timeout=10.0):
            for needle, result in table.items():
                if any(needle in str(a) for a in argv):
                    return result
            return (1, "", "not stubbed")

        return run

    def test_a_healthy_host_reads_as_healthy(self, monkeypatch):
        from console.services import host_checks

        checks = [
            host_checks.HostCheck("filesystem", host_checks.OK, "ext4",
                                  data={"fstype": "ext4"}),
            host_checks.HostCheck("sleep_ac", host_checks.OK, "never sleeps"),
            host_checks.HostCheck("keepalive_task", host_checks.OK, "ready"),
            host_checks.HostCheck("docker", host_checks.OK, "engine 27"),
            host_checks.HostCheck("ntp", host_checks.OK, "synced"),
        ]
        monkeypatch.setattr(host_checks, "collect", lambda cfg=None, **kw: checks)
        monkeypatch.setattr(ps, "_cron_facts", lambda cfg: (True, True))
        facts = ps.host_facts_probe()
        assert facts.filesystem == "ext4"
        assert facts.sleep_on_ac_disabled is True
        assert facts.keepalive_task is True
        assert facts.docker_running is True
        assert facts.ntp_skew_s == 0.0
        assert facts.cron_installed is True and facts.cron_matches is True

    def test_a_warn_is_unknown_and_a_fail_is_false(self, monkeypatch):
        """The distinction matters: preflight blocks on ``not True``, so a probe it could
        not run must not read as a confident ``False`` in the evidence either."""
        from console.services import host_checks

        checks = [
            host_checks.HostCheck("sleep_ac", host_checks.WARN, "interop disabled"),
            host_checks.HostCheck("docker", host_checks.FAIL, "not answering"),
        ]
        monkeypatch.setattr(host_checks, "collect", lambda cfg=None, **kw: checks)
        monkeypatch.setattr(ps, "_cron_facts", lambda cfg: (None, None))
        facts = ps.host_facts_probe()
        assert facts.sleep_on_ac_disabled is None
        assert facts.docker_running is False
        assert facts.keepalive_task is None

    def test_a_collect_that_explodes_is_unknown_not_a_crash(self, monkeypatch):
        from console.services import host_checks

        def boom(cfg=None, **kw):
            raise OSError("no findmnt here")

        monkeypatch.setattr(host_checks, "collect", boom)
        facts = ps.host_facts_probe()
        assert facts.to_json() == pf.HostFacts().to_json()
