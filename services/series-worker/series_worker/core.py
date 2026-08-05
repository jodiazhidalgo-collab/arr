"""Coordinador durable del flujo completo de Series Worker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import unicodedata
import urllib.request
import uuid
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, ContextManager
from urllib.parse import urlsplit

from .delivery import (
    MARKER_NAME,
    DeliveryError,
    RecoveryAmbiguous,
    preflight_atomic_exchange,
    publish_series,
    recover_delivery,
)
from .heavy_lock import HeavyLockTimeout, series_heavy_lock
from .journal import (
    DurableJournal,
    JournalContradiction,
)
from .manifest import (
    ManifestEntry,
    ManifestError,
    ManifestSidecar,
    SeriesManifest,
    discover_manifest,
    episode_cluster_numbers,
    validate_relative_path,
)
from .processing import (
    BASE_TOOLS,
    OCR_TOOLS,
    EpisodeProcessingError,
    ProcessingResult,
    ReviewRequiredError,
    SeriesProcessor,
    processing_review_identity,
    unavailable_tools,
)
from .rules import RulesSnapshot, RulesStore, RulesValidationError, rules_fingerprint


JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EPISODE_TOKEN_RE = re.compile(
    r"(?i)(?<![A-Z0-9])S(?P<season>\d{1,3})(?P<body>E\d{1,3}(?:(?:[ ._-]*E|[ ._-]+)\d{1,3})*)"
)
PAYLOAD_KEYS = {
    "job_id",
    "job_root",
    "source_root",
    "final_root",
    "review_root",
    "reports_root",
    "callback_url",
}
RESULT_FILE = "series_result.json"
REQUEST_FILE = "request.json"
MANIFEST_FILE = "manifest.json"
RULES_SNAPSHOT_FILE = "rules_snapshot.json"
DEFAULT_ALLOWED_ROOT = "/data/downloads/torrents/complete/taller"
DEFAULT_FINAL_ROOT = "/data/media/tv"
DEFAULT_REVIEW_ROOT = "/data/media/repetidas_vs_error"
LEGACY_REVIEW_ROOT = "/data/media/repetidas_vs_error_series"
DEFAULT_REPORT_ROOT = "/config/series-worker"
DEFAULT_HEALTH_ATOMICITY_CACHE_TTL_SEC = 300.0
DEFAULT_CALLBACK_ORIGIN = "http://arr-orchestrator:8787"
DEFAULT_LOCK_PATH = "/config/worker-locks/media-heavy.lock"


class SeriesWorkerError(RuntimeError):
    code = "series_worker_failed"
    http_status = 500
    retryable = False


class RequestValidationError(SeriesWorkerError):
    code = "invalid_request"
    http_status = 400


class ServiceUnavailable(SeriesWorkerError):
    code = "series_worker_unavailable"
    http_status = 503


class SeriesWorkerBusy(SeriesWorkerError):
    code = "series_worker_busy"
    http_status = 409
    retryable = True


class JobConflict(SeriesWorkerError):
    code = "job_conflict"
    http_status = 409


@dataclass(frozen=True)
class ValidatedPayload:
    job_id: str
    job_root: Path
    source_root: Path
    final_root: Path
    review_root: Path
    reports_root: Path
    callback_url: str

    def persisted(self) -> dict[str, str]:
        return {
            "job_id": self.job_id,
            "job_root": str(self.job_root),
            "source_root": str(self.source_root),
            "final_root": str(self.final_root),
            "review_root": str(self.review_root),
            "reports_root": str(self.reports_root),
            "callback_url": self.callback_url,
        }


@dataclass(frozen=True)
class CollisionPlan:
    final_series_root: Path | None
    pending: tuple[ManifestEntry, ...]
    satisfied: tuple[ManifestEntry, ...]
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class PreparedJob:
    payload: ValidatedPayload
    manifest: SeriesManifest
    rules_snapshot: RulesSnapshot
    payload_digest: str
    request_digest: str
    journal: DurableJournal
    collision: CollisionPlan
    generation: str
    prepared_series_root: Path | None


@dataclass(frozen=True)
class ReservedJob:
    payload: ValidatedPayload
    rules_snapshot: RulesSnapshot
    payload_digest: str
    generation: str
    journal: DurableJournal


@dataclass(frozen=True)
class Submission:
    http_status: int
    payload: dict[str, Any]


def _configured_path(name: str, default: str) -> Path:
    return Path(str(os.environ.get(name, default) or default).strip()).resolve()


def _configured_reports_root() -> Path:
    lexical = Path(
        str(
            os.environ.get("SERIES_WORKER_REPORT_ROOT", DEFAULT_REPORT_ROOT)
            or DEFAULT_REPORT_ROOT
        ).strip()
    )
    if lexical.is_symlink():
        raise ServiceUnavailable("La raíz de informes no puede ser un enlace simbólico")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as error:
        raise ServiceUnavailable("La raíz de informes no existe") from error
    if not resolved.is_dir():
        raise ServiceUnavailable("La raíz de informes no es una carpeta")
    return resolved


def _allowed_roots() -> tuple[Path, ...]:
    raw = str(os.environ.get("SERIES_WORKER_ALLOWED_ROOTS", DEFAULT_ALLOWED_ROOT))
    roots = tuple(
        Path(value.strip()).resolve()
        for value in raw.split(os.pathsep)
        if value.strip()
    )
    return roots or (Path(DEFAULT_ALLOWED_ROOT).resolve(),)


def _regular_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise RequestValidationError(f"{label} no puede ser un enlace simbólico")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RequestValidationError(f"{label} no existe") from error
    if not resolved.is_dir():
        raise RequestValidationError(f"{label} debe ser una carpeta")
    return resolved


def _validated_callback(value: Any, job_id: str) -> str:
    callback = str(value or "").strip()
    if not callback:
        return ""
    parsed = urlsplit(callback)
    allowed = urlsplit(
        str(
            os.environ.get("SERIES_WORKER_CALLBACK_ORIGIN", DEFAULT_CALLBACK_ORIGIN)
            or DEFAULT_CALLBACK_ORIGIN
        ).strip()
    )
    if (
        parsed.scheme != allowed.scheme
        or parsed.hostname != allowed.hostname
        or parsed.port != allowed.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/jobs/{job_id}/events"
    ):
        raise RequestValidationError("callback_url no pertenece al orquestador permitido")
    return callback


def _validated_payload(
    payload: Any,
    *,
    require_directories: bool,
    allow_legacy_review_root: bool = False,
) -> ValidatedPayload:
    if not isinstance(payload, dict):
        raise RequestValidationError("El payload debe ser un objeto JSON")
    unknown = sorted(set(payload) - PAYLOAD_KEYS)
    missing = sorted(PAYLOAD_KEYS - set(payload))
    if unknown or missing:
        detail = []
        if missing:
            detail.append("faltan: " + ", ".join(missing))
        if unknown:
            detail.append("desconocidos: " + ", ".join(unknown))
        raise RequestValidationError("Payload exacto inválido; " + "; ".join(detail))
    if any(not isinstance(payload[key], str) for key in PAYLOAD_KEYS):
        raise RequestValidationError("Todos los campos del payload deben ser texto")
    job_id = payload["job_id"].strip()
    if not JOB_ID_RE.fullmatch(job_id):
        raise RequestValidationError("job_id no es válido")

    path_fields = ("job_root", "source_root", "final_root", "review_root", "reports_root")
    if any(not payload[field].strip() for field in path_fields):
        raise RequestValidationError("Las rutas del payload no pueden estar vacías")

    resolved: dict[str, Path] = {}
    for field in path_fields:
        lexical = Path(payload[field].strip())
        if require_directories:
            resolved[field] = _regular_directory(lexical, field)
            continue
        if lexical.is_symlink():
            raise RequestValidationError(f"{field} no puede ser un enlace simbólico")
        try:
            resolved[field] = lexical.resolve(strict=False)
        except OSError as error:
            raise RequestValidationError(f"{field} no es una ruta válida") from error

    job_root = resolved["job_root"]
    source_root = resolved["source_root"]
    final_root = resolved["final_root"]
    review_root = resolved["review_root"]
    reports_root = resolved["reports_root"]

    allowed = _allowed_roots()
    if job_root.name != job_id or job_root.parent not in allowed:
        raise RequestValidationError("job_root debe ser <taller>/<job_id>")
    if source_root != job_root / "series_filebot_output":
        raise RequestValidationError(
            "source_root debe ser <job_root>/series_filebot_output"
        )
    if final_root != _configured_path("SERIES_WORKER_FINAL_ROOT", DEFAULT_FINAL_ROOT):
        raise RequestValidationError("final_root no es la raíz TV canónica")
    configured_review_root = _configured_path(
        "SERIES_WORKER_REVIEW_ROOT",
        DEFAULT_REVIEW_ROOT,
    )
    legacy_review_root = Path(LEGACY_REVIEW_ROOT).resolve()
    if review_root != configured_review_root and not (
        allow_legacy_review_root and review_root == legacy_review_root
    ):
        raise RequestValidationError("review_root no es la raíz de revisión canónica")
    if reports_root != _configured_reports_root():
        raise RequestValidationError("reports_root no es la raíz de informes canónica")
    return ValidatedPayload(
        job_id=job_id,
        job_root=job_root,
        source_root=source_root,
        final_root=final_root,
        review_root=review_root,
        reports_root=reports_root,
        callback_url=_validated_callback(payload["callback_url"], job_id),
    )


def validate_payload(payload: Any) -> ValidatedPayload:
    return _validated_payload(payload, require_directories=True)


def _digest(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ServiceUnavailable(
                f"Estado durable atraviesa un enlace simbólico: {path.name}"
            )
    if not path.is_file():
        return None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ServiceUnavailable(f"Estado durable no regular: {path.name}")
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
                value = json.load(handle)
        finally:
            os.close(descriptor)
    except (OSError, json.JSONDecodeError) as error:
        raise ServiceUnavailable(f"Estado durable ilegible: {path.name}") from error
    if not isinstance(value, dict):
        raise ServiceUnavailable(f"Estado durable inválido: {path.name}")
    return value


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _series_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _path_key(value: str) -> str:
    """Clave uniforme para comparar rutas logicas sin perder fail-closed."""

    return unicodedata.normalize("NFKC", value).casefold()


def _collision_to_dict(collision: CollisionPlan) -> dict[str, Any]:
    final_series_root = ""
    if collision.final_series_root is not None:
        final_series_root = collision.final_series_root.name
    return {
        "final_series_root": final_series_root,
        "pending": [entry.source_relpath for entry in collision.pending],
        "satisfied": [entry.source_relpath for entry in collision.satisfied],
        "review_reasons": list(collision.review_reasons),
    }


def _collision_from_dict(
    value: Any,
    payload: ValidatedPayload,
    manifest: SeriesManifest,
) -> CollisionPlan:
    if not isinstance(value, dict) or set(value) != {
        "final_series_root",
        "pending",
        "satisfied",
        "review_reasons",
    }:
        raise ServiceUnavailable("Plan de colisiones durable no válido")
    final_name = value.get("final_series_root")
    pending_values = value.get("pending")
    satisfied_values = value.get("satisfied")
    reasons = value.get("review_reasons")
    if (
        not isinstance(final_name, str)
        or not isinstance(pending_values, list)
        or not isinstance(satisfied_values, list)
        or not isinstance(reasons, list)
        or any(not isinstance(reason, str) or not reason for reason in reasons)
        or len(set(reasons)) != len(reasons)
    ):
        raise ServiceUnavailable("Plan de colisiones durable no válido")
    by_source = {entry.source_relpath: entry for entry in manifest.entries}

    def select(values: list[Any], label: str) -> tuple[ManifestEntry, ...]:
        if (
            any(not isinstance(item, str) for item in values)
            or len(set(values)) != len(values)
            or any(item not in by_source for item in values)
        ):
            raise ServiceUnavailable(f"Plan de colisiones durable: {label} no válido")
        return tuple(by_source[item] for item in values)

    pending = select(pending_values, "pending")
    satisfied = select(satisfied_values, "satisfied")
    if set(pending_values) & set(satisfied_values):
        raise ServiceUnavailable("Plan de colisiones durable solapado")
    if not manifest.review_reasons and not reasons and (
        set(pending_values) | set(satisfied_values) != set(by_source)
    ):
        raise ServiceUnavailable("Plan de colisiones durable incompleto")
    final_root: Path | None = None
    if final_name:
        try:
            normalized = validate_relative_path(final_name)
        except ManifestError as error:
            raise ServiceUnavailable("Raíz final durable no válida") from error
        if len(PurePosixPath(normalized).parts) != 1:
            raise ServiceUnavailable("Raíz final durable no válida")
        final_root = payload.final_root / final_name
    elif manifest.ready and not reasons:
        raise ServiceUnavailable("Plan durable sin raíz final")
    return CollisionPlan(
        final_series_root=final_root,
        pending=pending,
        satisfied=satisfied,
        review_reasons=tuple(reasons),
    )


def _manifest_from_dict(payload: dict[str, Any]) -> SeriesManifest:
    try:
        expected_document = {
            "schema", "status", "digest", "series_name", "series_key",
            "review_reasons", "entries",
        }
        if set(payload) != expected_document or payload.get("schema") != "series-manifest-v1":
            raise ValueError("esquema de manifiesto no soportado")
        raw_entries = payload["entries"]
        raw_reasons = payload["review_reasons"]
        if not isinstance(raw_entries, list) or not isinstance(raw_reasons, list):
            raise TypeError("entries y review_reasons deben ser listas")
        if any(not isinstance(reason, str) or not reason for reason in raw_reasons):
            raise ValueError("review_reasons contiene valores no válidos")
        if len(set(raw_reasons)) != len(raw_reasons):
            raise ValueError("review_reasons contiene duplicados")

        entries_list: list[ManifestEntry] = []
        for item in raw_entries:
            if not isinstance(item, dict) or set(item) != {
                "source_relpath", "target_relpath", "series_name", "series_key",
                "season", "episodes", "size", "mtime_ns", "source_fingerprint",
                "content_sha256", "subtitle_sidecars",
            }:
                raise ValueError("entrada durable con estructura no válida")
            string_fields = (
                "source_relpath", "target_relpath", "series_name", "series_key",
                "source_fingerprint",
            )
            if any(not isinstance(item[field], str) or not item[field] for field in string_fields):
                raise ValueError("entrada durable contiene texto no válido")
            if not isinstance(item["content_sha256"], str):
                raise ValueError("entrada durable contiene texto no válido")
            if (
                not isinstance(item["season"], int)
                or isinstance(item["season"], bool)
                or item["season"] < 0
                or not isinstance(item["size"], int)
                or isinstance(item["size"], bool)
                or item["size"] < 0
                or not isinstance(item["mtime_ns"], int)
                or isinstance(item["mtime_ns"], bool)
            ):
                raise ValueError("entrada durable contiene números no válidos")
            episodes = item["episodes"]
            if (
                not isinstance(episodes, list)
                or not episodes
                or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in episodes)
                or len(set(episodes)) != len(episodes)
            ):
                raise ValueError("episodes no es válido")
            source_relpath = validate_relative_path(item["source_relpath"])
            target_relpath = validate_relative_path(item["target_relpath"])
            target = PurePosixPath(target_relpath)
            if (
                len(target.parts) != 3
                or target.parts[0] != item["series_name"]
                or target.parts[1] != f"Season {item['season']:02d}"
                or target.name != f"{PurePosixPath(source_relpath).stem}.mkv"
                or item["series_key"] != _series_key(item["series_name"])
                or not _is_sha256(item["source_fingerprint"])
                or (
                    item["content_sha256"] != ""
                    and not _is_sha256(item["content_sha256"])
                )
            ):
                raise ValueError("identidad de entrada durable no válida")
            raw_sidecars = item["subtitle_sidecars"]
            if not isinstance(raw_sidecars, list):
                raise TypeError("subtitle_sidecars debe ser una lista")
            sidecars: list[ManifestSidecar] = []
            for sidecar in raw_sidecars:
                if not isinstance(sidecar, dict) or set(sidecar) != {
                    "source_relpath", "size", "mtime_ns", "content_sha256"
                }:
                    raise ValueError("sidecar durable con estructura no válida")
                if (
                    not isinstance(sidecar["source_relpath"], str)
                    or not isinstance(sidecar["size"], int)
                    or isinstance(sidecar["size"], bool)
                    or sidecar["size"] < 0
                    or not isinstance(sidecar["mtime_ns"], int)
                    or isinstance(sidecar["mtime_ns"], bool)
                    or not isinstance(sidecar["content_sha256"], str)
                    or (
                        sidecar["content_sha256"] != ""
                        and not _is_sha256(sidecar["content_sha256"])
                    )
                ):
                    raise ValueError("sidecar durable no válido")
                sidecars.append(
                    ManifestSidecar(
                        source_relpath=validate_relative_path(sidecar["source_relpath"]),
                        size=sidecar["size"],
                        mtime_ns=sidecar["mtime_ns"],
                        content_sha256=sidecar["content_sha256"],
                    )
                )
            sidecar_sort_keys = tuple(
                (_path_key(sidecar.source_relpath), sidecar.source_relpath)
                for sidecar in sidecars
            )
            if sidecar_sort_keys != tuple(sorted(sidecar_sort_keys)):
                raise ValueError("sidecars durables fuera de orden")
            if len({key for key, _raw in sidecar_sort_keys}) != len(sidecars):
                if not any(
                    reason.startswith("colision_sidecar_casefold:")
                    for reason in raw_reasons
                ):
                    raise ValueError(
                        "sidecars durables equivalentes sin motivo de revisión"
                    )
            entries_list.append(
                ManifestEntry(
                    source_relpath=source_relpath,
                    target_relpath=target_relpath,
                    series_name=item["series_name"],
                    series_key=item["series_key"],
                    season=item["season"],
                    episodes=tuple(episodes),
                    size=item["size"],
                    mtime_ns=item["mtime_ns"],
                    source_fingerprint=item["source_fingerprint"],
                    content_sha256=item["content_sha256"],
                    subtitle_sidecars=tuple(sidecars),
                )
            )

        entries = tuple(entries_list)
        if entries != tuple(
            sorted(entries, key=lambda entry: (_path_key(entry.target_relpath), entry.source_relpath))
        ):
            raise ValueError("entradas durables fuera de orden")
        if len({_path_key(entry.source_relpath) for entry in entries}) != len(entries):
            raise ValueError("source_relpath durable duplicado")
        if len({_path_key(entry.target_relpath) for entry in entries}) != len(entries):
            raise ValueError("target_relpath durable duplicado")
        status = payload["status"]
        if status not in {"ready", "review"}:
            raise ValueError("status durable no válido")
        if (status == "ready" and raw_reasons) or (status == "review" and not raw_reasons):
            raise ValueError("status y review_reasons se contradicen")
        series = {entry.series_key: entry.series_name for entry in entries}
        expected_name = next(iter(series.values())) if len(series) == 1 else None
        expected_key = next(iter(series)) if len(series) == 1 else None
        if payload["series_name"] != expected_name or payload["series_key"] != expected_key:
            raise ValueError("identidad global de serie no válida")
        if status == "ready" and (not entries or len(series) != 1):
            raise ValueError("manifiesto ready sin una única serie")
        digest = payload["digest"]
        if not _is_sha256(digest):
            raise ValueError("digest durable no válido")
        expected_digest = _digest(
            {
                "entries": [entry.to_dict() for entry in entries],
                "review_reasons": sorted(raw_reasons),
            }
        )
        if digest != expected_digest:
            raise ValueError("digest durable no coincide con su contenido")
        return SeriesManifest(
            status=status,
            digest=digest,
            entries=entries,
            series_name=expected_name,
            series_key=expected_key,
            review_reasons=tuple(raw_reasons),
        )
    except (KeyError, ManifestError, TypeError, ValueError) as error:
        raise ServiceUnavailable("manifest.json durable no es válido") from error


def _rules_from_dict(payload: dict[str, Any]) -> RulesSnapshot:
    rules = payload.get("rules")
    fingerprint = payload.get("fingerprint")
    if (
        set(payload) != {"rules", "fingerprint"}
        or not isinstance(rules, dict)
        or not _is_sha256(fingerprint)
        or rules_fingerprint(rules) != fingerprint
    ):
        raise ServiceUnavailable("rules_snapshot.json durable no es válido")
    return RulesSnapshot(rules=deepcopy(rules), fingerprint=fingerprint)


def _episode_identity(path: Path) -> tuple[int, frozenset[int]] | None:
    match = EPISODE_TOKEN_RE.search(path.stem)
    if match is None:
        return None
    episodes = frozenset(episode_cluster_numbers(match.group("body")))
    if not episodes:
        return None
    return int(match.group("season")), episodes


def _existing_series_root(final_root: Path, name: str) -> tuple[Path, list[str]]:
    direct = final_root / name
    folded = _series_key(name)
    matches = [
        child
        for child in final_root.iterdir()
        if _series_key(child.name) == folded
    ]
    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        return direct, ["varias_raices_casefold_en_tv"]
    return direct, []


def _season_directory_number(value: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    match = re.fullmatch(r"season\s*0*(\d+)", normalized)
    return int(match.group(1)) if match else None


def plan_collisions(
    payload: ValidatedPayload,
    manifest: SeriesManifest,
) -> CollisionPlan:
    if not manifest.ready or not manifest.series_name:
        return CollisionPlan(
            final_series_root=None,
            pending=(),
            satisfied=(),
            review_reasons=manifest.review_reasons or ("manifest_no_apto",),
        )
    final_series, reasons = _existing_series_root(payload.final_root, manifest.series_name)
    if final_series.exists() and (final_series.is_symlink() or not final_series.is_dir()):
        reasons.append("raiz_final_no_es_directorio")
    existing_by_case: dict[str, Path] = {}
    existing_relative_by_case: dict[str, str] = {}
    existing_directories_by_case: dict[str, str] = {}
    existing_season_directories: dict[int, list[str]] = {}
    existing_episode_directories: dict[int, set[str]] = {}
    existing_episodes: list[tuple[Path, int, frozenset[int]]] = []
    if final_series.is_dir() and not final_series.is_symlink():
        for path in sorted(final_series.rglob("*")):
            if path.is_symlink():
                reasons.append("symlink_en_serie_final")
                continue
            relative = path.relative_to(final_series).as_posix()
            folded = _path_key(relative)
            if path.is_dir():
                previous_directory = existing_directories_by_case.get(folded)
                if previous_directory is not None and previous_directory != relative:
                    reasons.append(f"colision_directorio_casefold_tv:{relative}")
                else:
                    existing_directories_by_case[folded] = relative
                relative_path = PurePosixPath(relative)
                if len(relative_path.parts) == 1:
                    season_number = _season_directory_number(relative_path.name)
                    if season_number is not None:
                        existing_season_directories.setdefault(
                            season_number, []
                        ).append(relative)
                continue
            if path.name == MARKER_NAME:
                continue
            if not path.is_file():
                reasons.append(f"entrada_no_regular_en_serie_final:{relative}")
                continue
            previous = existing_by_case.get(folded)
            if previous is not None and previous != path:
                reasons.append(f"colision_casefold_tv:{relative}")
            else:
                existing_by_case[folded] = path
                existing_relative_by_case[folded] = relative
            identity = _episode_identity(path)
            if identity is not None:
                existing_episodes.append((path, identity[0], identity[1]))
                existing_episode_directories.setdefault(identity[0], set()).add(
                    PurePosixPath(relative).parent.as_posix()
                )

    pending: list[ManifestEntry] = []
    satisfied: list[ManifestEntry] = []
    for entry in manifest.entries:
        parts = PurePosixPath(entry.target_relpath).parts
        if len(parts) < 2:
            reasons.append(f"target_sin_raiz:{entry.target_relpath}")
            continue
        inside_series = PurePosixPath(*parts[1:]).as_posix()
        expected_directory = PurePosixPath(*parts[1:-1]).as_posix()
        expected_directory_key = _path_key(expected_directory)
        equivalent_directory = existing_directories_by_case.get(
            expected_directory_key
        )
        if equivalent_directory is not None and equivalent_directory != expected_directory:
            reasons.append(
                f"directorio_no_canonico_tv:{equivalent_directory}:"
                f"{expected_directory}"
            )
            continue
        other_season_directories = [
            directory
            for directory in existing_season_directories.get(entry.season, [])
            if directory != expected_directory
        ]
        other_season_directories.extend(
            sorted(
                directory
                for directory in existing_episode_directories.get(entry.season, set())
                if directory != expected_directory
                and directory not in other_season_directories
            )
        )
        if other_season_directories:
            reasons.append(
                f"temporada_en_directorio_distinto_tv:{other_season_directories[0]}:"
                f"{expected_directory}"
            )
            continue
        inside_key = _path_key(inside_series)
        exact = existing_by_case.get(inside_key)
        exact_relative = existing_relative_by_case.get(inside_key)
        source = payload.source_root / Path(*PurePosixPath(entry.source_relpath).parts)
        if exact is not None:
            if exact_relative != inside_series:
                reasons.append(
                    f"ruta_no_canonica_tv:{exact_relative}:{inside_series}"
                )
                continue
            reasons.append(f"colision_existente:{entry.target_relpath}")
            continue
        wanted_episodes = frozenset(entry.episodes)
        overlaps = [
            path
            for path, season, episodes in existing_episodes
            if season == entry.season and bool(episodes & wanted_episodes)
        ]
        if overlaps:
            suffixes = {path.suffix.casefold() for path in overlaps}
            code = "colision_otra_extension" if suffixes != {".mkv"} else "colision_otro_nombre"
            reasons.append(f"{code}:{entry.target_relpath}")
            continue
        pending.append(entry)
    return CollisionPlan(
        final_series_root=final_series,
        pending=tuple(pending),
        satisfied=tuple(satisfied),
        review_reasons=tuple(dict.fromkeys(reasons)),
    )


def _safe_fragment(value: str) -> str:
    return (re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-") or "job")[:48]


def _safe_review_folder_name(value: str) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', " ", str(value or "")).strip(" .")
    text = re.sub(r"[\x00-\x1f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:180].rstrip(" .") or "Series sin clasificar"


def _review_folder_label(manifest: SeriesManifest) -> str:
    series_name = str(manifest.series_name or "").strip()
    entries = tuple(manifest.entries)
    if (
        not series_name
        or not entries
        or any(entry.series_name != series_name for entry in entries)
    ):
        return "Series sin clasificar"
    if len(entries) == 1:
        entry = entries[0]
        episode_code = f"S{entry.season:02d}" + "".join(
            f"E{episode:02d}" for episode in entry.episodes
        )
        return _safe_review_folder_name(f"{series_name} - {episode_code}")
    seasons = sorted({entry.season for entry in entries})
    if len(seasons) == 1:
        pack_label = f"Temporada {seasons[0]:02d}"
    else:
        pack_label = f"Temporadas {seasons[0]:02d}-{seasons[-1]:02d}"
    return _safe_review_folder_name(f"{series_name} - {pack_label}")


def _review_source(prepared: PreparedJob) -> tuple[Path, str, str]:
    """Elige el directorio comun mas cercano sin duplicar Serie/Temporada."""

    manifest = prepared.manifest
    if manifest.ready and manifest.entries:
        parent_parts: list[tuple[str, ...]] = []
        for entry in manifest.entries:
            parent_parts.append(PurePosixPath(entry.source_relpath).parent.parts)
            parent_parts.extend(
                PurePosixPath(sidecar.source_relpath).parent.parts
                for sidecar in entry.subtitle_sidecars
            )
        common = list(parent_parts[0])
        for parts in parent_parts[1:]:
            matching = 0
            for left, right in zip(common, parts):
                if left != right:
                    break
                matching += 1
            common = common[:matching]
            if not common:
                break
        if common:
            common = common[:2]
            prefix = PurePosixPath(*common).as_posix()
            source = prepared.payload.source_root.joinpath(*common)
            if source.is_dir() and not source.is_symlink():
                layout = "season_root" if len(common) >= 2 else "series_root"
                return source, prefix, layout
    return prepared.payload.source_root, "", "source_root"


def _read_review_reason(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((path / "reason.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _review_reason_matches(path: Path, expected: dict[str, Any]) -> bool:
    payload = _read_review_reason(path)
    return bool(
        payload is not None
        and all(payload.get(key) == value for key, value in expected.items())
    )


def _review_destination(
    review_root: Path,
    label: str,
    expected: dict[str, Any],
) -> tuple[Path, bool]:
    base = review_root / label
    for index in range(10000):
        candidate = base if index == 0 else base.with_name(f"{base.name} ({index})")
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_dir() and not candidate.is_symlink() and _review_reason_matches(
                candidate,
                expected,
            ):
                return candidate, True
            continue
        return candidate, False
    fallback = base.with_name(f"{base.name} ({int(time.time())})")
    if fallback.exists() or fallback.is_symlink():
        raise SeriesWorkerError("No hay un nombre libre para la revisión de Series")
    return fallback, False


def _prepared_root(
    job_root: Path,
    series_name: str,
    job_id: str,
    generation: str,
) -> Path:
    del job_id, generation
    return job_root / "series_filebot_output" / series_name


def _legacy_prepared_root(job_root: Path, series_name: str) -> Path:
    """Ruta antigua aceptada únicamente para recuperar trabajos ya persistidos."""

    return job_root / "series_work" / "processed" / series_name


def _finalize_processed_in_place(
    prepared: PreparedJob,
    processing: ProcessingResult | None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Reemplaza cada original de FileBot por su ``.limpio`` ya comprobado."""

    destination_root = prepared.prepared_series_root
    if destination_root is None:
        raise SeriesWorkerError("No existe salida procesada en el taller")
    if destination_root.is_symlink() or not destination_root.is_dir():
        raise SeriesWorkerError("La carpeta de FileBot de la serie no es válida")
    by_source = {
        episode.source_relpath: episode
        for episode in (processing.episodes if processing else ())
    }
    subtitle_suffix = str(
        prepared.rules_snapshot.rules["subtitulos"]["sufijo_srt_externo"]
    )
    expected_files: list[str] = []
    if prepared.collision.satisfied:
        raise ReviewRequiredError("Hay episodios existentes en la biblioteca")
    for entry in prepared.collision.pending:
        episode = by_source.get(entry.source_relpath)
        if episode is None:
            raise SeriesWorkerError(f"Falta salida verificada: {entry.source_relpath}")
        provisional_relative = validate_relative_path(episode.provisional_relpath)
        provisional = prepared.payload.job_root / Path(
            *PurePosixPath(provisional_relative).parts
        )
        expected_provisional = (
            prepared.payload.job_root
            / "series_filebot_output"
            / Path(*PurePosixPath(entry.target_relpath).parts)
        ).with_suffix(".limpio.mkv")
        if provisional != expected_provisional:
            raise SeriesWorkerError(
                f"Salida provisional fuera de su destino: {entry.source_relpath}"
            )
        target_parts = PurePosixPath(entry.target_relpath).parts
        relative = Path(*target_parts[1:])
        destination = destination_root / relative
        original_relative = validate_relative_path(entry.source_relpath)
        original = prepared.payload.source_root / Path(
            *PurePosixPath(original_relative).parts
        )
        if provisional.is_symlink() or not provisional.is_file():
            raise SeriesWorkerError(f"Falta salida limpia: {entry.source_relpath}")
        if provisional.stat().st_size != episode.output_size:
            raise SeriesWorkerError(f"Tamaño de salida inesperado: {entry.source_relpath}")
        consumed_original = (
            str(episode.verification.get("processing_mode") or "") == "metadata_only"
            and not original.exists()
            and not original.is_symlink()
        )
        if not consumed_original and (original.is_symlink() or not original.is_file()):
            raise SeriesWorkerError(f"Falta el original de FileBot: {entry.source_relpath}")
        if destination != original and (destination.exists() or destination.is_symlink()):
            raise SeriesWorkerError(f"Ya existe el destino limpio: {entry.target_relpath}")
        os.replace(provisional, destination)
        if original != destination and not consumed_original:
            original.unlink(missing_ok=True)
        relative_text = relative.as_posix()
        expected_files.append(relative_text)
        clean_stem = provisional.stem.removesuffix(".limpio")
        expected_srt = provisional.with_name(f"{clean_stem}{subtitle_suffix}")
        subtitle_evidence = (episode.subtitle_provisional_relpath, episode.subtitle_size)
        if any(value is not None for value in subtitle_evidence) and not all(
            value is not None for value in subtitle_evidence
        ):
            raise SeriesWorkerError(
                f"Evidencia de sidecar incompleta: {entry.source_relpath}"
            )
        if episode.subtitle_provisional_relpath is not None:
            subtitle_relative = validate_relative_path(
                episode.subtitle_provisional_relpath
            )
            provisional_srt = prepared.payload.job_root / Path(
                *PurePosixPath(subtitle_relative).parts
            )
            if provisional_srt != expected_srt:
                raise SeriesWorkerError(
                    f"Sidecar verificado fuera de su destino: {entry.source_relpath}"
                )
            subtitle_destination = destination.with_name(
                f"{destination.stem}{subtitle_suffix}"
            )
            if provisional_srt.is_symlink() or not provisional_srt.is_file():
                raise SeriesWorkerError(
                    f"Falta sidecar procesado: {entry.source_relpath}"
                )
            if provisional_srt.stat().st_size != episode.subtitle_size:
                raise SeriesWorkerError(
                    f"Tamaño de sidecar inesperado: {entry.source_relpath}"
                )
            subtitle_text = subtitle_destination.relative_to(
                destination_root
            ).as_posix()
            expected_files.append(subtitle_text)
        elif expected_srt.exists() or expected_srt.is_symlink():
            raise SeriesWorkerError(
                f"Apareció un sidecar no verificado: {entry.source_relpath}"
            )
    ordered_files = tuple(sorted(expected_files, key=str.casefold))
    return ordered_files, {}


def _discard_generated_artifacts(prepared: PreparedJob) -> None:
    """Retira solo temporales creados por Series; conserva intacto FileBot."""

    source_root = prepared.payload.source_root
    subtitle_suffix = str(
        prepared.rules_snapshot.rules["subtitulos"]["sufijo_srt_externo"]
    )
    original_sidecars = {
        sidecar.source_relpath
        for entry in prepared.manifest.entries
        for sidecar in entry.subtitle_sidecars
    }
    for entry in prepared.manifest.entries:
        target = source_root / Path(*PurePosixPath(entry.target_relpath).parts)
        clean = target.with_suffix(".limpio.mkv")
        temporary = clean.with_suffix(".procesando.tmp.mkv")
        clean.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        clean_stem = clean.stem.removesuffix(".limpio")
        sidecar = clean.with_name(f"{clean_stem}{subtitle_suffix}")
        sidecar_relative = sidecar.relative_to(source_root).as_posix()
        if sidecar_relative not in original_sidecars:
            sidecar.unlink(missing_ok=True)
        sidecar.with_name(f"{sidecar.stem}.procesando.tmp.srt").unlink(
            missing_ok=True
        )


@dataclass(frozen=True)
class _ReviewContract:
    reason_code: str
    reason_kind: str
    reason_file: str
    reason_title: str
    reason_lines: tuple[str, ...]

    def fields(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "reason_kind": self.reason_kind,
            "reason_file": self.reason_file,
            "reason_title": self.reason_title,
            "reason_lines": list(self.reason_lines),
        }

    def text(self) -> str:
        return "\n".join(self.reason_lines) + "\n"


_REVIEW_TYPES = {
    "series_duplicate": ("duplicate", "Serie repetida.txt", "Serie repetida"),
    "series_audio_invalid": ("audio", "Audio no valido.txt", "Audio no valido"),
    "series_video_invalid": ("video", "Video no valido.txt", "Video no valido"),
    "series_subtitle_not_convertible": (
        "subtitle",
        "Subtitulo no convertible.txt",
        "Subtitulo no convertible",
    ),
    "series_ocr_subtitle_failed": (
        "ocr",
        "OCR subtitulo fallido.txt",
        "OCR subtitulo fallido",
    ),
    "series_manual_review": ("manual", "Revision de serie.txt", "Revision de serie"),
    "series_process_error": ("process", "Error de proceso.txt", "Error de proceso"),
}
_REVIEW_SUMMARIES = {
    "duplicate": "Ya existe en la biblioteca al menos uno de los episodios del pack.",
    "audio": "El pack no cumple las reglas de audio.",
    "video": "El pack no cumple las reglas de vídeo.",
    "subtitle": "El subtítulo no se puede convertir automáticamente.",
    "ocr": "Ha fallado el OCR del subtítulo.",
    "manual": "El pack necesita revisión manual antes de publicarse.",
    "process": "El pack no se ha podido procesar de forma segura.",
}
_DUPLICATE_REASON_CODES = frozenset(
    {"colision_existente", "colision_otra_extension", "colision_otro_nombre"}
)
_PROCESS_REASON_CODES = frozenset({"preparacion_fallida", "procesamiento_fallido"})
_REASON_LABELS = {
    "episodio_no_reconocido": "No se pudo reconocer el episodio",
    "colision_casefold": "El pack contiene rutas equivalentes",
    "episodio_duplicado": "El pack contiene el mismo episodio más de una vez",
    "colision_sidecar_casefold": "El pack contiene subtítulos con rutas equivalentes",
    "archivo_no_clasificado": "Archivo sin clasificar",
    "sin_episodios_validos": "El pack no contiene episodios válidos",
    "varias_series": "El pack contiene varias series",
    "manifest_no_apto": "El manifiesto del pack no es apto",
    "varias_raices_casefold_en_tv": "La biblioteca contiene varias raíces equivalentes",
    "raiz_final_no_es_directorio": "La raíz final de la serie no es un directorio",
    "symlink_en_serie_final": "La raíz final de la serie es un enlace simbólico",
    "colision_directorio_casefold_tv": "La biblioteca contiene directorios equivalentes",
    "entrada_no_regular_en_serie_final": "La biblioteca contiene una entrada no regular",
    "colision_casefold_tv": "La biblioteca contiene rutas equivalentes",
    "target_sin_raiz": "El destino no conserva la raíz de la serie",
    "directorio_no_canonico_tv": "La biblioteca usa un directorio no canónico",
    "temporada_en_directorio_distinto_tv": "La temporada ya existe en otro directorio",
    "ruta_no_canonica_tv": "El episodio ya existe con una ruta no canónica",
    "colision_existente": "Episodio ya existente en la biblioteca",
    "colision_otra_extension": "Episodio ya existente con otra extensión",
    "colision_otro_nombre": "Episodio ya existente con otro nombre",
    "biblioteca_cambio_durante_procesado": "La biblioteca cambió durante el procesado",
    "preparacion_fallida": "Falló la preparación del pack",
}
_REVIEW_OUTPUT_MARKERS = frozenset(
    value[1] for value in _REVIEW_TYPES.values()
)
_REVIEW_MARKERS = frozenset(
    {
        *_REVIEW_OUTPUT_MARKERS,
        "Error de FileBot.txt",
        "Error de extraccion.txt",
        "Pelicula repetida.txt",
        "Revision manual.txt",
    }
)
_REVIEW_METADATA = frozenset({"reason.json", *_REVIEW_MARKERS})


def _reason_code(reason: str) -> str:
    return str(reason).partition(":")[0].strip().casefold()


def _reason_detail(reason: str) -> str:
    raw = re.sub(r"\s+", " ", str(reason)).strip()
    code, separator, detail = raw.partition(":")
    if code in {"procesamiento_fallido", "procesamiento_requiere_revision"}:
        line = detail.strip() or raw
    else:
        label = _REASON_LABELS.get(code.casefold())
        if label is None:
            line = raw
        else:
            line = (
                f"{label}: {detail.strip()}"
                if separator and detail.strip()
                else label
            )
    return line[:1200].strip()


def _review_contract(
    reasons: tuple[str, ...],
    *,
    processing_identity: tuple[str, str] | None = None,
) -> _ReviewContract:
    if (
        not reasons
        or any(
            not isinstance(reason, str)
            or not reason
            or reason != reason.strip()
            or len(reason) > 8192
            or "\x00" in reason
            for reason in reasons
        )
    ):
        raise SeriesWorkerError("Las razones técnicas de Series no son válidas")
    codes = tuple(_reason_code(reason) for reason in reasons)
    if processing_identity is not None:
        reason_code, expected_kind = processing_identity
    elif codes and all(code in _DUPLICATE_REASON_CODES for code in codes):
        reason_code, expected_kind = "series_duplicate", "duplicate"
    elif any(code in _PROCESS_REASON_CODES for code in codes):
        reason_code, expected_kind = "series_process_error", "process"
    else:
        reason_code, expected_kind = "series_manual_review", "manual"
    configured = _REVIEW_TYPES.get(reason_code)
    if configured is None or configured[0] != expected_kind:
        raise SeriesWorkerError("Clasificación de revisión de Series no válida")
    reason_kind, reason_file, reason_title = configured
    details = tuple(_reason_detail(reason) for reason in reasons if str(reason).strip())
    reason_lines = (_REVIEW_SUMMARIES[reason_kind], *details)
    return _ReviewContract(
        reason_code=reason_code,
        reason_kind=reason_kind,
        reason_file=reason_file,
        reason_title=reason_title,
        reason_lines=reason_lines,
    )


def _review_result(
    prepared: PreparedJob,
    destination: Path,
    review_layout: str,
    review_source_prefix: str,
    reasons: tuple[str, ...],
    contract: _ReviewContract | None,
) -> dict[str, Any]:
    result = {
        "status": "review",
        "job_id": prepared.payload.job_id,
        "kind": "series",
        "rules_fingerprint": prepared.rules_snapshot.fingerprint,
        "manifest": prepared.manifest.to_dict(),
        "review_path": destination.relative_to(
            prepared.payload.review_root
        ).as_posix(),
        "review_layout": review_layout,
        "review_source_prefix": review_source_prefix,
        "review_reasons": list(reasons),
        "published": [],
    }
    if contract is not None:
        result.update(contract.fields())
    return result


def _existing_review_contract(
    destination: Path,
    reason: dict[str, Any],
) -> _ReviewContract | None:
    schema = reason.get("schema")
    if schema == "series-review-v1":
        reasons = reason.get("reasons")
        if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
            raise SeriesWorkerError("La revisión v1 conservada tiene motivos inválidos")
        expected = "Serie repetida\n" + "\n".join(reasons) + "\n"
        marker = destination / "Serie repetida.txt"
        if (
            marker.is_symlink()
            or not marker.is_file()
            or marker.read_text(encoding="utf-8") != expected
        ):
            raise SeriesWorkerError("La revisión v1 conservada no supera su contrato")
        existing_markers = {
            path.name
            for path in destination.iterdir()
            if path.name in _REVIEW_MARKERS and (path.exists() or path.is_symlink())
        }
        if existing_markers != {"Serie repetida.txt"}:
            raise SeriesWorkerError("La revisión v1 no conserva un único marcador")
        return None
    if schema != "series-review-v2":
        raise SeriesWorkerError("La revisión conservada usa un esquema desconocido")
    raw_reason_lines = reason.get("reason_lines")
    if not isinstance(raw_reason_lines, list) or not all(
        isinstance(line, str) for line in raw_reason_lines
    ):
        raise SeriesWorkerError("La revisión v2 conservada tiene líneas inválidas")
    try:
        reason_lines = tuple(raw_reason_lines)
        contract = _ReviewContract(
            reason_code=str(reason["reason_code"]),
            reason_kind=str(reason["reason_kind"]),
            reason_file=str(reason["reason_file"]),
            reason_title=str(reason["reason_title"]),
            reason_lines=reason_lines,
        )
    except (KeyError, TypeError) as error:
        raise SeriesWorkerError("La revisión v2 conservada está incompleta") from error
    configured = _REVIEW_TYPES.get(contract.reason_code)
    if (
        configured is None
        or configured != (
            contract.reason_kind,
            contract.reason_file,
            contract.reason_title,
        )
        or not reason_lines
        or not all(isinstance(line, str) and line.strip() for line in reason_lines)
    ):
        raise SeriesWorkerError("La revisión v2 conservada tiene clasificación inválida")
    marker = destination / contract.reason_file
    if (
        marker.is_symlink()
        or not marker.is_file()
        or marker.read_text(encoding="utf-8") != contract.text()
    ):
        raise SeriesWorkerError("La revisión v2 conservada no supera su contrato")
    existing_markers = {
        path.name
        for path in destination.iterdir()
        if path.name in _REVIEW_MARKERS and (path.exists() or path.is_symlink())
    }
    if existing_markers != {contract.reason_file}:
        raise SeriesWorkerError("La revisión v2 no conserva un único marcador")
    return contract


def _review_pack(
    prepared: PreparedJob,
    reasons: tuple[str, ...],
    *,
    processing_identity: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Mueve el pack completo a revisión una sola vez, igual que películas."""

    payload = prepared.payload
    match_reason = {
        "profile": "series",
        "category": "tv",
        "job_id": payload.job_id,
        "manifest_digest": prepared.manifest.digest,
        "reasons": list(reasons),
    }
    contract = _review_contract(reasons, processing_identity=processing_identity)
    expected_text = contract.text()
    source, review_source_prefix, review_layout = _review_source(prepared)
    payload.review_root.mkdir(parents=True, exist_ok=True)
    label = _review_folder_label(prepared.manifest)
    destination, already_moved = _review_destination(
        payload.review_root,
        label,
        match_reason,
    )
    if already_moved:
        existing_reason = _read_review_reason(destination) or {}
        existing_contract = _existing_review_contract(destination, existing_reason)
        if existing_contract is not None and existing_contract != contract:
            raise SeriesWorkerError(
                "La revisión v2 conservada no coincide con el motivo calculado"
            )
        return _review_result(
            prepared,
            destination,
            str(existing_reason.get("review_layout") or review_layout),
            str(existing_reason.get("review_source_prefix") or review_source_prefix),
            reasons,
            existing_contract,
        )
    expected_reason = {
        "schema": "series-review-v2",
        **match_reason,
        **contract.fields(),
        "review_layout": review_layout,
        "review_source_prefix": review_source_prefix,
    }
    reason_path = source / "reason.json"
    text_path = source / contract.reason_file
    existing_metadata: dict[str, bytes] = {}
    retry_reason = _read_review_reason(source)
    legacy_reason = {
        "schema": "series-review-v1",
        **match_reason,
        "review_layout": review_layout,
        "review_source_prefix": review_source_prefix,
    }
    for reserved in _REVIEW_METADATA:
        reserved_path = source / reserved
        if not (reserved_path.exists() or reserved_path.is_symlink()):
            continue
        if reserved_path.is_symlink() or not reserved_path.is_file():
            raise SeriesWorkerError(
                f"El pack usa un nombre reservado para revisión: {reserved}"
            )
        own_retry_metadata = (
            reserved == "reason.json"
            and retry_reason in (expected_reason, legacy_reason)
        ) or (
            retry_reason == expected_reason
            and reserved == contract.reason_file
            and reserved_path.read_text(encoding="utf-8") == expected_text
        ) or (
            retry_reason == legacy_reason
            and reserved == "Serie repetida.txt"
            and reserved_path.read_text(encoding="utf-8")
            == "Serie repetida\n" + "\n".join(reasons) + "\n"
        )
        if not own_retry_metadata:
            raise SeriesWorkerError(
                f"El pack usa un nombre reservado para revisión: {reserved}"
            )
        existing_metadata[reserved] = reserved_path.read_bytes()
    if source.is_symlink() or not source.is_dir():
        raise SeriesWorkerError("El pack de revisión no es una carpeta física")
    try:
        for reserved in existing_metadata:
            (source / reserved).unlink()
        reason_path.write_text(
            json.dumps(expected_reason, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        text_path.write_text(expected_text, encoding="utf-8")
        shutil.move(str(source), str(destination))
    except Exception:
        if source.is_dir() and not source.is_symlink():
            for reserved in _REVIEW_METADATA:
                candidate = source / reserved
                if candidate.is_file() and not candidate.is_symlink():
                    candidate.unlink()
            for reserved, content in existing_metadata.items():
                (source / reserved).write_bytes(content)
        raise
    if payload.source_root.is_dir() and not payload.source_root.is_symlink():
        remaining = tuple(payload.source_root.rglob("*"))
        if not any(path.is_file() or path.is_symlink() for path in remaining):
            shutil.rmtree(payload.source_root, ignore_errors=True)
    return _review_result(
        prepared,
        destination,
        review_layout,
        review_source_prefix,
        reasons,
        contract,
    )


def _published_manifest(prepared: PreparedJob) -> dict[str, Any]:
    series_root = prepared.collision.final_series_root
    if (
        series_root is None
        or series_root.parent != prepared.payload.final_root
        or series_root.is_symlink()
        or not series_root.is_dir()
    ):
        raise SeriesWorkerError("La raíz publicada de la serie no es segura")
    snapshot = prepared.journal.snapshot()
    details = snapshot.get("details", {}) if isinstance(snapshot, dict) else {}
    expected = details.get("expected_files")
    if not isinstance(expected, list) or not expected:
        expected = []
        sidecar_suffixes = {".srt", ".ass", ".ssa", ".sub", ".idx"}
        for manifest_entry in prepared.manifest.entries:
            parts = PurePosixPath(manifest_entry.target_relpath).parts
            if len(parts) < 2:
                raise SeriesWorkerError("El pack publicado contiene un destino inválido")
            relative_video = PurePosixPath(*parts[1:])
            expected.append(relative_video.as_posix())
            parent = series_root.joinpath(*relative_video.parent.parts)
            if parent.is_dir() and not parent.is_symlink():
                prefix = f"{relative_video.stem}.".casefold()
                for sibling in parent.iterdir():
                    if (
                        sibling.is_file()
                        and not sibling.is_symlink()
                        and sibling.name.casefold().startswith(prefix)
                        and sibling.suffix.casefold() in sidecar_suffixes
                    ):
                        expected.append(sibling.relative_to(series_root).as_posix())

    entries: list[dict[str, Any]] = []
    for raw_relative in expected:
        relative = validate_relative_path(raw_relative)
        path = series_root.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink() or not path.is_file():
            raise SeriesWorkerError("Falta un archivo publicado del pack")
        size = path.stat().st_size
        entries.append(
            {
                "path": PurePosixPath(series_root.name, *PurePosixPath(relative).parts).as_posix(),
                "size": size,
                "content_sha256": "",
            }
        )
    entries.sort(key=lambda item: (_path_key(item["path"]), item["path"]))
    if not entries:
        raise SeriesWorkerError("El pack publicado no contiene archivos")
    folded = [_path_key(item["path"]) for item in entries]
    if len(set(folded)) != len(folded):
        raise SeriesWorkerError("La serie publicada contiene rutas equivalentes")
    return {
        "schema": "series-published-manifest-v1",
        "digest": _digest(entries),
        "entries": entries,
    }


def _validate_published_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "digest", "entries"}:
        raise ServiceUnavailable("Manifiesto publicado terminal no válido")
    if value.get("schema") != "series-published-manifest-v1":
        raise ServiceUnavailable("Esquema de manifiesto publicado no soportado")
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ServiceUnavailable("Manifiesto publicado terminal vacío")
    entries: list[dict[str, Any]] = []
    for item in raw_entries:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "size",
            "content_sha256",
        }:
            raise ServiceUnavailable("Entrada de manifiesto publicado no válida")
        path = item.get("path")
        size = item.get("size")
        content_sha256 = item.get("content_sha256")
        if (
            not isinstance(path, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(content_sha256, str)
            or (content_sha256 != "" and not _is_sha256(content_sha256))
        ):
            raise ServiceUnavailable("Entrada de manifiesto publicado no válida")
        try:
            normalized_path = validate_relative_path(path)
        except ManifestError as error:
            raise ServiceUnavailable("Ruta de manifiesto publicado no válida") from error
        normalized_parts = PurePosixPath(normalized_path).parts
        if len(normalized_parts) == 2 and normalized_parts[-1] == MARKER_NAME:
            raise ServiceUnavailable("El manifiesto publicado expone el marcador interno")
        entries.append(
            {
                "path": normalized_path,
                "size": size,
                "content_sha256": content_sha256,
            }
        )
    if entries != sorted(
        entries,
        key=lambda item: (_path_key(item["path"]), item["path"]),
    ):
        raise ServiceUnavailable("Manifiesto publicado fuera de orden")
    folded = [_path_key(item["path"]) for item in entries]
    if len(set(folded)) != len(folded):
        raise ServiceUnavailable("Manifiesto publicado contiene rutas equivalentes")
    digest = value.get("digest")
    if not _is_sha256(digest) or digest != _digest(entries):
        raise ServiceUnavailable("Huella de manifiesto publicado no coincide")
    return {
        "schema": "series-published-manifest-v1",
        "digest": digest,
        "entries": entries,
    }


def _safe_error(error: BaseException) -> str:
    text = str(error).strip() or type(error).__name__
    text = re.sub(r"(?i)(token|password|secret|auth)\s*[:=]\s*\S+", r"\1=<REDACTED>", text)
    text = re.sub(r"(?i)https?://\S+", "<URL>", text)
    text = re.sub(
        r"(?i)(?:[A-Z]:[\\/]|/(?:data|config|tmp|opt|var|home|mnt|volume\d+)/)"
        r"[^\r\n]*?(?=:\s|$)",
        "<PATH>",
        text,
    )
    text = re.sub(
        r"(?i)(?:[A-Z]:[\\/]|/(?:data|config|tmp|opt|var|home|mnt|volume\d+)/)\S*",
        "<PATH>",
        text,
    )
    return re.sub(r"\s+", " ", text)[:500]


def _caused_by(error: BaseException, wanted: type[BaseException]) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, wanted):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _emit_callback(prepared: PreparedJob, phase: str, event_type: str, message: str) -> None:
    callback = prepared.payload.callback_url
    if not callback:
        return
    body = json.dumps(
        {
            "phase": phase,
            "event_type": event_type,
            "message": message,
            "structured": {
                "job_id": prepared.payload.job_id,
                "kind": "series",
                "manifest_digest": prepared.manifest.digest,
                "rules_fingerprint": prepared.rules_snapshot.fingerprint,
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        callback,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2):
            pass
    except Exception:
        pass


class SeriesCoordinator:
    def __init__(
        self,
        *,
        rules_store: RulesStore | None = None,
        processor_factory: Callable[[], SeriesProcessor] = SeriesProcessor,
        publisher: Callable[..., dict[str, Any]] = publish_series,
        recoverer: Callable[[str, Path, Path, DurableJournal], dict[str, Any]] = recover_delivery,
        atomic_preflight: Callable[[Path], dict[str, Any]] = preflight_atomic_exchange,
        tool_checker: Callable[[], list[str]] | None = None,
        lock_factory: Callable[..., ContextManager[Any]] = series_heavy_lock,
        health_clock: Callable[[], float] = time.monotonic,
        health_atomicity_cache_ttl_sec: float = DEFAULT_HEALTH_ATOMICITY_CACHE_TTL_SEC,
    ) -> None:
        self._rules_error: BaseException | None = None
        if rules_store is None:
            try:
                rules_store = RulesStore()
            except Exception as error:
                self._rules_error = error
        self.rules_store = rules_store
        self.processor_factory = processor_factory
        self.publisher = publisher
        self.recoverer = recoverer
        # Se conserva el argumento por compatibilidad con clientes antiguos,
        # pero el flujo rápido ya no crea ni ejecuta probes de intercambio.
        self.atomic_preflight = atomic_preflight
        self.tool_checker = tool_checker or (
            lambda: unavailable_tools(
                names=(*BASE_TOOLS, *OCR_TOOLS),
                timeout=3,
                parallel=True,
            )
        )
        self.lock_factory = lock_factory
        self.health_clock = health_clock
        self.health_atomicity_cache_ttl_sec = max(
            0.0,
            float(health_atomicity_cache_ttl_sec),
        )
        self.lock_path = Path(
            str(
                os.environ.get("SERIES_WORKER_LOCK_PATH")
                or os.environ.get("SERIES_HEAVY_LOCK_PATH")
                or DEFAULT_LOCK_PATH
            )
        )
        self._mutex = threading.RLock()
        self._active: dict[str, Any] | None = None
        self._threads: dict[str, threading.Thread] = {}
        self._last_errors: dict[str, str] = {}

    def rules_payload(self) -> dict[str, Any]:
        if self.rules_store is None:
            raise ServiceUnavailable(
                "Reglas no disponibles: " + _safe_error(self._rules_error or RuntimeError())
            )
        return self.rules_store.payload()

    def save_rules(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.rules_store is None:
            raise ServiceUnavailable(
                "Reglas no disponibles: " + _safe_error(self._rules_error or RuntimeError())
            )
        try:
            return self.rules_store.save(payload)
        except OSError as error:
            raise ServiceUnavailable("No se pudieron persistir las reglas") from error

    def health(self) -> Submission:
        checks: dict[str, Any] = {
            "rules": {"ok": False},
            "tools": {"ok": False},
            "atomicity": {"ok": False, "mode": "direct_move"},
        }
        errors: list[str] = []
        try:
            rules = self.rules_payload()
            checks["rules"] = {"ok": True, "fingerprint": rules["fingerprint"]}
        except Exception as error:
            errors.append("rules:" + _safe_error(error))
        try:
            missing = self.tool_checker()
            checks["tools"] = {"ok": not missing, "missing": missing}
            if missing:
                errors.append("tools:" + ",".join(missing))
        except Exception as error:
            errors.append("tools:" + _safe_error(error))
        try:
            final_root = _configured_path("SERIES_WORKER_FINAL_ROOT", DEFAULT_FINAL_ROOT)
            if not final_root.is_dir():
                raise NotADirectoryError(str(final_root))
            checks["atomicity"] = {
                "ok": True,
                "mode": "direct_move",
                "verified": False,
            }
        except Exception as error:
            errors.append("atomicity:" + _safe_error(error))
        ok = not errors
        return Submission(
            200 if ok else 503,
            {
                "ok": ok,
                "status": "ok" if ok else "unavailable",
                "service": "series-worker",
                "checks": checks,
                "errors": errors,
            },
        )

    def _request_identity(
        self,
        validated: ValidatedPayload,
        existing: dict[str, Any],
    ) -> tuple[ValidatedPayload, str, str, str, str]:
        schema = existing.get("schema")
        if schema not in {"series-worker-request-v1", "series-worker-request-v2"}:
            raise ServiceUnavailable("request.json durable no es válido")
        stage = "prepared" if schema == "series-worker-request-v1" else existing.get("stage")
        if stage not in {"reserved", "preparing", "prepared"}:
            raise ServiceUnavailable("request.json durable no conserva una fase válida")
        common_keys = {
            "schema",
            "payload",
            "payload_digest",
            "rules_fingerprint",
            "generation",
        }
        if schema == "series-worker-request-v1":
            required_keys = common_keys | {
                "manifest_digest",
                "request_digest",
                "prepared_series_root",
            }
            allowed_keys = required_keys | {"atomic_preflight", "publication_preflight"}
        elif stage == "prepared":
            required_keys = common_keys | {
                "stage",
                "manifest_digest",
                "request_digest",
                "prepared_series_root",
            }
            allowed_keys = required_keys | {
                "atomic_preflight",
                "publication_preflight",
                "collision_plan",
            }
        else:
            required_keys = common_keys | {"stage"}
            allowed_keys = required_keys | {"atomic_preflight", "publication_preflight"}
        if not required_keys.issubset(existing) or not set(existing).issubset(allowed_keys):
            raise ServiceUnavailable("request.json durable tiene una estructura inválida")
        stored_payload = existing.get("payload")
        if not isinstance(stored_payload, dict):
            raise ServiceUnavailable("request.json durable no conserva el payload")
        try:
            persisted_validated = _validated_payload(
                stored_payload,
                require_directories=False,
                allow_legacy_review_root=True,
            )
        except RequestValidationError as error:
            raise ServiceUnavailable("request.json durable contiene rutas no válidas") from error
        persisted_payload = persisted_validated.persisted()
        payload_digest = _digest(persisted_payload)
        if existing.get("payload_digest") != payload_digest:
            raise ServiceUnavailable("request.json durable no coincide con su payload")
        if validated.persisted() != persisted_payload:
            raise JobConflict("job_id ya está ligado a otro payload")
        rules_fingerprint_value = existing.get("rules_fingerprint")
        if not _is_sha256(rules_fingerprint_value):
            raise ServiceUnavailable("request.json durable no conserva reglas válidas")
        generation = existing.get("generation")
        if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
            raise ServiceUnavailable("El job durable no tiene una generación válida")
        return (
            persisted_validated,
            payload_digest,
            rules_fingerprint_value,
            generation,
            stage,
        )

    def _new_reservation(
        self,
        validated: ValidatedPayload,
    ) -> tuple[ReservedJob, dict[str, Any]]:
        if self.rules_store is None:
            raise ServiceUnavailable("Las reglas de series no son válidas")
        rules_snapshot = self.rules_store.snapshot()
        persisted_payload = validated.persisted()
        payload_digest = _digest(persisted_payload)
        generation = uuid.uuid4().hex
        journal = DurableJournal(validated.reports_root / validated.job_id)
        if journal.snapshot() is not None or _read_json(journal.job_dir / RESULT_FILE) is not None:
            raise ServiceUnavailable("Job durable sin request.json commit marker")
        record = {
            "schema": "series-worker-request-v2",
            "stage": "reserved",
            "payload": persisted_payload,
            "payload_digest": payload_digest,
            "rules_fingerprint": rules_snapshot.fingerprint,
            "generation": generation,
        }
        journal.write_json_atomic(
            RULES_SNAPSHOT_FILE,
            {
                "rules": rules_snapshot.rules,
                "fingerprint": rules_snapshot.fingerprint,
            },
        )
        journal.write_json_atomic(REQUEST_FILE, record)
        return (
            ReservedJob(
                payload=validated,
                rules_snapshot=rules_snapshot,
                payload_digest=payload_digest,
                generation=generation,
                journal=journal,
            ),
            record,
        )

    def _reservation_from_record(
        self,
        validated: ValidatedPayload,
        existing: dict[str, Any],
    ) -> tuple[ReservedJob, dict[str, Any]]:
        (
            persisted_validated,
            payload_digest,
            rules_fingerprint_value,
            generation,
            stage,
        ) = self._request_identity(validated, existing)
        if existing.get("schema") != "series-worker-request-v2" or stage == "prepared":
            raise ServiceUnavailable("El job durable ya no es una reserva")
        rules_payload = _read_json(
            persisted_validated.reports_root
            / persisted_validated.job_id
            / RULES_SNAPSHOT_FILE
        )
        if rules_payload is None:
            raise ServiceUnavailable("Reserva durable sin snapshot de reglas")
        rules_snapshot = _rules_from_dict(rules_payload)
        if rules_snapshot.fingerprint != rules_fingerprint_value:
            raise ServiceUnavailable("Reserva durable contradice sus reglas")
        return (
            ReservedJob(
                payload=persisted_validated,
                rules_snapshot=rules_snapshot,
                payload_digest=payload_digest,
                generation=generation,
                journal=DurableJournal(
                    persisted_validated.reports_root / persisted_validated.job_id
                ),
            ),
            existing,
        )

    def _prepare_reserved(
        self,
        reserved: ReservedJob,
        request_record: dict[str, Any],
    ) -> PreparedJob:
        current = _read_json(reserved.journal.job_dir / REQUEST_FILE)
        if current is None:
            raise ServiceUnavailable("La reserva durable desapareció")
        _, current = self._reservation_from_record(reserved.payload, current)
        stage = str(current["stage"])
        trust_existing_manifest = stage == "preparing"
        if stage == "reserved":
            current = {**current, "stage": "preparing"}
            reserved.journal.write_json_atomic(REQUEST_FILE, current)
        persisted_manifest = (
            _read_json(reserved.journal.job_dir / MANIFEST_FILE)
            if trust_existing_manifest
            else None
        )
        if persisted_manifest is None:
            try:
                manifest = discover_manifest(
                    reserved.payload.source_root,
                    reserved.rules_snapshot,
                )
            except ManifestError as error:
                raise RequestValidationError(_safe_error(error)) from error
            reserved.journal.write_json_atomic(MANIFEST_FILE, manifest.to_dict())
        else:
            manifest = _manifest_from_dict(persisted_manifest)
        collision = plan_collisions(reserved.payload, manifest)
        request_digest = _digest(
            {
                "payload_digest": reserved.payload_digest,
                "manifest_digest": manifest.digest,
                "rules_fingerprint": reserved.rules_snapshot.fingerprint,
            }
        )
        prepared_root = None
        if manifest.ready and manifest.series_name:
            prepared_root = _prepared_root(
                reserved.payload.job_root,
                collision.final_series_root.name
                if collision.final_series_root is not None
                else manifest.series_name,
                reserved.payload.job_id,
                reserved.generation,
            )
        prepared = PreparedJob(
            payload=reserved.payload,
            manifest=manifest,
            rules_snapshot=reserved.rules_snapshot,
            payload_digest=reserved.payload_digest,
            request_digest=request_digest,
            journal=reserved.journal,
            collision=collision,
            generation=reserved.generation,
            prepared_series_root=prepared_root,
        )
        prepared_record = {
            **current,
            "stage": "prepared",
            "manifest_digest": manifest.digest,
            "request_digest": request_digest,
            "prepared_series_root": str(prepared_root) if prepared_root else "",
            "collision_plan": _collision_to_dict(collision),
        }
        reserved.journal.write_json_atomic(REQUEST_FILE, prepared_record)
        self._transition_prepared(prepared)
        return prepared

    def _operational_preflight(self, final_root: Path) -> dict[str, Any]:
        try:
            missing = self.tool_checker()
        except Exception as error:
            raise ServiceUnavailable("No se pudieron validar las herramientas") from error
        if missing:
            raise ServiceUnavailable("Faltan herramientas: " + ", ".join(missing))
        try:
            if not final_root.is_dir():
                raise NotADirectoryError(str(final_root))
            current_device = int(final_root.stat().st_dev)
        except Exception as error:
            raise ServiceUnavailable("Destino de Series no disponible") from error
        return {
            "operation": "direct_move",
            "supported": True,
            "st_dev": current_device,
        }

    def _preflight_prepared(
        self,
        prepared: PreparedJob,
        atomic_result: dict[str, Any] | None = None,
    ) -> None:
        if atomic_result is None:
            atomic_result = self._operational_preflight(prepared.payload.final_root)
        current_device = int(prepared.payload.final_root.stat().st_dev)
        if int(atomic_result.get("st_dev", -1)) != current_device:
            raise ServiceUnavailable("El destino cambió de dispositivo")
        request_record = _read_json(prepared.journal.job_dir / REQUEST_FILE)
        if request_record is None:
            raise ServiceUnavailable("request.json durable desapareció durante preflight")
        prepared.journal.write_json_atomic(
            REQUEST_FILE,
            {**request_record, "publication_preflight": atomic_result},
        )

    def _preparation_failure_job(
        self,
        validated: ValidatedPayload,
        request_record: dict[str, Any],
        error: BaseException,
    ) -> PreparedJob:
        (
            persisted,
            payload_digest,
            rules_fingerprint_value,
            generation,
            _stage,
        ) = self._request_identity(validated, request_record)
        reason = "preparacion_fallida:" + _safe_error(error)
        reasons = (reason,)
        manifest = SeriesManifest(
            status="review",
            digest=_digest({"entries": [], "review_reasons": sorted(reasons)}),
            entries=(),
            series_name=None,
            series_key=None,
            review_reasons=reasons,
        )
        rules_snapshot_payload = _read_json(
            persisted.reports_root / persisted.job_id / RULES_SNAPSHOT_FILE
        )
        try:
            rules_snapshot = (
                _rules_from_dict(rules_snapshot_payload)
                if rules_snapshot_payload is not None
                else None
            )
        except ServiceUnavailable:
            rules_snapshot = None
        if rules_snapshot is None or rules_snapshot.fingerprint != rules_fingerprint_value:
            rules_snapshot = RulesSnapshot(
                rules={},
                fingerprint=rules_fingerprint_value,
            )
        request_digest = _digest(
            {
                "payload_digest": payload_digest,
                "manifest_digest": manifest.digest,
                "rules_fingerprint": rules_snapshot.fingerprint,
            }
        )
        journal = DurableJournal(persisted.reports_root / persisted.job_id)
        prepared = PreparedJob(
            payload=persisted,
            manifest=manifest,
            rules_snapshot=rules_snapshot,
            payload_digest=payload_digest,
            request_digest=request_digest,
            journal=journal,
            collision=CollisionPlan(None, (), (), ()),
            generation=generation,
            prepared_series_root=None,
        )
        journal.write_json_atomic(MANIFEST_FILE, manifest.to_dict())
        journal.write_json_atomic(
            REQUEST_FILE,
            {
                "schema": "series-worker-request-v2",
                "stage": "prepared",
                "payload": persisted.persisted(),
                "payload_digest": payload_digest,
                "rules_fingerprint": rules_snapshot.fingerprint,
                "generation": generation,
                "manifest_digest": manifest.digest,
                "request_digest": request_digest,
                "prepared_series_root": "",
                "collision_plan": _collision_to_dict(prepared.collision),
            },
        )
        self._transition_prepared(prepared)
        return prepared

    def _finish_preparation_failure(
        self,
        prepared: PreparedJob,
        error: BaseException,
    ) -> dict[str, Any]:
        reasons = ("preparacion_fallida:" + _safe_error(error),)
        try:
            result = _review_pack(prepared, reasons)
        except Exception as review_error:
            result = self._failed_result(prepared, review_error)
        result = self._finish_without_publish(prepared, result)
        _emit_callback(
            prepared,
            "series_review",
            "finished" if result["status"] == "review" else "error",
            (
                "Preparación fallida; pack enviado a revisión"
                if result["status"] == "review"
                else "Falló la revisión tras preparar el job"
            ),
        )
        return result

    def _existing_candidate(
        self,
        validated: ValidatedPayload,
        existing: dict[str, Any],
    ) -> tuple[PreparedJob, bool, dict[str, Any]]:
        (
            persisted_validated,
            payload_digest,
            rules_fingerprint_value,
            generation,
            stage,
        ) = self._request_identity(validated, existing)
        if stage != "prepared":
            raise ServiceUnavailable("El job durable todavía no está preparado")

        job_dir = persisted_validated.reports_root / persisted_validated.job_id
        journal = DurableJournal(job_dir)
        persisted_manifest = _read_json(job_dir / MANIFEST_FILE)
        persisted_rules = _read_json(job_dir / RULES_SNAPSHOT_FILE)
        if persisted_manifest is None or persisted_rules is None:
            raise ServiceUnavailable("Estado durable del job incompleto")
        manifest = _manifest_from_dict(persisted_manifest)
        rules_snapshot = _rules_from_dict(persisted_rules)
        request_digest = _digest(
            {
                "payload_digest": payload_digest,
                "manifest_digest": manifest.digest,
                "rules_fingerprint": rules_snapshot.fingerprint,
            }
        )
        if (
            existing.get("manifest_digest") != manifest.digest
            or existing.get("rules_fingerprint") != rules_snapshot.fingerprint
            or existing.get("request_digest") != request_digest
        ):
            raise ServiceUnavailable("request.json durable contradice sus snapshots")
        if rules_snapshot.fingerprint != rules_fingerprint_value:
            raise ServiceUnavailable("request.json durable contradice sus reglas")
        persisted_collision = existing.get("collision_plan")
        collision = (
            _collision_from_dict(
                persisted_collision,
                persisted_validated,
                manifest,
            )
            if persisted_collision is not None
            else plan_collisions(persisted_validated, manifest)
        )
        prepared_value = existing.get("prepared_series_root")
        if not isinstance(prepared_value, str):
            raise ServiceUnavailable("request.json durable no conserva el staging")
        prepared_root = Path(prepared_value).resolve(strict=False) if prepared_value else None
        if prepared_root is not None:
            series_name = (
                collision.final_series_root.name
                if collision.final_series_root is not None
                else manifest.series_name
            )
            if not series_name:
                raise ServiceUnavailable("El staging durable no tiene identidad de serie")
            allowed_prepared_roots = {
                _prepared_root(
                    persisted_validated.job_root,
                    series_name,
                    persisted_validated.job_id,
                    generation,
                ),
                _legacy_prepared_root(
                    persisted_validated.job_root,
                    series_name,
                ),
            }
            if prepared_root not in allowed_prepared_roots:
                raise ServiceUnavailable("El staging durable no coincide con el job")
        return (
            PreparedJob(
                payload=persisted_validated,
                manifest=manifest,
                rules_snapshot=rules_snapshot,
                payload_digest=payload_digest,
                request_digest=request_digest,
                journal=journal,
                collision=collision,
                generation=generation,
                prepared_series_root=prepared_root,
            ),
            True,
            existing,
        )

    def _candidate(self, validated: ValidatedPayload) -> tuple[PreparedJob, bool, dict[str, Any]]:
        job_dir = validated.reports_root / validated.job_id
        existing = _read_json(job_dir / REQUEST_FILE)
        if existing is not None:
            return self._existing_candidate(validated, existing)
        journal = DurableJournal(job_dir)
        if journal.snapshot() is not None or _read_json(job_dir / RESULT_FILE) is not None:
            raise ServiceUnavailable("Job durable sin request.json commit marker")
        if self.rules_store is None:
            raise ServiceUnavailable("Las reglas de series no son válidas")
        rules_snapshot = self.rules_store.snapshot()
        try:
            manifest = discover_manifest(validated.source_root, rules_snapshot)
        except ManifestError as error:
            raise RequestValidationError(_safe_error(error)) from error
        collision = plan_collisions(validated, manifest)
        persisted_payload = validated.persisted()
        payload_digest = _digest(persisted_payload)
        request_digest = _digest(
            {
                "payload_digest": payload_digest,
                "manifest_digest": manifest.digest,
                "rules_fingerprint": rules_snapshot.fingerprint,
            }
        )
        generation = uuid.uuid4().hex
        prepared_root = None
        if manifest.ready and manifest.series_name:
            prepared_root = _prepared_root(
                validated.job_root,
                collision.final_series_root.name
                if collision.final_series_root is not None
                else manifest.series_name,
                validated.job_id,
                generation,
            )
        request_record = {
            "schema": "series-worker-request-v1",
            "payload": persisted_payload,
            "payload_digest": payload_digest,
            "manifest_digest": manifest.digest,
            "rules_fingerprint": rules_snapshot.fingerprint,
            "request_digest": request_digest,
            "generation": generation,
            "prepared_series_root": str(prepared_root) if prepared_root else "",
        }
        return (
            PreparedJob(
                payload=validated,
                manifest=manifest,
                rules_snapshot=rules_snapshot,
                payload_digest=payload_digest,
                request_digest=request_digest,
                journal=journal,
                collision=collision,
                generation=generation,
                prepared_series_root=prepared_root,
            ),
            False,
            request_record,
        )

    def _persist_prepared(
        self,
        prepared: PreparedJob,
        request_record: dict[str, Any],
    ) -> None:
        journal = prepared.journal
        journal.write_json_atomic(MANIFEST_FILE, prepared.manifest.to_dict())
        journal.write_json_atomic(
            RULES_SNAPSHOT_FILE,
            {
                "rules": prepared.rules_snapshot.rules,
                "fingerprint": prepared.rules_snapshot.fingerprint,
            },
        )
        journal.write_json_atomic(REQUEST_FILE, request_record)
        self._transition_prepared(prepared)

    def _transition_prepared(self, prepared: PreparedJob) -> None:
        journal = prepared.journal
        if journal.state is not None:
            return
        mode = "review"
        details: dict[str, Any] = {
            "job_id": prepared.payload.job_id,
            "generation": prepared.generation,
            "payload_digest": prepared.payload_digest,
            "manifest_digest": prepared.manifest.digest,
            "rules_fingerprint": prepared.rules_snapshot.fingerprint,
            "mode": mode,
        }
        if prepared.prepared_series_root is not None and prepared.collision.final_series_root is not None:
            mode = "direct_move"
            details.update(
                {
                    "mode": mode,
                    "prepared_series_root": str(prepared.prepared_series_root.resolve(strict=False)),
                    "final_series_root": str(prepared.collision.final_series_root.resolve(strict=False)),
                }
            )
        journal.transition("PREPARED", **details)

    def _terminal_from_job(
        self,
        journal: DurableJournal,
        job_id: str,
        *,
        prepared: PreparedJob | None = None,
    ) -> dict[str, Any] | None:
        result = _read_json(journal.job_dir / RESULT_FILE)
        try:
            snapshot = journal.snapshot()
        except JournalContradiction as error:
            raise ServiceUnavailable("Journal durable contradictorio") from error
        if snapshot is not None and snapshot["state"] == "ROLLED_BACK":
            journal_result = snapshot["details"].get("terminal_result")
            if journal_result is not None and not isinstance(journal_result, dict):
                raise ServiceUnavailable("Resultado terminal inválido en el journal")
            if result is None and isinstance(journal_result, dict):
                journal.write_json_atomic(RESULT_FILE, journal_result)
                result = journal_result
            elif result is not None and journal_result is not None and result != journal_result:
                raise ServiceUnavailable("Resultado y journal durable se contradicen")
        if result is None:
            return None
        status = result.get("status")
        expected_state = {
            "done": "COMMITTED",
            "review": "ROLLED_BACK",
            "failed": "ROLLED_BACK",
        }.get(status)
        if expected_state is None:
            raise ServiceUnavailable("series_result.json tiene un estado inválido")
        if result.get("job_id") != job_id or result.get("kind") != "series":
            raise ServiceUnavailable("series_result.json no pertenece al job")
        if snapshot is None:
            raise ServiceUnavailable("Resultado terminal sin journal durable")
        if snapshot["state"] != expected_state:
            if snapshot["state"] in {"PREPARED", "PROCESSING", "VERIFIED", "COMMITTING"}:
                return None
            raise ServiceUnavailable("Resultado terminal contradice el journal durable")
        if status == "done" and bool(
            (result.get("delivery") or {}).get("cleanup_pending")
        ):
            return None
        if status == "done":
            published_manifest = result.get("published_manifest")
            if published_manifest is None:
                if prepared is None:
                    return None
                result = {**result, "published_manifest": _published_manifest(prepared)}
                snapshot = journal.transition(
                    "COMMITTED",
                    published_manifest_digest=result["published_manifest"]["digest"],
                    published_manifest_entries=len(
                        result["published_manifest"]["entries"]
                    ),
                )
                journal.write_json_atomic(RESULT_FILE, result)
            else:
                normalized_manifest = _validate_published_manifest(published_manifest)
                if normalized_manifest != published_manifest:
                    raise ServiceUnavailable("Manifiesto publicado no canónico")
                details = snapshot["details"]
                bound_digest = details.get("published_manifest_digest")
                bound_entries = details.get("published_manifest_entries")
                if bound_digest is None and bound_entries is None:
                    if prepared is None:
                        return None
                    snapshot = journal.transition(
                        "COMMITTED",
                        published_manifest_digest=normalized_manifest["digest"],
                        published_manifest_entries=len(normalized_manifest["entries"]),
                    )
                elif (
                    bound_digest != normalized_manifest["digest"]
                    or bound_entries != len(normalized_manifest["entries"])
                ):
                    raise ServiceUnavailable(
                        "Manifiesto publicado contradice el journal durable"
                    )
            published = result.get("published")
            if (
                not isinstance(published, list)
                or any(
                    not isinstance(path, str)
                    or not path.casefold().endswith(".mkv")
                    for path in published
                )
            ):
                raise ServiceUnavailable("Lista de MKV publicados no válida")
            manifested = {
                item["path"] for item in result["published_manifest"]["entries"]
            }
            if not set(published).issubset(manifested):
                raise ServiceUnavailable("Los MKV del pack no están en el manifiesto final")
        return result

    def _terminal(self, prepared: PreparedJob) -> dict[str, Any] | None:
        return self._terminal_from_job(
            prepared.journal,
            prepared.payload.job_id,
            prepared=prepared,
        )

    def _committed_recovery_only(
        self,
        journal: DurableJournal,
    ) -> bool:
        try:
            snapshot = journal.snapshot()
        except JournalContradiction as error:
            raise ServiceUnavailable("Journal durable contradictorio") from error
        return snapshot is not None and snapshot["state"] == "COMMITTED"

    def submit(self, raw_payload: Any) -> Submission:
        replay_validated = _validated_payload(
            raw_payload,
            require_directories=False,
            allow_legacy_review_root=True,
        )
        with self._mutex:
            operational_preflight: dict[str, Any] | None = None
            committed_recovery_only = False
            incoming_payload_digest = _digest(replay_validated.persisted())
            if self._active is not None:
                if self._active["job_id"] != replay_validated.job_id:
                    raise SeriesWorkerBusy("Otro job de series está activo")
                if self._active["payload_digest"] != incoming_payload_digest:
                    raise JobConflict("job_id activo con otro payload")
                return Submission(202, self._active_response(self._active))
            job_dir = replay_validated.reports_root / replay_validated.job_id
            existing_record = _read_json(job_dir / REQUEST_FILE)
            if existing_record is not None:
                (
                    validated,
                    payload_digest,
                    rules_fingerprint_value,
                    _generation,
                    _stage,
                ) = self._request_identity(
                    replay_validated,
                    existing_record,
                )
                journal = DurableJournal(job_dir)
                terminal = self._terminal_from_job(journal, validated.job_id)
                if terminal is not None:
                    return Submission(
                        200,
                        self._terminal_response(validated.job_id, terminal),
                    )
                committed_recovery_only = self._committed_recovery_only(journal)
                existed = True
            else:
                validated = validate_payload(raw_payload)
                operational_preflight = self._operational_preflight(
                    validated.final_root
                )
                reserved, existing_record = self._new_reservation(validated)
                payload_digest = reserved.payload_digest
                rules_fingerprint_value = reserved.rules_snapshot.fingerprint
                existed = False
            if existing_record is not None and existed and not committed_recovery_only:
                operational_preflight = self._operational_preflight(
                    validated.final_root
                )
            if committed_recovery_only:
                lock_context = nullcontext(
                    {"enabled": False, "reason": "committed_recovery"}
                )
                lock_context.__enter__()
            else:
                lock_context = self.lock_factory(
                    self.lock_path,
                    timeout_sec=0,
                )
                try:
                    lock_context.__enter__()
                except HeavyLockTimeout as error:
                    raise SeriesWorkerBusy(
                        "El motor audiovisual compartido está ocupado"
                    ) from error
            try:
                active_record = {
                    "job_id": validated.job_id,
                    "payload_digest": payload_digest,
                    "rules_fingerprint": rules_fingerprint_value,
                    "started_at": time.time(),
                }
                self._last_errors.pop(validated.job_id, None)
                self._active = active_record
                thread = threading.Thread(
                    target=self._background,
                    args=(validated, lock_context, operational_preflight),
                    name=f"series-worker-{validated.job_id}",
                    daemon=True,
                )
                self._threads[validated.job_id] = thread
                thread.start()
            except Exception:
                self._active = None
                self._threads.pop(validated.job_id, None)
                lock_context.__exit__(None, None, None)
                raise
            response = (
                self._active_response(active_record)
                if existed
                else self._accepted_response(active_record)
            )
            return Submission(202, response)

    def _accepted_response(self, active: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "accepted",
            "job_id": active["job_id"],
            "kind": "series",
            "rules_fingerprint": active["rules_fingerprint"],
            "started_at": active["started_at"],
        }

    def _active_response(self, active: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "active",
            "job_id": active["job_id"],
            "kind": "series",
            "rules_fingerprint": active["rules_fingerprint"],
            "started_at": active["started_at"],
        }

    def _terminal_response(
        self, job_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "terminal",
            "job_id": job_id,
            "kind": "series",
            "result": result,
        }

    def _background(
        self,
        validated: ValidatedPayload,
        lock_context: ContextManager[Any],
        operational_preflight: dict[str, Any] | None,
    ) -> None:
        gate_entered = True
        processing_context: ContextManager[Any] | None = None
        processing_lock_entered = False

        def release_gate() -> None:
            nonlocal gate_entered
            if not gate_entered:
                return
            lock_context.__exit__(None, None, None)
            gate_entered = False

        def release_heavy_lock() -> None:
            nonlocal processing_lock_entered
            if not processing_lock_entered or processing_context is None:
                return
            processing_context.__exit__(None, None, None)
            processing_lock_entered = False

        def acquire_heavy_lock() -> None:
            nonlocal processing_context, processing_lock_entered
            if processing_lock_entered:
                return
            processing_context = self.lock_factory(
                self.lock_path,
                timeout_sec=0,
            )
            try:
                processing_context.__enter__()
            except HeavyLockTimeout as error:
                processing_context = None
                raise SeriesWorkerBusy(
                    "El motor audiovisual compartido está ocupado"
                ) from error
            processing_lock_entered = True

        try:
            # El primer flock es solo un gate no bloqueante: conserva la cola
            # de uno en uno y se libera antes de preparar el trabajo.
            release_gate()
            request_record = _read_json(
                validated.reports_root / validated.job_id / REQUEST_FILE
            )
            if request_record is None:
                raise ServiceUnavailable("La reserva durable desapareció")
            _persisted, _digest_value, _fingerprint, _generation, stage = (
                self._request_identity(validated, request_record)
            )
            try:
                if (
                    request_record.get("schema") == "series-worker-request-v2"
                    and stage in {"reserved", "preparing"}
                ):
                    reserved, request_record = self._reservation_from_record(
                        validated,
                        request_record,
                    )
                    prepared = self._prepare_reserved(reserved, request_record)
                else:
                    prepared, _existed, _request_record = self._existing_candidate(
                        validated,
                        request_record,
                    )
                    if prepared.journal.state is None:
                        self._transition_prepared(prepared)
            except Exception as preparation_error:
                prepared = self._preparation_failure_job(
                    validated,
                    request_record,
                    preparation_error,
                )
                self._finish_preparation_failure(prepared, preparation_error)
                return

            state = prepared.journal.state
            ready_for_work = (
                not prepared.manifest.review_reasons
                and not prepared.collision.review_reasons
                and state in {"PREPARED", "PROCESSING"}
            )
            if ready_for_work:
                self._preflight_prepared(prepared, operational_preflight)
            self._run_job(
                prepared,
                acquire_heavy_lock=acquire_heavy_lock,
                release_heavy_lock=release_heavy_lock,
            )
        except SeriesWorkerBusy:
            # PREPARED ya es durable; el siguiente POST/poll reintenta sin
            # redescubrir ni rehashear el manifiesto.
            pass
        except Exception as error:
            with self._mutex:
                self._last_errors[validated.job_id] = _safe_error(error)
        finally:
            if processing_lock_entered:
                try:
                    release_heavy_lock()
                except Exception:
                    pass
            if gate_entered:
                try:
                    release_gate()
                except Exception:
                    pass
            with self._mutex:
                if self._active and self._active["job_id"] == validated.job_id:
                    self._active = None
                self._threads.pop(validated.job_id, None)

    def _write_result(self, prepared: PreparedJob, result: dict[str, Any]) -> None:
        prepared.journal.write_json_atomic(RESULT_FILE, result)

    def _complete_rolled_back_cleanup(self, prepared: PreparedJob) -> None:
        if (
            prepared.prepared_series_root is None
            or prepared.collision.final_series_root is None
        ):
            return
        _discard_generated_artifacts(prepared)
        recovered = self.recoverer(
            prepared.payload.job_id,
            prepared.prepared_series_root,
            prepared.collision.final_series_root,
            prepared.journal,
        )
        if recovered.get("status") != "rolled_back":
            raise RecoveryAmbiguous("Rollback durable no terminó su limpieza")

    def _finish_without_publish(
        self,
        prepared: PreparedJob,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        state = prepared.journal.state
        if state == "ROLLED_BACK":
            self._complete_rolled_back_cleanup(prepared)
        if state not in {"COMMITTED", "ROLLED_BACK", "COMMITTING"}:
            prepared.journal.transition(
                "ROLLED_BACK",
                terminal_status=result["status"],
                result_file=RESULT_FILE,
                terminal_result=result,
            )
        elif state == "ROLLED_BACK":
            prepared.journal.transition(
                "ROLLED_BACK",
                terminal_status=result["status"],
                result_file=RESULT_FILE,
                terminal_result=result,
            )
        else:
            raise RecoveryAmbiguous(
                f"No se puede guardar {result['status']} desde {state}"
            )
        self._write_result(prepared, result)
        return result

    def _done_result(
        self,
        prepared: PreparedJob,
        delivery_result: dict[str, Any],
        *,
        recovered: bool,
    ) -> dict[str, Any]:
        final_root = prepared.collision.final_series_root
        if final_root is None:
            raise SeriesWorkerError("La publicación terminal no conserva raíz de serie")

        def final_relpath(entry: ManifestEntry) -> str:
            parts = PurePosixPath(entry.target_relpath).parts
            return PurePosixPath(final_root.name, *parts[1:]).as_posix()

        published = [final_relpath(entry) for entry in prepared.manifest.entries]
        satisfied = [final_relpath(entry) for entry in prepared.collision.satisfied]
        published_manifest = _published_manifest(prepared)
        prepared.journal.transition(
            "COMMITTED",
            published_manifest_digest=published_manifest["digest"],
            published_manifest_entries=len(published_manifest["entries"]),
        )
        return {
            "status": "done",
            "job_id": prepared.payload.job_id,
            "kind": "series",
            "rules_fingerprint": prepared.rules_snapshot.fingerprint,
            "manifest": prepared.manifest.to_dict(),
            "published": published,
            "published_manifest": published_manifest,
            "satisfied": satisfied,
            "series_root": final_root.name if final_root else prepared.manifest.series_name,
            "review_path": "",
            "delivery": {
                "mode": delivery_result.get("mode"),
                "generation": delivery_result.get("generation", prepared.generation),
                "recovered": bool(delivery_result.get("recovered", recovered)),
                "cleanup_pending": bool(delivery_result.get("cleanup_pending")),
            },
        }

    def _failed_result(
        self,
        prepared: PreparedJob,
        error: BaseException,
    ) -> dict[str, Any]:
        partial: list[str] = []
        if isinstance(error, EpisodeProcessingError):
            partial = [item.provisional_relpath for item in error.partial_results]
        return {
            "status": "failed",
            "job_id": prepared.payload.job_id,
            "kind": "series",
            "rules_fingerprint": prepared.rules_snapshot.fingerprint,
            "manifest": prepared.manifest.to_dict(),
            "published": [],
            "review_path": "",
            "provisional": partial,
            "error_code": getattr(error, "code", type(error).__name__),
            "error": _safe_error(error),
        }

    def _resume_delivery(self, prepared: PreparedJob) -> dict[str, Any]:
        if prepared.prepared_series_root is None or prepared.collision.final_series_root is None:
            raise RecoveryAmbiguous("El job no conserva rutas de publicación")
        delivery_result = self.recoverer(
            prepared.payload.job_id,
            prepared.prepared_series_root,
            prepared.collision.final_series_root,
            prepared.journal,
        )
        if delivery_result.get("status") != "committed":
            raise RecoveryAmbiguous(
                "La recuperación no alcanzó COMMITTED: "
                + str(delivery_result.get("status"))
            )
        result = self._done_result(prepared, delivery_result, recovered=True)
        self._write_result(prepared, result)
        return result

    def _run_job(
        self,
        prepared: PreparedJob,
        *,
        acquire_heavy_lock: Callable[[], None] = lambda: None,
        release_heavy_lock: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        existing = self._terminal(prepared)
        if existing is not None:
            return existing
        state = prepared.journal.state
        if state in {"VERIFIED", "COMMITTING", "COMMITTED"}:
            return self._resume_delivery(prepared)
        if state == "ROLLED_BACK":
            self._complete_rolled_back_cleanup(prepared)
            result = self._failed_result(
                prepared, SeriesWorkerError("El job ya quedó ROLLED_BACK")
            )
            self._write_result(prepared, result)
            return result

        _emit_callback(prepared, "series", "started", "Series Worker iniciado")
        if prepared.manifest.review_reasons or prepared.collision.review_reasons:
            reasons = tuple(
                dict.fromkeys(
                    (*prepared.manifest.review_reasons, *prepared.collision.review_reasons)
                )
            )
            release_heavy_lock()
            try:
                result = _review_pack(prepared, reasons)
            except Exception as error:
                result = self._failed_result(prepared, error)
            result = self._finish_without_publish(prepared, result)
            _emit_callback(
                prepared,
                "series_review",
                "finished" if result["status"] == "review" else "error",
                "Pack enviado a revisión" if result["status"] == "review" else "Falló la revisión",
            )
            return result

        if prepared.journal.state == "PREPARED":
            prepared.journal.transition(
                "PROCESSING",
                source_count=len(prepared.manifest.entries),
                pending_count=len(prepared.collision.pending),
                satisfied_count=len(prepared.collision.satisfied),
            )
        processing: ProcessingResult | None = None
        finalized = False
        try:
            if prepared.collision.pending:
                subset = SeriesManifest(
                    status="ready",
                    digest=prepared.manifest.digest,
                    entries=prepared.collision.pending,
                    series_name=prepared.manifest.series_name,
                    series_key=prepared.manifest.series_key,
                    review_reasons=(),
                )
                acquire_heavy_lock()
                processing = self.processor_factory().process(
                    manifest=subset,
                    source_root=prepared.payload.source_root,
                    job_root=prepared.payload.job_root,
                    rules_snapshot=prepared.rules_snapshot,
                )
                if len(processing.episodes) != len(prepared.collision.pending):
                    raise SeriesWorkerError("No se verificaron todos los episodios pendientes")
                release_heavy_lock()
            latest_collision = plan_collisions(prepared.payload, prepared.manifest)
            expected_pending = {entry.source_relpath for entry in prepared.collision.pending}
            expected_satisfied = {
                entry.source_relpath for entry in prepared.collision.satisfied
            }
            current_pending = {entry.source_relpath for entry in latest_collision.pending}
            current_satisfied = {
                entry.source_relpath for entry in latest_collision.satisfied
            }
            if (
                latest_collision.review_reasons
                or current_pending != expected_pending
                or current_satisfied != expected_satisfied
                or latest_collision.final_series_root != prepared.collision.final_series_root
            ):
                reasons = tuple(
                    dict.fromkeys(
                        (
                            "biblioteca_cambio_durante_procesado",
                            *latest_collision.review_reasons,
                        )
                    )
                )
                release_heavy_lock()
                _discard_generated_artifacts(prepared)
                result = _review_pack(prepared, reasons)
                self._finish_without_publish(prepared, result)
                _emit_callback(
                    prepared,
                    "series_review",
                    "finished",
                    "Biblioteca cambió; pack enviado a revisión",
                )
                return result
            expected_files, _unused_digests = _finalize_processed_in_place(
                prepared,
                processing,
            )
            finalized = True
            if prepared.prepared_series_root is None or prepared.collision.final_series_root is None:
                raise SeriesWorkerError("Faltan rutas de publicación")
            delivery_result = self.publisher(
                prepared.payload.job_id,
                prepared.prepared_series_root,
                prepared.collision.final_series_root,
                prepared.journal,
                expected_files=expected_files,
                expected_file_digests={},
                allowed_existing_files={},
            )
            if delivery_result.get("status") != "committed":
                raise DeliveryError("La publicación no terminó COMMITTED")
            result = self._done_result(prepared, delivery_result, recovered=False)
            if processing is not None:
                result["processing"] = {
                    "episodes": [
                        {
                            "source_relpath": episode.source_relpath,
                            "processing_mode": episode.verification.get("processing_mode"),
                            "timings_ms": episode.verification.get("timings_ms") or {},
                        }
                        for episode in processing.episodes
                    ]
                }
            self._write_result(prepared, result)
            _emit_callback(prepared, "series", "finished", "Serie publicada")
            return result
        except SeriesWorkerBusy:
            raise
        except Exception as error:
            state = prepared.journal.state
            if state in {"COMMITTING", "COMMITTED"}:
                _emit_callback(
                    prepared,
                    "series_publish",
                    "warning",
                    "Publicación pendiente de recuperación",
                )
                raise
            if not finalized:
                _discard_generated_artifacts(prepared)
            if _caused_by(error, ReviewRequiredError):
                release_heavy_lock()
                try:
                    result = _review_pack(
                        prepared,
                        ("procesamiento_requiere_revision:" + _safe_error(error),),
                        processing_identity=processing_review_identity(error),
                    )
                except Exception as review_error:
                    result = self._failed_result(prepared, review_error)
                self._finish_without_publish(prepared, result)
                _emit_callback(
                    prepared,
                    "series_review",
                    "finished" if result["status"] == "review" else "error",
                    "Procesamiento enviado a revisión",
                )
                return result
            release_heavy_lock()
            try:
                result = _review_pack(
                    prepared,
                    ("procesamiento_fallido:" + _safe_error(error),),
                    processing_identity=processing_review_identity(error),
                )
            except Exception as review_error:
                result = self._failed_result(prepared, review_error)
            self._finish_without_publish(prepared, result)
            _emit_callback(
                prepared,
                "series_review",
                "finished" if result["status"] == "review" else "error",
                (
                    "Procesamiento fallido; pack enviado a revisión"
                    if result["status"] == "review"
                    else "Falló la revisión tras el error de procesamiento"
                ),
            )
            return result

    def status(self, job_id: str) -> Submission:
        if not JOB_ID_RE.fullmatch(str(job_id or "")):
            raise RequestValidationError("job_id no es válido")
        reports_root = _configured_reports_root()
        job_dir = reports_root / job_id
        with self._mutex:
            if self._active is not None and self._active["job_id"] == job_id:
                return Submission(
                    202,
                    {
                        "ok": True,
                        "status": "active",
                        "job_id": job_id,
                        "kind": "series",
                        "rules_fingerprint": self._active["rules_fingerprint"],
                        "started_at": self._active["started_at"],
                    },
                )
        journal = DurableJournal(job_dir)
        request_record = _read_json(job_dir / REQUEST_FILE)
        if request_record is None:
            if _read_json(job_dir / RESULT_FILE) is not None:
                raise ServiceUnavailable("Resultado terminal sin request.json durable")
            try:
                orphan_snapshot = journal.snapshot()
            except JournalContradiction as error:
                raise ServiceUnavailable("Journal durable contradictorio") from error
            if orphan_snapshot is not None:
                raise ServiceUnavailable("Journal durable sin request.json")
            return Submission(
                404,
                {
                    "ok": False,
                    "status": "not_found",
                    "error": "series_job_not_found",
                    "retryable": False,
                    "job_id": job_id,
                    "kind": "series",
                },
            )
        stored_payload = request_record.get("payload")
        if not isinstance(stored_payload, dict):
            raise ServiceUnavailable("request.json durable no conserva el payload")
        try:
            replay_validated = _validated_payload(
                stored_payload,
                require_directories=False,
                allow_legacy_review_root=True,
            )
        except RequestValidationError as error:
            raise ServiceUnavailable("request.json durable contiene rutas no válidas") from error
        _persisted, _payload_digest, _fingerprint, _generation, stage = (
            self._request_identity(replay_validated, request_record)
        )
        terminal = self._terminal_from_job(journal, job_id)
        if terminal is not None:
            return Submission(200, self._terminal_response(job_id, terminal))
        try:
            snapshot = journal.snapshot()
        except JournalContradiction as error:
            raise ServiceUnavailable("Journal durable contradictorio") from error
        stored_result = _read_json(job_dir / RESULT_FILE)
        delivery_recoverable = (
            snapshot is not None
            and snapshot["state"] in {"VERIFIED", "COMMITTING", "COMMITTED"}
        )
        if stored_result is None or delivery_recoverable:
            try:
                resumed = self.submit(stored_payload)
            except SeriesWorkerBusy:
                pass
            except SeriesWorkerError as error:
                with self._mutex:
                    self._last_errors[job_id] = _safe_error(error)
            else:
                if resumed.http_status in {200, 202}:
                    with self._mutex:
                        self._last_errors.pop(job_id, None)
                    return resumed
        return Submission(
            202,
            {
                "ok": True,
                "status": "recoverable",
                "job_id": job_id,
                "kind": "series",
                "journal_state": snapshot["state"] if snapshot is not None else stage.upper(),
                "retryable": True,
                "last_error": self._last_errors.get(job_id, ""),
            },
        )

    def wait(self, job_id: str, timeout: float = 10.0) -> Submission:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status(job_id)
            if status.http_status != 202:
                return status
            time.sleep(0.01)
        return self.status(job_id)


__all__ = [
    "JobConflict",
    "PreparedJob",
    "RequestValidationError",
    "SeriesCoordinator",
    "SeriesWorkerBusy",
    "SeriesWorkerError",
    "ServiceUnavailable",
    "Submission",
    "ValidatedPayload",
    "plan_collisions",
    "validate_payload",
]
