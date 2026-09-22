# Overfitting and drift guards

- **Expanding windows.** Walk-forward IS spans grow, OOS spans are fixed
  (6 months). A candidate that wins in-sample but loses out-of-sample is rejected
  automatically by apply_changes — do not present it.
- **Sensitivity.** Re-run the backtest at the changed parameter ± one `max_step`.
  If the sign of the improvement flips, the result is noise; stop.
- **Step limits.** `bounds.max_step` caps how far a parameter moves per change;
  ≤ 2 parameter changes per month total. Slow is the design.
- **One variable at a time.** A change proposal carries at most 2 parameters
  (schema-enforced) and one hypothesis.
- **Costs are measured.** Backtests use `config/backtest.yaml` values (TCA-
  calibrated monthly). If measured costs are frozen (tier1_freeze), parameter
  changes hold until the gap is explained.
- **Lessons decay.** A lesson not re-confirmed in 180 days is archived; a change
  justified only by an archived lesson needs the lesson re-established first.
