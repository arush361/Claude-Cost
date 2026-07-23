"""Zero-dependency server for ClaudeLens.

Local-first: reads ~/.claude/projects, computes cost/usage aggregates, and
serves them as JSON to a single-page frontend. No cloud, no telemetry, and no
third-party packages — just the Python standard library.

    python3 app.py [--host 127.0.0.1] [--port 5000]
"""

import argparse
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import history
import insights
import parser
import wrapped

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "templates")
STATIC = os.path.join(HERE, "static")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console
        return

    def _send(self, status, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send(404, {"error": "not found"})
            return
        self._send(200, data, ctype)

    def do_GET(self):
        route = urlparse(self.path).path

        if route == "/" or route == "/index.html":
            self._file(os.path.join(TEMPLATES, "index.html"), "text/html; charset=utf-8")
            return

        if route.startswith("/static/"):
            rel = os.path.normpath(route[len("/static/"):])
            if rel.startswith(".."):
                self._send(403, {"error": "forbidden"})
                return
            full = os.path.join(STATIC, rel)
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            self._file(full, ctype)
            return

        if route == "/api/summary":
            try:
                force = "refresh=1" in (urlparse(self.path).query or "")
                summary = parser.build_summary(force=force)
                payload = dict(summary)
                payload["insights"] = insights.build_insights(summary)
                payload["wrapped"] = wrapped.build_wrapped(summary)
                self._send(200, payload)
            except Exception as e:  # surface parse errors to the UI
                self._send(500, {"error": str(e)})
            return

        if route == "/api/usage":
            try:
                q = parse_qs(urlparse(self.path).query)
                view = parser.usage_view(
                    project=(q.get("project", [""])[0] or None),
                    date_from=(q.get("from", [""])[0] or None),
                    date_to=(q.get("to", [""])[0] or None),
                )
                self._send(200, view)
            except Exception as e:
                self._send(500, {"error": str(e)})
            return

        if route == "/api/tools":
            try:
                parser.build_summary()  # ensures the warehouse is synced
                self._send(200, history.get_attribution())
            except Exception as e:
                self._send(500, {"error": str(e)})
            return

        if route.startswith("/api/session/"):
            sid = unquote(route[len("/api/session/"):])
            detail = parser.session_detail(sid)
            if detail is None:
                self._send(404, {"error": "session not found"})
            else:
                self._send(200, detail)
            return

        self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="ClaudeLens")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--db", default=None,
                    help="warehouse DB path (default ~/.claude-cost/history.db "
                         "or $CLAUDE_COST_DB)")
    args = ap.parse_args()
    if args.db:
        history.set_db_path(args.db)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ClaudeLens -> http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
        server.shutdown()


if __name__ == "__main__":
    main()
