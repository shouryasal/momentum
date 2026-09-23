"""The round-trip editor: comments survive, key order survives, only the edited node moves."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops import config_io

SAMPLE = """\
# Earn limits. This header comment must survive every edit.
meta:
  config_version: 2          # trailing comment on a scalar
  display_timezone: "Asia/Dubai"

risk:
  # per-asset caps, as fractions of NAV
  max_weight: { BTC: 0.60, ETH: 0.40, default: 0.25 }
  usdt_floor: 0.10           # never spend the last tenth
  blackout:
    enabled: true
    window_minutes: 30

research:
  slots: ["08:30", "16:00"]  # the single source of research fire times
  deadline_s: 2400
"""


@pytest.fixture()
def doc(tmp_path: Path) -> config_io.Document:
    p = tmp_path / "earn.yaml"
    p.write_text(SAMPLE, encoding="utf-8")
    return config_io.read_document(p, rel="config/earn.yaml")


def test_scalar_edit_is_byte_for_byte_outside_the_node(doc: config_io.Document) -> None:
    result = config_io.apply_patch(doc, [{"op": "replace", "path": "/risk/usdt_floor",
                                          "value": 0.15}])
    assert result.surgical is True
    assert result.reflowed is False
    assert "0.15" in result.text
    # everything else, including both comments on that line's neighbours, is identical
    before = doc.text.replace("usdt_floor: 0.10", "usdt_floor: 0.15")
    assert result.text == before
    assert "# Earn limits. This header comment must survive every edit." in result.text
    assert "# never spend the last tenth" in result.text


def test_nested_edit_keeps_comments_and_order(doc: config_io.Document) -> None:
    result = config_io.apply_patch(
        doc, [{"op": "replace", "path": "/risk/blackout/window_minutes", "value": 45}]
    )
    assert result.surgical
    assert "# per-asset caps, as fractions of NAV" in result.text
    lines = [line.split(":")[0].strip() for line in result.text.splitlines() if line.strip()]
    assert lines.index("risk") < lines.index("research")
    assert "window_minutes: 45" in result.text


def test_flow_mapping_edit_keeps_padding_and_siblings(doc: config_io.Document) -> None:
    result = config_io.apply_patch(
        doc,
        [{"op": "replace", "path": "/risk/max_weight",
          "value": {"BTC": 0.5, "ETH": 0.4, "default": 0.25}}],
    )
    assert result.surgical
    assert "max_weight: { BTC: 0.5, ETH: 0.4, default: 0.25 }" in result.text
    assert "# per-asset caps, as fractions of NAV" in result.text


def test_flow_sequence_edit_keeps_quotes(doc: config_io.Document) -> None:
    result = config_io.apply_patch(
        doc, [{"op": "replace", "path": "/research/slots", "value": ["09:00", "17:00"]}]
    )
    assert result.surgical
    assert '["09:00", "17:00"]' in result.text
    assert "# the single source of research fire times" in result.text


def test_quoted_string_keeps_its_quote_style(doc: config_io.Document) -> None:
    result = config_io.apply_patch(
        doc, [{"op": "replace", "path": "/meta/display_timezone", "value": "Europe/London"}]
    )
    assert '"Europe/London"' in result.text
    assert "# trailing comment on a scalar" in result.text


def test_dotted_paths_are_accepted(doc: config_io.Document) -> None:
    result = config_io.apply_patch(doc, [{"op": "replace", "path": "risk.usdt_floor",
                                          "value": 0.2}])
    assert result.changed == ["risk.usdt_floor"]


def test_structural_edit_falls_back_but_keeps_comments(doc: config_io.Document) -> None:
    result = config_io.apply_patch(
        doc, [{"op": "add", "path": "/risk/new_limit", "value": 7}]
    )
    assert result.surgical is False
    assert result.reflowed is True
    assert "new_limit: 7" in result.text
    assert "# Earn limits. This header comment must survive every edit." in result.text
    assert "# never spend the last tenth" in result.text


def test_remove_is_structural_and_drops_the_key(doc: config_io.Document) -> None:
    result = config_io.apply_patch(doc, [{"op": "remove", "path": "/research/deadline_s"}])
    assert "deadline_s" not in result.text
    assert result.changed == ["research.deadline_s"]


def test_replace_on_a_missing_key_is_refused(doc: config_io.Document) -> None:
    with pytest.raises(config_io.PatchError):
        config_io.apply_patch(doc, [{"op": "replace", "path": "/risk/nope", "value": 1}])


def test_changed_paths_reports_adds_removes_and_edits() -> None:
    before = {"a": 1, "b": {"c": 2}, "d": [1, 2]}
    after = {"a": 1, "b": {"c": 3}, "e": 5}
    assert config_io.changed_paths(before, after) == ["b.c", "d.0", "d.1", "e"]


def test_flatten_and_pointer_round_trip() -> None:
    data = {"risk": {"max_weight": {"BTC": 0.6}}, "slots": ["08:30"]}
    flat = config_io.flatten(data)
    assert flat == {"risk.max_weight.BTC": 0.6, "slots.0": "08:30"}
    ptr = config_io.pointer(["risk", "max_weight", "BTC"])
    assert ptr == "/risk/max_weight/BTC"
    assert config_io.dotted(ptr) == "risk.max_weight.BTC"
    assert config_io.get_at(data, ptr) == 0.6


def test_pointer_escapes() -> None:
    assert config_io.pointer_parts("/a~1b/c~0d") == ["a/b", "c~d"]
    assert config_io.pointer(["a/b"]) == "/a~1b"


def test_raw_mode_replaces_the_whole_document(doc: config_io.Document) -> None:
    result = config_io.apply_raw(doc, "meta:\n  config_version: 2\n")
    assert result.text.endswith("\n")
    assert "risk.usdt_floor" in result.changed


def test_json_documents_are_reemitted_sorted(tmp_path: Path) -> None:
    p = tmp_path / "params.json"
    p.write_text(json.dumps({"sleeve": "a", "params": {"b": 2, "a": 1}}), encoding="utf-8")
    doc = config_io.read_document(p, rel="config/params-sleeve-a.json")
    result = config_io.apply_patch(doc, [{"op": "replace", "path": "/params/a", "value": 9}])
    assert json.loads(result.text)["params"]["a"] == 9
    assert result.text.index('"params"') < result.text.index('"sleeve"')


def test_atomic_write_replaces_and_keeps_mode(tmp_path: Path) -> None:
    target = tmp_path / "x.yaml"
    config_io.atomic_write(target, "a: 1\n")
    assert target.read_text(encoding="utf-8") == "a: 1\n"
    config_io.atomic_write(target, "a: 2\n")
    assert target.read_text(encoding="utf-8") == "a: 2\n"
    assert not list(tmp_path.glob(".x.yaml*"))


def test_diff_text_is_a_unified_diff(doc: config_io.Document) -> None:
    result = config_io.apply_patch(doc, [{"op": "replace", "path": "/risk/usdt_floor",
                                          "value": 0.2}])
    diff = config_io.diff_text(doc.text, result.text, rel="config/earn.yaml")
    assert diff.startswith("--- a/config/earn.yaml")
    assert "-  usdt_floor: 0.10" in diff
    assert "+  usdt_floor: 0.2" in diff


def test_sha_changes_with_content(doc: config_io.Document) -> None:
    result = config_io.apply_patch(doc, [{"op": "replace", "path": "/risk/usdt_floor",
                                          "value": 0.2}])
    assert result.sha != doc.sha
    assert len(doc.sha) == 64


def test_real_earn_yaml_survives_a_scalar_edit() -> None:
    """The committed config is the case that matters: 250+ comments, mixed flow styles."""
    src = config_io.read_document(
        Path(__file__).resolve().parents[2] / "config" / "earn.yaml", rel="config/earn.yaml"
    )
    result = config_io.apply_patch(
        src, [{"op": "replace", "path": "/console/session_hours", "value": 9}]
    )
    assert result.surgical
    assert result.text.count("#") == src.text.count("#")
    assert len(result.text.splitlines()) == len(src.text.splitlines())
    assert "session_hours: 9" in result.text
