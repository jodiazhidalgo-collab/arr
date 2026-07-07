import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from modulos.diagnostic_sanitizer import collect_related_paths, phase_label, sanitize_for_export, sanitize_text


SAFE_KIND = {"search", "download", "monitor"}
TRACE_READ_ORDER = [
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
    "paths": {
        "rdt_save_root": "RDT_SAVE_ROOT",
        "qbit_save_root": "QBIT_SAVE_ROOT",
        "logs": "LOG_DIR",
        "data": "DATA_DIR",
        "diagnostics": "ARR_DIAGNOSTICS_ROOT",
    },
    "services": {
        "real_debrid": "REAL_DEBRID_API",
    },
}
CONFIG_CREDENTIAL_FLAGS = {
    "jackett_credential_present": ("JACKETT_API_KEY",),
    "real_debrid_credential_present": ("REAL_DEBRID_TOKEN",),
}


class ArrTrace:
    def __init__(self, root: Path, logger=None, enabled: bool = True):
        self.root = Path(root)
        self.logger = logger
        self.enabled = enabled
        self.lock = threading.RLock()

    def trace_id(self, prefix: str, payload: Any) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return safe_trace_id(f"{prefix}-{int(time.time())}-{digest}")

    def start(self, kind: str, trace_id: str, request: dict[str, Any]) -> None:
        self.event(kind, trace_id, "request", "started", "Trazabilidad iniciada", request)

    def event(
        self,
        kind: str,
        trace_id: str,
        phase: str,
        event_type: str,
        message: str,
        structured: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            self._event(kind, trace_id, phase, event_type, message, structured)
        except Exception as exc:
            if self.logger:
                self.logger.warning("arr trace failed id=%s error=%s", trace_id, str(exc)[:160])

    def finish(
        self,
        kind: str,
        trace_id: str,
        state: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        event_type = "error" if error else "finished"
        payload = {"state": state, "result": result or {}, "error": error}
        self.event(kind, trace_id, "finish", event_type, f"Traza cerrada: {state}", payload)

    def link_job(
        self,
        kind: str,
        trace_id: str,
        job_id: str,
        **metadata: Any,
    ) -> None:
        payload = {"job_id": job_id, **metadata}
        self.event(kind, trace_id, "correlation", "decision", "Traza enlazada con job ARR", payload)

    def _event(
        self,
        kind: str,
        trace_id: str,
        phase: str,
        event_type: str,
        message: str,
        structured: dict[str, Any] | None,
    ) -> None:
        kind = kind if kind in SAFE_KIND else "download"
        trace_id = safe_trace_id(trace_id)
        if not trace_id:
            return
        ts = time.time()
        payload = {
            "schema": "arr-search-trace-event-v1",
            "trace_id": trace_id,
            "kind": kind,
            "ts": ts,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)),
            "phase": str(phase or "unknown"),
            "phase_label": phase_label(phase or "unknown"),
            "event_type": clean_event_type(event_type),
            "message": sanitize_text(message or ""),
            "structured": sanitize_for_export(json_safe(structured or {})),
        }
        with self.lock:
            trace_dir = self.root / kind / time.strftime("%Y-%m-%d", time.localtime(ts)) / trace_id
            trace_dir.mkdir(parents=True, exist_ok=True)
            write_meta_if_missing(trace_dir / "meta.json", payload)
            touch_if_missing(trace_dir / "warnings.jsonl")
            touch_if_missing(trace_dir / "errors.jsonl")
            append_jsonl(trace_dir / "events.jsonl", payload)
            append_timeline(trace_dir / "timeline.md", payload)
            if payload["event_type"] == "error":
                append_jsonl(trace_dir / "errors.jsonl", payload)
            elif payload["event_type"] in {"warning", "retry", "skipped"}:
                append_jsonl(trace_dir / "warnings.jsonl", payload)
            write_summary(trace_dir / "summary.json", payload)
            write_human_follow(trace_dir / "human_follow.json", payload)
            write_related_files(trace_dir / "related_files.json", payload)


def safe_trace_id(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip())
    return text.strip("-_")[:96]


def clean_event_type(value: str) -> str:
    text = str(value or "decision").strip().lower()
    return text if text in {"started", "finished", "decision", "command", "warning", "error", "skipped", "retry"} else "decision"


def json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def touch_if_missing(path: Path) -> None:
    if not path.exists():
        path.touch()


def write_meta_if_missing(path: Path, event: dict[str, Any]) -> None:
    if path.exists():
        return
    payload = {
        "schema": "arr-search-trace-meta-v1",
        "trace_id": event.get("trace_id"),
        "kind": event.get("kind"),
        "trace_kind": "buscador_bridge_trace",
        "action": event.get("kind"),
        "created_ts": event.get("ts"),
        "created_iso": event.get("ts_iso"),
        "config_snapshot": config_snapshot(),
        "read_order": TRACE_READ_ORDER,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def append_timeline(path: Path, payload: dict[str, Any]) -> None:
    line = "[{ts}] {label} - {phase}/{event_type}: {message}\n".format(
        ts=payload.get("ts_iso"),
        label=payload.get("phase_label") or phase_label(payload.get("phase")),
        phase=payload.get("phase"),
        event_type=payload.get("event_type"),
        message=payload.get("message"),
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def write_human_follow(path: Path, event: dict[str, Any]) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            payload = {}
    if not payload:
        payload = {
            "schema": "arr-search-trace-human-follow-v1",
            "translator_version": "arr-search-trace-human-v2",
            "read_order": TRACE_READ_ORDER,
            "trace_id": event.get("trace_id"),
            "kind": event.get("kind"),
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
        payload["operation_status"] = "final" if str(structured.get("state")) not in {"received", "running"} else "running"
    correlation = extract_correlation(event, structured)
    if correlation:
        current = payload.get("correlation") if isinstance(payload.get("correlation"), dict) else {}
        current.update(correlation)
        payload["correlation"] = current
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def write_related_files(path: Path, event: dict[str, Any]) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            payload = {}
    if not payload:
        payload = {
            "schema": "arr-search-trace-related-files-v1",
            "trace_id": event.get("trace_id"),
            "kind": event.get("kind"),
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
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def collect_paths(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {
                "torrent_path",
                "download_path",
                "savepath",
                "file",
                "path",
                "log_file",
            } and isinstance(item, str) and item.strip():
                found.append(item.strip())
            found.extend(collect_paths(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(collect_paths(item))
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(("/app/", "/data/", "/diagnostics/")) or text.endswith((".json", ".log", ".torrent")):
            found.append(text)
    return found


def write_summary(path: Path, event: dict[str, Any]) -> None:
    summary = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            summary = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            summary = {}
    if not summary:
        summary = {
            "schema": "arr-search-trace-summary-v1",
            "source": "buscador-puente-arr:arr_trace",
            "read_order": TRACE_READ_ORDER,
            "trace_id": event.get("trace_id"),
            "kind": event.get("kind"),
            "first_ts": event.get("ts"),
            "first_iso": event.get("ts_iso"),
            "event_count": 0,
            "errors": 0,
            "warnings": 0,
            "counts": {"events": 0, "warnings": 0, "errors": 0},
            "phases": [],
            "diagnostic_status": "ok",
            "config_snapshot": config_snapshot(),
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
        "ts": event.get("ts"),
        "ts_iso": event.get("ts_iso"),
        "phase": event.get("phase"),
        "phase_label": event.get("phase_label"),
        "event_type": event_type,
        "message": event.get("message"),
    }
    if structured.get("state"):
        summary["state"] = structured.get("state")
        summary["operation_status"] = "final" if str(structured.get("state")) not in {"received", "running"} else "running"
    if event_type == "error":
        summary["errors"] = int(summary.get("errors") or 0) + 1
    if event_type in {"warning", "retry", "skipped"}:
        summary["warnings"] = int(summary.get("warnings") or 0) + 1
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    counts["events"] = int(summary.get("event_count") or 0)
    counts["warnings"] = int(summary.get("warnings") or 0)
    counts["errors"] = int(summary.get("errors") or 0)
    summary["counts"] = counts
    summary["diagnostic_status"] = "error" if counts["errors"] else ("warning" if counts["warnings"] else "ok")
    summary["phases"] = updated_phases(summary.get("phases"), event)
    correlation = extract_correlation(event, structured)
    if correlation:
        current = summary.get("correlation") if isinstance(summary.get("correlation"), dict) else {}
        current.update(correlation)
        summary["correlation"] = current
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def updated_phases(existing: Any, event: dict[str, Any]) -> list[dict[str, Any]]:
    phases: dict[str, dict[str, Any]] = {}
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
    if event_type in {"warning", "retry", "skipped"}:
        item["warnings"] = int(item.get("warnings") or 0) + 1
    item["last_event_type"] = event_type
    item["last_message"] = event.get("message")
    item["last_ts"] = event.get("ts")
    item["last_iso"] = event.get("ts_iso")
    phases[phase] = item
    return list(phases.values())[-40:]


def config_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "schema": "arr-bridge-config-snapshot-v1",
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
    return sanitized if isinstance(sanitized, dict) else {"schema": "arr-bridge-config-snapshot-v1"}


def extract_correlation(event: dict[str, Any], structured: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    result["trace_id"] = sanitize_text(event.get("trace_id"))
    for key in ("job_id", "correlation_id", "source_trace_id", "search_trace_id", "download_trace_id"):
        value = find_first(structured, key)
        if value:
            result[key] = sanitize_text(value)
    for target, keys in {
        "qbit_hash": ("qbit_hash", "qbt_hash", "infohash", "hash"),
        "rdt_id": ("rdt_id", "torrent_id", "torrentId"),
        "engine": ("engine",),
    }.items():
        value = find_first(structured, *keys)
        if value:
            result[target] = sanitize_text(value)
    return {key: value for key, value in result.items() if value}


def find_first(value: Any, *keys: str) -> Any:
    wanted = {key.lower() for key in keys}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in wanted and item not in ("", None):
                return item
        for item in value.values():
            found = find_first(item, *keys)
            if found not in ("", None):
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_first(item, *keys)
            if found not in ("", None):
                return found
    return None
