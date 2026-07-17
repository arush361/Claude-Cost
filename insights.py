"""Turn the usage summary into actionable, quantified cost-reduction insights.

Each insight is a dict:
  {
    severity: high|medium|low|good,
    kind:     savings | signal | positive,
    category: slug for the icon,
    icon:     emoji,
    title, detail,
    impact:       display string (optional),
    impact_value: float USD you could actually cut (only for kind == "savings"),
  }

IMPORTANT: `savings` and `signal` numbers are NOT additive and must never be
summed into a single "total savings" headline — spend-concentration figures are
"where to look", not money you can cut, and the real savings overlap each other.
We headline the single largest *savings* item instead, and expose an efficiency
score that is clearly a heuristic.
"""

import pricing

ICONS = {
    "cache": "🗄️", "ttl": "⏱️", "model": "🧭", "cold-cache": "🧊",
    "output": "📤", "concentration": "🎯", "context": "📚",
    "haiku": "🪶", "unknown": "❓", "efficiency": "⚡",
}


def _fmt_usd(x):
    if x >= 100:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    return f"${x:.3f}"


def _fmt_tok(n):
    if n >= 1e9:
        return f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}K"
    return str(int(n))


def _item(severity, kind, category, title, detail, impact=None, impact_value=0.0):
    return {
        "severity": severity, "kind": kind, "category": category,
        "icon": ICONS.get(category, "•"), "title": title, "detail": detail,
        "impact": impact, "impact_value": impact_value,
    }


def _efficiency(hit_rate, opus_share, out_share):
    """Heuristic 0-100 efficiency score with a letter grade."""
    cache_pts = 50 * max(0.0, min(1.0, hit_rate))
    # full 30 when opus_share <= 0.3, 0 when >= 1.0
    model_pts = 30 * max(0.0, 1 - max(0.0, opus_share - 0.3) / 0.7)
    # full 20 when out_share <= 0.2, 0 when >= 0.8
    out_pts = 20 * max(0.0, 1 - max(0.0, out_share - 0.2) / 0.6)
    score = round(cache_pts + model_pts + out_pts)
    score = max(0, min(100, score))
    if score >= 85:
        grade, label = "A", "Excellent"
    elif score >= 70:
        grade, label = "B", "Good"
    elif score >= 55:
        grade, label = "C", "Fair"
    else:
        grade, label = "D", "Needs work"
    return score, grade, label


def build_insights(summary):
    items = []
    totals = summary["totals"]
    by_model = summary["by_model"]
    by_project = summary["by_project"]
    sessions = summary["sessions"]

    total_cost = totals["cost"] or 0.0
    cread = totals["cache_read_tokens"]
    cwrite = totals["cache_write_tokens"]
    inp = totals["input_tokens"]
    out_tok = totals["output_tokens"]

    cache_base = cread + cwrite + inp
    hit_rate = cread / cache_base if cache_base else 0.0
    opus_cost = sum(v["cost"] for k, v in by_model.items() if "opus" in (k or "").lower())
    opus_share = opus_cost / total_cost if total_cost else 0.0
    out_cost = out_tok * (25.0 / 1e6)  # upper-bound proxy at Opus output rate
    out_share = out_cost / total_cost if total_cost else 0.0

    # ---- 1. cache hit efficiency ----
    if cache_base > 0:
        if hit_rate < 0.5:
            wasted = inp * 0.9
            items.append(_item(
                "high", "savings", "cache", f"Low cache hit rate ({hit_rate:.0%})",
                "Most input is processed fresh instead of served from cache "
                "(cache reads cost 10% of full input). Keep sessions alive within "
                "the cache TTL, avoid editing the system prompt mid-session, and "
                "don't reorder tools — any prefix change invalidates the cache.",
                f"~{_fmt_tok(wasted)} input tokens could move to cache-read rates"))
        elif hit_rate > 0.8:
            items.append(_item(
                "good", "positive", "cache", f"Strong cache reuse ({hit_rate:.0%})",
                "A high share of input is served from cache at 10% of full price. "
                "This is where caching is already paying off — keep sessions warm "
                "to hold it."))

    # ---- 2. Opus model mix (largest lever) ----
    if total_cost > 0 and opus_share > 0.6:
        shiftable = opus_cost * 0.30
        est = shiftable * (1 - 0.6)  # Sonnet ~0.6x the Opus blended rate
        items.append(_item(
            "high", "savings", "model", f"Opus is {opus_share:.0%} of your spend",
            "Opus costs $5/$25 (in/out) per Mtok vs Sonnet at $3/$15 and Haiku at "
            "$1/$5. Route simpler work — routine edits, summaries, classification, "
            "quick lookups — to Sonnet or Haiku, and reserve Opus for hard "
            "reasoning and long-horizon agentic tasks.",
            f"save ~{_fmt_usd(est)} if ~30% of Opus work moved to Sonnet", est))

    # ---- 3. 1h cache writes ----
    c1h = totals["cache_write_1h_tokens"]
    c5m = totals["cache_write_5m_tokens"]
    if c1h > 0 and (c1h + c5m) and c1h / (c1h + c5m) > 0.3:
        est = c1h * (5.0 / 1e6) * (2.0 - 1.25)
        items.append(_item(
            "medium", "savings", "ttl", f"{_fmt_tok(c1h)} tokens on the 1-hour cache",
            "1-hour cache writes cost 2x the input rate vs 1.25x for the 5-minute "
            "cache. The 1h TTL only pays off across long idle gaps; for back-to-back "
            "requests the 5-minute cache is cheaper.",
            f"up to ~{_fmt_usd(est)} if those were 5-minute writes", est))

    # ---- 4. cold cache writes ----
    cold = [s for s in sessions
            if s["cache_write_tokens"] > 50_000
            and s["cache_read_tokens"] < 0.15 * (s["cache_write_tokens"] + 1)]
    if len(cold) >= 5:
        cold_write = sum(s["cache_write_tokens"] for s in cold)
        est = cold_write * (5.0 / 1e6) * 0.25  # wasted 5m write premium proxy
        items.append(_item(
            "medium", "savings", "cold-cache",
            f"{len(cold)} sessions wrote cache they barely reused",
            "These paid the cache-write premium (1.25x-2x) but read little back — "
            "usually short sessions or ones resumed after the TTL expired. Batching "
            "related work into one continuous session recovers the write cost as "
            "cheap reads.",
            f"~{_fmt_usd(est)} of write premium with little payoff", est))

    # ---- 5. output-heavy ----
    if total_cost > 0 and out_share > 0.35:
        items.append(_item(
            "medium", "savings", "output", "Output tokens are a large share of cost",
            "Output is billed at 5x the input rate on every model. If responses run "
            "longer than needed, lower effort on routine tasks, ask for concise "
            "output, and trim oversized max_tokens on classification-style calls.",
            f"{_fmt_tok(out_tok)} output tokens generated"))

    # ---- 6. long-context sessions ----
    heavy = [s for s in sessions
             if s["assistant_messages"] >= 20
             and s["cache_read_tokens"] / (s["assistant_messages"] or 1) > 150_000]
    if len(heavy) >= 3:
        items.append(_item(
            "medium", "savings", "context",
            f"{len(heavy)} sessions carry very large context",
            "These average 150K+ cache-read tokens per message — a big context "
            "re-sent every turn. Use /compact, context editing, or start a fresh "
            "session once the working context is stale to cut the per-turn base.",
            f"{len(heavy)} heavy sessions to trim"))

    # ---- 7. Haiku opportunity ----
    haiku_cost = sum(v["cost"] for k, v in by_model.items() if "haiku" in (k or "").lower())
    haiku_share = haiku_cost / total_cost if total_cost else 0.0
    if total_cost > 5 and haiku_share < 0.02:
        items.append(_item(
            "low", "signal", "haiku", "Haiku is barely in your mix",
            "Haiku 4.5 is ~5x cheaper than Opus and handles trivial tasks well — "
            "quick lookups, formatting, short classifications, commit messages. "
            "Sending those to Haiku instead of Opus is close to free.",
            f"Haiku is {haiku_share:.0%} of spend today"))

    # ---- 8. spend concentration (SIGNAL, not savings) ----
    top_sessions = sorted(sessions, key=lambda s: s["cost"], reverse=True)[:5]
    if top_sessions and total_cost > 0:
        top_cost = sum(s["cost"] for s in top_sessions)
        if top_cost / total_cost > 0.25:
            items.append(_item(
                "low", "signal", "concentration",
                f"Top 5 sessions are {top_cost / total_cost:.0%} of spend",
                "Cost is concentrated in a few heavy sessions — the highest-leverage "
                "places to optimize. Check them (Sessions tab) for avoidable "
                "re-reads, oversized context, or Opus where a cheaper model would do.",
                f"{_fmt_usd(top_cost)} across 5 sessions"))

    if by_project:
        top_proj, tp = next(iter(by_project.items()))
        if total_cost > 0 and tp["cost"] / total_cost > 0.4:
            items.append(_item(
                "low", "signal", "concentration",
                "Spend is concentrated in one project",
                f"'{top_proj}' is {tp['cost'] / total_cost:.0%} of total cost. If "
                "it's an agentic/automated workflow, it's the best candidate for "
                "caching and model-routing wins.",
                f"{_fmt_usd(tp['cost'])} in this project"))

    # ---- 9. unpriced models ----
    if summary.get("unpriced_models"):
        names = ", ".join(summary["unpriced_models"].keys())
        items.append(_item(
            "low", "signal", "unknown", "Some usage came from unrecognized models",
            f"Messages from {names} could not be priced and are excluded from cost "
            "totals (token counts still included). Update pricing.py to bill them.",
            None))

    if not any(i["severity"] != "good" for i in items):
        items.append(_item(
            "good", "positive", "efficiency",
            "No obvious savings opportunities", "Your usage already looks efficient."))

    order = {"high": 0, "medium": 1, "low": 2, "good": 3}
    items.sort(key=lambda i: (order.get(i["severity"], 9), -i["impact_value"]))

    # headline = single largest real savings item (never a sum of overlapping figures)
    savings_items = [i for i in items if i["kind"] == "savings" and i["impact_value"] > 0]
    top_saving = max(savings_items, key=lambda i: i["impact_value"], default=None)
    score, grade, grade_label = _efficiency(hit_rate, opus_share, out_share)
    opportunity_count = sum(1 for i in items if i["kind"] == "savings")

    return {
        "score": score,
        "grade": grade,
        "grade_label": grade_label,
        "opportunity_count": opportunity_count,
        "top_saving": {
            "amount": _fmt_usd(top_saving["impact_value"]),
            "title": top_saving["title"],
        } if top_saving else None,
        "hit_rate": hit_rate,
        "items": items,
    }
