import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

from .diagnostic_sanitizer import json_dumps_sanitized, phase_label, sanitize_for_export, sanitize_text


MAX_RELATED_FILE_BYTES = 512 * 1024
MAX_DIAGNOSTIC_STRING_CHARS = 24 * 1024
DIAGNOSTIC_TEXT_EDGE_CHARS = 10 * 1024
TERMINAL_REVIEW_STATES = {"manual_review", "duplicate", "error_terminal"}


def create_codex_diagnostic(
    database: object,
    job_id: str,
    root: Path,
    status: Optional[Dict[str, Any]] = None,
    force: bool = False,
    diagnostics_root: Optional[Path] = None,
) -> Dict[str, Any]:
    detail = database.job_detail(job_id)
    if not detail or not detail.get("ok"):
        return {"ok": False, "error": "job_no_encontrado"}

    job = detail.get("job") or {}
    bucket = diagnostic_bucket(job)
    target_root = root / bucket
    target_root.mkdir(parents=True, exist_ok=True)

    short_id = str(job.get("job_id") or job_id)[:8]
    existing = _find_existing(root, short_id)
    if existing and not force:
        return {
            "ok": True,
            "created": False,
            "file": existing.name,
            "path": str(existing),
            "relative": str(existing.relative_to(root)),
        }

    stamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{stamp}_{_slug(job.get('name'))}_{short_id}_informe_codex.zip"
    target = target_root / filename
    temp_target = target_root / f".{filename}.tmp"
    temp_target.unlink(missing_ok=True)

    safe_detail = _diagnostic_payload(detail)
    status_payload = status or {}
    with zipfile.ZipFile(temp_target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("LEEME_PRIMERO.txt", _codex_summary(detail, status_payload, filename))
        archive.writestr("resumen.txt", _codex_summary(detail, status_payload, filename))
        archive.writestr("job.json", _json_text(safe_detail.get("job") or {}))
        archive.writestr("timeline.json", _json_text(safe_detail.get("timeline") or []))
        archive.writestr("timings.json", _json_text(safe_detail.get("timings") or []))
        archive.writestr("decisiones.json", _json_text(safe_detail.get("decisions") or []))
        archive.writestr("errores.txt", _json_text(safe_detail.get("errors") or []))
        archive.writestr("logs_filtrados.txt", _logs_filtered_text(detail))
        archive.writestr("health_contenedores.json", _json_text(status_payload))
        archive.writestr("rutas.txt", _paths_text(detail))
        archive.writestr("detalle_completo.json", _json_text(safe_detail))
        _write_live_trace_files(archive, diagnostics_root, str(job.get("job_id") or job_id))

        for index, report in enumerate(detail.get("reports") or [], start=1):
            file_path = _container_file(str(report))
            if not file_path:
                continue
            archive.writestr(
                f"archivos_relacionados/{_report_file_name(str(report), index)}",
                _related_file_content(file_path),
            )

    if not zipfile.is_zipfile(temp_target):
        temp_target.unlink(missing_ok=True)
        raise RuntimeError("El informe generado no es un ZIP valido")
    with zipfile.ZipFile(temp_target) as archive:
        bad_member = archive.testzip()
        if bad_member:
            temp_target.unlink(missing_ok=True)
            raise RuntimeError(f"El informe ZIP falla en {bad_member}")
    temp_target.replace(target)
    return {
        "ok": True,
        "created": True,
        "file": filename,
        "path": str(target),
        "relative": str(target.relative_to(root)),
    }


def diagnostic_bucket(job: Dict[str, Any]) -> str:
    state = str(job.get("state") or "")
    category = str(job.get("category") or "")
    if state in TERMINAL_REVIEW_STATES:
        return "repetidas_vs_error"
    if category == "movies":
        return "movies"
    if category == "tv":
        return "tv"
    if category == "trailers_automatizacion":
        return "trailers"
    return "repetidas_vs_error"


def _find_existing(root: Path, short_id: str) -> Optional[Path]:
    if not root.is_dir() or not short_id:
        return None
    matches = sorted(
        root.rglob(f"*_{short_id}_informe_codex.zip"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    return matches[0] if matches else None


def _write_live_trace_files(
    archive: zipfile.ZipFile,
    diagnostics_root: Optional[Path],
    job_id: str,
) -> None:
    trace_dir = _find_live_trace(diagnostics_root, job_id)
    if not trace_dir:
        return
    for path in sorted(trace_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        with path.open("rb") as handle:
            archive.writestr(
                f"traza_viva/{path.relative_to(trace_dir).as_posix()}",
                _live_trace_content(path),
            )


def _find_live_trace(diagnostics_root: Optional[Path], job_id: str) -> Optional[Path]:
    if not diagnostics_root or not job_id:
        return None
    base = Path(diagnostics_root) / "jobs"
    if not base.is_dir():
        return None
    matches = sorted(path for path in base.glob("*/*") if path.name == job_id and path.is_dir())
    return matches[-1] if matches else None


def _slug(value: Any, fallback: str = "job") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text, flags=re.IGNORECASE).strip("_")
    return (text or fallback)[:90]


def _json_text(payload: Any) -> str:
    return json_dumps_sanitized(payload)


def _trim_diagnostic_text(value: str) -> str:
    if len(value) <= MAX_DIAGNOSTIC_STRING_CHARS:
        return value
    omitted = len(value) - (DIAGNOSTIC_TEXT_EDGE_CHARS * 2)
    return (
        value[:DIAGNOSTIC_TEXT_EDGE_CHARS]
        + f"\n\n[RECORTADO PARA INFORME CODEX: {omitted} caracteres omitidos]\n\n"
        + value[-DIAGNOSTIC_TEXT_EDGE_CHARS:]
    )


def _diagnostic_payload(value: Any) -> Any:
    return sanitize_for_export(value)


def _format_duration(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if value < 1:
        return f"{value:.3f}s"
    if value < 60:
        return f"{value:.1f}s"
    minutes, rest = divmod(int(round(value)), 60)
    return f"{minutes}m {rest:02d}s"


def _format_time(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def _container_file(path_text: str) -> Optional[Path]:
    if not path_text:
        return None
    path = Path(path_text)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve()
        allowed = [Path("/config").resolve(), Path("/data").resolve()]
        if not any(resolved == root or root in resolved.parents for root in allowed):
            return None
        return resolved if resolved.is_file() else None
    except OSError:
        return None


def _report_file_name(path_text: str, index: int) -> str:
    path = Path(path_text)
    stem = _slug(path.stem, f"archivo_{index:02d}")
    suffix = path.suffix if path.suffix else ".txt"
    return f"{index:02d}_{stem}{suffix}"


def _codex_summary(detail: Dict[str, Any], status: Dict[str, Any], filename: str) -> str:
    job = detail.get("job") or {}
    source_meta = detail.get("source_meta") if isinstance(detail.get("source_meta"), dict) else {}
    correlation = _correlation_summary(job, source_meta)
    timings = detail.get("timings") or []
    errors = detail.get("errors") or []
    decisions = detail.get("decisions") or []
    lines = [
        "INFORME CODEX - LEER PRIMERO",
        "",
        f"Archivo: {filename}",
        f"Job ID: {job.get('job_id', '')}",
        f"Nombre: {job.get('name', '')}",
        f"Categoria: {job.get('category', '')}",
        f"Estado: {job.get('state', '')}",
        f"Origen: {job.get('origin', '')}",
        f"Creado: {_format_time(job.get('created_at'))}",
        f"Actualizado: {_format_time(job.get('updated_at'))}",
        "",
        "CORRELACION",
        f"trace_id: {sanitize_text(correlation.get('trace_id', '')) or '-'}",
        f"correlation_id: {sanitize_text(correlation.get('correlation_id', '')) or '-'}",
        f"job_id: {sanitize_text(correlation.get('job_id', '')) or sanitize_text(job.get('job_id', ''))}",
        "",
        "RUTAS",
        f"source_path: {sanitize_text(job.get('source_path', ''))}",
        f"stage_path: {sanitize_text(job.get('stage_path', ''))}",
        f"output_root: {sanitize_text(job.get('output_root', ''))}",
        "",
        "RESUMEN RAPIDO",
        f"Eventos: {len(detail.get('timeline') or [])}",
        f"Decisiones/avisos: {len(decisions)}",
        f"Errores: {len(errors)}",
        f"Orquestador: {(status.get('orchestrator') or {}).get('status', '-')}",
        f"Media worker: {(status.get('media_worker') or {}).get('status', '-')}",
        "",
        "TIEMPOS POR FASE",
    ]
    if timings:
        for item in timings:
            lines.append(
                f"- {phase_label(item.get('phase'))} [{item.get('phase')}]: {_format_duration(item.get('duration_seconds'))} ({item.get('events')} eventos)"
            )
    else:
        lines.append("- Sin tiempos registrados.")
    lines.extend(
        [
            "",
            "ORDEN DE LECTURA PARA IA/CODEX",
            "1. Leer este resumen.",
            "2. Abrir job.json y timeline.json.",
            "3. Mirar decisiones.json y errores.txt.",
            "4. Mirar traza_viva/ si aparece en el ZIP.",
            "5. Usar logs_filtrados.txt antes de buscar logs sueltos.",
            "6. Si hace falta, contrastar con orchestrator.db/job_events.",
        ]
    )
    return "\n".join(lines) + "\n"


def _correlation_summary(job: Dict[str, Any], source_meta: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"job_id": job.get("job_id")}
    for container in (source_meta, source_meta.get("trace") if isinstance(source_meta.get("trace"), dict) else {}):
        if not isinstance(container, dict):
            continue
        for key in ("trace_id", "correlation_id", "source_trace_id", "search_trace_id", "download_trace_id"):
            value = container.get(key)
            if value and key not in result:
                result[key] = value
    return result


def _timeline_text(detail: Dict[str, Any]) -> str:
    lines = []
    for event in detail.get("timeline") or []:
        lines.append(
            "[{time}] {phase}/{kind}: {message}".format(
                time=_format_time(event.get("ts")),
                phase=f"{phase_label(event.get('phase'))} [{event.get('phase', '')}]",
                kind=event.get("event_type", ""),
                message=sanitize_text(event.get("message", "")),
            )
        )
        structured = event.get("structured")
        if structured:
            lines.append(_json_text(_diagnostic_payload(structured)).rstrip())
    return "\n".join(lines) + ("\n" if lines else "")


def _paths_text(detail: Dict[str, Any]) -> str:
    job = detail.get("job") or {}
    lines = [
        f"source_path={sanitize_text(job.get('source_path', ''))}",
        f"torrent_path={sanitize_text(job.get('torrent_path', ''))}",
        f"stage_path={sanitize_text(job.get('stage_path', ''))}",
        f"output_root={sanitize_text(job.get('output_root', ''))}",
        "",
        "reports:",
    ]
    for report in detail.get("reports") or []:
        lines.append(f"- {sanitize_text(report)}")
    return "\n".join(lines) + "\n"


def _live_trace_content(path: Path) -> str:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".json":
        try:
            return _json_text(json.loads(text))
        except json.JSONDecodeError:
            return sanitize_text(text) + "\n"
    if suffix == ".jsonl":
        lines = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                lines.append(json.dumps(sanitize_for_export(json.loads(line)), ensure_ascii=False, default=str))
            except json.JSONDecodeError:
                lines.append(sanitize_text(line))
        return "\n".join(lines) + ("\n" if lines else "")
    return sanitize_text(text) + "\n"


def _related_file_content(path: Path) -> str | bytes:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".log", ".json", ".jsonl", ".md"}:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_RELATED_FILE_BYTES:
            text = text[:MAX_RELATED_FILE_BYTES]
        return sanitize_text(text) + "\n"
    with path.open("rb") as handle:
        return handle.read(MAX_RELATED_FILE_BYTES)


def _logs_filtered_text(detail: Dict[str, Any]) -> str:
    lines = ["EVENTOS DEL JOB", ""]
    lines.append(_timeline_text(detail).rstrip() or "Sin eventos.")
    errors = detail.get("errors") or []
    lines.extend(["", "ERRORES"])
    if errors:
        for error in errors:
            lines.append(_json_text(_diagnostic_payload(error)).rstrip())
    else:
        lines.append("Sin errores registrados.")
    return "\n".join(lines) + "\n"
