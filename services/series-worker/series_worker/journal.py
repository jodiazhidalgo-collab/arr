"""Journal durable de una entrega de series.

Cada cambio se escribe primero en un JSONL append-only y despues en el
snapshot JSON. Ambos pasos hacen ``fsync``. Si el proceso cae entre ambos, el
JSONL es la fuente autoritativa y el snapshot se reconstruye al abrirlo.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


STATES = (
    "PREPARED",
    "PROCESSING",
    "VERIFIED",
    "COMMITTING",
    "COMMITTED",
    "ROLLED_BACK",
)

_ALLOWED_NEXT = {
    "PREPARED": {"PROCESSING", "ROLLED_BACK"},
    "PROCESSING": {"VERIFIED", "ROLLED_BACK"},
    "VERIFIED": {"COMMITTING", "ROLLED_BACK"},
    "COMMITTING": {"COMMITTED", "ROLLED_BACK"},
    "COMMITTED": set(),
    "ROLLED_BACK": set(),
}

_IDENTITY_DETAILS = {
    "job_id",
    "generation",
    "prepared_series_root",
    "final_series_root",
    "shadow_root",
    "mode",
    "marker_name",
}


class JournalError(RuntimeError):
    """Error base del journal."""


class JournalContradiction(JournalError):
    """El journal contiene dos hechos que no pueden ser ciertos a la vez."""

    code = "journal_contradiction"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Las claves del journal deben ser texto.")
            normalized[key] = _json_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"Valor no serializable en el journal: {type(value).__name__}")


def _encoded_json(payload: Any) -> bytes:
    try:
        text = json.dumps(
            _json_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise JournalError(f"Payload JSON no valido: {exc}") from exc
    return (text + "\n").encode("utf-8")


def fsync_directory(path: Path | str) -> None:
    """Sincroniza una entrada de directorio cuando el sistema lo permite."""

    directory = Path(path)
    if os.name == "nt":  # Windows no permite abrir directorios con os.open.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise NotADirectoryError(str(path))
    if not existed:
        fsync_directory(path)
        fsync_directory(path.parent)


def write_json_file_atomic(path: Path | str, payload: Any) -> Path:
    """Escribe JSON con temp + fsync + replace + fsync del directorio."""

    destination = Path(path)
    _ensure_directory(destination.parent)
    encoded = _encoded_json(payload)
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
        fsync_directory(destination.parent)
        return destination
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_directory(path.parent)
    encoded = _encoded_json(payload)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - defensa del contrato os.write
                raise OSError(errno.EIO, "No se pudo completar el append del journal")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def _record_shape(record: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise JournalContradiction(f"{source}: el registro no es un objeto JSON")
    sequence = record.get("sequence")
    state = record.get("state")
    details = record.get("details")
    if not isinstance(sequence, int) or sequence < 1:
        raise JournalContradiction(f"{source}: sequence no valido")
    if state not in STATES:
        raise JournalContradiction(f"{source}: estado no valido: {state!r}")
    if not isinstance(details, dict):
        raise JournalContradiction(f"{source}: details no es un objeto")
    if record.get("schema_version") != 1:
        raise JournalContradiction(f"{source}: schema_version no soportado")
    if not isinstance(record.get("updated_at"), str):
        raise JournalContradiction(f"{source}: updated_at no valido")
    return record


def _validate_history(records: list[dict[str, Any]]) -> None:
    previous: dict[str, Any] | None = None
    identity: dict[str, Any] = {}
    for expected, record in enumerate(records, start=1):
        _record_shape(record, source=f"journal.jsonl linea {expected}")
        if record["sequence"] != expected:
            raise JournalContradiction(
                "journal.jsonl: la secuencia no es continua"
            )
        if previous is None:
            if record["state"] != "PREPARED":
                raise JournalContradiction(
                    "journal.jsonl: el primer estado debe ser PREPARED"
                )
        elif record["state"] != previous["state"]:
            allowed = _ALLOWED_NEXT[previous["state"]]
            if record["state"] not in allowed:
                raise JournalContradiction(
                    f"Transicion imposible {previous['state']} -> {record['state']}"
                )
        for key in _IDENTITY_DETAILS:
            if key not in record["details"]:
                continue
            value = record["details"][key]
            if key in identity and identity[key] != value:
                raise JournalContradiction(
                    f"Identidad contradictoria para {key}: {identity[key]!r} != {value!r}"
                )
            identity[key] = value
        previous = record


class DurableJournal:
    """Journal JSON/JSONL durable, idempotente y verificable."""

    def __init__(self, job_dir: Path | str):
        self.job_dir = Path(job_dir)
        self.snapshot_path = self.job_dir / "journal.json"
        self.events_path = self.job_dir / "journal.jsonl"
        self._lock = threading.RLock()

    def write_json_atomic(self, name: str | Path, payload: Any) -> Path:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
            raise ValueError("El nombre debe ser una ruta relativa segura al job.")
        destination = self.job_dir / relative
        return write_json_file_atomic(destination, payload)

    def _read_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        try:
            raw = self.events_path.read_bytes()
        except OSError as exc:
            raise JournalError(f"No se puede leer journal.jsonl: {exc}") from exc
        records: list[dict[str, Any]] = []
        valid_length = 0
        chunks = raw.splitlines(keepends=True)
        for line_number, encoded_line in enumerate(chunks, start=1):
            complete = encoded_line.endswith((b"\n", b"\r"))
            if line_number == len(chunks) and not complete:
                self._truncate_torn_tail(valid_length)
                break
            try:
                line = encoded_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise JournalContradiction(
                    f"journal.jsonl linea {line_number} no es UTF-8"
                ) from exc
            if not line.strip():
                valid_length += len(encoded_line)
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JournalContradiction(
                    f"journal.jsonl linea {line_number} truncada o invalida"
                ) from exc
            records.append(_record_shape(value, source=f"journal.jsonl linea {line_number}"))
            valid_length += len(encoded_line)
        _validate_history(records)
        return records

    def _truncate_torn_tail(self, valid_length: int) -> None:
        """Retira solo el último append incompleto; nunca repara corrupción intermedia."""

        descriptor = os.open(self.events_path, os.O_RDWR)
        try:
            os.ftruncate(descriptor, valid_length)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(self.events_path.parent)

    def _read_snapshot_file(self) -> dict[str, Any] | None:
        if not self.snapshot_path.exists():
            return None
        try:
            value = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JournalContradiction("journal.json truncado o invalido") from exc
        return _record_shape(value, source="journal.json")

    def _load(self, *, repair: bool) -> dict[str, Any] | None:
        records = self._read_events()
        snapshot = self._read_snapshot_file()
        if not records and snapshot is None:
            return None
        if not records:
            raise JournalContradiction("Existe snapshot sin historial JSONL")

        latest = records[-1]
        if snapshot is None:
            if repair:
                write_json_file_atomic(self.snapshot_path, latest)
            return latest

        sequence = snapshot["sequence"]
        if sequence > latest["sequence"]:
            raise JournalContradiction("El snapshot esta por delante del JSONL")
        logged_at_sequence = records[sequence - 1]
        if snapshot != logged_at_sequence:
            raise JournalContradiction("Snapshot y JSONL se contradicen")
        if sequence < latest["sequence"] and repair:
            write_json_file_atomic(self.snapshot_path, latest)
        return latest

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            current = self._load(repair=True)
            return copy.deepcopy(current)

    @property
    def state(self) -> str | None:
        current = self.snapshot()
        return None if current is None else str(current["state"])

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            records = self._read_events()
            # Tambien comprueba la coherencia snapshot/JSONL y repara un corte.
            self._load(repair=True)
            return copy.deepcopy(records)

    def transition(self, state: str, **details: Any) -> dict[str, Any]:
        requested = str(state).strip().upper()
        if requested not in STATES:
            raise JournalError(f"Estado no valido: {state!r}")
        normalized_details = _json_value(details)
        with self._lock:
            current = self._load(repair=True)
            if current is None:
                if requested != "PREPARED":
                    raise JournalContradiction(
                        "El primer estado del journal debe ser PREPARED"
                    )
                merged = normalized_details
                sequence = 1
            else:
                current_state = current["state"]
                if requested == current_state:
                    merged = dict(current["details"])
                    for key, value in normalized_details.items():
                        if key in merged and merged[key] != value:
                            raise JournalContradiction(
                                f"Replay contradictorio de {requested}: {key}"
                            )
                        merged[key] = value
                    if merged == current["details"]:
                        return copy.deepcopy(current)
                else:
                    if requested not in _ALLOWED_NEXT[current_state]:
                        raise JournalContradiction(
                            f"Transicion imposible {current_state} -> {requested}"
                        )
                    merged = dict(current["details"])
                    for key, value in normalized_details.items():
                        if (
                            key in _IDENTITY_DETAILS
                            and key in merged
                            and merged[key] != value
                        ):
                            raise JournalContradiction(
                                f"Identidad contradictoria para {key}"
                            )
                        merged[key] = value
                sequence = int(current["sequence"]) + 1

            record = {
                "schema_version": 1,
                "sequence": sequence,
                "state": requested,
                "updated_at": _utc_now(),
                "details": merged,
            }
            _append_jsonl(self.events_path, record)
            write_json_file_atomic(self.snapshot_path, record)
            return copy.deepcopy(record)


__all__ = [
    "DurableJournal",
    "JournalContradiction",
    "JournalError",
    "STATES",
    "fsync_directory",
    "write_json_file_atomic",
]
