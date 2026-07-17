"""Per-model pricing and per-message cost math for Claude Code usage.

Rates are USD per 1,000,000 tokens, sourced from the authoritative Claude
pricing table (input, output). Cache economics follow the Claude prompt-caching
model:
  - cache READ      -> 0.10x the input rate
  - cache WRITE 5m  -> 1.25x the input rate
  - cache WRITE 1h  -> 2.00x the input rate

The four token buckets in a `usage` object are DISJOINT (input_tokens is the
uncached remainder), so each is priced at its own rate and summed. Nothing is
subtracted to "avoid double counting".
"""

# input, output USD / 1e6 tokens
PRICES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),   # standard rate (per user decision)
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Cache multipliers relative to the input rate.
CACHE_READ_MULT = 0.10
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.00

SYNTHETIC = "<synthetic>"


def normalize_model(model):
    """Collapse dated snapshot IDs onto their base alias (e.g. the haiku
    ...-20251001 snapshot -> claude-haiku-4-5). Returns the model unchanged if
    it is already a known base alias."""
    if not model:
        return model
    if model in PRICES:
        return model
    # Strip a trailing -YYYYMMDD date snapshot and retry.
    parts = model.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        base = parts[0]
        if base in PRICES:
            return base
    return model


def is_synthetic(model):
    return model == SYNTHETIC


def is_priced(model):
    return normalize_model(model) in PRICES


def _cache_write_split(usage):
    """Return (ephemeral_5m, ephemeral_1h) cache-creation token counts.

    Prefers the detailed cache_creation breakdown; falls back to treating the
    flat cache_creation_input_tokens as a 5-minute write when the breakdown is
    absent.
    """
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        return (
            cc.get("ephemeral_5m_input_tokens", 0) or 0,
            cc.get("ephemeral_1h_input_tokens", 0) or 0,
        )
    flat = usage.get("cache_creation_input_tokens", 0) or 0
    return (flat, 0)


def cost_breakdown(model, usage):
    """Return a dict with per-bucket cost + tokens for one message's `usage`.

    Thin wrapper: extracts the five disjoint token buckets from the raw usage
    object and delegates the arithmetic to cost_from_buckets so the live parse
    and the persisted-warehouse path price identically.
    """
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cread = usage.get("cache_read_input_tokens", 0) or 0
    w5m, w1h = _cache_write_split(usage)
    return cost_from_buckets(model, inp, out, cread, w5m, w1h)


def cost_from_buckets(model, inp, out, cread, w5m, w1h):
    """Price one message from its already-split token buckets.

    This is the single source of truth for cost math. The warehouse stores raw
    buckets (never cost) and calls this at read time, so edits to PRICES re-price
    all history — including rows whose source logs Claude has since pruned.

    `priced` is False for synthetic/unknown models (cost contributions are 0 so
    they never silently inflate totals, but token counts are still reported).
    """
    inp = inp or 0
    out = out or 0
    cread = cread or 0
    w5m = w5m or 0
    w1h = w1h or 0

    norm = normalize_model(model)
    priced = norm in PRICES and not is_synthetic(model)

    if priced:
        in_rate, out_rate = PRICES[norm]
        in_rate /= 1e6
        out_rate /= 1e6
        cost = (
            inp * in_rate
            + out * out_rate
            + cread * in_rate * CACHE_READ_MULT
            + w5m * in_rate * CACHE_WRITE_5M_MULT
            + w1h * in_rate * CACHE_WRITE_1H_MULT
        )
        # Net savings from prompt caching vs. a no-cache world where every
        # cache-read token would have been billed as fresh input (1.0x) and
        # cache writes carried no premium:
        #   read discount:  cread * in_rate * (1.0 - 0.10)
        #   write premium:  w5m * in_rate * (1.25 - 1.0) + w1h * in_rate * (2.0 - 1.0)
        cache_saving = (
            cread * in_rate * (1.0 - CACHE_READ_MULT)
            - w5m * in_rate * (CACHE_WRITE_5M_MULT - 1.0)
            - w1h * in_rate * (CACHE_WRITE_1H_MULT - 1.0)
        )
    else:
        cost = 0.0
        cache_saving = 0.0

    return {
        "priced": priced,
        "cost": cost,
        "cache_saving": cache_saving,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cread,
        "cache_write_5m_tokens": w5m,
        "cache_write_1h_tokens": w1h,
        "cache_write_tokens": w5m + w1h,
        "total_tokens": inp + out + cread + w5m + w1h,
    }
