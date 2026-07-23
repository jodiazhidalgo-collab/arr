import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
STATIC_DIR = WEB_DIR / "static"

RULES_PATH = Path(os.environ.get("MEDIA_RULES_PATH", "/config/media-rules/reglas_motor.json"))
DEFAULT_RULES_PATH = Path(os.environ.get("MEDIA_DEFAULT_RULES_PATH", "/defaults/reglas_motor_default.json"))
REPORT_ROOT = Path(os.environ.get("MEDIA_REPORT_ROOT", "/config/media-worker"))
REVIEW_DIR = Path(os.environ.get("MEDIA_REVIEW_DIR", "/data/media/repetidas_vs_error"))
COMPLETE_ROOT = Path(os.environ.get("ARR_COMPLETE_ROOT", "/data/downloads/torrents/complete"))
MOVIES_ROOT = Path(os.environ.get("ARR_MOVIES_ROOT", "/data/media/movies"))
TV_ROOT = Path(os.environ.get("ARR_TV_ROOT", "/data/media/tv"))
ORCH_URL = os.environ.get("ARR_ORCHESTRATOR_URL", "http://arr-orchestrator:8787").rstrip("/")
WORKER_URL = os.environ.get("MEDIA_WORKER_URL", "http://media-worker:8790").rstrip("/")
CODEX_DIAG_ROOT = Path(os.environ.get("CODEX_DIAG_ROOT", "/diagnosticos_codex"))

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".zip": "application/zip",
}

RULES_SCHEMA: Dict[str, Any] = {
    "version": True,
    "entrada": {
        "extensiones_video": True,
    },
    "video": {
        "pistas_exactas": True,
        "idiomas_aceptados": True,
        "idiomas_indeterminados_como_es": True,
        "aceptar_por_audio_es": True,
        "idiomas_corregibles_por_audio_es": True,
        "idioma_final_por_audio_es": True,
        "idioma_final": True,
        "marcar_default": True,
        "marcar_forzado": True,
    },
    "audio": {
        "idiomas_aceptados": True,
        "aceptar_indeterminado_si_video_es": True,
        "idiomas_condicionales_si_video_es": True,
        "idioma_final_condicional": True,
        "canales_convertir_ac3_desde": True,
        "bitrate_ac3": True,
        "titulo_ac3_convertido": True,
        "marcar_default": True,
        "marcar_forzado": True,
        "codec_prioridad": True,
        "titulos_codec": True,
    },
    "subtitulos": {
        "idiomas_aceptados": True,
        "formatos_texto_aceptados": True,
        "formatos_imagen_no_aceptados": True,
        "frases_descartar_hasta": True,
        "delay_audio": {
            "activo": True,
            "texto_titulo": True,
            "frases_maximo": True,
        },
        "sin_subtitulos_modo": True,
        "frases_maximo_unico_forzado": True,
        "unico_es_modo": True,
        "titulo_final": True,
        "sufijo_srt_externo": True,
        "interno_default": True,
        "interno_forzado": True,
    },
    "limpieza": {
        "crear_capitulos": True,
        "capitulo_cada_segundos": True,
        "borrar_metadata_original": True,
        "limpiar_tags_mkv": True,
        "exportar_srt_externo": True,
    },
    "trailers": {
        "extensiones_video": True,
        "score_minimo_con_ano": True,
        "score_minimo_sin_ano": True,
        "nombre_final": True,
        "si_existe": True,
        "palabras_ruido_titulo": True,
    },
}


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        result = deepcopy(base)
        for key, value in override.items():
            result[key] = _merge(result.get(key), value)
        return result
    if override is None:
        return deepcopy(base)
    return deepcopy(override)


def _sanitize_rules(source: Any, schema: Dict[str, Any] = RULES_SCHEMA) -> Dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    result: Dict[str, Any] = {}
    for key, rule in schema.items():
        if key not in source:
            continue
        value = source[key]
        if rule is True:
            result[key] = deepcopy(value)
        elif isinstance(rule, dict) and isinstance(value, dict):
            child = _sanitize_rules(value, rule)
            if child:
                result[key] = child
    return result


def _safe_child(root: Path, value: str) -> Optional[Path]:
    try:
        root = root.resolve()
        target = (root / value).resolve()
        target.relative_to(root)
        return target
    except (OSError, ValueError):
        return None


def _upstream_json(url: str, timeout: int = 8) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as error:
        return {"ok": False, "error": str(error)}


def _upstream_post_json(url: str, payload: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as error:
        return {"ok": False, "error": str(error)}


def _count_children(path: Path) -> int:
    try:
        return sum(1 for _ in path.iterdir()) if path.is_dir() else 0
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _short_text(path: Path, limit: int = 5000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[:limit]
    except OSError:
        return ""


def _codex_bucket_label(bucket: str) -> str:
    return {
        "movies": "Peliculas",
        "tv": "Series",
        "trailers": "Trailers",
        "repetidas_vs_error": "Repetidas / Error",
    }.get(bucket or "", "Sin clasificar")


def _codex_zip_metadata(path: Path) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open("job.json") as handle:
                metadata = json.loads(handle.read().decode("utf-8"))
    except Exception:
        metadata = {}
    return metadata


def _codex_diagnostics_payload(limit: int = 80) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    if CODEX_DIAG_ROOT.is_dir():
        candidates = [
            path
            for path in CODEX_DIAG_ROOT.rglob("*.zip")
            if path.is_file() and not path.name.startswith(".")
        ]
        for path in sorted(candidates, key=_mtime, reverse=True)[:limit]:
            rel = str(path.relative_to(CODEX_DIAG_ROOT)).replace("\\", "/")
            folder = path.parent.name if path.parent != CODEX_DIAG_ROOT else ""
            job = _codex_zip_metadata(path)
            display_name = str(job.get("name") or path.stem.replace("_informe_codex", ""))
            category = str(job.get("category") or "")
            state = str(job.get("state") or "")
            files.append(
                {
                    "name": path.name,
                    "relative": rel,
                    "folder": folder,
                    "folder_label": _codex_bucket_label(folder) if folder else "Antiguos",
                    "display_name": display_name,
                    "category": category,
                    "state": state,
                    "updated_at": job.get("updated_at"),
                    "size": path.stat().st_size,
                    "mtime": _mtime(path),
                    "download_url": f"/api/codex-diagnostic?file={urllib.parse.quote(rel)}",
                }
            )
    return {"ok": True, "root": str(CODEX_DIAG_ROOT), "files": files}


def _create_codex_diagnostic(job_id: str) -> Dict[str, Any]:
    if not job_id:
        return {"ok": False, "error": "job_id_vacio"}
    result = _upstream_post_json(
        f"{ORCH_URL}/jobs/{urllib.parse.quote(job_id)}/diagnostic",
        {"force": False},
        timeout=60,
    )
    if result.get("ok") and result.get("relative") and not result.get("download_url"):
        result["download_url"] = (
            f"/api/codex-diagnostic?file={urllib.parse.quote(str(result.get('relative')))}"
        )
    return result


def _rules_payload() -> Dict[str, Any]:
    defaults = _read_json(DEFAULT_RULES_PATH)
    active = _read_json(RULES_PATH)
    rules = _sanitize_rules(_merge(defaults, active))
    return {
        "ok": True,
        "rules": rules,
        "active": active,
        "defaults": defaults,
        "rules_path": str(RULES_PATH),
        "defaults_path": str(DEFAULT_RULES_PATH),
    }


def _save_rules(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = payload.get("rules") if isinstance(payload, dict) else None
    if not isinstance(rules, dict):
        return {"ok": False, "error": "Payload de reglas no valido."}
    rules = _sanitize_rules(rules)

    RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    backup_dir = RULES_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if RULES_PATH.exists():
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        shutil.copy2(RULES_PATH, backup_dir / f"reglas_motor_{stamp}.json")

    tmp = RULES_PATH.with_suffix(RULES_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(RULES_PATH)
    result = _rules_payload()
    result["saved"] = True
    return result


def _watcher_rules_payload() -> Dict[str, Any]:
    return _upstream_json(f"{ORCH_URL}/settings/watcher")


def _save_watcher_rules(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _upstream_post_json(f"{ORCH_URL}/settings/watcher", payload)


def _status_payload() -> Dict[str, Any]:
    orch = _upstream_json(f"{ORCH_URL}/health")
    worker = _upstream_json(f"{WORKER_URL}/health")
    return {
        "ok": True,
        "orchestrator": orch,
        "media_worker": worker,
        "paths": {
            "rules": {"path": str(RULES_PATH), "exists": RULES_PATH.exists()},
            "defaults": {"path": str(DEFAULT_RULES_PATH), "exists": DEFAULT_RULES_PATH.exists()},
            "reports": {"path": str(REPORT_ROOT), "exists": REPORT_ROOT.exists()},
            "review": {"path": str(REVIEW_DIR), "exists": REVIEW_DIR.exists(), "items": _count_children(REVIEW_DIR)},
            "movies_final": {"path": str(MOVIES_ROOT), "exists": MOVIES_ROOT.exists(), "items": _count_children(MOVIES_ROOT)},
            "tv_final": {"path": str(TV_ROOT), "exists": TV_ROOT.exists(), "items": _count_children(TV_ROOT)},
            "movies_automatizacion": {
                "path": str(COMPLETE_ROOT / "movies_automatizacion"),
                "exists": (COMPLETE_ROOT / "movies_automatizacion").exists(),
                "items": _count_children(COMPLETE_ROOT / "movies_automatizacion"),
            },
            "trailers_automatizacion": {
                "path": str(COMPLETE_ROOT / "trailers_automatizacion"),
                "exists": (COMPLETE_ROOT / "trailers_automatizacion").exists(),
                "items": _count_children(COMPLETE_ROOT / "trailers_automatizacion"),
            },
        },
    }


def _jobs_payload() -> Dict[str, Any]:
    jobs = _upstream_json(f"{ORCH_URL}/jobs", timeout=12)
    if isinstance(jobs, list):
        return {"ok": True, "jobs": jobs}
    return {"ok": False, "jobs": [], "error": jobs.get("error", "No se pudo leer jobs.")}


def _job_detail_payload(job_id: str) -> Dict[str, Any]:
    if not job_id:
        return {"ok": False, "error": "job_id_vacio"}
    detail = _upstream_json(f"{ORCH_URL}/jobs/{urllib.parse.quote(job_id)}", timeout=12)
    if isinstance(detail, dict):
        return detail
    return {"ok": False, "error": "No se pudo leer detalle del job."}


def _job_follow_payload(job_id: str) -> Dict[str, Any]:
    if not job_id:
        return {"ok": False, "error": "job_id_vacio"}
    detail = _upstream_json(
        f"{ORCH_URL}/jobs/{urllib.parse.quote(job_id)}/follow",
        timeout=12,
    )
    if isinstance(detail, dict):
        return detail
    return {"ok": False, "error": "No se pudo leer seguimiento del job."}


def _review_payload(limit: int = 80) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    if REVIEW_DIR.is_dir():
        for folder in sorted(REVIEW_DIR.iterdir(), key=_mtime, reverse=True)[:limit]:
            if not folder.is_dir():
                continue
            txts = sorted(folder.glob("*.txt"))
            reason_json = folder / "reason.json"
            payload = _read_json(reason_json)
            items.append(
                {
                    "name": folder.name,
                    "path": str(folder),
                    "mtime": _mtime(folder),
                    "reason_file": txts[0].name if txts else "",
                    "reason_text": _short_text(txts[0], 2000) if txts else "",
                    "phase": payload.get("phase", ""),
                    "job_id": payload.get("job_id", ""),
                    "file_count": _count_children(folder),
                }
            )
    return {"ok": True, "review_dir": str(REVIEW_DIR), "items": items}


def _reports_payload(limit: int = 120) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    roots = [REPORT_ROOT / "runtime", REPORT_ROOT / "logs", REPORT_ROOT]
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), key=_mtime, reverse=True):
            if len(files) >= limit:
                break
            if not path.is_file():
                continue
            if any(part in {"temp", "backups"} for part in path.relative_to(REPORT_ROOT).parts):
                continue
            rel = str(path.relative_to(REPORT_ROOT))
            if rel in seen:
                continue
            seen.add(rel)
            files.append(
                {
                    "name": path.name,
                    "relative": rel,
                    "size": path.stat().st_size,
                    "mtime": _mtime(path),
                    "kind": path.suffix.lower().lstrip(".") or "file",
                }
            )
    return {"ok": True, "report_root": str(REPORT_ROOT), "files": files}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _download(self, path: Path) -> None:
        content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _read_payload(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        data = self.rfile.read(min(length, 4 * 1024 * 1024)) if length else b"{}"
        try:
            payload = json.loads(data.decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _static(self, path: str) -> None:
        rel = path.removeprefix("/static/").strip("/")
        target = _safe_child(STATIC_DIR, rel)
        if not target or not target.is_file():
            self._send(404, b"No encontrado.", "text/plain; charset=utf-8")
            return
        content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), content_type)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            self._json(200, {"status": "ok"})
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/api/status":
            self._json(200, _status_payload())
            return
        if path == "/api/jobs":
            self._json(200, _jobs_payload())
            return
        if path.startswith("/api/jobs/"):
            suffix = urllib.parse.unquote(path.removeprefix("/api/jobs/")).strip("/")
            if suffix.endswith("/follow"):
                job_id = suffix.removesuffix("/follow").strip("/")
                detail = _job_follow_payload(job_id)
            else:
                detail = _job_detail_payload(suffix)
            self._json(200 if detail.get("ok") else 404, detail)
            return
        if path == "/api/rules":
            self._json(200, _rules_payload())
            return
        if path == "/api/watcher-rules":
            result = _watcher_rules_payload()
            self._json(200 if result.get("ok") else 502, result)
            return
        if path == "/api/review":
            self._json(200, _review_payload())
            return
        if path == "/api/reports":
            self._json(200, _reports_payload())
            return
        if path == "/api/codex-diagnostics":
            self._json(200, _codex_diagnostics_payload())
            return
        if path == "/api/codex-diagnostic":
            rel = query.get("file", [""])[0]
            target = _safe_child(CODEX_DIAG_ROOT, rel)
            if not target or not target.is_file() or target.suffix.lower() != ".zip":
                self._send(404, b"No hay informe Codex.", "text/plain; charset=utf-8")
                return
            self._download(target)
            return
        if path == "/api/report":
            rel = query.get("file", [""])[0]
            target = _safe_child(REPORT_ROOT, rel)
            if not target or not target.is_file():
                self._send(404, b"No hay informe.", "text/plain; charset=utf-8")
                return
            self._send(200, _short_text(target, 512000).encode("utf-8"), "text/plain; charset=utf-8")
            return
        if path.startswith("/static/"):
            self._static(path)
            return
        if path == "/" or path == "/index.html":
            self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        self._send(404, b"No encontrado.", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/rules":
            try:
                self._json(200, _save_rules(self._read_payload()))
            except Exception as error:
                self._json(500, {"ok": False, "error": str(error)})
            return
        if parsed.path == "/api/watcher-rules":
            try:
                result = _save_watcher_rules(self._read_payload())
                self._json(200 if result.get("ok") else 400, result)
            except Exception as error:
                self._json(500, {"ok": False, "error": str(error)})
            return
        if parsed.path == "/api/codex-diagnostic":
            try:
                payload = self._read_payload()
                result = _create_codex_diagnostic(str(payload.get("job_id") or "").strip())
                self._json(200 if result.get("ok") else 404, result)
            except Exception as error:
                self._json(500, {"ok": False, "error": str(error)})
            return
        self._json(404, {"ok": False, "error": "Ruta no reconocida."})


def main() -> int:
    port = int(os.environ.get("MEDIA_PANEL_PORT", "8080") or "8080")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"media-panel iniciado en puerto {port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
