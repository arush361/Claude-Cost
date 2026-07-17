"""Verification gate for the SQLite warehouse.

Asserts that the warehouse-backed build_summary() equals the direct in-memory
parse (build_summary_live()) to the penny on the same non-pruned data. If this
fails, the persistence refactor changed the numbers and must not ship.

    python3 verify_warehouse.py
"""

import os
import sys
import tempfile

import history
import parser

CENT = 1e-6  # cost tolerance (same arithmetic -> effectively exact)


def _fail(msg):
    print(f"  FAIL: {msg}")
    return False


def _cmp_bucket(name, a, b):
    ok = True
    for k, av in a.items():
        bv = b.get(k)
        if isinstance(av, float):
            if abs(av - (bv or 0)) > CENT:
                ok = _fail(f"{name}.{k}: live={av} warehouse={bv}")
        elif av != bv:
            ok = _fail(f"{name}.{k}: live={av} warehouse={bv}")
    return ok


def main():
    # Isolate the warehouse in a throwaway DB so the check reflects exactly the
    # current live logs, independent of any previously persisted history.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    history.set_db_path(tmp.name)

    print("building live (in-memory) summary…")
    live = parser.build_summary_live()
    print("building warehouse-backed summary…")
    parser._CACHE["signature"] = None  # force re-aggregate through the DB
    warehouse = parser.build_summary(force=True)

    ok = True

    print("totals…")
    ok &= _cmp_bucket("totals", live["totals"], warehouse["totals"])

    for section in ("by_model", "by_project", "by_day"):
        print(f"{section}…")
        lk, wk = set(live[section]), set(warehouse[section])
        if lk != wk:
            ok &= _fail(f"{section} keys differ: only-live={lk - wk} only-wh={wk - lk}")
        for key in lk & wk:
            ok &= _cmp_bucket(f"{section}[{key}]", live[section][key], warehouse[section][key])

    print("max_message…")
    lm, wm = live["max_message"], warehouse["max_message"]
    if (lm is None) != (wm is None):
        ok &= _fail(f"max_message presence differs: {lm} vs {wm}")
    elif lm is not None:
        for k in ("cost", "total_tokens", "output_tokens"):
            av, bv = lm[k], wm[k]
            if isinstance(av, float):
                if abs(av - bv) > CENT:
                    ok &= _fail(f"max_message.{k}: {av} vs {bv}")
            elif av != bv:
                ok &= _fail(f"max_message.{k}: {av} vs {bv}")

    print(f"sessions: live={live['totals']['sessions']} warehouse={warehouse['totals']['sessions']}")

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass

    print()
    if ok:
        print("PASS — warehouse matches the in-memory parse to the penny.")
        sys.exit(0)
    print("FAIL — warehouse diverges from the in-memory parse (see above).")
    sys.exit(1)


if __name__ == "__main__":
    main()
