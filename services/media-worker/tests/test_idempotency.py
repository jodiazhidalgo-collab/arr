import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from media_worker import server


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


@pytest.fixture
def service(tmp_path, monkeypatch):
    report_root = tmp_path / "reports"
    data_root = tmp_path / "data"
    workshop_root = data_root / "downloads" / "torrents" / "complete" / "taller"
    movies_root = data_root / "media" / "movies"
    tv_root = data_root / "media" / "tv"
    review_root = data_root / "media" / "review"
    (workshop_root / "input").mkdir(parents=True)
    movies_root.mkdir(parents=True)
    tv_root.mkdir(parents=True)
    review_root.mkdir(parents=True)
    monkeypatch.setenv(server.REPORT_ROOT_ENV, str(report_root))
    monkeypatch.setenv("MEDIA_WORKER_ALLOWED_ROOTS", str(workshop_root))
    monkeypatch.setenv(server.MOVIES_ROOT_ENV, str(movies_root))
    monkeypatch.setenv(server.REVIEW_ROOT_ENV, str(review_root))
    monkeypatch.setattr(server, "JOB_REGISTRY", server.MediaJobRegistry())
    http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    host, port = http_server.server_address
    try:
        yield f"http://{host}:{port}", report_root
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)


def _payload(report_root: Path, job_id: str):
    data_root = report_root.parent / "data"
    workshop_root = data_root / "downloads" / "torrents" / "complete" / "taller"
    return {
        "job_id": job_id,
        "source_path": str(workshop_root / "input"),
        "final_root": str(data_root / "media" / "movies"),
        "movies_root": str(data_root / "media" / "movies"),
        "review_root": str(data_root / "media" / "review"),
        "reports_root": str(report_root),
        "callback_url": "",
    }


def _bluray_payload(report_root: Path, job_id: str, source: Path = None):
    source_path = source or (
        report_root.parent
        / "data"
        / "downloads"
        / "torrents"
        / "complete"
        / "taller"
        / "input"
    )
    return {
        "job_id": job_id,
        "source_path": str(source_path),
        "reports_root": str(report_root),
        "callback_url": "",
    }


def test_concurrent_post_runs_movie_once_and_exposes_active_status(service, monkeypatch):
    base_url, report_root = service
    entered = threading.Event()
    release = threading.Event()
    calls = []
    first_response = []

    def process(payload):
        calls.append(payload["job_id"])
        entered.set()
        assert release.wait(timeout=3)
        return {"status": "done", "job_id": payload["job_id"], "value": "first"}

    monkeypatch.setattr(server, "process_movie", process)
    payload = _payload(report_root, "movie-concurrent")
    first = threading.Thread(
        target=lambda: first_response.append(
            _request(f"{base_url}/process-movie", method="POST", payload=payload)
        )
    )
    first.start()
    assert entered.wait(timeout=2)

    duplicate_status, duplicate = _request(
        f"{base_url}/process-movie", method="POST", payload=payload
    )
    active_status, active = _request(
        f"{base_url}/jobs/movie-concurrent/status?kind=movie"
    )

    assert duplicate_status == 409
    assert duplicate["error_code"] == "media_job_active"
    assert duplicate["retryable"] is True
    assert active_status == 200
    assert active["status"] == "active"
    assert calls == ["movie-concurrent"]

    release.set()
    first.join(timeout=3)
    assert first_response[0][0] == 200
    assert len(calls) == 1
    terminal_status, terminal = _request(
        f"{base_url}/jobs/movie-concurrent/status?kind=movie"
    )
    assert terminal_status == 200
    assert terminal["status"] == "terminal"
    assert terminal["result"] == first_response[0][1]


def test_concurrent_bluray_runs_once_and_exposes_active_and_terminal(
    service, monkeypatch
):
    base_url, report_root = service
    entered = threading.Event()
    release = threading.Event()
    calls = []
    first_response = []

    def normalize(payload):
        calls.append(payload)
        entered.set()
        assert release.wait(timeout=3)
        return {"status": "normalized", "normalized": True}

    monkeypatch.setattr(server, "normalize_bluray", normalize)
    payload = _bluray_payload(report_root, "bluray-concurrent")
    first = threading.Thread(
        target=lambda: first_response.append(
            _request(f"{base_url}/normalize-bluray", method="POST", payload=payload)
        )
    )
    first.start()
    assert entered.wait(timeout=2)

    duplicate_status, duplicate = _request(
        f"{base_url}/normalize-bluray", method="POST", payload=payload
    )
    active_status, active = _request(
        f"{base_url}/jobs/bluray-concurrent/status?kind=bluray"
    )

    assert duplicate_status == 409
    assert duplicate["error_code"] == "media_job_active"
    assert duplicate["kind"] == "bluray"
    assert active_status == 200
    assert active["status"] == "active"
    assert len(calls) == 1
    assert "final_root" not in calls[0]
    assert "review_root" not in calls[0]

    release.set()
    first.join(timeout=3)
    assert first_response[0][0] == 200
    assert first_response[0][1]["status"] == "normalized"
    terminal_status, terminal = _request(
        f"{base_url}/jobs/bluray-concurrent/status?kind=bluray"
    )
    assert terminal_status == 200
    assert terminal["status"] == "terminal"
    assert terminal["result"] == first_response[0][1]
    assert json.loads(
        (report_root / "bluray-concurrent" / "bluray_result.json").read_text(
            encoding="utf-8"
        )
    ) == first_response[0][1]


def test_bluray_accepts_nonempty_status_and_replays_durable_result(
    service, monkeypatch
):
    base_url, report_root = service
    calls = []

    def normalize(payload):
        calls.append(payload["job_id"])
        return {"status": "ambiguous", "reason": "controlled"}

    monkeypatch.setattr(server, "normalize_bluray", normalize)
    payload = _bluray_payload(report_root, "bluray-replay")

    first_status, first = _request(
        f"{base_url}/normalize-bluray", method="POST", payload=payload
    )
    monkeypatch.setattr(server, "JOB_REGISTRY", server.MediaJobRegistry())
    replay_status, replay = _request(
        f"{base_url}/normalize-bluray", method="POST", payload=payload
    )

    assert first_status == replay_status == 200
    assert first == replay
    assert first["status"] == "ambiguous"
    assert first["kind"] == "bluray"
    assert calls == ["bluray-replay"]


def test_bluray_error_is_durable_and_replayed_without_second_execution(
    service, monkeypatch
):
    base_url, report_root = service
    calls = []

    def fail(payload):
        calls.append(payload["job_id"])
        raise RuntimeError(f"fallo en {report_root}/privado token=secreto")

    monkeypatch.setattr(server, "normalize_bluray", fail)
    payload = _bluray_payload(report_root, "bluray-error")

    first_status, first = _request(
        f"{base_url}/normalize-bluray", method="POST", payload=payload
    )
    monkeypatch.setattr(server, "JOB_REGISTRY", server.MediaJobRegistry())
    replay_status, replay = _request(
        f"{base_url}/normalize-bluray", method="POST", payload=payload
    )

    assert first_status == replay_status == 500
    assert first == replay
    assert first["status"] == "error"
    assert first["error_code"] == "media_worker_failed"
    assert first["kind"] == "bluray"
    assert "secreto" not in first["error"]
    assert calls == ["bluray-error"]
    assert json.loads(
        (report_root / "bluray-error" / "bluray_result.json").read_text(
            encoding="utf-8"
        )
    ) == first


@pytest.mark.parametrize(
    "kind,endpoint,status,filename",
    [
        ("movie", "/process-movie", "review", "media_result.json"),
        ("trailer", "/process-trailer", "done", "trailer_result.json"),
    ],
)
def test_terminal_done_and_review_are_durable_and_replayed(
    service, monkeypatch, kind, endpoint, status, filename
):
    base_url, report_root = service
    calls = []

    def process(payload):
        calls.append(payload["job_id"])
        return {"status": status, "marker": f"{kind}-{status}"}

    monkeypatch.setattr(
        server, "process_movie" if kind == "movie" else "process_trailer", process
    )
    job_id = f"{kind}-{status}"
    payload = _payload(report_root, job_id)

    first_status, first = _request(
        f"{base_url}{endpoint}", method="POST", payload=payload
    )
    replay_status, replay = _request(
        f"{base_url}{endpoint}", method="POST", payload=payload
    )

    assert first_status == replay_status == 200
    assert first == replay
    assert first["job_id"] == job_id
    assert first["kind"] == kind
    assert calls == [job_id]
    persisted = json.loads((report_root / job_id / filename).read_text(encoding="utf-8"))
    assert persisted == first


def test_error_is_safe_durable_and_replayed_without_execution(service, monkeypatch):
    base_url, report_root = service
    calls = []

    def fail(payload):
        calls.append(payload["job_id"])
        raise RuntimeError(
            f"fallo en {report_root}/privado token=secreto "
            "Authorization: Bearer cabecera-secreta "
            "magnet:?xt=urn:btih:secreto "
            "download_url=https://privado.local/file?token=secreto "
            "/home/usuario/privado.mkv C:\\privado\\video.mkv "
            + "x" * 800
        )

    monkeypatch.setattr(server, "process_trailer", fail)
    payload = _payload(report_root, "trailer-error")

    first_status, first = _request(
        f"{base_url}/process-trailer", method="POST", payload=payload
    )
    replay_status, replay = _request(
        f"{base_url}/process-trailer", method="POST", payload=payload
    )

    assert first_status == 500
    assert replay_status == 500
    assert first == replay
    assert first["status"] == "error"
    assert first["error_code"] == "media_worker_failed"
    assert first["retryable"] is False
    assert str(report_root) not in first["error"]
    assert "secreto" not in first["error"]
    assert "cabecera-secreta" not in first["error"]
    assert "magnet:?" not in first["error"]
    assert "privado.local" not in first["error"]
    assert "/home/usuario" not in first["error"]
    assert "C:\\privado" not in first["error"]
    assert "<REPORT_ROOT>" in first["error"]
    assert len(first["error"]) <= server.MAX_ERROR_LENGTH
    assert calls == ["trailer-error"]
    persisted = json.loads(
        (report_root / "trailer-error" / "trailer_result.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted == first


def test_get_not_found_and_invalid_kind_are_typed(service):
    base_url, _report_root = service

    missing_status, missing = _request(
        f"{base_url}/jobs/missing/status?kind=movie"
    )
    invalid_status, invalid = _request(
        f"{base_url}/jobs/missing/status?kind=other"
    )
    missing_bluray_status, missing_bluray = _request(
        f"{base_url}/jobs/missing/status?kind=bluray"
    )

    assert missing_status == 404
    assert missing["status"] == "not_found"
    assert missing["error_code"] == "media_job_not_found"
    assert invalid_status == 400
    assert invalid["error_code"] == "media_invalid_request"
    assert missing_bluray_status == 404
    assert missing_bluray["kind"] == "bluray"
    assert missing_bluray["error_code"] == "media_job_not_found"


def test_post_rejects_noncanonical_report_root(service, monkeypatch):
    base_url, report_root = service
    calls = []
    monkeypatch.setattr(
        server,
        "process_movie",
        lambda payload: calls.append(payload) or {"status": "done"},
    )
    payload = _payload(report_root, "wrong-root")
    payload["reports_root"] = str(report_root.parent / "other")

    status, result = _request(
        f"{base_url}/process-movie", method="POST", payload=payload
    )

    assert status == 400
    assert result["error_code"] == "media_invalid_request"
    assert calls == []


@pytest.mark.parametrize(
    "field,value_factory",
    [
        ("source_path", lambda report_root: report_root.parent / "outside" / "source"),
        ("final_root", lambda report_root: report_root.parent / "data" / "other-movies"),
        ("review_root", lambda report_root: report_root.parent / "data" / "other-review"),
        ("callback_url", lambda _report_root: "http://attacker.invalid/jobs/path/events"),
    ],
)
def test_post_rejects_paths_and_callback_outside_contract(
    service, monkeypatch, field, value_factory
):
    base_url, report_root = service
    calls = []
    monkeypatch.setattr(
        server,
        "process_movie",
        lambda payload: calls.append(payload) or {"status": "done"},
    )
    payload = _payload(report_root, f"invalid-{field}")
    payload[field] = str(value_factory(report_root))

    status, result = _request(
        f"{base_url}/process-movie", method="POST", payload=payload
    )

    assert status == 400
    assert result["error_code"] == "media_invalid_request"
    assert calls == []


@pytest.mark.parametrize(
    "endpoint,kind",
    [
        ("/process-movie", "movie"),
        ("/process-trailer", "trailer"),
        ("/normalize-bluray", "bluray"),
    ],
)
@pytest.mark.parametrize("forbidden_area", ["movies", "tv", "review"])
def test_post_rejects_library_and_review_as_source(
    service, monkeypatch, endpoint, kind, forbidden_area
):
    base_url, report_root = service
    data_root = report_root.parent / "data"
    forbidden = {
        "movies": data_root / "media" / "movies" / "Pelicula real",
        "tv": data_root / "media" / "tv" / "Serie real",
        "review": data_root / "media" / "review" / "Revision real",
    }[forbidden_area]
    forbidden.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(
        server,
        {
            "movie": "process_movie",
            "trailer": "process_trailer",
            "bluray": "normalize_bluray",
        }[kind],
        lambda payload: calls.append(payload) or {"status": "done"},
    )
    payload = (
        _bluray_payload(report_root, f"reject-{kind}-{forbidden_area}", forbidden)
        if kind == "bluray"
        else _payload(report_root, f"reject-{kind}-{forbidden_area}")
    )
    payload["source_path"] = str(forbidden)

    status, result = _request(
        f"{base_url}{endpoint}", method="POST", payload=payload
    )

    assert status == 400
    assert result["error_code"] == "media_invalid_request"
    assert calls == []


def test_post_accepts_exact_orchestrator_callback(service, monkeypatch):
    base_url, report_root = service
    calls = []
    monkeypatch.setattr(
        server,
        "process_movie",
        lambda payload: calls.append(payload) or {"status": "review"},
    )
    payload = _payload(report_root, "valid-callback")
    payload["callback_url"] = (
        "http://arr-orchestrator:8787/jobs/valid-callback/events"
    )

    status, _result = _request(
        f"{base_url}/process-movie", method="POST", payload=payload
    )

    assert status == 200
    assert calls[0]["callback_url"] == payload["callback_url"]


def test_post_rejects_invalid_job_id_before_execution(service, monkeypatch):
    base_url, report_root = service
    calls = []
    monkeypatch.setattr(
        server,
        "process_movie",
        lambda payload: calls.append(payload) or {"status": "done"},
    )
    payload = _payload(report_root, "../escape")

    status, result = _request(
        f"{base_url}/process-movie", method="POST", payload=payload
    )

    assert status == 400
    assert result["error_code"] == "media_invalid_request"
    assert calls == []


def test_registry_key_separates_movie_trailer_and_bluray(tmp_path, monkeypatch):
    monkeypatch.setenv(server.REPORT_ROOT_ENV, str(tmp_path / "reports"))
    registry = server.MediaJobRegistry()

    movie_state, _ = registry.claim("movie", "shared-job")
    trailer_state, _ = registry.claim("trailer", "shared-job")
    bluray_state, _ = registry.claim("bluray", "shared-job")
    duplicate_movie_state, _ = registry.claim("movie", "shared-job")

    assert movie_state == "claimed"
    assert trailer_state == "claimed"
    assert bluray_state == "claimed"
    assert duplicate_movie_state == "active"
    registry.release("movie", "shared-job")
    registry.release("trailer", "shared-job")
    registry.release("bluray", "shared-job")


def test_health_and_normalize_bluray_keep_their_previous_contract(
    service, monkeypatch
):
    base_url, report_root = service
    allowed_root = (
        report_root.parent
        / "data"
        / "downloads"
        / "torrents"
        / "complete"
        / "taller"
    )
    source = allowed_root / "bluray"
    source.mkdir(parents=True)
    monkeypatch.setenv("MEDIA_WORKER_ALLOWED_ROOTS", str(allowed_root))
    calls = []
    monkeypatch.setattr(
        server,
        "normalize_bluray",
        lambda payload: calls.append(payload) or {"status": "normalized"},
    )

    health_status, health = _request(f"{base_url}/health")
    normalize_status, normalized = _request(
        f"{base_url}/normalize-bluray",
        method="POST",
        payload=_bluray_payload(report_root, "bluray-job", source),
    )

    assert health_status == 200
    assert health == {"status": "ok"}
    assert normalize_status == 200
    assert normalized == {
        "status": "normalized",
        "job_id": "bluray-job",
        "kind": "bluray",
    }
    assert Path(calls[0]["source_path"]) == source.resolve()


def test_terminal_writer_uses_atomic_replace(tmp_path, monkeypatch):
    report_root = tmp_path / "reports"
    monkeypatch.setenv(server.REPORT_ROOT_ENV, str(report_root))
    original_replace = server.os.replace
    replacements = []

    def observed_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        original_replace(source, destination)

    monkeypatch.setattr(server.os, "replace", observed_replace)
    payload = {
        "status": "done",
        "job_id": "atomic-job",
        "kind": "movie",
    }

    path = server._write_terminal_atomic("movie", "atomic-job", payload)

    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert temporary != destination
    assert destination == path
    assert not temporary.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert list(path.parent.glob("*.tmp")) == []
