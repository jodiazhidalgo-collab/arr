import json
import logging
import queue
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from watchdog.observers import Observer

from .clients import QbitLikeClient
from .config import Config
from .codex_diagnostics import create_codex_diagnostic
from .db import Database
from .filebot import FileBotRunner
from .filesystem import (
    clean_junk,
    extract_archives,
    extraction_command_previews,
    manifest,
    matching_root,
    media_files,
    media_worker_source,
    move_into_job,
    move_job_to_review_clean,
    move_tv_job_to_review,
    move_trailer_package_into_job,
    prepare_filebot_input,
    top_level_item,
    trailer_package_manifest,
    trailer_ready_source,
    write_reason,
)
from .media_worker import MediaWorkerClient
from .name_resolver import (
    NameResolver,
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
    "media_postprocess_ready",
    "media_postprocess_running",
    "trailer_ready",
    "trailer_running",
    "verifying_output",
    "ready_cleanup",
}
COMPLETE_CATEGORIES = ("movies", "tv", "manual", "movies_automatizacion", "trailers_automatizacion")
IGNORED_COMPLETE_SUFFIXES = (".delay-audio-part",)


def ignored_complete_item(item: Path) -> bool:
    name = item.name.lower()
    return any(name.endswith(suffix) for suffix in IGNORED_COMPLETE_SUFFIXES)


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
        self.name_resolver = NameResolver(
            config.tmdb_api_token,
            config.resolver_language,
            config.resolver_region,
            config.resolver_http_timeout_ms,
            config.resolver_total_budget_ms,
            database,
            self.log,
        )
        self.media_worker = MediaWorkerClient(config.media_worker_url, config.callback_url)
        self.events: "queue.Queue[Tuple[str, Path]]" = queue.Queue()
        self.observer = Observer()
        self._stable: Dict[str, Tuple[str, float]] = {}
        self._stable_log_at: Dict[str, float] = {}
        self._last_reconcile = 0.0
        self._last_heartbeat = 0.0
        self.running = True
        self.dependencies: Dict[str, str] = {}

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
        return {
            "status": "ok",
            "mode": self.config.mode,
            "heartbeat": self._last_heartbeat,
            "dependencies": self.dependencies,
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

    def _heartbeat(self) -> None:
        self._last_heartbeat = time.time()
        (self.config.config_dir / "heartbeat").write_text(
            str(self._last_heartbeat), encoding="ascii"
        )

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
            category = self._category(str(torrent.get("category") or ""), str(torrent.get("name") or ""))
            content_path = Path(str(torrent.get("content_path") or ""))
            if not infohash or not content_path.exists():
                continue
            source_path = self._qbt_materialized_source(content_path)
            if not source_path:
                continue
            job = self._job_for_qbt_content(infohash, source_path, content_path)
            if not job:
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
        category = self._watch_category(path, name)
        job = self.db.get_active_job_by_infohash(infohash)
        if not job:
            job = self.db.create_job(
                self._new_source_uid("torrent", infohash),
                "watch",
                category,
                name,
                infohash=infohash,
                torrent_path=str(path),
            )
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
        category = self._category(str(torrent.get("category") or ""), str(torrent.get("name") or ""))
        content_path = Path(str(torrent.get("content_path") or ""))
        source_path = self._qbt_materialized_source(content_path) if content_path.exists() else None
        if not source_path:
            self.log.warning(
                "Evento qB aplazado: contenido terminado aún no visible en complete: %s",
                content_path,
            )
            return
        job = self._job_for_qbt_content(infohash, source_path, content_path)
        if not job:
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
                if ignored_complete_item(item):
                    return
                self._register_materialized(category, item)
                return

    def _register_materialized(self, category: str, item: Path) -> None:
        if not item.exists():
            return
        if ignored_complete_item(item):
            return
        if category == "trailers_automatizacion":
            ready_source = trailer_ready_source(item)
            if not ready_source:
                return
            item = ready_source
        job = self.db.get_job_by_source_path(str(item))
        if not job:
            job = self._job_for_materialized(category, item)
        if not job:
            source_uid = self._new_source_uid(f"fs:{category}", item.name)
            job = self.db.create_job(
                source_uid,
                "fs",
                category,
                item.name,
                state="waiting_stable",
                source_path=str(item),
            )
        elif job["state"] not in TERMINAL_STATES:
            self.db.update_job(
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

    def _adopt_qbt_for_materialized_job(
        self,
        job: Dict[str, object],
        category: str,
        item: Path,
    ) -> Dict[str, object]:
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
        return {
            "orchestrator": {
                "status": "ok",
                "mode": self.config.mode,
                "dependencies": dict(self.dependencies),
            },
            "media_worker": {
                "status": "ok"
                if str(self.dependencies.get("media-worker") or "").startswith("media-worker")
                else self.dependencies.get("media-worker", "-"),
            },
        }

    def _process_job(self, job: Dict[str, object]) -> None:
        source_path = Path(str(job.get("source_path") or ""))
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
            if not source_path.exists():
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
        self.db.transition(str(job["job_id"]), "staging", "stage", "Moviendo a taller")
        if job["category"] == "trailers_automatizacion":
            job_root, source_item = move_trailer_package_into_job(
                source,
                self.config.workshop_root,
                str(job["job_id"]),
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

        job_root = move_into_job(source, self.config.workshop_root, str(job["job_id"]))
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
        self.db.transition(str(job["job_id"]), "extracting", "extract", "Extracción iniciada")
        previews = extraction_command_previews(job_root)
        if previews:
            self.db.add_event(
                str(job["job_id"]),
                "extract",
                "command",
                "Comando de extraccion preparado",
                {
                    "command_preview": previews,
                    "cwd": str(job_root),
                    "timeout_sec": 7200,
                },
            )
        try:
            input_root = extract_archives(job_root)
            clean_junk(input_root)
        except Exception as error:
            failed = move_job_to_review_clean(job_root, self.config.review_dir, str(job["name"]))
            write_reason(
                failed,
                {
                    "job_id": job["job_id"],
                    "phase": "extract",
                    "error": str(error),
                    "timestamp": time.time(),
                },
                "Error de extraccion.txt",
                [str(error)],
            )
            self.db.transition(
                str(job["job_id"]),
                "error_terminal",
                "extract",
                f"Extracción fallida: {error}",
                stage_path=str(failed),
                last_error_code="extract_failed",
                last_error_message=str(error),
            )
            return
        self.db.transition(
            str(job["job_id"]),
            "ready_filebot",
            "extract",
            "Extracción terminada",
            source_path=str(input_root),
        )

    def _run_filebot(self, job: Dict[str, object]) -> None:
        job_root = Path(str(job["stage_path"]))
        input_root = prepare_filebot_input(
            Path(str(job["source_path"])),
            job_root,
            str(job.get("name") or ""),
        )
        identity: Optional[ResolvedIdentity] = None
        media_decision = self._media_decision_for_job(job, input_root)
        self.db.add_event(
            str(job["job_id"]),
            "identity",
            "decision",
            f"Decision local: {media_decision.media_type} ({media_decision.confidence})",
            {"media_decision": media_decision.to_dict()},
        )
        if media_decision.block_reason == "category_conflict":
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
        output_root = (
            job_root / "filebot_output"
            if job["category"] == "movies"
            else self.config.tv_output
        )
        output_root.mkdir(parents=True, exist_ok=True)
        command_preview = self._filebot_command_preview(
            str(job["job_id"]),
            str(job["category"]),
            input_root,
            output_root,
            identity,
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
            {
                "command_preview": command_preview,
                "cwd": str(job_root),
                "timeout_sec": command_preview.get("timeout_sec", 14400)
                if isinstance(command_preview, dict)
                else 14400,
            },
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
        if result.get("duplicate"):
            duplicate = self._move_duplicate_to_review(job, job_root)
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
        if not output_media and not moves:
            duplicate = self._move_duplicate_to_review(job, job_root)
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
        if remaining_media:
            self.db.add_event(
                str(job["job_id"]),
                "verify",
                "warning",
                f"Quedan {len(remaining_media)} archivos multimedia sin mover",
            )
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

        if identity:
            output_names = self._tv_output_names(result, output_root)
            if not output_names or not self.name_resolver.output_matches(
                identity, output_names
            ):
                self._reject_identity_output(
                    job,
                    job_root,
                    result,
                    identity,
                    output_names,
                    output_root,
                )
                return
        self.db.transition(
            str(job["job_id"]),
            "ready_cleanup",
            "verify",
            f"Salida verificada: {len(output_media) or len(moves)} elementos",
            result_json=json.dumps(result, ensure_ascii=False),
        )

    def _move_duplicate_to_review(self, job: Dict[str, object], job_root: Path) -> Path:
        if job["category"] == "tv":
            return move_tv_job_to_review(job_root, self.config.review_dir, str(job["name"]))
        return move_job_to_review_clean(job_root, self.config.review_dir, str(job["name"]))

    def _media_decision_for_job(self, job: Dict[str, object], input_root: Path) -> MediaDecision:
        category = str(job.get("category") or "")
        sources = [str(job.get("name") or ""), input_root.name]
        files = media_files(input_root)
        files.sort(key=lambda path: path.stat().st_size if path.exists() else 0, reverse=True)
        sources.extend(path.stem for path in files[:3])
        decisions = [decide_media(source, category) for source in sources if str(source or "").strip()]
        if not decisions:
            return decide_media("", category)
        for decision in decisions:
            if decision.block_reason == "category_conflict":
                return decision
        for decision in decisions:
            if decision.media_type == category and decision.confidence in {"high", "medium"}:
                return decision
        for decision in decisions:
            if decision.media_type in {"movies", "tv"}:
                return decision
        return decisions[0]

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

    def _defer_identity(
        self,
        job: Dict[str, object],
        input_root: Path,
        error: ResolutionError,
    ) -> None:
        retry = int(job.get("retry_filebot") or 0) + 1
        delay = min(
            300,
            self.config.resolver_retry_seconds * (2 ** min(retry - 1, 3)),
        )
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

    @staticmethod
    def _quarantine_output_moves(
        result: Dict[str, object], output_root: Path, job_root: Path
    ) -> None:
        quarantine = job_root / "filebot_rejected"
        for item in result.get("moves") or []:
            destination = Path(str(item.get("destination") or ""))
            if not destination.exists():
                continue
            try:
                relative = destination.relative_to(output_root)
            except ValueError:
                continue
            target = quarantine / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(target))
            parent = destination.parent
            while parent != output_root and parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

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
            {"command_preview": command_preview},
        )
        result = self.media_worker.process_movie(
            str(job["job_id"]),
            source,
            self.config.movies_final,
            self.config.review_dir,
            self.config.media_reports_root,
        )
        self._finish_worker_result(job, result, "media")

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
            {"command_preview": command_preview},
        )
        result = self.media_worker.process_trailer(
            str(job["job_id"]),
            source,
            self.config.movies_final,
            self.config.review_dir,
            self.config.media_reports_root,
        )
        self._finish_worker_result(job, result, "trailer")

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

    def _finish_worker_result(self, job: Dict[str, object], result: Dict[str, object], phase: str) -> None:
        status = str(result.get("status") or "")
        job_root = Path(str(job.get("stage_path") or ""))
        if status == "done":
            self.db.transition(
                str(job["job_id"]),
                "ready_cleanup",
                phase,
                f"{phase} terminado correctamente",
                result_json=json.dumps(result, ensure_ascii=False),
            )
            return
        if status == "review":
            self._cleanup_clients(job, strict=False)
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
        if not self._cleanup_clients(job, strict=True):
            return
        job_root = Path(str(job.get("stage_path") or ""))
        if job_root.exists() and self._inside_workshop(job_root):
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
                "media_postprocess_running",
                "trailer_running",
                "verifying_output",
            ],
            500,
        )
        for job in interrupted:
            job_root = Path(str(job.get("stage_path") or ""))
            source = Path(str(job.get("source_path") or ""))
            if job["state"] == "staging":
                target = "ready_extract" if job_root.exists() else "ready_stage"
            elif job["state"] == "extracting":
                target = "ready_extract"
            elif job["state"] == "media_postprocess_running":
                target = "media_postprocess_ready"
            elif job["state"] == "trailer_running":
                target = "trailer_ready"
            else:
                target = "ready_filebot"
            if target == "ready_stage" and not source.exists():
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

    def _watch_category(self, path: Path, name: str) -> str:
        try:
            relative = path.relative_to(self.config.watch_inbox)
            if len(relative.parts) > 1 and relative.parts[0] in ("movies", "tv", "manual"):
                return relative.parts[0]
        except ValueError:
            pass
        return self._category("", name)

    @staticmethod
    def _category(current: str, name: str) -> str:
        decision = decide_media(name, current)
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
