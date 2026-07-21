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
_RECORDS = {"signature": None, "messages": None}


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


def _local_dt_from_iso(s):
    """Reconstruct a local-aware datetime from an ISO string we wrote earlier."""
    if not s:
        return None
    if not isinstance(s, str):
        return s
    try:
        return datetime.fromisoformat(s)
    except ValueError:
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


# --- attribution helpers (Phase B) ---------------------------------------

_FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"}


def _file_key(name, inp):
    """Path a file-touching tool acted on, or None."""
    if not isinstance(inp, dict):
        return None
    if name == "NotebookEdit":
        return inp.get("notebook_path")
    if name in _FILE_TOOLS:
        return inp.get("file_path")
    return None


def _result_bytes(rc):
    """Approximate size of a tool_result payload (proxy for context injected)."""
    if rc is None:
        return 0
    if isinstance(rc, str):
        return len(rc)
    try:
        return len(json.dumps(rc))
    except (TypeError, ValueError):
        return 0


def parse_session_file(path):
    """Canonical single-pass parse of one session file.

    Returns (session_meta, messages, attribution) or (None, [], []) on error:
      * session_meta: dict of the session-grain fields that cannot be rederived
        from message rows (cwd, git_branch, first/last ts spanning ALL events,
        user_prompts, compactions).
      * messages: one dict per UNIQUE assistant message.id (deduped), carrying
        the raw model string, local timestamp, and the five disjoint token
        buckets. Cost is intentionally NOT stored — priced at read time.
      * attribution: per-(kind,key) aggregates {kind,key,calls,result_bytes}
        for kind in {tool,file,skill,subagent}. result_bytes is the size of the
        tool_result fed back for that tool/file (approximate context proxy).

    This is the one place raw JSONL is interpreted; both the live aggregation and
    the SQLite warehouse consume its output, so the dedup/bucketing lives once.
    """
    meta = {
        "session_id": os.path.splitext(os.path.basename(path))[0],
        "cwd": None,
        "git_branch": None,
        "first_ts": None,
        "last_ts": None,
        "user_prompts": 0,
        "compactions": 0,
    }
    messages = []
    seen_ids = set()

    # attribution accumulators
    tool_calls = defaultdict(int)
    file_calls = defaultdict(int)
    skill_calls = defaultdict(int)
    subagent_calls = defaultdict(int)
    tool_bytes = defaultdict(int)
    file_bytes = defaultdict(int)
    tool_ref = {}  # tool_use id -> (tool_name, file_key)

    try:
        fh = open(path, "r", errors="replace")
    except OSError:
        return None, [], []

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
            if meta["cwd"] is None and d.get("cwd"):
                meta["cwd"] = d["cwd"]
            if meta["git_branch"] is None and d.get("gitBranch"):
                meta["git_branch"] = d["gitBranch"]

            ts = _local_dt(d.get("timestamp"))
            if ts:
                if meta["first_ts"] is None or ts < meta["first_ts"]:
                    meta["first_ts"] = ts
                if meta["last_ts"] is None or ts > meta["last_ts"]:
                    meta["last_ts"] = ts

            if t == "user":
                msg = d.get("message", {})
                content = msg.get("content")
                # Harvest tool_result sizes (context these tools injected back).
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            ref = tool_ref.get(b.get("tool_use_id"))
                            if ref:
                                nbytes = _result_bytes(b.get("content"))
                                tool_bytes[ref[0]] += nbytes
                                if ref[1]:
                                    file_bytes[ref[1]] += nbytes
                is_tool_result = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if not is_tool_result and not d.get("isMeta"):
                    meta["user_prompts"] += 1
                continue

            if t == "system" and d.get("subtype") == "compact_boundary":
                meta["compactions"] += 1
                continue

            if t != "assistant":
                continue

            msg = d.get("message", {})

            # Attribution scans EVERY assistant line: content is one block per
            # line, so tool_use blocks are spread across the duplicate lines that
            # share a message.id. This is independent of the usage dedup below.
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name = b.get("name") or "(unknown)"
                    inp = b.get("input") or {}
                    tool_calls[name] += 1
                    fkey = _file_key(name, inp)
                    if fkey:
                        file_calls[fkey] += 1
                    if name == "Skill" and inp.get("skill"):
                        skill_calls[inp["skill"]] += 1
                    if name in ("Agent", "Task") and inp.get("subagent_type"):
                        subagent_calls[inp["subagent_type"]] += 1
                    if b.get("id"):
                        tool_ref[b["id"]] = (name, fkey)

            # Usage: count exactly once per unique message.id.
            mid = msg.get("id")
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)

            usage = msg.get("usage", {}) or {}
            w5m, w1h = pricing._cache_write_split(usage)
            messages.append({
                "message_id": mid or f"{meta['session_id']}:{len(messages)}",
                "model": msg.get("model"),
                "ts": ts,
                "input_tokens": usage.get("input_tokens", 0) or 0,
                "output_tokens": usage.get("output_tokens", 0) or 0,
                "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
                "cache_write_5m_tokens": w5m,
                "cache_write_1h_tokens": w1h,
            })

    attribution = []
    for kind, calls, byts in (
        ("tool", tool_calls, tool_bytes),
        ("file", file_calls, file_bytes),
        ("skill", skill_calls, {}),
        ("subagent", subagent_calls, {}),
    ):
        for key, n in calls.items():
            attribution.append({
                "kind": kind, "key": key, "calls": n,
                "result_bytes": byts.get(key, 0),
            })

    return meta, messages, attribution


def _per_message(messages):
    """(ts_dt, raw_model, breakdown) tuples for the aggregation loop."""
    out = []
    for m in messages:
        ts = m["ts"]
        if isinstance(ts, str):
            ts = _local_dt_from_iso(ts)
        bd = pricing.cost_from_buckets(
            m["model"], m["input_tokens"], m["output_tokens"],
            m["cache_read_tokens"], m["cache_write_5m_tokens"],
            m["cache_write_1h_tokens"], on_date=ts,
        )
        out.append((ts, m["model"], bd))
    return out


def _session_from_meta(meta):
    """Blank aggregation-ready session dict seeded from session_meta."""
    s = {
        "session_id": meta["session_id"],
        "cwd": meta["cwd"],
        "project": meta["cwd"] or "(unknown)",
        "git_branch": meta["git_branch"],
        "models": set(),
        "first_ts": _local_dt_from_iso(meta["first_ts"]),
        "last_ts": _local_dt_from_iso(meta["last_ts"]),
        "assistant_messages": 0,
        "user_prompts": meta["user_prompts"],
        "compactions": meta["compactions"],
    }
    s.update(_blank_bucket())
    return s


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


def _aggregate(session_provider):
    """Run the (penny-verified) global aggregation over a provider of
    (session_meta, message_rows) pairs. The provider is either the live parse or
    the SQLite warehouse — the arithmetic here is identical either way."""
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

    for meta, msg_rows in session_provider:
        if meta is None:
            continue
        per_message = _per_message(msg_rows)
        if not per_message:
            continue

        session = _session_from_meta(meta)
        proj = session["project"]
        by_project[proj]  # touch

        for ts, model, bd in per_message:
            _add(session, bd)
            session["assistant_messages"] += 1
            nm = pricing.normalize_model(model)
            if nm:
                session["models"].add(nm)
            _add(totals, bd)
            _add(by_model[nm or "(none)"], bd)
            _add(by_project[proj], bd)
            if not bd["priced"] and not pricing.is_synthetic(model):
                unpriced_models[model or "(none)"] += 1
            if bd["cost"] > 0 and (max_message is None or bd["cost"] > max_message["cost"]):
                max_message = {
                    "cost": bd["cost"],
                    "total_tokens": bd["total_tokens"],
                    "output_tokens": bd["output_tokens"],
                    "model": nm,
                    "project": proj,
                    "date": ts.strftime("%Y-%m-%d") if ts else None,
                }
            if ts:
                _add(by_day[ts.strftime("%Y-%m-%d")], bd)
                _add(by_hour[ts.hour], bd)
                _add(by_weekday[ts.weekday()], bd)
                wh_messages[ts.weekday()][ts.hour] += 1
                wh_cost[ts.weekday()][ts.hour] += bd["cost"]

        sessions.append(_serialize_session(session))

    sessions.sort(key=lambda s: s["last_ts"] or "", reverse=True)

    return {
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


def _live_sessions():
    """Provider: parse every current log file directly (no warehouse)."""
    for path in _session_files():
        meta, msgs, _attr = parse_session_file(path)
        yield meta, msgs


def build_summary_live():
    """In-memory summary straight from the live logs. Used by the verification
    gate to prove the warehouse-backed path matches it to the penny."""
    return _aggregate(_live_sessions())


def build_summary(force=False):
    """Warehouse-backed summary. Syncs changed logs into SQLite (incremental),
    then aggregates over ALL persisted sessions — including any whose source
    logs Claude has pruned. An in-memory cache keyed on live-file signatures
    sits on top so we only re-aggregate when the on-disk set changes."""
    import history

    files = _session_files()
    sig = _signature(files)
    if not force and _CACHE["signature"] == sig and _CACHE["summary"] is not None:
        return _CACHE["summary"]

    history.sync(files)
    summary = _aggregate(history.iter_sessions())

    _CACHE["signature"] = sig
    _CACHE["summary"] = summary
    return summary


def _build_records():
    """Per-assistant-message records for the filterable Usage view, sourced from
    the warehouse (so pruned history is included). Cached on live-file mtimes."""
    import history

    files = _session_files()
    sig = _signature(files)
    if _RECORDS["signature"] == sig and _RECORDS["messages"] is not None:
        return _RECORDS["messages"]

    history.sync(files)
    messages = history.get_records()

    _RECORDS["signature"] = sig
    _RECORDS["messages"] = messages
    return messages


_USAGE_KEYS = ("cost", "input_tokens", "output_tokens", "cache_read_tokens",
               "cache_write_tokens", "cache_write_1h_tokens", "total_tokens")


def _usage_blank():
    b = {k: 0 for k in _USAGE_KEYS}
    b["cost"] = 0.0
    b["messages"] = 0
    return b


def _usage_add(bucket, m):
    bucket["messages"] += 1
    for k in _USAGE_KEYS:
        bucket[k] += m[k]


def usage_view(project=None, date_from=None, date_to=None):
    """Filtered aggregates for the Usage tab: totals, by_day, by_model, and a
    per-session list, all restricted to the given project and [from, to] date
    range (inclusive, local dates as YYYY-MM-DD)."""
    messages = _build_records()

    # project dropdown + date bounds are computed over the FULL dataset
    all_projects = {}
    min_date = max_date = None
    for m in messages:
        p = all_projects.setdefault(m["project"], 0.0)
        all_projects[m["project"]] = p + m["cost"]
        if m["date"]:
            if min_date is None or m["date"] < min_date:
                min_date = m["date"]
            if max_date is None or m["date"] > max_date:
                max_date = m["date"]
    projects = [{"name": k, "cost": v} for k, v in
                sorted(all_projects.items(), key=lambda kv: kv[1], reverse=True)]

    def keep(m):
        if project and m["project"] != project:
            return False
        if date_from or date_to:
            if not m["date"]:
                return False
            if date_from and m["date"] < date_from:
                return False
            if date_to and m["date"] > date_to:
                return False
        return True

    totals = _usage_blank()
    by_day = {}
    by_model = {}
    sess = {}
    for m in messages:
        if not keep(m):
            continue
        _usage_add(totals, m)
        if m["date"]:
            _usage_add(by_day.setdefault(m["date"], _usage_blank()), m)
        _usage_add(by_model.setdefault(m["model"], _usage_blank()), m)
        s = sess.get(m["session_id"])
        if s is None:
            s = sess[m["session_id"]] = {
                "session_id": m["session_id"], "project": m["project"],
                "cwd": m["project"], "models": set(), "first_ts": None,
                "last_ts": None, "assistant_messages": 0, **_usage_blank(),
            }
        _usage_add(s, m)
        s["assistant_messages"] += 1
        if m["model"]:
            s["models"].add(m["model"])
        if m["ts"]:
            if s["first_ts"] is None or m["ts"] < s["first_ts"]:
                s["first_ts"] = m["ts"]
            if s["last_ts"] is None or m["ts"] > s["last_ts"]:
                s["last_ts"] = m["ts"]

    sessions = []
    for s in sess.values():
        s["models"] = sorted(s["models"])
        s.pop("messages", None)
        sessions.append(s)
    sessions.sort(key=lambda s: s["last_ts"] or "", reverse=True)

    return {
        "totals": {**totals, "sessions": len(sessions)},
        "by_day": dict(sorted(by_day.items())),
        "by_model": {k: v for k, v in sorted(
            by_model.items(), key=lambda kv: kv[1]["cost"], reverse=True)},
        "sessions": sessions,
        "projects": projects,
        "bounds": {"min": min_date, "max": max_date},
        "filter": {"project": project or "", "from": date_from or "", "to": date_to or ""},
    }


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
                bd = pricing.cost_breakdown(model, usage, on_date=ts)
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
