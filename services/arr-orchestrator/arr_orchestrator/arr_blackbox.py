import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .diagnostic_sanitizer import collect_related_paths, phase_label, sanitize_for_export, sanitize_text


WARNING_TYPES = {"warning", "retry", "skipped"}
FINAL_STATES = {"done", "manual_review", "error_terminal", "duplicate", "discarded"}
SUMMARY_READ_ORDER = [
    "summary.json",
    "timeline.md",
    "warnings.jsonl",
    "errors.jsonl",
    "events.jsonl",
    "human_follow.json",
    "related_files.json",
    "meta.json",
]
CONFIG_SNAPSHOT_KEYS = {
    "mode": "ARR_MODE",
    "timing": {
        "stable_seconds": "ARR_STABLE_SECONDS",
        "reconcile_seconds": "ARR_RECONCILE_SECONDS",
        "rdt_fallback_seconds": "ARR_RDT_FALLBACK_SECONDS",
        "resolver_http_timeout_ms": "ARR_RESOLVER_HTTP_TIMEOUT_MS",
        "resolver_total_budget_ms": "ARR_RESOLVER_TOTAL_BUDGET_MS",
        "resolver_retry_seconds": "ARR_RESOLVER_RETRY_SECONDS",
    },
    "paths": {
        "workshop": "ARR_WORKSHOP_ROOT",
        "media_automation_inbox": "ARR_MEDIA_AUTOMATION_INBOX",
        "trailers_inbox": "ARR_TRAILERS_INBOX",
        "review": "ARR_REVIEW_DIR",
        "movies_final": "ARR_MOVIES_FINAL",
        "tv_final": "ARR_TV_FINAL",
        "codex_diagnostics": "CODEX_DIAG_ROOT",
        "diagnostics": "ARR_DIAGNOSTICS_ROOT",
    },
    "services": {
        "media_worker": "MEDIA_WORKER_URL",
        "callback": "ARR_CALLBACK_URL",
        "qbit": "QBT_URL",
        "rdt": "RDT_URL",
    },
    "resolver": {
        "language": "ARR_RESOLVER_LANGUAGE",
        "region": "ARR_RESOLVER_REGION",
    },
}
CONFIG_CREDENTIAL_FLAGS = {
    "qbt_login_secret_present": ("QBT_PASSWORD", "QBT_PASSWORD_FILE"),
    "rdt_login_secret_present": ("RDT_PASSWORD", "RDT_PASSWORD_FILE"),
    "tmdb_credential_present": ("TMDB_API_TOKEN", "TMDB_API_TOKEN_FILE"),
}


class ArrBlackbox:
    """Best-effort mirror of job_events for Codex diagnostics."""

    def __init__(self, root: Path, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled
        self._lock = threading.Lock()

    def record_event(self, event: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        normalized = _normalize_event(event)
        job_id = str(normalized.get("job_id") or "").strip()
        if not job_id:
            return

        with self._lock:
            job_dir = self.root / "jobs" / _day(normalized["ts"]) / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            _write_meta_if_missing(job_dir / "meta.json", normalized)
            _touch_if_missing(job_dir / "warnings.jsonl")
            _touch_if_missing(job_dir / "errors.jsonl")
            _append_jsonl(job_dir / "events.jsonl", normalized)
            _append_timeline(job_dir / "timeline.md", normalized)

            event_type = str(normalized.get("event_type") or "")
            if event_type == "error":
                _append_jsonl(job_dir / "errors.jsonl", normalized)
            elif event_type in WARNING_TYPES:
                _append_jsonl(job_dir / "warnings.jsonl", normalized)

            _write_summary(job_dir / "summary.json", normalized)
            _write_human_follow(job_dir / "human_follow.json", normalized)
            _write_related_files(job_dir / "related_files.json", normalized)


def _normalize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    ts = _float_or_now(event.get("ts"))
    structured = sanitize_for_export(_json_safe(event.get("structured")))
    return {
        "schema": "arr-blackbox-event-v1",
        "event_id": event.get("event_id"),
        "job_id": str(event.get("job_id") or ""),
        "ts": ts,
        "ts_iso": _format_time(ts),
        "phase": str(event.get("phase") or "unknown"),
        "phase_label": phase_label(event.get("phase") or "unknown"),
        "event_type": str(event.get("event_type") or "decision"),
        "message": sanitize_text(event.get("message") or ""),
        "structured": structured if isinstance(structured, dict) else structured,
    }


def _write_meta_if_missing(path: Path, event: Dict[str, Any]) -> None:
    if path.exists():
        return
    payload = {
        "schema": "arr-blackbox-meta-v1",
        "source": "orchestrator.db:job_events",
        "canonical_source": "config/arr-orchestrator/orchestrator.db",
        "kind": "job",
        "trace_kind": "orchestrator_job",
        "trace_id": event.get("job_id"),
        "action": "job_events_mirror",
        "job_id": event.get("job_id"),
        "created_ts": event.get("ts"),
        "created_iso": event.get("ts_iso"),
        "config_snapshot": _config_snapshot(),
        "read_order": SUMMARY_READ_ORDER,
    }
    _write_json(path, payload)


def _write_summary(path: Path, event: Dict[str, Any]) -> None:
    summary = _read_json(path) or {
        "schema": "arr-blackbox-summary-v1",
        "source": "orchestrator.db:job_events",
        "canonical_source": "config/arr-orchestrator/orchestrator.db",
        "read_order": SUMMARY_READ_ORDER,
        "job_id": event.get("job_id"),
        "first_ts": event.get("ts"),
        "first_iso": event.get("ts_iso"),
        "event_count": 0,
        "errors": 0,
        "warnings": 0,
        "counts": {"events": 0, "warnings": 0, "errors": 0},
        "phases": [],
        "diagnostic_status": "ok",
        "operation_status": "running",
        "config_snapshot": _config_snapshot(),
    }
    event_type = str(event.get("event_type") or "")
    structured = event.get("structured") if isinstance(event.get("structured"), dict) else {}

    summary["event_count"] = int(summary.get("event_count") or 0) + 1
    summary["last_ts"] = event.get("ts")
    summary["last_iso"] = event.get("ts_iso")
    summary["last_phase"] = event.get("phase")
    summary["last_phase_label"] = event.get("phase_label")
    summary["last_event_type"] = event_type
    summary["last_message"] = event.get("message")
    summary["last_event"] = {
        "event_id": event.get("event_id"),
        "ts": event.get("ts"),
        "ts_iso": event.get("ts_iso"),
        "phase": event.get("phase"),
        "phase_label": event.get("phase_label"),
        "event_type": event_type,
        "message": event.get("message"),
        "structured_keys": sorted(structured.keys())[:20] if structured else [],
    }

    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    counts["events"] = summary["event_count"]
    if structured.get("state"):
        state = str(structured.get("state") or "")
        summary["state"] = state
        summary["lifecycle"] = "final" if state in FINAL_STATES else "running"
        summary["operation_status"] = summary["lifecycle"]
    if event_type == "error":
        summary["errors"] = int(summary.get("errors") or 0) + 1
        summary["last_error_code"] = _first_text(
            structured,
            "last_error_code",
            "error_code",
            "code",
            fallback=event.get("message"),
        )
    if event_type in WARNING_TYPES:
        summary["warnings"] = int(summary.get("warnings") or 0) + 1
    counts["warnings"] = int(summary.get("warnings") or 0)
    counts["errors"] = int(summary.get("errors") or 0)
    summary["counts"] = counts
    summary["diagnostic_status"] = _diagnostic_status(counts["warnings"], counts["errors"])
    summary["phases"] = _updated_phases(summary.get("phases"), event)
    correlation = _extract_correlation(event, structured)
    if correlation:
        current = summary.get("correlation") if isinstance(summary.get("correlation"), dict) else {}
        current.update(correlation)
        summary["correlation"] = current
    _write_json(path, summary)


def _write_human_follow(path: Path, event: Dict[str, Any]) -> None:
    payload = _read_json(path) or {
        "schema": "arr-blackbox-human-follow-v1",
        "translator_version": "arr-human-follow-v2",
        "read_order": SUMMARY_READ_ORDER,
        "job_id": event.get("job_id"),
        "lines": [],
    }
    lines = list(payload.get("lines") or [])
    lines.append(
        "{label} - {phase}/{event_type}: {message}".format(
            label=event.get("phase_label") or phase_label(event.get("phase")),
            phase=event.get("phase") or "unknown",
            event_type=event.get("event_type") or "decision",
            message=event.get("message") or "",
        )
    )
    payload["lines"] = lines[-80:]
    payload["current"] = {
        "phase": event.get("phase"),
        "phase_label": event.get("phase_label"),
        "event_type": event.get("event_type"),
        "message": event.get("message"),
        "ts": event.get("ts"),
        "ts_iso": event.get("ts_iso"),
    }
    structured = event.get("structured") if isinstance(event.get("structured"), dict) else {}
    if structured.get("state"):
        payload["state"] = structured.get("state")
        payload["operation_status"] = "final" if str(structured.get("state")) in FINAL_STATES else "running"
    payload["diagnostic_status"] = _diagnostic_status(
        len(_read_jsonl_safely(path.with_name("warnings.jsonl"))),
        len(_read_jsonl_safely(path.with_name("errors.jsonl"))),
    )
    correlation = _extract_correlation(event, structured)
    if correlation:
        current = payload.get("correlation") if isinstance(payload.get("correlation"), dict) else {}
        current.update(correlation)
        payload["correlation"] = current
    _write_json(path, payload)


def _write_related_files(path: Path, event: Dict[str, Any]) -> None:
    payload = _read_json(path) or {
        "schema": "arr-blackbox-related-files-v1",
        "job_id": event.get("job_id"),
        "files": [],
    }
    files = list(payload.get("files") or [])
    existing = {
        str(item.get("path") or "")
        for item in files
        if isinstance(item, dict)
    }
    for found in collect_related_paths(event.get("structured")):
        if found in existing:
            continue
        existing.add(found)
        files.append({"path": found, "source": "structured"})
        if len(files) >= 20:
            break
    payload["files"] = files
    _write_json(path, payload)


def _collect_paths(value: Any) -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {
                "log_file",
                "reports_dir",
                "report_path",
                "review_path",
                "reason_file",
                "final_dir",
                "final_video",
                "final_srt",
                "source_path",
                "stage_path",
                "output_root",
                "torrent_path",
            } and isinstance(item, str) and item.strip():
                found.append(item.strip())
            found.extend(_collect_paths(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_paths(item))
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(("/config/", "/data/")) or text.endswith((".json", ".log", ".txt")):
            found.append(text)
    return found


def _config_snapshot() -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "schema": "arr-config-snapshot-v1",
        "source": "environment",
    }
    for key, env_name in CONFIG_SNAPSHOT_KEYS.items():
        if isinstance(env_name, dict):
            group = {
                item_key: os.environ.get(item_env)
                for item_key, item_env in env_name.items()
                if os.environ.get(item_env) not in (None, "")
            }
            if group:
                snapshot[key] = group
        else:
            value = os.environ.get(env_name)
            if value not in (None, ""):
                snapshot[key] = value

    credential_flags = {
        key: any(os.environ.get(name) not in (None, "") for name in names)
        for key, names in CONFIG_CREDENTIAL_FLAGS.items()
    }
    if any(credential_flags.values()):
        snapshot["credential_flags"] = credential_flags

    sanitized = sanitize_for_export(snapshot)
    return sanitized if isinstance(sanitized, dict) else {"schema": "arr-config-snapshot-v1"}


def _updated_phases(existing: Any, event: Dict[str, Any]) -> List[Dict[str, Any]]:
    phases: Dict[str, Dict[str, Any]] = {}
    if isinstance(existing, list):
        for item in existing:
            if not isinstance(item, dict):
                continue
            phase = str(item.get("phase") or "")
            if phase:
                phases[phase] = dict(item)

    phase = str(event.get("phase") or "unknown")
    item = phases.get(phase) or {
        "phase": phase,
        "label": event.get("phase_label") or phase_label(phase),
        "events": 0,
        "warnings": 0,
        "errors": 0,
    }
    event_type = str(event.get("event_type") or "")
    item["label"] = event.get("phase_label") or phase_label(phase)
    item["events"] = int(item.get("events") or 0) + 1
    if event_type == "error":
        item["errors"] = int(item.get("errors") or 0) + 1
    if event_type in WARNING_TYPES:
        item["warnings"] = int(item.get("warnings") or 0) + 1
    item["last_event_type"] = event_type
    item["last_message"] = event.get("message")
    item["last_ts"] = event.get("ts")
    item["last_iso"] = event.get("ts_iso")
    phases[phase] = item
    return list(phases.values())[-40:]


def _diagnostic_status(warnings: int, errors: int) -> str:
    if errors:
        return "error"
    if warnings:
        return "warning"
    return "ok"


def _extract_correlation(event: Dict[str, Any], structured: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key in ("trace_id", "correlation_id", "source_trace_id", "search_trace_id", "download_trace_id"):
        value = structured.get(key)
        if value:
            result[key] = sanitize_text(value)
    job_id = event.get("job_id") or structured.get("job_id")
    if job_id:
        result["job_id"] = sanitize_text(job_id)
    return result


def _first_text(payload: Dict[str, Any], *keys: str, fallback: Any = "") -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return sanitize_text(value)
    return sanitize_text(fallback)


def _read_jsonl_safely(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    result: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            result.append(loaded)
    return result


def _touch_if_missing(path: Path) -> None:
    if not path.exists():
        path.touch()


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _append_timeline(path: Path, event: Dict[str, Any]) -> None:
    line = "[{ts}] {label} - {phase}/{event_type}: {message}\n".format(
        ts=event.get("ts_iso"),
        label=event.get("phase_label") or phase_label(event.get("phase")),
        phase=event.get("phase"),
        event_type=event.get("event_type"),
        message=event.get("message"),
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _float_or_now(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.time()


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(float(ts)))


def _format_time(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(ts)))
