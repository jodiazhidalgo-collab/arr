import json
from copy import deepcopy
from pathlib import Path

import pytest

from series_worker.rules import (
    DEFAULT_RULES_PATH,
    RULE_BLOCKS,
    RulesConflictError,
    RulesStore,
    RulesValidationError,
    rules_fingerprint,
)


def _store(tmp_path: Path) -> RulesStore:
    return RulesStore(config_path=tmp_path / "reglas_series.json")


def test_defaults_have_exactly_five_blocks_and_no_trailers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = store.payload()

    assert tuple(payload["rules"]) == RULE_BLOCKS
    assert "trailers" not in payload["rules"]
    assert payload["fingerprint"] == rules_fingerprint(payload["rules"])
    assert payload["applies_to"] == "new_jobs"
    assert payload["rules_path"] == "<CONFIG>/series-rules/reglas_series.json"
    assert payload["defaults_path"] == "<APP>/series-worker/default_rules.json"
    assert payload["rules"]["video"]["seleccionar_mejor_si_hay_varias"] is False


def test_save_uses_cas_persists_and_keeps_old_snapshot_frozen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = store.snapshot()
    changed = deepcopy(old.rules)
    changed["audio"]["bitrate_ac3"] = "448k"

    saved = store.save({"rules": changed, "expected_fingerprint": old.fingerprint})

    assert saved["saved"] is True
    assert saved["fingerprint"] != old.fingerprint
    assert old.rules["audio"]["bitrate_ac3"] == "640k"
    restarted = RulesStore(config_path=tmp_path / "reglas_series.json")
    assert restarted.snapshot().fingerprint == saved["fingerprint"]
    assert restarted.snapshot().rules["audio"]["bitrate_ac3"] == "448k"


def test_stale_fingerprint_is_rejected_without_writing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    before = store.payload()

    with pytest.raises(RulesConflictError) as captured:
        store.save({"rules": before["rules"], "expected_fingerprint": "stale"})

    assert captured.value.current["fingerprint"] == before["fingerprint"]
    assert not (tmp_path / "reglas_series.json").exists()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda rules: rules.update({"trailers": {}}),
        lambda rules: rules["video"].update({"campo_inventado": True}),
        lambda rules: rules["entrada"].update({"extensiones_video": ["mkv"]}),
        lambda rules: rules["entrada"].update({"extensiones_video": [".mkv", ".flv"]}),
        lambda rules: rules["limpieza"].update({"capitulo_cada_segundos": -1}),
        lambda rules: rules["video"].update({"pistas_exactas": 0}),
        lambda rules: rules["video"].update({"pistas_exactas": 2}),
        lambda rules: rules["limpieza"].update({"capitulo_cada_segundos": 0}),
        lambda rules: rules["subtitulos"]["delay_audio"].update({"frases_maximo": 0}),
        lambda rules: rules["subtitulos"].update(
            {"frases_descartar_hasta": rules["subtitulos"]["frases_maximo_unico_forzado"]}
        ),
        lambda rules: rules["subtitulos"]["delay_audio"].update(
            {"frases_maximo": rules["subtitulos"]["frases_maximo_unico_forzado"] + 1}
        ),
    ],
)
def test_unknown_or_semantically_invalid_rules_are_rejected(
    tmp_path: Path, mutator
) -> None:
    store = _store(tmp_path)
    current = store.payload()
    changed = deepcopy(current["rules"])
    mutator(changed)

    with pytest.raises(RulesValidationError):
        store.save(
            {"rules": changed, "expected_fingerprint": current["fingerprint"]}
        )


def test_invalid_persisted_json_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "reglas_series.json"
    config.write_text('{"trailers": {}}', encoding="utf-8")

    with pytest.raises(RulesValidationError):
        RulesStore(config_path=config, default_path=DEFAULT_RULES_PATH)


def test_video_extensions_are_closed_to_implemented_formats(tmp_path: Path) -> None:
    assert _store(tmp_path).snapshot().rules["entrada"]["extensiones_video"] == [
        ".mkv",
        ".mp4",
        ".m4v",
        ".avi",
        ".mov",
        ".wmv",
        ".ts",
        ".m2ts",
        ".mts",
        ".webm",
    ]


def test_first_start_seeds_five_blocks_from_movies_once(tmp_path: Path) -> None:
    defaults = json.loads(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))
    movie_rules = deepcopy(defaults)
    movie_rules["audio"]["bitrate_ac3"] = "448k"
    movie_rules["trailers"] = {"activo": True}
    movie_rules["version"] = 99
    seed = tmp_path / "reglas_motor.json"
    seed.write_text(json.dumps(movie_rules), encoding="utf-8")
    config = tmp_path / "series" / "reglas_series.json"

    first = RulesStore(config_path=config, seed_path=seed)

    assert first.payload()["seeded_from_movies"] is True
    assert first.snapshot().rules["audio"]["bitrate_ac3"] == "448k"
    persisted = json.loads(config.read_text(encoding="utf-8"))
    assert tuple(persisted) == RULE_BLOCKS
    assert "trailers" not in persisted
    assert "version" not in persisted

    movie_rules["audio"]["bitrate_ac3"] = "384k"
    seed.write_text(json.dumps(movie_rules), encoding="utf-8")
    restarted = RulesStore(config_path=config, seed_path=seed)

    assert restarted.payload()["seeded_from_movies"] is False
    assert restarted.snapshot().rules["audio"]["bitrate_ac3"] == "448k"


def test_seed_accepts_movie_rules_envelope(tmp_path: Path) -> None:
    movie_rules = json.loads(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))
    seed = tmp_path / "reglas_motor.json"
    seed.write_text(json.dumps({"rules": movie_rules, "revision": 8}), encoding="utf-8")

    store = RulesStore(
        config_path=tmp_path / "series" / "reglas_series.json",
        seed_path=seed,
    )

    assert store.payload()["seeded_from_movies"] is True
    assert store.snapshot().rules == movie_rules


def test_second_save_creates_verified_backup_of_previous_document(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.payload()
    rules = deepcopy(first["rules"])
    rules["audio"]["bitrate_ac3"] = "448k"
    store.save({"rules": rules, "expected_fingerprint": first["fingerprint"]})
    previous_document = json.loads((tmp_path / "reglas_series.json").read_text("utf-8"))

    current = store.payload()
    rules = deepcopy(current["rules"])
    rules["audio"]["bitrate_ac3"] = "384k"
    result = store.save({"rules": rules, "expected_fingerprint": current["fingerprint"]})

    assert result["backup"]
    backup = tmp_path / "backups" / result["backup"]
    assert json.loads(backup.read_text("utf-8")) == previous_document
