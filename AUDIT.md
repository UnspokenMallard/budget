# Budget (Fafnir) audit, 2026-11-01

Audited by Vali (HUD task vali-8a78), branch `hud/vali-8a78`. Scope: usage collection, the 5-hour window and caps, forecasts, burn-check, the brake, per-chat spend ceilings, the usage digest. Read-only against live state; nothing in `~/.jarvis/config.toml`, secrets or other components was touched, and no spend limit was changed.

## What I checked

- Code: `fafnir/{ledger,brake,store,quota,limits,cli}.py`, `server/app.py`, `fafnir/jobs/{update_status,update-usage,update-usage-job,usage-digest,spend-by-effort}.py`, `DESIGN.md`, `docs/AUDIT-2026-10-27.md`, `docs/TASKS.md`, `config.toml`.
- Live data through the Mimir ledger (`fafnir.provider_call`, `job_call`, `usage_sample`, `job_reject`), `state/brake*.json`, `state/usage/*.json`, `state/claude-observed-ceilings.json`, and the limit-hit samples.
- Tests: `~/.budget/.venv/bin/python tests/test_fafnir.py` (6 passed before, 9 passed after). `~/.jarvis/jarvis.sh doctor`: budget `pass` (:3025 http 200).
- Not read: `context-audit.py`, `token-report.py` (the latter is exercised by the existing test).

## Verification of the four observations

1. **BURN / BURN-CALLS repeated with no attribution: confirmed, and attribution fixed.** Every window on 2026-11-01 (06:15, 12:00 to 13:00, 13:05 to 14:00) came from Mind's improve loop, not the daemon: `mind-improve-subject` (Sonnet) and `mind-improve-judge` (Haiku). Example: the 12:35 window had 112 calls and $3.42, of which the subject surface made 53 calls and $3.39; the 13:05 to 14:00 windows had 782 calls, 663 of them judge calls. The alert text said "the daemon", which was misleading. Messages now end with `Top: <surface> N calls $X; ...` (three busiest surfaces).
2. **Brake until 13:53: engage and release behave as designed.** It engaged at 12:53:51 after 3 consecutive over-limit 15-minute windows and cleared at 13:53:51 (3600 s). The problem is not the timing but the reach: the brake only blocks Jarvis queue dispatch, brain escalations and HUD finding triage. Mind's improve loop is gated only by `job-budget --optional`, which ignores the brake, so the calls continued during the brake. After expiry `release` resets the streak to 0, so a surviving runaway gets about 45 minutes free before it can re-engage.
3. **Vali passed the $10 per-chat ceiling: the ceiling is advisory.** `~/.jarvis/hud/lib/spendceiling.js` sends a one-time ping (persona "Fafnir") at $10, checked every 60 s, and the text says "It keeps running." Nothing stops the chat. Budget holds no per-chat ceiling at all; the "from Fafnir" label is only the persona. This is HUD code, so reported, not fixed.
4. **Forecast "1.7 h to the line": real, now cleared.** The alert was 2026-10-30 16:58 (2,899,282 of the 4,000,000 line). It reads 42.8 h now. The forecast itself has weaknesses, listed below.

## Faults found and fixed (commits on `hud/vali-8a78`)

| Commit | Fault | Fix |
|---|---|---|
| `ca346fd` | BURN and BURN-CALLS alerts named no job or chat. | Both messages end with the top three surfaces (calls and spend). Keys and the `BURN:` / `BURN[work]:` / `BURN-CALLS:` prefixes are unchanged, because `brake.consider` matches on them. `call_rate` is unchanged. |
| `ca346fd` | `ledger._rows` read at most 5000 rows ascending, so beyond that the newest rows were silently dropped. Seven days of provider calls was already 3342 rows, and the Mind loop produces hundreds per hour. | Reads in pages of 5000 with an inclusive cursor and id dedupe. A test fakes `mr.query` with a tie on the page boundary. |
| `ca346fd` | `five_hour_tokens` trusted `status.json` with no age check, so a stale high count could pin the optional-job pause on. | Returns `None` (fail open) when `updated_at` is missing, unparseable, has no offset, or is older than 3 hours. |
| `dc90b7a` | `_record_observed_ceiling` ratcheted both the 5-hour and 7-day maxima on every hit. A weekly hit therefore set the 5-hour maximum (stored: 2,899,282) although the last real 5-hour hits happened at roughly 113k to 200k local billable tokens. | Each hit ratchets only the window it names (`five_hour` / `seven_day`; a hit naming neither still ratchets both), stores `window` in the limit file, and keeps the last 50 hits. |
| `dc90b7a` | Limit-hit evidence was the first 200 characters of one-line JSON noise. | When the line is a JSON object with a string `result`, that message is the evidence. |
| `c2cfab5` | `DESIGN.md` listed endpoints that were never built (`/burn/tick`, `/limits/hit`, brake release, `/run/*`, `/report`). | The interface section now matches the routes in `server/app.py`; the brake is released by deleting its state file. |

The two ceiling and evidence fixes share one commit and the three ledger fixes share one, because the tests interleave in the same file; each could be split if wanted.

## Faults found and not fixed

- **The stored 5-hour maximum is still wrong.** `state/claude-observed-ceilings.json` holds 2,899,282, set by the old both-windows ratchet. Fixing the code does not rewrite the state file, and state is outside the worktree. Anze can reset the 5-hour field (or delete the file) once; the next real 5-hour hit sets an honest value.
- **The local transcript token count does not predict server limits.** The four real hits (10-29 10:41 weekly; 10-30 16:19, 10-30 21:18 and 11-01 17:40 five-hour) happened at counts far below the stored maximum, so `five_hour_used_pct` is misleading and the 4,000,000 optional-job pause line (config, not mine) sits above the observed cap. No code fix is possible; it needs a different signal.
- **No usage-limit alert for Gateway-recorded hits.** Gateway (`~/.gateway/bifrost/claude.py:220`, `agent.py:164`) calls `limits.record` and `note_success`, but nothing calls `limits.alert` / `core.alert_limit` for those hits, so none of the four hits above alerted. Jarvis and Gateway code, so reported only.
- **The per-chat $10 ceiling cannot stop spend** (HUD code, see above).
- **The brake does not gate optional jobs or Mind's improve loop.** Making `job_budget` / `paused_for_optional` honour the brake is a design decision I did not take without Anze; it is the first suggestion below.
- **Forecast input is duplicated.** `burn-check` calls `ledger.sample_usage()` every 900 s but `status.json` refreshes hourly, so four identical samples per hour feed the slope in `budget-forecast.py` (Jarvis code).
- **`status.json` and `usage.json` are written non-atomically** by `update_status.py` and `update-usage.py`, so a reader can see a half-written file.
- **Usage digest row mixes accounts.** Tokens are personal-only, but the cost column is `usage.json claude.estimatedCostUsd`, which covers both accounts over a rolling 24 hours and is labelled by date.
- **Test isolation gap:** `quota.py` `USAGE_DIR` and `limits.py` `STATE_DIR` / `RAW_DIR` bind at import, so tests that patch `store.STATE` after import do not affect them (the suite works because it sets `FAFNIR_HOME` before import).
- **`/rate` does not validate `scope`.**
- **A tie group that fills a whole page would stop the new paging early** (rows after that timestamp stay unread). Needs more than 5000 rows with one identical timestamp; not realistic today.
- **Stale artifact:** `state/budget.db` (0 bytes) still exists. I did not delete it; deleting needs Anze's yes (pending since the 2026-10-27 audit).

## Suggested improvements, ranked

1. **Make the brake reach optional work.** Have `job_budget --optional` (and so Mind's improve loop) refuse while a brake is engaged. This is the one change that would have stopped the 11-01 burn.
2. **Keep the streak after a brake expires** (set it to need-1) so a still-running runaway re-engages in one window, not three.
3. **Give the improve loop its own cap or exclude `mind-improve-*` from the burn count and budget it separately** (config change in `config.toml`, outside my scope).
4. **Alert on Gateway-recorded limit hits** by calling `limits.alert` / `core.alert_limit` where Gateway records them.
5. **Reset the stored 5-hour observed maximum** and show the last hits per window in `/quota` and the digest.
6. **Replace the local-token proxy for the 5-hour pause** with the hit history now kept in `hits` (for example, pause optional jobs at some fraction of the lowest recent 5-hour hit).
7. **Offer a hard stop for the per-chat ceiling** in the HUD (an option to pause the chat, not only to ping).
8. **Dedupe `sample_usage`** by the status `updated_at`, so the forecast slope uses real samples.
9. **Write `status.json` and `usage.json` atomically** (temp file and rename).
10. **Split the digest cost by account** and label the rolling window honestly.
11. **Validate `scope` on `/rate`** and bind `quota.py` / `limits.py` paths lazily so tests can patch them.
12. **Run `usage-refresh` every 30 minutes** if a fresher 5-hour number is wanted (it is hourly now, with a 3-hour freshness limit in the ledger).

## Done-readiness

Tests: 9 passed. Branch is 0 commits behind `main`. Tree clean after this file. Nothing pushed.
