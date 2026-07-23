import copy
import json
import threading
import unittest
import urllib.error
import urllib.request

from arr_orchestrator.filebot_settings import (
    DEFAULT_FILEBOT_RULES,
    FILEBOT_RULES_SETTING_KEY,
    FileBotRulesStore,
    resolver_fingerprint,
)
from arr_orchestrator.health import start_health_server


class FakeSettingsDatabase:
    def __init__(self, stored=None):
        self.values = {}
        if stored is not None:
            self.values[FILEBOT_RULES_SETTING_KEY] = stored
        self.get_calls = 0
        self.set_calls = 0

    def get_setting(self, key):
        self.get_calls += 1
        return self.values.get(key)

    def set_setting(self, key, value):
        self.set_calls += 1
        self.values[key] = value


class BlockingSettingsDatabase(FakeSettingsDatabase):
    def __init__(self):
        super().__init__()
        self.write_started = threading.Event()
        self.allow_write = threading.Event()

    def set_setting(self, key, value):
        self.write_started.set()
        if not self.allow_write.wait(timeout=5):
            raise TimeoutError("La prueba no libero la escritura")
        super().set_setting(key, value)


def changed_rules():
    rules = copy.deepcopy(DEFAULT_FILEBOT_RULES)
    rules["movies"].update(
        {
            "language": "EN-us",
            "region": "us",
            "query_aliases": ["The Visitors|Los visitantes"],
            "forced_matches": ["The Visitors | 1993 | 11687"],
            "filename_style": "title_year_quality",
        }
    )
    rules["tv"].update(
        {
            "query_aliases": ["The Office | La oficina", "the office|la oficina"],
            "forced_matches": ["Shogun | 126308"],
            "filename_style": "series_sxxexx_title",
            "episode_order": "DVD",
        }
    )
    return rules


class FileBotRulesStoreTests(unittest.TestCase):
    def test_defaults_are_safe_and_read_only_in_memory(self):
        database = FakeSettingsDatabase()
        store = FileBotRulesStore(database)

        first = store.payload()
        first["rules"]["movies"]["language"] = "xx-XX"
        first["safety"]["action"] = "delete"
        second = store.payload()

        self.assertEqual(second["revision"], 0)
        self.assertIsNone(second["saved_at"])
        self.assertEqual(second["rules"], DEFAULT_FILEBOT_RULES)
        self.assertEqual(second["safety"]["action"], "move")
        self.assertTrue(second["safety"]["read_only"])
        self.assertEqual(second["rules_path"], "settings/filebot.rules")
        self.assertTrue(second["resolver_fingerprint"].startswith("sha256:"))
        self.assertEqual(database.get_calls, 1)
        self.assertEqual(database.set_calls, 0)

    def test_runtime_defaults_follow_existing_resolver_configuration(self):
        store = FileBotRulesStore(
            FakeSettingsDatabase(), default_language="en-gb", default_region="gb"
        )

        rules = store.snapshot()
        self.assertEqual(rules["movies"]["language"], "en-GB")
        self.assertEqual(rules["tv"]["language"], "en-GB")
        self.assertEqual(rules["movies"]["region"], "GB")
        self.assertNotIn("region", rules["tv"])

    def test_save_normalizes_persists_and_reloads(self):
        database = FakeSettingsDatabase()
        store = FileBotRulesStore(database)
        before = store.payload()["resolver_fingerprint"]

        result = store.update({"rules": changed_rules(), "expected_revision": 0})

        self.assertTrue(result["ok"])
        self.assertTrue(result["saved"])
        self.assertEqual(result["revision"], 1)
        self.assertIsNotNone(result["saved_at"])
        self.assertEqual(result["rules"]["movies"]["language"], "en-US")
        self.assertEqual(result["rules"]["movies"]["region"], "US")
        self.assertEqual(
            result["rules"]["movies"]["query_aliases"],
            ["The Visitors | Los visitantes"],
        )
        self.assertEqual(
            result["rules"]["tv"]["query_aliases"], ["The Office | La oficina"]
        )
        self.assertNotEqual(result["resolver_fingerprint"], before)
        self.assertEqual(database.set_calls, 1)

        persisted = json.loads(database.values[FILEBOT_RULES_SETTING_KEY])
        self.assertEqual(persisted["revision"], 1)
        restarted = FileBotRulesStore(database)
        self.assertEqual(restarted.snapshot(), result["rules"])
        self.assertEqual(restarted.payload()["revision"], 1)

    def test_noop_does_not_write_or_increment_revision(self):
        database = FakeSettingsDatabase()
        store = FileBotRulesStore(database)

        result = store.update(
            {"rules": copy.deepcopy(DEFAULT_FILEBOT_RULES), "expected_revision": 0}
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["saved"])
        self.assertEqual(result["revision"], 0)
        self.assertEqual(database.set_calls, 0)

    def test_revision_conflict_returns_current_snapshot(self):
        database = FakeSettingsDatabase()
        store = FileBotRulesStore(database)
        first = store.update({"rules": changed_rules(), "expected_revision": 0})

        conflict = store.update(
            {"rules": copy.deepcopy(DEFAULT_FILEBOT_RULES), "expected_revision": 0}
        )

        self.assertTrue(first["ok"])
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"], "revision_conflict")
        self.assertEqual(conflict["current_revision"], 1)
        self.assertEqual(conflict["rules"], first["rules"])
        self.assertEqual(database.set_calls, 1)

    def test_concurrent_writers_with_same_revision_cannot_overwrite(self):
        database = FakeSettingsDatabase()
        store = FileBotRulesStore(database)
        barrier = threading.Barrier(3)
        results = []

        def save(style):
            rules = changed_rules()
            rules["movies"]["filename_style"] = style
            barrier.wait()
            results.append(store.update({"rules": rules, "expected_revision": 0}))

        threads = [
            threading.Thread(target=save, args=("title_year",)),
            threading.Thread(target=save, args=("title_year_quality",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(bool(result["ok"]) for result in results), 1)
        self.assertEqual(
            sum(result.get("error") == "revision_conflict" for result in results), 1
        )
        self.assertEqual(database.set_calls, 1)

    def test_job_snapshot_cannot_mix_revision_and_rules_during_save(self):
        database = BlockingSettingsDatabase()
        store = FileBotRulesStore(database)
        rules = changed_rules()
        update_result = []
        snapshot_result = []
        reader_started = threading.Event()
        reader_finished = threading.Event()

        writer = threading.Thread(
            target=lambda: update_result.append(
                store.update({"rules": rules, "expected_revision": 0})
            )
        )

        def read_job_snapshot():
            reader_started.set()
            snapshot_result.append(store.job_snapshot())
            reader_finished.set()

        writer.start()
        self.assertTrue(database.write_started.wait(timeout=2))
        reader = threading.Thread(target=read_job_snapshot)
        reader.start()
        self.assertTrue(reader_started.wait(timeout=2))
        self.assertFalse(
            reader_finished.wait(timeout=0.1),
            "job_snapshot no espero al guardado que poseia el lock",
        )

        database.allow_write.set()
        writer.join(timeout=2)
        reader.join(timeout=2)

        self.assertFalse(writer.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertTrue(update_result[0]["ok"])
        snapshot = snapshot_result[0]
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["rules"], update_result[0]["rules"])
        self.assertEqual(
            snapshot["resolver_fingerprint"], resolver_fingerprint(snapshot["rules"])
        )
        self.assertEqual(snapshot["saved_at"], update_result[0]["saved_at"])

    def test_rejects_unknown_dangerous_and_malformed_fields(self):
        database = FakeSettingsDatabase()
        store = FileBotRulesStore(database)
        invalid_payloads = []

        protected = changed_rules()
        protected["movies"]["action"] = "delete"
        invalid_payloads.append({"rules": protected, "expected_revision": 0})
        bad_alias = changed_rules()
        bad_alias["movies"]["query_aliases"] = ["sin separador"]
        invalid_payloads.append({"rules": bad_alias, "expected_revision": 0})
        conflicting_alias = changed_rules()
        conflicting_alias["movies"]["query_aliases"] = [
            "The Visitors | Los visitantes",
            "the visitors | Otro titulo",
        ]
        invalid_payloads.append({"rules": conflicting_alias, "expected_revision": 0})
        bad_match = changed_rules()
        bad_match["tv"]["forced_matches"] = ["Serie | año | no-id"]
        invalid_payloads.append({"rules": bad_match, "expected_revision": 0})
        movie_without_year = changed_rules()
        movie_without_year["movies"]["forced_matches"] = ["The Visitors | 11687"]
        invalid_payloads.append({"rules": movie_without_year, "expected_revision": 0})
        conflicting_match = changed_rules()
        conflicting_match["movies"]["forced_matches"] = [
            "The Visitors | 1993 | 11687",
            "the visitors | 1993 | 99999",
        ]
        invalid_payloads.append({"rules": conflicting_match, "expected_revision": 0})
        bad_style = changed_rules()
        bad_style["tv"]["filename_style"] = "groovy:{exec}"
        invalid_payloads.append({"rules": bad_style, "expected_revision": 0})
        invalid_payloads.append({"rules": changed_rules()})

        for payload in invalid_payloads:
            result = store.update(payload)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "invalid_rules")
        self.assertEqual(database.set_calls, 0)

    def test_tv_forced_match_accepts_an_empty_optional_year(self):
        database = FakeSettingsDatabase()
        store = FileBotRulesStore(database)
        rules = changed_rules()
        rules["tv"]["forced_matches"] = ["Shogun | | 126308"]

        result = store.update({"rules": rules, "expected_revision": 0})

        self.assertTrue(result["ok"])
        self.assertEqual(result["rules"]["tv"]["forced_matches"], ["Shogun | 126308"])

    def test_invalid_persisted_value_falls_back_without_rewriting_database(self):
        database = FakeSettingsDatabase('{"rules":{"schema_version":99}}')
        store = FileBotRulesStore(database)

        self.assertEqual(store.snapshot(), DEFAULT_FILEBOT_RULES)
        self.assertEqual(store.payload()["revision"], 0)
        self.assertEqual(database.set_calls, 0)


class FileBotSettingsHealthTests(unittest.TestCase):
    def test_health_endpoint_get_save_and_conflict(self):
        store = FileBotRulesStore(FakeSettingsDatabase())
        server = start_health_server(
            0,
            lambda: {"status": "ok"},
            lambda: [],
            filebot_rules_provider=store.payload,
            filebot_rules_updater=store.update,
        )
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/settings/filebot", timeout=5
            ) as response:
                initial = json.loads(response.read().decode("utf-8"))
            self.assertEqual(initial["revision"], 0)

            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/settings/filebot",
                data=json.dumps(
                    {"rules": changed_rules(), "expected_revision": 0}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                saved = json.loads(response.read().decode("utf-8"))
            self.assertEqual(saved["revision"], 1)

            try:
                urllib.request.urlopen(request, timeout=5)
                self.fail("El segundo guardado debio devolver conflicto")
            except urllib.error.HTTPError as error:
                self.assertEqual(error.code, 409)
                conflict = json.loads(error.read().decode("utf-8"))
            self.assertEqual(conflict["error"], "revision_conflict")
            self.assertEqual(conflict["current_revision"], 1)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
