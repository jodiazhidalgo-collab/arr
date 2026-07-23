import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote


def start_health_server(
    port: int,
    status_provider: Callable[[], Dict[str, object]],
    jobs_provider: Callable[[], List[Dict[str, object]]],
    job_provider: Optional[Callable[[str], Optional[Dict[str, object]]]] = None,
    event_recorder: Optional[Callable[[str, str, str, str, Optional[Dict[str, object]]], None]] = None,
    follow_provider: Optional[Callable[[str], Dict[str, object]]] = None,
    diagnostic_creator: Optional[Callable[[str, bool], Dict[str, object]]] = None,
    watcher_rules_provider: Optional[Callable[[], Dict[str, object]]] = None,
    watcher_rules_updater: Optional[Callable[[Dict[str, object]], Dict[str, object]]] = None,
    filebot_rules_provider: Optional[Callable[[], Dict[str, object]]] = None,
    filebot_rules_updater: Optional[Callable[[Dict[str, object]], Dict[str, object]]] = None,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._json(200, status_provider())
            elif path == "/settings/watcher" and watcher_rules_provider:
                self._json(200, watcher_rules_provider())
            elif path == "/settings/filebot" and filebot_rules_provider:
                self._json(200, filebot_rules_provider())
            elif path == "/jobs":
                self._json(200, jobs_provider())
            elif path.startswith("/jobs/") and path.endswith("/follow") and follow_provider:
                job_id = unquote(path.removeprefix("/jobs/").removesuffix("/follow")).strip("/")
                payload = follow_provider(job_id)
                self._json(200 if payload.get("ok") else 404, payload)
            elif path.startswith("/jobs/") and job_provider:
                job_id = unquote(path.removeprefix("/jobs/")).strip()
                detail = job_provider(job_id)
                if detail:
                    self._json(200, detail)
                else:
                    self._json(404, {"ok": False, "error": "job_not_found"})
            else:
                self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/settings/watcher" and watcher_rules_updater:
                result = watcher_rules_updater(self._read_json())
                self._json(200 if result.get("ok") else 400, result)
                return
            if path == "/settings/filebot" and filebot_rules_updater:
                result = filebot_rules_updater(self._read_json())
                if result.get("ok"):
                    status = 200
                elif result.get("error") == "revision_conflict":
                    status = 409
                elif result.get("error") == "persistence_failed":
                    status = 500
                else:
                    status = 400
                self._json(status, result)
                return
            if path.startswith("/jobs/") and path.endswith("/events") and event_recorder:
                job_id = unquote(
                    path.removeprefix("/jobs/").removesuffix("/events")
                ).strip("/")
                payload = self._read_json()
                phase = str(payload.get("phase") or "media").strip() or "media"
                event_type = str(payload.get("event_type") or "decision").strip() or "decision"
                message = str(payload.get("message") or "").strip() or "Evento media-worker"
                structured = payload.get("structured")
                if not isinstance(structured, dict):
                    structured = None
                event_recorder(job_id, phase, event_type, message, structured)
                self._json(200, {"ok": True})
                return
            if path.startswith("/jobs/") and path.endswith("/diagnostic") and diagnostic_creator:
                job_id = unquote(
                    path.removeprefix("/jobs/").removesuffix("/diagnostic")
                ).strip("/")
                payload = self._read_json()
                result = diagnostic_creator(job_id, bool(payload.get("force")))
                self._json(200 if result.get("ok") else 404, result)
                return
            self._json(404, {"error": "not_found"})

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _read_json(self) -> Dict[str, object]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(min(length, 256 * 1024)) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                return {}
            return payload if isinstance(payload, dict) else {}

        def _json(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    return server
