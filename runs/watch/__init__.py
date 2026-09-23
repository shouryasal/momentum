"""The local watcher — a cheap, constant check on what Earn already holds.

Between decisions, nothing was watching. The scanner looks for reasons to *enter*; the
risk gate refuses bad orders; the validator reasons about signals. But a position opened on
Monday with a thesis and an invalidation condition attached sat unexamined until the next
scheduled research run, however loudly the world changed in between.

This package fills that gap with the cheapest thing that can do the job, and it is
deliberately structured so that the cheap thing is doing almost none of the work:

* :mod:`runs.watch.positions` computes every number — price against entry, stop and
  take-profit, unrealised P&L, time held, weight against target and against the gate's
  cap, distance to each limit. Pure Python, from Freqtrade's ledger and the knowledge DB.
* :mod:`runs.watch.invalidation` reads back the invalidation condition a strong model
  wrote and checks the numeric parts of it exactly. **A numeric invalidation that has
  fired needs no model at all** — it escalates on arithmetic.
* :mod:`runs.watch.headlines` collapses one story reported by several feeds into one, with
  ``nomic-embed-text``, so the generator is not billed three times for it.
* :mod:`runs.watch.prompt` renders one small prompt per holding — target ~1,500 tokens,
  measured on every call — carrying the position, the thesis in the strong model's own
  words, the host's check of the invalidation, and the surviving headlines.
* :mod:`runs.watch.runner` asks the local model the single fuzzy question: does anything
  here break that thesis, and how strongly.
* :mod:`runs.watch.policy` decides what wakes Claude, from config, with a cooldown.
* :mod:`runs.watch.guard` makes "it may only raise a hand" a property of the code.

What it cannot do, enforced rather than promised: place, size or cancel an order; write a
proposal; change config; move a flag. Its whole authority is a row in ``watch_events`` and
a request for escalation.
"""

from __future__ import annotations

__all__ = ["CycleReport", "HoldingResult", "WatchForbidden", "run_once"]


def __getattr__(name: str):
    """Lazy re-exports: importing the package must not drag in the LLM stack."""
    if name in ("CycleReport", "HoldingResult", "run_once"):
        from runs.watch import runner

        return getattr(runner, name)
    if name == "WatchForbidden":
        from runs.watch.guard import WatchForbidden

        return WatchForbidden
    raise AttributeError(name)
