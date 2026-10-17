# Next prompt: Budget

Budget is the spend ledger, ceilings, usage-limit windows and brake decision split out of Jarvis
on 2026-10-17 (`~/.budget/DESIGN.md`). It is built and live; what is left is cleanup that waits on
Anze and a check of the first nights.

## When to fire this

Earliest-useful dates, not deadlines; being late costs nothing.

| When | Step | Waits on |
|---|---|---|
| 2026-10-18 08:00 | the night's jobs recorded into `state/budget.db` (`budget report`: LLM jobs 7d grew), `usage-refresh` hourly fresh, no `handler-failed` for `burn-check`, `usage-refresh`, `spend-by-effort` (Mon) | the first night |
| after that check, Anze | drop `provider_calls`, `job_calls`, `usage_samples` from `~/.jarvis/jarvis.db` and delete `~/.jarvis/jarvis.db.bak-budget` | Anze's confirmation |

## A: advance the work

> Read `~/.budget/DESIGN.md` and this file. Do the first row whose date has passed and report.
> Rules: Budget measures and answers; it never notifies, pings or holds work (Jarvis does).
> Recording never raises into the caller. The job budget gate is three-valued (2 = could not
> check, and an optional job then does not start). Never drop a table or delete a backup without
> Anze's confirmation. Stop and tell me if anything differs from what this file expects.

## B: status only

> Run `~/.budget/bin/budget report`, `curl -s 127.0.0.1:3025/health`, and
> `~/.budget/bin/install-launchd status`. Change nothing.

## C: something is broken

> Budget is broken. First, do not delete `state/budget.db` or a brake/limit file to "reset" it:
> they are the evidence. Copy the db with `sqlite3 .backup`, read `state/logs/server.log` and
> the daemon log, run `~/.budget/.venv/bin/python ~/.budget/tests/test_budget.py`, and report
> before fixing.
