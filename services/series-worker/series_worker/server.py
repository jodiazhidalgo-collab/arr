"""API HTTP independiente de Series Worker."""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .core import (
    RequestValidationError,
    SeriesCoordinator,
    SeriesWorkerError,
    ServiceUnavailable,
    Submission,
)
from .rules import RulesConflictError, RulesValidationError


MAX_REQUEST_BYTES = 256 * 1024
JOB_STATUS_RE = re.compile(r"/jobs/([^/]+)/status")


def _error_payload(error: SeriesWorkerError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error.code,
        "message": str(error),
        "retryable": bool(error.retryable),
    }


class SeriesHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], coordinator: SeriesCoordinator):
        self.coordinator = coordinator
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: SeriesHTTPServer

    def log_message(self, fmt: str, *args: object) -> None:
        if urlsplit(self.path).path == "/health":
            return
        print(fmt % args, flush=True)

    def _json(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _read_payload(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as error:
            raise RequestValidationError("Content-Length no es válido") from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise RequestValidationError("El payload supera el límite permitido")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestValidationError("El JSON no es válido") from error
        if not isinstance(payload, dict):
            raise RequestValidationError("El payload debe ser un objeto JSON")
        return payload

    def _run(self, callback) -> None:
        try:
            submission = callback()
        except SeriesWorkerError as error:
            self._json(error.http_status, _error_payload(error))
            return
        except Exception as error:
            unavailable = ServiceUnavailable("Fallo interno del servicio")
            self._json(
                unavailable.http_status,
                {**_error_payload(unavailable), "detail": type(error).__name__},
            )
            return
        if not isinstance(submission, Submission):
            submission = Submission(200, submission)
        self._json(submission.http_status, submission.payload)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._run(self.server.coordinator.health)
            return
        if parsed.path == "/settings/rules":
            self._run(
                lambda: Submission(200, self.server.coordinator.rules_payload())
            )
            return
        match = JOB_STATUS_RE.fullmatch(parsed.path)
        if match is None:
            self._json(404, {"ok": False, "error": "not_found"})
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        kinds = query.get("kind", [])
        if kinds != ["series"]:
            self._json(
                400,
                {
                    "ok": False,
                    "error": "invalid_request",
                    "message": "kind debe ser series",
                    "retryable": False,
                },
            )
            return
        job_id = unquote(match.group(1))
        self._run(lambda: self.server.coordinator.status(job_id))

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path not in {"/process-series", "/settings/rules"}:
            self._json(404, {"ok": False, "error": "not_found"})
            return
        try:
            payload = self._read_payload()
        except RequestValidationError as error:
            self._json(error.http_status, _error_payload(error))
            return
        if parsed.path == "/process-series":
            self._run(lambda: self.server.coordinator.submit(payload))
            return
        try:
            result = self.server.coordinator.save_rules(payload)
        except RulesConflictError as error:
            current = dict(error.current)
            current.update(
                {
                    "ok": False,
                    "error": "fingerprint_conflict",
                    "message": str(error),
                }
            )
            self._json(409, current)
        except RulesValidationError as error:
            self._json(
                400,
                {"ok": False, "error": "invalid_rules", "message": str(error)},
            )
        except ServiceUnavailable as error:
            self._json(error.http_status, _error_payload(error))
        except OSError:
            error = ServiceUnavailable("No se pudieron persistir las reglas")
            self._json(error.http_status, _error_payload(error))
        else:
            self._json(200, result)


def create_server(
    coordinator: SeriesCoordinator | None = None,
    *,
    host: str = "0.0.0.0",
    port: int | None = None,
) -> SeriesHTTPServer:
    selected_port = int(
        port if port is not None else os.environ.get("SERIES_WORKER_PORT", "8791")
    )
    return SeriesHTTPServer((host, selected_port), coordinator or SeriesCoordinator())


def main() -> int:
    server = create_server()
    print(f"series-worker iniciado en puerto {server.server_port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["Handler", "SeriesHTTPServer", "create_server", "main"]
