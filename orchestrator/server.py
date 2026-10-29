"""QDKEP Knowledge Console — stdlib HTTP server (orchestrator).

ThreadingHTTPServer with route_get/route_post dispatch, mirroring the native
QDKEP execution plane. No FastAPI, no external web framework. The Anthropic API
is used only as the answer generator, behind a callable.

Endpoints:
  GET  /health
  POST /upload   {filename, text}
  POST /search   {query, top_k?, customer_id?, system_id?, interface_id?, artifact_type?}
  POST /answer   {query, top_k?, ...filters}

Run:  python -m orchestrator.server
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from orchestrator import db as dbmod
from orchestrator import handlers


def make_anthropic_generator():
    """Return a generate(prompt)->str callable backed by the Anthropic API.

    Falls back to an explicit error string if the SDK/key is unavailable, so the
    server still runs for retrieval-only testing.
    """
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    except Exception:
        return lambda prompt: "[answer generation unavailable: no ANTHROPIC_API_KEY]"

    def generate(prompt: str) -> str:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    return generate


class Handler(BaseHTTPRequestHandler):
    server_version = "QDKEP/1.0"

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # --- routing -----------------------------------------------------------
    def route_get(self, path: str):
        if path == "/health":
            return self._send(200, {"status": "ok", "service": "qdkep-console"})
        if path in ("/", "/index.html"):
            return self._send_html()
        # DB-backed read endpoints (support an optional ?id= / ?limit= query).
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(path)
        route = parsed.path
        q = parse_qs(parsed.query)
        if route in ("/indexes", "/dashboard", "/documents", "/document"):
            import traceback
            conn = None
            try:
                conn = self.server.connect()
                if route == "/indexes":
                    status, payload = handlers.handle_indexes(conn)
                elif route == "/dashboard":
                    status, payload = handlers.handle_dashboard(conn)
                elif route == "/documents":
                    limit = int(q.get("limit", ["100"])[0])
                    offset = int(q.get("offset", ["0"])[0])
                    status, payload = handlers.handle_documents(conn, limit=limit, offset=offset)
                else:  # /document?id=N
                    doc_id = int(q.get("id", ["0"])[0])
                    status, payload = handlers.handle_document_detail(conn, doc_id)
            except Exception as exc:
                traceback.print_exc()
                status, payload = 500, {"error": type(exc).__name__, "detail": str(exc)}
            finally:
                if conn is not None:
                    conn.close()
            return self._send(status, payload)
        return self._send(404, {"error": "not found"})

    def _send_html(self):
        here = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(here, "platform_c_dashboard.html")
        try:
            with open(candidate, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send(404, {"error": "dashboard not found"})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route_post(self, path: str, body: dict):
        import traceback
        conn = None
        try:
            conn = self.server.connect()
            embedder = getattr(self.server, "embedder", None)
            if path == "/upload":
                status, payload = handlers.handle_upload(
                    conn, filename=body.get("filename", "upload.md"),
                    raw_text=body.get("text", ""), embedder=embedder,
                    chunk_mode=body.get("chunk_mode", "bm25_typed"),
                    chunk_size=body.get("chunk_size"),
                    chunk_overlap=body.get("chunk_overlap"),
                )
            elif path == "/search":
                status, payload = handlers.handle_search(
                    conn, query=body.get("query", ""),
                    top_k=int(body.get("top_k", 5)), embedder=embedder,
                    customer_id=body.get("customer_id"),
                    system_id=body.get("system_id"),
                    interface_id=body.get("interface_id"),
                    artifact_type=body.get("artifact_type"),
                )
            elif path == "/answer":
                status, payload = handlers.handle_answer(
                    conn, query=body.get("query", ""),
                    generate=self.server.generate, embedder=embedder,
                    top_k=int(body.get("top_k", 5)),
                    customer_id=body.get("customer_id"),
                    system_id=body.get("system_id"),
                    interface_id=body.get("interface_id"),
                    artifact_type=body.get("artifact_type"),
                )
            elif path == "/benchmark":
                status, payload = handlers.handle_benchmark(
                    conn, query_set=body.get("query_set", []),
                    generate=self.server.generate, embedder=embedder,
                    static_categories=body.get("static_categories"),
                )
            elif path == "/delete":
                status, payload = handlers.handle_delete_document(
                    conn, int(body.get("id", 0)),
                )
            else:
                status, payload = 404, {"error": "not found"}
        except Exception as exc:
            traceback.print_exc()
            status, payload = 500, {"error": type(exc).__name__, "detail": str(exc)}
        finally:
            if conn is not None:
                conn.close()
        return self._send(status, payload)

    def do_GET(self):
        self.route_get(self.path)

    def do_POST(self):
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid json"})
        self.route_post(self.path, body)

    def log_message(self, fmt, *args):
        print("[qdkep]", self.command, self.path, "->", args[1] if len(args) > 1 else "")


def build_server(host: str = "127.0.0.1", port: int = 8800) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.connect = dbmod.connect
    httpd.generate = make_anthropic_generator()
    return httpd


def main():
    from orchestrator.env import load_dotenv
    load_dotenv()  # load .env if present (does not override real shell vars)
    host = os.environ.get("QDKEP_HOST", "127.0.0.1")
    port = int(os.environ.get("QDKEP_PORT", "8800"))
    httpd = build_server(host, port)
    print(f"QDKEP Knowledge Console on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
