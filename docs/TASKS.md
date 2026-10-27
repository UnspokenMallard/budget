# Fafnir tasks (from the 2026-10-27 audit)

- [ ] [medium] [underperforming] Set or calibrate the 5 h / 7 d token cap — `state/usage/status-config.json`, `fafnir/limits.py`, the gate in `fafnir` budget check — acceptance: `status.json` shows a non-null `budget_source` and `five_hour_used_pct`; `promises-extract` no longer defers at 1.92M tokens.
- [ ] [medium] [underperforming] Raise `wasted-spend` once per new failed run, with the cause — Fafnir wasted-spend rule — acceptance: one `alerts.log` line per new failed run, message names the cap or empty-output cause.
- [ ] [low] [improvement] Fix `$0.00` cost in job summaries (read `cost_usd` from `provider_call`) — `~/.jarvis/scripts/run-promises.py` and any similar summary — acceptance: summary prints non-zero list price.
- [ ] [low] [improvement] Decide whether `mind-improve-subject` belongs in `[budget_forecast] low_priority` — `~/.jarvis/config.toml` (Anze's edit) — acceptance: budget pauser can defer it.
- [ ] [low] [improvement] Make `tests/test_fafnir.py` runnable with one documented command — acceptance: prints a pass count.
- [ ] [low] [improvement] After Anze's yes, delete `state/budget.db` (0 B) and stale `state/usage` leftovers; add to `pending-confirmations.toml` first.
