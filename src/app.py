from __future__ import annotations

import base64
import json
import os
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .core import LogWatcher, SwitchEngine
from .newapi import PostgresChannelRepository, UpstreamTester
from .store import ConfigStore


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


class Service:
    def __init__(self):
        self.store = ConfigStore(os.getenv("CONFIG_DB", str(ROOT / "data/config.db")))
        self.repository = PostgresChannelRepository(os.environ["NEWAPI_DB_DSN"])
        self.tester = UpstreamTester(os.getenv("TEST_MODEL", "gpt-6.1-sol"), float(os.getenv("TEST_TIMEOUT", "20")))
        self.engine = SwitchEngine(self.repository, self.store, self.tester, self.record_event)
        self.watcher = LogWatcher(os.getenv("NEWAPI_LOG_DIR", "/var/lib/newapi/logs"), self.on_503, float(os.getenv("LOG_POLL_INTERVAL", "1")))

    def record_event(self, event: dict):
        self.store.add_event(json.dumps(event, ensure_ascii=False))

    def on_503(self, channel_id: int, message: str):
        self.engine.handle_503(channel_id, message)

    def start(self):
        self.watcher.start()

    def stop(self):
        self.watcher.stop()


class Handler(BaseHTTPRequestHandler):
    server_version = "newapi-channel-switcher/0.1"

    def _service(self) -> Service:
        return self.server.service  # type: ignore[attr-defined]

    def _auth(self) -> bool:
        expected_user = os.getenv("ADMIN_USER", "root")
        expected_password = os.getenv("ADMIN_PASSWORD", "")
        if not expected_password:
            return False
        value = self.headers.get("Authorization", "")
        if not value.startswith("Basic "):
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="newapi-channel-switcher"'); self.send_header("Content-Length", "0"); self.end_headers(); return False
        try:
            decoded = base64.b64decode(value[6:]).decode()
            user, password = decoded.split(":", 1)
        except Exception:
            user = password = ""
        if not (secrets.compare_digest(user, expected_user) and secrets.compare_digest(password, expected_password)):
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="newapi-channel-switcher"'); self.send_header("Content-Length", "0"); self.end_headers(); return False
        return True

    def _json(self, status: int, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0")); return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._json(200, {"ok": True, "service": "newapi渠道切换"})
        if path == "/" or path == "/index.html":
            data = (STATIC / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if not self._auth(): return
        service = self._service(); store = service.store
        try:
            if path == "/api/groups":
                groups = store.groups()
                for group in groups:
                    group["channels"] = store.candidates(group["id"])
                return self._json(200, {"groups": groups})
            if path == "/api/channels":
                channels = [c.__dict__ | {"key": ""} for c in service.repository.get_channels()]
                return self._json(200, {"channels": channels})
            if path == "/api/events":
                return self._json(200, {"events": store.recent_events()})
            self._json(404, {"error": "不存在的接口"})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def do_POST(self):
        if not self._auth(): return
        path = urlparse(self.path).path; service = self._service(); store = service.store
        try:
            body = self._body()
            if path == "/api/groups":
                group = store.create_group(str(body.get("name", "")).strip())
                return self._json(201, group)
            if path.startswith("/api/groups/") and path.endswith("/channels"):
                group_id = int(path.split("/")[3]); rows = body.get("channels", [])
                store.replace_candidates(group_id, rows); return self._json(200, {"ok": True, "channels": store.candidates(group_id)})
            if path.startswith("/api/groups/") and path.endswith("/activate"):
                group_id = int(path.split("/")[3]); result = service.engine.test_and_activate(group_id, int(body["channel_id"])); return self._json(200, {"events": result})
            if path.startswith("/api/groups/"):
                group_id = int(path.split("/")[3]); return self._json(200, store.update_group(group_id, body.get("name"), body.get("enabled")))
            if path == "/api/test-channel":
                channel_id = int(body["channel_id"]); channel = next((c for c in service.repository.get_channels([channel_id]) if c.id == channel_id), None)
                if not channel: return self._json(404, {"error": "渠道不存在"})
                result = service.tester(channel); return self._json(200, result.__dict__)
            self._json(404, {"error": "不存在的接口"})
        except Exception as exc:
            self._json(400, {"error": str(exc)})

    def do_DELETE(self):
        if not self._auth(): return
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/groups/"):
                group_id = int(path.split("/")[3]); self._service().store.delete_group(group_id); return self._json(200, {"ok": True})
            self._json(404, {"error": "不存在的接口"})
        except Exception as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, fmt, *args):
        print("[http]", fmt % args)


def main():
    service = Service(); service.start()
    host = os.getenv("BIND_HOST", "0.0.0.0"); port = int(os.getenv("BIND_PORT", "19041"))
    server = ThreadingHTTPServer((host, port), Handler); server.service = service  # type: ignore[attr-defined]
    print(f"newapi渠道切换已启动: http://{host}:{port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: service.stop(); server.server_close()


if __name__ == "__main__": main()
