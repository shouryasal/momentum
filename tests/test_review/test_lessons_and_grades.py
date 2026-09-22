"""lessons_tool: append-only, linter, 180-day archive, id monotonicity;
write_grades: computed rubric scores, schema rejection; outcome_stats floor."""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta

import pytest

from ops import db
from ops.config import REPO_ROOT, load_config

sys.path.insert(0, str(REPO_ROOT / ".claude" / "skills" / "post-mortem" / "scripts"))
import lessons_tool  # noqa: E402

NOW = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / ".claude" / "skills" / "post-mortem" / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestLessons:
    def _append(self, path, **over):
        kw = dict(week="2026-W39", title="Funding spike is not trend confirmation",
                  cause="reasoning", decisions="2026-09-23T08:30+04:00",
                  evidence_type="process",
                  lesson="A funding spike alone does not confirm a trend regime change.",
                  falsified_if="3 of the next 5 funding spikes precede a confirmed regime change",
                  now=NOW)
        kw.update(over)
        return lessons_tool.append(path, **kw)

    def test_append_lints_and_ids_monotonic(self, tmp_path):
        p = tmp_path / "lessons.md"
        p.write_text("# Earn lessons\n")
        assert self._append(p) == "L-2026-W39-01"
        assert self._append(p, title="Second") == "L-2026-W39-02"
        assert lessons_tool.lint(p.read_text()) == []

    def test_append_only_check(self, tmp_path):
        p = tmp_path / "lessons.md"
        p.write_text("# Earn lessons\n")
        self._append(p)
        old = p.read_text()
        mutated = old.replace("Funding spike", "EDITED")
        assert not lessons_tool.append_only_ok(old, mutated)
        assert lessons_tool.append_only_ok(old, old + "\n## more\n")

    def test_outcome_stats_lesson_needs_n(self, tmp_path):
        p = tmp_path / "lessons.md"
        p.write_text("")
        with pytest.raises(ValueError, match="stats-n"):
            self._append(p, evidence_type="outcome-stats", stats_n=12)
        self._append(p, evidence_type="outcome-stats", stats_n=35)

    def test_archive_moves_stale_entries(self, tmp_path):
        p, a = tmp_path / "lessons.md", tmp_path / "lessons-archive.md"
        p.write_text("# Earn lessons\n")
        self._append(p, title="Old one", now=NOW - timedelta(days=200))
        self._append(p, title="Fresh one", now=NOW)
        moved = lessons_tool.archive_stale(p, a, days=180, now=NOW)
        assert moved == ["L-2026-W39-01"]
        assert "Old one" in a.read_text()
        text = p.read_text()
        assert "Fresh one" in text and "Old one" not in text
        assert lessons_tool.lint(text) == []


class TestWriteGrades:
    @pytest.fixture
    def jdb(self, tmp_path):
        journal, _ = db.init_all(load_config(), root=tmp_path)
        conn = db.connect(journal)
        yield conn
        conn.close()

    def _grading(self, grade=75):
        return {
            "review_week": "2026-W39",
            "grades": [{"run_id": "2026-09-23T08:30+04:00", "process_grade": grade,
                        "rubric": {"thesis_consistent": True,
                                   "invalidation_stated_respected": True,
                                   "checklist_completed": True, "flags_honored": True,
                                   "abstain_when_stale": False,
                                   "lessons_applied": False}}],
            "root_causes": [{"event_id": "e-1", "kind": "gate_rejection",
                             "cause": "reasoning", "recurrence_key": "funding-whipsaw",
                             "fix_path": "decide skill checklist step 4",
                             "learn_eligible": False,
                             "eligibility_rule": "reasoning: 3 repeats required",
                             "evidence": {"gate_rows": [12]}}],
        }

    def test_write_and_computed_score(self, jdb):
        wg = _load("write_grades")
        n = wg.write(jdb, self._grading(), "claude-fable-5-1", "review-2026-W39", NOW)
        assert n == 1
        row = jdb.execute("SELECT * FROM decision_grades").fetchone()
        assert row["process_grade"] == 75  # computed from booleans
        rc = jdb.execute("SELECT * FROM root_cause_events").fetchone()
        assert rc["recurrence_key"] == "funding-whipsaw" and rc["learn_eligible"] == 0

    def test_asserted_grade_far_from_rubric_rejected(self, jdb):
        wg = _load("write_grades")
        with pytest.raises(ValueError, match="computed"):
            wg.write(jdb, self._grading(grade=95), "m", "r", NOW)

    def test_schema_rejects_bad_cause(self, jdb):
        import jsonschema

        wg = _load("write_grades")
        g = self._grading()
        g["root_causes"][0]["cause"] = "bad_luck"
        with pytest.raises(jsonschema.ValidationError):
            wg.write(jdb, g, "m", "r", NOW)


class TestOutcomeStats:
    def test_floor_refuses_stats(self, tmp_path):
        os_mod = _load("outcome_stats")
        cfg = load_config()
        journal, _ = db.init_all(cfg, root=tmp_path)
        with db.connect(journal) as jdb:
            # 5 resolved decisions: nav rows + proposals
            for i in range(5):
                rid = f"2026-09-{10 + i:02d}T08:30+04:00"
                jdb.execute("INSERT INTO proposals(run_id, shadow, ts_utc, valid,"
                            " horizon_days, confidence) VALUES (?,0,?,1,7,0.6)",
                            (rid, f"2026-09-{10 + i:02d}T04:30:00Z"))
            for d in range(8, 30):
                for sleeve, nav in (("a", 10000 + d), ("b", 10000 + 2 * d),
                                    ("benchmark", 10000 + d)):
                    jdb.execute("INSERT INTO nav_daily(date_utc, sleeve, nav_usdt)"
                                " VALUES (?,?,?)", (f"2026-09-{d:02d}", sleeve, nav))
            jdb.commit()
            result = os_mod.build(jdb, NOW)
        assert result["resolved_decisions"] == 5
        assert result["stats_citable"] is False and result["stats"] is None
        assert "refusal" in result
        assert all(o["vs_rules_bps"] is not None for o in result["per_decision"])
