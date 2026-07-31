import json
import threading
import urllib.error
import urllib.request
from copy import deepcopy

import pytest

from series_worker.core import (
    JobConflict,
    RequestValidationError,
    SeriesWorkerBusy,
    Submission,
)
from series_worker.rules import RulesStore
from series_worker.server import create_server


class StubCoordinator:
    def __init__(self, tmp_path):
        self.store = RulesStore(config_path=tmp_path / "rules.json")
        self.active = None
        self.terminal = {}
        self.unhealthy = False

    def health(self):
        if self.unhealthy:
            return Submission(503, {"ok": False, "status": "unavailable"})
        return Submission(
            200,
            {
                "ok": True,
                "status": "ok",
                "service": "series-worker",
                "checks": {
                    "rules": {"ok": True},
                    "tools": {"ok": True},
                    "atomicity": {"ok": True, "verified": True},
                },
            },
        )

    def rules_payload(self):
        return self.store.payload()

    def save_rules(self, payload):
        return self.store.save(payload)

    def submit(self, payload):
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            raise RequestValidationError("job_id no es válido")
        if job_id == "busy":
            raise SeriesWorkerBusy("ocupado")
        if job_id == "conflict":
            raise JobConflict("conflicto")
        if job_id in self.terminal:
            return Submission(
                200,
                {
                    "ok": True,
                    "status": "terminal",
                    "job_id": job_id,
                    "kind": "series",
                    "result": self.terminal[job_id],
                },
            )
        if self.active == job_id:
            return Submission(
                202,
                {
                    "ok": True,
                    "status": "active",
                    "job_id": job_id,
                    "kind": "series",
                    "rules_fingerprint": self.store.snapshot().fingerprint,
                },
            )
        self.active = job_id
        return Submission(
            202,
            {
                "ok": True,
                "status": "accepted",
                "job_id": job_id,
                "kind": "series",
                "rules_fingerprint": self.store.snapshot().fingerprint,
            },
        )

    def finish(self, job_id, status="done"):
        self.terminal[job_id] = {
            "status": status,
            "job_id": job_id,
            "kind": "series",
            "published": ["Serie/Season 01/Serie.S01E01.mkv"],
        }
        if self.active == job_id:
            self.active = None

    def status(self, job_id):
        if self.active == job_id:
            return Submission(
                202,
                {"ok": True, "status": "active", "job_id": job_id, "kind": "series"},
            )
        if job_id in self.terminal:
            return Submission(
                200,
                {
                    "ok": True,
                    "status": "terminal",
                    "job_id": job_id,
                    "kind": "series",
                    "result": self.terminal[job_id],
                },
            )
        return Submission(
            404,
            {"ok": False, "status": "not_found", "error": "series_job_not_found"},
        )


@pytest.fixture
def api(tmp_path):
    coordinator = StubCoordinator(tmp_path)
    server = create_server(coordinator, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", coordinator
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(base, path, *, method="GET", payload=None, raw=None):
    data = raw
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base + path, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _payload(job_id="job-1"):
    return {
        "job_id": job_id,
        "job_root": "/data/downloads/torrents/complete/taller/" + job_id,
        "source_root": "/data/downloads/torrents/complete/taller/" + job_id + "/series_filebot_output",
        "final_root": "/data/media/tv",
        "review_root": "/data/media/repetidas_vs_error_series",
        "reports_root": "/config/series-worker",
        "callback_url": "",
    }


def test_health_and_rules_endpoints(api):
    base, coordinator = api
    status, health = _request(base, "/health")
    rules_status, current = _request(base, "/settings/rules")

    changed = deepcopy(current["rules"])
    changed["audio"]["bitrate_ac3"] = "448k"
    save_status, saved = _request(
        base,
        "/settings/rules",
        method="POST",
        payload={"rules": changed, "expected_fingerprint": current["fingerprint"]},
    )
    conflict_status, conflict = _request(
        base,
        "/settings/rules",
        method="POST",
        payload={"rules": current["rules"], "expected_fingerprint": current["fingerprint"]},
    )

    assert status == 200 and health["status"] == "ok"
    assert health["checks"]["atomicity"]["verified"] is True
    assert rules_status == 200 and current["applies_to"] == "new_jobs"
    assert save_status == 200 and saved["fingerprint"] != current["fingerprint"]
    assert conflict_status == 409 and conflict["error"] == "fingerprint_conflict"


def test_process_active_status_terminal_and_replay_http_contract(api):
    base, coordinator = api
    payload = _payload()

    accepted_status, accepted = _request(
        base, "/process-series", method="POST", payload=payload
    )
    active_status, active = _request(
        base, "/process-series", method="POST", payload=payload
    )
    poll_status, poll = _request(base, "/jobs/job-1/status?kind=series")
    coordinator.finish("job-1")
    terminal_status, terminal = _request(base, "/jobs/job-1/status?kind=series")
    replay_status, replay = _request(
        base, "/process-series", method="POST", payload=payload
    )

    assert accepted_status == 202 and accepted["status"] == "accepted"
    assert active_status == 202 and active["status"] == "active"
    assert poll_status == 202 and poll["status"] == "active"
    assert terminal_status == 200 and terminal["result"]["status"] == "done"
    assert replay_status == 200 and replay == terminal


def test_busy_conflict_validation_and_kind_statuses(api):
    base, _ = api
    busy_status, busy = _request(
        base, "/process-series", method="POST", payload=_payload("busy")
    )
    conflict_status, conflict = _request(
        base, "/process-series", method="POST", payload=_payload("conflict")
    )
    invalid_status, invalid = _request(
        base, "/process-series", method="POST", payload={}
    )
    kind_status, kind = _request(base, "/jobs/job-1/status?kind=movie")
    missing_status, missing = _request(base, "/jobs/missing/status?kind=series")

    assert busy_status == 409
    assert busy == {
        "ok": False,
        "error": "series_worker_busy",
        "message": "ocupado",
        "retryable": True,
    }
    assert conflict_status == 409 and conflict["error"] == "job_conflict"
    assert invalid_status == 400 and invalid["error"] == "invalid_request"
    assert kind_status == 400 and kind["error"] == "invalid_request"
    assert missing_status == 404 and missing["error"] == "series_job_not_found"


def test_invalid_json_unknown_route_and_unhealthy_are_bounded(api):
    base, coordinator = api
    invalid_status, invalid = _request(
        base, "/process-series", method="POST", raw=b"{broken"
    )
    unknown_status, unknown = _request(base, "/unknown")
    coordinator.unhealthy = True
    health_status, health = _request(base, "/health")

    assert invalid_status == 400 and invalid["error"] == "invalid_request"
    assert unknown_status == 404 and unknown["error"] == "not_found"
    assert health_status == 503 and health["status"] == "unavailable"
