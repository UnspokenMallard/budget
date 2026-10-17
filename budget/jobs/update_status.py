#!/Users/anzes/.budget/.venv/bin/python
"""Update ~/.budget/state/usage/status.json with the Claude usage estimate.

Moved here 2026-10-01 from cursor-delegation/scripts/update_status.py when Cursor
delegation was removed; the Cursor fields and routing advice went with it.

Estimated from local transcript token usage (~/.claude{,-personal}/projects/**/*.jsonl),
same source as token-report.py. Approximate, not the server-side percentage Anthropic
enforces - only becomes a percent if claude_5h_budget_tokens / claude_7d_budget_tokens
are set in usage/status-config.json, or a real limit hit has been observed. Otherwise
only raw token counts are recorded.

Usage:
  python update_status.py [--stdout]
"""

import sys as _sys
if _sys.version_info < (3, 11):  # /usr/bin/python3 is 3.9 (no tomllib); the daemon runs .venv (3.13)
    raise SystemExit(f"{__file__}: needs Python 3.11+, got {_sys.version.split()[0]}; "
                     "run it with ~/.budget/.venv/bin/python")

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

USAGE_DIR = Path.home() / '.budget' / 'state' / 'usage'
STATUS_PATH = USAGE_DIR / 'status.json'
CONFIG_PATH = USAGE_DIR / 'status-config.json'
# Personal account only: ~/.claude is the archive from before the 2026-09-10
# config-dir split, ~/.claude-personal receives every session since. ~/.claude-work
# is a different account with its own quota, so it must not count here.
PROJECTS_DIRS = [Path.home() / d / 'projects' for d in ('.claude', '.claude-personal')]
WORK_PROJECTS_DIRS = [Path.home() / '.claude-work' / 'projects']

# Written by ~/.jarvis/daemon/limits.py's _record_observed_ceiling() whenever
# Claude Code (or a claude -p call sharing the same Pro/Max quota) actually
# reports a real usage-limit hit. Read-only from here - a fallback budget
# source when status-config.json sets no explicit claude_5h/7d_budget_tokens,
# since Anthropic publishes no exact number to configure in the first place
# (checked 2026-09-03). Absent until the first real hit occurs.
CEILINGS_PATH = Path.home() / '.budget' / 'state' / 'claude-observed-ceilings.json'

DEFAULT_CONFIG = {
    "claude_5h_budget_tokens": None,
    "claude_7d_budget_tokens": None,
}


def load_config():
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        return {**DEFAULT_CONFIG, **cfg}
    return dict(DEFAULT_CONFIG)


def parse_ts(rec):
    ts = rec.get('timestamp')
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return None


def billable_tokens_since(cutoff, dirs=None):
    total = 0
    for f in (f for d in (dirs or PROJECTS_DIRS) for f in d.glob('*/*.jsonl')):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < cutoff:
                continue
        except OSError:
            continue
        try:
            fh = open(f, encoding='utf-8', errors='replace')
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get('type') != 'assistant':
                    continue
                msg = rec.get('message') or {}
                u = msg.get('usage')
                if not isinstance(u, dict):
                    continue
                ts = parse_ts(rec)
                if ts and ts < cutoff:
                    continue
                total += int(u.get('input_tokens') or 0)
                total += int(u.get('output_tokens') or 0)
                total += int(u.get('cache_creation_input_tokens') or 0)
    return total


def load_observed_ceilings(path=None):
    path = path or CEILINGS_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def compute_claude_usage(cfg, work=False):
    """One account's estimate. `work` switches to the work account's transcripts,
    ceilings file and `claude_work_*` budget keys."""
    now = datetime.now(timezone.utc)
    dirs = WORK_PROJECTS_DIRS if work else PROJECTS_DIRS
    tokens_5h = billable_tokens_since(now - timedelta(hours=5), dirs)
    tokens_7d = billable_tokens_since(now - timedelta(days=7), dirs)

    ceilings = load_observed_ceilings(
        CEILINGS_PATH.with_name('claude-work-observed-ceilings.json') if work else None)

    prefix = 'claude_work' if work else 'claude'
    budget_5h = cfg.get(f'{prefix}_5h_budget_tokens')
    budget_7d = cfg.get(f'{prefix}_7d_budget_tokens')
    budget_source = 'configured' if (budget_5h or budget_7d) else None

    if not budget_5h and ceilings:
        budget_5h = ceilings.get('five_hour_tokens_observed_max')
        budget_source = 'observed-limit-hit'
    if not budget_7d and ceilings:
        budget_7d = ceilings.get('seven_day_tokens_observed_max')
        budget_source = 'observed-limit-hit'

    note = (
        'Estimated from local transcript token usage in the trailing window, not the '
        'official server-side percentage - Anthropic publishes no exact Pro/Max token cap '
        f'(checked 2026-09-03). Set {prefix}_5h_budget_tokens / {prefix}_7d_budget_tokens in '
        'usage/status-config.json to override, or wait for a real usage-limit hit to '
        'self-calibrate (see ~/.budget/budget/limits.py); until then this stays null rather '
        'than guess.'
    )
    if budget_source == 'observed-limit-hit':
        note = (
            f"Budget is a lower-bound estimate from {ceilings.get('hit_count')} real usage-limit "
            f"hit(s), most recently {ceilings.get('last_hit_at')} - not an official number, "
            'just what has actually been observed. Could still be an underestimate if the '
            'true cap is higher than any hit so far revealed.'
        )

    return {
        'five_hour_billable_tokens': tokens_5h,
        'five_hour_used_pct': round(100 * tokens_5h / budget_5h, 1) if budget_5h else None,
        'seven_day_billable_tokens': tokens_7d,
        'seven_day_used_pct': round(100 * tokens_7d / budget_7d, 1) if budget_7d else None,
        'source': 'transcript-estimate',
        'budget_source': budget_source,
        'note': note,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stdout', action='store_true')
    args = ap.parse_args()

    USAGE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding='utf-8')

    status = {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'claude': compute_claude_usage(cfg),
        'claudeWork': compute_claude_usage(cfg, work=True),
    }
    STATUS_PATH.write_text(json.dumps(status, indent=2), encoding='utf-8')
    if args.stdout:
        print(json.dumps(status, indent=2))


if __name__ == '__main__':
    main()
