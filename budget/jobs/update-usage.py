#!/Users/anzes/.budget/.venv/bin/python
"""Refresh ~/.budget/state/usage/usage.json - the global, repo-agnostic Claude usage file,
read by burn.py, the usage digest and the /quota skill.

Moved here 2026-10-01 from orchestrator/scripts/ when the orchestrator agent and
Cursor delegation were removed; the Cursor block and roundRobinNext went with them.

Scans the transcript dirs of every config dir (ACCOUNT_DIRS) for assistant turns
in the trailing window, splits tokens by type, prices them per-model from
~/.budget/pricing.json. `claude` is the all-accounts total; `claude.byAccount`
splits it per account (personal vs work are separate subscriptions with
separate quotas).

Usage:
  python update-usage.py                 # refresh
  python update-usage.py --stdout        # also print the result
"""

import sys as _sys
if _sys.version_info < (3, 11):  # /usr/bin/python3 is 3.9 (no tomllib); the daemon runs .venv (3.13)
    raise SystemExit(f"{__file__}: needs Python 3.11+, got {_sys.version.split()[0]}; "
                     "run it with ~/.budget/.venv/bin/python")

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = HOME / '.budget' / 'pricing.json'
USAGE_PATH = HOME / '.budget' / 'state' / 'usage' / 'usage.json'
# All config dirs that hold transcripts, by account (~/.claude is the personal
# account's archive since the 2026-09-10 split; ~/.claude-work is another account).
ACCOUNT_DIRS = {
    'personal': [HOME / '.claude' / 'projects', HOME / '.claude-personal' / 'projects'],
    'work': [HOME / '.claude-work' / 'projects'],
}

WINDOW_HOURS = {'daily': 24, '5h': 5, '7d': 24 * 7}


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))


def price_tier(model_id):
    if not model_id:
        return 'sonnet'
    m = model_id.lower()
    if 'haiku' in m:
        return 'haiku'
    if 'opus' in m:
        return 'opus'
    return 'sonnet'


def parse_ts(rec):
    ts = rec.get('timestamp')
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return None


def scan_claude_usage(cutoff, pricing, project_dirs):
    totals = {'inputTokens': 0, 'outputTokens': 0, 'cacheReadTokens': 0, 'cacheWriteTokens': 0}
    cost = 0.0
    for f in (f for d in project_dirs if d.exists() for f in d.glob('*/*.jsonl')):
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
                ts = parse_ts(rec)
                if ts and ts < cutoff:
                    continue
                msg = rec.get('message') or {}
                u = msg.get('usage')
                if not isinstance(u, dict):
                    continue
                inp = int(u.get('input_tokens') or 0)
                out = int(u.get('output_tokens') or 0)
                cread = int(u.get('cache_read_input_tokens') or 0)
                cwrite = int(u.get('cache_creation_input_tokens') or 0)
                totals['inputTokens'] += inp
                totals['outputTokens'] += out
                totals['cacheReadTokens'] += cread
                totals['cacheWriteTokens'] += cwrite

                tier = price_tier(msg.get('model'))
                rates = pricing.get(tier, pricing['sonnet'])
                cost += inp * rates['input'] / 1_000_000
                cost += out * rates['output'] / 1_000_000
                cost += cread * rates['cacheRead'] / 1_000_000
                cost += cwrite * rates['cacheWrite'] / 1_000_000
    return totals, round(cost, 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stdout', action='store_true')
    args = ap.parse_args()

    cfg = load_config()
    window = cfg.get('usageWindow', 'daily')
    hours = WINDOW_HOURS.get(window, 24)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    by_account = {}
    for account, dirs in ACCOUNT_DIRS.items():
        totals, cost = scan_claude_usage(cutoff, cfg['pricingUsdPerMTok'], dirs)
        by_account[account] = {**totals, 'estimatedCostUsd': cost}

    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    usage = {}
    if USAGE_PATH.exists():
        try:
            usage = json.loads(USAGE_PATH.read_text(encoding='utf-8'))
        except Exception:
            usage = {}

    usage['window'] = now.date().isoformat()
    keys = ('inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens')
    usage['claude'] = {k: sum(a[k] for a in by_account.values()) for k in keys}
    usage['claude']['estimatedCostUsd'] = round(sum(a['estimatedCostUsd'] for a in by_account.values()), 4)
    usage['claude']['byAccount'] = by_account
    usage.pop('cursor', None)
    usage['lastUpdated'] = now.isoformat()
    usage.pop('roundRobinNext', None)

    USAGE_PATH.write_text(json.dumps(usage, indent=2), encoding='utf-8')
    if args.stdout:
        print(json.dumps(usage, indent=2))


if __name__ == '__main__':
    main()
