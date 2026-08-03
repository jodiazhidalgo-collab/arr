import hashlib
import json
import logging
import os
import queue
import re
import shutil
import stat
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from watchdog.observers import Observer

from .clients import QbitLikeClient
from .config import Config
from .codex_diagnostics import create_codex_diagnostic
from .db import Database
from .diagnostic_sanitizer import sanitize_for_export
from .filebot import FileBotRunner
from .identity.controller import IdentityController
from .filesystem import (
    JUNK_EXTENSIONS,
    MEDIA_EXTENSIONS,
    SIDECAR_EXTENSIONS,
    ExtractionError,
    clean_junk,
    extract_archives,
    full_bluray_folders,
    manifest,
    matching_root,
    media_files,
    media_worker_source,
    move_extraction_failure_to_review,
    move_into_job,
    move_job_to,
    move_job_to_review_clean,
    move_tv_job_to_review,
    move_trailer_package_into_job,
    prepare_filebot_input,
    review_content_signature,
    top_level_item,
    trailer_package_manifest,
    trailer_ready_source,
    write_reason,
)
from .media_worker import (
    MediaWorkerBusy,
    MediaWorkerClient,
    MediaWorkerError,
    MediaWorkerJobActive,
    MediaWorkerTransportError,
)
from .series_worker import (
    SeriesWorkerBadRequest,
    SeriesWorkerBusy,
    SeriesWorkerClient,
    SeriesWorkerConflict,
    SeriesWorkerError,
    SeriesWorkerTransportError,
    SeriesWorkerUnavailable,
)
from .name_resolver import (
    ResolutionError,
    ResolvedIdentity,
    ResolverAmbiguous,
    ResolverUnavailable,
)
from .name_parser import MediaDecision, decide_media
from .torrent import torrent_info
from .watchers import EventHandler


TERMINAL_STATES = {"done", "manual_review", "duplicate", "error_terminal", "discarded"}
PROCESSABLE_STATES = {
    "waiting_stable",
    "ready_stage",
    "staging",
    "ready_extract",
    "extracting",
    "ready_filebot",
    "identity_retry",
    "filebot_running",
    "bluray_running",
    "media_postprocess_ready",
    "media_postprocess_running",
    "series_postprocess_ready",
    "series_postprocess_running",
    "series_review_cleanup",
    "trailer_ready",
    "trailer_running",
    "verifying_output",
    "ready_cleanup",
}


def _sanitize_extraction_details(job_root: Path, details: Dict[str, object]) -> Dict[str, object]:
    root_text = str(job_root)

    def sanitize(value: object) -> object:
        if isinstance(value, dict):
            return {str(key): sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [sanitize(item) for item in list(value)[:32]]
        if isinstance(value, str):
            cleaned = value.replace(root_text, "<JOB_ROOT>").replace("\\", "/")
            return cleaned[-2000:] if len(cleaned) > 2000 else cleaned
        return value

    return sanitize(details)  # type: ignore[return-value]
COMPLETE_CATEGORIES = ("movies", "tv", "manual", "movies_automatizacion", "trailers_automatizacion")
WATCHER_RULES_SETTING_KEY = "watcher.movies.ignored_suffixes"
TV_WATCHER_RULES_SETTING_KEY = "watcher.tv.ignored_suffixes"
DEFAULT_IGNORED_MOVIES_SUFFIXES = (".delay-audio-part",)
DEFAULT_IGNORED_TV_SUFFIXES: Tuple[str, ...] = ()
WORKER_STATUS_POLL_SECONDS = 5.0
WORKER_ACTIVE_MAX_SECONDS = 4 * 60 * 60 + 5 * 60
MAX_WORKER_RESULT_BYTES = 16 * 1024 * 1024
SERIES_PIPELINE_SCHEMA = "arr-series-pipeline-v1"
SERIES_CANARY_PREFIX = "codex_live_flow_probe_"
SERIES_PUBLISHED_MANIFEST_SCHEMA = "series-published-manifest-v1"
SERIES_GENERATION_MARKER = ".series-worker-generation.json"
SERIES_REVIEW_METADATA_FILES = (
    "reason.json",
    "Revision de serie.txt",
    "Serie repetida.txt",
)
SERIES_REVIEW_SIGNATURE_STAT_V1 = "stat-v1"
SERIES_REVIEW_SIGNATURE_SHA256_V1 = "sha256-v1"
LEGACY_SERIES_REVIEW_DIRNAME = "repetidas_vs_error_series"
SERIES_FULL_PIPELINE_STATES = {
    "ready_stage",
    "staging",
    "ready_extract",
    "extracting",
    "ready_filebot",
    "identity_retry",
    "filebot_running",
    "series_postprocess_ready",
    "series_postprocess_running",
    "series_review_cleanup",
    "verifying_output",
    "ready_cleanup",
}


class Engine:
    def __init__(self, config: Config, database: Database):
        self.config = config
        self.db = database
        self.log = logging.getLogger("arr-orchestrator")
        self.qbt = QbitLikeClient(
            config.qbt_url, config.qbt_user, config.qbt_password, "qBittorrent"
        )
        self.rdt = QbitLikeClient(
            config.rdt_url, config.rdt_user, config.rdt_password, "RDT-Client"
        )
        self.filebot = FileBotRunner(config.filebot_bin, config.log_dir)
        self.identity = IdentityController(
            config,
            database,
            logger=self.log,
        )
        self.name_resolver = self.identity.resolver
        self.media_worker = MediaWorkerClient(config.media_worker_url, config.callback_url)
        self.series_worker = SeriesWorkerClient(
            config.series_worker_url,
            config.callback_url,
        )
        self.events: "queue.Queue[Tuple[str, Path]]" = queue.Queue()
        self.observer = Observer()
        self._stable: Dict[str, Tuple[str, float]] = {}
        self._stable_log_at: Dict[str, float] = {}
        self._missing_source_since: Dict[str, float] = {}
        self._worker_status_log_at: Dict[str, float] = {}
        self._worker_status_checked_at: Dict[str, float] = {}
        self._worker_started_at: Dict[str, float] = {}
        self._series_retry_at: Dict[str, float] = {}
        self._last_reconcile = 0.0
        self._last_heartbeat = 0.0
        self._last_series_dependency_check = 0.0
        self._series_dependency_refreshing = False
        self.running = True
        self.dependencies: Dict[str, str] = {}
        self._watcher_rules_locks = {
            "movies": threading.RLock(),
            "tv": threading.RLock(),
        }
        self._watcher_rules_snapshot = self._load_ignored_movies_suffixes()
        self._tv_watcher_rules_snapshot = self._load_watcher_suffixes("tv")

    @staticmethod
    def _normalize_ignored_movies_suffixes(values: object) -> Tuple[str, ...]:
        if not isinstance(values, list):
            raise ValueError("La lista de finales ignorados no es valida.")
        normalized: List[str] = []
        seen = set()
        for value in values:
            if not isinstance(value, str):
                raise ValueError("Cada final ignorado debe ser texto.")
            suffix = value.strip().casefold()
            if not suffix:
                continue
            if len(suffix) > 255 or "\x00" in suffix:
                raise ValueError("Hay un final ignorado no valido.")
            if suffix not in seen:
                seen.add(suffix)
                normalized.append(suffix)
        if len(normalized) > 256:
            raise ValueError("Hay demasiados finales ignorados.")
        return tuple(normalized)

    def _load_ignored_movies_suffixes(
        self,
    ) -> Tuple[
        Tuple[str, ...],
        float,
        Tuple[Tuple[float, Tuple[str, ...]], ...],
    ]:
        return self._load_watcher_suffixes("movies")

    @staticmethod
    def _watcher_profile(profile: str) -> str:
        normalized = str(profile or "movies").strip().lower()
        if normalized not in {"movies", "tv"}:
            raise ValueError("El perfil del vigilante debe ser movies o tv.")
        return normalized

    def _watcher_snapshot(
        self, profile: str
    ) -> Tuple[
        Tuple[str, ...],
        float,
        Tuple[Tuple[float, Tuple[str, ...]], ...],
    ]:
        normalized = self._watcher_profile(profile)
        with self._watcher_rules_locks[normalized]:
            return (
                self._watcher_rules_snapshot
                if normalized == "movies"
                else self._tv_watcher_rules_snapshot
            )

    def _load_watcher_suffixes(
        self, profile: str
    ) -> Tuple[
        Tuple[str, ...],
        float,
        Tuple[Tuple[float, Tuple[str, ...]], ...],
    ]:
        normalized = self._watcher_profile(profile)
        stored = self.db.get_setting(self._watcher_setting_key(normalized))
        return self._decode_watcher_suffixes(normalized, stored)

    @staticmethod
    def _watcher_setting_key(profile: str) -> str:
        return (
            WATCHER_RULES_SETTING_KEY
            if profile == "movies"
            else TV_WATCHER_RULES_SETTING_KEY
        )

    @staticmethod
    def _watcher_defaults(profile: str) -> Tuple[str, ...]:
        return (
            DEFAULT_IGNORED_MOVIES_SUFFIXES
            if profile == "movies"
            else DEFAULT_IGNORED_TV_SUFFIXES
        )

    def _decode_watcher_suffixes(
        self,
        profile: str,
        stored: Optional[str],
    ) -> Tuple[
        Tuple[str, ...],
        float,
        Tuple[Tuple[float, Tuple[str, ...]], ...],
    ]:
        defaults = self._watcher_defaults(profile)
        if stored is None:
            return (
                defaults,
                0.0,
                ((0.0, defaults),),
            )
        try:
            payload = json.loads(stored)
            if isinstance(payload, list):
                suffixes = self._normalize_ignored_movies_suffixes(payload)
                return suffixes, 0.0, ((0.0, suffixes),)
            if not isinstance(payload, dict):
                raise ValueError("Formato de reglas no valido.")
            suffixes = self._normalize_ignored_movies_suffixes(payload.get("ignored_suffixes"))
            effective_at = max(0.0, float(payload.get("effective_at") or 0.0))
            history: List[Tuple[float, Tuple[str, ...]]] = []
            raw_history = payload.get("history")
            if raw_history is not None:
                if not isinstance(raw_history, list):
                    raise ValueError("Historial de reglas no valido.")
                for entry in raw_history:
                    if not isinstance(entry, dict):
                        raise ValueError("Historial de reglas no valido.")
                    entry_at = max(0.0, float(entry.get("effective_at") or 0.0))
                    entry_suffixes = self._normalize_ignored_movies_suffixes(
                        entry.get("ignored_suffixes")
                    )
                    history.append((entry_at, entry_suffixes))
            if not history:
                history.append((0.0, defaults))
            if history[-1] != (effective_at, suffixes):
                history.append((effective_at, suffixes))
            history.sort(key=lambda entry: entry[0])
            return suffixes, effective_at, tuple(history)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self.log.warning(
                "Reglas del vigilante invalidas; se usa el valor por defecto: %s",
                error,
            )
            return (
                defaults,
                0.0,
                ((0.0, defaults),),
            )

    def _set_watcher_snapshot(
        self,
        profile: str,
        snapshot: Tuple[
            Tuple[str, ...],
            float,
            Tuple[Tuple[float, Tuple[str, ...]], ...],
        ],
    ) -> None:
        with self._watcher_rules_locks[profile]:
            if profile == "movies":
                self._watcher_rules_snapshot = snapshot
            else:
                self._tv_watcher_rules_snapshot = snapshot

    @staticmethod
    def _watcher_fingerprint(setting_key: str, stored: Optional[str]) -> str:
        comparison_document = json.dumps(
            {"setting_key": setting_key, "value": stored},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(comparison_document.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize_watcher_rules(
        suffixes: Tuple[str, ...],
        effective_at: float,
        history: Tuple[Tuple[float, Tuple[str, ...]], ...],
    ) -> str:
        return json.dumps(
            {
                "ignored_suffixes": list(suffixes),
                "effective_at": effective_at,
                "history": [
                    {
                        "effective_at": entry_at,
                        "ignored_suffixes": list(entry_suffixes),
                    }
                    for entry_at, entry_suffixes in history
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _watcher_rules_payload(
        self,
        profile: str,
        setting_key: str,
        stored: Optional[str],
        snapshot: Tuple[
            Tuple[str, ...],
            float,
            Tuple[Tuple[float, Tuple[str, ...]], ...],
        ],
    ) -> Dict[str, object]:
        suffixes, _effective_at, _history = snapshot
        return {
            "ok": True,
            "profile": profile,
            "rules": {"ignored_suffixes": list(suffixes)},
            "fingerprint": self._watcher_fingerprint(setting_key, stored),
            "rules_path": f"{self.db.path}:settings/{setting_key}",
            "scope": str(self.config.complete_root / profile),
        }

    def watcher_rules(self, profile: str = "movies") -> Dict[str, object]:
        normalized = self._watcher_profile(profile)
        with self._watcher_rules_locks[normalized]:
            setting_key = self._watcher_setting_key(normalized)
            stored = self.db.get_setting(setting_key)
            snapshot = self._decode_watcher_suffixes(normalized, stored)
            self._set_watcher_snapshot(normalized, snapshot)
            return self._watcher_rules_payload(
                normalized,
                setting_key,
                stored,
                snapshot,
            )

    def update_watcher_rules(
        self,
        payload: Dict[str, object],
        profile: str = "movies",
        *,
        require_expected_fingerprint: bool = False,
    ) -> Dict[str, object]:
        try:
            normalized = self._watcher_profile(profile)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        rules = payload.get("rules") if isinstance(payload, dict) else None
        if not isinstance(rules, dict) or "ignored_suffixes" not in rules:
            return {"ok": False, "error": "Payload de reglas no valido."}
        try:
            suffixes = self._normalize_ignored_movies_suffixes(rules["ignored_suffixes"])
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        raw_expected = payload.get("expected_fingerprint")
        expected_fingerprint: Optional[str]
        if raw_expected is None:
            if require_expected_fingerprint:
                return {
                    "ok": False,
                    "error": "expected_fingerprint_required",
                    "message": "Falta expected_fingerprint para guardar este perfil.",
                }
            expected_fingerprint = None
        elif not isinstance(raw_expected, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", raw_expected.strip()
        ):
            return {
                "ok": False,
                "error": "expected_fingerprint_invalid",
                "message": "expected_fingerprint no es valido.",
            }
        else:
            expected_fingerprint = raw_expected.strip().lower()

        with self._watcher_rules_locks[normalized]:
            return self._update_watcher_rules_locked(
                normalized,
                suffixes,
                expected_fingerprint,
            )

    def _update_watcher_rules_locked(
        self,
        normalized: str,
        suffixes: Tuple[str, ...],
        expected_fingerprint: Optional[str],
    ) -> Dict[str, object]:
        setting_key = self._watcher_setting_key(normalized)
        attempts = 1 if expected_fingerprint is not None else 4
        for _attempt in range(attempts):
            stored = self.db.get_setting(setting_key)
            current_snapshot = self._decode_watcher_suffixes(normalized, stored)
            current_fingerprint = self._watcher_fingerprint(setting_key, stored)
            if (
                expected_fingerprint is not None
                and expected_fingerprint != current_fingerprint
            ):
                self._set_watcher_snapshot(normalized, current_snapshot)
                return {
                    "ok": False,
                    "error": "watcher_rules_conflict",
                    "message": "Las reglas cambiaron desde la ultima lectura.",
                    "current": self._watcher_rules_payload(
                        normalized,
                        setting_key,
                        stored,
                        current_snapshot,
                    ),
                }

            effective_at = time.time()
            current_history = current_snapshot[2]
            history = current_history + ((effective_at, suffixes),)
            serialized = self._serialize_watcher_rules(
                suffixes,
                effective_at,
                history,
            )
            if self.db.compare_and_set_setting_value(
                setting_key,
                stored,
                serialized,
            ):
                saved_snapshot = (suffixes, effective_at, history)
                self._set_watcher_snapshot(normalized, saved_snapshot)
                result = self._watcher_rules_payload(
                    normalized,
                    setting_key,
                    serialized,
                    saved_snapshot,
                )
                result["saved"] = True
                return result
            if expected_fingerprint is not None:
                break

        return {
            "ok": False,
            "error": "watcher_rules_conflict",
            "message": "Las reglas cambiaron durante el guardado.",
            "current": self.watcher_rules(normalized),
        }

    def identity_rules(self, profile: str = "common") -> Dict[str, object]:
        return self.identity.payload(profile)

    def update_identity_rules(
        self, payload: Dict[str, object], profile: str = "common"
    ) -> Dict[str, object]:
        return self.identity.update(payload, profile)

    def reset_identity_rules(
        self, payload: Dict[str, object], profile: str = "common"
    ) -> Dict[str, object]:
        return self.identity.reset(payload, profile)

    def clear_identity_cache(
        self, _payload: Dict[str, object], profile: str = "common"
    ) -> Dict[str, object]:
        return self.identity.clear_cache(profile)

    def test_identity_parser(
        self, payload: Dict[str, object], profile: str = "common"
    ) -> Dict[str, object]:
        return self.identity.test_parser(payload, profile)

    def test_identity_resolver(
        self, payload: Dict[str, object], profile: str = "common"
    ) -> Dict[str, object]:
        return self.identity.test_resolver(payload, profile)

    def _new_job_source_meta_json(
        self,
        *,
        identity_context: Optional[Dict[str, object]] = None,
        category: Optional[str] = None,
        name: str = "",
    ) -> str:
        normalized_category = str(category or "common").strip().lower()
        return json.dumps(
            {
                "identity_rules": (
                    identity_context
                    if isinstance(identity_context, dict)
                    else self.identity.job_snapshot_for_category(category)
                ),
                "series_pipeline": self._new_series_pipeline_snapshot(
                    normalized_category,
                    name,
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _new_series_pipeline_snapshot(
        self,
        category: str,
        name: str,
        *,
        configured_mode: Optional[str] = None,
    ) -> Dict[str, object]:
        normalized = str(category or "common").strip().lower()
        mode = str(configured_mode or self.config.series_mode).strip().lower()
        if mode not in {"legacy", "canary", "active"}:
            raise ValueError("Modo de Series no valido")
        canary_eligible = str(name or "").casefold().startswith(SERIES_CANARY_PREFIX)
        if normalized == "tv":
            selected = mode == "active" or (mode == "canary" and canary_eligible)
            route = "series-worker" if selected else "legacy"
            profile = "tv"
        elif normalized == "movies":
            route = "not-applicable"
            profile = "movies"
        else:
            route = "pending"
            profile = "common"
        return {
            "schema": SERIES_PIPELINE_SCHEMA,
            "profile": profile,
            "configured_mode": mode,
            "canary_eligible": canary_eligible,
            "route": route,
        }

    @staticmethod
    def _source_meta(job: Dict[str, object]) -> Dict[str, object]:
        raw = job.get("source_meta_json")
        if isinstance(raw, dict):
            return dict(raw)
        try:
            payload = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _series_source_meta(job: Dict[str, object]) -> Dict[str, object]:
        """Lee metadatos para enrutar TV sin convertir corrupción en legado."""

        raw = job.get("source_meta_json")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return {}
        if isinstance(raw, dict):
            return dict(raw)
        try:
            payload = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Metadatos del snapshot de Series no son JSON valido") from error
        if not isinstance(payload, dict):
            raise ValueError("Metadatos del snapshot de Series no son un objeto")
        return dict(payload)

    def _series_pipeline_for_job(self, job: Dict[str, object]) -> Dict[str, object]:
        source_meta = self._series_source_meta(job)
        pipeline = source_meta.get("series_pipeline")
        if pipeline is None:
            identity_snapshot = source_meta.get("identity_rules")
            if isinstance(identity_snapshot, dict) and identity_snapshot.get(
                "profile"
            ) in {"common", "movies", "tv"}:
                raise ValueError("Trabajo nuevo sin snapshot de ruta de Series")
            return {
                "schema": SERIES_PIPELINE_SCHEMA,
                "profile": "tv" if str(job.get("category") or "") == "tv" else "common",
                "configured_mode": "legacy",
                "canary_eligible": False,
                "route": "legacy",
                "source": "historical_fallback",
            }
        if not isinstance(pipeline, dict):
            raise ValueError("Snapshot de Series no valido")
        normalized = dict(pipeline)
        mode = str(normalized.get("configured_mode") or "")
        route = str(normalized.get("route") or "")
        profile = str(normalized.get("profile") or "")
        if (
            normalized.get("schema") != SERIES_PIPELINE_SCHEMA
            or mode not in {"legacy", "canary", "active"}
            or route not in {"legacy", "series-worker", "pending", "not-applicable"}
            or profile not in {"common", "movies", "tv"}
            or not isinstance(normalized.get("canary_eligible"), bool)
        ):
            raise ValueError("Snapshot de Series contradictorio")
        if str(job.get("category") or "") == "tv" and (
            profile != "tv" or route not in {"legacy", "series-worker"}
        ):
            raise ValueError("Snapshot de Series no esta congelado para TV")
        if str(job.get("category") or "") == "tv":
            expected = self._new_series_pipeline_snapshot(
                "tv",
                str(job.get("name") or ""),
                configured_mode=mode,
            )
            if any(
                normalized.get(key) != expected.get(key)
                for key in (
                    "schema",
                    "profile",
                    "configured_mode",
                    "canary_eligible",
                    "route",
                )
            ):
                raise ValueError("Snapshot de Series contradice la ruta congelada")
        return normalized

    def _series_selected_for_job(self, job: Dict[str, object]) -> bool:
        return bool(
            str(job.get("category") or "") == "tv"
            and self._series_pipeline_for_job(job).get("route") == "series-worker"
        )

    def _series_full_pipeline_owner(self) -> Optional[str]:
        """Devuelve el unico trabajo autorizado a avanzar por el flujo TV.

        La cola se decide con estado persistido, no con un lock en memoria, para
        conservar el mismo propietario despues de reiniciar el orquestador.
        """

        candidates: List[Dict[str, object]] = []
        for candidate in self.db.jobs_in_states(SERIES_FULL_PIPELINE_STATES, 500):
            try:
                if self._series_selected_for_job(candidate):
                    candidates.append(candidate)
            except ValueError:
                # Un snapshot corrupto debe llegar a su manejo de error sin
                # secuestrar la cola de trabajos validos.
                continue
        if not candidates:
            return None

        def order(candidate: Dict[str, object]) -> Tuple[int, float, str]:
            state = str(candidate.get("state") or "")
            # Un trabajo que ya salio de ready_stage conserva la propiedad
            # aunque exista otro mas antiguo que aun no haya creado taller.
            active_rank = 1 if state == "ready_stage" else 0
            return (
                active_rank,
                float(candidate.get("created_at") or 0.0),
                str(candidate.get("job_id") or ""),
            )

        return str(min(candidates, key=order).get("job_id") or "") or None

    def _series_waits_for_full_pipeline(self, job: Dict[str, object]) -> bool:
        if (
            not self.config.active
            or str(job.get("state") or "") not in SERIES_FULL_PIPELINE_STATES
        ):
            return False
        try:
            if not self._series_selected_for_job(job):
                return False
        except ValueError:
            return False
        job_id = str(job["job_id"])
        owner = self._series_full_pipeline_owner()
        if owner in {None, job_id}:
            if str(job.get("last_error_code") or "") == "series_worker_pipeline_queued":
                self.db.update_job(
                    job_id,
                    last_error_code=None,
                    last_error_message=None,
                )
            return False
        self._record_series_wait(
            job,
            "pipeline_queued",
            "Otro episodio de Series conserva la cola completa hasta terminar",
        )
        return True

    def _ignored_movies_item(
        self,
        item: Path,
        suffixes: Optional[Tuple[str, ...]] = None,
    ) -> bool:
        return self._ignored_watcher_item("movies", item, suffixes)

    def _ignored_watcher_item(
        self,
        category: str,
        item: Path,
        suffixes: Optional[Tuple[str, ...]] = None,
    ) -> bool:
        if category not in {"movies", "tv"}:
            return False
        if suffixes is None:
            suffixes, _effective_at, _history = self._watcher_snapshot(category)
        if not suffixes:
            return False
        category_root = self.config.complete_root / category
        try:
            item.resolve().relative_to(category_root.resolve())
        except (OSError, ValueError):
            return False
        # Conserva la regla historica del elemento superior, sea archivo o carpeta.
        if item.name.casefold().endswith(suffixes):
            return True
        if not item.is_dir():
            return False
        try:
            for candidate in item.rglob("*"):
                if candidate.is_file() and candidate.name.casefold().endswith(suffixes):
                    return True
        except OSError as error:
            self.log.warning("No se pudo revisar el contenido de %s: %s", item, error)
        return False

    def _ignored_movies_job(self, job: Dict[str, object], source_path: Path) -> bool:
        return self._ignored_watcher_job(job, source_path)

    def _ignored_watcher_job(self, job: Dict[str, object], source_path: Path) -> bool:
        category = str(job.get("category") or "")
        if category not in {"movies", "tv"}:
            return False
        _current_suffixes, _effective_at, history = self._watcher_snapshot(category)
        created_at = float(job.get("created_at") or 0.0)
        suffixes = (
            DEFAULT_IGNORED_MOVIES_SUFFIXES
            if category == "movies"
            else DEFAULT_IGNORED_TV_SUFFIXES
        )
        for entry_at, entry_suffixes in history:
            if entry_at > created_at:
                break
            suffixes = entry_suffixes
        return self._ignored_watcher_item(category, source_path, suffixes)

    def start(self) -> None:
        self._check_dependencies()
        self._recover_interrupted_jobs()
        self._activate_dry_run_jobs()
        self._start_watchers()
        self.log.info("Motor iniciado en modo %s", self.config.mode)
        while self.running:
            self._heartbeat()
            self._drain_events()
            now = time.time()
            if now - self._last_reconcile >= self.config.reconcile_seconds:
                self.reconcile()
                self._last_reconcile = now
            self.process_jobs()
            time.sleep(0.5)

    def stop(self) -> None:
        self.running = False
        self.observer.stop()
        self.observer.join(timeout=10)

    def status(self) -> Dict[str, object]:
        identity_profiles = {
            profile: self.identity.payload(profile)
            for profile in ("common", "movies", "tv")
        }
        identity_rules = identity_profiles["common"]
        return {
            "status": "ok",
            "mode": self.config.mode,
            "series_mode": self.config.series_mode,
            "heartbeat": self._last_heartbeat,
            "dependencies": self.dependencies,
            "identity_rules": {
                "revision": identity_rules.get("revision"),
                "fingerprint": identity_rules.get("fingerprint"),
                "profiles": {
                    profile: {
                        "revision": payload.get("revision"),
                        "fingerprint": payload.get("fingerprint"),
                    }
                    for profile, payload in identity_profiles.items()
                },
            },
            "queue_size": self.events.qsize(),
        }

    def _manifest_summary(self, entries: List[Dict[str, object]]) -> Dict[str, object]:
        total_size = sum(int(entry.get("size") or 0) for entry in entries)
        largest = sorted(
            entries,
            key=lambda entry: int(entry.get("size") or 0),
            reverse=True,
        )[:3]
        return {
            "files": len(entries),
            "total_size": total_size,
            "largest": [
                {
                    "path": str(entry.get("path") or ""),
                    "size": int(entry.get("size") or 0),
                }
                for entry in largest
            ],
        }

    def _log_stability_wait(
        self,
        job: Dict[str, object],
        event_type: str,
        message: str,
        entries: List[Dict[str, object]],
        now: float,
        stable_since: float,
        extra: Optional[Dict[str, object]] = None,
    ) -> None:
        job_id = str(job["job_id"])
        payload = {
            "state": "waiting_stable",
            "category": job["category"],
            "source_path": job["source_path"],
            "stable_seconds_required": self.config.stable_seconds,
            "stable_seconds_current": round(max(0.0, now - stable_since), 1),
            **self._manifest_summary(entries),
        }
        if extra:
            payload.update(extra)
        self._stable_log_at[job_id] = now
        self.db.add_event(job_id, "stability", event_type, message, payload)

    def _wait_for_missing_source(
        self,
        job: Dict[str, object],
        now: Optional[float] = None,
    ) -> None:
        job_id = str(job["job_id"])
        checked_at = time.time() if now is None else now
        missing_since = self._missing_source_since.get(job_id)
        grace_seconds = max(1, int(self.config.missing_source_grace_seconds))
        if missing_since is None:
            self._missing_source_since[job_id] = checked_at
            self.db.add_event(
                job_id,
                "stability",
                "warning",
                "Origen no disponible; se inicia margen antes de descartar",
                {
                    "state": "waiting_stable",
                    "category": job.get("category"),
                    "source_path": job.get("source_path"),
                    "missing_source_grace_seconds": grace_seconds,
                },
            )
            return
        missing_seconds = max(0.0, checked_at - missing_since)
        if missing_seconds < grace_seconds:
            return
        self._missing_source_since.pop(job_id, None)
        self._stable_log_at.pop(job_id, None)
        self._stable.pop(job_id, None)
        self.db.transition(
            job_id,
            "discarded",
            "stability",
            "Origen ausente tras el margen de seguridad; trabajo descartado",
            last_error_code="source_missing_after_grace",
            last_error_message=(
                f"El origen no reaparecio durante {grace_seconds} segundos"
            ),
        )

    def _clear_missing_source_wait(self, job: Dict[str, object]) -> None:
        job_id = str(job["job_id"])
        missing_since = self._missing_source_since.pop(job_id, None)
        if missing_since is None:
            return
        self.db.add_event(
            job_id,
            "stability",
            "decision",
            "El origen ha reaparecido; continua la comprobacion de estabilidad",
            {
                "state": "waiting_stable",
                "category": job.get("category"),
                "missing_seconds": round(max(0.0, time.time() - missing_since), 1),
            },
        )

    def _heartbeat(self) -> None:
        self._last_heartbeat = time.time()
        (self.config.config_dir / "heartbeat").write_text(
            str(self._last_heartbeat), encoding="ascii"
        )
        if self._last_heartbeat - self._last_series_dependency_check >= 30.0:
            self._schedule_series_worker_dependency_refresh()

    def _schedule_series_worker_dependency_refresh(self) -> None:
        if self._series_dependency_refreshing:
            return
        self._series_dependency_refreshing = True
        self._last_series_dependency_check = time.time()
        threading.Thread(
            target=self._refresh_series_worker_dependency,
            name="arr-series-worker-health",
            daemon=True,
        ).start()

    def _refresh_series_worker_dependency(self) -> None:
        try:
            try:
                self.dependencies["series-worker"] = self.series_worker.version()
            except Exception as error:
                self.dependencies["series-worker"] = f"error: {error}"
        finally:
            self._series_dependency_refreshing = False

    def _check_dependencies(self) -> None:
        for name, client in (("qbittorrent", self.qbt), ("rdtclient", self.rdt)):
            try:
                self.dependencies[name] = client.version()
            except Exception as error:
                self.dependencies[name] = f"error: {error}"
                self.log.warning("%s no disponible al arrancar: %s", name, error)
        try:
            self.dependencies["media-worker"] = self.media_worker.version()
        except Exception as error:
            self.dependencies["media-worker"] = f"error: {error}"
            self.log.warning("media-worker no disponible al arrancar: %s", error)
        try:
            self.dependencies["series-worker"] = self.series_worker.version()
        except Exception as error:
            self.dependencies["series-worker"] = f"error: {error}"
            self.log.warning("series-worker no disponible al arrancar: %s", error)
        self._last_series_dependency_check = time.time()
        self.dependencies["name-resolver"] = (
            "configured" if self.name_resolver.enabled else "legacy: TMDB_API_TOKEN missing"
        )

    def _start_watchers(self) -> None:
        watched = [
            (self.config.watch_inbox, "watch"),
            (self.config.event_dir, "qbt_event"),
        ]
        watched.extend((self.config.complete_root / category, "complete") for category in COMPLETE_CATEGORIES)
        for path, event_type in watched:
            self.observer.schedule(
                EventHandler(self.events, event_type), str(path), recursive=True
            )
        self.observer.start()

    def _drain_events(self) -> None:
        for _ in range(500):
            try:
                event_type, path = self.events.get_nowait()
            except queue.Empty:
                return
            try:
                if event_type == "watch":
                    self._handle_watch_path(path)
                elif event_type == "qbt_event":
                    self._handle_qbt_event(path)
                elif event_type == "complete":
                    self._handle_complete_path(path)
            except Exception:
                self.log.exception("Error manejando evento %s: %s", event_type, path)

    def reconcile(self) -> None:
        self._reconcile_watch_inbox()
        self._reconcile_qbt_events()
        self._reconcile_qbt()
        self._reconcile_rdt()
        self._reconcile_complete()
        self._reconcile_late_worker_results()

    def _reconcile_watch_inbox(self) -> None:
        for path in self.config.watch_inbox.rglob("*.torrent"):
            self._handle_watch_path(path)

    def _reconcile_qbt_events(self) -> None:
        for path in self.config.event_dir.glob("*.event"):
            self._handle_qbt_event(path)

    def _reconcile_complete(self) -> None:
        for category in COMPLETE_CATEGORIES:
            root = self.config.complete_root / category
            if not root.exists():
                continue
            for item in root.iterdir():
                self._register_materialized(category, item)

    def _reconcile_qbt(self) -> None:
        try:
            torrents = self.qbt.torrents("completed")
        except Exception as error:
            self.dependencies["qbittorrent"] = f"error: {error}"
            return
        self.dependencies["qbittorrent"] = "ok"
        for torrent in torrents:
            infohash = str(torrent.get("hash") or "").lower()
            content_path = Path(str(torrent.get("content_path") or ""))
            if not infohash or not content_path.exists():
                continue
            source_path = self._qbt_materialized_source(content_path)
            if not source_path:
                continue
            job = self._job_for_qbt_content(infohash, source_path, content_path)
            identity_context = self.identity.rules_for_job(job) if job else None
            classification_context = (
                identity_context
                if identity_context is not None
                else self.identity.classification_snapshot()
            )
            identity_rules = dict(classification_context.get("rules") or {})
            category = self._category(
                str(torrent.get("category") or ""),
                str(torrent.get("name") or ""),
                identity_rules,
            )
            if not job and self._ignored_watcher_item(category, source_path):
                continue
            if not job:
                identity_context = self.identity.job_snapshot_for_category(category)
                job = self.db.create_job(
                    self._new_source_uid("qbt", infohash),
                    "qbt",
                    category,
                    str(torrent.get("name") or content_path.name),
                    state="waiting_stable",
                    infohash=infohash,
                    qbt_hash=infohash,
                    source_path=str(source_path),
                    submitted_at=float(torrent.get("added_on") or time.time()),
                    source_meta_json=self._new_job_source_meta_json(
                        identity_context=identity_context,
                        category=category,
                        name=str(torrent.get("name") or content_path.name),
                    ),
                )
            elif job["state"] not in TERMINAL_STATES:
                self._attach_qbt_identity(
                    job,
                    infohash,
                    category,
                    source_path,
                    content_path,
                    float(torrent.get("added_on") or time.time()),
                    "Descarga qBittorrent correlacionada con trabajo existente",
                )

    def _reconcile_rdt(self) -> None:
        try:
            torrents = self.rdt.torrents("all")
        except Exception as error:
            self.dependencies["rdtclient"] = f"error: {error}"
            return
        self.dependencies["rdtclient"] = "ok"
        for torrent in torrents:
            infohash = str(torrent.get("hash") or "").lower()
            if not infohash:
                continue
            job = self.db.get_active_job_by_infohash(infohash)
            if not job:
                continue
            updates = {
                "rdt_id": str(torrent.get("id") or torrent.get("hash") or ""),
                "rdt_progress": float(torrent.get("progress") or 0),
            }
            content_path = self._translate_rdt_path(str(torrent.get("content_path") or ""))
            if content_path and content_path.exists():
                updates["source_path"] = str(content_path)
                updates["state"] = "waiting_stable"
            self.db.update_job(job["job_id"], **updates)
        self._apply_rdt_fallback()

    def _apply_rdt_fallback(self) -> None:
        now = time.time()
        jobs = self.db.jobs_in_states(["source_submitted", "waiting_materialization"], 500)
        for job in jobs:
            if job["origin"] != "rdt" or not job.get("submitted_at"):
                continue
            if now - float(job["submitted_at"]) < self.config.fallback_seconds:
                continue
            torrent_path = Path(job.get("torrent_path") or "")
            if not torrent_path.exists():
                self.db.add_event(
                    job["job_id"],
                    "fallback",
                    "blocked",
                    "No existe el torrent original para fallback qB",
                )
                continue
            try:
                self._submit_qbt(job, torrent_path)
            except Exception as error:
                self.db.update_job(
                    job["job_id"],
                    retry_source=int(job["retry_source"] or 0) + 1,
                    last_error_code="qbt_fallback_failed",
                    last_error_message=str(error),
                )

    def _handle_watch_path(self, path: Path) -> None:
        if path.suffix.lower() != ".torrent" or not path.is_file():
            return
        try:
            infohash, name = torrent_info(path)
        except (OSError, ValueError):
            return
        job = self.db.get_active_job_by_infohash(infohash)
        if not job:
            classification_context = self.identity.classification_snapshot()
            identity_rules = dict(classification_context.get("rules") or {})
            category = self._watch_category(path, name, identity_rules)
            identity_context = self.identity.job_snapshot_for_category(category)
            job = self.db.create_job(
                self._new_source_uid("torrent", infohash),
                "watch",
                category,
                name,
                infohash=infohash,
                torrent_path=str(path),
                source_meta_json=self._new_job_source_meta_json(
                    identity_context=identity_context,
                    category=category,
                    name=name,
                ),
            )
        else:
            category = str(job.get("category") or "manual")
        if self.config.active and job["state"] == "received":
            self._submit_rdt(job, path)
        elif not self.config.active and job["state"] == "received":
            self.db.add_event(
                job["job_id"],
                "dry_run",
                "planned",
                f"DRY-RUN: se enviaría primero a RDT ({category})",
                {"torrent": str(path)},
            )

    def _submit_rdt(self, job: Dict[str, object], torrent_path: Path) -> None:
        category = str(job["category"])
        fields = {
            "category": category,
            "savepath": f"/data/downloads/{category}",
            "paused": "false",
        }
        try:
            self.rdt.add_torrent(torrent_path, fields)
            archived = self._archive_torrent(torrent_path, "rd")
            self.db.transition(
                str(job["job_id"]),
                "waiting_materialization",
                "rdt",
                "Torrent enviado a RDT",
                origin="rdt",
                torrent_path=str(archived),
                submitted_at=time.time(),
            )
        except Exception as rdt_error:
            self.db.add_event(
                str(job["job_id"]), "rdt", "failed", f"RDT rechazó el alta: {rdt_error}"
            )
            self._submit_qbt(job, torrent_path)

    def _submit_qbt(self, job: Dict[str, object], torrent_path: Path) -> None:
        category = str(job["category"])
        fields = {
            "category": category,
            "savepath": f"/data/downloads/torrents/complete/{category}",
            "downloadPath": f"/data/downloads/torrents/incomplete/{category}",
            "useDownloadPath": "true",
            "paused": "false",
            "autoTMM": "false",
        }
        self.qbt.add_torrent(torrent_path, fields)
        archived = self._archive_torrent(torrent_path, "qbit")
        self.db.transition(
            str(job["job_id"]),
            "waiting_materialization",
            "qbt",
            "Torrent enviado a qBittorrent",
            origin="qbt",
            qbt_hash=str(job["infohash"]),
            torrent_path=str(archived),
            submitted_at=time.time(),
        )

    def _archive_torrent(self, path: Path, engine: str) -> Path:
        destination_root = self.config.processed_root / engine
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / path.name
        if destination.exists() and destination.resolve() != path.resolve():
            destination = destination_root / f"{path.stem}__{int(time.time())}{path.suffix}"
        if path.resolve() != destination.resolve():
            shutil.move(str(path), str(destination))
        return destination

    def _handle_qbt_event(self, path: Path) -> None:
        if path.suffix != ".event" or not path.is_file():
            return
        content = path.read_text(encoding="utf-8", errors="replace")
        fields = dict(
            line.split("=", 1)
            for line in content.splitlines()
            if "=" in line
        )
        infohash = fields.get("hash", "").strip().lower()
        if len(infohash) < 32:
            self.log.warning("Evento qB inválido: %s -> %r", path, infohash)
            return
        torrent = self.qbt.torrent(infohash)
        if not torrent:
            return
        progress = float(torrent.get("progress") or 0)
        completion_on = int(torrent.get("completion_on") or 0)
        if progress < 0.999 and completion_on <= 0:
            self.log.warning("Evento qB descartado porque el torrent no está completo: %s", infohash)
            path.unlink(missing_ok=True)
            return
        content_path = Path(str(torrent.get("content_path") or ""))
        source_path = self._qbt_materialized_source(content_path) if content_path.exists() else None
        if not source_path:
            self.log.warning(
                "Evento qB aplazado: contenido terminado aún no visible en complete: %s",
                content_path,
            )
            return
        job = self._job_for_qbt_content(infohash, source_path, content_path)
        identity_context = self.identity.rules_for_job(job) if job else None
        classification_context = (
            identity_context
            if identity_context is not None
            else self.identity.classification_snapshot()
        )
        identity_rules = dict(classification_context.get("rules") or {})
        category = self._category(
            str(torrent.get("category") or ""),
            str(torrent.get("name") or ""),
            identity_rules,
        )
        if not job and self._ignored_watcher_item(category, source_path):
            path.unlink(missing_ok=True)
            return
        if not job:
            identity_context = self.identity.job_snapshot_for_category(category)
            job = self.db.create_job(
                self._new_source_uid("qbt", infohash),
                "qbt",
                category,
                str(torrent.get("name") or content_path.name),
                state="waiting_stable",
                infohash=infohash,
                qbt_hash=infohash,
                source_path=str(source_path),
                submitted_at=float(torrent.get("added_on") or time.time()),
                source_meta_json=self._new_job_source_meta_json(
                    identity_context=identity_context,
                    category=category,
                    name=str(torrent.get("name") or content_path.name),
                ),
            )
        else:
            self._attach_qbt_identity(
                job,
                infohash,
                category,
                source_path,
                content_path,
                float(torrent.get("added_on") or time.time()),
                "Evento de finalización recibido de qBittorrent",
            )
        path.unlink(missing_ok=True)

    def _handle_complete_path(self, path: Path) -> None:
        for category in COMPLETE_CATEGORIES:
            root = self.config.complete_root / category
            item = top_level_item(root, path)
            if item:
                self._register_materialized(category, item)
                return

    def _register_materialized(self, category: str, item: Path) -> None:
        if not item.exists():
            return
        if category == "trailers_automatizacion":
            ready_source = trailer_ready_source(item)
            if not ready_source:
                return
            item = ready_source
        job = self.db.get_job_by_source_path(str(item))
        if not job:
            job = self._job_for_materialized(category, item)
        if not job and self._ignored_watcher_item(category, item):
            return
        if not job:
            source_uid = self._new_source_uid(f"fs:{category}", item.name)
            job = self.db.create_job(
                source_uid,
                "fs",
                category,
                item.name,
                state="waiting_stable",
                source_path=str(item),
                source_meta_json=self._new_job_source_meta_json(
                    category=category,
                    name=item.name,
                ),
            )
        elif job["state"] not in TERMINAL_STATES:
            job = self._freeze_category_identity_snapshot(job, category)
            job = self.db.update_job(
                job["job_id"],
                category=category,
                source_path=str(item),
                state="waiting_stable",
            )
        if job["state"] not in TERMINAL_STATES and not job.get("qbt_hash"):
            self._adopt_qbt_for_materialized_job(job, category, item)

    def _job_for_materialized(self, category: str, item: Path) -> Optional[Dict[str, object]]:
        for job in self.db.jobs_in_states(
            ["received", "source_submitted", "waiting_materialization", "waiting_stable"], 500
        ):
            if job["category"] != category:
                continue
            if self._same_name(str(job["name"]), item.name):
                return job
        return None

    def _qbt_materialized_source(self, content_path: Path) -> Optional[Path]:
        root = self._complete_category_path(content_path)
        if not root:
            return None
        return top_level_item(root, content_path)

    def _job_for_qbt_content(
        self,
        infohash: str,
        source_path: Path,
        content_path: Path,
    ) -> Optional[Dict[str, object]]:
        job = self.db.get_active_job_by_infohash(infohash)
        if job:
            return job
        seen: set[str] = set()
        for candidate in (source_path, content_path):
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            job = self.db.get_job_by_source_path(key)
            if job:
                return job
        return None

    def _attach_qbt_identity(
        self,
        job: Dict[str, object],
        infohash: str,
        category: str,
        source_path: Path,
        content_path: Path,
        submitted_at: float,
        message: str,
    ) -> Dict[str, object]:
        job = self._freeze_category_identity_snapshot(job, category)
        job_id = str(job["job_id"])
        current_state = str(job.get("state") or "")
        materializing_states = {
            "received",
            "source_submitted",
            "waiting_materialization",
            "waiting_stable",
        }
        target_state = "waiting_stable" if current_state in materializing_states else current_state
        updates: Dict[str, object] = {}
        if str(job.get("infohash") or "").lower() != infohash:
            updates["infohash"] = infohash
        if str(job.get("qbt_hash") or "").lower() != infohash:
            updates["qbt_hash"] = infohash
        if str(job.get("category") or "") != category:
            updates["category"] = category
        if str(job.get("source_path") or "") != str(source_path):
            updates["source_path"] = str(source_path)
        if not job.get("submitted_at") and submitted_at:
            updates["submitted_at"] = submitted_at

        structured = {
            "state": target_state,
            "infohash": infohash,
            "qbt_hash": infohash,
            "category": category,
            "source_path": str(source_path),
            "content_path": str(content_path),
            "previous_source_path": str(job.get("source_path") or ""),
        }
        if target_state != current_state:
            return self.db.transition(
                job_id,
                target_state,
                "qbt",
                message,
                **updates,
            )
        if updates:
            updated = self.db.update_job(job_id, **updates)
            self.db.add_event(job_id, "qbt", "decision", message, structured)
            return updated
        return job

    def _freeze_category_identity_snapshot(
        self, job: Dict[str, object], category: str
    ) -> Dict[str, object]:
        """Congela identidad y ruta de Series al resolver movies/tv.

        Los snapshots historicos no incluian ``profile`` y se dejan intactos.
        El marcador explicito permite completar de forma segura trabajos nuevos
        que nacieron como manual y obtuvieron su categoria mas tarde.
        """

        if category not in {"movies", "tv"}:
            return job
        current = self.db.get_job(str(job["job_id"])) or job
        source_meta = self._source_meta(current)
        if not source_meta:
            return job
        stored = source_meta.get("identity_rules")
        identity_snapshot: Optional[Dict[str, object]] = None
        if isinstance(stored, dict) and stored.get("profile") == "common":
            identity_snapshot = self.identity.job_snapshot_for_category(category)
            source_meta["identity_rules"] = identity_snapshot

        pipeline = source_meta.get("series_pipeline")
        pipeline_snapshot: Optional[Dict[str, object]] = None
        if (
            isinstance(pipeline, dict)
            and pipeline.get("schema") == SERIES_PIPELINE_SCHEMA
            and pipeline.get("profile") == "common"
            and pipeline.get("route") == "pending"
        ):
            pipeline_snapshot = self._new_series_pipeline_snapshot(
                category,
                str(job.get("name") or ""),
                configured_mode=str(pipeline.get("configured_mode") or ""),
            )
            source_meta["series_pipeline"] = pipeline_snapshot

        if identity_snapshot is None and pipeline_snapshot is None:
            return job
        updated = self.db.update_job(
            str(job["job_id"]),
            source_meta_json=json.dumps(
                source_meta,
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        self.db.add_event(
            str(job["job_id"]),
            "settings",
            "decision",
            f"Perfil {category} congelado tras clasificacion",
            {
                "category": category,
                "identity_profile": category,
                "identity_revision": int(
                    (identity_snapshot or stored or {}).get("revision") or 0
                )
                if isinstance(identity_snapshot or stored, dict)
                else 0,
                "identity_fingerprint": str(
                    (identity_snapshot or stored or {}).get("fingerprint") or ""
                )
                if isinstance(identity_snapshot or stored, dict)
                else "",
                "series_pipeline": pipeline_snapshot or pipeline,
            },
        )
        return updated

    def _adopt_qbt_for_materialized_job(
        self,
        job: Dict[str, object],
        category: str,
        item: Path,
    ) -> Dict[str, object]:
        identity_context = self.identity.rules_for_job(job)
        identity_rules = dict(identity_context.get("rules") or {})
        try:
            torrents = self.qbt.torrents("completed")
        except Exception as error:
            self.dependencies["qbittorrent"] = f"error: {error}"
            return job
        self.dependencies["qbittorrent"] = "ok"
        for torrent in torrents:
            infohash = str(torrent.get("hash") or "").lower()
            content_path = Path(str(torrent.get("content_path") or ""))
            if not infohash or not content_path.exists():
                continue
            source_path = self._qbt_materialized_source(content_path)
            if not source_path or not self._same_path(source_path, item):
                continue
            return self._attach_qbt_identity(
                job,
                infohash,
                self._category(
                    str(torrent.get("category") or category),
                    str(torrent.get("name") or item.name),
                    identity_rules,
                ),
                source_path,
                content_path,
                float(torrent.get("added_on") or time.time()),
                "Descarga qBittorrent adoptada por trabajo detectado en carpeta",
            )
        return job

    def process_jobs(self) -> None:
        jobs = self.db.jobs_in_states(PROCESSABLE_STATES, 100)
        for job in jobs:
            try:
                self._process_job(job)
                updated = self.db.get_job(str(job["job_id"]))
                if updated and updated.get("state") in TERMINAL_STATES - {"discarded"}:
                    self._create_terminal_diagnostic(updated)
            except Exception as error:
                self.log.exception("Fallo procesando job %s", job["job_id"])
                self.db.update_job(
                    job["job_id"],
                    last_error_code="engine_exception",
                    last_error_message=str(error),
                )

    def _create_terminal_diagnostic(self, job: Dict[str, object]) -> None:
        try:
            result = create_codex_diagnostic(
                self.db,
                str(job["job_id"]),
                self.config.codex_diag_root,
                self._diagnostic_status(),
                force=False,
                diagnostics_root=self.config.diagnostics_root,
            )
            if result.get("ok") and result.get("created"):
                self.log.info("Informe Codex automatico generado: %s", result.get("relative"))
        except Exception as error:
            self.log.warning(
                "No se pudo generar Informe Codex automatico para %s: %s",
                job.get("job_id"),
                error,
            )
            self.db.add_event(
                str(job["job_id"]),
                "diagnostic",
                "warning",
                "No se pudo generar Informe Codex automatico",
                {"error": str(error)},
            )

    def _diagnostic_status(self) -> Dict[str, object]:
        identity_profiles = {
            profile: self.identity.payload(profile)
            for profile in ("common", "movies", "tv")
        }
        identity_rules = identity_profiles["common"]
        return {
            "orchestrator": {
                "status": "ok",
                "mode": self.config.mode,
                "series_mode": self.config.series_mode,
                "dependencies": dict(self.dependencies),
                "identity_rules": {
                    "revision": identity_rules.get("revision"),
                    "fingerprint": identity_rules.get("fingerprint"),
                    "profiles": {
                        profile: {
                            "revision": payload.get("revision"),
                            "fingerprint": payload.get("fingerprint"),
                        }
                        for profile, payload in identity_profiles.items()
                    },
                },
            },
            "media_worker": {
                "status": "ok"
                if str(self.dependencies.get("media-worker") or "").startswith("media-worker")
                else self.dependencies.get("media-worker", "-"),
            },
            "series_worker": {
                "status": "ok"
                if str(self.dependencies.get("series-worker") or "") == "ok"
                else self.dependencies.get("series-worker", "-"),
                "mode": self.config.series_mode,
            },
        }

    def _process_job(self, job: Dict[str, object]) -> None:
        source_value = str(job.get("source_path") or "").strip()
        source_path = Path(source_value) if source_value else None
        if job["state"] == "series_review_cleanup":
            self._run_series_review_cleanup(job)
            return
        if job["state"] == "series_postprocess_running":
            self._reconcile_running_series(job)
            return
        if job["state"] == "media_postprocess_running":
            self._reconcile_running_worker(job, "media")
            return
        if job["state"] == "trailer_running":
            self._reconcile_running_worker(job, "trailer")
            return
        if job["state"] == "bluray_running":
            self._reconcile_bluray_running(job)
            return
        if job["state"] != "waiting_stable" and self._series_waits_for_full_pipeline(job):
            return
        if job["state"] == "identity_retry":
            retry_at = float(job.get("identity_retry_at") or 0)
            if time.time() < retry_at:
                return
            self.db.transition(
                str(job["job_id"]),
                "ready_filebot",
                "identity",
                "Reintentando identificacion automatica",
                identity_retry_at=None,
            )
            job = self.db.get_job(str(job["job_id"]))
        if job["state"] == "waiting_stable":
            if source_path is None or not source_path.exists():
                self._wait_for_missing_source(job)
                return
            self._clear_missing_source_wait(job)
            if self._ignored_watcher_job(job, source_path):
                job_id = str(job["job_id"])
                self._stable_log_at.pop(job_id, None)
                self._stable.pop(job_id, None)
                return
            if job["category"] == "trailers_automatizacion":
                signature, entries = trailer_package_manifest(source_path)
                if not entries:
                    return
            else:
                signature, entries = manifest(source_path)
            previous = self._stable.get(str(job["job_id"]))
            now = time.time()
            job_id = str(job["job_id"])
            if not previous:
                self._stable[job_id] = (signature, now)
                self._log_stability_wait(
                    job,
                    "observed",
                    f"Estabilidad observando: {len(entries)} archivos",
                    entries,
                    now,
                    now,
                )
                return
            if previous[0] != signature:
                elapsed = now - previous[1]
                self._stable[job_id] = (signature, now)
                self._log_stability_wait(
                    job,
                    "changed",
                    f"Estabilidad reiniciada: cambio detectado tras {elapsed:.1f}s",
                    entries,
                    now,
                    now,
                    {"previous_stable_seconds": round(elapsed, 1)},
                )
                return
            elapsed = now - previous[1]
            if elapsed < self.config.stable_seconds:
                last_log = self._stable_log_at.get(job_id, 0.0)
                if now - last_log >= 30:
                    remaining = max(0.0, self.config.stable_seconds - elapsed)
                    self._log_stability_wait(
                        job,
                        "waiting",
                        f"Estabilidad esperando: faltan {remaining:.1f}s",
                        entries,
                        now,
                        previous[1],
                        {"remaining_seconds": round(remaining, 1)},
                    )
                return
            self._stable_log_at.pop(job_id, None)
            self._stable.pop(job_id, None)
            self.db.transition(
                str(job["job_id"]),
                "ready_stage",
                "stability",
                f"Contenido estable: {len(entries)} archivos",
                result_json=json.dumps({"input_manifest": entries}, ensure_ascii=False),
            )
            job = self.db.get_job(str(job["job_id"]))
        if self._series_waits_for_full_pipeline(job):
            return
        if not self.config.active:
            if job["state"] == "ready_stage":
                self.db.transition(
                    str(job["job_id"]),
                    "dry_run_ready",
                    "dry_run",
                    "DRY-RUN: contenido listo; se harían taller, extracción, FileBot y limpieza",
                )
            return
        if job["state"] == "ready_stage":
            self._run_stage(job)
            job = self.db.get_job(str(job["job_id"]))
        if job["state"] == "trailer_ready":
            self._run_trailer(job)
            return
        if job["state"] == "media_postprocess_ready":
            self._run_media_postprocess(job)
            job = self.db.get_job(str(job["job_id"]))
        if job["state"] == "series_postprocess_ready":
            retry_at = self._series_retry_at.get(str(job["job_id"]), 0.0)
            if time.time() < retry_at:
                return
            self._run_series_postprocess(job)
            job = self.db.get_job(str(job["job_id"]))
        if job["state"] == "ready_extract":
            self._run_extract(job)
            job = self.db.get_job(str(job["job_id"]))
        if job["state"] == "ready_filebot":
            self._run_filebot(job)
            job = self.db.get_job(str(job["job_id"]))
        if job["state"] == "ready_cleanup":
            self._run_cleanup(job)

    def _run_stage(self, job: Dict[str, object]) -> None:
        source = Path(str(job["source_path"]))
        job_id = str(job["job_id"])
        self._require_series_lexical_path(
            self.config.workshop_root / job_id,
            self.config.workshop_root,
            "destino de taller",
        )
        self.db.transition(job_id, "staging", "stage", "Moviendo a taller")
        if job["category"] == "trailers_automatizacion":
            job_root, source_item = move_trailer_package_into_job(
                source,
                self.config.workshop_root,
                job_id,
            )
            self.db.transition(
                str(job["job_id"]),
                "trailer_ready",
                "stage",
                "Trailer preparado para worker",
                stage_path=str(job_root),
                source_path=str(source_item),
            )
            return

        job_root = move_into_job(source, self.config.workshop_root, job_id)
        if job["category"] == "manual":
            destination = move_job_to_review_clean(job_root, self.config.review_dir, str(job["name"]))
            write_reason(
                destination,
                {
                    "job_id": job["job_id"],
                    "phase": "manual",
                    "reason": "manual_review",
                    "category": job["category"],
                    "timestamp": time.time(),
                },
                "Revision manual.txt",
                [
                    "El item entro como manual o no se pudo clasificar con seguridad.",
                    f"Categoria: {job['category']}",
                ],
            )
            self._cleanup_clients(job, strict=False)
            self.db.transition(
                str(job["job_id"]),
                "manual_review",
                "manual",
                "Enviado a revisión manual",
                stage_path=str(destination),
            )
            return
        if job["category"] == "movies_automatizacion":
            source_item = media_worker_source(job_root / "original")
            self.db.transition(
                str(job["job_id"]),
                "media_postprocess_ready",
                "stage",
                "Pelicula preparada para Media Worker",
                stage_path=str(job_root),
                source_path=str(source_item),
            )
            return
        self.db.transition(
            str(job["job_id"]),
            "ready_extract",
            "stage",
            "Taller preparado",
            stage_path=str(job_root),
        )

    def _run_extract(self, job: Dict[str, object]) -> None:
        job_root = Path(str(job["stage_path"]))
        job_id = str(job["job_id"])
        try:
            series_selected = self._series_selected_for_job(job)
        except ValueError as error:
            self._preserve_series_job_for_review(
                job,
                job_root,
                "series_pipeline_invalid",
                str(error),
                phase="extract",
            )
            return
        if series_selected:
            try:
                expected_job_root = self._require_series_lexical_path(
                    self.config.workshop_root / job_id,
                    self.config.workshop_root,
                    "taller esperado antes de extracción",
                )
                job_root = self._require_series_lexical_path(
                    job_root,
                    self.config.workshop_root,
                    "taller de Series antes de extracción",
                )
                if job_root != expected_job_root:
                    raise ValueError("El taller de Series no coincide con <taller>/<job_id>")
            except (OSError, ValueError) as error:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_extract_path_invalid",
                    str(error),
                    phase="extract",
                )
                return
        self.db.transition(job_id, "extracting", "extract", "Extracción iniciada")

        def record_extract_event(
            event_type: str,
            message: str,
            structured: Dict[str, object],
        ) -> None:
            self.db.add_event(
                job_id,
                "extract",
                event_type,
                message,
                structured,
            )
        try:
            input_root = extract_archives(job_root, event_callback=record_extract_event)
            clean_junk(input_root)
        except ExtractionError as error:
            self._finish_extraction_failure(
                job,
                job_root,
                error,
                series_selected=series_selected,
            )
            return
        except Exception as error:
            self._finish_extraction_failure(
                job,
                job_root,
                ExtractionError(
                    "extract_tool_failed", str(error), output_tail=str(error)[-2000:]
                ),
                series_selected=series_selected,
            )
            return
        self.db.transition(
            job_id,
            "ready_filebot",
            "extract",
            "Extracción terminada",
            source_path=str(input_root),
        )

    def _finish_extraction_failure(
        self,
        job: Dict[str, object],
        job_root: Path,
        error: ExtractionError,
        *,
        series_selected: bool,
    ) -> None:
        job_id = str(job["job_id"])
        error_details = _sanitize_extraction_details(job_root, error.details)
        self.db.add_event(
            job_id,
            "extract",
            "error",
            f"Extracción fallida: {error.message}",
            error_details,
        )
        if series_selected:
            self._preserve_series_job_for_review(
                job,
                job_root,
                error.code,
                error.message,
                phase="extract",
            )
            return
        failed, preservation = move_extraction_failure_to_review(
            job_root,
            self.config.review_dir,
            str(job["name"]),
        )
        preservation = _sanitize_extraction_details(job_root, preservation)
        reason = {
            "job_id": job_id,
            "phase": "extract",
            "error": error.message,
            **error_details,
            **preservation,
            "timestamp": time.time(),
        }
        write_reason(
            failed,
            reason,
            "Error de extraccion.txt",
            [error.message],
        )
        self.db.add_event(
            job_id,
            "extract",
            "decision",
            "Material técnico de extracción conservado para revisión",
            preservation,
        )
        self.db.transition(
            job_id,
            "error_terminal",
            "extract",
            f"Extracción fallida: {error.message}",
            stage_path=str(failed),
            last_error_code=error.code,
            last_error_message=error.message,
            result_json=json.dumps(reason, ensure_ascii=False, default=str),
        )

    def _run_filebot(self, job: Dict[str, object]) -> None:
        job_root = Path(str(job["stage_path"]))
        try:
            series_selected = self._series_selected_for_job(job)
        except ValueError as error:
            self._preserve_series_job_for_review(
                job,
                job_root,
                "series_pipeline_invalid",
                str(error),
                phase="settings",
            )
            return
        source_path = Path(str(job["source_path"]))
        if series_selected:
            try:
                expected_job_root = self._require_series_lexical_path(
                    self.config.workshop_root / str(job["job_id"]),
                    self.config.workshop_root,
                    "taller esperado antes de FileBot",
                )
                job_root = self._require_series_lexical_path(
                    job_root,
                    self.config.workshop_root,
                    "taller de Series antes de FileBot",
                )
                if job_root != expected_job_root:
                    raise ValueError("El taller de Series no coincide con <taller>/<job_id>")
                source_path = self._require_series_lexical_path(
                    source_path,
                    job_root,
                    "entrada de Series antes de FileBot",
                )
            except (OSError, ValueError) as error:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_filebot_path_invalid",
                    str(error),
                    phase="filebot",
                )
                return
        input_root = prepare_filebot_input(
            source_path,
            job_root,
            str(job.get("name") or ""),
        )
        if series_selected:
            try:
                input_root = self._require_series_lexical_path(
                    input_root,
                    job_root,
                    "entrada preparada de Series",
                )
            except (OSError, ValueError) as error:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_filebot_path_invalid",
                    str(error),
                    phase="filebot",
                )
                return
        identity_context = self.identity.configure_for_job(job)
        identity_rules = dict(identity_context.get("rules") or {})
        filebot_identity_configure = getattr(
            self.filebot, "configure_identity_rules", None
        )
        if callable(filebot_identity_configure):
            filebot_identity_configure(identity_rules)
        self.db.add_event(
            str(job["job_id"]),
            "settings",
            "decision",
            (
                "Configuracion de identidad revision "
                f"{int(identity_context.get('revision') or 0)}"
            ),
            {
                "identity_revision": int(identity_context.get("revision") or 0),
                "identity_fingerprint": str(identity_context.get("fingerprint") or ""),
                "identity_source": str(identity_context.get("source") or "job_snapshot"),
                "revision": int(identity_context.get("revision") or 0),
                "resolver_fingerprint": str(identity_context.get("fingerprint") or ""),
                "category": str(job.get("category") or ""),
            },
        )
        identity: Optional[ResolvedIdentity] = None
        series_expected_episode_codes: set[Tuple[int, int]] = set()
        series_expected_episode_groups: List[Tuple[Tuple[int, int], ...]] = []
        series_expected_physical_manifest: List[
            Tuple[int, int, int, str, Tuple[Tuple[int, int], ...]]
        ] = []
        media_decision = self._media_decision_for_job(job, input_root, identity_rules)
        self.db.add_event(
            str(job["job_id"]),
            "identity",
            "decision",
            f"Decision local: {media_decision.media_type} ({media_decision.confidence})",
            {"media_decision": media_decision.to_dict()},
        )
        if media_decision.block_reason == "category_conflict":
            if series_selected:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "category_conflict",
                    "Conflicto fuerte entre la categoría TV y el nombre detectado",
                    phase="identity",
                )
                return
            review = move_job_to_review_clean(job_root, self.config.review_dir, str(job["name"]))
            reason = {
                "job_id": job["job_id"],
                "phase": "identity",
                "reason": "category_conflict",
                "category": job["category"],
                "media_decision": media_decision.to_dict(),
                "timestamp": time.time(),
            }
            parsed = media_decision.parsed
            write_reason(
                review,
                reason,
                "Revision manual.txt",
                [
                    "Conflicto fuerte entre la categoria del trabajo y el nombre detectado.",
                    f"Categoria actual: {job['category']}",
                    f"Parser detecta: {media_decision.media_type}",
                    f"Motivos: {', '.join(media_decision.reason_codes)}",
                    "No se ejecuta TMDb ni FileBot para evitar un renombrado incorrecto.",
                ],
            )
            self._cleanup_clients(job, strict=False)
            self.db.transition(
                str(job["job_id"]),
                "manual_review",
                "identity",
                "Conflicto de categoria antes de FileBot",
                stage_path=str(review),
                last_error_code="category_conflict",
                last_error_message=parsed.category_conflict if parsed else None,
                result_json=json.dumps(reason, ensure_ascii=False, default=str),
            )
            return
        if media_decision.block_reason == "no_usable_title":
            if series_selected:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    media_decision.block_reason,
                    "La decisión local no encontró un título TV utilizable",
                    phase="identity",
                )
                return
            self._send_media_decision_review(job, job_root, media_decision)
            return
        if self.name_resolver.enabled:
            try:
                identity = self.name_resolver.resolve(job, input_root)
            except ResolverUnavailable as error:
                self._defer_identity(job, input_root, error)
                return
            except ResolverAmbiguous as error:
                if self._can_continue_tv_without_identity(job, media_decision):
                    self.db.add_event(
                        str(job["job_id"]),
                        "identity",
                        "warning",
                        "TMDb no confirma, pero se continua por senal TV local",
                        {
                            "media_decision": media_decision.to_dict(),
                            "resolver_error": str(error),
                            "resolver_details": error.details,
                        },
                    )
                    self.db.update_job(
                        str(job["job_id"]),
                        last_error_code=None,
                        last_error_message=None,
                    )
                else:
                    if series_selected:
                        self._preserve_series_job_for_review(
                            job,
                            job_root,
                            "identity_ambiguous",
                            str(error),
                            phase="identity",
                        )
                        return
                    self._send_identity_review(job, job_root, error)
                    return
            except ResolutionError as error:
                self._defer_identity(job, input_root, error)
                return
            if identity:
                self.db.update_job(
                    str(job["job_id"]),
                    identity_json=json.dumps(identity.to_dict(), ensure_ascii=False),
                    identity_retry_at=None,
                    last_error_code=None,
                    last_error_message=None,
                )
                self.db.add_event(
                    str(job["job_id"]),
                    "identity",
                    "resolved",
                    f"Identidad confirmada: TMDb {identity.tmdb_id} - {identity.title}",
                    identity.to_dict(),
                )
        else:
            self.db.add_event(
                str(job["job_id"]),
                "identity",
                "legacy",
                "TMDB_API_TOKEN no configurado; se mantiene AMC",
            )
        if series_selected:
            input_media = media_files(input_root)
            series_expected_episode_groups, unclassified_input = (
                self._series_episode_groups(input_media)
            )
            series_expected_episode_codes, unclassified_input = (
                self._series_episode_manifest(input_media)
            )
            if not input_media or unclassified_input or not series_expected_episode_codes:
                details = ", ".join(unclassified_input[:8]) or "sin codigos SxxExx"
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_input_episode_manifest_invalid",
                    f"No se puede congelar el pack de episodios antes de FileBot: {details}",
                    phase="verify",
                )
                return
            input_files = self._series_remaining_input_files(input_root)
            series_expected_physical_manifest = self._series_bound_manifest(
                input_files,
            )
            if len(series_expected_physical_manifest) != len(input_files):
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_input_physical_manifest_invalid",
                    "No se puede congelar la identidad fisica completa del pack",
                    phase="verify",
                )
                return
            if identity is not None and identity.season is not None and identity.episodes:
                identity_codes = {
                    (int(identity.season), int(episode)) for episode in identity.episodes
                }
                if identity_codes != series_expected_episode_codes:
                    self._preserve_series_job_for_review(
                        job,
                        job_root,
                        "series_identity_episode_input_mismatch",
                        "La identidad TV congelada no coincide con los episodios de entrada",
                        phase="identity",
                    )
                    return
            self.db.add_event(
                str(job["job_id"]),
                "verify",
                "decision",
                "Pack de episodios congelado antes de FileBot",
                {
                    "episode_codes": self._format_episode_codes(
                        series_expected_episode_codes
                    ),
                    "media_files": len(input_media),
                },
            )
        if job["category"] == "movies" and full_bluray_folders(input_root):
            normalized_input = self._normalize_bluray_before_filebot(
                job,
                job_root,
                input_root,
            )
            if normalized_input is None:
                return
            input_root = normalized_input
        if job["category"] == "movies":
            output_root = job_root / "filebot_output"
        elif series_selected:
            output_root = job_root / "series_filebot_output"
        else:
            output_root = self.config.tv_output
        if series_selected:
            try:
                output_root = self._require_series_lexical_path(
                    output_root,
                    job_root,
                    "salida provisional de Series",
                )
            except (OSError, ValueError) as error:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_filebot_path_invalid",
                    str(error),
                    phase="filebot",
                )
                return
        if series_selected and output_root.exists() and any(output_root.iterdir()):
            self._preserve_series_job_for_review(
                job,
                job_root,
                "series_filebot_output_not_empty",
                "La salida provisional de FileBot ya contenia datos antes de empezar",
                phase="filebot",
            )
            return
        output_root.mkdir(parents=True, exist_ok=True)
        if series_selected:
            try:
                output_root = self._require_series_lexical_path(
                    output_root,
                    job_root,
                    "salida provisional de Series",
                )
            except (OSError, ValueError) as error:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_filebot_path_invalid",
                    str(error),
                    phase="filebot",
                )
                return
        command_preview = self._filebot_command_preview(
            str(job["job_id"]),
            str(job["category"]),
            input_root,
            output_root,
            identity,
        )
        command_preview["identity_rules_revision"] = int(identity_context.get("revision") or 0)
        command_preview["identity_fingerprint"] = str(identity_context.get("fingerprint") or "")
        command_preview["rules_revision"] = int(identity_context.get("revision") or 0)
        command_preview["resolver_fingerprint"] = str(
            identity_context.get("fingerprint") or ""
        )
        self.db.transition(
            str(job["job_id"]),
            "filebot_running",
            "filebot",
            "FileBot one-shot iniciado",
            source_path=str(input_root),
            output_root=str(output_root),
        )
        self.db.add_event(
            str(job["job_id"]),
            "filebot",
            "command",
            "Comando FileBot preparado",
            self._sanitize_command_event({
                "command_preview": command_preview,
                "cwd": str(job_root),
                "timeout_sec": command_preview.get("timeout_sec", 14400)
                if isinstance(command_preview, dict)
                else 14400,
            }),
        )
        if identity:
            result = self.filebot.run(
                str(job["job_id"]),
                str(job["category"]),
                input_root,
                output_root,
                identity,
            )
        else:
            result = self.filebot.run(
                str(job["job_id"]), str(job["category"]), input_root, output_root
            )
        if result.get("timed_out"):
            if series_selected:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "filebot_timeout",
                    "FileBot agotó el plazo antes de entregar el pack completo",
                    phase="filebot",
                )
                return
            self._finish_filebot_timeout(job, job_root, input_root, output_root, result)
            return
        if result.get("duplicate"):
            if series_selected:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_filebot_duplicate",
                    "FileBot indicó un duplicado dentro de la salida provisional privada",
                    phase="filebot",
                )
                return
            duplicate = self._move_duplicate_to_review(
                job, job_root, identity_rules
            )
            write_reason(
                duplicate,
                {
                    "job_id": job["job_id"],
                    "phase": "filebot",
                    "reason": "destination_exists",
                    "timestamp": time.time(),
                },
                "Serie repetida.txt" if job["category"] == "tv" else "Pelicula repetida.txt",
                ["FileBot indica que el destino ya existe."],
            )
            self._cleanup_clients(job, strict=False)
            self.db.transition(
                str(job["job_id"]),
                "duplicate",
                "filebot",
                "FileBot confirmó que el destino ya existe; enviado a repetidas",
                stage_path=str(duplicate),
                result_json=json.dumps(result, ensure_ascii=False),
            )
            return
        if result["exit_code"] != 0:
            if series_selected:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "filebot_failed",
                    f"FileBot falló con código {result['exit_code']}",
                    phase="filebot",
                )
                return
            failed = move_job_to_review_clean(job_root, self.config.review_dir, str(job["name"]))
            write_reason(
                failed,
                {
                    "job_id": job["job_id"],
                    "phase": "filebot",
                    "exit_code": result["exit_code"],
                    "error": result["stdout_tail"],
                    "timestamp": time.time(),
                },
                "Error de FileBot.txt",
                [
                    f"FileBot fallo con codigo {result['exit_code']}.",
                    str(result["stdout_tail"])[-2000:],
                ],
            )
            self.db.transition(
                str(job["job_id"]),
                "error_terminal",
                "filebot",
                f"FileBot falló con código {result['exit_code']}",
                stage_path=str(failed),
                last_error_code="filebot_failed",
                last_error_message=str(result["stdout_tail"]),
                result_json=json.dumps(result, ensure_ascii=False),
            )
            return
        output_media = list(result.get("output_media") or [])
        moves = list(result.get("moves") or [])
        remaining_media = media_files(input_root)
        remaining_relevant = (
            self._series_remaining_input_files(input_root)
            if series_selected
            else remaining_media
        )
        if not output_media and not moves:
            if series_selected:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_filebot_no_output",
                    "FileBot no produjo una salida provisional completa",
                    phase="filebot",
                )
                return
            duplicate = self._move_duplicate_to_review(
                job, job_root, identity_rules
            )
            write_reason(
                duplicate,
                {
                    "job_id": job["job_id"],
                    "phase": "filebot",
                    "reason": "no_new_output",
                    "timestamp": time.time(),
                },
                "Serie repetida.txt" if job["category"] == "tv" else "Pelicula repetida.txt",
                ["FileBot no produjo salida nueva; normalmente significa que ya existia."],
            )
            self._cleanup_clients(job, strict=False)
            self.db.transition(
                str(job["job_id"]),
                "duplicate",
                "filebot",
                "FileBot no produjo salida nueva; enviado a repetidas",
                stage_path=str(duplicate),
                result_json=json.dumps(result, ensure_ascii=False),
            )
            return
        if remaining_relevant:
            self.db.add_event(
                str(job["job_id"]),
                "verify",
                "warning",
                f"Quedan {len(remaining_relevant)} archivos relevantes sin mover",
            )
            if series_selected:
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_filebot_partial_output",
                    f"FileBot dejó {len(remaining_relevant)} archivos sin clasificar",
                    phase="verify",
                )
                return
        if job["category"] == "movies":
            media_sources = self._filebot_output_roots(result, output_root)
            if len(media_sources) > 1:
                review = move_job_to_review_clean(job_root, self.config.review_dir, str(job["name"]))
                write_reason(
                    review,
                    {
                        "job_id": job["job_id"],
                        "phase": "filebot",
                        "reason": "multiple_movie_outputs",
                        "outputs": [str(path) for path in media_sources],
                        "timestamp": time.time(),
                    },
                    "Revision manual.txt",
                    [
                        "FileBot produjo varias peliculas en un mismo trabajo.",
                        "ARR no las procesa automaticamente para evitar nombres incorrectos.",
                        *[str(path) for path in media_sources],
                    ],
                )
                self._cleanup_clients(job, strict=False)
                self.db.transition(
                    str(job["job_id"]),
                    "manual_review",
                    "filebot",
                    "FileBot produjo varias peliculas; enviado a revision",
                    stage_path=str(review),
                    last_error_code="multiple_movie_outputs",
                    result_json=json.dumps(result, ensure_ascii=False),
                )
                return
            media_source = media_sources[0] if media_sources else None
            if not media_source:
                failed = move_job_to_review_clean(job_root, self.config.review_dir, str(job["name"]))
                write_reason(
                    failed,
                    {
                        "job_id": job["job_id"],
                        "phase": "filebot",
                        "reason": "media_output_missing",
                        "timestamp": time.time(),
                    },
                    "Error de FileBot.txt",
                    ["FileBot termino, pero no encuentro carpeta de salida para Media Worker."],
                )
                self.db.transition(
                    str(job["job_id"]),
                    "error_terminal",
                    "filebot",
                    "No se encontro salida de FileBot para Media Worker",
                    stage_path=str(failed),
                    result_json=json.dumps(result, ensure_ascii=False),
                )
                return
            if identity and not self.name_resolver.output_matches(
                identity, [media_source.name]
            ):
                self._reject_identity_output(
                    job,
                    job_root,
                    result,
                    identity,
                    [media_source.name],
                    output_root,
                )
                return
            self.db.transition(
                str(job["job_id"]),
                "media_postprocess_ready",
                "verify",
                f"FileBot dejo pelicula lista para Media Worker: {media_source.name}",
                source_path=str(media_source),
                output_root=str(self.config.movies_final),
                result_json=json.dumps(result, ensure_ascii=False),
            )
            return

        if series_selected:
            observed_media = media_files(output_root)
            observed_groups, unclassified_output = self._series_episode_groups(
                observed_media
            )
            observed_codes, unclassified_output = self._series_episode_manifest(
                observed_media
            )
            observed_files = self._series_remaining_input_files(output_root)
            observed_physical_manifest = self._series_bound_manifest(
                observed_files,
            )
            if (
                not observed_media
                or unclassified_output
                or observed_codes != series_expected_episode_codes
                or observed_groups != series_expected_episode_groups
                or observed_physical_manifest != series_expected_physical_manifest
            ):
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_filebot_episode_manifest_mismatch",
                    (
                        "La salida provisional de FileBot no conserva exactamente "
                        "el pack de episodios de entrada"
                    ),
                    phase="verify",
                )
                return
            self.db.add_event(
                str(job["job_id"]),
                "verify",
                "decision",
                "Cobertura exacta de episodios verificada tras FileBot",
                {
                    "expected": self._format_episode_codes(
                        series_expected_episode_codes
                    ),
                    "observed": self._format_episode_codes(observed_codes),
                    "media_files": len(observed_media),
                },
            )
        if identity:
            output_names = self._tv_output_names(result, output_root)
            if not output_names or not self.name_resolver.output_matches(
                identity, output_names
            ):
                if series_selected:
                    self._preserve_series_job_for_review(
                        job,
                        job_root,
                        "series_identity_output_mismatch",
                        "La salida provisional de FileBot no coincide con la identidad TV congelada",
                        phase="identity",
                    )
                    return
                self._reject_identity_output(
                    job,
                    job_root,
                    result,
                    identity,
                    output_names,
                    output_root,
                )
                return
        if series_selected:
            self.db.transition(
                str(job["job_id"]),
                "series_postprocess_ready",
                "verify",
                f"FileBot dejo serie lista para Series Worker: {len(output_media) or len(moves)} elementos",
                source_path=str(output_root),
                output_root=str(self.config.tv_output),
                result_json=json.dumps(result, ensure_ascii=False),
            )
        else:
            self.db.transition(
                str(job["job_id"]),
                "ready_cleanup",
                "verify",
                f"Salida verificada: {len(output_media) or len(moves)} elementos",
                result_json=json.dumps(result, ensure_ascii=False),
            )

    def _normalize_bluray_before_filebot(
        self,
        job: Dict[str, object],
        job_root: Path,
        input_root: Path,
    ) -> Optional[Path]:
        job_id = str(job["job_id"])
        preview = self._bluray_worker_command_preview(job_id, input_root)
        self.db.add_event(
            job_id,
            "bluray",
            "command",
            "Llamada al normalizador Blu-ray preparada",
            self._sanitize_command_event({
                "event_name": "bluray_normalization_requested",
                "command_preview": preview,
            }),
        )
        started = self.db.transition(
            job_id,
            "bluray_running",
            "bluray",
            "Normalización Blu-ray iniciada",
        )
        self._worker_started_at[job_id] = float(started.get("updated_at") or time.time())
        current = self.db.get_job(job_id) or job
        try:
            result = self.media_worker.normalize_bluray(
                job_id,
                input_root,
                self.config.media_reports_root,
            )
        except MediaWorkerBusy as error:
            self._defer_media_worker_busy(current, "bluray", "ready_filebot", error)
            return None
        except (MediaWorkerJobActive, MediaWorkerTransportError) as error:
            return self._reconcile_bluray_running(current, call_error=error)
        except MediaWorkerError as error:
            if isinstance(error.result, dict):
                result = dict(error.result)
            else:
                self._finish_worker_failure(
                    current,
                    "bluray",
                    error,
                    error_code="bluray_worker_transport_unknown",
                )
                return None
        except Exception as error:
            self._finish_worker_failure(
                current,
                "bluray",
                error,
                error_code="bluray_worker_exception",
            )
            return None
        return self._apply_bluray_result(current, result, recovery=False)

    def _apply_bluray_result(
        self,
        job: Dict[str, object],
        result: Dict[str, object],
        *,
        recovery: bool,
    ) -> Optional[Path]:
        job_id = str(job["job_id"])
        self._worker_status_checked_at.pop(job_id, None)
        self._worker_started_at.pop(job_id, None)
        kind = str(result.get("kind") or "")
        status = str(result.get("status") or "")
        if (
            str(result.get("job_id") or "") != job_id
            or (kind and kind != "bluray")
            or not status
            or status in {"active", "terminal"}
        ):
            self._finish_worker_failure(
                job,
                "bluray",
                ValueError("El resultado terminal Blu-ray no es válido para el trabajo"),
                error_code="bluray_worker_invalid_terminal",
                recovery=recovery,
            )
            return None

        current = self.db.get_job(job_id) or job
        job_root = Path(str(current.get("stage_path") or ""))
        input_root = Path(str(current.get("source_path") or ""))
        if status != "normalized":
            if not job_root.exists() or not self._inside_workshop(job_root):
                self._finish_worker_failure(
                    current,
                    "bluray",
                    RuntimeError(str(result.get("reason") or status)),
                    error_code=f"bluray_{status}",
                    recovery=recovery,
                )
                return None
            normalized_failure = dict(result)
            if status == "error":
                normalized_failure["status"] = "unexpected_error"
                normalized_failure.setdefault("reason", result.get("error"))
            self._send_bluray_review(current, job_root, normalized_failure)
            return None

        normalized_file = Path(str(result.get("result_file") or ""))
        valid = (
            job_root.exists()
            and self._inside_workshop(job_root)
            and normalized_file.is_file()
            and normalized_file.suffix.lower() == ".mkv"
            and self._path_is_inside(normalized_file, job_root)
            and not full_bluray_folders(input_root)
        )
        if not valid:
            self._finish_worker_failure(
                current,
                "bluray",
                ValueError("El resultado Blu-ray no deja un único MKV válido en el taller"),
                error_code="bluray_worker_invalid_terminal",
                recovery=recovery,
            )
            return None

        if recovery:
            self.db.add_event(
                job_id,
                "recovery",
                "decision",
                "Resultado Blu-ray durable reconciliado sin repetir la normalización",
                {"status": status},
            )
            self.db.transition(
                job_id,
                "ready_filebot",
                "bluray",
                "Normalización Blu-ray recuperada; FileBot puede continuar",
                source_path=str(normalized_file),
                last_error_code=None,
                last_error_message=None,
                result_json=json.dumps(result, ensure_ascii=False, default=str),
            )
            return normalized_file

        recalculated = prepare_filebot_input(
            normalized_file,
            job_root,
            str(current.get("name") or normalized_file.stem),
        )
        recalculated_media = media_files(recalculated)
        if (
            len(recalculated_media) != 1
            or recalculated_media[0].suffix.lower() != ".mkv"
            or any(
                part.casefold() in {"bdmv", "stream", "extra", "extras"}
                for part in recalculated_media[0].parts
            )
        ):
            self._finish_worker_failure(
                current,
                "bluray",
                ValueError("El input recalculado para FileBot no es un MKV aislado"),
                error_code="bluray_worker_invalid_terminal",
            )
            return None
        self.db.add_event(
            job_id,
            "bluray",
            "finished",
            "Input de FileBot recalculado desde el MKV verificado",
            {
                "event_name": "bluray_filebot_input_ready",
                "input_path": str(recalculated),
                "media_file": str(recalculated_media[0]),
            },
        )
        return recalculated

    def _bluray_started_timestamp(self, job: Dict[str, object]) -> float:
        job_id = str(job["job_id"])
        cached = self._worker_started_at.get(job_id)
        if cached:
            return cached
        detail = self.db.job_detail(job_id) or {}
        timeline = list(detail.get("timeline") or [])
        for event in reversed(timeline):
            if not isinstance(event, dict) or event.get("phase") != "bluray":
                continue
            structured = event.get("structured")
            state = structured.get("state") if isinstance(structured, dict) else None
            if state == "bluray_running" or event.get("message") == "Normalización Blu-ray iniciada":
                try:
                    started_at = float(event.get("ts") or 0.0)
                except (TypeError, ValueError):
                    started_at = 0.0
                if started_at:
                    self._worker_started_at[job_id] = started_at
                    return started_at
        fallback = float(job.get("updated_at") or time.time())
        self._worker_started_at[job_id] = fallback
        return fallback

    def _reconcile_bluray_running(
        self,
        job: Dict[str, object],
        *,
        call_error: Optional[object] = None,
        recovery: bool = False,
    ) -> Optional[Path]:
        job_id = str(job["job_id"])
        now = time.time()
        if not call_error and not recovery:
            last_checked = self._worker_status_checked_at.get(job_id, 0.0)
            if now - last_checked < WORKER_STATUS_POLL_SECONDS:
                return None
        self._worker_status_checked_at[job_id] = now
        try:
            result = self._load_worker_result(job, "bluray")
        except (OSError, TypeError, ValueError) as error:
            self._finish_worker_failure(
                job,
                "bluray",
                error,
                error_code="bluray_worker_invalid_terminal",
                recovery=recovery,
            )
            return None
        if result is not None:
            return self._apply_bluray_result(job, result, recovery=True)
        try:
            worker_status = self.media_worker.job_status(job_id, "bluray")
        except Exception as status_error:
            active_age = max(0.0, now - self._bluray_started_timestamp(job))
            if active_age > WORKER_ACTIVE_MAX_SECONDS:
                self._finish_worker_failure(
                    job,
                    "bluray",
                    call_error or status_error,
                    error_code="bluray_worker_active_timeout",
                    recovery=recovery,
                )
            else:
                self._record_worker_wait(
                    job,
                    "bluray",
                    "status_unavailable",
                    call_error or status_error,
                )
            return None
        status = str(worker_status.get("status") or "")
        if status == "terminal" and isinstance(worker_status.get("result"), dict):
            return self._apply_bluray_result(
                job,
                dict(worker_status["result"]),
                recovery=True,
            )
        if status == "active":
            try:
                started_at = float(worker_status.get("started_at") or 0.0)
            except (TypeError, ValueError):
                started_at = 0.0
            active_age = max(0.0, now - started_at) if started_at else 0.0
            if started_at and active_age > WORKER_ACTIVE_MAX_SECONDS:
                self._finish_worker_failure(
                    job,
                    "bluray",
                    RuntimeError("La normalización Blu-ray superó el plazo máximo"),
                    error_code="bluray_worker_active_timeout",
                    recovery=recovery,
                )
            else:
                self._record_worker_wait(
                    job,
                    "bluray",
                    "active",
                    call_error or RuntimeError("La normalización Blu-ray sigue activa"),
                )
            return None
        self._finish_worker_failure(
            job,
            "bluray",
            call_error or RuntimeError("No hay actividad ni resultado Blu-ray terminal"),
            error_code=(
                "bluray_recovery_inconclusive" if recovery else "bluray_worker_not_found"
            ),
            recovery=recovery,
        )
        return None

    def _send_bluray_review(
        self,
        job: Dict[str, object],
        job_root: Path,
        result: Dict[str, object],
    ) -> None:
        status = str(result.get("status") or "unexpected_error")
        ambiguity = status in {"ambiguous", "no_safe_playlist"}
        destination = move_job_to_review_clean(
            job_root,
            self.config.review_dir,
            str(job["name"]),
        )
        reason_file = "Revision manual.txt" if ambiguity else "Error de proceso.txt"
        reason = str(result.get("reason") or status)
        write_reason(
            destination,
            {
                "job_id": job["job_id"],
                "phase": "bluray",
                "reason": status,
                "details": result,
                "timestamp": time.time(),
            },
            reason_file,
            [
                "La estructura Blu-ray no se proceso automaticamente.",
                reason,
                "El origen se conserva para revision.",
            ],
        )
        self._cleanup_clients(job, strict=False)
        self.db.transition(
            str(job["job_id"]),
            "manual_review" if ambiguity else "error_terminal",
            "bluray",
            "Blu-ray enviado a revision" if ambiguity else "Normalizacion Blu-ray fallida",
            stage_path=str(destination),
            last_error_code=f"bluray_{status}",
            last_error_message=reason,
            result_json=json.dumps(result, ensure_ascii=False, default=str),
        )

    def _move_duplicate_to_review(
        self,
        job: Dict[str, object],
        job_root: Path,
        identity_rules: Optional[Dict[str, object]] = None,
    ) -> Path:
        if job["category"] == "tv":
            parser_rules = (
                identity_rules.get("parser")
                if isinstance(identity_rules, dict)
                and isinstance(identity_rules.get("parser"), dict)
                else None
            )
            return move_tv_job_to_review(
                job_root,
                self.config.review_dir,
                str(job["name"]),
                parser_rules,
            )
        return move_job_to_review_clean(job_root, self.config.review_dir, str(job["name"]))

    def _media_decision_for_job(
        self,
        job: Dict[str, object],
        input_root: Path,
        identity_rules: Optional[Dict[str, object]] = None,
    ) -> MediaDecision:
        category = str(job.get("category") or "")
        sources = [str(job.get("name") or ""), input_root.name]
        files = media_files(input_root)
        files.sort(key=lambda path: path.stat().st_size if path.exists() else 0, reverse=True)
        sources.extend(path.stem for path in files[:3])
        return self.identity.decide_sources(sources, category, identity_rules)

    @staticmethod
    def _can_continue_tv_without_identity(
        job: Dict[str, object],
        media_decision: MediaDecision,
    ) -> bool:
        return (
            str(job.get("category") or "") == "tv"
            and media_decision.media_type == "tv"
            and media_decision.confidence in {"high", "medium"}
            and not media_decision.block_reason
        )

    def _send_media_decision_review(
        self,
        job: Dict[str, object],
        job_root: Path,
        media_decision: MediaDecision,
    ) -> None:
        review = move_job_to_review_clean(job_root, self.config.review_dir, str(job["name"]))
        reason = {
            "job_id": job["job_id"],
            "phase": "identity",
            "reason": media_decision.block_reason or "media_decision_blocked",
            "media_decision": media_decision.to_dict(),
            "timestamp": time.time(),
        }
        write_reason(
            review,
            reason,
            "Revision manual.txt",
            [
                "La decision local no encontro un titulo util para continuar.",
                f"Tipo detectado: {media_decision.media_type}",
                f"Motivos: {', '.join(media_decision.reason_codes)}",
            ],
        )
        self._cleanup_clients(job, strict=False)
        self.db.transition(
            str(job["job_id"]),
            "manual_review",
            "identity",
            "Decision local bloquea por titulo no usable",
            stage_path=str(review),
            last_error_code=media_decision.block_reason or "media_decision_blocked",
            result_json=json.dumps(reason, ensure_ascii=False, default=str),
        )

    def _preserve_series_job_for_review(
        self,
        job: Dict[str, object],
        job_root: Path,
        error_code: str,
        message: str,
        *,
        phase: str = "series",
        whole_job: bool = True,
    ) -> bool:
        """Mueve y verifica el pack completo antes de limpiar clientes."""

        review: Optional[Path] = None
        try:
            job_id = str(job["job_id"])
            expected_job_root = self.config.workshop_root / job_id
            self._require_series_lexical_path(
                job_root,
                self.config.workshop_root,
                "taller de Series",
            )
            self._require_series_lexical_path(
                expected_job_root,
                self.config.workshop_root,
                "taller esperado de Series",
            )
            resolved_job_root = job_root.resolve(strict=True)
            resolved_expected = expected_job_root.resolve(strict=True)
            if resolved_job_root != resolved_expected:
                raise ValueError(
                    "La preservación de Series solo puede mover <taller>/<job_id>"
                )
            filebot_output = job_root / "series_filebot_output"
            clean_review = self._series_clean_review_ready(
                job_root,
                filebot_output,
            )
            preserve_whole_tree = whole_job and not clean_review
            before = review_content_signature(
                job_root,
                whole_tree=preserve_whole_tree,
            )
            if not before:
                raise ValueError("El taller no contiene un pack preservable")
            review_root = self._require_series_physical_path(
                self.config.series_review_dir,
                "raíz de revisión de Series",
            )
            review_root.mkdir(parents=True, exist_ok=True)
            review_root = self._require_series_physical_path(
                review_root,
                "raíz de revisión de Series",
            )
            if not review_root.is_dir():
                raise ValueError("La raiz de revision de Series no es fisica")
            resolved_review_root = review_root.resolve(strict=True)
            lexical_review_root = Path(os.path.abspath(str(review_root)))
            if resolved_review_root != lexical_review_root:
                raise ValueError("La raiz de revision de Series atraviesa un enlace simbolico")
            review_name = str(job.get("name") or job.get("job_id") or "serie")
            if clean_review:
                review = move_tv_job_to_review(
                    job_root,
                    resolved_review_root,
                    review_name,
                )
            else:
                mover = move_job_to if preserve_whole_tree else move_job_to_review_clean
                review = mover(job_root, resolved_review_root, review_name)
            self.db.update_job(job_id, stage_path=str(review))
            if review.parent != resolved_review_root or review.is_symlink():
                raise ValueError("El destino de revision de Series no es canonico")
            after = review_content_signature(
                review,
                whole_tree=preserve_whole_tree,
            )
            signatures_match = (
                sorted((size, fingerprint) for _path, size, fingerprint in before)
                == sorted((size, fingerprint) for _path, size, fingerprint in after)
                if clean_review
                else before == after
            )
            if not signatures_match:
                raise ValueError("La copia de revision no coincide con el pack completo")
            if not self._path_is_inside(review, resolved_review_root):
                raise ValueError("El destino de revision de Series no es canonico")
            reason = {
                "profile": "series",
                "category": "tv",
                "job_id": str(job["job_id"]),
                "phase": phase,
                "reason": error_code,
                "message": self._safe_worker_error(message),
                "preserved_files": len(after),
                "_arr_review_signature": self._series_review_signature_digest(review),
                "_arr_review_signature_method": SERIES_REVIEW_SIGNATURE_STAT_V1,
                "timestamp": time.time(),
            }
            write_reason(
                review,
                reason,
                "Serie repetida.txt" if clean_review else "Revision de serie.txt",
                [
                    "El pack completo se ha conservado en Repetidas / Error.",
                    self._safe_worker_error(message),
                ],
            )
        except Exception as error:
            self.db.add_event(
                str(job["job_id"]),
                phase,
                "error",
                "No se pudo confirmar la preservacion completa del pack de Series",
                {
                    "error": self._safe_worker_error(error),
                    "cleanup_blocked": True,
                },
            )
            self.db.transition(
                str(job["job_id"]),
                "manual_review",
                phase,
                (
                    "Series requiere revision; copia movida sin confirmar y clientes conservados"
                    if review is not None
                    else "Series requiere revision; taller y clientes se conservan"
                ),
                **({"stage_path": str(review)} if review is not None else {}),
                last_error_code=f"{error_code}_preservation_unconfirmed",
                last_error_message=self._safe_worker_error(error),
            )
            return False

        clients_cleaned = self._cleanup_clients(job, strict=True)
        reason["clients_cleanup_pending"] = not clients_cleaned
        try:
            write_reason(
                review,
                reason,
                "Serie repetida.txt" if clean_review else "Revision de serie.txt",
                [
                    "El pack completo se ha conservado en Repetidas / Error.",
                    self._safe_worker_error(message),
                    (
                        "La limpieza de clientes queda pendiente de reintento automatico."
                        if not clients_cleaned
                        else "Las entradas de clientes se han limpiado sin borrar archivos."
                    ),
                ],
            )
        except Exception as reason_error:
            self.db.add_event(
                str(job["job_id"]),
                phase,
                "warning",
                "No se pudo actualizar el motivo con el estado de limpieza",
                {"error": self._safe_worker_error(reason_error)},
            )
        terminal_code = (
            error_code
            if clients_cleaned
            else f"{error_code}_client_cleanup_pending"
        )
        self.db.transition(
            str(job["job_id"]),
            "manual_review",
            phase,
            (
                "Pack preservado; limpieza de clientes pendiente"
                if not clients_cleaned
                else "Pack completo preservado en Repetidas / Error"
            ),
            stage_path=str(review),
            last_error_code=terminal_code,
            last_error_message=self._safe_worker_error(message),
            result_json=json.dumps(reason, ensure_ascii=False),
        )
        return True

    @classmethod
    def _series_clean_review_ready(
        cls,
        job_root: Path,
        filebot_output: Path,
    ) -> bool:
        """Usa el formato humano solo si FileBot contiene todo lo preservable."""

        if (
            not filebot_output.is_dir()
            or filebot_output.is_symlink()
            or not media_files(filebot_output)
        ):
            return False
        relevant_suffixes = MEDIA_EXTENSIONS | SIDECAR_EXTENSIONS
        try:
            for path in filebot_output.rglob("*"):
                if path.is_symlink():
                    return False
                if path.is_dir():
                    continue
                if not path.is_file() or path.suffix.casefold() not in relevant_suffixes:
                    return False
        except OSError:
            return False
        for folder_name in ("filebot_input", "extracted", "original"):
            root = job_root / folder_name
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                return False
            for path in root.rglob("*"):
                try:
                    if path.is_symlink():
                        return False
                    if path.is_dir():
                        continue
                    if not path.is_file():
                        return False
                    suffix = path.suffix.casefold()
                    if suffix not in JUNK_EXTENSIONS:
                        return False
                except OSError:
                    return False
        return True

    def _defer_identity(
        self,
        job: Dict[str, object],
        input_root: Path,
        error: ResolutionError,
    ) -> None:
        retry = int(job.get("retry_filebot") or 0) + 1
        delay = self.identity.retry_delay(job, retry)
        retry_at = time.time() + delay
        self.db.transition(
            str(job["job_id"]),
            "identity_retry",
            "identity",
            f"TMDb no disponible; reintento automatico en {delay}s",
            source_path=str(input_root),
            retry_filebot=retry,
            identity_retry_at=retry_at,
            last_error_code="identity_unavailable",
            last_error_message=str(error),
        )

    def _send_identity_review(
        self,
        job: Dict[str, object],
        job_root: Path,
        error: ResolverAmbiguous,
    ) -> None:
        review = move_job_to_review_clean(job_root, self.config.review_dir, str(job["name"]))
        reason = {
            "job_id": job["job_id"],
            "phase": "identity",
            "reason": "identity_suspicious",
            "message": str(error),
            "details": error.details,
            "timestamp": time.time(),
        }
        write_reason(
            review,
            reason,
            "Revision manual.txt",
            [
                "No se encontro una identidad unica despues de agotar las consultas automaticas.",
                str(error),
            ],
        )
        self._cleanup_clients(job, strict=False)
        self.db.transition(
            str(job["job_id"]),
            "manual_review",
            "identity",
            "Identidad realmente ambigua tras los intentos automaticos",
            stage_path=str(review),
            last_error_code="identity_suspicious",
            last_error_message=str(error),
            result_json=json.dumps(reason, ensure_ascii=False, default=str),
        )

    def _finish_filebot_timeout(
        self,
        job: Dict[str, object],
        job_root: Path,
        input_root: Path,
        output_root: Path,
        result: Dict[str, object],
    ) -> None:
        moves = list(result.get("moves") or [])
        output_media = list(result.get("output_media") or [])
        recovered_outputs = 0
        if job.get("category") == "tv":
            # TV escribe directamente en la biblioteca final. Recuperamos solo
            # destinos confirmados por el propio log de FileBot; nunca hacemos
            # un barrido destructivo del arbol compartido.
            recovered_outputs = self._quarantine_output_moves(
                result, output_root, job_root
            )
            if output_media and recovered_outputs == 0:
                self.db.add_event(
                    str(job["job_id"]),
                    "filebot",
                    "warning",
                    "Timeout con salida detectada sin inventario seguro para recuperarla",
                    {"detected_output_media": len(output_media)},
                )
        input_preserved = input_root.exists() or bool(moves)
        destination = move_job_to_review_clean(
            job_root,
            self.config.review_dir,
            str(job.get("name") or "Trabajo FileBot"),
        )
        timeout_message = str(
            result.get("timeout_message") or "FileBot agoto el tiempo maximo"
        )
        reason = {
            "job_id": job.get("job_id"),
            "phase": "filebot",
            "reason": "filebot_timeout",
            "message": timeout_message,
            "confirmed_moves": len(moves),
            "detected_output_media": len(output_media),
            "recovered_outputs": recovered_outputs,
            "input_preserved": input_preserved,
            "timestamp": time.time(),
        }
        write_reason(
            destination,
            reason,
            "Error de FileBot.txt",
            [
                timeout_message,
                "ARR ha parado el trabajo para evitar una segunda pasada a ciegas.",
                f"Movimientos confirmados antes del timeout: {len(moves)}.",
                "El material recuperado queda dentro de esta carpeta para revision.",
            ],
        )
        self._cleanup_clients(job, strict=False)
        self.db.transition(
            str(job["job_id"]),
            "manual_review",
            "filebot",
            "FileBot agoto el timeout; material preservado para revision",
            stage_path=str(destination),
            last_error_code="filebot_timeout",
            last_error_message=timeout_message,
            result_json=json.dumps(result, ensure_ascii=False, default=str),
        )

    def _reject_identity_output(
        self,
        job: Dict[str, object],
        job_root: Path,
        result: Dict[str, object],
        identity: ResolvedIdentity,
        output_names: List[str],
        output_root: Path,
    ) -> None:
        if job["category"] == "tv":
            self._quarantine_output_moves(result, output_root, job_root)
        review = move_job_to_review_clean(job_root, self.config.review_dir, str(job["name"]))
        reason = {
            "job_id": job["job_id"],
            "phase": "identity",
            "reason": "filebot_identity_mismatch",
            "resolved_identity": identity.to_dict(),
            "filebot_output_names": output_names,
            "timestamp": time.time(),
        }
        write_reason(
            review,
            reason,
            "Error de FileBot.txt",
            [
                "FileBot devolvio un nombre distinto de la identidad TMDb confirmada.",
                f"Esperado: {identity.title} ({identity.year or 'sin ano'})",
                f"Devuelto: {', '.join(output_names) or 'sin nombre'}",
            ],
        )
        self._cleanup_clients(job, strict=False)
        self.db.transition(
            str(job["job_id"]),
            "manual_review",
            "identity",
            "Salida de FileBot bloqueada por no coincidir con TMDb",
            stage_path=str(review),
            last_error_code="filebot_identity_mismatch",
            result_json=json.dumps(reason, ensure_ascii=False),
        )

    @staticmethod
    def _tv_output_names(result: Dict[str, object], output_root: Path) -> List[str]:
        names: List[str] = []
        destinations = [
            str(item.get("destination") or "") for item in result.get("moves") or []
        ]
        destinations.extend(str(path) for path in result.get("output_media") or [])
        for value in destinations:
            if not value:
                continue
            try:
                relative = Path(value).relative_to(output_root)
            except ValueError:
                continue
            if relative.parts and relative.parts[0] not in names:
                names.append(relative.parts[0])
        return names

    def _quarantine_output_moves(
        self, result: Dict[str, object], output_root: Path, job_root: Path
    ) -> int:
        quarantine = job_root / "filebot_rejected"
        candidates: List[Path] = []
        for item in result.get("moves") or []:
            if isinstance(item, dict):
                candidates.append(Path(str(item.get("destination") or "")))

        # Si el timeout corto stdout antes de la linea MOVE, FileBotRunner
        # aporta los ficheros nuevos por mtime. Solo los recuperamos cuando la
        # carpeta raiz coincide con la identidad TMDb confirmada del trabajo.
        identity_payload = result.get("identity")
        identity: Optional[ResolvedIdentity] = None
        if result.get("timed_out") and isinstance(identity_payload, dict):
            try:
                identity = ResolvedIdentity.from_dict(identity_payload)
            except (KeyError, TypeError, ValueError):
                identity = None
        if identity is not None:
            for value in result.get("output_media") or []:
                destination = Path(str(value or ""))
                try:
                    relative = destination.resolve().relative_to(output_root.resolve())
                except (OSError, ValueError):
                    continue
                if relative.parts and self.name_resolver.output_matches(
                    identity, [relative.parts[0]]
                ) and self._tv_identity_matches_output(identity, relative):
                    candidates.append(destination)

        moved = 0
        seen = set()
        for destination in candidates:
            key = str(destination)
            if key in seen:
                continue
            seen.add(key)
            if not destination.exists():
                continue
            try:
                relative = destination.resolve().relative_to(output_root.resolve())
            except (OSError, ValueError):
                continue
            target = quarantine / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(target))
            moved += 1
            parent = destination.parent
            while parent != output_root and parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        return moved

    @staticmethod
    def _tv_identity_matches_output(
        identity: ResolvedIdentity, relative: Path
    ) -> bool:
        if (
            identity.media_type != "tv"
            or identity.season is None
            or not identity.episodes
            or not relative.parts
        ):
            return False
        observed_codes = {
            (int(match.group(1)), int(match.group(2)))
            for match in re.finditer(
                r"(?i)(?<![a-z0-9])s(\d{1,3})e(\d{1,4})(?!\d)",
                relative.name,
            )
        }
        expected_codes = {
            (int(identity.season), int(episode)) for episode in identity.episodes
        }
        return bool(observed_codes.intersection(expected_codes))

    @staticmethod
    def _series_episode_codes(path: Path) -> set[Tuple[int, int]]:
        codes: set[Tuple[int, int]] = set()
        for match in re.finditer(
            r"(?i)(?<![a-z0-9])s(\d{1,3})(e\d{1,4}(?:(?:[ ._-]*e|[ ._-]+)\d{1,4})*)",
            path.name,
        ):
            season = int(match.group(1))
            episodes = Engine._episode_cluster_numbers(match.group(2))
            codes.update((season, episode) for episode in episodes)
        for match in re.finditer(
            r"(?i)(?<!\d)(\d{1,3})x(\d{1,4}(?:(?:x|[ ._-]+)\d{1,4})*)(?!\d)",
            path.name,
        ):
            season = int(match.group(1))
            codes.update(
                (season, episode)
                for episode in Engine._episode_cluster_numbers(match.group(2))
            )
        return codes

    @staticmethod
    def _episode_cluster_numbers(value: str) -> List[int]:
        matches = list(re.finditer(r"\d{1,4}", value))
        episodes: List[int] = []
        index = 0
        while index < len(matches):
            current = int(matches[index].group(0))
            if index + 1 < len(matches):
                following = int(matches[index + 1].group(0))
                separator = value[matches[index].end() : matches[index + 1].start()]
                if (
                    current <= following
                    and re.fullmatch(
                        r"[ ._]*-[ ._]*(?:e[ ._]*)?",
                        separator,
                        flags=re.IGNORECASE,
                    )
                    is not None
                ):
                    episodes.extend(range(current, following + 1))
                    index += 2
                    continue
            episodes.append(current)
            index += 1
        return list(dict.fromkeys(episodes))

    @classmethod
    def _series_episode_groups(
        cls, paths: List[Path]
    ) -> Tuple[List[Tuple[Tuple[int, int], ...]], List[str]]:
        groups: List[Tuple[Tuple[int, int], ...]] = []
        unclassified: List[str] = []
        for path in paths:
            observed = tuple(sorted(cls._series_episode_codes(path)))
            if not observed:
                unclassified.append(path.name)
                continue
            groups.append(observed)
        groups.sort()
        return groups, sorted(unclassified, key=str.casefold)

    @classmethod
    def _series_episode_manifest(
        cls, paths: List[Path]
    ) -> Tuple[set[Tuple[int, int]], List[str]]:
        groups, unclassified = cls._series_episode_groups(paths)
        return {code for group in groups for code in group}, unclassified

    @staticmethod
    def _series_bound_manifest(
        paths: List[Path],
    ) -> List[Tuple[int, int, int, str, Tuple[Tuple[int, int], ...]]]:
        result: List[
            Tuple[int, int, int, str, Tuple[Tuple[int, int], ...]]
        ] = []
        for path in paths:
            try:
                info = path.lstat()
            except OSError:
                continue
            if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                continue
            result.append(
                (
                    int(info.st_dev),
                    int(info.st_ino),
                    int(info.st_size),
                    path.suffix.casefold(),
                    tuple(sorted(Engine._series_episode_codes(path))),
                )
            )
        return sorted(result)

    @staticmethod
    def _series_remaining_input_files(root: Path) -> List[Path]:
        remaining: List[Path] = []
        if not root.exists():
            return remaining
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or path.is_file():
                    remaining.append(path)
            except OSError:
                remaining.append(path)
        return sorted(remaining, key=lambda item: str(item).casefold())

    @staticmethod
    def _format_episode_codes(codes: set[Tuple[int, int]]) -> List[str]:
        return [f"S{season:02d}E{episode:02d}" for season, episode in sorted(codes)]

    @staticmethod
    def _safe_series_relative(value: object, label: str) -> PurePosixPath:
        text = str(value or "").replace("\\", "/")
        relative = PurePosixPath(text)
        if (
            not text
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or "\x00" in text
        ):
            raise ValueError(f"{label} no es una ruta relativa segura")
        return relative

    def _series_paths_for_job(
        self,
        job: Dict[str, object],
        *,
        source_may_be_missing: bool = False,
    ) -> Tuple[Path, Path]:
        job_id = str(job["job_id"])
        stage_value = str(job.get("stage_path") or "").strip()
        source_value = str(job.get("source_path") or "").strip()
        if not stage_value or not source_value:
            raise ValueError("El trabajo de Series no conserva sus rutas")
        lexical_job_root = Path(stage_value)
        lexical_source_root = Path(source_value)
        lexical_job_root = self._require_series_lexical_path(
            lexical_job_root,
            self.config.workshop_root,
            "taller de Series",
        )
        lexical_source_root = self._require_series_lexical_path(
            lexical_source_root,
            lexical_job_root,
            "entrada de Series",
        )
        if lexical_source_root != lexical_job_root / "series_filebot_output":
            raise ValueError(
                "La entrada de Series no coincide con series_filebot_output"
            )
        job_root = lexical_job_root.resolve(strict=True)
        expected_job_root = self._require_series_lexical_path(
            self.config.workshop_root / job_id,
            self.config.workshop_root,
            "taller esperado de Series",
        ).resolve(strict=True)
        if job_root != expected_job_root or job_root.name != job_id:
            raise ValueError("El taller no coincide con <taller>/<job_id>")
        expected_source = job_root / "series_filebot_output"
        if expected_source.exists() or expected_source.is_symlink():
            if expected_source.is_symlink() or not expected_source.is_dir():
                raise ValueError(
                    "La entrada de Series no coincide con series_filebot_output"
                )
            source_root = expected_source.resolve(strict=True)
        elif source_may_be_missing:
            source_root = expected_source
        else:
            raise ValueError("No existe la entrada de Series")
        for root, label in (
            (self.config.tv_output, "biblioteca TV"),
            (self.config.series_review_dir, "revisión de Series"),
            (self.config.series_reports_root, "informes de Series"),
        ):
            lexical_root = self._require_series_physical_path(root, label)
            if not lexical_root.resolve(strict=True).is_dir():
                raise ValueError(f"No está disponible la ruta canónica de {label}")
        return job_root, source_root

    @staticmethod
    def _require_series_physical_path(path: Path, label: str) -> Path:
        lexical = Path(os.path.abspath(str(path)))
        for current in reversed((lexical, *lexical.parents)):
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ValueError(f"No se puede validar la ruta física de {label}") from error
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"{label} atraviesa un enlace simbólico")
        return lexical

    @classmethod
    def _require_series_lexical_path(
        cls,
        path: Path,
        root: Path,
        label: str,
    ) -> Path:
        lexical = Path(os.path.abspath(str(path)))
        lexical_root = cls._require_series_physical_path(root, label)
        try:
            lexical.relative_to(lexical_root)
        except ValueError as error:
            raise ValueError(f"{label} queda fuera de su raíz autorizada") from error
        return cls._require_series_physical_path(lexical, label)

    def _series_review_root_for_job(
        self,
        job_id: str,
        candidate: Optional[Path] = None,
    ) -> Path:
        """Mantiene recuperables únicamente los jobs persistidos con la raíz antigua."""

        current_root = self._require_series_physical_path(
            self.config.series_review_dir,
            "revisión de Series",
        )
        if candidate is not None:
            lexical_candidate = Path(os.path.abspath(str(candidate)))
            try:
                lexical_candidate.relative_to(current_root)
                return current_root
            except ValueError:
                pass
        request_path = self.config.series_reports_root / job_id / "request.json"
        try:
            request_path = self._require_series_lexical_path(
                request_path,
                self.config.series_reports_root,
                "request durable de Series",
            )
            request_stat = request_path.lstat()
            if (
                not stat.S_ISREG(request_stat.st_mode)
                or request_stat.st_size > MAX_WORKER_RESULT_BYTES
            ):
                return current_root
            request = json.loads(request_path.read_text(encoding="utf-8"))
            payload = request.get("payload") if isinstance(request, dict) else None
            if (
                not isinstance(payload, dict)
                or str(payload.get("job_id") or "") != job_id
            ):
                return current_root
            requested_root = Path(
                os.path.abspath(str(payload.get("review_root") or ""))
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return current_root

        legacy_root = Path(
            os.path.abspath(
                str(
                    self.config.data_root
                    / "media"
                    / LEGACY_SERIES_REVIEW_DIRNAME
                )
            )
        )
        if requested_root != legacy_root:
            return current_root
        try:
            legacy_root = self._require_series_physical_path(
                legacy_root,
                "revisión histórica de Series",
            )
        except ValueError:
            return current_root
        return legacy_root if legacy_root.is_dir() else current_root

    def _series_worker_command_preview(
        self,
        job: Dict[str, object],
        job_root: Path,
        source_root: Path,
    ) -> Dict[str, object]:
        preview = getattr(self.series_worker, "preview_process_series", None)
        if callable(preview):
            return dict(
                preview(
                    str(job["job_id"]),
                    job_root,
                    source_root,
                    self.config.tv_output,
                    self.config.series_review_dir,
                    self.config.series_reports_root,
                )
            )
        return {
            "method": "POST",
            "service": "series-worker",
            "endpoint": "/process-series",
            "payload": {
                "job_id": str(job["job_id"]),
                "job_root": str(job_root),
                "source_root": str(source_root),
                "final_root": str(self.config.tv_output),
                "review_root": str(self.config.series_review_dir),
                "reports_root": str(self.config.series_reports_root),
            },
            "timeout_sec": 30,
        }

    def _record_series_worker_fingerprint(
        self,
        job: Dict[str, object],
        payload: Dict[str, object],
    ) -> None:
        fingerprint = str(payload.get("rules_fingerprint") or "")
        if not fingerprint:
            return
        if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise ValueError("Series Worker devolvió una huella de reglas inválida")
        current = self.db.get_job(str(job["job_id"])) or job
        source_meta = self._source_meta(current)
        worker_meta = source_meta.get("series_worker")
        if worker_meta is None:
            worker_meta = {}
        if not isinstance(worker_meta, dict):
            raise ValueError("El snapshot de Series Worker es contradictorio")
        previous = str(worker_meta.get("rules_fingerprint") or "")
        if previous and previous != fingerprint:
            raise ValueError("Series Worker cambió las reglas durante el trabajo")
        if previous:
            return
        worker_meta = {
            **worker_meta,
            "schema": "series-worker-job-v1",
            "rules_fingerprint": fingerprint,
            "accepted_at": time.time(),
        }
        source_meta["series_worker"] = worker_meta
        self.db.update_job(
            str(job["job_id"]),
            source_meta_json=json.dumps(
                source_meta,
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        self.db.add_event(
            str(job["job_id"]),
            "settings",
            "decision",
            "Huella de reglas de Series congelada por el worker",
            {"series_rules_fingerprint": fingerprint},
        )

    def _load_series_worker_result(
        self,
        job_id: str,
    ) -> Optional[Dict[str, object]]:
        relative_job = self._safe_series_relative(job_id, "job_id de Series")
        if len(relative_job.parts) != 1:
            raise ValueError("El job_id de Series no identifica un único directorio")
        result_dir = self._require_series_lexical_path(
            self.config.series_reports_root / relative_job.name,
            self.config.series_reports_root,
            "directorio durable de Series",
        )
        result_path = self._require_series_lexical_path(
            result_dir / "series_result.json",
            result_dir,
            "resultado durable de Series",
        )
        descriptor = -1
        try:
            descriptor = self._open_series_result_descriptor(
                self.config.series_reports_root,
                relative_job.name,
                result_path,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ValueError("El resultado durable de Series no se puede abrir") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("El resultado durable de Series no es un archivo regular")
            if info.st_size > MAX_WORKER_RESULT_BYTES:
                raise ValueError("El resultado durable de Series supera el límite")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                result = json.load(handle)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("El resultado durable de Series no se puede leer") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(result, dict):
            raise ValueError("El resultado durable de Series no es un objeto JSON")
        delivery = result.get("delivery")
        if (
            result.get("status") == "done"
            and isinstance(delivery, dict)
            and bool(delivery.get("cleanup_pending"))
        ):
            # El propio worker debe recuperar y cerrar su limpieza durable antes
            # de que ARR trate el fichero como terminal.
            return None
        return result

    @staticmethod
    def _open_series_result_descriptor(
        reports_root: Path,
        job_id: str,
        fallback_path: Path,
    ) -> int:
        """Abre el resultado sin seguir componentes intercambiados por symlinks.

        En Linux se recorre desde ``/`` con descriptores de directorio y
        ``O_NOFOLLOW``. Así, aunque un ancestro cambie después de la validación
        léxica, el fichero se abre dentro de la cadena física ya fijada.
        """

        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        if (
            os.name != "posix"
            or not nofollow
            or not directory
            or os.open not in os.supports_dir_fd
        ):
            return os.open(fallback_path, os.O_RDONLY | nofollow)

        root = Path(os.path.abspath(str(reports_root)))
        if root.anchor != "/":
            raise OSError("La raíz durable de Series no es absoluta")
        directory_flags = os.O_RDONLY | directory | nofollow
        current = os.open("/", directory_flags)
        try:
            for part in (*root.parts[1:], job_id):
                next_descriptor = os.open(
                    part,
                    directory_flags,
                    dir_fd=current,
                )
                try:
                    if not stat.S_ISDIR(os.fstat(next_descriptor).st_mode):
                        raise OSError("La cadena durable contiene un componente no directorio")
                except Exception:
                    os.close(next_descriptor)
                    raise
                os.close(current)
                current = next_descriptor
            return os.open(
                "series_result.json",
                os.O_RDONLY | nofollow,
                dir_fd=current,
            )
        finally:
            os.close(current)

    def _validate_series_manifest(
        self,
        result: Dict[str, object],
        source_root: Path,
        *,
        verify_source: bool = True,
        ignored_root_names: Tuple[str, ...] = (),
        source_path_prefix: Optional[PurePosixPath] = None,
    ) -> Tuple[Dict[str, object], List[PurePosixPath]]:
        manifest_payload = result.get("manifest")
        if not isinstance(manifest_payload, dict):
            raise ValueError("El resultado de Series no contiene manifiesto")
        expected_manifest_keys = {
            "schema",
            "status",
            "digest",
            "series_name",
            "series_key",
            "review_reasons",
            "entries",
        }
        if set(manifest_payload) != expected_manifest_keys:
            raise ValueError("El manifiesto de Series no respeta el contrato exacto")
        if manifest_payload.get("schema") != "series-manifest-v1":
            raise ValueError("El manifiesto de Series usa un esquema desconocido")
        manifest_status = str(manifest_payload.get("status") or "")
        review_reasons = manifest_payload.get("review_reasons")
        if (
            manifest_status not in {"ready", "review"}
            or not isinstance(review_reasons, list)
            or any(not isinstance(reason, str) or not reason for reason in review_reasons)
            or len(set(review_reasons)) != len(review_reasons)
        ):
            raise ValueError("El manifiesto de Series no conserva un estado valido")
        digest = str(manifest_payload.get("digest") or "")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("El manifiesto de Series no conserva una huella válida")
        entries = manifest_payload.get("entries")
        if not isinstance(entries, list):
            raise ValueError("El manifiesto de Series no contiene entradas válidas")
        expected_entry_keys = {
            "source_relpath",
            "target_relpath",
            "series_name",
            "series_key",
            "season",
            "episodes",
            "size",
            "mtime_ns",
            "source_fingerprint",
            "content_sha256",
            "subtitle_sidecars",
        }
        expected_sidecar_keys = {
            "source_relpath",
            "size",
            "mtime_ns",
            "content_sha256",
        }
        targets: List[PurePosixPath] = []
        seen_sources = set()
        seen_targets = set()
        declared_files = set()

        def review_relative(path: PurePosixPath) -> PurePosixPath:
            if source_path_prefix is None:
                return path
            prefix_parts = source_path_prefix.parts
            if (
                not prefix_parts
                or len(path.parts) <= len(prefix_parts)
                or path.parts[: len(prefix_parts)] != prefix_parts
            ):
                raise ValueError("La revisión no coincide con la raíz del pack")
            return PurePosixPath(*path.parts[len(prefix_parts) :])

        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != expected_entry_keys:
                raise ValueError("El manifiesto de Series contiene una entrada inválida")
            source = self._safe_series_relative(
                entry.get("source_relpath"), "source_relpath"
            )
            target = self._safe_series_relative(
                entry.get("target_relpath"), "target_relpath"
            )
            if source.as_posix() in seen_sources or target.as_posix() in seen_targets:
                raise ValueError("El manifiesto de Series contiene rutas duplicadas")
            seen_sources.add(source.as_posix())
            seen_targets.add(target.as_posix())
            series_name = str(entry.get("series_name") or "")
            series_key = str(entry.get("series_key") or "")
            season = entry.get("season")
            episodes = entry.get("episodes")
            size = entry.get("size")
            mtime_ns = entry.get("mtime_ns")
            source_fingerprint = str(entry.get("source_fingerprint") or "")
            if (
                not series_name
                or not series_key
                or not isinstance(season, int)
                or isinstance(season, bool)
                or season < 0
                or not isinstance(episodes, list)
                or not episodes
                or any(
                    not isinstance(episode, int)
                    or isinstance(episode, bool)
                    or episode < 0
                    for episode in episodes
                )
                or len(target.parts) != 3
                or target.parts[0] != series_name
                or target.parts[1] != f"Season {season:02d}"
                or target.suffix.casefold() != ".mkv"
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(mtime_ns, int)
                or isinstance(mtime_ns, bool)
                or mtime_ns < 0
                or re.fullmatch(r"[0-9a-f]{64}", source_fingerprint) is None
            ):
                raise ValueError("El manifiesto contiene una identidad de episodio inválida")
            expected_hash = str(entry.get("content_sha256") or "")
            expected_source_fingerprint = hashlib.sha256(
                f"{source.as_posix()}\0{size}\0{mtime_ns}".encode("utf-8")
            ).hexdigest()
            if (
                (expected_hash and re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None)
                or source_fingerprint != expected_source_fingerprint
            ):
                raise ValueError("El manifiesto no coincide con el episodio de entrada")
            if verify_source:
                physical_source = review_relative(source)
                source_file = source_root.joinpath(*physical_source.parts)
                try:
                    source_stat = source_file.lstat()
                except OSError as error:
                    raise ValueError("Falta un episodio de entrada declarado") from error
                if (
                    not stat.S_ISREG(source_stat.st_mode)
                    or not self._path_is_inside(source_file, source_root)
                    or source_stat.st_size != size
                    or source_stat.st_mtime_ns != mtime_ns
                ):
                    raise ValueError("El manifiesto no coincide con el episodio de entrada")
            declared_files.add(review_relative(source).as_posix())
            sidecars = entry.get("subtitle_sidecars")
            if not isinstance(sidecars, list):
                raise ValueError("El manifiesto de Series no conserva sus subtítulos")
            for sidecar in sidecars:
                if not isinstance(sidecar, dict) or set(sidecar) != expected_sidecar_keys:
                    raise ValueError("El manifiesto contiene un subtítulo inválido")
                sidecar_relative = self._safe_series_relative(
                    sidecar.get("source_relpath"), "subtitle source_relpath"
                )
                if sidecar_relative.as_posix() in declared_files:
                    raise ValueError("El manifiesto repite un archivo de entrada")
                sidecar_hash = str(sidecar.get("content_sha256") or "")
                sidecar_size = sidecar.get("size")
                sidecar_mtime = sidecar.get("mtime_ns")
                if (
                    (sidecar_hash and re.fullmatch(r"[0-9a-f]{64}", sidecar_hash) is None)
                    or not isinstance(sidecar_size, int)
                    or isinstance(sidecar_size, bool)
                    or sidecar_size < 0
                    or not isinstance(sidecar_mtime, int)
                    or isinstance(sidecar_mtime, bool)
                    or sidecar_mtime < 0
                ):
                    raise ValueError("El manifiesto no coincide con el subtítulo externo")
                if verify_source:
                    physical_sidecar = review_relative(sidecar_relative)
                    sidecar_file = source_root.joinpath(*physical_sidecar.parts)
                    try:
                        sidecar_stat = sidecar_file.lstat()
                    except OSError as error:
                        raise ValueError("Falta un subtitulo externo declarado") from error
                    if (
                        not stat.S_ISREG(sidecar_stat.st_mode)
                        or not self._path_is_inside(sidecar_file, source_root)
                        or sidecar_stat.st_size != sidecar_size
                        or sidecar_stat.st_mtime_ns != sidecar_mtime
                    ):
                        raise ValueError("El manifiesto no coincide con el subtítulo externo")
                declared_files.add(review_relative(sidecar_relative).as_posix())
            targets.append(target)

        canonical_manifest = json.dumps(
            {
                "entries": entries,
                "review_reasons": sorted(review_reasons),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(canonical_manifest).hexdigest() != digest:
            raise ValueError("La huella del manifiesto de Series no coincide")
        if verify_source:
            actual_files = self._series_regular_file_inventory(
                source_root,
                ignored_root_names=ignored_root_names,
            )
            if not declared_files.issubset(actual_files):
                raise ValueError("El manifiesto declara archivos que no existen en el origen")
            if manifest_status == "ready" and declared_files != set(actual_files):
                raise ValueError("El manifiesto no cubre el pack completo de entrada")
        return manifest_payload, targets

    def _series_regular_file_inventory(
        self,
        root: Path,
        *,
        ignored_root_names: Tuple[str, ...] = (),
    ) -> Dict[str, int]:
        if root.is_symlink() or not root.is_dir():
            raise ValueError("La raiz del pack de Series no es un directorio fisico")
        inventory: Dict[str, int] = {}
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            if current_path.is_symlink():
                raise ValueError("El pack de Series contiene un enlace simbolico")
            for name in directories:
                directory = current_path / name
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("El pack de Series contiene una carpeta no regular")
            for name in filenames:
                path = current_path / name
                if current_path == root and name in ignored_root_names:
                    continue
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("El pack de Series contiene un archivo no regular")
                relative = self._safe_series_relative(
                    path.relative_to(root).as_posix(),
                    "series inventory",
                ).as_posix()
                inventory[relative] = int(info.st_size)
        return inventory

    def _validate_series_published_manifest(
        self,
        result: Dict[str, object],
        series_root: PurePosixPath,
        published_paths: List[PurePosixPath],
    ) -> None:
        payload = result.get("published_manifest")
        if not isinstance(payload, dict) or set(payload) != {"schema", "digest", "entries"}:
            raise ValueError("Series Worker no conserva el manifiesto final del pack")
        if payload.get("schema") != SERIES_PUBLISHED_MANIFEST_SCHEMA:
            raise ValueError("El manifiesto final de Series usa un esquema desconocido")
        digest = str(payload.get("digest") or "")
        entries = payload.get("entries")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None or not isinstance(entries, list):
            raise ValueError("El manifiesto final de Series no es valido")
        declared: Dict[str, int] = {}
        normalized_entries: List[Dict[str, object]] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "size", "content_sha256"}:
                raise ValueError("El manifiesto final contiene una entrada invalida")
            relative = self._safe_series_relative(entry.get("path"), "published manifest path")
            size = entry.get("size")
            content_hash = str(entry.get("content_sha256") or "")
            if (
                len(relative.parts) < 2
                or relative.parts[0] != series_root.name
                or (
                    len(relative.parts) == 2
                    and relative.name == SERIES_GENERATION_MARKER
                )
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or (content_hash and re.fullmatch(r"[0-9a-f]{64}", content_hash) is None)
                or relative.as_posix() in declared
            ):
                raise ValueError("El manifiesto final contiene una ruta o huella invalida")
            declared[relative.as_posix()] = size
            normalized_entries.append(
                {
                    "path": relative.as_posix(),
                    "size": size,
                    "content_sha256": content_hash,
                }
            )
        if normalized_entries != sorted(
            normalized_entries,
            key=lambda entry: (str(entry["path"]).casefold(), str(entry["path"])),
        ):
            raise ValueError("El manifiesto final de Series no esta ordenado")
        canonical = json.dumps(
            normalized_entries,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != digest:
            raise ValueError("La huella del manifiesto final de Series no coincide")
        final_series = self.config.tv_output.joinpath(*series_root.parts)
        if final_series.is_symlink() or not final_series.is_dir():
            raise ValueError("La raíz final de Series no es válida")
        if (final_series / SERIES_GENERATION_MARKER).exists():
            raise ValueError("La biblioteca conserva el marcador interno de Series")
        for path, size in declared.items():
            relative = PurePosixPath(path)
            destination = final_series.joinpath(*relative.parts[1:])
            try:
                destination_stat = destination.lstat()
            except OSError as error:
                raise ValueError(
                    "La biblioteca no coincide con el manifiesto final del pack"
                ) from error
            if (
                not stat.S_ISREG(destination_stat.st_mode)
                or destination_stat.st_size != size
                or not self._path_is_inside(destination, final_series)
            ):
                raise ValueError("La biblioteca no coincide con el manifiesto final del pack")
        if any(path.as_posix() not in declared for path in published_paths):
            raise ValueError("Falta un episodio del pack en el manifiesto final")

    def _validate_series_worker_result(
        self,
        job: Dict[str, object],
        result: Dict[str, object],
    ) -> Optional[Path]:
        job_id = str(job["job_id"])
        if str(result.get("job_id") or "") != job_id:
            raise ValueError("El resultado de Series pertenece a otro trabajo")
        if result.get("kind") != "series":
            raise ValueError("El resultado terminal no pertenece a Series")
        status = str(result.get("status") or "")
        if status not in {"done", "review", "failed"}:
            raise ValueError("El resultado terminal de Series tiene estado inválido")
        _job_root, source_root = self._series_paths_for_job(
            job,
            source_may_be_missing=status in {"done", "review"},
        )
        self._record_series_worker_fingerprint(job, result)
        manifest_payload, targets = self._validate_series_manifest(
            result,
            source_root,
            verify_source=status == "failed",
        )
        if status == "done":
            if not targets:
                raise ValueError("Series Worker marcó done sin episodios")
            published = result.get("published")
            if not isinstance(published, list):
                raise ValueError("Series Worker no declara los episodios publicados")
            published_paths = [
                self._safe_series_relative(value, "published") for value in published
            ]
            if [path.as_posix() for path in published_paths] != [
                path.as_posix() for path in targets
            ]:
                raise ValueError("La publicación no coincide con el manifiesto completo")
            series_root = self._safe_series_relative(
                result.get("series_root"), "series_root"
            )
            if len(series_root.parts) != 1 or any(
                target.parts[0] != series_root.parts[0] for target in targets
            ):
                raise ValueError("La raíz publicada no coincide con el pack")
            delivery = result.get("delivery")
            if (
                not isinstance(delivery, dict)
                or delivery.get("mode") != "direct_move"
                or delivery.get("cleanup_pending") is not False
            ):
                raise ValueError("La entrega directa de Series no está completada")
            self._validate_series_published_manifest(
                result,
                series_root,
                published_paths,
            )
            return None
        if status == "review":
            review_root = self._series_review_root_for_job(job_id)
            review_relative = self._safe_series_relative(
                result.get("review_path"), "review_path"
            )
            review = self._require_series_lexical_path(
                review_root.joinpath(*review_relative.parts),
                review_root,
                "revisión durable de Series",
            )
            if (
                not review.is_dir()
                or not self._path_is_inside(review, review_root)
            ):
                raise ValueError("La revisión de Series no conserva un destino válido")
            try:
                reason = json.loads((review / "reason.json").read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("La revisión de Series no conserva reason.json") from error
            if (
                not isinstance(reason, dict)
                or str(reason.get("job_id") or "") != job_id
                or str(reason.get("manifest_digest") or "")
                != str(manifest_payload.get("digest") or "")
            ):
                raise ValueError("La revisión de Series no coincide con el trabajo")
            review_layout = str(result.get("review_layout") or "source_root")
            prefix_value = str(result.get("review_source_prefix") or "")
            if review_layout not in {"source_root", "series_root", "season_root"}:
                raise ValueError("La revisión de Series declara una estructura desconocida")
            source_prefix: Optional[PurePosixPath] = None
            if review_layout == "source_root":
                if prefix_value:
                    raise ValueError("La revisión completa no puede recortar su raíz")
            else:
                source_prefix = self._safe_series_relative(
                    prefix_value,
                    "review_source_prefix",
                )
                series_name = str(manifest_payload.get("series_name") or "")
                expected_parts = 1 if review_layout == "series_root" else 2
                if (
                    len(source_prefix.parts) != expected_parts
                    or source_prefix.parts[0] != series_name
                ):
                    raise ValueError("La raíz limpia de revisión no coincide con la serie")
            if reason.get("schema") == "series-review-v1":
                if (
                    reason.get("profile") != "series"
                    or reason.get("category") != "tv"
                    or reason.get("review_layout") != review_layout
                    or str(reason.get("review_source_prefix") or "") != prefix_value
                ):
                    raise ValueError("Los metadatos de revisión de Series no son coherentes")
                marker = review / "Serie repetida.txt"
                try:
                    marker_stat = marker.lstat()
                except OSError as error:
                    raise ValueError("La revisión no conserva Serie repetida.txt") from error
                if not stat.S_ISREG(marker_stat.st_mode):
                    raise ValueError("Serie repetida.txt no es un archivo regular")
            self._validate_series_manifest(
                result,
                review,
                verify_source=True,
                ignored_root_names=SERIES_REVIEW_METADATA_FILES,
                source_path_prefix=source_prefix,
            )
            return review
        return None

    def _record_series_wait(
        self,
        job: Dict[str, object],
        status: str,
        detail: object,
    ) -> None:
        job_id = str(job["job_id"])
        key = f"{job_id}:series:{status}"
        now = time.time()
        if now - self._worker_status_log_at.get(key, 0.0) < 30:
            return
        self._worker_status_log_at[key] = now
        message = self._safe_worker_error(detail)
        if status not in {"accepted", "active", "recoverable"}:
            self.db.update_job(
                job_id,
                last_error_code=f"series_worker_{status}",
                last_error_message=message,
            )
        self.db.add_event(
            job_id,
            "series",
            "progress",
            f"Series Worker pendiente: {status}",
            {"status": status, "detail": message},
        )

    def _series_started_timestamp(self, job: Dict[str, object]) -> float:
        job_id = str(job["job_id"])
        cached = self._worker_started_at.get(job_id)
        if cached:
            return cached
        detail = self.db.job_detail(job_id) or {}
        timeline = list(detail.get("timeline") or [])
        for event in reversed(timeline):
            if not isinstance(event, dict) or event.get("phase") != "series":
                continue
            structured = event.get("structured")
            state = structured.get("state") if isinstance(structured, dict) else None
            if state != "series_postprocess_running" and event.get("message") != "Series Worker iniciado":
                continue
            try:
                started_at = float(event.get("ts") or 0.0)
            except (TypeError, ValueError):
                started_at = 0.0
            if started_at:
                self._worker_started_at[job_id] = started_at
                return started_at
        fallback = float(job.get("created_at") or time.time())
        self._worker_started_at[job_id] = fallback
        return fallback

    def _mark_series_active_timeout(
        self,
        job: Dict[str, object],
        detail: object,
        *,
        recovery: bool,
    ) -> None:
        job_id = str(job["job_id"])
        message = self._safe_worker_error(detail) or "Series Worker supero el plazo maximo"
        payload = {
            "status": "active_timeout",
            "cleanup_blocked": True,
            "workshop_preserved": True,
            "clients_preserved": True,
        }
        key = f"{job_id}:series:active_timeout"
        now = time.time()
        if now - self._worker_status_log_at.get(key, 0.0) < 300:
            return
        self._worker_status_log_at[key] = now
        self.db.add_event(
            job_id,
            "recovery" if recovery else "series",
            "error",
            "Series Worker superó el plazo; no se borra ni se mueve material activo",
            payload,
        )
        # No se convierte en terminal mientras el worker pueda seguir activo:
        # una publicación tardía todavía debe reconciliarse y verificarse.
        self.db.update_job(
            job_id,
            last_error_code="series_worker_active_timeout",
            last_error_message=message,
        )

    def _apply_series_worker_result(
        self,
        job: Dict[str, object],
        result: Dict[str, object],
        *,
        recovery: bool,
    ) -> None:
        job_id = str(job["job_id"])
        try:
            review = self._validate_series_worker_result(job, result)
        except (OSError, TypeError, ValueError) as error:
            job_root_value = str(job.get("stage_path") or "").strip()
            job_root = Path(job_root_value) if job_root_value else self.config.workshop_root / job_id
            self._preserve_series_job_for_review(
                job,
                job_root,
                "series_worker_invalid_terminal",
                str(error),
                phase="series",
            )
            return
        status = str(result.get("status") or "")
        if status == "failed":
            job_root = Path(str(job["stage_path"]))
            self._preserve_series_job_for_review(
                job,
                job_root,
                str(result.get("error_code") or "series_worker_failed"),
                str(result.get("error") or "Series Worker terminó con error"),
                phase="series",
            )
            return
        self._worker_status_checked_at.pop(job_id, None)
        self._worker_started_at.pop(job_id, None)
        self._series_retry_at.pop(job_id, None)
        if recovery:
            self.db.add_event(
                job_id,
                "recovery",
                "decision",
                "Resultado durable de Series Worker reconciliado sin repetir FileBot",
                {"status": status},
            )
        if status == "done":
            self.db.transition(
                job_id,
                "ready_cleanup",
                "series",
                "Series Worker publicó el episodio y ARR comprobó su destino",
                output_root=str(self.config.tv_output),
                result_json=json.dumps(result, ensure_ascii=False),
                last_error_code=None,
                last_error_message=None,
            )
            return
        if review is None:
            raise ValueError("El resultado review no conserva destino")
        result = {
            **result,
            "_arr_review_signature": self._series_review_signature_digest(review),
            "_arr_review_signature_method": SERIES_REVIEW_SIGNATURE_STAT_V1,
        }
        current = self.db.transition(
            job_id,
            "series_review_cleanup",
            "series_review",
            "Revision completa verificada; limpieza recuperable pendiente",
            stage_path=str(review),
            last_error_code="series_review_cleanup_pending",
            last_error_message="; ".join(
                str(value) for value in list(result.get("review_reasons") or [])[:8]
            ),
            result_json=json.dumps(result, ensure_ascii=False),
        )
        self._run_series_review_cleanup(current)

    @staticmethod
    def _series_review_signature_digest(
        review: Path,
        method: str = SERIES_REVIEW_SIGNATURE_STAT_V1,
    ) -> str:
        if method not in {
            SERIES_REVIEW_SIGNATURE_STAT_V1,
            SERIES_REVIEW_SIGNATURE_SHA256_V1,
        }:
            raise ValueError("La firma durable de revisión usa un método desconocido")
        signature_options: Dict[str, object] = {
            "whole_tree": True,
            "ignored_names": SERIES_REVIEW_METADATA_FILES,
        }
        if method == SERIES_REVIEW_SIGNATURE_SHA256_V1:
            signature_options["content_hash"] = True
        signature = review_content_signature(review, **signature_options)
        if not signature:
            raise ValueError("La revisión de Series no contiene material verificable")
        encoded = json.dumps(
            signature,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _run_series_review_cleanup(self, job: Dict[str, object]) -> None:
        job_id = str(job["job_id"])
        requested_review = Path(str(job.get("stage_path") or ""))
        review_root = self._series_review_root_for_job(job_id, requested_review)
        try:
            review = self._require_series_lexical_path(
                requested_review,
                review_root,
                "revisión pendiente de Series",
            )
        except (OSError, ValueError) as error:
            self._record_series_wait(job, "review_cleanup_invalid", error)
            return
        if (
            not review.is_dir()
            or not self._path_is_inside(review, review_root)
        ):
            self._record_series_wait(
                job,
                "review_cleanup_invalid",
                "La copia verificada de Series ya no está en Repetidas / Error",
            )
            return
        try:
            result = json.loads(str(job.get("result_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        expected_signature = str(result.get("_arr_review_signature") or "")
        signature_method = str(
            result.get("_arr_review_signature_method")
            or SERIES_REVIEW_SIGNATURE_SHA256_V1
        )
        try:
            actual_signature = self._series_review_signature_digest(
                review,
                signature_method,
            )
        except (OSError, TypeError, ValueError) as error:
            self._record_series_wait(job, "review_integrity_failed", error)
            return
        job_root = self.config.workshop_root / job_id
        if not expected_signature and job_root.is_dir() and not job_root.is_symlink():
            source_root = job_root / "series_filebot_output"
            try:
                compare_options: Dict[str, object] = {"whole_tree": True}
                if signature_method == SERIES_REVIEW_SIGNATURE_SHA256_V1:
                    compare_options["content_hash"] = True
                source_signature = review_content_signature(source_root, **compare_options)
                review_signature = review_content_signature(
                    review,
                    ignored_names=SERIES_REVIEW_METADATA_FILES,
                    **compare_options,
                )
            except (OSError, ValueError) as error:
                self._record_series_wait(job, "review_integrity_failed", error)
                return
            if not source_signature or source_signature != review_signature:
                self._record_series_wait(
                    job,
                    "review_integrity_failed",
                    "La revisión ya no coincide con el pack del taller",
                )
                return
            expected_signature = actual_signature
            result["_arr_review_signature"] = expected_signature
            result["_arr_review_signature_method"] = signature_method
            job = self.db.update_job(
                job_id,
                result_json=json.dumps(result, ensure_ascii=False),
            )
        if expected_signature and actual_signature != expected_signature:
            self._record_series_wait(
                job,
                "review_integrity_failed",
                "La copia de revisión cambió después del checkpoint",
            )
            return
        if not expected_signature and job_root.exists():
            self._record_series_wait(
                job,
                "review_integrity_unknown",
                "No existe una firma durable para autorizar el borrado del taller",
            )
            return
        if not self._cleanup_clients(job, strict=True):
            self._record_series_wait(
                job,
                "review_cleanup_pending",
                "La copia está verificada, pero quedan clientes por limpiar",
            )
            return
        if job_root.exists():
            try:
                job_root = self._require_series_lexical_path(
                    job_root,
                    self.config.workshop_root,
                    "taller pendiente de Series",
                )
                if not self._inside_workshop(job_root):
                    raise ValueError("El taller pendiente de Series no es canonico")
                shutil.rmtree(job_root)
            except (OSError, ValueError) as error:
                self._record_series_wait(job, "workshop_cleanup_pending", error)
                return
        review_reasons = list(result.get("review_reasons") or []) if isinstance(result, dict) else []
        self.db.transition(
            job_id,
            "manual_review",
            "series_review",
            "Pack completo preservado en Repetidas / Error",
            stage_path=str(review),
            last_error_code="series_review",
            last_error_message="; ".join(
                str(value) for value in review_reasons[:8]
            ),
            result_json=json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else "{}",
        )

    def _handle_series_submission(
        self,
        job: Dict[str, object],
        response: Dict[str, object],
        *,
        recovery: bool,
    ) -> None:
        job_id = str(job["job_id"])
        if str(response.get("job_id") or "") != job_id or response.get("kind") != "series":
            raise ValueError("Series Worker respondió por otro trabajo")
        status = str(response.get("status") or "")
        if status == "terminal":
            result = response.get("result")
            if not isinstance(result, dict):
                raise ValueError("Series Worker no devolvió el resultado terminal")
            self._apply_series_worker_result(job, dict(result), recovery=recovery)
            return
        if status not in {"accepted", "active", "recoverable"}:
            raise ValueError("Series Worker devolvió un estado no soportado")
        self._record_series_worker_fingerprint(job, response)
        try:
            reported_started_at = float(response.get("started_at") or 0.0)
        except (TypeError, ValueError):
            reported_started_at = 0.0
        started_at = self._series_started_timestamp(job)
        if 0.0 < reported_started_at <= time.time() + 60:
            started_at = reported_started_at
            self._worker_started_at[job_id] = started_at
        if time.time() - started_at > WORKER_ACTIVE_MAX_SECONDS:
            self._mark_series_active_timeout(
                job,
                RuntimeError(f"Series Worker sigue {status} fuera de plazo"),
                recovery=recovery,
            )
            return
        self._record_series_wait(job, status, "Trabajo aceptado y pendiente")

    def _run_series_postprocess(self, job: Dict[str, object]) -> None:
        job_id = str(job["job_id"])
        other_running = any(
            str(candidate.get("job_id") or "") != job_id
            for candidate in self.db.jobs_in_states(
                {"series_postprocess_running"},
                100,
            )
        )
        if other_running:
            self._series_retry_at[job_id] = time.time() + WORKER_STATUS_POLL_SECONDS
            self._record_series_wait(
                job,
                "pipeline_busy",
                "Otro capítulo de Series sigue procesándose",
            )
            return
        try:
            if not self._series_selected_for_job(job):
                raise ValueError("El snapshot no autoriza Series Worker")
            job_root, source_root = self._series_paths_for_job(job)
        except (OSError, TypeError, ValueError) as error:
            job_root_value = str(job.get("stage_path") or "").strip()
            job_root = Path(job_root_value) if job_root_value else self.config.workshop_root / job_id
            self._preserve_series_job_for_review(
                job,
                job_root,
                "series_worker_path_invalid",
                str(error),
                phase="series",
            )
            return
        preview = self._series_worker_command_preview(job, job_root, source_root)
        started = self.db.transition(
            job_id,
            "series_postprocess_running",
            "series",
            "Series Worker iniciado",
        )
        self._worker_started_at[job_id] = float(started.get("updated_at") or time.time())
        self.db.add_event(
            job_id,
            "series",
            "command",
            "Llamada a Series Worker preparada",
            self._sanitize_command_event({"command_preview": preview}),
        )
        current = self.db.get_job(job_id) or job
        try:
            response = self.series_worker.process_series(
                job_id,
                job_root,
                source_root,
                self.config.tv_output,
                self.config.series_review_dir,
                self.config.series_reports_root,
            )
            self._handle_series_submission(current, response, recovery=False)
        except SeriesWorkerBusy as error:
            self._worker_started_at.pop(job_id, None)
            self._series_retry_at[job_id] = time.time() + WORKER_STATUS_POLL_SECONDS
            self.db.transition(
                job_id,
                "series_postprocess_ready",
                "series",
                "Series Worker ocupado; reintento seguro pendiente",
                last_error_code=error.error_code,
                last_error_message=self._safe_worker_error(error),
            )
        except SeriesWorkerTransportError as error:
            self._reconcile_running_series(current, call_error=error)
        except (SeriesWorkerBadRequest, SeriesWorkerConflict, SeriesWorkerUnavailable) as error:
            self._preserve_series_job_for_review(
                current,
                job_root,
                error.error_code,
                str(error),
                phase="series",
            )
        except (OSError, TypeError, ValueError, SeriesWorkerError) as error:
            self._preserve_series_job_for_review(
                current,
                job_root,
                str(getattr(error, "error_code", "series_worker_invalid_response")),
                str(error),
                phase="series",
            )

    def _reconcile_running_series(
        self,
        job: Dict[str, object],
        *,
        call_error: Optional[object] = None,
        recovery: bool = False,
    ) -> None:
        job_id = str(job["job_id"])
        try:
            pending_result = json.loads(str(job.get("result_json") or "null"))
        except (TypeError, ValueError, json.JSONDecodeError):
            pending_result = None
        if (
            isinstance(pending_result, dict)
            and pending_result.get("status") == "done"
            and pending_result.get("kind") == "series"
        ):
            try:
                self._validate_series_worker_result(job, pending_result)
            except (OSError, TypeError, ValueError) as error:
                self._preserve_series_job_for_review(
                    job,
                    self.config.workshop_root / job_id,
                    "series_worker_invalid_terminal",
                    str(error),
                    phase="recovery" if recovery else "series",
                )
                return
            self.db.transition(
                job_id,
                "ready_cleanup",
                "recovery",
                "Resultado directo de Series reconciliado",
                output_root=str(self.config.tv_output),
                last_error_code=None,
                last_error_message=None,
                result_json=json.dumps(pending_result, ensure_ascii=False),
            )
            return
        now = time.time()
        if not call_error and not recovery:
            last_checked = self._worker_status_checked_at.get(job_id, 0.0)
            if now - last_checked < WORKER_STATUS_POLL_SECONDS:
                return
        self._worker_status_checked_at[job_id] = now
        try:
            durable = self._load_series_worker_result(job_id)
        except (OSError, TypeError, ValueError) as error:
            job_root = Path(str(job.get("stage_path") or self.config.workshop_root / job_id))
            self._preserve_series_job_for_review(
                job,
                job_root,
                "series_worker_invalid_terminal",
                str(error),
                phase="recovery" if recovery else "series",
            )
            return
        if durable is not None:
            self._apply_series_worker_result(job, durable, recovery=True)
            return
        try:
            response = self.series_worker.job_status(job_id)
        except (SeriesWorkerTransportError, SeriesWorkerUnavailable) as status_error:
            started_at = self._series_started_timestamp(job)
            if now - started_at > WORKER_ACTIVE_MAX_SECONDS:
                self._mark_series_active_timeout(
                    job,
                    str(call_error or status_error),
                    recovery=recovery,
                )
                return
            self._record_series_wait(
                job,
                "status_unavailable",
                call_error or status_error,
            )
            return
        except SeriesWorkerError as error:
            job_root = Path(str(job.get("stage_path") or self.config.workshop_root / job_id))
            self._preserve_series_job_for_review(
                job,
                job_root,
                error.error_code,
                str(error),
                phase="recovery" if recovery else "series",
            )
            return
        status = str(response.get("status") or "")
        if status == "not_found":
            try:
                self._series_paths_for_job(job)
            except (OSError, TypeError, ValueError) as error:
                job_root = Path(str(job.get("stage_path") or self.config.workshop_root / job_id))
                self._preserve_series_job_for_review(
                    job,
                    job_root,
                    "series_worker_not_found_source_invalid",
                    str(error),
                    phase="recovery" if recovery else "series",
                )
                return
            self._worker_started_at.pop(job_id, None)
            self._series_retry_at[job_id] = time.time() + 1.0
            self.db.transition(
                job_id,
                "series_postprocess_ready",
                "recovery" if recovery else "series",
                "Series Worker no llegó a aceptar el POST; se reintentará idempotentemente",
                last_error_code="series_worker_not_found",
                last_error_message="No existe estado durable; FileBot no se repetirá",
            )
            return
        try:
            self._handle_series_submission(job, response, recovery=recovery)
        except (OSError, TypeError, ValueError) as error:
            job_root = Path(str(job.get("stage_path") or self.config.workshop_root / job_id))
            self._preserve_series_job_for_review(
                job,
                job_root,
                "series_worker_invalid_response",
                str(error),
                phase="recovery" if recovery else "series",
            )

    def _run_media_postprocess(self, job: Dict[str, object]) -> None:
        source = Path(str(job["source_path"]))
        command_preview = self._media_worker_command_preview(
            "movie",
            str(job["job_id"]),
            source,
        )
        self.db.transition(
            str(job["job_id"]),
            "media_postprocess_running",
            "media",
            "Media Worker iniciado",
            output_root=str(self.config.movies_final),
        )
        self.db.add_event(
            str(job["job_id"]),
            "media",
            "command",
            "Llamada a Media Worker preparada",
            self._sanitize_command_event({"command_preview": command_preview}),
        )
        try:
            result = self.media_worker.process_movie(
                str(job["job_id"]),
                source,
                self.config.movies_final,
                self.config.review_dir,
                self.config.media_reports_root,
            )
            self._apply_worker_result(job, "media", result, recovery=False)
        except MediaWorkerBusy as error:
            self._defer_media_worker_busy(
                job,
                "media",
                "media_postprocess_ready",
                error,
            )
        except MediaWorkerJobActive as error:
            self._record_worker_wait(job, "media", "active", error)
        except MediaWorkerTransportError as error:
            self._reconcile_running_worker(job, "media", call_error=error)
        except Exception as error:
            self._finish_worker_failure(job, "media", error)

    def _run_trailer(self, job: Dict[str, object]) -> None:
        source = Path(str(job["source_path"]))
        command_preview = self._media_worker_command_preview(
            "trailer",
            str(job["job_id"]),
            source,
        )
        self.db.transition(
            str(job["job_id"]),
            "trailer_running",
            "trailer",
            "Media Worker iniciado para trailer",
        )
        self.db.add_event(
            str(job["job_id"]),
            "trailer",
            "command",
            "Llamada a Media Worker preparada para trailer",
            self._sanitize_command_event({"command_preview": command_preview}),
        )
        try:
            result = self.media_worker.process_trailer(
                str(job["job_id"]),
                source,
                self.config.movies_final,
                self.config.review_dir,
                self.config.media_reports_root,
            )
            self._apply_worker_result(job, "trailer", result, recovery=False)
        except MediaWorkerBusy as error:
            self._defer_media_worker_busy(job, "trailer", "trailer_ready", error)
        except MediaWorkerJobActive as error:
            self._record_worker_wait(job, "trailer", "active", error)
        except MediaWorkerTransportError as error:
            self._reconcile_running_worker(job, "trailer", call_error=error)
        except Exception as error:
            self._finish_worker_failure(job, "trailer", error)

    @staticmethod
    def _worker_kind(phase: str) -> str:
        if phase == "trailer":
            return "trailer"
        if phase == "bluray":
            return "bluray"
        return "movie"

    def _worker_result_path(self, job_id: str, phase: str) -> Path:
        if phase == "trailer":
            filename = "trailer_result.json"
        elif phase == "bluray":
            filename = "bluray_result.json"
        else:
            filename = "media_result.json"
        return self.config.media_reports_root / job_id / filename

    def _safe_worker_error(self, error: object) -> str:
        message = str(error or "Error desconocido de Media Worker")
        replacements = (
            (str(self.config.data_root), "<DATA>"),
            (str(self.config.config_dir), "<CONFIG>"),
            (str(self.config.diagnostics_root), "<DIAGNOSTICS>"),
            (str(self.config.codex_diag_root), "<CODEX_DIAGS>"),
        )
        for raw, alias in replacements:
            if raw:
                message = message.replace(raw, alias)
                message = message.replace(raw.replace("\\", "/"), alias)
        message = re.sub(
            r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
            "Authorization: <REDACTED>",
            message,
        )
        message = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer <REDACTED>", message)
        message = re.sub(
            r"(?i)(token|password|passwd|secret|auth)\s*[:=]\s*([^\s&,;]+)",
            r"\1=<REDACTED>",
            message,
        )
        message = re.sub(
            r"(?i)\bdownload_url\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
            "download_url=<REDACTED>",
            message,
        )
        message = re.sub(r"(?i)magnet:\?[^\s]+", "<MAGNET_REDACTED>", message)
        message = re.sub(r"(?i)https?://[^\s]+", "<URL_REDACTED>", message)
        message = re.sub(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\s,;]+", "<PATH_REDACTED>", message)
        message = re.sub(r"(?<![>\w])/(?:[^/\s]+/)*[^\s,;]*", "<PATH_REDACTED>", message)
        return message[-1200:]

    def _sanitize_command_event(self, value: object) -> Dict[str, object]:
        """Sanea la huella canónica antes de escribirla en ``job_events``."""

        replacements = sorted(
            (
                (str(self.config.data_root / "downloads"), "<DATA_DOWNLOADS>"),
                (str(self.config.data_root / "media"), "<DATA_MEDIA>"),
                (str(self.config.config_dir), "<CONFIG>"),
                (str(self.config.diagnostics_root), "<DIAGNOSTICS>"),
                (str(self.config.codex_diag_root), "<CODEX_DIAGS>"),
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )

        def alias(item: object) -> object:
            if isinstance(item, dict):
                return {str(key): alias(child) for key, child in item.items()}
            if isinstance(item, (list, tuple)):
                return [alias(child) for child in item]
            if not isinstance(item, str):
                return item
            text = item
            for raw, replacement in replacements:
                if not raw:
                    continue
                text = text.replace(raw, replacement)
                text = text.replace(raw.replace("\\", "/"), replacement)
            return text

        sanitized = sanitize_for_export(alias(value))
        return dict(sanitized) if isinstance(sanitized, dict) else {"value": sanitized}

    def _load_worker_result(
        self,
        job: Dict[str, object],
        phase: str,
    ) -> Optional[Dict[str, object]]:
        job_id = str(job["job_id"])
        result_path = self._worker_result_path(job_id, phase)
        if not result_path.exists() and phase == "trailer":
            reports_dir = result_path.parent
            if reports_dir.exists():
                for candidate in sorted(reports_dir.glob("*.json")):
                    if candidate.name == result_path.name:
                        continue
                    try:
                        if candidate.stat().st_size > MAX_WORKER_RESULT_BYTES:
                            continue
                        legacy = json.loads(candidate.read_text(encoding="utf-8"))
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    destination = str(legacy.get("moved_to") or "") if isinstance(legacy, dict) else ""
                    if destination and Path(destination).exists():
                        return {
                            "status": "done",
                            "job_id": job_id,
                            "destination": destination,
                            "reports_dir": str(reports_dir),
                            "legacy_report": candidate.name,
                        }
            return None
        if not result_path.exists():
            return None
        try:
            if result_path.stat().st_size > MAX_WORKER_RESULT_BYTES:
                raise ValueError("El resultado durable supera el límite permitido")
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("El resultado durable no se puede leer") from error
        if not isinstance(result, dict):
            raise ValueError("El resultado durable no es un objeto JSON")
        return result

    @staticmethod
    def _path_is_inside(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=True).relative_to(root.resolve(strict=True))
            return True
        except (OSError, ValueError):
            return False

    def _validate_worker_result(
        self,
        job: Dict[str, object],
        phase: str,
        result: Dict[str, object],
    ) -> None:
        job_id = str(job["job_id"])
        if str(result.get("job_id") or "") != job_id:
            raise ValueError("El resultado terminal no pertenece al trabajo")
        expected_kind = self._worker_kind(phase)
        result_kind = str(result.get("kind") or "")
        if result_kind and result_kind != expected_kind:
            raise ValueError("El resultado terminal pertenece a otro tipo de trabajo")
        status = str(result.get("status") or "")
        if status == "done":
            if phase == "trailer":
                delivered = str(result.get("destination") or "")
            else:
                final = result.get("final")
                delivered = str(final.get("final_video") or "") if isinstance(final, dict) else ""
            delivered_path = Path(delivered) if delivered else Path()
            if (
                not delivered
                or not delivered_path.is_file()
                or not self._path_is_inside(delivered_path, self.config.movies_final)
            ):
                raise ValueError(
                    "El resultado indica terminado, pero la entrega válida no existe"
                )
        elif status == "review":
            review_path = str(result.get("review_path") or "")
            review = Path(review_path) if review_path else Path()
            if (
                not review_path
                or not review.is_dir()
                or not self._path_is_inside(review, self.config.review_dir)
            ):
                raise ValueError(
                    "El resultado de revisión no conserva un destino válido"
                )
        elif status != "error":
            raise ValueError("El resultado terminal tiene un estado inesperado")

    def _defer_media_worker_busy(
        self,
        job: Dict[str, object],
        phase: str,
        ready_state: str,
        error: MediaWorkerBusy,
    ) -> None:
        job_id = str(job["job_id"])
        message = self._safe_worker_error(error)
        self._worker_status_checked_at.pop(job_id, None)
        self._worker_started_at.pop(job_id, None)
        self.db.update_job(
            job_id,
            state=ready_state,
            last_error_code=error.error_code,
            last_error_message=message,
        )
        self.db.add_event(
            job_id,
            phase,
            "retry",
            "Media Worker ocupado; reintento seguro pendiente",
            {
                "state": ready_state,
                "error_code": error.error_code,
                "error": message,
                "source_exists": Path(str(job.get("source_path") or "")).exists(),
                "stage_exists": Path(str(job.get("stage_path") or "")).exists(),
            },
        )

    def _record_worker_wait(
        self,
        job: Dict[str, object],
        phase: str,
        reason: str,
        error: object,
    ) -> None:
        job_id = str(job["job_id"])
        key = f"{job_id}:{reason}"
        now = time.time()
        if now - self._worker_status_log_at.get(key, 0.0) < 30:
            return
        self._worker_status_log_at[key] = now
        message = self._safe_worker_error(error)
        self.db.update_job(
            job_id,
            last_error_code=f"{phase}_worker_{reason}",
            last_error_message=message,
        )
        self.db.add_event(
            job_id,
            phase,
            "warning",
            f"Media Worker pendiente: {reason}",
            {
                "reason": reason,
                "error": message,
                "source_exists": Path(str(job.get("source_path") or "")).exists(),
                "stage_exists": Path(str(job.get("stage_path") or "")).exists(),
            },
        )

    def _finish_worker_failure(
        self,
        job: Dict[str, object],
        phase: str,
        error: object,
        *,
        error_code: Optional[str] = None,
        recovery: bool = False,
    ) -> None:
        job_id = str(job["job_id"])
        current = self.db.get_job(job_id) or job
        message = self._safe_worker_error(error)
        typed_code = str(getattr(error, "error_code", "") or "")
        code = error_code or typed_code or f"{phase}_worker_exception"
        current_state = str(current.get("state") or "")
        if current_state in {"done", "duplicate", "error_terminal"}:
            return
        if (
            current_state == "manual_review"
            and str(current.get("last_error_code") or "") == code
            and str(current.get("last_error_message") or "") == message
        ):
            return
        self._worker_status_checked_at.pop(job_id, None)
        self._worker_started_at.pop(job_id, None)
        structured = {
            "error_code": code,
            "error": message,
            "exception_type": type(error).__name__,
            "http_status": getattr(error, "status_code", None),
            "source_exists": Path(str(current.get("source_path") or "")).exists(),
            "stage_exists": Path(str(current.get("stage_path") or "")).exists(),
            "terminal_report_exists": self._worker_result_path(job_id, phase).exists(),
            "recovery": recovery,
        }
        self.db.add_event(
            job_id,
            phase,
            "error",
            f"Media Worker detenido de forma segura: {message}",
            structured,
        )
        self.db.transition(
            job_id,
            "manual_review",
            phase,
            "Media Worker requiere revisión manual; el material se conserva",
            last_error_code=code,
            last_error_message=message,
        )

    def _apply_worker_result(
        self,
        job: Dict[str, object],
        phase: str,
        result: Dict[str, object],
        *,
        recovery: bool,
    ) -> None:
        self._worker_status_checked_at.pop(str(job["job_id"]), None)
        try:
            self._validate_worker_result(job, phase, result)
        except (OSError, TypeError, ValueError) as error:
            self._finish_worker_failure(
                job,
                phase,
                error,
                error_code=f"{phase}_worker_invalid_terminal",
                recovery=recovery,
            )
            return
        if str(result.get("status") or "") == "error":
            error = MediaWorkerError(
                str(result.get("error") or "Media Worker terminó con error"),
                endpoint=f"/{self._worker_kind(phase)}",
                status_code=None,
                error_code=str(result.get("error_code") or f"{phase}_worker_exception"),
                result=result,
            )
            self._finish_worker_failure(job, phase, error, recovery=recovery)
            return
        if recovery:
            self.db.add_event(
                str(job["job_id"]),
                "recovery",
                "decision",
                "Resultado durable de Media Worker reconciliado sin repetir el proceso",
                {"phase": phase, "status": result.get("status")},
            )
        self._finish_worker_result(job, result, phase)

    def _reconcile_running_worker(
        self,
        job: Dict[str, object],
        phase: str,
        *,
        call_error: Optional[object] = None,
        recovery: bool = False,
    ) -> None:
        job_id = str(job["job_id"])
        now = time.time()
        if not call_error and not recovery:
            last_checked = self._worker_status_checked_at.get(job_id, 0.0)
            if now - last_checked < WORKER_STATUS_POLL_SECONDS:
                return
        self._worker_status_checked_at[job_id] = now
        try:
            result = self._load_worker_result(job, phase)
        except (OSError, TypeError, ValueError) as error:
            self._finish_worker_failure(
                job,
                phase,
                error,
                error_code=f"{phase}_worker_invalid_terminal",
                recovery=recovery,
            )
            return
        if result is not None:
            self._apply_worker_result(job, phase, result, recovery=recovery)
            return
        try:
            status = self.media_worker.job_status(
                job_id,
                self._worker_kind(phase),
            )
        except Exception as status_error:
            source_exists = Path(str(job.get("source_path") or "")).exists()
            stage_exists = Path(str(job.get("stage_path") or "")).exists()
            if recovery:
                unavailable_code = (
                    f"{phase}_recovery_inconclusive"
                    if source_exists or stage_exists
                    else f"{phase}_recovery_source_missing"
                )
            else:
                unavailable_code = f"{phase}_worker_transport_unknown"
            self._finish_worker_failure(
                job,
                phase,
                call_error or status_error,
                error_code=unavailable_code,
                recovery=recovery,
            )
            return
        worker_status = str(status.get("status") or "")
        if worker_status == "terminal" and isinstance(status.get("result"), dict):
            terminal = dict(status["result"])
            if str(terminal.get("job_id") or "") != job_id:
                self._finish_worker_failure(
                    job,
                    phase,
                    RuntimeError("Media Worker devolvió el resultado de otro trabajo"),
                    error_code=f"{phase}_worker_foreign_result",
                    recovery=recovery,
                )
                return
            self._apply_worker_result(job, phase, terminal, recovery=recovery)
            return
        if worker_status == "active":
            try:
                started_at = float(status.get("started_at") or 0.0)
            except (TypeError, ValueError):
                started_at = 0.0
            active_seconds = max(0.0, time.time() - started_at) if started_at else 0.0
            if started_at and active_seconds > WORKER_ACTIVE_MAX_SECONDS:
                self._finish_worker_failure(
                    job,
                    phase,
                    RuntimeError("Media Worker superó el plazo máximo de ejecución"),
                    error_code=f"{phase}_worker_active_timeout",
                    recovery=recovery,
                )
                return
            self._record_worker_wait(
                job,
                phase,
                "active",
                call_error or RuntimeError("El trabajo sigue activo en Media Worker"),
            )
            return
        source_exists = Path(str(job.get("source_path") or "")).exists()
        stage_exists = Path(str(job.get("stage_path") or "")).exists()
        recovery_code = (
            f"{phase}_recovery_inconclusive"
            if source_exists or stage_exists
            else f"{phase}_recovery_source_missing"
        )
        self._finish_worker_failure(
            job,
            phase,
            call_error or RuntimeError("Media Worker no conserva actividad ni resultado terminal"),
            error_code=recovery_code if recovery else f"{phase}_worker_not_found",
            recovery=recovery,
        )

    def _reconcile_late_worker_results(self) -> None:
        recoverable_codes = {
            "media_worker_transport_unknown",
            "trailer_worker_transport_unknown",
            "media_worker_invalid_terminal",
            "trailer_worker_invalid_terminal",
            "media_worker_active_timeout",
            "trailer_worker_active_timeout",
            "media_recovery_inconclusive",
            "trailer_recovery_inconclusive",
            "media_recovery_source_missing",
            "trailer_recovery_source_missing",
            "bluray_worker_transport_unknown",
            "bluray_worker_status_unavailable",
            "bluray_worker_active_timeout",
            "bluray_worker_not_found",
            "bluray_worker_invalid_terminal",
            "bluray_recovery_inconclusive",
        }
        cleanup_suffix = "_client_cleanup_pending"
        cleanup_jobs = self.db.jobs_in_state_with_error_suffix(
            "manual_review",
            cleanup_suffix,
            500,
        )
        for job in cleanup_jobs:
            error_code = str(job.get("last_error_code") or "")
            try:
                self._load_series_review_cleanup_state(job)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                original_code = error_code.removesuffix(cleanup_suffix)
                self.db.update_job(
                    str(job["job_id"]),
                    last_error_code=f"{original_code}_review_integrity_failed",
                    last_error_message=self._safe_worker_error(error),
                )
                self.db.add_event(
                    str(job["job_id"]),
                    "cleanup",
                    "error",
                    "La limpieza de clientes se bloqueó porque la revisión ya no es íntegra",
                    {
                        "error": self._safe_worker_error(error),
                        "cleanup_blocked": True,
                    },
                )
                continue
            if not self._cleanup_clients(job, strict=True):
                # Rota el lote para que un cliente caído no bloquee otros
                # reintentos pendientes detrás del límite de esta pasada.
                self.db.update_job(
                    str(job["job_id"]),
                    last_error_code=error_code,
                )
                continue
            try:
                cleanup_result = self._mark_series_review_cleanup_completed(job)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                self.db.update_job(
                    str(job["job_id"]),
                    last_error_code=error_code,
                )
                self.db.add_event(
                    str(job["job_id"]),
                    "cleanup",
                    "warning",
                    "Los clientes se limpiaron, pero el estado de la revisión sigue pendiente",
                    {"error": self._safe_worker_error(error)},
                )
                continue
            original_code = str(
                cleanup_result.get("reason")
                or error_code.removesuffix(cleanup_suffix)
            )
            self.db.update_job(
                str(job["job_id"]),
                last_error_code=original_code,
                last_error_message=str(
                    cleanup_result.get("message")
                    or job.get("last_error_message")
                    or ""
                ),
                result_json=json.dumps(cleanup_result, ensure_ascii=False),
            )
            self.db.add_event(
                str(job["job_id"]),
                "cleanup",
                "decision",
                "Limpieza pendiente de clientes completada sin borrar archivos",
            )

        for job in self.db.jobs_in_states(["manual_review"], 500):
            error_code = str(job.get("last_error_code") or "")
            if error_code not in recoverable_codes:
                continue
            if error_code.startswith("bluray"):
                try:
                    bluray_result = self._load_worker_result(job, "bluray")
                except (OSError, TypeError, ValueError):
                    continue
                if bluray_result is not None:
                    self._apply_bluray_result(job, bluray_result, recovery=True)
                continue
            phase = "trailer" if error_code.startswith("trailer") else "media"
            try:
                result = self._load_worker_result(job, phase)
            except (OSError, TypeError, ValueError):
                continue
            if result is None or str(result.get("status") or "") == "error":
                continue
            self._apply_worker_result(job, phase, result, recovery=True)

    def _mark_series_review_cleanup_completed(
        self,
        job: Dict[str, object],
    ) -> Dict[str, object]:
        review, reason = self._load_series_review_cleanup_state(job)
        reason["clients_cleanup_pending"] = False
        reason_file = (
            "Serie repetida.txt"
            if reason.get("schema") == "series-review-v1"
            else "Revision de serie.txt"
        )
        write_reason(
            review,
            reason,
            reason_file,
            [
                "El pack completo se ha conservado en Repetidas / Error.",
                self._safe_worker_error(reason.get("message") or ""),
                "Las entradas de clientes se han limpiado sin borrar archivos.",
            ],
        )
        return reason

    def _load_series_review_cleanup_state(
        self,
        job: Dict[str, object],
    ) -> Tuple[Path, Dict[str, object]]:
        job_id = str(job["job_id"])
        requested_review = Path(str(job.get("stage_path") or ""))
        configured_review_root = self._series_review_root_for_job(
            job_id,
            requested_review,
        )
        review_root = self._require_series_physical_path(
            configured_review_root,
            "raíz de revisión de Series",
        ).resolve(strict=True)
        review = self._require_series_lexical_path(
            requested_review,
            review_root,
            "revisión pendiente de limpieza",
        )
        if (
            review.parent != review_root
            or not review.is_dir()
            or review.resolve(strict=True) != review
        ):
            raise ValueError("La revisión pendiente no es una carpeta canónica de Series")
        reason_path = review / "reason.json"
        reason = json.loads(reason_path.read_text(encoding="utf-8"))
        if not isinstance(reason, dict) or str(reason.get("job_id") or "") != job_id:
            raise ValueError("El motivo de revisión no pertenece al trabajo de Series")
        expected_signature = str(reason.get("_arr_review_signature") or "")
        signature_method = str(
            reason.get("_arr_review_signature_method")
            or SERIES_REVIEW_SIGNATURE_SHA256_V1
        )
        if (
            not expected_signature
            or self._series_review_signature_digest(review, signature_method)
            != expected_signature
        ):
            raise ValueError("La revisión preservada de Series cambió desde su confirmación")
        return review, reason

    def _filebot_command_preview(
        self,
        job_id: str,
        category: str,
        input_root: Path,
        output_root: Path,
        identity: Optional[ResolvedIdentity],
    ) -> Dict[str, object]:
        preview = getattr(self.filebot, "preview_command", None)
        if callable(preview):
            return dict(preview(job_id, category, input_root, output_root, identity))
        return {
            "mode": "guided" if identity else "legacy_amc",
            "input_path": str(input_root),
            "output_root": str(output_root),
            "timeout_sec": 14400,
        }

    def _media_worker_command_preview(
        self,
        kind: str,
        job_id: str,
        source: Path,
    ) -> Dict[str, object]:
        if kind == "trailer":
            preview = getattr(self.media_worker, "preview_process_trailer", None)
            if callable(preview):
                return dict(
                    preview(
                        job_id,
                        source,
                        self.config.movies_final,
                        self.config.review_dir,
                        self.config.media_reports_root,
                    )
                )
            endpoint = "/process-trailer"
            payload = {
                "job_id": job_id,
                "source_path": str(source),
                "movies_root": str(self.config.movies_final),
                "review_root": str(self.config.review_dir),
                "reports_root": str(self.config.media_reports_root),
            }
        else:
            preview = getattr(self.media_worker, "preview_process_movie", None)
            if callable(preview):
                return dict(
                    preview(
                        job_id,
                        source,
                        self.config.movies_final,
                        self.config.review_dir,
                        self.config.media_reports_root,
                    )
                )
            endpoint = "/process-movie"
            payload = {
                "job_id": job_id,
                "source_path": str(source),
                "final_root": str(self.config.movies_final),
                "review_root": str(self.config.review_dir),
                "reports_root": str(self.config.media_reports_root),
            }
        return {
            "method": "POST",
            "service": "media-worker",
            "endpoint": endpoint,
            "payload": payload,
            "timeout_sec": 14400,
        }

    def _bluray_worker_command_preview(
        self,
        job_id: str,
        source: Path,
    ) -> Dict[str, object]:
        preview = getattr(self.media_worker, "preview_normalize_bluray", None)
        if callable(preview):
            return dict(
                preview(
                    job_id,
                    source,
                    self.config.media_reports_root,
                )
            )
        return {
            "method": "POST",
            "service": "media-worker",
            "endpoint": "/normalize-bluray",
            "payload": {
                "job_id": job_id,
                "source_path": str(source),
                "reports_root": str(self.config.media_reports_root),
            },
            "timeout_sec": 14400,
        }

    def _finish_worker_result(self, job: Dict[str, object], result: Dict[str, object], phase: str) -> None:
        current = self.db.get_job(str(job["job_id"])) or job
        status = str(result.get("status") or "")
        job_root = Path(str(current.get("stage_path") or ""))
        if status == "done":
            if str(current.get("state") or "") in {"ready_cleanup", "done"}:
                return
            self.db.transition(
                str(job["job_id"]),
                "ready_cleanup",
                phase,
                f"{phase} terminado correctamente",
                last_error_code=None,
                last_error_message=None,
                result_json=json.dumps(result, ensure_ascii=False),
            )
            return
        if status == "review":
            if str(current.get("state") or "") in {"duplicate", "error_terminal"}:
                return
            self._cleanup_clients(current, strict=False)
            if job_root.exists() and self._inside_workshop(job_root):
                shutil.rmtree(job_root, ignore_errors=True)
            reason_file = str(result.get("reason_file") or "")
            terminal = "duplicate" if "repetida" in reason_file.lower() else "error_terminal"
            self.db.transition(
                str(job["job_id"]),
                terminal,
                phase,
                f"{phase} enviado a revision: {reason_file}",
                stage_path=str(result.get("review_path") or ""),
                last_error_code=f"{phase}_review",
                last_error_message=reason_file,
                result_json=json.dumps(result, ensure_ascii=False),
            )
            return
        raise RuntimeError(f"Respuesta inesperada de Media Worker: {result}")

    def _run_cleanup(self, job: Dict[str, object]) -> None:
        job_root = Path(str(job.get("stage_path") or ""))
        lexical_job_root = Path(os.path.abspath(str(job_root)))
        lexical_workshop = Path(os.path.abspath(str(self.config.workshop_root)))
        remove_from_workshop = False
        try:
            lexical_job_root.relative_to(lexical_workshop)
        except ValueError:
            pass
        else:
            try:
                expected_job_root = lexical_workshop / str(job["job_id"])
                if lexical_job_root != expected_job_root:
                    raise ValueError(
                        "El taller pendiente de limpieza no coincide con <taller>/<job_id>"
                    )
                job_root = self._require_series_lexical_path(
                    lexical_job_root,
                    lexical_workshop,
                    "taller pendiente de limpieza",
                )
                remove_from_workshop = True
            except (OSError, ValueError) as error:
                self.db.update_job(
                    str(job["job_id"]),
                    last_error_code="cleanup_path_invalid",
                    last_error_message=self._safe_worker_error(error),
                )
                self.db.add_event(
                    str(job["job_id"]),
                    "cleanup",
                    "error",
                    "La limpieza se bloqueó porque el taller no es una ruta física segura",
                    {"cleanup_blocked": True},
                )
                return
        if not self._cleanup_clients(job, strict=True):
            return
        if remove_from_workshop and job_root.exists():
            shutil.rmtree(job_root)
        self.db.transition(
            str(job["job_id"]), "done", "cleanup", "Trabajo terminado correctamente"
        )

    def _cleanup_clients(self, job: Dict[str, object], strict: bool) -> bool:
        success = True
        if job.get("qbt_hash"):
            try:
                self.qbt.delete(str(job["qbt_hash"]), delete_files=False)
                self.db.add_event(
                    str(job["job_id"]),
                    "cleanup",
                    "qbt_deleted",
                    "Entrada de qBittorrent eliminada sin borrar archivos",
                )
            except Exception as error:
                success = False
                self.db.add_event(
                    str(job["job_id"]),
                    "cleanup",
                    "warning",
                    f"No se pudo limpiar qBittorrent: {error}",
                )
        if (job.get("rdt_id") or job.get("origin") == "rdt") and job.get("infohash"):
            try:
                self.rdt.delete(str(job["infohash"]), delete_files=False)
                self.db.add_event(
                    str(job["job_id"]),
                    "cleanup",
                    "rdt_deleted",
                    "Entrada de RDT eliminada sin borrar archivos",
                )
            except Exception as error:
                success = False
                self.db.add_event(
                    str(job["job_id"]),
                    "cleanup",
                    "warning",
                    f"No se pudo limpiar RDT: {error}",
                )
        return success or not strict

    def _recover_interrupted_jobs(self) -> None:
        interrupted = self.db.jobs_in_states(
            [
                "staging",
                "extracting",
                "filebot_running",
                "bluray_running",
                "media_postprocess_running",
                "series_postprocess_running",
                "trailer_running",
                "verifying_output",
            ],
            500,
        )
        for job in interrupted:
            job_root_value = str(job.get("stage_path") or "").strip()
            source_value = str(job.get("source_path") or "").strip()
            job_root = Path(job_root_value) if job_root_value else None
            source = Path(source_value) if source_value else None
            if job["state"] == "series_postprocess_running":
                self._reconcile_running_series(job, recovery=True)
                updated = self.db.get_job(str(job["job_id"]))
                if updated and updated.get("state") in TERMINAL_STATES - {"discarded"}:
                    self._create_terminal_diagnostic(updated)
                continue
            if job["state"] == "media_postprocess_running":
                self._reconcile_running_worker(job, "media", recovery=True)
                updated = self.db.get_job(str(job["job_id"]))
                if updated and updated.get("state") in TERMINAL_STATES - {"discarded"}:
                    self._create_terminal_diagnostic(updated)
                continue
            if job["state"] == "trailer_running":
                self._reconcile_running_worker(job, "trailer", recovery=True)
                updated = self.db.get_job(str(job["job_id"]))
                if updated and updated.get("state") in TERMINAL_STATES - {"discarded"}:
                    self._create_terminal_diagnostic(updated)
                continue
            if job["state"] == "bluray_running":
                self._reconcile_bluray_running(job, recovery=True)
                updated = self.db.get_job(str(job["job_id"]))
                if updated and updated.get("state") in TERMINAL_STATES - {"discarded"}:
                    self._create_terminal_diagnostic(updated)
                continue
            if job["state"] == "staging":
                target = "ready_extract" if job_root and job_root.exists() else "ready_stage"
            elif job["state"] == "extracting":
                target = "ready_extract"
            elif job["state"] in {"filebot_running", "verifying_output"}:
                try:
                    series_selected = self._series_selected_for_job(job)
                except ValueError as error:
                    series_selected = True
                    self.db.add_event(
                        str(job["job_id"]),
                        "recovery",
                        "error",
                        "Snapshot de Series contradictorio durante recuperación",
                        {"error": self._safe_worker_error(error)},
                    )
                if series_selected:
                    preserve_root = job_root or self.config.workshop_root / str(job["job_id"])
                    self._preserve_series_job_for_review(
                        job,
                        preserve_root,
                        "series_filebot_interrupted",
                        "El reinicio impide demostrar que FileBot terminó el pack completo",
                        phase="recovery",
                    )
                    continue
                target = "ready_filebot"
            else:
                target = "ready_filebot"
            if target == "ready_stage" and (source is None or not source.exists()):
                self.db.transition(
                    str(job["job_id"]),
                    "manual_review",
                    "recovery",
                    "No se localiza el origen tras reinicio; requiere revisión",
                    last_error_code="recovery_source_missing",
                )
                continue
            self.db.transition(
                str(job["job_id"]),
                target,
                "recovery",
                f"Trabajo recuperado después de reinicio: {target}",
            )

    def _activate_dry_run_jobs(self) -> None:
        if not self.config.active:
            return
        for job in self.db.jobs_in_states(["dry_run_ready"], 500):
            source = Path(str(job.get("source_path") or ""))
            if source.exists():
                self.db.transition(
                    str(job["job_id"]),
                    "waiting_stable",
                    "activation",
                    "Trabajo observado en dry-run revalidado para modo activo",
                )
            else:
                self.db.transition(
                    str(job["job_id"]),
                    "discarded",
                    "activation",
                    "Observación dry-run descartada porque el origen ya no existe",
                )

    def _watch_category(
        self,
        path: Path,
        name: str,
        identity_rules: Optional[Dict[str, object]] = None,
    ) -> str:
        try:
            relative = path.relative_to(self.config.watch_inbox)
            if len(relative.parts) > 1 and relative.parts[0] in ("movies", "tv", "manual"):
                return relative.parts[0]
        except ValueError:
            pass
        rules = (
            identity_rules
            if isinstance(identity_rules, dict)
            else self.identity.stores["common"].snapshot()
        )
        return self._category("", name, rules)

    @staticmethod
    def _category(
        current: str,
        name: str,
        identity_rules: Optional[Dict[str, object]] = None,
    ) -> str:
        parser_rules = (
            identity_rules.get("parser")
            if isinstance(identity_rules, dict) and isinstance(identity_rules.get("parser"), dict)
            else None
        )
        decision = decide_media(name, current, rules=parser_rules)
        if current in ("movies", "tv"):
            return current
        if decision.media_type in ("movies", "tv"):
            return decision.media_type
        return "manual"

    def _inside_complete(self, path: Path) -> bool:
        return self._complete_category_path(path) is not None

    def _inside_workshop(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.config.workshop_root.resolve())
            return True
        except (OSError, ValueError):
            return False

    def _complete_category_path(self, path: Path) -> Optional[Path]:
        roots = (self.config.complete_root / category for category in COMPLETE_CATEGORIES)
        return matching_root(path, roots)

    def _filebot_output_roots(self, result: Dict[str, object], output_root: Path) -> List[Path]:
        candidates: List[Path] = []
        for value in list(result.get("output_media") or []):
            candidates.append(Path(str(value)))
        for move in list(result.get("moves") or []):
            destination = move.get("destination") if isinstance(move, dict) else None
            if destination:
                candidates.append(Path(str(destination)))
        roots: List[Path] = []
        for candidate in candidates:
            try:
                relative = candidate.resolve().relative_to(output_root.resolve())
            except (OSError, ValueError):
                continue
            if relative.parts:
                root = output_root / relative.parts[0]
                if root.is_file():
                    root = root.parent
                if root not in roots:
                    roots.append(root)
        return roots

    def _translate_rdt_path(self, raw_path: str) -> Optional[Path]:
        if not raw_path:
            return None
        path = Path(raw_path)
        if path.exists():
            return path
        prefix = Path("/data/downloads")
        try:
            relative = path.relative_to(prefix)
        except ValueError:
            return path
        if not relative.parts or relative.parts[0] not in ("movies", "tv", "manual"):
            return path
        return self.config.complete_root / relative

    def _new_source_uid(self, prefix: str, infohash: str) -> str:
        base = f"{prefix}:{infohash}"
        if not self.db.get_job_by_source_uid(base):
            return base
        return f"{base}:{int(time.time() * 1000)}"

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        try:
            return left.resolve() == right.resolve()
        except OSError:
            return str(left) == str(right)

    @staticmethod
    def _same_name(left: str, right: str) -> bool:
        normalize = lambda value: "".join(ch.lower() for ch in value if ch.isalnum())
        left_normalized = normalize(left)
        right_normalized = normalize(right)
        return (
            left_normalized == right_normalized
            or left_normalized in right_normalized
            or right_normalized in left_normalized
        )


def _single_child(root: Path) -> Path:
    children = sorted(root.iterdir(), key=lambda item: item.name.lower())
    if len(children) == 1:
        return children[0]
    return root
