"""Compute the Earn volatility surface — sigma_hat, the position scalar, and the skew.

Deterministic: every number here is produced by `runs.features.volatility` from the local
feather candles and the cached Deribit DVOL series. Nothing in this file estimates a
volatility, and no model is involved.

Writes `knowledge/state/volsurface.json` and prints a one-line-per-pair summary.

    python3 compute_volsurface.py                 # refresh the DVOL cache, then compute
    python3 compute_volsurface.py --offline       # cache only, never touch the network
    python3 compute_volsurface.py --self-test     # frozen fixture, no data, no network

Definitions and the measurements behind every default are pinned in
`../references/definitions.md`. If this script and that file disagree, this script is
wrong — raise it in the weekly review rather than describing around it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from runs.features import (  # noqa: E402
    deribit,
    iso,
    knowledge_dir,
    load_candles,
    utcnow,
    write_json_atomic,
)
from runs.features import volatility as vol  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parents[1]
FIXTURE_DIR = SKILL_DIR / "tests" / "fixtures"

#: Fixture-mode settings, pinned so the self-test is a golden-value test and not a smoke test.
FIXTURE_MIN_TRAIN = 365
FIXTURE_TARGET_ANNUAL = 30.0


def _target_annual() -> float:
    """The live sizing target in vol POINTS, from the tier-1 sleeve params.

    Read, never restated: the limit lives in the sleeve params file and this script only
    converts the fraction to points. A hard-coded 30.0 here would be a second source of
    truth that silently disagrees the first time somebody tunes the real one.
    """
    from ops.config import load_config

    cfg = load_config()
    target = float(cfg.sleeve_a.vol.target_annual)
    try:
        raw = json.loads((REPO_ROOT / cfg.paths.params_a).read_text(encoding="utf-8"))
        target = float(raw["params"]["vol"]["target_annual"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return target * 100.0


def _pairs() -> list[str]:
    from ops.config import load_config

    return list(load_config().universe.pairs)


def _currency(pair: str) -> str:
    return pair.split("/")[0].upper()


def compute(pairs: list[str], *, target_annual: float, offline: bool,
            now=None) -> dict:
    """The whole payload. One entry per pair plus a shared `meta` block."""
    ref = now or utcnow()
    out: dict = {"computed_utc": iso(ref), "target_annual_pts": round(target_annual, 4),
                 "pairs": {}, "meta": {"offline": offline}}
    for pair in pairs:
        cur = _currency(pair)
        fetched = False
        if not offline:
            _, fetched = deribit.update_dvol_cache(cur, now=ref)
        series = deribit.load_dvol(cur)
        try:
            candles = load_candles(pair, "4h")
        except (FileNotFoundError, ValueError) as exc:
            out["pairs"][pair] = {"pair": pair, "sigma_hat": None, "source": "none",
                                  "refused": f"no candles: {exc}", "stale": True}
            continue
        f = vol.forecast(pair, candles, series if not series.empty else None,
                         target_annual=target_annual)
        row = f.as_dict()
        row["dvol_fetch_ok"] = bool(fetched)
        row["dvol_rows_cached"] = int(len(series))
        out["pairs"][pair] = row
    return out


def add_skew(payload: dict, currency: str = "BTC", *, offline: bool) -> dict:
    """Attach the 25-delta surface, labelled observe_only.

    It is carried because six months from now it will have a self-collected history worth
    testing, and barred from sizing today because it has none. A number with no backtest is
    context, not an input.
    """
    if offline:
        payload["skew"] = {"observe_only": True, "as_of": None,
                           "note": "offline: option chain not fetched"}
        return payload
    chain = deribit.fetch_option_chain(currency)
    if chain is None:
        payload["skew"] = {"observe_only": True, "as_of": None, "stale": True,
                           "note": "Deribit option chain unreachable"}
        return payload
    summary = deribit.skew_25d(chain).as_dict()
    summary["currency"] = currency
    payload["skew"] = summary
    return payload


def _fixture_forecast():
    candles = pd.read_csv(FIXTURE_DIR / "btc_4h_close.csv")
    candles["date"] = pd.to_datetime(candles["date"], utc=True)
    dvol = pd.read_csv(FIXTURE_DIR / "btc_dvol_1d.csv")
    dvol["date"] = pd.to_datetime(dvol["date"], utc=True)
    return candles, dvol


def self_test() -> int:
    """Golden values on the frozen fixture — no candles store, no network, no config.

    This is what the eval cases run. It proves the four things that matter and that no unit
    test of a helper can prove on its own: the blend is used when DVOL is present, the HAR
    fallback is used when it is not, `sigma_hat` is never the raw DVOL level, and a
    withheld forecast still leaves a cautious scalar behind rather than none.
    """
    candles, dvol = _fixture_forecast()
    blend = vol.forecast("BTC/USDT", candles, dvol, target_annual=FIXTURE_TARGET_ANNUAL,
                         min_train=FIXTURE_MIN_TRAIN)
    har = vol.forecast("BTC/USDT", candles, None, target_annual=FIXTURE_TARGET_ANNUAL,
                       min_train=FIXTURE_MIN_TRAIN)
    stale_dvol = dvol.loc[dvol["date"] < pd.Timestamp("2025-06-01", tz="UTC")]
    stale = vol.forecast("BTC/USDT", candles, stale_dvol,
                         target_annual=FIXTURE_TARGET_ANNUAL, min_train=FIXTURE_MIN_TRAIN)
    rv = vol.realized_variance_daily(candles)
    panel = rv[["date"]].copy()
    panel["fwd"] = vol.forward_vol(rv, 7)
    panel["trail"] = vol.trailing_vol(rv, 30)
    panel = panel.merge(dvol[["date", "close"]].set_axis(["date", "dvol"], axis=1),
                        on="date", how="inner").dropna()
    fitted = vol.fit_dvol_map(panel["dvol"], panel["fwd"])
    print(f"fixture rows={len(candles)} rv_days={len(rv)} panel={len(panel)}")
    print(f"blend sigma_hat={blend.sigma_hat} source={blend.source} "
          f"scalar={blend.vol_target_scalar}")
    print(f"har   sigma_hat={har.sigma_hat} source={har.source} "
          f"scalar={har.vol_target_scalar}")
    print(f"stale sigma_hat={stale.sigma_hat} source={stale.source} stale={stale.stale}")
    print(f"map a={fitted.a:.4f} b={fitted.b:.4f} at_dvol_60={fitted.apply(60):.2f} "
          f"raw_would_be=60.00")
    withheld = vol.forecast("BTC/USDT", candles, dvol, target_annual=FIXTURE_TARGET_ANNUAL,
                            min_train=FIXTURE_MIN_TRAIN, min_oos_r2=0.99)
    print(f"withheld sigma_hat={withheld.sigma_hat} scalar={withheld.vol_target_scalar} "
          f"scalar_source={withheld.scalar_source} degraded={withheld.degraded}")
    scalars = (blend.vol_target_scalar, har.vol_target_scalar, withheld.vol_target_scalar)
    print(f"scalar_cap_ok={all(s is None or s <= 1.0 for s in scalars)}")
    ok = (blend.source == "blend" and har.source == "har" and stale.source == "har"
          and stale.stale and fitted.apply(60) < 55.0
          and (blend.vol_target_scalar or 0) <= 1.0
          and withheld.sigma_hat is None and withheld.degraded
          and withheld.vol_target_scalar is not None
          and withheld.vol_target_scalar <= 1.0)
    print("self_test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def summarise(payload: dict) -> str:
    lines = []
    for pair, row in payload["pairs"].items():
        if row.get("sigma_hat") is None:
            lines.append(
                f"{pair}: NO FORECAST — {row.get('refused', 'unknown')} "
                f"| degraded scalar={row.get('vol_target_scalar')} "
                f"({row.get('scalar_source')}) trailing30d={row.get('trailing_vol_30d')}%")
            continue
        lines.append(
            f"{pair}: sigma_hat={row['sigma_hat']}% ({row['source']}) "
            f"scalar={row['vol_target_scalar']} dvol={row['dvol_last']} "
            f"pctile_2y={row['dvol_pctile_2y']} vrp={row['vrp']} "
            f"trailing30d={row['trailing_vol_30d']}% oos_r2_250d={row['oos_r2_250d']}"
            + (" STALE" if row.get("stale") else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true",
                    help="use the cached DVOL series only; never reach the network")
    ap.add_argument("--self-test", action="store_true",
                    help="golden values on the frozen fixture; writes nothing")
    ap.add_argument("--pairs", help="comma-separated override of the universe pairs")
    ap.add_argument("--json", action="store_true", help="print the payload as JSON")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    pairs = [p.strip() for p in args.pairs.split(",")] if args.pairs else _pairs()
    payload = compute(pairs, target_annual=_target_annual(), offline=args.offline)
    payload = add_skew(payload, _currency(pairs[0]), offline=args.offline)
    out_path = knowledge_dir() / "state" / "volsurface.json"
    write_json_atomic(out_path, payload)
    print(json.dumps(payload, indent=2) if args.json else summarise(payload))
    print(f"wrote {out_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
