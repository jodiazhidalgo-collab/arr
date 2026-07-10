import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import normalize_bluray, process_movie, process_trailer


def _allowed_roots() -> list[Path]:
    configured = os.environ.get("MEDIA_WORKER_ALLOWED_ROOTS", "/data")
    return [
        Path(item).resolve()
        for item in configured.split(os.pathsep)
        if item.strip()
    ]


def _validated_normalize_payload(payload: dict) -> dict:
    source_text = str(payload.get("source_path") or "").strip()
    if not source_text:
        raise ValueError("source_path es obligatorio")
    source = Path(source_text).resolve(strict=True)
    if not source.is_dir():
        raise ValueError("source_path debe ser una carpeta")
    if not any(source == root or source.is_relative_to(root) for root in _allowed_roots()):
        raise ValueError("source_path queda fuera de los volumenes permitidos")
    return {**payload, "source_path": str(source)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        if self.path == "/health":
            return
        print(fmt % args, flush=True)

    def _json(self, status: int, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_payload(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        data = self.rfile.read(length) if length else b"{}"
        return json.loads(data.decode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        try:
            payload = self._read_payload()
            if self.path == "/process-movie":
                self._json(200, process_movie(payload))
                return
            if self.path == "/process-trailer":
                self._json(200, process_trailer(payload))
                return
            if self.path == "/normalize-bluray":
                self._json(200, normalize_bluray(_validated_normalize_payload(payload)))
                return
            self._json(404, {"error": "not_found"})
        except Exception as error:
            self._json(500, {"status": "error", "error": str(error)})


def main() -> int:
    port = int(os.environ.get("MEDIA_WORKER_PORT", "8790") or "8790")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"media-worker iniciado en puerto {port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
