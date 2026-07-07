import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .diagnostic_sanitizer import phase_label, sanitize_for_export, sanitize_text

FOLLOW_TRANSLATOR_VERSION = "arr-follow-v2"
FOLLOW_READ_ORDER = [
    "follow payload",
    "diagnostics/arr/jobs/<date>/<job_id>/summary.json",
    "diagnostics/arr/jobs/<date>/<job_id>/timeline.md",
    "diagnostics/arr/jobs/<date>/<job_id>/warnings.jsonl",
    "diagnostics/arr/jobs/<date>/<job_id>/errors.jsonl",
    "diagnostics/arr/jobs/<date>/<job_id>/events.jsonl",
    "diagnostics/arr/jobs/<date>/<job_id>/human_follow.json",
    "diagnostics/arr/jobs/<date>/<job_id>/related_files.json",
    "diagnostics/arr/jobs/<date>/<job_id>/meta.json",
    "diagnosticos_codex/<bucket>/<zip>",
    "orchestrator.db job_events",
]
FINAL_STATES = {"done", "manual_review", "error_terminal", "duplicate", "discarded"}


def build_follow_payload(
    detail: Optional[Dict[str, Any]],
    diagnostics_root: Optional[Path] = None,
) -> Dict[str, Any]:
    if not detail or not detail.get("ok"):
        return {"ok": False, "error": "job_no_encontrado"}

    job = detail.get("job") or {}
    timeline = list(detail.get("timeline") or [])
    decisions = list(detail.get("decisions") or [])
    errors = list(detail.get("errors") or [])
    timings = list(detail.get("timings") or [])
    current = timeline[-1] if timeline else {}
    blackbox = _blackbox_payload(diagnostics_root, str(job.get("job_id") or ""))
    correlation = _correlation_payload(detail, diagnostics_root, blackbox)
    diagnostic_status = _diagnostic_status(errors, decisions)
    operation_status = _operation_status(job)

    return {
        "schema": "arr-follow-payload-v2",
        "ok": True,
        "translator_version": FOLLOW_TRANSLATOR_VERSION,
        "read_order": FOLLOW_READ_ORDER,
        "job_id": job.get("job_id"),
        "name": sanitize_text(job.get("name")),
        "category": job.get("category"),
        "state": job.get("state"),
        "origin": job.get("origin"),
        "diagnostic_status": diagnostic_status,
        "operation_status": operation_status,
        "current": {
            "phase": current.get("phase"),
            "phase_label": phase_label(current.get("phase")),
            "event_type": current.get("event_type"),
            "message": sanitize_text(current.get("message")),
            "ts": current.get("ts"),
        },
        "cursor": _cursor(timeline, blackbox),
        "resume": _human_resume(job, current, errors),
        "advice": _advice(job, current, errors),
        "phases": _phase_resume(timings),
        "lines": _timeline_lines(timeline),
        "decisions": sanitize_for_export(decisions[-12:]),
        "errors": sanitize_for_export(errors),
        "timeline": sanitize_for_export(timeline[-80:]),
        "paths": {
            "source_path": sanitize_text(job.get("source_path")),
            "stage_path": sanitize_text(job.get("stage_path")),
            "output_root": sanitize_text(job.get("output_root")),
            "torrent_path": sanitize_text(job.get("torrent_path")),
        },
        "blackbox": blackbox,
        "correlation": correlation,
    }


def _human_resume(
    job: Dict[str, Any],
    current: Dict[str, Any],
    errors: List[Dict[str, Any]],
) -> List[str]:
    lines = [
        f"Estado actual: {job.get('state') or '-'}",
        f"Categoria: {job.get('category') or '-'}",
    ]
    if current:
        lines.append(
            "Ultimo evento: {phase}/{kind} - {message}".format(
                phase=phase_label(current.get("phase")) or "-",
                kind=current.get("event_type") or "-",
                message=sanitize_text(current.get("message")) or "-",
            )
        )
    if errors:
        last = errors[-1]
        lines.append(f"Error clave: {sanitize_text(last.get('message') or last.get('phase') or '-')}")
    else:
        lines.append("Error clave: ninguno registrado")
    return lines


def _phase_resume(timings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for item in timings:
        result.append(
            {
                "phase": item.get("phase"),
                "label": phase_label(item.get("phase")),
                "events": item.get("events"),
                "duration_seconds": item.get("duration_seconds"),
                "started_at": item.get("started_at"),
                "finished_at": item.get("finished_at"),
            }
        )
    return result


def _timeline_lines(timeline: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for event in timeline[-40:]:
        phase = str(event.get("phase") or "unknown")
        lines.append(
            "{label} - {phase}: {message}".format(
                label=phase_label(phase),
                phase=phase,
                message=sanitize_text(event.get("message") or ""),
            )
        )
    return lines


def _phase_label(phase: str) -> str:
    return phase_label(phase)


def _blackbox_payload(root: Optional[Path], job_id: str) -> Dict[str, Any]:
    if not root or not job_id:
        return {"available": False}
    base = Path(root) / "jobs"
    if not base.is_dir():
        return {"available": False, "root": str(root)}
    matches = sorted(path for path in base.glob("*/*") if path.name == job_id)
    if not matches:
        return {"available": False, "root": str(root)}
    job_dir = matches[-1]
    summary = _read_json(job_dir / "summary.json")
    return {
        "available": True,
        "path": sanitize_text(job_dir),
        "summary": sanitize_for_export(summary),
        "files": {
            "meta": sanitize_text(job_dir / "meta.json"),
            "summary": sanitize_text(job_dir / "summary.json"),
            "events": sanitize_text(job_dir / "events.jsonl"),
            "warnings": sanitize_text(job_dir / "warnings.jsonl"),
            "errors": sanitize_text(job_dir / "errors.jsonl"),
            "timeline": sanitize_text(job_dir / "timeline.md"),
            "human_follow": sanitize_text(job_dir / "human_follow.json"),
            "related_files": sanitize_text(job_dir / "related_files.json"),
        },
    }


def _cursor(timeline: List[Dict[str, Any]], blackbox: Dict[str, Any]) -> Dict[str, Any]:
    current = timeline[-1] if timeline else {}
    summary = blackbox.get("summary") if isinstance(blackbox.get("summary"), dict) else {}
    return {
        "event_count": len(timeline),
        "last_ts": current.get("ts"),
        "last_phase": current.get("phase"),
        "last_event_type": current.get("event_type"),
        "blackbox_event_count": summary.get("event_count"),
        "blackbox_path": blackbox.get("path"),
    }


def _diagnostic_status(errors: List[Dict[str, Any]], decisions: List[Dict[str, Any]]) -> str:
    if errors:
        return "error"
    for item in decisions:
        if str(item.get("event_type") or "").lower() in {"warning", "retry", "skipped"}:
            return "warning"
    return "ok"


def _operation_status(job: Dict[str, Any]) -> str:
    state = str(job.get("state") or "")
    return "final" if state in FINAL_STATES else "running"


def _advice(
    job: Dict[str, Any],
    current: Dict[str, Any],
    errors: List[Dict[str, Any]],
) -> List[str]:
    state = str(job.get("state") or "")
    if state == "manual_review":
        return ["Revisar identidad manual: no hay candidato unico automatico."]
    if state == "error_terminal" and errors:
        last = errors[-1]
        return [f"Revisar error clave en {phase_label(last.get('phase'))}: {sanitize_text(last.get('message') or '-')}"]
    if state == "done":
        return ["Trabajo finalizado. Revisar limpieza solo si falta salida en biblioteca."]
    if current:
        return [f"Seguir desde {phase_label(current.get('phase'))}: {sanitize_text(current.get('message') or '-')}"]
    return ["Sin eventos suficientes para recomendar siguiente paso."]


def _correlation_payload(
    detail: Dict[str, Any],
    diagnostics_root: Optional[Path],
    blackbox: Dict[str, Any],
) -> Dict[str, Any]:
    job = detail.get("job") or {}
    source_meta = detail.get("source_meta") if isinstance(detail.get("source_meta"), dict) else {}
    result: Dict[str, Any] = {
        "job_id": sanitize_text(job.get("job_id")),
        "infohash": sanitize_text(job.get("infohash") or job.get("qbt_hash")),
        "qbit_hash": sanitize_text(job.get("qbt_hash") or job.get("infohash")),
        "rdt_id": sanitize_text(job.get("rdt_id")),
        "source": "job_detail+diagnostics",
        "related_traces": [],
    }
    for key in ("trace_id", "correlation_id", "source_trace_id", "search_trace_id", "download_trace_id"):
        value = _find_first(source_meta, key)
        if value:
            result[key] = sanitize_text(value)
    summary = blackbox.get("summary") if isinstance(blackbox.get("summary"), dict) else {}
    blackbox_correlation = summary.get("correlation") if isinstance(summary.get("correlation"), dict) else {}
    for key, value in blackbox_correlation.items():
        if value and key not in result:
            result[key] = sanitize_text(value)
    result["related_traces"] = _find_related_traces(diagnostics_root, job, source_meta)
    result["available"] = bool(result.get("trace_id") or result["related_traces"])
    return sanitize_for_export(result)


def _find_related_traces(
    diagnostics_root: Optional[Path],
    job: Dict[str, Any],
    source_meta: Dict[str, Any],
    limit: int = 5,
) -> List[Dict[str, Any]]:
    if not diagnostics_root:
        return []
    root = Path(diagnostics_root)
    if not root.is_dir():
        return []
    wanted = _correlation_wanted(job, source_meta)
    if not any(wanted.values()):
        return []
    matches: List[Dict[str, Any]] = []
    for summary_path in sorted(root.glob("*/*/*/summary.json")):
        summary = _read_json(summary_path)
        if not summary:
            continue
        reason = _trace_match_reason(wanted, summary)
        if not reason:
            continue
        matches.append(
            {
                "kind": summary.get("kind"),
                "trace_id": summary.get("trace_id"),
                "match": reason,
                "path": sanitize_text(summary_path.parent),
                "state": summary.get("state"),
                "last_phase": summary.get("last_phase"),
                "last_message": summary.get("last_message"),
            }
        )
        if len(matches) >= limit:
            break
    return matches


def _correlation_wanted(job: Dict[str, Any], source_meta: Dict[str, Any]) -> Dict[str, str]:
    wanted = {
        "job_id": str(job.get("job_id") or ""),
        "trace_id": str(_find_first(source_meta, "trace_id", "source_trace_id", "download_trace_id", "search_trace_id") or ""),
        "correlation_id": str(_find_first(source_meta, "correlation_id") or ""),
        "qbit_hash": str(job.get("qbt_hash") or job.get("infohash") or ""),
        "rdt_id": str(job.get("rdt_id") or ""),
    }
    return {key: value.strip().lower() for key, value in wanted.items() if value and value.strip()}


def _trace_match_reason(wanted: Dict[str, str], summary: Dict[str, Any]) -> str:
    correlation = summary.get("correlation") if isinstance(summary.get("correlation"), dict) else {}
    values = {
        "job_id": str(correlation.get("job_id") or "").lower(),
        "trace_id": str(summary.get("trace_id") or correlation.get("trace_id") or "").lower(),
        "correlation_id": str(correlation.get("correlation_id") or "").lower(),
        "qbit_hash": str(correlation.get("qbit_hash") or "").lower(),
        "rdt_id": str(correlation.get("rdt_id") or "").lower(),
    }
    for key, wanted_value in wanted.items():
        if wanted_value and values.get(key) == wanted_value:
            return key
    return ""


def _find_first(value: Any, *keys: str) -> Any:
    wanted = {key.lower() for key in keys}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in wanted and item not in ("", None):
                return item
        for item in value.values():
            found = _find_first(item, *keys)
            if found not in ("", None):
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first(item, *keys)
            if found not in ("", None):
                return found
    return None


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
