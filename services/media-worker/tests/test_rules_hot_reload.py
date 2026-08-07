import json
import threading
import urllib.error
import urllib.request
from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from media_worker import core, server
from media_worker.legacy import detector, planificador, procesador, reglas, trailer_runner, verificador


def _request(url, *, method="GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _defaults():
    return json.loads(reglas.DEFAULT_PATH.read_text(encoding="utf-8"))


def _store(tmp_path: Path):
    active = tmp_path / "media-rules" / "reglas_motor.json"
    active.parent.mkdir(parents=True)
    active.write_text(
        json.dumps(_defaults(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return reglas.MediaRulesStore(active, reglas.DEFAULT_PATH), active


@pytest.fixture
def rules_service(tmp_path, monkeypatch):
    store, active = _store(tmp_path)
    report_root = tmp_path / "reports"
    workshop_root = tmp_path / "data" / "downloads" / "torrents" / "complete" / "taller"
    movies_root = tmp_path / "data" / "media" / "movies"
    review_root = tmp_path / "data" / "media" / "review"
    (workshop_root / "input").mkdir(parents=True)
    movies_root.mkdir(parents=True)
    review_root.mkdir(parents=True)
    monkeypatch.setattr(server, "MEDIA_RULES_STORE", store)
    monkeypatch.setattr(server, "JOB_REGISTRY", server.MediaJobRegistry())
    monkeypatch.setenv(server.REPORT_ROOT_ENV, str(report_root))
    monkeypatch.setenv("MEDIA_WORKER_ALLOWED_ROOTS", str(workshop_root))
    monkeypatch.setenv(server.MOVIES_ROOT_ENV, str(movies_root))
    monkeypatch.setenv(server.REVIEW_ROOT_ENV, str(review_root))
    http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    host, port = http_server.server_address
    try:
        yield f"http://{host}:{port}", store, active, report_root
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)


def _movie_payload(report_root: Path, job_id: str):
    data_root = report_root.parent / "data"
    return {
        "job_id": job_id,
        "source_path": str(data_root / "downloads" / "torrents" / "complete" / "taller" / "input"),
        "final_root": str(data_root / "media" / "movies"),
        "review_root": str(data_root / "media" / "review"),
        "reports_root": str(report_root),
        "callback_url": "",
    }


def test_store_persists_backs_up_activates_and_recovers_after_restart(tmp_path):
    store, active = _store(tmp_path)
    initial = store.payload()
    updated = deepcopy(initial["rules"])
    updated["audio"]["aceptar_indeterminado_si_video_es"] = False

    result = store.save(
        {"rules": updated, "expected_fingerprint": initial["fingerprint"]}
    )

    assert result["ok"] is True
    assert result["saved"] is True
    assert result["applied"] is True
    assert result["applies_to"] == "new_jobs"
    assert result["fingerprint"] != initial["fingerprint"]
    assert json.loads(active.read_text(encoding="utf-8")) == updated
    assert list((active.parent / "backups").glob("reglas_motor_*.json"))
    assert list(active.parent.glob("*.tmp")) == []

    restarted = reglas.MediaRulesStore(active, reglas.DEFAULT_PATH)
    assert restarted.payload()["rules"] == updated
    assert restarted.payload()["fingerprint"] == result["fingerprint"]


def test_invalid_or_stale_save_never_changes_disk_or_memory(tmp_path):
    store, active = _store(tmp_path)
    initial = store.payload()
    original_bytes = active.read_bytes()
    invalid = deepcopy(initial["rules"])
    invalid["video"]["pistas_exactas"] = "una"

    with pytest.raises(reglas.RulesValidationError):
        store.save(
            {"rules": invalid, "expected_fingerprint": initial["fingerprint"]}
        )
    with pytest.raises(reglas.RulesConflictError):
        store.save(
            {"rules": initial["rules"], "expected_fingerprint": "stale"}
        )

    assert active.read_bytes() == original_bytes
    assert store.payload()["fingerprint"] == initial["fingerprint"]


def test_video_selection_toggle_persists_and_track_count_stays_fixed(tmp_path):
    store, _active = _store(tmp_path)
    initial = store.payload()
    assert initial["rules"]["video"]["seleccionar_mejor_si_hay_varias"] is False

    updated = deepcopy(initial["rules"])
    updated["video"]["seleccionar_mejor_si_hay_varias"] = True
    saved = store.save(
        {"rules": updated, "expected_fingerprint": initial["fingerprint"]}
    )
    assert saved["rules"]["video"]["seleccionar_mejor_si_hay_varias"] is True

    invalid = deepcopy(saved["rules"])
    invalid["video"]["pistas_exactas"] = 2
    with pytest.raises(reglas.RulesValidationError, match="debe ser 1"):
        store.save(
            {"rules": invalid, "expected_fingerprint": saved["fingerprint"]}
        )


def test_settings_endpoint_preserves_success_validation_and_conflict(rules_service):
    base_url, _store_value, _active, _report_root = rules_service
    get_status, current = _request(f"{base_url}/settings/rules")
    updated = deepcopy(current["rules"])
    updated["video"]["idioma_final"] = "es-test"

    save_status, saved = _request(
        f"{base_url}/settings/rules",
        method="POST",
        payload={
            "rules": updated,
            "expected_fingerprint": current["fingerprint"],
        },
    )
    conflict_status, conflict = _request(
        f"{base_url}/settings/rules",
        method="POST",
        payload={
            "rules": current["rules"],
            "expected_fingerprint": current["fingerprint"],
        },
    )
    invalid = deepcopy(saved["rules"])
    invalid["audio"]["canales_convertir_ac3_desde"] = "seis"
    invalid_status, invalid_result = _request(
        f"{base_url}/settings/rules",
        method="POST",
        payload={
            "rules": invalid,
            "expected_fingerprint": saved["fingerprint"],
        },
    )

    assert get_status == save_status == 200
    assert saved["rules"]["video"]["idioma_final"] == "es-test"
    assert saved["applied"] is True
    assert conflict_status == 409
    assert conflict["error"] == "fingerprint_conflict"
    assert conflict["fingerprint"] == saved["fingerprint"]
    assert invalid_status == 400
    assert invalid_result["error"] == "invalid_rules"


def test_running_job_keeps_snapshot_and_next_job_gets_saved_rules(
    rules_service, monkeypatch
):
    base_url, _store_value, _active, report_root = rules_service
    entered = threading.Event()
    release = threading.Event()
    observed = []
    first_response = []

    def process(payload):
        first = reglas.valor("video.idioma_final")
        fingerprint = reglas.huella_reglas()
        entered.set()
        assert release.wait(timeout=3)
        observed.append((payload["job_id"], first, reglas.valor("video.idioma_final"), fingerprint, reglas.huella_reglas()))
        return {"status": "done"}

    monkeypatch.setattr(server, "process_movie", process)
    current = _request(f"{base_url}/settings/rules")[1]
    first = threading.Thread(
        target=lambda: first_response.append(
            _request(
                f"{base_url}/process-movie",
                method="POST",
                payload=_movie_payload(report_root, "old-rules-job"),
            )
        )
    )
    first.start()
    assert entered.wait(timeout=2)

    updated = deepcopy(current["rules"])
    updated["video"]["idioma_final"] = "nuevo"
    save_status, saved = _request(
        f"{base_url}/settings/rules",
        method="POST",
        payload={
            "rules": updated,
            "expected_fingerprint": current["fingerprint"],
        },
    )
    release.set()
    first.join(timeout=3)
    entered.clear()
    release.set()
    second_status, second = _request(
        f"{base_url}/process-movie",
        method="POST",
        payload=_movie_payload(report_root, "new-rules-job"),
    )

    assert save_status == second_status == 200
    assert first_response[0][0] == 200
    assert observed[0][1:3] == ("es", "es")
    assert observed[0][3] == observed[0][4] == current["fingerprint"]
    assert observed[1][1:3] == ("nuevo", "nuevo")
    assert observed[1][3] == observed[1][4] == saved["fingerprint"]
    assert first_response[0][1]["rules_fingerprint"] == current["fingerprint"]
    assert second["rules_fingerprint"] == saved["fingerprint"]


def test_all_media_components_use_the_bound_snapshot(tmp_path, monkeypatch):
    store, _active = _store(tmp_path)
    old = store.snapshot()
    updated = deepcopy(old.rules)
    updated["entrada"]["extensiones_video"] = [".new"]
    updated["video"]["idioma_final"] = "nuevo"
    updated["trailers"]["nombre_final"] = "avance"
    store.save({"rules": updated, "expected_fingerprint": old.fingerprint})
    monkeypatch.setattr(reglas, "RULES_STORE", store)
    folder = tmp_path / "videos"
    folder.mkdir()
    (folder / "old.old").write_bytes(b"")
    (folder / "new.new").write_bytes(b"")
    old_rules = deepcopy(old.rules)
    old_rules["entrada"]["extensiones_video"] = [".old"]
    old_rules["trailers"]["nombre_final"] = "trailer-viejo"
    old_snapshot = reglas.RulesSnapshot(old_rules, reglas._fingerprint(old_rules))

    with reglas.usar_reglas(old_snapshot):
        decision = detector.decision_idioma_video({"tags": {"language": "es"}})
        assert decision[1] == "es"
        assert [path.name for path in core._video_files(folder)] == ["old.old"]
        assert planificador.REGLAS.get("trailers", {})["nombre_final"] == "trailer-viejo"
        assert procesador.REGLAS.get("trailers", {})["nombre_final"] == "trailer-viejo"
        assert verificador.REGLAS.get("trailers", {})["nombre_final"] == "trailer-viejo"
        assert trailer_runner.valor("trailers.nombre_final") == "trailer-viejo"
        assert reglas.huella_reglas() == old_snapshot.fingerprint

    assert reglas.valor("video.idioma_final") == "nuevo"
    assert [path.name for path in core._video_files(folder)] == ["new.new"]
