"""Parse ~/.claude/projects/**/*.jsonl into usage/cost aggregates.

Correctness invariants (verified against real data):
  * A single assistant message.id is split across multiple JSONL lines (one per
    content block) and the SAME usage object is repeated on each. We DEDUP on
    message.id and count usage exactly once per unique message.
  * The four token buckets are disjoint (see pricing.py).
  * Projects are grouped by the real `cwd`, not the mangled directory name.
  * Daily/hourly buckets use LOCAL time (per user decision); timestamps in the
    logs are UTC (trailing 'Z').

The full parse is cached in memory keyed on the set of (path, mtime, size) so
778 MB is only walked when a session file actually changes.
"""

import glob
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

import pricing

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")

_CACHE = {"signature": None, "summary": None}


def _session_files():
    return sorted(
        glob.glob(os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"), recursive=True)
    )


def _signature(files):
    parts = []
    for f in files:
        try:
            st = os.stat(f)
            parts.append((f, int(st.st_mtime), st.st_size))
        except OSError:
            continue
    return tuple(parts)


def _local_dt(ts):
    """ISO-8601 UTC string -> local-aware datetime, or None."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except (ValueError, AttributeError):
        return None


def _blank_bucket():
    return {
        "cost": 0.0,
        "cache_saving": 0.0,
        "messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
    }


def _add(bucket, bd):
    bucket["cost"] += bd["cost"]
    bucket["cache_saving"] += bd.get("cache_saving", 0.0)
    bucket["messages"] += 1
    for k in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "cache_write_tokens",
        "total_tokens",
    ):
        bucket[k] += bd[k]


def _parse_file(path):
    """Parse one session file into a session record (usage deduped by msg id)."""
    session = {
        "session_id": os.path.splitext(os.path.basename(path))[0],
        "file": path,
        "cwd": None,
        "project": None,
        "models": set(),
        "first_ts": None,
        "last_ts": None,
        "assistant_messages": 0,
        "user_prompts": 0,
        "compactions": 0,
        "git_branch": None,
    }
    session.update(_blank_bucket())
    session["messages"] = 0  # will hold assistant message count via _add

    seen_ids = set()
    per_message = []  # (local_dt, model, breakdown) for global daily/hourly agg

    try:
        fh = open(path, "r", errors="replace")
    except OSError:
        return None, []

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = d.get("type")
            if session["cwd"] is None and d.get("cwd"):
                session["cwd"] = d["cwd"]
            if session["git_branch"] is None and d.get("gitBranch"):
                session["git_branch"] = d["gitBranch"]

            ts = _local_dt(d.get("timestamp"))
            if ts:
                if session["first_ts"] is None or ts < session["first_ts"]:
                    session["first_ts"] = ts
                if session["last_ts"] is None or ts > session["last_ts"]:
                    session["last_ts"] = ts

            if t == "user":
                # Count only genuine user prompts, not tool-result envelopes.
                msg = d.get("message", {})
                content = msg.get("content")
                is_tool_result = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if not is_tool_result and not d.get("isMeta"):
                    session["user_prompts"] += 1
                continue

            if t == "system" and d.get("subtype") == "compact_boundary":
                session["compactions"] += 1
                continue

            if t != "assistant":
                continue

            msg = d.get("message", {})
            mid = msg.get("id")
            if mid and mid in seen_ids:
                continue  # duplicate content-block line for the same message
            if mid:
                seen_ids.add(mid)

            model = msg.get("model")
            usage = msg.get("usage", {}) or {}
            if model:
                session["models"].add(pricing.normalize_model(model))

            bd = pricing.cost_breakdown(model, usage)
            _add(session, bd)
            session["assistant_messages"] += 1
            per_message.append((ts, model, bd))

    if session["cwd"]:
        session["project"] = session["cwd"]
    else:
        session["project"] = "(unknown)"
    return session, per_message


def _serialize_session(s):
    return {
        "session_id": s["session_id"],
        "project": s["project"],
        "cwd": s["cwd"],
        "git_branch": s["git_branch"],
        "models": sorted(s["models"]),
        "first_ts": s["first_ts"].isoformat() if s["first_ts"] else None,
        "last_ts": s["last_ts"].isoformat() if s["last_ts"] else None,
        "assistant_messages": s["assistant_messages"],
        "user_prompts": s["user_prompts"],
        "compactions": s["compactions"],
        "cost": s["cost"],
        "input_tokens": s["input_tokens"],
        "output_tokens": s["output_tokens"],
        "cache_read_tokens": s["cache_read_tokens"],
        "cache_write_tokens": s["cache_write_tokens"],
        "cache_write_1h_tokens": s["cache_write_1h_tokens"],
        "total_tokens": s["total_tokens"],
    }


def build_summary(force=False):
    files = _session_files()
    sig = _signature(files)
    if not force and _CACHE["signature"] == sig and _CACHE["summary"] is not None:
        return _CACHE["summary"]

    totals = _blank_bucket()
    by_model = defaultdict(_blank_bucket)
    by_project = defaultdict(_blank_bucket)
    by_day = defaultdict(_blank_bucket)
    by_hour = defaultdict(_blank_bucket)   # 0..23 local
    by_weekday = defaultdict(_blank_bucket)  # 0=Mon..6=Sun
    # True weekday(0=Mon..6=Sun) x hour(0..23) matrices (not marginals).
    wh_messages = [[0] * 24 for _ in range(7)]
    wh_cost = [[0.0] * 24 for _ in range(7)]
    sessions = []
    unpriced_models = defaultdict(int)
    max_message = None  # biggest single message by cost

    for path in files:
        session, per_message = _parse_file(path)
        if session is None:
            continue
        if session["assistant_messages"] == 0:
            continue
        sessions.append(_serialize_session(session))

        proj = session["project"]
        by_project[proj]  # touch

        for ts, model, bd in per_message:
            _add(totals, bd)
            _add(by_model[pricing.normalize_model(model) or "(none)"], bd)
            _add(by_project[proj], bd)
            if not bd["priced"] and not pricing.is_synthetic(model):
                unpriced_models[model or "(none)"] += 1
            if bd["cost"] > 0 and (max_message is None or bd["cost"] > max_message["cost"]):
                max_message = {
                    "cost": bd["cost"],
                    "total_tokens": bd["total_tokens"],
                    "output_tokens": bd["output_tokens"],
                    "model": pricing.normalize_model(model),
                    "project": proj,
                    "date": ts.strftime("%Y-%m-%d") if ts else None,
                }
            if ts:
                _add(by_day[ts.strftime("%Y-%m-%d")], bd)
                _add(by_hour[ts.hour], bd)
                _add(by_weekday[ts.weekday()], bd)
                wh_messages[ts.weekday()][ts.hour] += 1
                wh_cost[ts.weekday()][ts.hour] += bd["cost"]

    sessions.sort(key=lambda s: s["last_ts"] or "", reverse=True)

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "totals": {
            **totals,
            "sessions": len(sessions),
            "projects": len(by_project),
        },
        "by_model": {k: v for k, v in sorted(
            by_model.items(), key=lambda kv: kv[1]["cost"], reverse=True)},
        "by_project": {k: v for k, v in sorted(
            by_project.items(), key=lambda kv: kv[1]["cost"], reverse=True)},
        "by_day": dict(sorted(by_day.items())),
        "by_hour": {h: by_hour.get(h, _blank_bucket()) for h in range(24)},
        "by_weekday": {w: by_weekday.get(w, _blank_bucket()) for w in range(7)},
        "weekhour": {"messages": wh_messages, "cost": wh_cost},
        "max_message": max_message,
        "sessions": sessions,
        "unpriced_models": dict(unpriced_models),
    }

    _CACHE["signature"] = sig
    _CACHE["summary"] = summary
    return summary


def session_detail(session_id):
    """Lazy-load one session file and return an ordered replay of the turns."""
    for path in _session_files():
        if os.path.splitext(os.path.basename(path))[0] == session_id:
            return _build_detail(path, session_id)
    return None


def _text_from_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                parts.append(b.get("text", ""))
            elif bt == "thinking":
                parts.append("[thinking]")
            elif bt == "tool_use":
                parts.append(f"[tool_use: {b.get('name', '?')}]")
            elif bt == "tool_result":
                parts.append("[tool_result]")
            elif bt == "server_tool_use":
                parts.append(f"[server_tool: {b.get('name', '?')}]")
        return "\n".join(p for p in parts if p)
    return ""


def _build_detail(path, session_id):
    events = []
    seen_ids = set()
    header = {"session_id": session_id, "cwd": None, "git_branch": None,
              "models": set(), "compactions": 0}
    header.update(_blank_bucket())

    try:
        fh = open(path, "r", errors="replace")
    except OSError:
        return None

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if header["cwd"] is None and d.get("cwd"):
                header["cwd"] = d["cwd"]
            if header["git_branch"] is None and d.get("gitBranch"):
                header["git_branch"] = d["gitBranch"]
            ts = d.get("timestamp")

            if t == "system" and d.get("subtype") == "compact_boundary":
                header["compactions"] += 1
                events.append({"role": "compaction", "ts": ts,
                               "text": "— context compacted —"})
                continue

            if t == "user":
                msg = d.get("message", {})
                content = msg.get("content")
                is_tool_result = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if is_tool_result or d.get("isMeta"):
                    continue
                text = _text_from_content(content)
                if text.strip():
                    events.append({"role": "user", "ts": ts,
                                   "text": text[:4000]})
                continue

            if t != "assistant":
                continue
            msg = d.get("message", {})
            mid = msg.get("id")
            model = msg.get("model")
            if model:
                header["models"].add(model)
            text = _text_from_content(msg.get("content"))
            first_line_of_msg = mid not in seen_ids
            if mid:
                seen_ids.add(mid)
            bd = None
            if first_line_of_msg:
                usage = msg.get("usage", {}) or {}
                bd = pricing.cost_breakdown(model, usage)
                _add(header, bd)
            if text.strip():
                events.append({
                    "role": "assistant", "ts": ts, "model": model,
                    "text": text[:4000],
                    "cost": bd["cost"] if bd else None,
                    "total_tokens": bd["total_tokens"] if bd else None,
                })

    return {
        "session_id": session_id,
        "cwd": header["cwd"],
        "git_branch": header["git_branch"],
        "models": sorted(header["models"]),
        "compactions": header["compactions"],
        "cost": header["cost"],
        "input_tokens": header["input_tokens"],
        "output_tokens": header["output_tokens"],
        "cache_read_tokens": header["cache_read_tokens"],
        "cache_write_tokens": header["cache_write_tokens"],
        "total_tokens": header["total_tokens"],
        "events": events,
    }
