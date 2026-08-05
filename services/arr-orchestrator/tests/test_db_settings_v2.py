from pathlib import Path

from arr_orchestrator.db import Database


def test_settings_batch_read_and_targeted_delete(tmp_path: Path) -> None:
    database = Database(tmp_path / "orchestrator.db")
    database.initialize()
    database.set_setting("identity.pipeline.common", "v1-common")
    database.set_setting("identity.pipeline.movies", "v1-movies")
    database.set_setting("unrelated", "keep")

    assert database.get_settings(
        ("identity.pipeline.common", "identity.pipeline.movies", "missing")
    ) == {
        "identity.pipeline.common": "v1-common",
        "identity.pipeline.movies": "v1-movies",
        "missing": None,
    }

    assert database.delete_settings(
        ("identity.pipeline.common", "identity.pipeline.movies")
    ) == 2
    assert database.get_setting("identity.pipeline.common") is None
    assert database.get_setting("identity.pipeline.movies") is None
    assert database.get_setting("unrelated") == "keep"


def test_settings_batch_helpers_accept_empty_input(tmp_path: Path) -> None:
    database = Database(tmp_path / "orchestrator.db")
    database.initialize()

    assert database.get_settings(()) == {}
    assert database.delete_settings(()) == 0
