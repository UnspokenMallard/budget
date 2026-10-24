# Next prompt: Budget

Budget is the spend ledger, ceilings, usage-limit windows and brake decision split out of Jarvis
on 2026-10-17 (`~/.budget/DESIGN.md`). It is built and live; what is left is cleanup that waits on
Anze and a check of the first nights.

## When to fire this

Earliest-useful dates, not deadlines; being late costs nothing.

| When | Step | Waits on |
|---|---|---|
| 2026-10-18 08:00 | the night's jobs recorded into Mimir (`budget report`: LLM jobs 7d grew), `usage-refresh` hourly fresh, no `handler-failed` for `burn-check`, `usage-refresh`, `spend-by-effort` (Mon) | the first night |
| after that check, Anze | drop `provider_calls`, `job_calls`, `usage_samples` from `~/.jarvis/jarvis.db` and delete `~/.jarvis/jarvis.db.bak-budget` | Anze's confirmation |

## Audit 2026-10-21: your items

From the Jarvis audit (HUD task audit-jarvis-and-all-its-com-e5a9). The evidence, effort, risk and
done-criteria of each item are in `~/.jarvis/AUDIT-2026-10-21.md`; the phases and Anze's decisions
are in `~/.jarvis/NEXT-PROMPT.md`. Build an item only after Anze approves its phase. Prompt A below
covers these rows too. The rule stays: this component measures or knows; Jarvis alerts and acts.

- **A05 / D3 (Phase 0)** Budget paused nightly-reflection on 10-20 ($40.09 of $40), correctly by the
  rules. If Anze picks a reserved line for it, the ceiling logic here must honour it.
- **A31 (Phase 3)** Report "spend on failed runs" per job per week (about $15 in 14 days:
  nightly-reflection $7.24, remediate $4.06, checkups $2.79, weekly-activity $1.03). Count a run
  with exit 1 as failed even if the job says OK (weekly-activity did). Jarvis alerts above $5.
- **A35 (Phase 3)** Interactive sessions are invisible here: the context audit shows $1,261 a week
  above the 200k window against a $1,241 baseline; the ledger shows about $101 in 14 days and the
  last 7 days ($70) more than double the week before ($31). Report interactive spend beside jobs.
- **A38 (Phase 3)** token-report and usage-digest are designated to move here from Analyst.

**Status 2026-10-25 (HUD task freyr-398c, merge with Jarvis):** A35 (1b74815, 1985947) is built: the weekly token report shows all Claude Code spend, LLM job spend and the interactive difference per account. First 7-day figures: personal $420 vs $37 jobs, work $649 vs $21.

| When (earliest useful, not a deadline) | Step | Waits on |
|---|---|---|
| 2026-10-26 (after the weekly context-audit) | A35: compare with the 10-19 figure ($1,261/wk) | the weekly run |
| with Phase 3 | A31, then A35, then the A38 moves | Phase 3 approval |

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

> Budget is broken. First, do not delete `state/budget.db.archived-2026-10-24` or a brake/limit file to "reset" it:
> they are the evidence. Check `budget report` and `mimirrecords.pending('fafnir')` (an outbox that does not drain means Mimir is down), read `state/logs/server.log` and
> the daemon log, run `~/.budget/.venv/bin/python ~/.budget/tests/test_fafnir.py`, and report
> before fixing.
