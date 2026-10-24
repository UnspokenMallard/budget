#!/Users/anzes/.budget/.venv/bin/python
"""Token attribution report for Claude Code sessions (`budget token-report`, Jarvis job `token-report`).

Copied into Fafnir from ~/.claude-personal/scripts/token-report.py on 2026-10-18 (that copy stays:
the tool-telemetry hook borrows its image-cost helper). As a job it writes
state/reports/token-attribution/<date>-token-attribution.md and the receipt state/jobs/token-report.json,
the work Jarvis's `token-report` handler did around the old script.

Written 2026-08-08. The premise, from ~/.claude-personal/CLAUDE.md, is that "file reads
dominate token use in a real session - far more than prompt overhead". That was
never measured here; every pruning decision so far has been a guess. The data to
settle it already exists in the session transcripts and nothing reads it.

Sources, and why not the obvious ones:
  ~/.claude/projects/**/*.jsonl  - AUTHORITATIVE. Every assistant message carries
                                   exact usage (input, output, cache_read,
                                   cache_creation) plus its model.
  ~/.claude/stats-cache.json     - stale since 2026-04-01 (24 sessions). Ignored.
  ~/.claude/telemetry/           - failed-event dumps only, no usage data.
  ~/.claude/history.jsonl        - prompts only, no token counts.

Exactness, stated plainly because it matters when acting on this:
  - Per-model and per-day totals are EXACT (read straight from usage fields).
  - Per-tool and per-file costs are ESTIMATES. The transcript does not break a
    turn's tokens down by content block, so tool-result payloads are measured in
    characters and divided by 4. Good enough to rank tools against each other;
    not a billing figure.

Usage:
  budget token-report [--days N] [--out PATH] [--stdout]   no --out/--stdout: the dated report and receipt
"""

import argparse
import base64
import io
import json
import os
import struct
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fafnir import store  # noqa: E402

CHARS_PER_TOKEN = 4  # rough, and only ever used for relative ranking
# Every config dir that has ever held transcripts. ~/.claude is the archive since the
# 2026-09-10 split; new sessions write under .claude-work or .claude-personal.
PROJECT_DIRS = [Path(os.path.expanduser(p)) for p in ("~/.claude/projects", "~/.claude-work/projects", "~/.claude-personal/projects")]


def parse_ts(rec):
    ts = rec.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


MAX_IMAGE_EDGE = 1568   # Anthropic downscales anything larger before charging
IMAGE_TOKEN_DIVISOR = 750


def image_size(raw):
    """(width, height) from a PNG, GIF, WebP or JPEG header, or None."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        return struct.unpack(">II", raw[16:24])
    if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
        return struct.unpack("<HH", raw[6:10])
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and len(raw) >= 30:
        chunk = raw[12:16]
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", raw[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L":
            b = raw[21:25]
            w = 1 + (((b[1] & 0x3F) << 8) | b[0])
            h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            return w, h
        if chunk == b"VP8X":
            return 1 + int.from_bytes(raw[24:27], "little"), 1 + int.from_bytes(raw[27:30], "little")
        return None
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                i += 1 if marker == 0xFF else 2
                continue
            seg_len = struct.unpack(">H", raw[i + 2:i + 4])[0]
            # SOF0-SOF15 carry the frame size; C4 (DHT), C8 (JPG), CC (DAC) do not.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return w, h
            i += 2 + seg_len
    return None


def image_tokens(b64_data):
    """Token cost of an image, from its DIMENSIONS - not its payload length.

    Fixed 2026-08-08. The first version of this script measured images the same
    way as text (base64 chars / 4), which overstated them by roughly 10x: a
    1920x1080 screenshot reported as ~33k tokens actually costs ~1.8k. That bad
    number made PNG reads look like the dominant cost in the first report; they
    are not. Images are charged on (width * height) / 750 after being downscaled
    to fit MAX_IMAGE_EDGE on the longest side.

    Only the header is decoded. Read with the stdlib, not Pillow (2026-10-01):
    neither the system python3 nor the Jarvis venv on the Mac Studio has
    Pillow, so the import alone broke the script and the weekly job.
    """
    size = None
    try:
        size = image_size(base64.b64decode(b64_data[:2048] + "==", validate=False))
        if size is None:
            # JPEG can put its SOF marker after a large EXIF block.
            size = image_size(base64.b64decode(b64_data, validate=False))
    except Exception:
        pass
    if size is None:
        # Unreadable header: fall back to the base64-length heuristic rather
        # than dropping the call from the totals entirely.
        return (len(b64_data) * 3 // 4) // CHARS_PER_TOKEN
    w, h = size

    longest = max(w, h)
    if longest > MAX_IMAGE_EDGE:
        scale = MAX_IMAGE_EDGE / longest
        w, h = w * scale, h * scale
    return int((w * h) / IMAGE_TOKEN_DIVISOR)


def block_tokens(block):
    """Estimated tokens for one content block, counting images by area."""
    if isinstance(block, str):
        return len(block) // CHARS_PER_TOKEN
    if not isinstance(block, dict):
        return 0

    if block.get("type") == "image":
        src = block.get("source") or {}
        if src.get("type") == "base64" and src.get("data"):
            return image_tokens(src["data"])
        return 0

    c = block.get("content")
    if isinstance(c, str):
        return len(c) // CHARS_PER_TOKEN
    if isinstance(c, list):
        return sum(block_tokens(b) for b in c)
    return len(json.dumps(block, default=str)) // CHARS_PER_TOKEN


def describe_tool_input(name, tool_input):
    """Short label for what a call actually touched - a path beats a tool name."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "path", "notebook_path", "pattern", "command", "url", "query"):
        v = tool_input.get(key)
        if isinstance(v, str) and v.strip():
            v = v.strip().replace("\n", " ")
            return v[:90]
    return ""


def collect(days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    model_tokens = defaultdict(lambda: defaultdict(int))   # model -> kind -> tokens
    daily = defaultdict(lambda: defaultdict(int))          # date  -> model -> billable
    tool_calls = defaultdict(int)                          # tool -> call count
    tool_result_tokens = defaultdict(int)                  # tool -> est. tokens returned
    mcp_tokens = defaultdict(int)                          # server -> est. tokens
    mcp_calls = defaultdict(int)
    biggest = []                                           # (tokens, tool, label, session)
    sessions = defaultdict(lambda: {"billable": 0, "prompt": "", "start": None})

    files = sorted(f for d in PROJECT_DIRS if d.exists() for f in d.glob("*/*.jsonl"))
    scanned = 0
    seen = set()

    for f in files:
        # Cheap prefilter: a transcript untouched since the cutoff has nothing
        # in window. Saves parsing most of the 328 MB on a 7-day report.
        try:
            if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < cutoff:
                continue
        except OSError:
            continue

        scanned += 1
        session_id = f.stem
        pending = {}  # tool_use_id -> (name, label)

        try:
            fh = io.open(f, encoding="utf-8", errors="replace")
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

                ts = parse_ts(rec)
                if ts and ts < cutoff:
                    continue

                msg = rec.get("message") or {}
                content = msg.get("content")

                dup = False
                if rec.get("type") == "assistant":
                    key = (msg.get("id"), rec.get("requestId"))
                    dup = bool(key[0]) and key in seen  # one line per content block repeats the message usage
                    seen.add(key)

                if rec.get("type") == "assistant" and isinstance(msg.get("usage"), dict) and not dup:
                    u = msg["usage"]
                    model = msg.get("model") or "unknown"
                    inp = int(u.get("input_tokens") or 0)
                    out = int(u.get("output_tokens") or 0)
                    cc = int(u.get("cache_creation_input_tokens") or 0)
                    cr = int(u.get("cache_read_input_tokens") or 0)

                    model_tokens[model]["input"] += inp
                    model_tokens[model]["output"] += out
                    model_tokens[model]["cache_write"] += cc
                    model_tokens[model]["cache_read"] += cr

                    # "Billable" here means tokens that are not cache reads -
                    # cache reads are the cheap path and lumping them in makes
                    # a well-cached session look catastrophic.
                    billable = inp + out + cc
                    if ts:
                        daily[ts.astimezone().strftime("%Y-%m-%d")][model] += billable
                    s = sessions[session_id]
                    s["billable"] += billable
                    if s["start"] is None and ts:
                        s["start"] = ts

                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")

                        if btype == "tool_use":
                            name = block.get("name") or "unknown"
                            tool_calls[name] += 1
                            if name.startswith("mcp__"):
                                mcp_calls[name.split("__")[1]] += 1
                            pending[block.get("id")] = (
                                name,
                                describe_tool_input(name, block.get("input")),
                            )

                        elif btype == "tool_result":
                            size = block_tokens(block)
                            name, label = pending.get(block.get("tool_use_id"), ("unknown", ""))
                            tool_result_tokens[name] += size
                            if name.startswith("mcp__"):
                                mcp_tokens[name.split("__")[1]] += size
                            biggest.append((size, name, label, session_id))

                        elif btype == "text" and rec.get("type") == "user":
                            s = sessions[session_id]
                            if not s["prompt"]:
                                s["prompt"] = (block.get("text") or "")[:100]

                if rec.get("type") == "user" and isinstance(content, str):
                    s = sessions[session_id]
                    if not s["prompt"]:
                        s["prompt"] = content[:100]

    biggest.sort(reverse=True)
    return {
        "model_tokens": model_tokens,
        "daily": daily,
        "tool_calls": tool_calls,
        "tool_result_tokens": tool_result_tokens,
        "mcp_tokens": mcp_tokens,
        "mcp_calls": mcp_calls,
        "biggest": biggest[:20],
        "sessions": sessions,
        "files_scanned": scanned,
        "files_total": len(files),
    }


def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def render(data, days):
    L = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    L.append(f"# Token attribution - last {days} days")
    L.append("")
    L.append(f"Generated {now} from {data['files_scanned']} of {data['files_total']} transcripts.")
    L.append("")
    L.append("Per-model and per-day figures are exact, read from the `usage` field of each")
    L.append("assistant message. Per-tool and per-file figures are **estimates**: a turn's")
    L.append("tokens are not broken down by content block, so text payloads are measured in")
    L.append("characters and divided by 4, and images are costed by area -")
    L.append("`(w * h) / 750` after downscaling to fit 1568px on the longest edge, which is")
    L.append("how images are actually charged. Use them to rank, not to bill.")
    L.append("")
    L.append('"Billable" excludes cache reads, which are the cheap path.')
    L.append("")

    # ---- models
    L.append("## Where the budget goes, by model")
    L.append("")
    L.append("| Model | Billable | Input | Output | Cache write | Cache read |")
    L.append("|---|--:|--:|--:|--:|--:|")
    total_billable = 0
    rows = []
    for model, k in data["model_tokens"].items():
        billable = k["input"] + k["output"] + k["cache_write"]
        total_billable += billable
        rows.append((billable, model, k))
    for billable, model, k in sorted(rows, reverse=True):
        L.append(
            f"| `{model}` | **{fmt(billable)}** | {fmt(k['input'])} | {fmt(k['output'])} "
            f"| {fmt(k['cache_write'])} | {fmt(k['cache_read'])} |"
        )
    L.append(f"| **total** | **{fmt(total_billable)}** | | | | |")
    L.append("")

    cache_read = sum(k["cache_read"] for k in data["model_tokens"].values())
    if total_billable:
        ratio = cache_read / total_billable
        L.append(f"Cache reads are {fmt(cache_read)} tokens, {ratio:.1f}x the billable total. "
                 "Higher is better - it means context is being reused rather than rebuilt.")
        L.append("")

    # ---- daily
    L.append("## By day")
    L.append("")
    L.append("| Date | Billable | Split |")
    L.append("|---|--:|---|")
    for date in sorted(data["daily"].keys(), reverse=True):
        by_model = data["daily"][date]
        tot = sum(by_model.values())
        split = ", ".join(
            f"{m.replace('claude-','')} {fmt(v)}"
            for m, v in sorted(by_model.items(), key=lambda x: -x[1])
        )
        L.append(f"| {date} | {fmt(tot)} | {split} |")
    L.append("")

    # ---- tools
    L.append("## Context cost by tool (estimated)")
    L.append("")
    L.append("| Tool | Calls | Est. tokens returned | Avg/call |")
    L.append("|---|--:|--:|--:|")
    ranked = sorted(data["tool_result_tokens"].items(), key=lambda x: -x[1])[:18]
    for name, est in ranked:
        calls = data["tool_calls"].get(name, 0) or 1
        L.append(f"| `{name}` | {calls} | {fmt(est)} | {fmt(est//calls)} |")
    L.append("")

    # ---- mcp
    if data["mcp_tokens"]:
        L.append("## Cost per MCP server (estimated)")
        L.append("")
        L.append("| Server | Calls | Est. tokens returned |")
        L.append("|---|--:|--:|")
        for server, est in sorted(data["mcp_tokens"].items(), key=lambda x: -x[1]):
            L.append(f"| `{server}` | {data['mcp_calls'].get(server,0)} | {fmt(est)} |")
        L.append("")
        L.append("A server with few calls and low returns is paying its tool-definition")
        L.append("overhead for nothing - candidate for pruning.")
        L.append("")

    # ---- biggest
    L.append("## Biggest single tool results (estimated)")
    L.append("")
    L.append("| Est. tokens | Tool | Target |")
    L.append("|--:|---|---|")
    for size, name, label, _sess in data["biggest"]:
        L.append(f"| {fmt(size)} | `{name}` | {label or '-'} |")
    L.append("")

    # ---- sessions
    L.append("## Most expensive sessions")
    L.append("")
    L.append("| Billable | Started | Opening prompt |")
    L.append("|--:|---|---|")
    top = sorted(data["sessions"].items(), key=lambda x: -x[1]["billable"])[:12]
    for _sid, s in top:
        if not s["billable"]:
            continue
        started = s["start"].astimezone().strftime("%m-%d %H:%M") if s["start"] else "-"
        prompt = (s["prompt"] or "").replace("\n", " ").replace("|", "/")[:70]
        L.append(f"| {fmt(s['billable'])} | {started} | {prompt} |")
    L.append("")

    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=None)
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()

    started = time.time()
    data = collect(args.days)
    report = render(data, args.days)

    if args.stdout:
        print(report)
    if args.stdout and not args.out:
        return 0
    job = not args.out
    out = (store.STATE / "reports" / "token-attribution" / f"{datetime.now():%Y-%m-%d}-token-attribution.md"
           if job else Path(os.path.expanduser(args.out)))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"written: {out}")
    if not job:
        return 0
    # Exit 0 is not evidence the report exists: Jarvis asserts the receipt, so check before writing it.
    size = out.stat().st_size if out.exists() else 0
    if size < 200:
        print(f"report missing or suspiciously small ({size} B) at {out}")
        return 1
    receipt = store.STATE / "jobs" / "token-report.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "days": args.days, "ok": True, "report": str(out), "bytes": size,
        "elapsed_s": round(time.time() - started, 1)}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
