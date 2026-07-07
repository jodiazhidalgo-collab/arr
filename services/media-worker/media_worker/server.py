import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import process_movie, process_trailer


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
