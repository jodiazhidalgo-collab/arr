import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from arr_orchestrator.config import Config
from arr_orchestrator.db import Database, SCHEMA
from arr_orchestrator.engine import Engine
from arr_orchestrator.name_resolver import ResolverAmbiguous
from arr_orchestrator.source_context.contract import (
    MAX_SOURCE_TITLE_CHARS,
    SourceContextContractError,
    SourceContextEvent,
)
from arr_orchestrator.source_context.service import SourceContextService
from arr_orchestrator.source_context.store import (
    CONTEXT_TTL_SECONDS,
    MAX_SOURCE_TITLES,
    NEUTRAL_JOB_NAME,
    TERMINAL_STATES,
)


RUNTIME_TEST_ROOT = Path(__file__).resolve().parents[3] / "_codex_runtime" / "test-data"


def _temporary_directory():
    RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=RUNTIME_TEST_ROOT)


def _config(root: Path, grace: int = 90) -> Config:
    data = root / "data"
    complete = data / "downloads" / "torrents" / "complete"
    return Config(
        mode="active",
        config_dir=root / "config",
        data_root=data,
        watch_inbox=data / "torrents" / "watch" / "inbox",
        processed_root=data / "torrents" / "watch" / "processed",
        watch_error=data / "torrents" / "watch" / "error",
        event_dir=data / "torrents" / "events" / "inbox" / "qbt",
        complete_root=complete,
        workshop_root=complete / "taller",
        movies_output=complete / "movies_automatizacion",
        movies_final=data / "media" / "movies",
        tv_output=data / "media" / "tv",
        trailers_inbox=complete / "trailers_automatizacion",
        review_dir=data / "media" / "repetidas_vs_error",
        media_worker_url="http://media-worker:8790",
        callback_url="http://arr-orchestrator:8787",
        media_reports_root=root / "config" / "media-worker",
        codex_diag_root=root / "diagnosticos_codex",
        diagnostics_root=root / "diagnostics" / "arr",
        qbt_url="http://gluetun:8080",
        qbt_user="admin",
        qbt_password="",
        rdt_url="http://rdtclient:6500",
        rdt_user="admin",
        rdt_password="",
        stable_seconds=1,
        reconcile_seconds=30,
        fallback_seconds=5400,
        health_port=8787,
        filebot_bin="/opt/filebot/filebot",
        tmdb_api_token="",
        resolver_language="es-ES",
        resolver_region="ES",
        resolver_http_timeout_ms=2500,
        resolver_total_budget_ms=5000,
        resolver_retry_seconds=60,
        source_context_token="test-token",
        source_context_correlation_grace_seconds=grace,
    )


def _payload(
    *,
    source: str = "buscador-pro",
    event_id: str = "event-1",
    infohash: str = "a" * 40,
    destination: str = "movies",
    title: str = "El regreso de la momia 2001 SPANISH 4K",
    route: str = "RD_VERIFIED_MAGNET_NATIVE",
    state: str = "intent",
) -> dict:
    return {
        "schema_version": 1,
        "source": source,
        "event_id": event_id,
        "infohash": infohash,
        "destination": destination,
        "source_title": title,
        "route": route,
        "delivery_state": state,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


class SourceContextContractTests(unittest.TestCase):
    def test_valid_contract_normalizes_only_hash_and_title_spacing(self) -> None:
        payload = _payload(title="  El   regreso de la momia  ")
        payload["infohash"] = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
        event = SourceContextEvent.from_payload(payload)

        self.assertEqual(event.infohash, payload["infohash"].lower())
        self.assertEqual(event.source_title, "El regreso de la momia")
        self.assertEqual(event.route, "RD_VERIFIED_MAGNET_NATIVE")

    def test_normal_title_slashes_are_not_mistaken_for_paths(self) -> None:
        for title in (
            "Película ES/EN",
            "Face/Off",
            "AC/DC Live",
            "Título /Original internacional",
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    SourceContextEvent.from_payload(_payload(title=title)).source_title,
                    title,
                )

    def test_contract_rejects_unknown_or_invalid_fields(self) -> None:
        cases = (
            ({**_payload(), "extra": True}, "unknown_fields"),
            ({**_payload(), "schema_version": 2}, "unsupported_schema"),
            ({**_payload(), "schema_version": True}, "unsupported_schema"),
            ({**_payload(), "infohash": "a" * 39}, "invalid_infohash"),
            ({**_payload(), "destination": "manual"}, "invalid_destination"),
            ({**_payload(), "route": "rdt"}, "invalid_route"),
            ({**_payload(), "delivery_state": "submitted"}, "invalid_delivery_state"),
            ({**_payload(), "source_title": "Titulo\x00secreto"}, "invalid_source_title"),
            ({**_payload(), "source_title": "magnet:?xt=urn:btih:" + "a" * 40}, "invalid_source_title"),
            ({**_payload(), "source_title": "https://privado.local/descarga"}, "invalid_source_title"),
            ({**_payload(), "source_title": "Authorization: Bearer secreto"}, "invalid_source_title"),
            ({**_payload(), "source_title": "token=secreto"}, "invalid_source_title"),
            ({**_payload(), "source_title": "credencial: secreta"}, "invalid_source_title"),
            ({**_payload(), "source_title": r"C:\datos\pelicula.mkv"}, "invalid_source_title"),
            ({**_payload(), "source_title": r"Pelicula 2024 C:\Users\admin\secret.txt"}, "invalid_source_title"),
            ({**_payload(), "source_title": "Titulo /volume1/docker/arr/config/.env"}, "invalid_source_title"),
            ({**_payload(), "source_title": r"Nombre \\NAS\share\secret"}, "invalid_source_title"),
            ({**_payload(), "source_title": "file:///etc/passwd"}, "invalid_source_title"),
            ({**_payload(), "source_title": "smb://nas/share/secret"}, "invalid_source_title"),
            ({**_payload(), "source_title": "nfs://nas/path"}, "invalid_source_title"),
            ({**_payload(), "source_title": "x://nas/path"}, "invalid_source_title"),
            ({**_payload(), "source_title": "Titulo /mnt/media/secreto"}, "invalid_source_title"),
            ({**_payload(), "source_title": "Titulo /custom/private/file"}, "invalid_source_title"),
            ({**_payload(), "source_title": "/directorio-arbitrario"}, "invalid_source_title"),
            ({**_payload(), "source_title": "Titulo /opt/app/.env"}, "invalid_source_title"),
            ({**_payload(), "source_title": "Titulo /srv/private/file"}, "invalid_source_title"),
            ({**_payload(), "source_title": "Titulo /tmp/token"}, "invalid_source_title"),
            ({**_payload(), "source_title": "Titulo /usr/local/bin"}, "invalid_source_title"),
            ({**_payload(), "source_title": r"Titulo ..\secret\file"}, "invalid_source_title"),
            ({**_payload(), "source_title": "Titulo carpeta/../secret"}, "invalid_source_title"),
            ({**_payload(), "source_title": "x" * (MAX_SOURCE_TITLE_CHARS + 1)}, "invalid_source_title"),
        )
        for payload, code in cases:
            with self.subTest(code=code), self.assertRaises(SourceContextContractError) as raised:
                SourceContextEvent.from_payload(payload)
            self.assertEqual(raised.exception.code, code)

    def test_created_at_accepts_rfc3339_and_rejects_naive_or_far_future(self) -> None:
        payload = _payload()
        payload["created_at"] = "2026-07-28T00:00:00Z"
        self.assertEqual(
            SourceContextEvent.from_payload(payload).created_at,
            "2026-07-28T00:00:00Z",
        )

        for value in (
            "2026-07-28T00:00:00",
            time.time(),
            str(time.time()),
            datetime.fromtimestamp(
                time.time() + 25 * 60 * 60, timezone.utc
            ).isoformat(),
        ):
            payload["created_at"] = value
            with self.subTest(value=value), self.assertRaises(SourceContextContractError):
                SourceContextEvent.from_payload(payload)


class SourceContextDatabaseMigrationTests(unittest.TestCase):
    def test_legacy_active_duplicates_are_traced_before_unique_index(self) -> None:
        with _temporary_directory() as temporary:
            path = Path(temporary) / "orchestrator.db"
            connection = sqlite3.connect(path)
            connection.executescript(SCHEMA)
            legacy_context = {
                key: value
                for key, value in _payload(
                    event_id="legacy-click", infohash="f" * 40
                ).items()
                if key != "schema_version"
            }
            legacy_context["received_at"] = time.time()
            pending_meta = json.dumps({"source_contexts": [legacy_context]})
            values = (
                (
                    "legacy-pending",
                    "legacy:pending",
                    "source_submitted",
                    None,
                    2.0,
                    pending_meta,
                ),
                (
                    "legacy-materialized",
                    "legacy:materialized",
                    "waiting_stable",
                    "/data/movie",
                    1.0,
                    None,
                ),
            )
            for job_id, source_uid, state, source_path, updated_at, source_meta in values:
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, source_uid, infohash, origin, category, name, state,
                        source_path, created_at, updated_at, source_meta_json
                    ) VALUES(?, ?, ?, 'bridge', 'movies', 'Legacy', ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        source_uid,
                        "f" * 40,
                        state,
                        source_path,
                        updated_at,
                        updated_at,
                        source_meta,
                    ),
                )
            connection.commit()
            connection.close()

            database = Database(path)
            database.initialize()
            try:
                active = database.get_active_job_by_infohash("f" * 40)
                self.assertEqual(active["job_id"], "legacy-materialized")
                merged_contexts = json.loads(active["source_meta_json"])[
                    "source_contexts"
                ]
                self.assertEqual(len(merged_contexts), 1)
                self.assertEqual(merged_contexts[0]["event_id"], "legacy-click")
                duplicate = database.get_job("legacy-pending")
                self.assertEqual(duplicate["state"], "duplicate")
                self.assertEqual(
                    duplicate["last_error_code"],
                    "duplicate_active_infohash_migration",
                )
                detail = database.job_detail("legacy-pending")
                self.assertEqual(
                    detail["timeline"][-1]["structured"]["reason"],
                    "active_infohash_migration",
                )
                indexes = database.connect().execute("PRAGMA index_list(jobs)").fetchall()
                self.assertIn(
                    "idx_jobs_active_infohash_unique",
                    {str(row["name"]) for row in indexes},
                )
                service = SourceContextService(database)
                self.assertEqual(
                    service.store.job_by_hash("f" * 40, "movies")["job_id"],
                    "legacy-materialized",
                )
                status, response = service.handle(
                    _payload(
                        event_id="post-migration",
                        infohash="f" * 40,
                        title="Otro titulo 2020",
                    )
                )
                self.assertEqual((status, response["action"]), (200, "appended"))
                self.assertEqual(response["job_id"], "legacy-materialized")
            finally:
                database.close()

    def test_migration_normalizes_spaced_uppercase_hash_before_deduplication(self) -> None:
        with _temporary_directory() as temporary:
            path = Path(temporary) / "orchestrator.db"
            connection = sqlite3.connect(path)
            connection.executescript(SCHEMA)
            for job_id, infohash, source_path in (
                ("spaced", f" {'A' * 40} ", None),
                ("exact", "a" * 40, "/data/movie"),
            ):
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, source_uid, infohash, origin, category, name, state,
                        source_path, created_at, updated_at
                    ) VALUES(?, ?, ?, 'qbt', 'movies', 'Legacy', 'waiting_stable', ?, 1, 1)
                    """,
                    (job_id, f"legacy:{job_id}", infohash, source_path),
                )
            connection.commit()
            connection.close()

            database = Database(path)
            database.initialize()
            try:
                rows = database.connect().execute(
                    "SELECT job_id, infohash, state FROM jobs ORDER BY job_id"
                ).fetchall()
                self.assertEqual({str(row["infohash"]) for row in rows}, {"a" * 40})
                active = [row for row in rows if str(row["state"]) != "duplicate"]
                self.assertEqual([str(row["job_id"]) for row in active], ["exact"])
                self.assertEqual(
                    database.get_active_job_by_infohash(f" {'A' * 40} ")["job_id"],
                    "exact",
                )
            finally:
                database.close()

    def test_migration_aborts_cross_category_duplicates_without_mutating_jobs(self) -> None:
        with _temporary_directory() as temporary:
            path = Path(temporary) / "orchestrator.db"
            connection = sqlite3.connect(path)
            connection.executescript(SCHEMA)
            originals = (
                ("movie", "legacy:movie", f" {'B' * 40} ", "movies"),
                ("series", "legacy:series", "b" * 40, "tv"),
            )
            for job_id, source_uid, infohash, category in originals:
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, source_uid, infohash, origin, category, name, state,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, 'bridge', ?, 'Legacy', 'source_submitted', 1, 1)
                    """,
                    (job_id, source_uid, infohash, category),
                )
            connection.commit()
            connection.close()

            database = Database(path)
            with self.assertRaisesRegex(RuntimeError, "categorias distintas"):
                database.initialize()
            database.close()

            verification = sqlite3.connect(path)
            try:
                rows = verification.execute(
                    "SELECT job_id, infohash, category, state FROM jobs ORDER BY job_id"
                ).fetchall()
                self.assertEqual(
                    rows,
                    [
                        ("movie", f" {'B' * 40} ", "movies", "source_submitted"),
                        ("series", "b" * 40, "tv", "source_submitted"),
                    ],
                )
                self.assertEqual(
                    verification.execute("SELECT COUNT(*) FROM job_events").fetchone()[0],
                    0,
                )
            finally:
                verification.close()

    def test_migration_consolidates_delivery_per_click_before_deduplicating_title(self) -> None:
        with _temporary_directory() as temporary:
            path = Path(temporary) / "orchestrator.db"
            connection = sqlite3.connect(path)
            connection.executescript(SCHEMA)
            now = time.time()

            def context(event_id: str, title: str, state: str, received_at: float) -> dict:
                stored = {
                    key: value
                    for key, value in _payload(
                        event_id=event_id,
                        infohash="c" * 40,
                        title=title,
                        state=state,
                    ).items()
                    if key != "schema_version"
                }
                stored["received_at"] = received_at
                return stored

            job_contexts = (
                (
                    "legacy-one",
                    [
                        context("accepted-click", "Titulo compartido", "accepted", now - 4),
                        context("same-click", "Titulo misma entrega", "accepted", now - 3),
                    ],
                ),
                (
                    "legacy-two",
                    [
                        context("failed-click", "Titulo compartido", "failed", now - 2),
                        context("same-click", "Titulo misma entrega", "failed", now - 1),
                    ],
                ),
            )
            for index, (job_id, contexts) in enumerate(job_contexts):
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, source_uid, infohash, origin, category, name, state,
                        created_at, updated_at, source_meta_json
                    ) VALUES(?, ?, ?, 'bridge', 'movies', 'Legacy',
                             'source_submitted', ?, ?, ?)
                    """,
                    (
                        job_id,
                        f"legacy:{job_id}",
                        "c" * 40,
                        index + 1,
                        index + 1,
                        json.dumps({"source_contexts": contexts}, ensure_ascii=False),
                    ),
                )
            connection.commit()
            connection.close()

            database = Database(path)
            database.initialize()
            try:
                active = database.get_active_job_by_infohash("c" * 40)
                contexts = json.loads(active["source_meta_json"])["source_contexts"]
                by_title = {item["source_title"]: item for item in contexts}
                self.assertEqual(
                    by_title["Titulo compartido"]["delivery_state"], "accepted"
                )
                self.assertEqual(
                    by_title["Titulo compartido"]["event_id"], "accepted-click"
                )
                self.assertEqual(
                    by_title["Titulo misma entrega"]["delivery_state"], "failed"
                )
                self.assertEqual(
                    by_title["Titulo misma entrega"]["event_id"], "same-click"
                )
            finally:
                database.close()


class SourceContextStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _temporary_directory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "orchestrator.db")
        self.database.initialize()
        self.service = SourceContextService(
            self.database,
            identity_snapshot_provider=lambda: {
                "revision": 7,
                "rules": {"schema_version": 1},
            },
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def test_first_intent_creates_neutral_pending_job_and_exact_context_shape(self) -> None:
        status, response = self.service.handle(_payload())

        self.assertEqual(status, 201)
        self.assertEqual(response["action"], "created")
        jobs = self.database.latest_jobs()
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["state"], "source_submitted")
        self.assertEqual(job["origin"], "bridge")
        self.assertEqual(job["name"], NEUTRAL_JOB_NAME)
        self.assertEqual(job["infohash"], "a" * 40)
        self.assertIsNone(job["qbt_hash"])
        detail = self.database.job_detail(job["job_id"])
        self.assertEqual(detail["source_meta"]["identity_rules"]["revision"], 7)
        contexts = detail["source_meta"]["source_contexts"]
        self.assertEqual(len(contexts), 1)
        self.assertEqual(
            set(contexts[0]),
            {
                "event_id",
                "source",
                "infohash",
                "destination",
                "source_title",
                "route",
                "delivery_state",
                "created_at",
                "received_at",
            },
        )

    def test_job_and_canonical_event_roll_back_together(self) -> None:
        with patch.object(
            self.database, "append_event", side_effect=OSError("fallo de evento")
        ):
            with self.assertRaisesRegex(OSError, "fallo de evento"):
                self.service.handle(_payload())

        self.assertEqual(self.database.latest_jobs(), [])
        self.assertEqual(
            self.database.connect().execute("SELECT COUNT(*) FROM job_events").fetchone()[0],
            0,
        )

        status, response = self.service.handle(_payload())
        self.assertEqual((status, response["action"]), (201, "created"))
        detail = self.database.job_detail(response["job_id"])
        self.assertEqual(detail["timeline"][0]["structured"]["action"], "created")
        contexts = detail["source_meta"]["source_contexts"]
        self.assertEqual(contexts[0]["source_title"], _payload()["source_title"])
        self.assertTrue(any(event["phase"] == "source_context" for event in detail["timeline"]))
        source_event = next(
            event for event in detail["timeline"] if event["phase"] == "source_context"
        )
        self.assertNotIn("source_title", source_event["structured"])
        self.assertEqual(
            len(source_event["structured"]["source_title_fingerprint"]), 64
        )
        self.assertNotIn(
            _payload()["source_title"],
            json.dumps(source_event["structured"], ensure_ascii=False),
        )

    def test_context_arriving_after_materialization_attaches_to_the_existing_job(self) -> None:
        existing = self.database.create_job(
            "qbt:already-materialized",
            "qbt",
            "movies",
            "Nombre físico real",
            state="waiting_stable",
            infohash="a" * 40,
            qbt_hash="a" * 40,
            source_meta_json=json.dumps({"identity_rules": {"revision": 4}}),
        )

        status, response = self.service.handle(_payload(state="accepted"))

        self.assertEqual(status, 200)
        self.assertEqual(response["job_id"], existing["job_id"])
        self.assertEqual(len(self.database.latest_jobs()), 1)
        updated = self.database.get_job(existing["job_id"])
        self.assertEqual(updated["name"], "Nombre físico real")
        self.assertEqual(updated["state"], "waiting_stable")
        meta = json.loads(updated["source_meta_json"])
        self.assertEqual(meta["identity_rules"], {"revision": 4})
        self.assertEqual(meta["source_contexts"][0]["delivery_state"], "accepted")

    def test_same_event_is_idempotent_and_records_the_duplicate(self) -> None:
        first = self.service.handle(_payload())
        first_updated_at = self.database.latest_jobs()[0]["updated_at"]
        second = self.service.handle(_payload())

        self.assertEqual(first[0], 201)
        self.assertEqual(second[0], 200)
        self.assertEqual(second[1]["action"], "duplicate")
        jobs = self.database.latest_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["updated_at"], first_updated_at)
        detail = self.database.job_detail(jobs[0]["job_id"])
        source_events = [event for event in detail["timeline"] if event["phase"] == "source_context"]
        self.assertEqual(len(source_events), 2)
        self.assertEqual(source_events[-1]["event_type"], "skipped")

    def test_same_event_advances_delivery_state_but_never_regresses(self) -> None:
        self.service.handle(_payload(event_id="one-click", state="intent"))
        status, updated = self.service.handle(
            _payload(event_id="one-click", state="accepted")
        )

        self.assertEqual((status, updated["action"]), (200, "updated"))
        job = self.database.latest_jobs()[0]
        context = json.loads(job["source_meta_json"])["source_contexts"][0]
        self.assertEqual(context["delivery_state"], "accepted")

        status, stale = self.service.handle(
            _payload(event_id="one-click", state="intent")
        )
        self.assertEqual((status, stale["action"]), (200, "stale_event"))
        context = json.loads(
            self.database.latest_jobs()[0]["source_meta_json"]
        )["source_contexts"][0]
        self.assertEqual(context["delivery_state"], "accepted")

    def test_reusing_an_event_id_with_another_title_is_a_conflict(self) -> None:
        self.service.handle(_payload(event_id="one-click", title="Título uno"))

        status, response = self.service.handle(
            _payload(event_id="one-click", title="Título contradictorio")
        )

        self.assertEqual(status, 409)
        self.assertEqual(response["error"], "event_conflict")
        self.assertFalse(response["ok"])
        job = self.database.latest_jobs()[0]
        contexts = json.loads(job["source_meta_json"])["source_contexts"]
        self.assertEqual([item["source_title"] for item in contexts], ["Título uno"])

    def test_reusing_an_event_id_with_another_hash_is_a_global_conflict(self) -> None:
        self.service.handle(_payload(event_id="same-click", infohash="a" * 40))

        status, response = self.service.handle(
            _payload(event_id="same-click", infohash="b" * 40)
        )

        self.assertEqual(status, 409)
        self.assertEqual(response["error"], "event_conflict")
        self.assertFalse(response["ok"])
        self.assertEqual(len(self.database.latest_jobs()), 1)
        detail = self.database.job_detail(response["job_id"])
        self.assertEqual(
            detail["timeline"][-1]["structured"]["infohash"], "b" * 40
        )

    def test_capped_event_id_remains_claimed_globally_by_job_events(self) -> None:
        for index in range(3):
            self.service.handle(
                _payload(event_id=f"seed-{index}", title=f"Titulo {index}")
            )
        status, capped = self.service.handle(
            _payload(event_id="capped-click", title="Cuarto titulo")
        )
        self.assertEqual((status, capped["action"]), (200, "title_limit"))

        status, replay = self.service.handle(
            _payload(
                event_id="capped-click",
                infohash="b" * 40,
                title="Cuarto titulo",
            )
        )
        self.assertEqual(status, 409)
        self.assertEqual(replay["error"], "event_conflict")
        self.assertEqual(len(self.database.latest_jobs()), 1)

    def test_replaced_same_title_still_preserves_each_sources_event_claim(self) -> None:
        self.service.handle(
            _payload(
                source="buscador-pro",
                event_id="pro-click",
                title="Titulo compartido",
            )
        )
        self.service.handle(
            _payload(
                source="buscador-jackett",
                event_id="jackett-click",
                title="Titulo compartido",
            )
        )

        status, replay = self.service.handle(
            _payload(
                source="buscador-pro",
                event_id="pro-click",
                infohash="c" * 40,
                title="Titulo compartido",
            )
        )
        self.assertEqual(status, 409)
        self.assertEqual(replay["error"], "event_conflict")
        self.assertEqual(len(self.database.latest_jobs()), 1)

    def test_replay_after_same_title_replacement_does_not_mutate_provenance(self) -> None:
        self.service.handle(
            _payload(event_id="click-a", title="Titulo compartido")
        )
        self.service.handle(
            _payload(event_id="click-b", title="Titulo compartido")
        )

        status, replay = self.service.handle(
            _payload(event_id="click-a", title="Titulo compartido")
        )

        self.assertEqual((status, replay["action"]), (200, "duplicate"))
        contexts = json.loads(self.database.latest_jobs()[0]["source_meta_json"])[
            "source_contexts"
        ]
        self.assertEqual(contexts[0]["event_id"], "click-b")

    def test_terminal_event_id_cannot_change_its_original_title(self) -> None:
        _status, created = self.service.handle(
            _payload(event_id="terminal-click", title="Titulo original")
        )
        self.database.transition(created["job_id"], "done", "cleanup", "Finalizado")

        status, response = self.service.handle(
            _payload(event_id="terminal-click", title="Titulo cambiado")
        )

        self.assertEqual(status, 409)
        self.assertEqual(response["error"], "event_conflict")
        self.assertEqual(self.database.get_job(created["job_id"])["state"], "done")

    def test_titles_are_deduplicated_capped_at_three_and_expire_after_24h(self) -> None:
        for index in range(1, 5):
            status, response = self.service.handle(
                _payload(event_id=f"event-{index}", title=f"Titulo distinto {index}")
            )
            self.assertIn(status, (200, 201))
        self.assertEqual(response["action"], "title_limit")
        job = self.database.latest_jobs()[0]
        meta = json.loads(job["source_meta_json"])
        self.assertEqual(len(meta["source_contexts"]), MAX_SOURCE_TITLES)

        for context in meta["source_contexts"]:
            context["received_at"] = time.time() - CONTEXT_TTL_SECONDS - 1
        self.database.update_job(
            job["job_id"], source_meta_json=json.dumps(meta, ensure_ascii=False)
        )
        status, response = self.service.handle(
            _payload(event_id="event-new", title="Titulo nuevo tras caducar")
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["context_count"], 1)

    def test_accepted_replaces_intent_but_late_intent_does_not_regress(self) -> None:
        self.service.handle(_payload(event_id="intent-1", state="intent"))
        self.service.handle(_payload(event_id="accepted-1", state="accepted"))
        status, response = self.service.handle(_payload(event_id="intent-late", state="intent"))

        self.assertEqual(status, 200)
        self.assertEqual(response["action"], "stale_event")
        job = self.database.latest_jobs()[0]
        context = json.loads(job["source_meta_json"])["source_contexts"][0]
        self.assertEqual(context["event_id"], "accepted-1")
        self.assertEqual(context["delivery_state"], "accepted")

    def test_failed_without_job_does_not_create_and_failed_pending_discards(self) -> None:
        status, response = self.service.handle(
            _payload(event_id="failed-alone", state="failed")
        )
        self.assertEqual((status, response["action"]), (200, "failed_without_job"))
        self.assertEqual(self.database.latest_jobs(), [])

        self.service.handle(_payload(event_id="delivery-1"))
        status, response = self.service.handle(
            _payload(event_id="delivery-1", state="failed")
        )
        self.assertEqual((status, response["action"]), (200, "discarded"))
        job = self.database.latest_jobs()[0]
        self.assertEqual(job["state"], "discarded")
        self.assertEqual(job["last_error_code"], "source_delivery_failed")

    def test_late_event_does_not_reopen_terminal_job(self) -> None:
        _status, created = self.service.handle(_payload())
        self.database.transition(
            created["job_id"], "done", "cleanup", "Trabajo terminado"
        )

        status, response = self.service.handle(
            _payload(event_id="accepted-late", state="accepted")
        )
        self.assertEqual((status, response["action"]), (200, "terminal_unchanged"))
        self.assertEqual(self.database.get_job(created["job_id"])["state"], "done")
        self.assertEqual(len(self.database.latest_jobs()), 1)
        detail = self.database.job_detail(created["job_id"])
        self.assertEqual(detail["timeline"][-1]["event_type"], "skipped")
        self.assertEqual(
            detail["timeline"][-1]["structured"]["action"], "terminal_unchanged"
        )

    def test_failed_after_materialization_is_recorded_without_state_regression(self) -> None:
        _status, created = self.service.handle(_payload())
        self.database.transition(
            created["job_id"], "waiting_stable", "qbt", "Materializado"
        )

        status, response = self.service.handle(
            _payload(event_id="failed-late", state="failed")
        )
        self.assertEqual(response["action"], "failed_after_materialization")
        self.assertEqual(status, 200)
        self.assertEqual(
            self.database.get_job(created["job_id"])["state"], "waiting_stable"
        )

    def test_failed_secondary_context_does_not_discard_an_accepted_context(self) -> None:
        self.service.handle(
            _payload(event_id="accepted-a", title="Título A", state="accepted")
        )

        status, response = self.service.handle(
            _payload(event_id="failed-b", title="Título B", state="failed")
        )

        self.assertEqual((status, response["action"]), (200, "failed_context_preserved"))
        job = self.database.latest_jobs()[0]
        self.assertEqual(job["state"], "source_submitted")
        contexts = json.loads(job["source_meta_json"])["source_contexts"]
        self.assertEqual(
            {item["delivery_state"] for item in contexts}, {"accepted", "failed"}
        )

    def test_failed_second_click_same_title_preserves_the_accepted_delivery(self) -> None:
        self.service.handle(
            _payload(event_id="accepted-click", state="accepted")
        )

        status, response = self.service.handle(
            _payload(event_id="failed-click", state="failed")
        )

        self.assertEqual((status, response["action"]), (200, "failed_context_preserved"))
        job = self.database.latest_jobs()[0]
        self.assertEqual(job["state"], "source_submitted")
        contexts = json.loads(job["source_meta_json"])["source_contexts"]
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["event_id"], "accepted-click")
        self.assertEqual(contexts[0]["delivery_state"], "accepted")
        detail = self.database.job_detail(job["job_id"])
        self.assertEqual(
            detail["timeline"][-1]["structured"]["action"],
            "failed_context_preserved",
        )

    def test_destination_conflict_does_not_mutate_existing_job(self) -> None:
        _status, created = self.service.handle(_payload(destination="movies"))
        status, response = self.service.handle(
            _payload(event_id="tv-event", destination="tv")
        )

        self.assertEqual(status, 409)
        self.assertEqual(response["error"], "destination_conflict")
        self.assertEqual(self.database.get_job(created["job_id"])["category"], "movies")
        detail = self.database.job_detail(created["job_id"])
        self.assertEqual(
            detail["timeline"][-1]["structured"]["action"], "destination_conflict"
        )

    def test_terminal_cross_category_event_is_still_a_destination_conflict(self) -> None:
        _status, created = self.service.handle(_payload(destination="movies"))
        self.database.transition(created["job_id"], "done", "cleanup", "Finalizado")

        status, response = self.service.handle(
            _payload(event_id="late-tv", destination="tv")
        )

        self.assertEqual(status, 409)
        self.assertEqual(response["error"], "destination_conflict")
        self.assertEqual(self.database.get_job(created["job_id"])["state"], "done")
        self.assertEqual(
            self.database.job_detail(created["job_id"])["timeline"][-1][
                "structured"
            ]["action"],
            "destination_conflict",
        )

    def test_source_title_is_raw_only_inside_source_meta_contexts(self) -> None:
        raw_title = "Titulo Origen Privado Unico 2001"
        _status, created = self.service.handle(_payload(title=raw_title))
        job_id = created["job_id"]

        self.database.update_job(
            job_id,
            identity_json=json.dumps(
                {
                    "query": raw_title,
                    "source_context": {
                        "source": "buscador-pro",
                        "event_id": "event-1",
                        "source_title": raw_title,
                    },
                },
                ensure_ascii=False,
            ),
            result_json=json.dumps(
                {"details": {"source_title": raw_title, "query": raw_title}},
                ensure_ascii=False,
            ),
        )
        self.database.add_event(
            job_id,
            "identity",
            "warning",
            f"Rechazo duradero: {raw_title}",
            {"source_title": raw_title, "query": raw_title},
        )

        row = self.database.get_job(job_id)
        source_meta = str(row["source_meta_json"])
        durable = "\n".join(
            [
                str(row["identity_json"]),
                str(row["result_json"]),
                *(
                    f"{event['message']} {event['structured_json']}"
                    for event in self.database.events_for_job(job_id)
                ),
            ]
        )

        self.assertIn(raw_title, source_meta)
        self.assertNotIn(raw_title, durable)
        self.assertIn("source_title_fingerprint", durable)

    def test_concurrent_events_create_one_job_and_at_most_three_titles(self) -> None:
        payloads = [
            _payload(event_id=f"parallel-{index}", title=f"Titulo paralelo {index}")
            for index in range(12)
        ]

        def send(payload):
            try:
                return self.service.handle(payload)
            finally:
                self.database.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(send, payloads))

        self.assertEqual(sum(status == 201 for status, _payload in results), 1)
        jobs = self.database.latest_jobs()
        self.assertEqual(len(jobs), 1)
        contexts = json.loads(jobs[0]["source_meta_json"])["source_contexts"]
        self.assertLessEqual(len(contexts), MAX_SOURCE_TITLES)
        self.assertEqual(len({item["source_title"] for item in contexts}), len(contexts))

    def test_http_and_engine_creation_race_keeps_one_active_job_per_hash(self) -> None:
        barrier = Barrier(2)

        def receive_context():
            try:
                barrier.wait()
                return self.service.handle(_payload())
            finally:
                self.database.close()

        def create_engine_job():
            try:
                barrier.wait()
                return self.database.create_job(
                    "qbt:race",
                    "qbt",
                    "movies",
                    "Nombre físico",
                    state="waiting_stable",
                    infohash="a" * 40,
                    qbt_hash="a" * 40,
                    source_meta_json=json.dumps({"identity_rules": {"revision": 1}}),
                )
            finally:
                self.database.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(receive_context),
                executor.submit(create_engine_job),
            ]
            for future in futures:
                future.result()

        active = self.database.connect().execute(
            """
            SELECT * FROM jobs
            WHERE lower(infohash)=? AND state NOT IN (
                'done', 'manual_review', 'duplicate', 'error_terminal', 'discarded'
            )
            """,
            ("a" * 40,),
        ).fetchall()
        self.assertEqual(len(active), 1)
        job = dict(active[0])
        self.assertTrue(json.loads(job["source_meta_json"])["source_contexts"])

    def test_expiration_discards_orphan_pending_context(self) -> None:
        _status, created = self.service.handle(_payload())
        job = self.database.get_job(created["job_id"])
        meta = json.loads(job["source_meta_json"])
        meta["source_contexts"][0]["received_at"] = (
            time.time() - CONTEXT_TTL_SECONDS - 1
        )
        self.database.update_job(
            job["job_id"], source_meta_json=json.dumps(meta, ensure_ascii=False)
        )

        self.assertEqual(self.service.store.expire_stale_pending(), 1)
        expired = self.database.get_job(job["job_id"])
        self.assertEqual(expired["state"], "discarded")
        self.assertEqual(expired["last_error_code"], "source_context_expired")

    def test_expiration_follows_context_after_transport_changes_origin(self) -> None:
        _status, created = self.service.handle(_payload(event_id="rdt-expired"))
        job = self.database.get_job(created["job_id"])
        meta = json.loads(job["source_meta_json"])
        meta["source_contexts"][0]["received_at"] = (
            time.time() - CONTEXT_TTL_SECONDS - 1
        )
        self.database.update_job(
            job["job_id"],
            origin="rdt",
            state="waiting_materialization",
            source_meta_json=json.dumps(meta, ensure_ascii=False),
        )

        self.assertEqual(self.service.store.expire_stale_pending(), 1)
        expired = self.database.get_job(job["job_id"])
        self.assertEqual(expired["state"], "discarded")
        self.assertEqual(expired["last_error_code"], "source_context_expired")

    def test_expiration_never_discards_legacy_jobs_without_source_context(self) -> None:
        legacy = self.database.create_job(
            "legacy-rdt-pending",
            "rdt",
            "movies",
            "Trabajo antiguo",
            state="waiting_materialization",
            source_meta_json=json.dumps({"identity_rules": {}}),
        )

        self.assertEqual(self.service.store.expire_stale_pending(), 0)
        self.assertEqual(
            self.database.get_job(legacy["job_id"])["state"],
            "waiting_materialization",
        )

    def test_pending_and_correlatable_follow_valid_context_not_job_origin(self) -> None:
        _status, created = self.service.handle(_payload())
        job_id = created["job_id"]
        self.database.update_job(job_id, origin="qbt")

        self.assertTrue(self.service.store.has_pending("movies"))
        self.assertTrue(self.service.store.has_correlatable("movies"))

        self.database.update_job(job_id, state="waiting_stable")
        self.assertFalse(self.service.store.has_pending("movies"))
        self.assertTrue(self.service.store.has_correlatable("movies"))

        self.database.update_job(job_id, state="done")
        self.assertFalse(self.service.store.has_pending("movies"))
        self.assertFalse(self.service.store.has_correlatable("movies"))

    def test_correlatable_search_has_no_arbitrary_recent_job_limit(self) -> None:
        _status, created = self.service.handle(_payload())
        self.database.update_job(created["job_id"], origin="fs", updated_at=1)
        for index in range(101):
            self.database.create_job(
                f"noise:{index}",
                "bridge",
                "movies",
                f"Sin contexto {index}",
                state="received",
            )

        self.assertTrue(self.service.store.has_correlatable("movies"))

    def test_malformed_source_meta_is_neither_pending_nor_correlatable(self) -> None:
        malformed = _payload(title="/private/path")
        malformed.pop("schema_version")
        malformed["received_at"] = time.time()
        self.database.create_job(
            "invalid-context",
            "qbt",
            "movies",
            "Contexto inválido",
            state="source_submitted",
            source_meta_json=json.dumps({"source_contexts": [malformed]}),
        )

        self.assertFalse(self.service.store.has_pending("movies"))
        self.assertFalse(self.service.store.has_correlatable("movies"))


class SourceContextEngineRaceTests(unittest.TestCase):
    def _engine(self, grace: int = 90):
        temporary = _temporary_directory()
        root = Path(temporary.name)
        config = _config(root, grace=grace)
        config.ensure_directories()
        database = Database(root / "orchestrator.db")
        database.initialize()
        engine = Engine(config, database)
        return temporary, config, database, engine

    def test_identity_review_does_not_duplicate_source_title_on_disk_or_db(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            raw_title = "Titulo Privado Revision 2001"
            _status, created = engine.source_context.handle(
                _payload(title=raw_title, state="accepted")
            )
            job = database.get_job(created["job_id"])
            job_root = config.workshop_root / str(job["job_id"])
            original = job_root / "original"
            original.mkdir(parents=True)
            (original / "archivo.mkv").write_bytes(b"movie")
            error = ResolverAmbiguous(
                "Identidad ambigua",
                {
                    "reason_code": "source_title_policy",
                    "source_title": raw_title,
                    "query": raw_title,
                    "source_fallback_attempts": [
                        {
                            "source": "buscador-pro",
                            "event_id": "event-1",
                            "source_title": raw_title,
                            "status": "REJECTED_SOURCE_TITLE",
                        }
                    ],
                },
            )

            with patch.object(engine, "_cleanup_clients"):
                engine._send_identity_review(job, job_root, error)

            updated = database.get_job(job["job_id"])
            review = Path(str(updated["stage_path"]))
            persisted = "\n".join(
                [
                    str(updated["result_json"]),
                    (review / "reason.json").read_text(encoding="utf-8"),
                    (review / "Revision manual.txt").read_text(encoding="utf-8"),
                    *(
                        f"{event['message']} {event['structured_json']}"
                        for event in database.events_for_job(job["job_id"])
                    ),
                ]
            )

            self.assertIn(raw_title, str(updated["source_meta_json"]))
            self.assertNotIn(raw_title, persisted)
            self.assertIn("source_title_fingerprint", persisted)
        finally:
            database.close()
            temporary.cleanup()

    def test_filesystem_race_correlates_qbt_by_hash_and_uses_physical_name(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            infohash = "b" * 40
            engine.source_context.handle(_payload(infohash=infohash))
            item = config.complete_root / "movies" / "La Momia 2 [Remux]"
            content = item / "archivo-con-nombre-distinto.mkv"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")
            torrent = {
                "hash": infohash,
                "category": "",
                "name": "Nombre engañoso S01E02",
                "content_path": str(content),
                "added_on": 123,
            }

            class FakeQbt:
                def torrents(self, _filter):
                    return [torrent]

            class FakeRdt:
                def torrents(self, _filter):
                    return []

            engine.qbt = FakeQbt()
            engine.rdt = FakeRdt()
            engine._register_materialized("movies", item)

            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "waiting_stable")
            self.assertEqual(jobs[0]["qbt_hash"], infohash)
            self.assertEqual(jobs[0]["category"], "movies")
            self.assertEqual(jobs[0]["source_path"], str(item))
            self.assertEqual(jobs[0]["name"], item.name)
        finally:
            database.close()
            temporary.cleanup()

    def test_intent_before_watch_is_adopted_and_submitted(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            infohash = "9" * 40
            engine.source_context.handle(_payload(infohash=infohash))
            torrent_path = config.watch_inbox / "pelicula.torrent"
            torrent_path.parent.mkdir(parents=True, exist_ok=True)
            torrent_path.write_bytes(b"torrent")

            with patch(
                "arr_orchestrator.engine.torrent_info",
                return_value=(infohash, "Nombre fisico de la pelicula"),
            ), patch.object(engine, "_submit_rdt") as submit:
                engine._handle_watch_path(torrent_path)

            job = database.get_active_job_by_infohash(infohash)
            self.assertEqual(job["state"], "received")
            self.assertEqual(job["origin"], "watch")
            self.assertEqual(job["name"], "Nombre fisico de la pelicula")
            submit.assert_called_once()
            detail = database.job_detail(job["job_id"])
            self.assertEqual(
                detail["timeline"][-1]["structured"]["action"], "watch_linked"
            )
        finally:
            database.close()
            temporary.cleanup()

    def test_concurrent_intent_and_watch_converge_on_one_submitted_job(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            infohash = "8" * 40
            torrent_path = config.watch_inbox / "carrera.torrent"
            torrent_path.parent.mkdir(parents=True, exist_ok=True)
            torrent_path.write_bytes(b"torrent")
            create_reached = Event()
            context_committed = Event()
            original_create = database.create_job

            def delayed_create(*args, **kwargs):
                create_reached.set()
                self.assertTrue(context_committed.wait(timeout=5))
                try:
                    return original_create(*args, **kwargs)
                finally:
                    database.close()

            def receive_context():
                self.assertTrue(create_reached.wait(timeout=5))
                try:
                    return engine.source_context.handle(_payload(infohash=infohash))
                finally:
                    context_committed.set()
                    database.close()

            def handle_watch():
                try:
                    return engine._handle_watch_path(torrent_path)
                finally:
                    database.close()

            with patch(
                "arr_orchestrator.engine.torrent_info",
                return_value=(infohash, "Nombre fisico concurrente"),
            ), patch.object(database, "create_job", side_effect=delayed_create), patch.object(
                engine, "_submit_rdt"
            ) as submit, ThreadPoolExecutor(max_workers=2) as executor:
                watch_future = executor.submit(handle_watch)
                context_future = executor.submit(receive_context)
                watch_future.result()
                context_future.result()

            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "received")
            self.assertEqual(jobs[0]["name"], "Nombre fisico concurrente")
            submit.assert_called_once()
        finally:
            database.close()
            temporary.cleanup()

    def test_rdt_helper_preserves_legacy_silence_without_source_context(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            item = config.complete_root / "movies" / "Trabajo RDT antiguo"
            item.mkdir(parents=True)
            job = database.create_job(
                "legacy-rdt-job",
                "rdt",
                "movies",
                "Trabajo RDT antiguo",
                state="source_submitted",
                infohash="e" * 40,
                source_meta_json=engine._new_job_source_meta_json(),
            )
            before = len(database.events_for_job(job["job_id"]))

            engine.source_context_correlation.attach_rdt(
                job,
                {
                    "hash": "e" * 40,
                    "id": "rdt-legacy",
                    "progress": 100,
                },
                item,
                "Correlacion legacy",
            )

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "waiting_stable")
            self.assertEqual(updated["rdt_id"], "rdt-legacy")
            self.assertEqual(len(database.events_for_job(job["job_id"])), before)
        finally:
            database.close()
            temporary.cleanup()

    def test_filesystem_race_correlates_rdt_by_hash_and_uses_physical_name(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            infohash = "c" * 40
            engine.source_context.handle(_payload(infohash=infohash))
            item = config.complete_root / "movies" / "Nombre fisico RDT"
            item.mkdir(parents=True)
            (item / "video.mkv").write_bytes(b"movie")
            torrent = {
                "hash": infohash,
                "id": "rdt-id",
                "content_path": str(item),
                "progress": 100,
            }

            class FakeQbt:
                def torrents(self, _filter):
                    return []

            class FakeRdt:
                def torrents(self, _filter):
                    return [torrent]

            engine.qbt = FakeQbt()
            engine.rdt = FakeRdt()
            engine._register_materialized("movies", item)

            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "waiting_stable")
            self.assertEqual(jobs[0]["rdt_id"], "rdt-id")
            self.assertEqual(jobs[0]["source_path"], str(item))
            self.assertEqual(jobs[0]["name"], item.name)
        finally:
            database.close()
            temporary.cleanup()

    def test_rdt_seen_before_file_exists_keeps_hash_path_until_materialization(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            infohash = "b" * 40
            engine.source_context.handle(_payload(infohash=infohash, state="accepted"))
            item = config.complete_root / "movies" / "Nombre fisico fugaz"

            class TransientRdt:
                def __init__(self):
                    self.calls = 0

                def torrents(self, _filter):
                    self.calls += 1
                    if self.calls == 1:
                        return [
                            {
                                "hash": infohash,
                                "id": "rdt-fugaz",
                                "content_path": str(item),
                                "progress": 10,
                            }
                        ]
                    return []

            engine.rdt = TransientRdt()
            engine._reconcile_rdt()

            waiting = database.get_active_job_by_infohash(infohash)
            self.assertEqual(waiting["state"], "waiting_materialization")
            self.assertEqual(waiting["rdt_id"], "rdt-fugaz")
            self.assertEqual(waiting["source_path"], str(item))
            self.assertEqual(waiting["name"], NEUTRAL_JOB_NAME)

            item.mkdir(parents=True)
            (item / "video.mkv").write_bytes(b"movie")
            engine._register_materialized("movies", item)

            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["job_id"], waiting["job_id"])
            self.assertEqual(jobs[0]["state"], "waiting_stable")
            self.assertEqual(jobs[0]["name"], item.name)
        finally:
            database.close()
            temporary.cleanup()

    def test_rdt_materialization_before_context_adopts_hash_then_same_job(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            infohash = "9" * 40
            item = config.complete_root / "movies" / "Descarga antes que la ficha"
            item.mkdir(parents=True)
            (item / "video.mkv").write_bytes(b"movie")
            torrent = {
                "hash": infohash,
                "id": "rdt-retenido",
                "content_path": str(item),
                "progress": 100,
            }

            class EmptyQbt:
                def torrents(self, _filter):
                    return []

            class RetainedRdt:
                def torrents(self, _filter):
                    return [torrent]

            engine.qbt = EmptyQbt()
            engine.rdt = RetainedRdt()
            engine._register_materialized("movies", item)

            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["infohash"], infohash)
            self.assertEqual(jobs[0]["rdt_id"], "rdt-retenido")
            materialized_job_id = jobs[0]["job_id"]

            status, response = engine.source_context.handle(
                _payload(infohash=infohash, state="accepted")
            )
            self.assertEqual(status, 200)
            self.assertEqual(response["job_id"], materialized_job_id)
            self.assertEqual(len(database.latest_jobs()), 1)
            self.assertTrue(
                engine.source_context.store.has_context(
                    database.get_job(materialized_job_id)
                )
            )
        finally:
            database.close()
            temporary.cleanup()

    def test_rdt_windows_file_path_maps_to_materialized_folder(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            item = config.complete_root / "movies" / "The Visitors 1993"
            item.mkdir(parents=True)
            physical_file = item / "The Visitors 1993.mp4"
            physical_file.write_bytes(b"movie")

            translated = engine._translate_rdt_path(
                "C:\\Downloads\\movies\\The Visitors 1993.mp4\\"
            )

            self.assertEqual(translated, item)
        finally:
            database.close()
            temporary.cleanup()

    def test_rdt_windows_file_path_adopts_existing_context_job(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            infohash = "8" * 40
            source_status, source_response = engine.source_context.handle(
                _payload(infohash=infohash, state="accepted")
            )
            self.assertEqual(source_status, 201)

            item = config.complete_root / "movies" / "The Visitors 1993"
            item.mkdir(parents=True)
            (item / "The Visitors 1993.mp4").write_bytes(b"movie")
            torrent = {
                "hash": infohash,
                "id": "rdt-windows-path",
                "content_path": "C:\\Downloads\\movies\\The Visitors 1993.mp4\\",
                "progress": 100,
            }

            class EmptyQbt:
                def torrents(self, _filter):
                    return []

            class RetainedRdt:
                def torrents(self, _filter):
                    return [torrent]

            engine.qbt = EmptyQbt()
            engine.rdt = RetainedRdt()
            engine._register_materialized("movies", item)

            jobs = database.latest_jobs()
            active = [job for job in jobs if job["state"] not in TERMINAL_STATES]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["job_id"], source_response["job_id"])
            self.assertEqual(active[0]["infohash"], infohash)
            self.assertEqual(active[0]["rdt_id"], "rdt-windows-path")
            self.assertEqual(active[0]["source_path"], str(item))
            self.assertEqual(active[0]["name"], item.name)
        finally:
            database.close()
            temporary.cleanup()

    def test_rdt_windows_path_reconcile_merges_preexisting_source_and_fs_jobs(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            infohash = "7" * 40
            source_status, source_response = engine.source_context.handle(
                _payload(infohash=infohash, state="accepted")
            )
            self.assertEqual(source_status, 201)

            item = config.complete_root / "movies" / "The Visitors 1993"
            item.mkdir(parents=True)
            (item / "The Visitors 1993.mp4").write_bytes(b"movie")
            materialized = database.create_job(
                "fs-rdt-windows-existing",
                "fs",
                "movies",
                item.name,
                state="waiting_stable",
                source_path=str(item),
                source_meta_json=engine._new_job_source_meta_json(),
            )

            class EmptyQbt:
                def torrents(self, _filter):
                    return []

            class RetainedRdt:
                def torrents(self, _filter):
                    return [
                        {
                            "hash": infohash,
                            "id": "rdt-windows-reconcile",
                            "content_path": (
                                "C:\\Downloads\\movies\\The Visitors 1993.mp4\\"
                            ),
                            "progress": 100,
                        }
                    ]

            engine.qbt = EmptyQbt()
            engine.rdt = RetainedRdt()
            engine._reconcile_rdt()
            engine._reconcile_complete()

            jobs = database.latest_jobs()
            active = [job for job in jobs if job["state"] not in TERMINAL_STATES]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["job_id"], materialized["job_id"])
            self.assertEqual(active[0]["infohash"], infohash)
            self.assertEqual(active[0]["rdt_id"], "rdt-windows-reconcile")
            self.assertTrue(engine.source_context.store.has_context(active[0]))
            self.assertEqual(
                database.get_job(source_response["job_id"])["state"], "duplicate"
            )
        finally:
            database.close()
            temporary.cleanup()

    def test_rdt_path_translation_does_not_adopt_internal_automation_roots(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            for category in ("movies_automatizacion", "trailers_automatizacion"):
                with self.subTest(category=category):
                    internal = config.complete_root / category / "internal.mkv"
                    internal.parent.mkdir(parents=True, exist_ok=True)
                    internal.write_bytes(b"internal")
                    self.assertIsNone(engine._translate_rdt_path(str(internal)))

            item = config.complete_root / "movies_automatizacion" / "internal.mkv"
            job = database.create_job(
                "fs-rdt-internal-root",
                "fs",
                "movies_automatizacion",
                item.name,
                state="waiting_stable",
                source_path=str(item),
                source_meta_json=engine._new_job_source_meta_json(),
            )
            engine.source_context_correlation.remember_rdt(
                [
                    {
                        "hash": "5" * 40,
                        "id": "rdt-internal-root",
                        "content_path": str(item),
                        "progress": 100,
                    }
                ]
            )
            adopted = engine.source_context_correlation.adopt_rdt_for_materialized_job(
                job, "movies_automatizacion", item
            )
            self.assertIsNone(adopted.get("infohash"))
            self.assertIsNone(adopted.get("rdt_id"))
        finally:
            database.close()
            temporary.cleanup()

    def test_rdt_adoption_and_simultaneous_context_converge_atomically(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            infohash = "6" * 40
            item = config.complete_root / "movies" / "Carrera RDT y ficha"
            item.mkdir(parents=True)
            materialized = database.create_job(
                "fs-rdt-race",
                "fs",
                "movies",
                item.name,
                state="waiting_stable",
                source_path=str(item),
                source_meta_json=engine._new_job_source_meta_json(),
            )
            torrent = {
                "hash": infohash,
                "id": "rdt-race",
                "content_path": str(item),
                "progress": 100,
            }

            class RetainedRdt:
                def torrents(self, _filter):
                    return [torrent]

            engine.rdt = RetainedRdt()
            original_attach = engine.source_context_correlation.attach_rdt
            calls = 0

            def attach_after_context(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    engine.source_context.handle(
                        _payload(infohash=infohash, state="accepted")
                    )
                return original_attach(*args, **kwargs)

            with patch.object(
                engine.source_context_correlation,
                "attach_rdt",
                side_effect=attach_after_context,
            ):
                adopted = engine.source_context_correlation.adopt_rdt_for_materialized_job(
                    materialized, "movies", item
                )

            active = [
                job
                for job in database.latest_jobs()
                if job["state"] not in TERMINAL_STATES
            ]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["job_id"], materialized["job_id"])
            self.assertEqual(active[0]["infohash"], infohash)
            self.assertEqual(active[0]["rdt_id"], "rdt-race")
            self.assertEqual(adopted["job_id"], materialized["job_id"])
            self.assertTrue(engine.source_context.store.has_context(active[0]))
        finally:
            database.close()
            temporary.cleanup()

    def test_correlation_grace_prevents_duplicate_fs_job(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            engine.source_context.handle(_payload())
            item = config.complete_root / "movies" / "Todavia sin transporte visible"
            item.mkdir(parents=True)

            class EmptyClient:
                def torrents(self, _filter):
                    return []

            engine.qbt = EmptyClient()
            engine.rdt = EmptyClient()
            engine._register_materialized("movies", item)

            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "source_submitted")
            self.assertEqual(jobs[0]["name"], NEUTRAL_JOB_NAME)
        finally:
            database.close()
            temporary.cleanup()

    def test_reconcile_cycle_reads_each_remote_inventory_only_once(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            engine.source_context.handle(_payload())
            for title in ("Item uno", "Item dos"):
                (config.complete_root / "movies" / title).mkdir(parents=True)

            class CountingClient:
                def __init__(self):
                    self.calls = 0

                def torrents(self, _filter):
                    self.calls += 1
                    return []

            qbt = CountingClient()
            rdt = CountingClient()
            engine.qbt = qbt
            engine.rdt = rdt
            engine.source_context_correlation.begin_cycle()
            try:
                engine._reconcile_complete()
            finally:
                engine.source_context_correlation.end_cycle()

            self.assertEqual(qbt.calls, 1)
            self.assertEqual(rdt.calls, 1)
        finally:
            database.close()
            temporary.cleanup()

    def test_unrelated_new_intents_do_not_restart_an_items_grace_window(self) -> None:
        temporary, config, database, engine = self._engine(grace=1)
        try:
            engine.source_context.handle(
                _payload(event_id="foreign-a", infohash="1" * 40)
            )
            item = config.complete_root / "movies" / "Descarga fisica independiente"
            item.mkdir(parents=True)

            class EmptyClient:
                def torrents(self, _filter):
                    return []

            engine.qbt = EmptyClient()
            engine.rdt = EmptyClient()
            with patch(
                "arr_orchestrator.source_context.correlation.time.monotonic",
                side_effect=(100.0, 102.0),
            ):
                engine._register_materialized("movies", item)
                engine.source_context.handle(
                    _payload(
                        event_id="foreign-b",
                        infohash="2" * 40,
                        title="Otro contexto ajeno",
                    )
                )
                engine._register_materialized("movies", item)

            filesystem_jobs = [
                job
                for job in database.latest_jobs()
                if job["origin"] == "fs" and job["source_path"] == str(item)
            ]
            self.assertEqual(len(filesystem_jobs), 1)
            self.assertEqual(filesystem_jobs[0]["name"], item.name)
        finally:
            database.close()
            temporary.cleanup()

    def test_old_pending_context_does_not_delay_an_unrelated_new_file(self) -> None:
        temporary, config, database, engine = self._engine(grace=1)
        try:
            _status, created = engine.source_context.handle(_payload())
            job = database.get_job(created["job_id"])
            meta = json.loads(job["source_meta_json"])
            meta["source_contexts"][0]["received_at"] = time.time() - 10
            database.update_job(
                job["job_id"],
                source_meta_json=json.dumps(meta, ensure_ascii=False),
            )
            item = config.complete_root / "movies" / "Archivo nuevo independiente"
            item.mkdir(parents=True)

            class EmptyClient:
                def torrents(self, _filter):
                    return []

            engine.qbt = EmptyClient()
            engine.rdt = EmptyClient()
            engine._register_materialized("movies", item)

            filesystem_jobs = [
                candidate
                for candidate in database.latest_jobs()
                if candidate["origin"] == "fs"
            ]
            self.assertEqual(len(filesystem_jobs), 1)
            self.assertEqual(filesystem_jobs[0]["source_path"], str(item))
        finally:
            database.close()
            temporary.cleanup()

    def test_transport_return_after_grace_merges_fs_and_context_jobs(self) -> None:
        temporary, config, database, engine = self._engine(grace=1)
        try:
            infohash = "7" * 40
            engine.source_context.handle(_payload(infohash=infohash, state="accepted"))
            item = config.complete_root / "movies" / "Material tardio"
            content = item / "video.mkv"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")

            class EmptyClient:
                def torrents(self, _filter):
                    return []

            engine.qbt = EmptyClient()
            engine.rdt = EmptyClient()
            with patch(
                "arr_orchestrator.source_context.correlation.time.monotonic",
                side_effect=(100.0, 102.0),
            ):
                engine._register_materialized("movies", item)
                engine._register_materialized("movies", item)

            self.assertEqual(
                len(
                    [
                        job
                        for job in database.latest_jobs()
                        if job["state"] not in TERMINAL_STATES
                    ]
                ),
                2,
            )

            class ReturningQbt:
                def torrents(self, _filter):
                    return [
                        {
                            "hash": infohash,
                            "category": "movies",
                            "name": item.name,
                            "content_path": str(content),
                            "added_on": 123,
                        }
                    ]

            engine.qbt = ReturningQbt()
            engine._register_materialized("movies", item)

            jobs = database.latest_jobs()
            active = [job for job in jobs if job["state"] not in TERMINAL_STATES]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["origin"], "fs")
            self.assertEqual(active[0]["infohash"], infohash)
            self.assertEqual(active[0]["qbt_hash"], infohash)
            self.assertTrue(json.loads(active[0]["source_meta_json"])["source_contexts"])
            bridge = next(job for job in jobs if job["origin"] == "bridge")
            self.assertEqual(bridge["state"], "duplicate")
            self.assertEqual(
                database.job_detail(bridge["job_id"])["timeline"][-1]["structured"][
                    "action"
                ],
                "merged_into_materialized",
            )
        finally:
            database.close()
            temporary.cleanup()

    def test_real_reconcile_order_merges_context_before_complete_scan(self) -> None:
        temporary, config, database, engine = self._engine(grace=1)
        try:
            infohash = "6" * 40
            engine.source_context.handle(_payload(infohash=infohash, state="accepted"))
            item = config.complete_root / "movies" / "Orden real de reconcile"
            content = item / "video.mkv"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")

            class EmptyClient:
                def torrents(self, _filter):
                    return []

            engine.qbt = EmptyClient()
            engine.rdt = EmptyClient()
            with patch(
                "arr_orchestrator.source_context.correlation.time.monotonic",
                side_effect=(100.0, 102.0),
            ):
                engine._register_materialized("movies", item)
                engine._register_materialized("movies", item)

            class ReturningQbt:
                def torrents(self, _filter):
                    return [
                        {
                            "hash": infohash,
                            "category": "movies",
                            "name": item.name,
                            "content_path": str(content),
                            "added_on": 123,
                        }
                    ]

            engine.qbt = ReturningQbt()
            engine.source_context_correlation.begin_cycle()
            try:
                engine._reconcile_qbt()
                engine._reconcile_complete()
            finally:
                engine.source_context_correlation.end_cycle()

            jobs = database.latest_jobs()
            active = [job for job in jobs if job["state"] not in TERMINAL_STATES]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["origin"], "fs")
            self.assertEqual(active[0]["infohash"], infohash)
            self.assertEqual(active[0]["qbt_hash"], infohash)
            self.assertTrue(json.loads(active[0]["source_meta_json"])["source_contexts"])
        finally:
            database.close()
            temporary.cleanup()

    def test_materialization_events_do_not_expose_absolute_paths(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            infohash = "5" * 40
            _status, response = engine.source_context.handle(
                _payload(infohash=infohash, state="accepted")
            )
            job = database.get_job(response["job_id"])
            item = config.complete_root / "movies" / "Ruta privada"
            content = item / "video.mkv"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")

            engine.source_context_correlation.attach_qbt(
                job,
                infohash,
                "movies",
                item,
                content,
                123,
                "Materializacion qB segura",
            )

            timeline = database.job_detail(job["job_id"])["timeline"]
            event = next(row for row in reversed(timeline) if row["phase"] == "qbt")
            serialized = json.dumps(event["structured"], ensure_ascii=False)
            self.assertNotIn(str(config.complete_root), serialized)
            self.assertNotIn(str(item), serialized)
            self.assertNotIn(str(content), serialized)
            self.assertTrue(event["structured"]["materialized"])
        finally:
            database.close()
            temporary.cleanup()

    def test_late_materialization_does_not_reopen_failed_terminal_context(self) -> None:
        temporary, config, database, engine = self._engine()
        try:
            infohash = "d" * 40
            engine.source_context.handle(_payload(event_id="failed", infohash=infohash))
            engine.source_context.handle(
                _payload(event_id="failed", infohash=infohash, state="failed")
            )
            item = config.complete_root / "movies" / "Materializacion tardia"
            item.mkdir(parents=True)
            torrent = {
                "hash": infohash,
                "category": "movies",
                "content_path": str(item),
                "added_on": 123,
            }

            class FakeQbt:
                def torrents(self, _filter):
                    return [torrent]

            class FakeRdt:
                def torrents(self, _filter):
                    return []

            engine.qbt = FakeQbt()
            engine.rdt = FakeRdt()
            engine._register_materialized("movies", item)

            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 2)
            discarded = next(job for job in jobs if job["state"] == "discarded")
            materialized = next(job for job in jobs if job["state"] != "discarded")
            self.assertEqual(discarded["name"], NEUTRAL_JOB_NAME)
            self.assertEqual(materialized["origin"], "fs")
            self.assertEqual(materialized["state"], "waiting_stable")
            self.assertEqual(materialized["name"], item.name)
            self.assertFalse(engine.source_context.store.has_context(materialized))
        finally:
            database.close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
