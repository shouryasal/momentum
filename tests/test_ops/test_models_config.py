"""`config/models.yaml` must be internally coherent, not merely loadable.

Both checks here come from the model-routing audit of 2026-10-01, and both are about numbers
that were *valid* on their own and contradictory together — the shape of defect a schema cannot
catch and a reader does not notice.
"""

from __future__ import annotations

import re

from ops.models_config import load_models_cfg


def test_the_per_task_budgets_do_not_exceed_the_total():
    """A total below the sum of the parts is a ceiling that cannot be enforced.

    Found 2026-10-01: the per-task `monthly_budget_usd` rows summed to $292 against a
    `budget.monthly_total_usd` of 150, so measured spend of $217.70/30d read as a 45% overrun
    while sitting inside every per-task cap. Nothing enforced either number — `budget.mode` is
    `telemetry` and dollars are notional under the Max subscription — but an incoherent pair
    means `mode: hard` could never be switched on safely, which is the point of having it.
    """
    cfg = load_models_cfg()
    per_task = {name: float(getattr(t, "monthly_budget_usd", 0) or 0)
                for name, t in cfg.tasks.items()}
    per_task = {k: v for k, v in per_task.items() if v}
    total = float(cfg.budget.monthly_total_usd)
    assert sum(per_task.values()) <= total, (
        f"per-task caps sum to {sum(per_task.values()):.0f} against a total of {total:.0f}: "
        f"{per_task}")


def test_a_dated_pin_is_a_deliberate_exception_not_a_drift():
    """Dated ids are allowed, but only where the config says why.

    The 2026-10-01 audit proposed unpinning `haiku` to `claude-haiku-4-5` as tidiness. That was
    not taken: the dated id is the one the tooling documents and the one demonstrably served
    (135 calls, latest 2026-10-01T18:35:22Z), and an alias that does not resolve breaks every
    call on that tier for no measured benefit. So the rule is not "never pin a date" — it is
    "a dated pin is a decision somebody wrote down". This test fails when a NEW one appears.
    """
    allowed = {"haiku"}
    dated = {name for name, m in load_models_cfg().models.items()
             if re.search(r"-20\d{6}$", str(getattr(m, "id", m) or ""))}
    assert dated <= allowed, f"new dated pin(s) with no recorded reason: {dated - allowed}"
