"""Monday 02:00 Gulf self-maintenance: the system keeps its own Claude substrate
current, on evidence, with git-revertible steps.

Three phases, each isolated (a failing phase never skips the rest):

1. refresh_catalog — `anthropic.models.list()` (via ops.lib.claude_auth's probing
   client) -> knowledge/models_catalog.json with each model's supported effort
   levels. No usable credential -> info-level skip, never an error: models.list
   under an OAuth subscription token is not guaranteed.
2. detect_new_models + start_shadow — a catalog model that is (a) newer than the
   current decide model, (b) same-or-higher tier by the name-token heuristic
   haiku < sonnet < opus <= fable/mythos (NEVER lower), and (c) not already
   pinned, opens a 30-day shadow window in config/models-auto.yaml (tier 1) with
   a change_log row and a git commit. One active shadow max; promotion itself is
   review_run.grade_shadow's evidence-gated job.
3. upgrade_sdk — LAST, because it mutates the environment: pip-upgrade
   claude-agent-sdk within [maintenance.sdk_floor, maintenance.sdk_ceiling),
   full pytest, rollback to the exact prior version on red. This module never
   imports the SDK itself, so it keeps working while the SDK is broken.

Journals one runs row (stage 'maintenance'); status 'failed' when the upgrade
rolled back or a phase errored, so the healthcheck digest surfaces it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import claude_auth, locks, tg
from runs import router
from runs.common import atomic_write_json, guard_env, utc_iso

# name-token tier heuristic: candidates must be same-or-higher, never lower
TIER_TOKENS = (("haiku", 0), ("sonnet", 1), ("opus", 2), ("fable", 3), ("mythos", 3))


def model_tier(model_id: str) -> int | None:
    low = model_id.lower()
    for token, tier in TIER_TOKENS:
        if token in low:
            return tier
    return None


class Maintenance:
    def __init__(self, cfg: EarnConfig, jdb: sqlite3.Connection,
                 kdb: sqlite3.Connection, *, root: Path | None = None,
                 now: datetime | None = None, runner=None, alert=None,
                 client_factory=None, version_fn=None, git_runner=None):
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        self.root = root or REPO_ROOT
        self.now = now or datetime.now(UTC)
        self.run_id = f"maint-{self.now.strftime('%Y-%m-%d')}"
        self.runner = runner or self._real_runner
        self.alert = alert or (lambda text, sev="info": tg.send(text, sev, conn=kdb))
        self.client_factory = client_factory or claude_auth.anthropic_client
        self.version_fn = version_fn or self._installed_version
        self.git = git_runner or self._git
        self.errors: list[str] = []

    @staticmethod
    def _real_runner(cmd: list[str], timeout: int = 900) -> int:
        return subprocess.run(cmd, timeout=timeout, cwd=REPO_ROOT).returncode

    @staticmethod
    def _installed_version() -> str:
        import importlib.metadata

        return importlib.metadata.version("claude-agent-sdk")

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              text=True)

    # ---------------------------------------------------------------- catalog

    def refresh_catalog(self) -> dict | None:
        client = self.client_factory()
        if client is None:
            print("maintenance: no models.list-capable credential — catalog skip",
                  file=sys.stderr)
            return None
        models = []
        for m in client.models.list(limit=100):
            caps = getattr(m, "capabilities", None)
            created = getattr(m, "created_at", None)
            models.append({
                "id": m.id,
                "display_name": getattr(m, "display_name", None),
                "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ")
                if isinstance(created, datetime) else (str(created) if created else None),
                "effort": list(getattr(caps, "effort", None) or []),
                "thinking": bool(getattr(caps, "thinking", False)),
            })
        catalog = {"refreshed_at": utc_iso(self.now), "models": models}
        atomic_write_json(self.root / "knowledge" / "models_catalog.json", catalog)
        return catalog

    # ---------------------------------------------------------------- shadow

    def detect_new_models(self, catalog: dict | None) -> str | None:
        if not catalog or not catalog.get("models"):
            return None
        mc = router.load_models_cfg(self.root / "config" / "models.yaml")
        if mc.get("shadow", {}).get("enabled"):
            return None  # one active shadow max
        current_id = mc["models"][mc["tasks"]["decide"]["model"]]
        cur_tier = model_tier(current_id)
        if cur_tier is None:
            return None
        by_id = {m["id"]: m for m in catalog["models"]}
        cur_created = (by_id.get(current_id) or {}).get("created_at")
        if not cur_created:
            return None  # can't establish "newer" — do nothing
        known = set(mc["models"].values())
        candidates = []
        for m in catalog["models"]:
            tier = model_tier(m["id"])
            if (m["id"] in known or tier is None or tier < cur_tier
                    or not m.get("created_at") or m["created_at"] <= cur_created):
                continue
            candidates.append((tier, m["created_at"], m["id"]))
        return max(candidates)[2] if candidates else None

    def start_shadow(self, model_id: str) -> None:
        key = "auto_" + re.sub(r"[^a-z0-9]+", "_", model_id.lower()).strip("_")
        days = self.cfg.maintenance.shadow_days
        started = self.now.strftime("%Y-%m-%d")

        def mutate(cur: dict) -> dict:
            cur.setdefault("models", {})[key] = model_id
            cur["shadow"] = {"enabled": True, "model": key, "started": started,
                             "days": days}
            return cur

        router.write_models_overlay(
            mutate, overlay_path=self.root / "config" / "models-auto.yaml")
        self.git("add", "config/models-auto.yaml")
        self.git("commit", "-m",
                 f"auto-shadow: open {days}-day window for {model_id}")
        sha = (self.git("rev-parse", "HEAD").stdout or "").strip() or None
        self.jdb.execute(
            "INSERT OR REPLACE INTO change_log(change_id, proposed_at, kind, target,"
            " status, author_model, decided_at, decided_by, reason, merge_commit)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"shadow-{started}-{key}", utc_iso(self.now), "model",
             "config/models-auto.yaml", "auto_merged", "code:maintenance",
             utc_iso(self.now), "maintenance",
             json.dumps({"detected": model_id, "days": days}), sha))
        self.jdb.commit()
        self.alert(f"auto-shadow: {model_id} starts a {days}-day shadow window"
                   f" (promotion needs validity>=95%, agreement>=80%, zero limit"
                   f" violations)", "warn")

    # ---------------------------------------------------------------- sdk upgrade

    def upgrade_sdk(self) -> dict:
        m = self.cfg.maintenance
        prior = self.version_fn()
        spec = f"claude-agent-sdk>={m.sdk_floor},<{m.sdk_ceiling}"
        rc = self.runner([sys.executable, "-m", "pip", "install", "--upgrade",
                          "--quiet", spec], 900)
        if rc != 0:
            self.alert(f"sdk upgrade: pip failed (rc={rc}); still on {prior}", "warn")
            return {"status": "pip_failed", "from": prior, "to": prior}
        new = self.version_fn()
        if new == prior:
            return {"status": "unchanged", "from": prior, "to": prior}
        rc = self.runner([sys.executable, "-m", "pytest", "tests", "-q"], 1800)
        if rc != 0:
            self.runner([sys.executable, "-m", "pip", "install", "--quiet",
                         f"claude-agent-sdk=={prior}"], 900)
            self.alert(f"sdk upgrade {prior} -> {new} FAILED tests (rc={rc});"
                       f" rolled back to {prior}", "critical")
            return {"status": "rolled_back", "from": prior, "to": new}
        self.alert(f"sdk upgraded {prior} -> {new}; full suite green", "info")
        return {"status": "upgraded", "from": prior, "to": new}

    # ---------------------------------------------------------------- flow

    def _phase(self, name: str, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — phases are isolated by design
            self.errors.append(f"{name}: {e}")
            print(f"maintenance phase {name} failed: {e}", file=sys.stderr)
            return None

    def journal(self, status: str, detail: str | None = None) -> None:
        self.jdb.execute(
            "INSERT OR REPLACE INTO runs(run_id, stage, kind, started_utc,"
            " finished_utc, status, error) VALUES (?,?,?,?,?,?,?)",
            (self.run_id, "maintenance", "maintenance", utc_iso(self.now),
             utc_iso(), status, detail))
        self.jdb.commit()

    def main_flow(self) -> int:
        from ops.lib import kill as killlib

        if killlib.is_engaged(self.cfg, self.root):
            self.journal("killed")
            return 0
        catalog = self._phase("catalog", self.refresh_catalog)
        candidate = self._phase("detect", lambda: self.detect_new_models(catalog))
        if candidate:
            self._phase("shadow", lambda: self.start_shadow(candidate))
        upgrade = self._phase("upgrade", self.upgrade_sdk) or {"status": "error"}
        failed = bool(self.errors) or upgrade["status"] == "rolled_back"
        detail = "; ".join([f"sdk={upgrade['status']}",
                            *(self.errors or [])])[:500]
        self.journal("failed" if failed else "success", detail)
        return 1 if failed else 0


def main() -> int:
    guard_env()
    cfg = load_config()
    with locks.acquire("maintenance"):
        with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb, \
                db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
            return Maintenance(cfg, jdb, kdb).main_flow()


if __name__ == "__main__":
    sys.exit(main())
