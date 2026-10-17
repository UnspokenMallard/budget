"""Phase 6 step 2: a usage limit is a defer, not a failure.

Moved from Jarvis's daemon/limits.py into Budget on 2026-10-17. Budget records and answers;
Jarvis raises the alert (`alert()` returns it) and defers its own work.

Read PHASE6-DESIGN.md §2 first. The short version of why this exists:

Every Claude-side surface in this daemon used to treat a usage limit as
breakage. A nightly `claude -p` job retried three times inside ten minutes
against a window that resets in hours, then alerted. `brain.work()` counted the
same condition toward its circuit breaker, so a throttled afternoon disabled
escalation until a human called `reset_breaker()` by hand. That last one is the
sharpest: **a usage limit required a human to clear**, which is the exact
property this phase exists to remove.

The correction is one idea. A limit is not a failed attempt -- it is the work
arriving before the resource does. So a detected limit:

  * does not consume a retry attempt,
  * does not raise `handler-failed`,
  * does not count toward the breaker,
  * and comes back on its own once the window resets.

**What is measured here and what is guessed.** Measured 2026-08-30 by reading
`~/.local/bin/claude.exe` (207 MB) rather than by assuming: the CLI carries the
strings `Usage limit reached`, `Usage limit reached . continuing automatically
at `, and `Usage limit has reset`, and it reads real headers named
`anthropic-ratelimit-unified-reset` and `-slow-retry-after`. Its headless
`--output-format json` subtypes are `error_max_turns`, `error_max_budget` and
`error_during_execution` -- there is **no** limit-specific subtype to key on.

So the CLI knows the reset instant and does not commit to handing it to a
headless caller. **No limit hit has ever been observed on this machine** -- every
"usage limit" match in the transcripts on disk is one of this repo's own test
fixtures. Therefore `_parse_reset()` reads a timestamp when the text carries
one, and otherwise the caller re-probes after `default_wait_s`. Guessing five
hours and being wrong costs five idle hours; re-probing hourly and being wrong
costs one wasted call. Re-probing is the cheap direction of the error.

Every hit writes its raw text to `logs/limit-hits/`, which is what turns the
guess above into a measurement the first time this actually fires.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import store
from .store import log

CLAUDE = "claude"
CLAUDE_WORK = "claude-work"
PROVIDERS = (CLAUDE, CLAUDE_WORK)

STATE_DIR = store.STATE
RAW_DIR = store.STATE / "limit-hits"
# Per account (same lists as update_status.py): ~/.claude is the pre-2026-09-10
# archive, ~/.claude-personal gets every personal session since the config-dir
# split. ~/.claude-work is another account with its own quota.
PROJECTS_DIRS_BY_PROVIDER = {
    CLAUDE: [Path.home() / d / "projects" for d in (".claude", ".claude-personal")],
    CLAUDE_WORK: [Path.home() / ".claude-work" / "projects"],
}


def state_path(provider: str) -> Path:
    return STATE_DIR / f"{provider}-limit.json"


def ceilings_path(provider: str) -> Path:
    """Persistent, never cleared by `clear()` -- state_path() is the current
    limit window (deleted once it resets); this is the running record of what
    a real hit has taught us about the account's actual quota, which must
    survive every window reset to be useful for the next one."""
    return STATE_DIR / f"{provider}-observed-ceilings.json"


# --------------------------------------------------------------------------
# Detection
#
# Broad in one direction only, deliberately: a false positive costs one
# deferred call that would have succeeded, a false negative costs a silent
# stream of failures nothing explains. But "broad" is not "sloppy" -- see the
# 429 note below, which is a false positive this environment would actually
# have produced.
# --------------------------------------------------------------------------

# Phrases that cannot plausibly mean anything else. Word-anchored so "unlimited"
# does not match "limit" and "rate limiter" does not match "rate limit".
_STRONG = (
    r"usage limit"
    r"|rate limit(?:ed|s)?\b"
    r"|quota (?:exceeded|reached|exhausted)"
    r"|out of (?:credits|requests)"
    r"|insufficient credits"
    r"|no credits remaining"
    r"|upgrade your plan"
    r"|plan limit"
    r"|too many requests"
    r"|you(?:'ve| have) (?:reached|hit) (?:your|the) limit"
)

# 429 needs context. `delegation.py` matched a bare `\b429\b`, which in THIS
# system is a live false positive: `ps-script` results routinely read
# "artifact usage.json fresh (0.0m old, 429 B)". A byte count is not a rate
# limit, and a detector that defers the nightly jobs because a file happened to
# be 429 bytes long is worse than no detector. Verified against the event
# corpus before this was narrowed -- see the replay assertion in selftest.py.
_CODE_429 = r"(?:http[/ ]?[\d.]*\s*|status(?:\s*code)?\s*[:=]?\s*|error\s*[:=]?\s*)429\b|429\s+too many requests"

MARKERS = re.compile(rf"(?i)\b(?:{_STRONG})|{_CODE_429}")

# This daemon's own writing about a limit contains every marker above. Replaying
# the corpus found three such texts -- the `CURSOR USAGE LIMIT reported` verdict
# and its selftest fixtures -- and treating those as fresh hits would let one
# real limit re-arm the deferral every time its own result text was re-read.
# A report of a handled limit is history, not a signal.
_OWN_REPORT = re.compile(
    r"(?i)USAGE LIMIT reported"
    r"|routing switched to Claude only"
    r"|work deferred until"
    r"|reported a usage limit \d+ times in a row"
    r"|ZZ-SELFTEST"
)


@dataclass(frozen=True)
class LimitHit:
    provider: str
    evidence: str
    resets_at: float | None
    raw: str

    @property
    def measured_reset(self) -> bool:
        """True when the reset time came from the text, not from a default."""
        return self.resets_at is not None


def _evidence_line(text: str, match: re.Match) -> str:
    """The line the marker appeared on, which is what a human needs to read."""
    for line in text.splitlines():
        if match.group(0).lower() in line.lower():
            return line.strip()[:200]
    return match.group(0)


# `Usage limit reached|1756612800` -- the pipe-plus-epoch form. Kept first
# because it is unambiguous when present.
_EPOCH_RE = re.compile(r"(?:limit reached|resets?(?:\s+at)?)\s*[|:]\s*(\d{10,13})\b", re.I)
_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)\b")
_IN_RE = re.compile(r"\bin\s+(\d{1,4})\s*(second|minute|hour)s?\b", re.I)
_RETRY_AFTER_RE = re.compile(r"retry[- ]after\s*[:=]?\s*(\d{1,6})\b", re.I)
_AT_CLOCK_RE = re.compile(r"\b(?:at|until)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)


def _parse_reset(text: str, now: float | None = None) -> float | None:
    """Best-effort absolute reset time from the text. None when absent.

    None is a legitimate answer and the common one -- the caller falls back to
    `default_wait_s` and re-probes. Returning a confident wrong number would be
    worse than returning nothing.
    """
    now = now if now is not None else time.time()

    m = _EPOCH_RE.search(text)
    if m:
        value = int(m.group(1))
        # 13 digits is milliseconds.
        seconds = value / 1000 if value > 10_000_000_000 else value
        if now < seconds < now + 30 * 86400:
            return float(seconds)

    m = _RETRY_AFTER_RE.search(text)
    if m:
        return now + float(m.group(1))

    m = _IN_RE.search(text)
    if m:
        scale = {"second": 1, "minute": 60, "hour": 3600}[m.group(2).lower()]
        return now + int(m.group(1)) * scale

    m = _ISO_RE.search(text)
    if m:
        try:
            parsed = datetime.fromisoformat(m.group(1).replace(" ", "T"))
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            stamp = parsed.timestamp()
            if now < stamp < now + 30 * 86400:
                return stamp
        except ValueError:
            pass

    m = _AT_CLOCK_RE.search(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        suffix = (m.group(3) or "").lower()
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            local = datetime.fromtimestamp(now).astimezone()
            target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            stamp = target.timestamp()
            if stamp <= now:  # already past today -> it means tomorrow
                stamp += 86400
            return stamp
    return None


_WORK_MARK = re.compile(r"claude\[work\]|PROVIDER LIMIT \(claude-work\)|CLAUDE_CONFIG_DIR=\S*claude-work")


def detect(text: str | None, provider: str | None = None) -> LimitHit | None:
    """Return a LimitHit if `text` is a provider saying it is out of quota.

    Without an explicit `provider` it is read off the text: the job scripts log
    `claude[work]` and the daemon writes `PROVIDER LIMIT (claude-work)`, so a
    hit in the work account's output lands on the work provider.
    """
    if not text:
        return None
    if _OWN_REPORT.search(text):
        return None
    match = MARKERS.search(text)
    if not match:
        return None
    if provider is None:
        provider = CLAUDE_WORK if _WORK_MARK.search(text) else CLAUDE
    return LimitHit(
        provider=provider,
        evidence=_evidence_line(text, match),
        resets_at=_parse_reset(text),
        raw=text[-4000:],
    )


# --------------------------------------------------------------------------
# State
#
# A file rather than a `meta` row: usage/status.json and the /quota skill live
# in other processes, and a limit that only the daemon knows
# about cannot inform anything else. `state/` is gitignored, so the evidence
# text never reaches git history.
# --------------------------------------------------------------------------


def _cfg(cfg: Mapping[str, Any] | None = None) -> dict:
    cfg = cfg if cfg is not None else store.load_config()
    section = dict(cfg.get("limits") or {})
    section.setdefault("enabled", True)
    section.setdefault("default_wait_s", 3600)
    section.setdefault("jitter_s", 60)
    section.setdefault("max_consecutive_defers", 6)
    return section


def _read(provider: str) -> dict | None:
    try:
        return json.loads(state_path(provider).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A malformed flag is treated as no limit. Unreadable state must not be
        # able to pin the daemon into deferring forever.
        return None


def _write(provider: str, payload: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = state_path(provider)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic, so a reader never sees a half-written flag
    except OSError as exc:
        log.warning("limits: could not write %s state: %s", provider, exc)


def _capture_raw(hit: LimitHit) -> None:
    """Keep the raw text of every hit.

    The reset-time handling in this module is the one genuinely assumed part of
    it, because no limit has ever been observed here. This is what replaces the
    assumption with a measurement the first time one fires -- without it, the
    first real hit teaches nothing.
    """
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        (RAW_DIR / f"{hit.provider}-{stamp}.txt").write_text(hit.raw, encoding="utf-8")
    except OSError as exc:
        log.debug("limits: could not capture raw hit: %s", exc)


def _billable_tokens_since(cutoff: float, provider: str = CLAUDE) -> int:
    """Tokens billed against the account in [cutoff, now], from local transcripts.

    Deliberately duplicated from scripts/update_status.py's
    billable_tokens_since() rather than imported: that script runs as a job,
    and the daemon should not import a job script for ~15 lines of logic. Keep both in sync by hand if the
    counting method changes.
    """
    cutoff_dt = datetime.fromtimestamp(cutoff, timezone.utc)
    total = 0
    for f in (f for d in PROJECTS_DIRS_BY_PROVIDER[provider] for f in d.glob("*/*.jsonl")):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < cutoff_dt:
                continue
        except OSError:
            continue
        try:
            fh = open(f, encoding="utf-8", errors="replace")
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
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                ts = rec.get("timestamp")
                if ts:
                    try:
                        rec_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if rec_dt < cutoff_dt:
                            continue
                    except Exception:
                        pass
                total += int(usage.get("input_tokens") or 0)
                total += int(usage.get("output_tokens") or 0)
                total += int(usage.get("cache_creation_input_tokens") or 0)
    return total


def _record_observed_ceiling(provider: str, now: float) -> None:
    """A real limit hit is the one honest way to learn this account's actual
    5h/7d quota -- Anthropic doesn't publish it (checked 2026-09-03: their own
    docs confirm no exact number for Pro/Max plans). Never fabricate a budget;
    only ever record one from a real 'you are out of quota' event, and only
    ratchet it up on new evidence, since a partial session ending early would
    otherwise look like a lower cap than the true one.
    """
    if provider not in PROJECTS_DIRS_BY_PROVIDER:
        return
    five_h = _billable_tokens_since(now - 5 * 3600, provider)
    seven_d = _billable_tokens_since(now - 7 * 86400, provider)

    path = ceilings_path(provider)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}

    hit_count = int(existing.get("hit_count", 0)) + 1
    payload = {
        "five_hour_tokens_observed_max": max(five_h, int(existing.get("five_hour_tokens_observed_max", 0))),
        "seven_day_tokens_observed_max": max(seven_d, int(existing.get("seven_day_tokens_observed_max", 0))),
        "last_hit_five_hour_tokens": five_h,
        "last_hit_seven_day_tokens": seven_d,
        "last_hit_at": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "first_hit_at": existing.get("first_hit_at") or datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "hit_count": hit_count,
        "note": (
            "Observed, not published - Anthropic exposes no exact Pro/Max token cap. "
            "*_observed_max is a lower-bound estimate: the real cap is at least this "
            "high, ratcheted up (never down) as more real hits provide better evidence. "
            "Read by ~/.jarvis/scripts/update_status.py as a fallback budget "
            "when usage/status-config.json sets no explicit claude_5h_budget_tokens/"
            "claude_7d_budget_tokens."
        ),
    }
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("limits: could not write %s ceilings: %s", provider, exc)


def record(hit: LimitHit, cfg: Mapping[str, Any] | None = None) -> float:
    """Persist a limit hit and return the time work may resume.

    Consecutive hits accumulate a `defers` count. That count is what stops this
    from becoming an infinite retry loop: a limit that never clears is a broken
    account, and that IS a human's problem -- see `should_defer`.
    """
    settings = _cfg(cfg)
    now = time.time()
    existing = _read(hit.provider) or {}

    _record_observed_ceiling(hit.provider, now)

    resets_at = hit.resets_at or (now + float(settings["default_wait_s"]))
    defers = int(existing.get("defers", 0)) + 1

    payload = {
        "hit_at": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "provider": hit.provider,
        "evidence": hit.evidence,
        "resets_at": resets_at,
        "resets_at_human": datetime.fromtimestamp(resets_at).astimezone().isoformat(timespec="seconds"),
        "reset_source": "parsed from provider output" if hit.measured_reset else "assumed default_wait_s, will re-probe",
        "defers": defers,
        "first_hit_at": existing.get("first_hit_at")
        or datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "note": "Delete this file to resume immediately.",
    }
    _write(hit.provider, payload)
    _capture_raw(hit)

    return resets_at


def alert(provider: str, cfg: Mapping[str, Any] | None = None) -> tuple[str, str, str, int] | None:
    """(key, message, severity, cooldown_s) for the hit just recorded, for Jarvis to raise.
    Budget records; Jarvis alerts. An error once `max_consecutive_defers` hits in a row have
    falsified "waiting fixes it", a warn before that."""
    state = _read(provider)
    if not state:
        return None
    defers = int(state.get("defers", 0))
    if defers >= int(_cfg(cfg)["max_consecutive_defers"]):
        return (f"{provider}-limit-persistent",
                f"{provider} has reported a usage limit {defers} times in a row and is no longer being deferred "
                f"- work will now fail normally. Evidence: {state.get('evidence')}", "error", 21600)
    return (f"{provider}-usage-limit",
            f"{provider} reported a usage limit; work deferred until {state.get('resets_at_human')} "
            f"({state.get('reset_source')}). Evidence: {state.get('evidence')}", "warn", 3600)


def blocked(provider: str = CLAUDE, cfg: Mapping[str, Any] | None = None) -> tuple[bool, float | None]:
    """Is `provider` currently inside a limit window? Returns (blocked, until).

    Self-clearing: once `resets_at` passes, the state file is removed and the
    answer is False. Nothing has to remember to clear it, which is the property
    that makes unattended resume work.
    """
    if not _cfg(cfg)["enabled"]:
        return False, None
    state = _read(provider)
    if not state:
        return False, None
    resets_at = float(state.get("resets_at") or 0)
    if time.time() >= resets_at:
        clear(provider)
        log.info("limits: %s limit window has passed, resuming", provider)
        return False, None
    return True, resets_at


def should_defer(provider: str = CLAUDE, cfg: Mapping[str, Any] | None = None) -> bool:
    """Whether another defer is still the right answer for this provider.

    False once `max_consecutive_defers` is reached, so the caller falls through
    to its normal failure path. A retry loop that never gives up is the failure
    mode this whole daemon was built against; the alert in `record()` is what
    makes the giving-up visible.
    """
    settings = _cfg(cfg)
    if not settings["enabled"]:
        return False
    state = _read(provider) or {}
    return int(state.get("defers", 0)) < int(settings["max_consecutive_defers"])


def clear(provider: str = CLAUDE) -> None:
    """Forget a limit. Called on expiry and after any successful call."""
    try:
        state_path(provider).unlink(missing_ok=True)
    except OSError as exc:
        log.debug("limits: could not clear %s state: %s", provider, exc)


def note_success(provider: str = CLAUDE) -> None:
    """A call got through, so the consecutive-defer count is no longer real.

    Separate from `clear()` only for readability at the call sites: this one
    means "the resource came back", which is the event that must reset the
    give-up counter.
    """
    if _read(provider):
        clear(provider)


def resume_at(provider: str = CLAUDE, cfg: Mapping[str, Any] | None = None) -> float:
    """When a deferred item should next be attempted, jitter included.

    The jitter matters because every deferred event would otherwise become
    claimable in the same instant and stampede the moment the window opens --
    on a 2-core box that is the shape that starves the daemon.
    """
    settings = _cfg(cfg)
    is_blocked, until = blocked(provider, cfg)
    base = until if (is_blocked and until) else time.time() + float(settings["default_wait_s"])
    return base + float(settings["jitter_s"])


def status() -> str:
    """One line per provider, for status.py and the checkpoint."""
    lines = []
    for provider in PROVIDERS:
        is_blocked, until = blocked(provider)
        if not is_blocked:
            lines.append(f"  {provider:11} clear")
            continue
        state = _read(provider) or {}
        mins = (until - time.time()) / 60 if until else 0
        lines.append(
            f"  {provider:11} LIMITED for {mins:.0f} more min "
            f"(defer {state.get('defers')}, {state.get('reset_source')})"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    print("limits\n" + status())
