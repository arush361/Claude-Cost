"""Persistent SQLite warehouse for Claude usage records.

Two jobs, one mechanism:

  * PERSISTENCE — Claude Code prunes its own JSONL logs after ~30 days. We
    ingest each session once, keyed on the stable `message.id`, and never delete
    rows when the source log disappears. Your spend history outlives the logs.

  * INCREMENTAL CACHE — sync() only re-parses a file whose (mtime, size) changed,
    so cold start no longer walks ~778 MB every launch.

Design note: this is a persistent *record cache*, not an aggregation engine. It
stores raw token buckets (never cost) and the non-message-grain session fields;
all cost math and roll-ups stay in pricing.py / parser.py so the penny-verified
arithmetic is unchanged. Cost is priced at read time, so editing PRICES re-prices
all history — even pruned rows.

Storage lives outside the repo at ~/.claude-cost/history.db (override with the
CLAUDE_COST_DB env var or set_db_path()).
"""

import os
import sqlite3
import threading

# Bump when parse_session_file's output shape/semantics change so on-disk logs
# get re-parsed (pruned rows, which can't be re-parsed, are preserved).
_PARSER_VERSION = 2

_DEFAULT_DB = os.path.expanduser("~/.claude-cost/history.db")
_DB_PATH = os.environ.get("CLAUDE_COST_DB") or _DEFAULT_DB
_WRITE_LOCK = threading.Lock()
_INIT_DONE = set()  # db paths whose schema has been ensured


def set_db_path(path):
    """Point the warehouse at a specific file (used by app.py --db)."""
    global _DB_PATH
    _DB_PATH = os.path.expanduser(path)


def db_path():
    return _DB_PATH


def _connect():
    """Fresh connection per call. ThreadingHTTPServer means connections must not
    be shared across threads; WAL keeps concurrent readers happy while a writer
    (serialized by _WRITE_LOCK) commits."""
    path = _DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if path not in _INIT_DONE:
        _init_schema(conn)
        _INIT_DONE.add(path)
    return conn


def _init_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            path       TEXT PRIMARY KEY,
            mtime      INTEGER,
            size       INTEGER,
            session_id TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id   TEXT PRIMARY KEY,
            cwd          TEXT,
            git_branch   TEXT,
            first_ts     TEXT,
            last_ts      TEXT,
            user_prompts INTEGER,
            compactions  INTEGER
        );
        CREATE TABLE IF NOT EXISTS messages (
            message_id            TEXT PRIMARY KEY,
            session_id            TEXT,
            model                 TEXT,
            ts                    TEXT,
            input_tokens          INTEGER,
            output_tokens         INTEGER,
            cache_read_tokens     INTEGER,
            cache_write_5m_tokens INTEGER,
            cache_write_1h_tokens INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE TABLE IF NOT EXISTS attribution (
            session_id   TEXT,
            kind         TEXT,
            key          TEXT,
            calls        INTEGER,
            result_bytes INTEGER,
            PRIMARY KEY (session_id, kind, key)
        );
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """
    )
    conn.commit()


def _check_parser_version(conn):
    """If the parser logic changed, clear the file-signature table so every
    on-disk log re-parses on the next sync. Rows for pruned files remain."""
    row = conn.execute("SELECT v FROM meta WHERE k = 'parser_version'").fetchone()
    stored = int(row["v"]) if row else 0
    if stored != _PARSER_VERSION:
        conn.execute("DELETE FROM files")
        conn.execute(
            "INSERT OR REPLACE INTO meta (k, v) VALUES ('parser_version', ?)",
            (str(_PARSER_VERSION),),
        )
        conn.commit()


def sync(files):
    """Ingest any changed/new log files. Files present in the DB but absent from
    `files` are treated as pruned and left intact (that is the persistence)."""
    import parser  # lazy: parser imports history

    conn = _connect()
    with _WRITE_LOCK:
        _check_parser_version(conn)
        known = {
            row["path"]: (row["mtime"], row["size"])
            for row in conn.execute("SELECT path, mtime, size FROM files")
        }
        for path in files:
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig = (int(st.st_mtime), st.st_size)
            if known.get(path) == sig:
                continue  # unchanged -> skip re-parse (incremental cache)

            meta, messages, attribution = parser.parse_session_file(path)
            if meta is None:
                continue
            sid = meta["session_id"]

            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(session_id, cwd, git_branch, first_ts, last_ts, user_prompts, compactions) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    sid, meta["cwd"], meta["git_branch"],
                    meta["first_ts"].isoformat() if meta["first_ts"] else None,
                    meta["last_ts"].isoformat() if meta["last_ts"] else None,
                    meta["user_prompts"], meta["compactions"],
                ),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO messages "
                "(message_id, session_id, model, ts, input_tokens, output_tokens, "
                " cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        m["message_id"], sid, m["model"],
                        m["ts"].isoformat() if m["ts"] else None,
                        m["input_tokens"], m["output_tokens"],
                        m["cache_read_tokens"], m["cache_write_5m_tokens"],
                        m["cache_write_1h_tokens"],
                    )
                    for m in messages
                ],
            )
            # Attribution is a full per-session recompute from the (re-)parsed
            # file, so replace this session's rows wholesale.
            conn.execute("DELETE FROM attribution WHERE session_id = ?", (sid,))
            conn.executemany(
                "INSERT OR REPLACE INTO attribution "
                "(session_id, kind, key, calls, result_bytes) VALUES (?,?,?,?,?)",
                [(sid, a["kind"], a["key"], a["calls"], a["result_bytes"])
                 for a in attribution],
            )
            conn.execute(
                "INSERT OR REPLACE INTO files (path, mtime, size, session_id) "
                "VALUES (?,?,?,?)",
                (path, sig[0], sig[1], sid),
            )
        conn.commit()
    conn.close()


def iter_sessions():
    """Yield (session_meta, message_rows) for every persisted session, for the
    parser aggregation loop. Includes sessions whose logs were pruned."""
    conn = _connect()
    try:
        sess = {
            row["session_id"]: {
                "session_id": row["session_id"],
                "cwd": row["cwd"],
                "git_branch": row["git_branch"],
                "first_ts": row["first_ts"],
                "last_ts": row["last_ts"],
                "user_prompts": row["user_prompts"] or 0,
                "compactions": row["compactions"] or 0,
            }
            for row in conn.execute("SELECT * FROM sessions")
        }
        by_session = {sid: [] for sid in sess}
        for m in conn.execute("SELECT * FROM messages"):
            by_session.setdefault(m["session_id"], []).append({
                "model": m["model"],
                "ts": m["ts"],
                "input_tokens": m["input_tokens"] or 0,
                "output_tokens": m["output_tokens"] or 0,
                "cache_read_tokens": m["cache_read_tokens"] or 0,
                "cache_write_5m_tokens": m["cache_write_5m_tokens"] or 0,
                "cache_write_1h_tokens": m["cache_write_1h_tokens"] or 0,
            })
    finally:
        conn.close()

    for sid, meta in sess.items():
        yield meta, by_session.get(sid, [])
    # Messages whose session row somehow went missing: still surface them.
    for sid, rows in by_session.items():
        if sid not in sess:
            yield {
                "session_id": sid, "cwd": None, "git_branch": None,
                "first_ts": None, "last_ts": None,
                "user_prompts": 0, "compactions": 0,
            }, rows


def get_records():
    """Flat per-message records (priced) for the filterable Usage view."""
    import pricing

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT m.session_id AS session_id, s.cwd AS cwd, m.ts AS ts, "
            "       m.model AS model, m.input_tokens AS input_tokens, "
            "       m.output_tokens AS output_tokens, m.cache_read_tokens AS cache_read_tokens, "
            "       m.cache_write_5m_tokens AS w5m, m.cache_write_1h_tokens AS w1h "
            "FROM messages m LEFT JOIN sessions s ON m.session_id = s.session_id"
        ).fetchall()
    finally:
        conn.close()

    records = []
    for r in rows:
        bd = pricing.cost_from_buckets(
            r["model"], r["input_tokens"], r["output_tokens"],
            r["cache_read_tokens"], r["w5m"], r["w1h"],
        )
        ts = r["ts"]
        records.append({
            "session_id": r["session_id"],
            "project": r["cwd"] or "(unknown)",
            "date": ts[:10] if ts else None,
            "ts": ts,
            "model": pricing.normalize_model(r["model"]) or "(none)",
            "cost": bd["cost"],
            "input_tokens": bd["input_tokens"],
            "output_tokens": bd["output_tokens"],
            "cache_read_tokens": bd["cache_read_tokens"],
            "cache_write_tokens": bd["cache_write_tokens"],
            "cache_write_1h_tokens": bd["cache_write_1h_tokens"],
            "total_tokens": bd["total_tokens"],
        })
    return records


def get_attribution():
    """Global tool/file/skill/subagent aggregates for the Activity tab."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT kind, key, SUM(calls) AS calls, SUM(result_bytes) AS result_bytes "
            "FROM attribution GROUP BY kind, key"
        ).fetchall()
    finally:
        conn.close()

    out = {"tool": [], "file": [], "skill": [], "subagent": []}
    for r in rows:
        out.setdefault(r["kind"], []).append({
            "key": r["key"],
            "calls": r["calls"] or 0,
            "result_bytes": r["result_bytes"] or 0,
        })
    for kind in out:
        out[kind].sort(key=lambda x: (x["calls"], x["result_bytes"]), reverse=True)
    return out
