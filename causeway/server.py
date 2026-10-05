"""`causeway serve`: the investigation app, its JSON API, and a token-protected ingestion endpoint.

GET  /                         the app
GET  /api/workspace            run summaries + cross-run influence graph
GET  /api/runs/<id>            everything the app shows for one run
POST /api/runs/<id>/tests      {"intervention","target","n"} -> counterfactual replay (allow-listed programs only)
POST /v1/ingest                Authorization: Bearer <token>; {"events":[...], "blobs":{ref: value}}

Security posture (prototype): binds to 127.0.0.1 by default. The read API and the app have no auth,
so do not expose them beyond localhost. Ingest needs the bearer token and validates every event's hash,
its link to the previous event, and every blob's content hash before anything is written. Replay
imports the program a run names, so only programs passed with --allow-program can be replayed.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from .core import GENESIS, SCHEMA, content_hash, event_hash, load_run
from .evals import list_runs
from .replay import counterfactual, load_system
from .view import render_app, run_detail
from .analysis import workspace

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
MAX_BODY = 20 * 1024 * 1024


class IngestError(ValueError):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class Store:
    def __init__(self, root: str, token: Optional[str], allow_programs: Iterable[str] = ()):
        self.root = root
        self.token = token
        self.allow = set(allow_programs)
        self.lock = threading.Lock()
        os.makedirs(root, exist_ok=True)

    def paths(self) -> List[str]:
        return list_runs(self.root) if os.listdir(self.root) else []

    def run_path(self, run_id: str) -> str:
        if not RUN_ID.match(run_id):
            raise IngestError(400, "bad run id")
        return os.path.join(self.root, run_id)

    # -- ingest
    def ingest(self, body: Dict[str, Any]) -> Dict[str, Any]:
        events, blobs = body.get("events"), body.get("blobs") or {}
        if not isinstance(events, list) or not events or not isinstance(blobs, dict):
            raise IngestError(400, "expected {events: [...], blobs: {...}}")
        for ref, val in blobs.items():
            if content_hash(val) != ref:
                raise IngestError(422, f"blob {str(ref)[:20]} does not match its hash")
        run_id = events[0].get("run_id", "")
        if any(e.get("run_id") != run_id for e in events):
            raise IngestError(400, "one batch must belong to one run")
        path = self.run_path(run_id)
        with self.lock:
            os.makedirs(os.path.join(path, "blobs"), exist_ok=True)
            ev_path = os.path.join(path, "events.jsonl")
            last_seq, last_hash = -1, GENESIS
            if os.path.exists(ev_path):
                with open(ev_path, "rb") as f:
                    lines = [l for l in f.read().splitlines() if l.strip()]
                if lines:
                    last = json.loads(lines[-1])
                    last_seq, last_hash = last["seq"], last["hash"]
            for ev in events:
                if ev.get("schema") != SCHEMA:
                    raise IngestError(422, "unknown schema")
                if ev.get("seq") != last_seq + 1:
                    raise IngestError(409, f"expected seq {last_seq + 1}, got {ev.get('seq')}")
                if ev.get("prev_hash") != last_hash:
                    raise IngestError(409, f"seq {ev['seq']}: does not link to the stored chain")
                if event_hash(ev) != ev.get("hash"):
                    raise IngestError(422, f"seq {ev['seq']}: hash mismatch")
                last_seq, last_hash = ev["seq"], ev["hash"]
            known = set()
            bdir = os.path.join(path, "blobs")
            known.update("sha256:" + n[:-5] for n in os.listdir(bdir))
            known.update(blobs)
            for ev in events:
                for k in ("ref", "output", "args_ref", "result_ref"):
                    if ev.get(k) and ev[k] not in known:
                        raise IngestError(422, f"seq {ev['seq']}: missing blob {ev[k][:20]}")
                for c in ev.get("context", []):
                    if c["ref"] not in known:
                        raise IngestError(422, f"seq {ev['seq']}: missing blob {c['ref'][:20]}")
            for ref, val in blobs.items():
                p = os.path.join(bdir, ref[7:] + ".json")
                if not os.path.exists(p):
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump({"v": val}, f, sort_keys=True, ensure_ascii=False, default=str)
            with open(ev_path, "a", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev, sort_keys=True, ensure_ascii=False) + "\n")
        return {"run_id": run_id, "accepted": len(events), "head_seq": last_seq}

    # -- tests
    def test(self, run_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        path = self.run_path(run_id)
        if not os.path.exists(os.path.join(path, "events.jsonl")):
            raise IngestError(404, "no such run")
        run = load_run(path)
        prog = run.start.get("program")
        if not prog or prog not in self.allow:
            raise IngestError(403, f"replay of program {prog!r} is not allowed on this server (--allow-program)")
        n = int(body.get("n", 30))
        if not 1 <= n <= 200:
            raise IngestError(400, "n must be 1..200")
        inter, target = str(body.get("intervention", "")), str(body.get("target", ""))
        if not inter or not target:
            raise IngestError(400, "intervention and target are required")
        try:
            return counterfactual(run, inter, target, n=n, system=load_system(prog))
        except ValueError as e:
            raise IngestError(400, str(e))


def make_handler(store: Store):
    class H(BaseHTTPRequestHandler):
        server_version = "causeway/0.2"

        def log_message(self, fmt, *args):  # quieter
            pass

        def _send(self, status: int, body: Any, ctype: str = "application/json") -> None:
            data = body if isinstance(body, (bytes, bytearray)) else (
                body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode())
            self.send_response(status)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> Dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise IngestError(413, "body too large")
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                raise IngestError(400, "invalid JSON")

        def do_GET(self):
            p = urlparse(self.path).path
            try:
                if p in ("/", "/index.html"):
                    return self._send(200, render_app(None, server=True), "text/html")
                if p == "/api/workspace":
                    return self._send(200, workspace(store.paths()))
                m = re.match(r"^/api/runs/([^/]+)$", p)
                if m:
                    path = store.run_path(unquote(m.group(1)))
                    if not os.path.exists(os.path.join(path, "events.jsonl")):
                        return self._send(404, {"error": "no such run"})
                    return self._send(200, run_detail(load_run(path)))
                if p == "/healthz":
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})
            except IngestError as e:
                self._send(e.status, {"error": str(e)})

        def do_POST(self):
            p = urlparse(self.path).path
            try:
                if p == "/v1/ingest":
                    auth = self.headers.get("Authorization", "")
                    if not store.token or not hmac.compare_digest(auth, f"Bearer {store.token}"):
                        return self._send(401, {"error": "unauthorized"})
                    return self._send(200, store.ingest(self._body()))
                m = re.match(r"^/api/runs/([^/]+)/tests$", p)
                if m:
                    if self.headers.get("Origin") and urlparse(self.headers["Origin"]).netloc != self.headers.get("Host"):
                        return self._send(403, {"error": "cross-origin request refused"})
                    return self._send(200, store.test(unquote(m.group(1)), self._body()))
                self._send(404, {"error": "not found"})
            except IngestError as e:
                self._send(e.status, {"error": str(e)})

    return H


def serve(root: str, host: str = "127.0.0.1", port: int = 7788, token: Optional[str] = None,
          allow_programs: Iterable[str] = ()) -> Tuple[ThreadingHTTPServer, Store]:
    store = Store(root, token, allow_programs)
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    return httpd, store
