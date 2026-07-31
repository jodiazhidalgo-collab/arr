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
    AtomicDeliveryUnsupported,
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
    fsync_directory,
    write_json_file_atomic,
)
from .manifest import (
    ManifestEntry,
    ManifestError,
    ManifestSidecar,
    SeriesManifest,
    discover_manifest,
    validate_relative_path,
)
from .processing import (
    BASE_TOOLS,
    OCR_TOOLS,
    EpisodeProcessingError,
    ProcessingResult,
    ReviewRequiredError,
    SeriesProcessor,
    unavailable_tools,
)
from .rules import RulesSnapshot, RulesStore, RulesValidationError, rules_fingerprint


JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EPISODE_TOKEN_RE = re.compile(
    r"(?i)(?<![A-Z0-9])S(?P<season>\d{1,3})(?P<body>E\d{1,3}(?:(?:[ ._-]*E|[ ._-]+)\d{1,3})*)"
)
EPISODE_NUMBER_RE = re.compile(r"(?i)E?(\d{1,3})")
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
DEFAULT_REVIEW_ROOT = "/data/media/repetidas_vs_error_series"
DEFAULT_REPORT_ROOT = "/config/series-worker"
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
class Submission:
    http_status: int
    payload: dict[str, Any]


def _configured_path(name: str, default: str) -> Path:
    return Path(str(os.environ.get(name, default) or default).strip()).resolve()


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


def _validated_payload(payload: Any, *, require_directories: bool) -> ValidatedPayload:
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
    if review_root != _configured_path("SERIES_WORKER_REVIEW_ROOT", DEFAULT_REVIEW_ROOT):
        raise RequestValidationError("review_root no es la raíz de revisión canónica")
    if reports_root != _configured_path("SERIES_WORKER_REPORT_ROOT", DEFAULT_REPORT_ROOT):
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
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
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
                "source_fingerprint", "content_sha256",
            )
            if any(not isinstance(item[field], str) or not item[field] for field in string_fields):
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
                or not _is_sha256(item["content_sha256"])
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
                    or not _is_sha256(sidecar["content_sha256"])
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


def _same_file(left: Path, right: Path) -> bool:
    if not left.is_file() or not right.is_file():
        return False
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as first, right.open("rb") as second:
        while True:
            left_chunk = first.read(1024 * 1024)
            right_chunk = second.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _episode_identity(path: Path) -> tuple[int, frozenset[int]] | None:
    match = EPISODE_TOKEN_RE.search(path.stem)
    if match is None:
        return None
    episodes = frozenset(int(value) for value in EPISODE_NUMBER_RE.findall(match.group("body")))
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
            if exact.suffix.casefold() == ".mkv" and _same_file(source, exact):
                if entry.subtitle_sidecars:
                    pending.append(entry)
                else:
                    satisfied.append(entry)
            else:
                reasons.append(f"colision_diferente:{entry.target_relpath}")
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


def _prepared_root(
    final_root: Path,
    series_name: str,
    job_id: str,
    generation: str,
) -> Path:
    return final_root / (
        f".{_safe_fragment(series_name)}.series-worker."
        f"{_safe_fragment(job_id)}.{generation}.prepared"
    )


def _cleanup_prepared_staging(prepared: PreparedJob) -> None:
    staging = prepared.prepared_series_root
    final_series = prepared.collision.final_series_root
    if staging is None:
        return
    series_name = final_series.name if final_series is not None else prepared.manifest.series_name
    if not series_name:
        raise SeriesWorkerError("Staging preparado sin identidad de serie")
    expected = _prepared_root(
        prepared.payload.final_root,
        series_name,
        prepared.payload.job_id,
        prepared.generation,
    )
    if staging != expected or staging.parent != prepared.payload.final_root:
        raise SeriesWorkerError("Staging preparado no coincide con la identidad durable")
    if not staging.exists():
        return
    if staging.is_symlink() or not staging.is_dir():
        raise SeriesWorkerError("Staging preparado no es un directorio propio seguro")
    shutil.rmtree(staging)
    fsync_directory(staging.parent)


def _copy_provisional_to_prepared(
    prepared: PreparedJob,
    processing: ProcessingResult | None,
) -> tuple[str, ...]:
    destination_root = prepared.prepared_series_root
    if destination_root is None:
        raise SeriesWorkerError("No existe staging de publicación")
    if destination_root.exists():
        if destination_root.is_symlink() or destination_root.parent != prepared.payload.final_root:
            raise SeriesWorkerError("Staging de publicación no reconocido")
        shutil.rmtree(destination_root)
    destination_root.mkdir(parents=True)
    by_source = {
        episode.source_relpath: episode
        for episode in (processing.episodes if processing else ())
    }
    subtitle_suffix = str(
        prepared.rules_snapshot.rules["subtitulos"]["sufijo_srt_externo"]
    )
    expected_files: list[str] = []
    final_series_root = prepared.collision.final_series_root
    if final_series_root is None:
        raise SeriesWorkerError("No existe raíz final de la serie")
    if prepared.collision.satisfied and (
        final_series_root.is_symlink() or not final_series_root.is_dir()
    ):
        raise ReviewRequiredError("La raíz de la serie cambió en la biblioteca")
    for entry in prepared.collision.satisfied:
        target_parts = PurePosixPath(entry.target_relpath).parts
        relative = Path(*target_parts[1:])
        current = final_series_root
        for index, part in enumerate(relative.parts):
            try:
                matches = [
                    child
                    for child in current.iterdir()
                    if _path_key(child.name) == _path_key(part)
                ]
            except OSError as error:
                raise ReviewRequiredError(
                    f"La biblioteca cambió: {entry.target_relpath}"
                ) from error
            if len(matches) != 1:
                raise ReviewRequiredError(
                    f"La biblioteca cambió: {entry.target_relpath}"
                )
            current = matches[0]
            if current.is_symlink():
                raise ReviewRequiredError(
                    f"La biblioteca cambió: {entry.target_relpath}"
                )
            if index < len(relative.parts) - 1 and not current.is_dir():
                raise ReviewRequiredError(
                    f"La biblioteca cambió: {entry.target_relpath}"
                )
        source = prepared.payload.source_root / Path(
            *PurePosixPath(entry.source_relpath).parts
        )
        if source.is_symlink() or not source.is_file() or not _same_file(source, current):
            raise ReviewRequiredError(
                f"La biblioteca cambió: {entry.target_relpath}"
            )
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise SeriesWorkerError(
                f"Destino preparado duplicado: {entry.target_relpath}"
            )
        try:
            os.link(current, destination, follow_symlinks=False)
        except (NotImplementedError, OSError) as error:
            raise ReviewRequiredError(
                f"No se pudo crear el hardlink satisfecho: {entry.target_relpath}"
            ) from error
        if destination.is_symlink() or not destination.is_file() or not _same_file(
            current, destination
        ):
            raise ReviewRequiredError(
                f"La biblioteca cambió: {entry.target_relpath}"
            )
        expected_files.append(relative.as_posix())
    for entry in prepared.collision.pending:
        episode = by_source.get(entry.source_relpath)
        if episode is None:
            raise SeriesWorkerError(f"Falta salida verificada: {entry.source_relpath}")
        provisional = prepared.payload.job_root / Path(
            *PurePosixPath(episode.provisional_relpath).parts
        )
        if provisional.is_symlink() or not provisional.is_file():
            raise SeriesWorkerError(f"Salida provisional inválida: {entry.source_relpath}")
        target_parts = PurePosixPath(entry.target_relpath).parts
        relative = Path(*target_parts[1:])
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(provisional, destination)
        expected_files.append(relative.as_posix())
        provisional_srt = provisional.with_name(f"{provisional.stem}{subtitle_suffix}")
        if provisional_srt.is_file() and not provisional_srt.is_symlink():
            subtitle_destination = destination.with_name(
                f"{destination.stem}{subtitle_suffix}"
            )
            shutil.copy2(
                provisional_srt,
                subtitle_destination,
            )
            expected_files.append(subtitle_destination.relative_to(destination_root).as_posix())
    return tuple(sorted(expected_files, key=str.casefold))


_REVIEW_METADATA = frozenset({"reason.json", "Revision de serie.txt"})


def _review_tree_signature(
    root: Path,
    *,
    ignore_metadata: bool = False,
) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise SeriesWorkerError("El árbol de revisión no es un directorio físico")
    signature: dict[str, str] = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative_root = current_path.relative_to(root)
        for name in directories:
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SeriesWorkerError("El pack contiene una carpeta no regular")
            relative = (relative_root / name).as_posix()
            signature[f"D:{relative}"] = "directory"
        for name in filenames:
            if ignore_metadata and relative_root == Path(".") and name in _REVIEW_METADATA:
                continue
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise SeriesWorkerError("El pack contiene un archivo no regular")
            relative = (relative_root / name).as_posix()
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            signature[f"F:{relative}"] = digest.hexdigest()
    return signature


def _write_text_durable(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_review_tree(root: Path) -> None:
    directories: list[Path] = []
    for current, child_directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories.append(current_path)
        for name in child_directories:
            path = current_path / name
            if path.is_symlink() or not path.is_dir():
                raise SeriesWorkerError("La copia de revisión contiene una carpeta no regular")
        for name in filenames:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise SeriesWorkerError("La copia de revisión contiene un archivo no regular")
            flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
            descriptor = os.open(path, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        fsync_directory(directory)


def _reset_review_temporary(
    temporary: Path,
    expected_reason: dict[str, Any],
) -> None:
    if not temporary.exists():
        return
    if temporary.is_symlink() or not temporary.is_dir():
        raise SeriesWorkerError("Staging de revisión existente no es seguro")
    try:
        entries = list(temporary.iterdir())
        marker = _read_json(temporary / "reason.json")
    except ServiceUnavailable as error:
        marker = None
        marker_error = error
    else:
        marker_error = None
    only_marker_temps = bool(entries) and all(
        entry.is_file()
        and entry.name.startswith(".reason.json.")
        and entry.name.endswith(".tmp")
        for entry in entries
    )
    if entries and marker != expected_reason and not only_marker_temps:
        if marker_error is not None:
            raise SeriesWorkerError("Staging de revisión tiene marcador ilegible") from marker_error
        raise SeriesWorkerError("Staging de revisión pertenece a otro trabajo")
    shutil.rmtree(temporary)
    fsync_directory(temporary.parent)


def _review_pack(prepared: PreparedJob, reasons: tuple[str, ...]) -> dict[str, Any]:
    payload = prepared.payload
    suffix = prepared.manifest.digest[:12]
    destination = payload.review_root / f"{_safe_fragment(payload.job_id)}-{suffix}"
    expected_reason = {
        "job_id": payload.job_id,
        "manifest_digest": prepared.manifest.digest,
        "reasons": list(reasons),
    }
    expected_text = "Revisión de serie\n" + "\n".join(reasons) + "\n"
    for reserved in _REVIEW_METADATA:
        if (payload.source_root / reserved).exists():
            raise SeriesWorkerError(
                f"El pack usa un nombre reservado para revisión: {reserved}"
            )
    source_signature = _review_tree_signature(payload.source_root)
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise SeriesWorkerError("Destino de revisión existente no es seguro")
        if _review_tree_signature(destination, ignore_metadata=True) != source_signature:
            raise SeriesWorkerError("La copia de revisión existente no coincide con el origen")
        if _read_json(destination / "reason.json") != expected_reason:
            raise SeriesWorkerError("La copia de revisión existente no conserva su motivo")
        try:
            stored_text = (destination / "Revision de serie.txt").read_text(
                encoding="utf-8"
            )
        except OSError as error:
            raise SeriesWorkerError("La copia de revisión no conserva su resumen") from error
        if stored_text != expected_text:
            raise SeriesWorkerError("La copia de revisión contradice su resumen")
        _fsync_review_tree(destination)
        fsync_directory(payload.review_root)
    else:
        temporary = payload.review_root / f".{destination.name}.series-worker.tmp"
        _reset_review_temporary(temporary, expected_reason)
        try:
            temporary.mkdir()
            fsync_directory(payload.review_root)
            write_json_file_atomic(temporary / "reason.json", expected_reason)
            shutil.copytree(
                payload.source_root,
                temporary,
                symlinks=False,
                dirs_exist_ok=True,
            )
            copied_signature = _review_tree_signature(
                temporary,
                ignore_metadata=True,
            )
            if copied_signature != source_signature:
                raise SeriesWorkerError("La copia de revisión no coincide con el origen")
            if _review_tree_signature(payload.source_root) != source_signature:
                raise SeriesWorkerError("El origen cambió durante la copia de revisión")
            _write_text_durable(
                temporary / "Revision de serie.txt",
                expected_text,
            )
            _fsync_review_tree(temporary)
            os.rename(temporary, destination)
            fsync_directory(payload.review_root)
            if _review_tree_signature(destination, ignore_metadata=True) != source_signature:
                raise SeriesWorkerError("La copia de revisión publicada no coincide con el origen")
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    if _review_tree_signature(payload.source_root) != source_signature:
        raise SeriesWorkerError("El origen cambió antes de confirmar la revisión")
    return {
        "status": "review",
        "job_id": payload.job_id,
        "kind": "series",
        "rules_fingerprint": prepared.rules_snapshot.fingerprint,
        "manifest": prepared.manifest.to_dict(),
        "review_path": destination.relative_to(payload.review_root).as_posix(),
        "review_reasons": list(reasons),
        "published": [],
    }


def _safe_error(error: BaseException) -> str:
    text = str(error).strip() or type(error).__name__
    text = re.sub(r"(?i)(token|password|secret|auth)\s*[:=]\s*\S+", r"\1=<REDACTED>", text)
    text = re.sub(r"(?i)https?://\S+", "<URL>", text)
    text = re.sub(r"(?i)(?:[A-Z]:[\\/]|/data/|/config/)\S*", "<PATH>", text)
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
        self.atomic_preflight = atomic_preflight
        self.tool_checker = tool_checker or (
            lambda: unavailable_tools(
                names=(*BASE_TOOLS, *OCR_TOOLS),
                timeout=3,
                parallel=True,
            )
        )
        self.lock_factory = lock_factory
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
        self._health_atomicity_cache: dict[str, Any] | None = None
        self._health_atomicity_lock = threading.Lock()

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
            "atomicity": {"ok": False},
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
            final_stat = final_root.stat()
            if not final_root.is_dir():
                raise NotADirectoryError(str(final_root))
            with self._health_atomicity_lock:
                cached = (
                    dict(self._health_atomicity_cache)
                    if self._health_atomicity_cache is not None
                    else None
                )
                if cached is not None and int(cached.get("st_dev", -1)) != int(
                    final_stat.st_dev
                ):
                    self._health_atomicity_cache = None
                    cached = None
            checks["atomicity"] = (
                {"ok": True, "verified": True, **cached}
                if cached is not None
                else {
                    "ok": True,
                    "verified": False,
                    "status": "preflight_on_submit",
                    "st_dev": int(final_stat.st_dev),
                }
            )
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

    def _existing_candidate(
        self,
        validated: ValidatedPayload,
        existing: dict[str, Any],
    ) -> tuple[PreparedJob, bool, dict[str, Any]]:
        if existing.get("schema") != "series-worker-request-v1":
            raise ServiceUnavailable("request.json durable no es válido")
        stored_payload = existing.get("payload")
        if not isinstance(stored_payload, dict):
            raise ServiceUnavailable("request.json durable no conserva el payload")
        try:
            persisted_validated = _validated_payload(
                stored_payload,
                require_directories=False,
            )
        except RequestValidationError as error:
            raise ServiceUnavailable("request.json durable contiene rutas no válidas") from error
        persisted_payload = persisted_validated.persisted()
        payload_digest = _digest(persisted_payload)
        if existing.get("payload_digest") != payload_digest:
            raise ServiceUnavailable("request.json durable no coincide con su payload")
        if validated.persisted() != persisted_payload:
            raise JobConflict("job_id ya está ligado a otro payload")

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
        generation = existing.get("generation")
        if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
            raise ServiceUnavailable("El job durable no tiene una generación válida")
        collision = plan_collisions(persisted_validated, manifest)
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
                    persisted_validated.final_root,
                    series_name,
                    persisted_validated.job_id,
                    generation,
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
                validated.final_root,
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
            "marker_name": MARKER_NAME,
        }
        if prepared.prepared_series_root is not None and prepared.collision.final_series_root is not None:
            mode = "exchange" if prepared.collision.final_series_root.exists() else "new"
            details.update(
                {
                    "mode": mode,
                    "prepared_series_root": str(prepared.prepared_series_root.resolve(strict=False)),
                    "final_series_root": str(prepared.collision.final_series_root.resolve(strict=False)),
                }
            )
        journal.transition("PREPARED", **details)

    def _terminal(self, prepared: PreparedJob) -> dict[str, Any] | None:
        result = _read_json(prepared.journal.job_dir / RESULT_FILE)
        try:
            snapshot = prepared.journal.snapshot()
        except JournalContradiction as error:
            raise ServiceUnavailable("Journal durable contradictorio") from error
        if snapshot is not None and snapshot["state"] == "ROLLED_BACK":
            journal_result = snapshot["details"].get("terminal_result")
            if journal_result is not None and not isinstance(journal_result, dict):
                raise ServiceUnavailable("Resultado terminal inválido en el journal")
            if result is None and isinstance(journal_result, dict):
                self._write_result(prepared, journal_result)
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
        return result

    def submit(self, raw_payload: Any) -> Submission:
        replay_validated = _validated_payload(raw_payload, require_directories=False)
        with self._mutex:
            if self._active is not None and self._active["job_id"] != replay_validated.job_id:
                raise SeriesWorkerBusy("Otro job de series está activo")
            job_dir = replay_validated.reports_root / replay_validated.job_id
            existing_record = _read_json(job_dir / REQUEST_FILE)
            if existing_record is not None:
                prepared, existed, request_record = self._existing_candidate(
                    replay_validated,
                    existing_record,
                )
                validated = prepared.payload
            else:
                validated = validate_payload(raw_payload)
                prepared, existed, request_record = self._candidate(validated)
            if self._active is not None:
                if self._active["request_digest"] != prepared.request_digest:
                    raise JobConflict("job_id activo con otro payload")
                return Submission(202, self._active_response(prepared))
            terminal = self._terminal(prepared)
            if terminal is not None:
                return Submission(200, self._terminal_response(prepared, terminal))

            lock_context: ContextManager[Any] = nullcontext({"enabled": False})
            needs_processing = (
                prepared.journal.state in {None, "PREPARED", "PROCESSING"}
                and not prepared.collision.review_reasons
            )
            if needs_processing:
                try:
                    missing = self.tool_checker()
                except Exception as error:
                    raise ServiceUnavailable("No se pudieron validar las herramientas") from error
                if missing:
                    raise ServiceUnavailable("Faltan herramientas: " + ", ".join(missing))
                try:
                    atomic_result = self.atomic_preflight(validated.final_root)
                    current_device = int(validated.final_root.stat().st_dev)
                    if int(atomic_result.get("st_dev", -1)) != current_device:
                        raise AtomicDeliveryUnsupported(
                            "El preflight no verificó el dispositivo actual"
                        )
                except Exception as error:
                    with self._health_atomicity_lock:
                        self._health_atomicity_cache = None
                    raise ServiceUnavailable(
                        "Publicación atómica no disponible: " + _safe_error(error)
                    ) from error
                with self._health_atomicity_lock:
                    self._health_atomicity_cache = dict(atomic_result)
                lock_context = self.lock_factory(
                    self.lock_path,
                    timeout_sec=0,
                )
                try:
                    lock_context.__enter__()
                except HeavyLockTimeout as error:
                    raise SeriesWorkerBusy("El motor audiovisual compartido está ocupado") from error
            try:
                if not existed:
                    if needs_processing:
                        request_record["atomic_preflight"] = atomic_result
                    self._persist_prepared(prepared, request_record)
                elif prepared.journal.state is None:
                    self._transition_prepared(prepared)
                self._active = {
                    "job_id": validated.job_id,
                    "request_digest": prepared.request_digest,
                    "rules_fingerprint": prepared.rules_snapshot.fingerprint,
                    "started_at": time.time(),
                }
                thread = threading.Thread(
                    target=self._background,
                    args=(prepared, lock_context, needs_processing),
                    name=f"series-worker-{validated.job_id}",
                    daemon=True,
                )
                self._threads[validated.job_id] = thread
                thread.start()
            except Exception:
                self._active = None
                self._threads.pop(validated.job_id, None)
                if needs_processing:
                    lock_context.__exit__(None, None, None)
                raise
            response = (
                self._active_response(prepared)
                if existed
                else self._accepted_response(prepared)
            )
            return Submission(202, response)

    def _accepted_response(self, prepared: PreparedJob) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "accepted",
            "job_id": prepared.payload.job_id,
            "kind": "series",
            "rules_fingerprint": prepared.rules_snapshot.fingerprint,
        }

    def _active_response(self, prepared: PreparedJob) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "active",
            "job_id": prepared.payload.job_id,
            "kind": "series",
            "rules_fingerprint": prepared.rules_snapshot.fingerprint,
        }

    def _terminal_response(
        self, prepared: PreparedJob, result: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "terminal",
            "job_id": prepared.payload.job_id,
            "kind": "series",
            "result": result,
        }

    def _background(
        self,
        prepared: PreparedJob,
        lock_context: ContextManager[Any],
        lock_entered: bool,
    ) -> None:
        try:
            self._run_job(prepared)
        except Exception as error:
            with self._mutex:
                self._last_errors[prepared.payload.job_id] = _safe_error(error)
        finally:
            if lock_entered:
                try:
                    lock_context.__exit__(None, None, None)
                except Exception:
                    pass
            with self._mutex:
                if self._active and self._active["job_id"] == prepared.payload.job_id:
                    self._active = None
                self._threads.pop(prepared.payload.job_id, None)

    def _write_result(self, prepared: PreparedJob, result: dict[str, Any]) -> None:
        prepared.journal.write_json_atomic(RESULT_FILE, result)

    def _complete_rolled_back_cleanup(self, prepared: PreparedJob) -> None:
        if (
            prepared.prepared_series_root is None
            or prepared.collision.final_series_root is None
        ):
            return
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
        _cleanup_prepared_staging(prepared)
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
        published = [entry.target_relpath for entry in prepared.manifest.entries]
        satisfied = [entry.target_relpath for entry in prepared.collision.satisfied]
        final_root = prepared.collision.final_series_root
        return {
            "status": "done",
            "job_id": prepared.payload.job_id,
            "kind": "series",
            "rules_fingerprint": prepared.rules_snapshot.fingerprint,
            "manifest": prepared.manifest.to_dict(),
            "published": published,
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

    def _run_job(self, prepared: PreparedJob) -> dict[str, Any]:
        existing = self._terminal(prepared)
        if existing is not None:
            return existing
        state = prepared.journal.state
        if state in {"VERIFIED", "COMMITTING", "COMMITTED"}:
            return self._resume_delivery(prepared)
        if state == "ROLLED_BACK":
            self._complete_rolled_back_cleanup(prepared)
            _cleanup_prepared_staging(prepared)
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
        try:
            processing: ProcessingResult | None = None
            if prepared.collision.pending:
                subset = SeriesManifest(
                    status="ready",
                    digest=prepared.manifest.digest,
                    entries=prepared.collision.pending,
                    series_name=prepared.manifest.series_name,
                    series_key=prepared.manifest.series_key,
                    review_reasons=(),
                )
                processing = self.processor_factory().process(
                    manifest=subset,
                    source_root=prepared.payload.source_root,
                    job_root=prepared.payload.job_root,
                    rules_snapshot=prepared.rules_snapshot,
                )
                if len(processing.episodes) != len(prepared.collision.pending):
                    raise SeriesWorkerError("No se verificaron todos los episodios pendientes")
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
                result = _review_pack(prepared, reasons)
                self._finish_without_publish(prepared, result)
                _emit_callback(
                    prepared,
                    "series_review",
                    "finished",
                    "Biblioteca cambió; pack enviado a revisión",
                )
                return result
            expected_files = _copy_provisional_to_prepared(prepared, processing)
            if prepared.prepared_series_root is None or prepared.collision.final_series_root is None:
                raise SeriesWorkerError("Faltan rutas de publicación")
            delivery_result = self.publisher(
                prepared.payload.job_id,
                prepared.prepared_series_root,
                prepared.collision.final_series_root,
                prepared.journal,
                expected_files=expected_files,
            )
            if delivery_result.get("status") != "committed":
                raise DeliveryError("La publicación no terminó COMMITTED")
            result = self._done_result(prepared, delivery_result, recovered=False)
            self._write_result(prepared, result)
            _emit_callback(prepared, "series", "finished", "Serie publicada")
            return result
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
            if _caused_by(error, ReviewRequiredError):
                try:
                    result = _review_pack(
                        prepared,
                        ("procesamiento_requiere_revision:" + _safe_error(error),),
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
            result = self._failed_result(prepared, error)
            self._finish_without_publish(prepared, result)
            _emit_callback(prepared, "series", "error", "Procesado de serie fallido")
            return result

    def status(self, job_id: str) -> Submission:
        if not JOB_ID_RE.fullmatch(str(job_id or "")):
            raise RequestValidationError("job_id no es válido")
        reports_root = _configured_path("SERIES_WORKER_REPORT_ROOT", DEFAULT_REPORT_ROOT)
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
        prepared: PreparedJob | None = None
        if request_record is not None:
            stored_payload = request_record.get("payload")
            if not isinstance(stored_payload, dict):
                raise ServiceUnavailable("request.json durable no conserva el payload")
            try:
                replay_validated = _validated_payload(
                    stored_payload,
                    require_directories=False,
                )
            except RequestValidationError as error:
                raise ServiceUnavailable("request.json durable contiene rutas no válidas") from error
            prepared, _, _ = self._existing_candidate(replay_validated, request_record)
            terminal = self._terminal(prepared)
            if terminal is not None:
                return Submission(200, self._terminal_response(prepared, terminal))
        try:
            snapshot = journal.snapshot()
        except JournalContradiction as error:
            raise ServiceUnavailable("Journal durable contradictorio") from error
        if (
            prepared is not None
            and snapshot is not None
            and snapshot["state"] in {"VERIFIED", "COMMITTING", "COMMITTED"}
        ):
            try:
                self._resume_delivery(prepared)
            except Exception as error:
                with self._mutex:
                    self._last_errors[job_id] = _safe_error(error)
            terminal = self._terminal(prepared)
            if terminal is not None:
                return Submission(200, self._terminal_response(prepared, terminal))
            snapshot = journal.snapshot()
        elif prepared is None and _read_json(job_dir / RESULT_FILE) is not None:
            raise ServiceUnavailable("Resultado terminal sin request.json durable")
        if (
            prepared is not None
            and snapshot is not None
            and snapshot["state"] in {"PREPARED", "PROCESSING"}
            and _read_json(job_dir / RESULT_FILE) is None
        ):
            try:
                resumed = self.submit(stored_payload)
            except SeriesWorkerBusy:
                pass
            except SeriesWorkerError as error:
                with self._mutex:
                    self._last_errors[job_id] = _safe_error(error)
            else:
                if resumed.http_status == 202:
                    return resumed
        if snapshot is not None:
            return Submission(
                202,
                {
                    "ok": True,
                    "status": "recoverable",
                    "job_id": job_id,
                    "kind": "series",
                    "journal_state": snapshot["state"],
                    "retryable": True,
                    "last_error": self._last_errors.get(job_id, ""),
                },
            )
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
