"""Build the shareable 'Claude Wrapped' payload: headline stats, records, and
superlatives derived from the parsed summary."""

from datetime import date

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"]


def _streaks(day_keys):
    """Longest run and current run of consecutive active days (YYYY-MM-DD)."""
    if not day_keys:
        return 0, 0
    days = sorted(date.fromisoformat(d) for d in day_keys)
    longest = cur = 1
    for prev, d in zip(days, days[1:]):
        cur = cur + 1 if (d - prev).days == 1 else 1
        longest = max(longest, cur)
    # current streak = trailing run of consecutive days ending at the last one
    current = 1
    for i in range(len(days) - 1, 0, -1):
        if (days[i] - days[i - 1]).days == 1:
            current += 1
        else:
            break
    return longest, current


def build_wrapped(summary):
    t = summary["totals"]
    by_day = summary["by_day"]
    by_hour = summary["by_hour"]
    by_weekday = summary["by_weekday"]
    by_model = summary["by_model"]
    by_project = summary["by_project"]
    sessions = summary["sessions"]

    total_cost = t["cost"] or 0.0

    # date range / days active
    day_keys = list(by_day.keys())
    days_active = len(day_keys)
    date_from = day_keys[0] if day_keys else None
    date_to = day_keys[-1] if day_keys else None
    span_days = 0
    if date_from and date_to:
        span_days = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1

    # top project / model
    top_project = None
    if by_project:
        name, v = next(iter(by_project.items()))
        top_project = {"name": name, "cost": v["cost"], "messages": v["messages"]}
    top_model = None
    if by_model:
        # exclude synthetic / none from "model of choice"
        real = [(k, v) for k, v in by_model.items()
                if k not in ("<synthetic>", "(none)")]
        if real:
            name, v = max(real, key=lambda kv: kv[1]["messages"])
            share = v["messages"] / (t["messages"] or 1)
            top_model = {"name": name, "share": share, "messages": v["messages"]}

    # priciest / busiest day
    priciest_day = busiest_day = None
    if by_day:
        pd = max(by_day.items(), key=lambda kv: kv[1]["cost"])
        priciest_day = {"date": pd[0], "cost": pd[1]["cost"]}
        bd = max(by_day.items(), key=lambda kv: kv[1]["messages"])
        busiest_day = {"date": bd[0], "messages": bd[1]["messages"]}

    # peak hour / weekday
    peak_hour = max(range(24), key=lambda h: by_hour[h]["messages"]) if by_hour else 0
    peak_weekday_idx = max(range(7), key=lambda w: by_weekday[w]["messages"]) if by_weekday else 0

    # night owl %: messages 22:00-05:59 local
    total_msgs = sum(by_hour[h]["messages"] for h in range(24)) or 1
    night = sum(by_hour[h]["messages"] for h in range(24) if h >= 22 or h < 6)
    night_owl_pct = night / total_msgs

    # marathon session = most assistant messages in a single session. (We rank
    # by message count rather than wall-clock time: session files get resumed
    # across days, so elapsed hours are not a meaningful "length".)
    marathon_session = None
    if sessions:
        ms = max(sessions, key=lambda s: s["assistant_messages"])
        marathon_session = {
            "project": ms["project"], "messages": ms["assistant_messages"],
            "cost": ms["cost"], "session_id": ms["session_id"],
        }

    # most expensive session
    priciest_session = None
    if sessions:
        ps = max(sessions, key=lambda s: s["cost"])
        priciest_session = {"project": ps["project"], "cost": ps["cost"],
                            "session_id": ps["session_id"],
                            "messages": ps["assistant_messages"]}

    longest_streak, current_streak = _streaks(day_keys)

    cache_saving = t.get("cache_saving", 0.0)
    no_cache_cost = total_cost + cache_saving

    return {
        "total_cost": total_cost,
        "total_tokens": t["total_tokens"],
        "output_tokens": t["output_tokens"],
        "total_messages": t["messages"],
        "sessions": t["sessions"],
        "projects": t["projects"],
        "days_active": days_active,
        "span_days": span_days,
        "date_from": date_from,
        "date_to": date_to,
        "top_project": top_project,
        "top_model": top_model,
        "priciest_day": priciest_day,
        "busiest_day": busiest_day,
        "peak_hour": peak_hour,
        "peak_weekday": WEEKDAYS[peak_weekday_idx],
        "night_owl_pct": night_owl_pct,
        "marathon_session": marathon_session,
        "priciest_session": priciest_session,
        "biggest_message": summary.get("max_message"),
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "cache_saving": cache_saving,
        "no_cache_cost": no_cache_cost,
        "avg_cost_per_day": total_cost / (days_active or 1),
    }
