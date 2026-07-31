import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote


MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
IDENTITY_PROFILES = ("common", "movies", "tv")
WATCHER_PROFILES = ("movies", "tv")


def start_health_server(
    port: int,
    status_provider: Callable[[], Dict[str, object]],
    jobs_provider: Callable[[], List[Dict[str, object]]],
    job_provider: Optional[Callable[[str], Optional[Dict[str, object]]]] = None,
    event_recorder: Optional[Callable[[str, str, str, str, Optional[Dict[str, object]]], None]] = None,
    follow_provider: Optional[Callable[[str], Dict[str, object]]] = None,
    diagnostic_creator: Optional[Callable[[str, bool], Dict[str, object]]] = None,
    watcher_rules_provider: Optional[Callable[..., Dict[str, object]]] = None,
    watcher_rules_updater: Optional[Callable[..., Dict[str, object]]] = None,
    identity_rules_provider: Optional[Callable[..., Dict[str, object]]] = None,
    identity_rules_updater: Optional[Callable[..., Dict[str, object]]] = None,
    identity_rules_resetter: Optional[Callable[..., Dict[str, object]]] = None,
    identity_cache_clearer: Optional[Callable[..., Dict[str, object]]] = None,
    identity_parser_tester: Optional[Callable[..., Dict[str, object]]] = None,
    identity_resolver_tester: Optional[Callable[..., Dict[str, object]]] = None,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._json(200, status_provider())
            elif path == "/settings/watcher" and watcher_rules_provider:
                self._json(200, watcher_rules_provider())
            elif path.startswith("/settings/watcher/") and watcher_rules_provider:
                profile = path.removeprefix("/settings/watcher/").strip("/")
                if profile in WATCHER_PROFILES:
                    self._json(200, watcher_rules_provider(profile))
                else:
                    self._json(404, {"error": "not_found"})
            elif path == "/settings/identity" and identity_rules_provider:
                self._json(200, identity_rules_provider())
            elif path.startswith("/settings/identity/") and identity_rules_provider:
                profile = path.removeprefix("/settings/identity/").strip("/")
                if profile in IDENTITY_PROFILES:
                    self._json(200, identity_rules_provider(profile))
                else:
                    self._json(404, {"error": "not_found"})
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
            length = self._content_length()
            if length is None:
                self._json(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_request",
                        "message": "Content-Length no es valido.",
                    },
                )
                return
            if length > MAX_REQUEST_BODY_BYTES:
                self._json(
                    413,
                    {
                        "ok": False,
                        "error": "payload_too_large",
                        "message": "El JSON supera el limite de 4 MB.",
                    },
                )
                return
            path = self.path.split("?", 1)[0]
            if path == "/settings/watcher" and watcher_rules_updater:
                result = watcher_rules_updater(self._read_json())
                status = (
                    200
                    if result.get("ok")
                    else 409
                    if result.get("error") == "watcher_rules_conflict"
                    else 400
                )
                self._json(status, result)
                return
            if path.startswith("/settings/watcher/") and watcher_rules_updater:
                profile = path.removeprefix("/settings/watcher/").strip("/")
                if profile not in WATCHER_PROFILES:
                    self._json(404, {"error": "not_found"})
                    return
                result = watcher_rules_updater(
                    self._read_json(),
                    profile,
                    require_expected_fingerprint=True,
                )
                if result.get("ok"):
                    status = 200
                elif result.get("error") == "watcher_rules_conflict":
                    status = 409
                else:
                    status = 400
                self._json(status, result)
                return
            identity_handlers = {
                "/settings/identity": identity_rules_updater,
                "/settings/identity/reset": identity_rules_resetter,
                "/settings/identity/cache/clear": identity_cache_clearer,
                "/settings/identity/test-parser": identity_parser_tester,
                "/settings/identity/test-resolver": identity_resolver_tester,
            }
            identity_handler = identity_handlers.get(path)
            identity_profile = None
            if not identity_handler and path.startswith("/settings/identity/"):
                relative = path.removeprefix("/settings/identity/").strip("/")
                parts = relative.split("/") if relative else []
                if parts and parts[0] in IDENTITY_PROFILES:
                    identity_profile = parts[0]
                    action = "/".join(parts[1:])
                    identity_handler = {
                        "": identity_rules_updater,
                        "reset": identity_rules_resetter,
                        "cache/clear": identity_cache_clearer,
                        "test-parser": identity_parser_tester,
                        "test-resolver": identity_resolver_tester,
                    }.get(action)
            if identity_handler:
                request = self._read_json()
                result = (
                    identity_handler(request, identity_profile)
                    if identity_profile
                    else identity_handler(request)
                )
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
            length = self._content_length() or 0
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                return {}
            return payload if isinstance(payload, dict) else {}

        def _content_length(self) -> Optional[int]:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except (TypeError, ValueError):
                return None
            return length if length >= 0 else None

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
