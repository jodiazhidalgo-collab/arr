import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote

from .source_context.contract import MAX_REQUEST_BODY_BYTES as SOURCE_CONTEXT_MAX_BODY_BYTES


MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024


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
    identity_rules_provider: Optional[Callable[[], Dict[str, object]]] = None,
    identity_rules_updater: Optional[Callable[[Dict[str, object]], Dict[str, object]]] = None,
    identity_rules_resetter: Optional[Callable[[Dict[str, object]], Dict[str, object]]] = None,
    identity_cache_clearer: Optional[Callable[[Dict[str, object]], Dict[str, object]]] = None,
    identity_parser_tester: Optional[Callable[[Dict[str, object]], Dict[str, object]]] = None,
    identity_resolver_tester: Optional[Callable[[Dict[str, object]], Dict[str, object]]] = None,
    source_context_event_handler: Optional[
        Callable[[Dict[str, object]], tuple[int, Dict[str, object]]]
    ] = None,
    source_context_token: str = "",
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._json(200, status_provider())
            elif path == "/settings/watcher" and watcher_rules_provider:
                self._json(200, watcher_rules_provider())
            elif path == "/settings/identity" and identity_rules_provider:
                self._json(200, identity_rules_provider())
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
            if path == "/internal/source-context/events" and source_context_event_handler:
                if not source_context_token:
                    self._json(
                        503,
                        {
                            "ok": False,
                            "error": "source_context_disabled",
                            "message": "La recepcion de contexto de origen no esta configurada.",
                        },
                    )
                    return
                if not _valid_bearer(self.headers.get("Authorization"), source_context_token):
                    self._json(
                        401,
                        {
                            "ok": False,
                            "error": "unauthorized",
                            "message": "Bearer no valido.",
                        },
                    )
                    return
                if not _is_json_content_type(self.headers.get("Content-Type")):
                    self._json(
                        415,
                        {
                            "ok": False,
                            "error": "unsupported_media_type",
                            "message": "Content-Type debe ser application/json.",
                        },
                    )
                    return
                if length > SOURCE_CONTEXT_MAX_BODY_BYTES:
                    self._json(
                        413,
                        {
                            "ok": False,
                            "error": "payload_too_large",
                            "message": "El JSON supera el limite de 16 KB.",
                        },
                    )
                    return
                payload = self._read_json_strict()
                if payload is None:
                    self._json(
                        400,
                        {
                            "ok": False,
                            "error": "invalid_json",
                            "message": "El cuerpo debe ser un objeto JSON valido.",
                        },
                    )
                    return
                try:
                    status, result = source_context_event_handler(payload)
                except Exception:
                    self._json(
                        500,
                        {
                            "ok": False,
                            "error": "source_context_failed",
                            "message": "No se pudo registrar el contexto de origen.",
                        },
                    )
                    return
                self._json(status, result)
                return
            if path == "/settings/watcher" and watcher_rules_updater:
                result = watcher_rules_updater(self._read_json())
                self._json(200 if result.get("ok") else 400, result)
                return
            identity_handlers = {
                "/settings/identity": identity_rules_updater,
                "/settings/identity/reset": identity_rules_resetter,
                "/settings/identity/cache/clear": identity_cache_clearer,
                "/settings/identity/test-parser": identity_parser_tester,
                "/settings/identity/test-resolver": identity_resolver_tester,
            }
            identity_handler = identity_handlers.get(path)
            if identity_handler:
                result = identity_handler(self._read_json())
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

        def _read_json_strict(self) -> Optional[Dict[str, object]]:
            length = self._content_length() or 0
            if length <= 0:
                return None
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

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


def _valid_bearer(header: object, expected: str) -> bool:
    if not isinstance(header, str) or not expected:
        return False
    scheme, separator, supplied = header.partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return False
    return hmac.compare_digest(supplied.strip(), expected)


def _is_json_content_type(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.split(";", 1)[0].strip().casefold() == "application/json"
