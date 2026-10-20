"""Disposable SQLite-backed reference adapter. Not a production webhook receiver."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit


class Store:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.executescript("""
            CREATE TABLE effects (mode TEXT, entity TEXT, event TEXT);
            CREATE TABLE seen (mode TEXT, event TEXT, PRIMARY KEY (mode, event));
            CREATE TABLE entities (mode TEXT, entity TEXT, sequence INTEGER, state TEXT,
                                   PRIMARY KEY (mode, entity));
            """)

    def apply(self, mode, event):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            if mode == "fixed":
                inserted = db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (mode, event["id"])).rowcount
                if not inserted:
                    return
            db.execute("INSERT INTO effects VALUES (?, ?, ?)", (mode, event["entity_id"], event["id"]))
            sql = """INSERT INTO entities VALUES (?, ?, ?, ?) ON CONFLICT(mode, entity)
                     DO UPDATE SET sequence=excluded.sequence, state=excluded.state"""
            if mode == "fixed":
                sql += " WHERE excluded.sequence > entities.sequence"
            db.execute(sql, (mode, event["entity_id"], event["sequence"], event["state"]))

    def read(self, mode, entity):
        # Separate connection queries the durable effects table, never request counters.
        with sqlite3.connect(self.path) as db:
            effects = [row[0] for row in db.execute("SELECT event FROM effects WHERE mode=? AND entity=? ORDER BY event", (mode, entity))]
            state = db.execute("SELECT state FROM entities WHERE mode=? AND entity=?", (mode, entity)).fetchone()
        return {"effect_count": len(effects), "applied_event_ids": effects,
                "state": state[0] if state else None}


@contextmanager
def demo_server():
    with tempfile.TemporaryDirectory(prefix="webhook-chaos-") as directory:
        store = Store(directory + "/effects.sqlite3")
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, code, body):
                data = json.dumps(body).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Expected for the intentional client timeout.
            def do_POST(self):
                mode = urlsplit(self.path).path.strip("/")
                if mode not in {"broken", "fixed"}:
                    return self.respond(404, {"error": "unknown mode"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384:
                        raise ValueError("invalid body size")
                    event = json.loads(self.rfile.read(length))
                    if event.get("test") is not True or not all(isinstance(event.get(k), str) for k in ("id", "entity_id", "state")) or type(event.get("sequence")) is not int:
                        raise ValueError("invalid test event")
                    delay = int(self.headers.get("X-Chaos-Delay-Response-Ms", "0"))
                    if not 0 <= delay <= 2000:
                        raise ValueError("invalid response delay")
                except (ValueError, TypeError, AttributeError):
                    return self.respond(400, {"error": "invalid demo request"})
                store.apply(mode, event)
                time.sleep(delay / 1000)
                self.respond(200, {"accepted": True})
            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                mode = query.get("mode", [""])[0]
                entity = query.get("entity_id", [""])[0]
                if parsed.path != "/state" or mode not in {"broken", "fixed"} or not entity:
                    return self.respond(400, {"error": "expected /state?mode=fixed&entity_id=..."})
                self.respond(200, store.read(mode, entity))
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
