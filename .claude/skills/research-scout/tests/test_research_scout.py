"""research-scout: the ledger is complete, the gates refuse, and the prose matches the code.

The regression this skill exists to prevent is a run spending a trial on something the repo
already measured and killed. So the tests that matter here are (a) every standing rejection
is actually in the ledger, (b) an idea phrased the way a run would phrase it *hits* it, and
(c) the tier-1 reference page and the tier-2 ledger have not drifted apart — because the
page is the half a model can edit.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_DIR.parents[2]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"research_scout_{name}",
                                                  SKILL_DIR / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: a frozen dataclass declared in a module that is not in
    # sys.modules makes dataclasses' InitVar resolution dereference None.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def scout():
    return load_script("scout")


def read_body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the body


def test_body_references_exist():
    body = read_body()
    for rel in re.findall(r"references/[\w.-]+\.md", body):
        assert (SKILL_DIR / rel).exists(), f"{rel} referenced but missing"
    for rel in re.findall(r"scripts/([\w.-]+\.py)", body):
        assert (SKILL_DIR / "scripts" / rel).exists(), f"scripts/{rel} missing"


def test_body_states_its_trigger_and_hard_stops():
    body = read_body().lower()
    assert "when this runs" in body
    assert "hard stop" in body


def test_body_forbids_measuring_and_directions():
    """The scout searches. Measuring is hypothesis-lab; direction is decide, at tier 4."""
    body = read_body().lower()
    assert "never emit a direction" in body
    assert "never measures anything" in body
    assert "hypothesis-lab" in body


def test_body_names_the_four_gates():
    body = read_body().lower()
    for gate in ("free", "keyless", "lag", "backtestable"):
        assert gate in body, gate


# --------------------------------------------------------------------------- the ledger


def test_every_standing_rejection_is_in_the_ledger(scout):
    """The five the owner named, by id. Losing one means rediscovering a dead end."""
    for rid in ("onchain-flows", "hash-ribbons", "etf-flow-proxy", "unlock-calendar",
                "execution-timing"):
        assert rid in scout.LEDGER, rid


def test_every_entry_carries_the_measurement_that_killed_it(scout):
    for rej in scout.LEDGER.values():
        assert rej.keywords, f"{rej.id}: no keywords, so nothing can ever match it"
        assert len(rej.reason) >= 80, f"{rej.id}: a one-line reason is an opinion"
        assert rej.evidence, f"{rej.id}: no evidence pointer"
        assert rej.verdict in (scout.REJECTED, scout.OBSERVE_ONLY), rej.id


@pytest.mark.parametrize("idea, want_id", [
    ("does exchange netflow predict next week's return", "onchain-flows"),
    ("look at whale deposits to exchanges", "onchain-flows"),
    ("hash ribbon miner capitulation as a bottom signal", "hash-ribbons"),
    ("use the US hours vs Asia hours spread as an ETF flow proxy", "etf-flow-proxy"),
    ("build an unlock calendar for the satellites", "unlock-calendar"),
    ("optimise entry timing with a limit maker order", "execution-timing"),
    ("sell when funding is high because the market is overheated",
     "funding-as-direction"),
    ("buy the OI flush after a deleveraging event", "oi-deleveraging-buy"),
    ("is there alpha in cumulative volume delta", "taker-imbalance"),
    ("does the CME gap fill", "cme-gap"),
    ("train an LSTM on OHLCV", "deep-learning-ohlcv"),
    ("fit a hidden markov regime classifier", "hmm-regime"),
    ("meta-labelling the primary signal with a secondary model", "meta-labelling"),
    ("backtest the top 100 coins by volume", "survivorship-universe"),
])
def test_an_idea_phrased_the_way_a_run_would_phrase_it_hits_the_ledger(scout, idea,
                                                                      want_id):
    res = scout.check_idea(idea)
    assert res["verdict"] == scout.REJECTED, res
    assert want_id in [h["id"] for h in res["hits"]], res["hits"]


def test_a_genuinely_new_idea_is_open(scout):
    res = scout.check_idea(
        "does the forecast drawdown from leverage-state tell us how wide a stop "
        "should be on an open position")
    assert res["verdict"] == scout.OPEN
    assert res["hits"] == []


def test_the_liquidation_stream_is_observe_only_not_rejected(scout):
    """Forward-only is a real answer: record it now, decide on it in a year."""
    res = scout.check_idea("record the forceorder liquidation stream")
    assert res["verdict"] == scout.OBSERVE_ONLY


def test_a_rejection_outranks_an_observe_only_hit(scout):
    res = scout.check_idea("use liquidations and the cme gap together")
    assert res["verdict"] == scout.REJECTED


# --------------------------------------------------------------------------- the gates


def test_a_thirty_day_endpoint_fails_the_depth_gate(scout):
    """The one that silently ships a one-month backtest."""
    res = scout.gate_source("openInterestHist", free=True, keyless=True, history_days=31,
                            lag_min=5, backtestable=True)
    assert not res.usable
    assert res.checks["deep_enough"] is False
    assert any("730" in r for r in res.reasons)


def test_a_paywalled_keyed_source_fails_twice(scout):
    res = scout.gate_source("etf-flows", free=False, keyless=False, history_days=3000,
                            lag_min=60, backtestable=True)
    assert not res.usable
    assert res.checks["free"] is False and res.checks["keyless"] is False
    assert len(res.reasons) == 2


def test_a_seven_day_delay_fails_the_lag_gate(scout):
    res = scout.gate_source("sopr", free=True, keyless=True, history_days=3000,
                            lag_min=7 * 24 * 60, backtestable=True)
    assert res.checks["low_lag"] is False


def test_a_forward_only_stream_fails_the_backtestable_gate(scout):
    res = scout.gate_source("forceorder-ws", free=True, keyless=True, history_days=0,
                            lag_min=1, backtestable=False)
    assert res.checks["backtestable"] is False
    assert any("point-in-time" in r for r in res.reasons)


def test_a_good_source_passes_all_four(scout):
    res = scout.gate_source("deribit-dvol", free=True, keyless=True, history_days=2010,
                            lag_min=1440, backtestable=True)
    assert res.usable and all(res.checks.values()) and res.reasons == []


def test_the_gate_is_all_four_not_a_score(scout):
    """Four passes and one failure is a rejection, not 80%."""
    res = scout.gate_source("nearly", free=True, keyless=True, history_days=3000,
                            lag_min=10, backtestable=False)
    assert not res.usable


# --------------------------------------------------------------------- code vs prose


def test_the_reference_page_and_the_ledger_have_not_drifted(scout):
    """references/ is TIER 1 — a model may edit it. scripts/ is tier 2 and is the
    authority. A rejection quietly dropped from the prose is exactly how a dead end comes
    back, so the two must name the same ids."""
    page = (SKILL_DIR / "references" / "rejected-ledger.md").read_text(encoding="utf-8")
    named = set(re.findall(r"`([a-z0-9-]+)`", page))
    missing = sorted(set(scout.LEDGER) - named)
    assert not missing, f"ledger ids absent from rejected-ledger.md: {missing}"


def test_the_surfaces_list_covers_the_local_stores(scout):
    names = {s["name"] for s in scout.SURFACES}
    assert {"local-candles", "wired-features", "news-archive"} <= names
    for surface in scout.SURFACES:
        assert surface["notes"] and surface["history"] and surface["where"]


def test_the_unavailable_list_comes_from_the_wired_registry(scout):
    """The scout must not keep its own copy of what is paywalled: runs/features owns it."""
    view = scout.registry_view()
    if not view.get("available"):
        pytest.skip(f"runs.features not importable here: {view.get('error')}")
    assert {"us_spot_etf_flows", "historical_liquidations",
            "realtime_sopr_nupl"} <= set(view["unavailable"])


# --------------------------------------------------------------------------- script


def test_the_self_test_passes(scout):
    assert scout.self_test() == 0
