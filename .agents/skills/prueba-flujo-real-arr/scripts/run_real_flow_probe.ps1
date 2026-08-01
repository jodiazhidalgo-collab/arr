param(
  [string]$HostName = "lacabra@192.168.1.159",
  [string]$RemoteDir = "/volume1/docker/arr",
  [string]$ContainerName = "arr-orchestrator",
  [string]$WorkerContainerName = "arr-media-worker",
  [string]$SeriesWorkerContainerName = "arr-series-worker",
  [int]$TimeoutSeconds = 240,
  [switch]$KeepProbeFiles
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
Set-Location $Root

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$artifactDir = Join-Path $Root "_codex_runtime\artifacts\real-flow\$stamp"
New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
$transcriptPath = Join-Path $artifactDir "transcript.txt"

$remote = @'
set -eu
cd '__REMOTE_DIR__'
PROBE_ID="codex_live_flow_probe_$(date +%Y%m%d_%H%M%S)_$$"
MEDIA_SMOKE_JOB_ID="${PROBE_ID}_media_worker"
SERIES_SOURCE_NAME="$PROBE_ID"
SERIES_EXPECTED_KEY="treme"
echo "ARR_REAL_FLOW_CONTEXT remote_dir=__REMOTE_DIR__ container=__CONTAINER__ worker=__WORKER_CONTAINER__ series_worker=__SERIES_WORKER_CONTAINER__ timeout=__TIMEOUT__"
sudo docker compose ps __CONTAINER__ media-worker series-worker

set +e
unit_output="$(sudo docker exec \
  -e RUN_ENGINE_LIVE_TESTS=1 \
  -e RUN_FILEBOT_LIVE_TESTS=1 \
  __CONTAINER__ sh -lc '
set -eu
cd /opt/arr-orchestrator
python3 - <<'"'"'PY'"'"'
import os
from pathlib import Path
missing = []
if os.environ.get("RUN_ENGINE_LIVE_TESTS") != "1":
    missing.append("RUN_ENGINE_LIVE_TESTS")
if os.environ.get("RUN_FILEBOT_LIVE_TESTS") != "1":
    missing.append("RUN_FILEBOT_LIVE_TESTS")
if os.environ.get("ARR_SERIES_MODE") not in {"canary", "active"}:
    missing.append("ARR_SERIES_MODE=canary|active")
if not os.environ.get("TMDB_API_TOKEN"):
    missing.append("TMDB_API_TOKEN")
if not Path("/opt/filebot/filebot").exists():
    missing.append("/opt/filebot/filebot")
if missing:
    raise SystemExit("LIVE_ENV_MISSING " + ",".join(missing))
print("LIVE_ENV_OK")
PY
python3 -m pytest --version
python3 -m pytest -q -rA -p no:cacheprovider \
  tests/test_live_resolver.py \
  tests/test_live_filebot.py \
  tests/test_live_engine.py \
  tests/test_media_worker_client.py \
  tests/test_core.py::CoreTests::test_real_torrent_fixture_when_available
')"
unit_exit=$?
set -e
printf '%s\n' "$unit_output"
if printf '%s\n' "$unit_output" | grep -Eiq '\bskipped\b|SKIPPED \['; then
  echo "ARR_REAL_FLOW_UNIT_HAS_SKIPS"
  exit 81
fi
if [ "$unit_exit" -ne 0 ]; then
  echo "ARR_REAL_FLOW_UNIT_FAILED exit=$unit_exit"
  exit "$unit_exit"
fi

sudo docker exec \
  -e ARR_SERIES_EXPECTED_KEY="$SERIES_EXPECTED_KEY" \
  __WORKER_CONTAINER__ python3 - <<'PY'
import os
import re
import unicodedata
from pathlib import Path


def engine_series_key(value):
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


if os.environ.get("ARR_SERIES_EXPECTED_KEY") != "treme":
    raise SystemExit("ARR_SERIES_PROBE_KEY_INVALID")
library = Path("/data/media/tv")
if library.is_symlink() or not library.is_dir():
    raise SystemExit("ARR_SERIES_PROBE_LIBRARY_INVALID")
expected = engine_series_key("Trem\u00e9")
matches = [entry.name for entry in library.iterdir() if engine_series_key(entry.name) == expected]
if matches:
    raise SystemExit("ARR_SERIES_PROBE_PREFLIGHT_SERIES_EXISTS")
print("ARR_SERIES_PROBE_PREFLIGHT_SERIES_ABSENT")
PY

sudo docker exec \
  -e ARR_SERIES_PROBE_ID="$PROBE_ID" \
  -e ARR_SERIES_SOURCE_NAME="$SERIES_SOURCE_NAME" \
  -e ARR_SERIES_EXPECTED_KEY="$SERIES_EXPECTED_KEY" \
  __WORKER_CONTAINER__ sh -lc '
set -eu
case "$ARR_SERIES_PROBE_ID" in
  codex_live_flow_probe_*) ;;
  *) echo "ARR_SERIES_PROBE_ID_INVALID" >&2; exit 82 ;;
esac
[ "$ARR_SERIES_EXPECTED_KEY" = "treme" ] || exit 82
build_root="/data/downloads/torrents/complete/taller/${ARR_SERIES_PROBE_ID}_normal_entry_build"
probe_root="/data/downloads/torrents/complete/tv/${ARR_SERIES_SOURCE_NAME}"
review_root="/data/media/repetidas_vs_error_series/${ARR_SERIES_SOURCE_NAME}"
for path in "$build_root" "$probe_root" "$review_root"; do
  if [ -e "$path" ] || [ -L "$path" ]; then
    echo "ARR_SERIES_PROBE_PREFLIGHT_PATH_EXISTS $path" >&2
    exit 83
  fi
done
cleanup_build() {
  if [ -d "$build_root" ] && [ ! -L "$build_root" ]; then
    rm -rf -- "$build_root"
  fi
}
trap cleanup_build EXIT
mkdir -p "$build_root"
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "color=c=black:s=320x180:r=5:d=305" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=305" \
  -map 0:v:0 -map 1:a:0 -shortest \
  -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
  -c:a ac3 -metadata:s:a:0 language=spa \
  "$build_root/Treme.tmdb-17967.S01E03.mkv"
printf "%s\n" \
  "1" \
  "00:00:00,200 --> 00:00:01,200" \
  "Subtitulo externo del probe ARR" \
  > "$build_root/Treme.tmdb-17967.S01E03.es.srt"
mv -- "$build_root" "$probe_root"
trap - EXIT
echo "ARR_SERIES_NORMAL_ENTRY_CREATED $probe_root"
'

set +e
probe_output="$(sudo docker exec -i \
  -e ARR_REAL_FLOW_PROBE_ID="$PROBE_ID" \
  -e ARR_MEDIA_SMOKE_JOB_ID="$MEDIA_SMOKE_JOB_ID" \
  -e ARR_SERIES_SOURCE_NAME="$SERIES_SOURCE_NAME" \
  -e ARR_SERIES_EXPECTED_KEY="$SERIES_EXPECTED_KEY" \
  __CONTAINER__ python3 - <<'PY'
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import zipfile
from pathlib import Path

TIMEOUT = int("__TIMEOUT__")
KEEP_PROBE_FILES = "__KEEP_PROBE__" == "1"
TERMINAL = {"done", "manual_review", "duplicate", "error_terminal", "discarded"}
WORKER_KINDS = ("movie", "trailer", "bluray", "series")
WORKER_TERMINAL_FILES = {
    "movie": "media_result.json",
    "trailer": "trailer_result.json",
    "bluray": "bluray_result.json",
    "series": "series_result.json",
}
PROBE_ID = os.environ["ARR_REAL_FLOW_PROBE_ID"]
MEDIA_SMOKE_JOB_ID = os.environ["ARR_MEDIA_SMOKE_JOB_ID"]
SERIES_SOURCE_NAME = os.environ["ARR_SERIES_SOURCE_NAME"]
SERIES_EXPECTED_KEY = os.environ["ARR_SERIES_EXPECTED_KEY"]
SERIES_JOB_ID = ""
DB_PATH = Path("/config/orchestrator.db")
DIAG_ROOT = Path("/diagnostics/arr")
ZIP_ROOT = Path("/diagnosticos_codex")
COMPLETE_ROOT = Path("/data/downloads/torrents/complete")
MOVIES_PROBE_ROOT = COMPLETE_ROOT / "movies"
TV_PROBE_ROOT = COMPLETE_ROOT / "tv"
WORKSHOP_ROOT = COMPLETE_ROOT / "taller"
REVIEW_ROOT = Path("/data/media/repetidas_vs_error")
SERIES_REVIEW_ROOT = Path("/data/media/repetidas_vs_error_series")
MOVIES_FINAL_ROOT = Path("/data/media/movies")
TV_FINAL_ROOT = Path("/data/media/tv")
WORKER_REPORT_ROOT = Path("/config/media-worker")
SERIES_REPORT_ROOT = Path("/config/series-worker")
WORKER_BASE_URL = "http://media-worker:8790"
SERIES_WORKER_BASE_URL = "http://series-worker:8791"
SERIES_PROBE_SOURCE = TV_PROBE_ROOT / SERIES_SOURCE_NAME
SERIES_FINAL_ROOT = None
SERIES_CLEANUP_CONTRACT = None
SERIES_GENERATION_RE = re.compile(
    r"(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})"
)
ARTIFACT_GRACE_SECONDS = 0.25
ARTIFACT_RETRY_SECONDS = 6.0
ARTIFACT_RETRY_INTERVAL = 0.4
ARTIFACT_ONLY_PREFIXES = (
    "missing_live_trace",
    "missing_codex_zip",
    "missing_config_snapshot",
    "missing_summary_read_order",
    "zip_missing:",
    "zip_missing_traza_viva",
)
ALLOWED_CLEANUP_ROOTS = (
    COMPLETE_ROOT,
    REVIEW_ROOT,
    SERIES_REVIEW_ROOT,
    MOVIES_FINAL_ROOT,
    TV_FINAL_ROOT,
)
REPORT_ALIASES = (
    ("/data/downloads", "<DATA_DOWNLOADS>"),
    ("/data/media", "<DATA_MEDIA>"),
    ("/diagnosticos_codex", "<CODEX_DIAGS>"),
    ("/diagnostics", "<DIAGNOSTICS>"),
    ("/config", "<CONFIG>"),
)


def connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_jobs():
    like = f"%{PROBE_ID}%"
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE name LIKE ? OR source_path LIKE ? OR stage_path LIKE ?
            ORDER BY created_at
            """,
            (like, like, like),
        ).fetchall()
        result = []
        for row in rows:
            payload = dict(row)
            events = conn.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY event_id ASC",
                (payload["job_id"],),
            ).fetchall()
            payload["events"] = [dict(event) for event in events]
            result.append(payload)
        return result


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_json_value(value):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except Exception:
        return None


def report_safe(value):
    if isinstance(value, dict):
        return {str(key): report_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [report_safe(item) for item in value]
    if isinstance(value, tuple):
        return [report_safe(item) for item in value]
    if isinstance(value, str):
        safe = value
        for raw, alias in REPORT_ALIASES:
            safe = safe.replace(raw, alias)
        return safe
    return value


def request_json(url, *, method="GET", payload=None, timeout=20):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
    parsed = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(parsed, dict):
        raise RuntimeError("La respuesta HTTP no es un objeto JSON")
    return status, parsed


def worker_status(job_id, kind):
    base_url = SERIES_WORKER_BASE_URL if kind == "series" else WORKER_BASE_URL
    endpoint = (
        f"{base_url}/jobs/{urllib.parse.quote(str(job_id), safe='')}/status"
        f"?kind={urllib.parse.quote(kind, safe='')}"
    )
    try:
        http_status, payload = request_json(endpoint, timeout=10)
    except Exception as error:
        return {
            "status": "unavailable",
            "error_type": type(error).__name__,
        }
    state = str(payload.get("status") or "")
    if (
        (http_status == 200 and state == "terminal")
        or (http_status in {200, 202} and state in {"active", "recoverable"})
    ):
        if state == "terminal":
            result = payload.get("result")
            if not isinstance(result, dict):
                return {"status": "unavailable", "error_type": "invalid_terminal"}
            if str(result.get("job_id") or "") != str(job_id):
                return {"status": "unavailable", "error_type": "foreign_job"}
            if str(result.get("kind") or "") != kind:
                return {"status": "unavailable", "error_type": "foreign_kind"}
        return {"status": state, "http_status": http_status, "payload": payload}
    if http_status == 404 and state == "not_found":
        return {"status": "not_found", "http_status": http_status}
    return {
        "status": "unavailable",
        "http_status": http_status,
        "error_code": str(payload.get("error_code") or "unexpected_status"),
    }


def post_diagnostic(job_id):
    try:
        _status, result = request_json(
            f"http://127.0.0.1:8787/jobs/{job_id}/diagnostic",
            method="POST",
            payload={"force": True},
            timeout=20,
        )
        return result
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


def trace_dir(job_id):
    matches = sorted((DIAG_ROOT / "jobs").glob(f"*/*{job_id}"))
    return matches[-1] if matches else None


def zip_path(job_id):
    short_id = str(job_id)[:8]
    matches = sorted(
        ZIP_ROOT.rglob(f"*_{short_id}_informe_codex.zip"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    return matches[0] if matches else None


def zip_names(path):
    if not path or not path.exists() or not zipfile.is_zipfile(path):
        return []
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def has_raw_path(text):
    raw_markers = (
        "/data/downloads",
        "/data/media",
        "/volume1/docker/arr",
        "Z:\\arr",
        "\\\\192.168.1.159\\docker\\arr",
    )
    return [marker for marker in raw_markers if marker in text]


def inspect_job(job):
    job_id = job["job_id"]
    events = job.get("events") or []
    phases = sorted({event.get("phase") for event in events if event.get("phase")})
    event_types = sorted({event.get("event_type") for event in events if event.get("event_type")})
    structured_events = []
    for event in events:
        structured = parse_json_value(event.get("structured_json"))
        structured_events.append(structured if isinstance(structured, dict) else {})
    state_sequence = [
        str(structured.get("state") or "")
        for structured in structured_events
        if structured.get("state")
    ]
    command_phases = sorted(
        {
            str(event.get("phase") or "")
            for event in events
            if event.get("event_type") == "command" and event.get("phase")
        }
    )
    source_meta = parse_json_value(job.get("source_meta_json"))
    source_meta = source_meta if isinstance(source_meta, dict) else {}
    series_pipeline = source_meta.get("series_pipeline")
    series_pipeline = series_pipeline if isinstance(series_pipeline, dict) else {}
    received = next(
        (
            structured
            for event, structured in zip(events, structured_events)
            if event.get("phase") == "received"
        ),
        {},
    )
    diag = post_diagnostic(job_id)
    tdir = trace_dir(job_id)
    zpath = zip_path(job_id)
    names = zip_names(zpath)
    noise = []
    meta = {}
    summary = {}
    related = {}
    events_text = ""
    if not tdir:
        noise.append("missing_live_trace")
    else:
        meta = load_json(tdir / "meta.json") or {}
        summary = load_json(tdir / "summary.json") or {}
        related = load_json(tdir / "related_files.json") or {}
        events_file = tdir / "events.jsonl"
        events_text = events_file.read_text(encoding="utf-8", errors="replace") if events_file.exists() else ""
        if not meta.get("config_snapshot"):
            noise.append("missing_config_snapshot")
        if not summary.get("read_order"):
            noise.append("missing_summary_read_order")
        related_files = related.get("files") if isinstance(related.get("files"), list) else []
        if len(related_files) > 20:
            noise.append(f"related_files_too_long:{len(related_files)}")
        raw = has_raw_path(events_text)
        if raw:
            noise.append("raw_paths_in_live_trace:" + ",".join(raw))
    if not zpath:
        noise.append("missing_codex_zip")
    else:
        required = {"LEEME_PRIMERO.txt", "timeline.json", "decisiones.json", "errores.txt", "detalle_completo.json"}
        missing = sorted(required - set(names))
        if missing:
            noise.append("zip_missing:" + ",".join(missing))
        if not any(name.startswith("traza_viva/") for name in names):
            noise.append("zip_missing_traza_viva")
    if len(events) < 4:
        noise.append(f"too_few_job_events:{len(events)}")
    return {
        "job_id": job_id,
        "category": job.get("category"),
        "name": job.get("name"),
        "origin": job.get("origin"),
        "state": job.get("state"),
        "last_error_code": job.get("last_error_code"),
        "events": len(events),
        "phases": phases,
        "event_types": event_types,
        "state_sequence": state_sequence,
        "command_phases": command_phases,
        "received_source_path": str(received.get("source_path") or ""),
        "series_pipeline": series_pipeline,
        "trace_dir": str(tdir) if tdir else "",
        "zip": str(zpath) if zpath else "",
        "diagnostic": diag,
        "config_snapshot_schema": (meta.get("config_snapshot") or {}).get("schema") if isinstance(meta.get("config_snapshot"), dict) else "",
        "related_files_count": len(related.get("files") or []) if isinstance(related, dict) else 0,
        "noise": noise,
    }


def artifact_noise_only(noise):
    return bool(noise) and all(
        any(str(item).startswith(prefix) for prefix in ARTIFACT_ONLY_PREFIXES)
        for item in noise
    )


def inspect_jobs_with_retry(jobs):
    attempts = 0
    retry_deadline = time.time() + ARTIFACT_RETRY_SECONDS
    while True:
        attempts += 1
        inspections = [inspect_job(job) for job in jobs]
        noise = [item for job in inspections for item in job["noise"]]
        if not artifact_noise_only(noise) or time.time() >= retry_deadline:
            return inspections, attempts
        time.sleep(ARTIFACT_RETRY_INTERVAL)


def category_key(value):
    text = str(value or "").strip().lower()
    if text.startswith("movie") or text.startswith("pelicula"):
        return "movies"
    if text.startswith("tv") or text.startswith("serie"):
        return "tv"
    if text.startswith("trailer"):
        return "trailers"
    return text


def series_name_key(value):
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", str(value or "")).casefold()
        if character.isalnum() and not unicodedata.combining(character)
    )


def validate_series_cleanup_contract(result, job_id):
    if not isinstance(result, dict) or result.get("status") != "done":
        return None
    if result.get("job_id") != job_id or result.get("kind") != "series":
        return None
    series_root = str(result.get("series_root") or "")
    delivery = result.get("delivery")
    manifest = result.get("manifest")
    published_manifest = result.get("published_manifest")
    if (
        Path(series_root).name != series_root
        or series_name_key(series_root) != SERIES_EXPECTED_KEY
        or not isinstance(delivery, dict)
        or delivery.get("mode") != "new"
        or delivery.get("cleanup_pending") is not False
        or not SERIES_GENERATION_RE.fullmatch(str(delivery.get("generation") or ""))
        or not isinstance(manifest, dict)
        or manifest.get("schema") != "series-manifest-v1"
        or manifest.get("status") != "ready"
        or series_name_key(manifest.get("series_name")) != SERIES_EXPECTED_KEY
        or manifest.get("review_reasons") != []
    ):
        return None
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != 1:
        return None
    entry = entries[0]
    sidecars = entry.get("subtitle_sidecars") if isinstance(entry, dict) else None
    if (
        not isinstance(entry, dict)
        or series_name_key(entry.get("series_name")) != SERIES_EXPECTED_KEY
        or entry.get("season") != 1
        or entry.get("episodes") != [3]
        or not str(entry.get("target_relpath") or "").endswith(" - S01E03.mkv")
        or not isinstance(sidecars, list)
        or len(sidecars) != 1
        or not str(sidecars[0].get("source_relpath") or "").lower().endswith(".srt")
    ):
        return None
    if (
        not isinstance(published_manifest, dict)
        or published_manifest.get("schema") != "series-published-manifest-v1"
        or not isinstance(published_manifest.get("entries"), list)
        or len(published_manifest["entries"]) != 1
    ):
        return None
    published_entries = published_manifest["entries"]
    expected_prefix = f"{series_root}/Season 01/"
    suffixes = []
    for item in published_entries:
        if not isinstance(item, dict) or set(item) != {"path", "size", "content_sha256"}:
            return None
        path = str(item.get("path") or "")
        digest = str(item.get("content_sha256") or "")
        if (
            not path.startswith(expected_prefix)
            or "S01E03" not in Path(path).name
            or not isinstance(item.get("size"), int)
            or isinstance(item.get("size"), bool)
            or item["size"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            return None
        suffixes.append(Path(path).suffix.lower())
    if suffixes != [".mkv"]:
        return None
    encoded = json.dumps(
        published_entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if published_manifest.get("digest") != hashlib.sha256(encoded).hexdigest():
        return None
    if result.get("published") != [entry.get("target_relpath")] or result.get("satisfied") != []:
        return None
    return {
        "job_id": job_id,
        "series_root": series_root,
        "generation": str(delivery["generation"]),
        "published_manifest": published_manifest,
    }


def verify_owned_series_root(path, job_id):
    contract = SERIES_CLEANUP_CONTRACT
    if not isinstance(contract, dict) or contract.get("job_id") != job_id:
        return False, "missing_series_cleanup_contract"
    marker_path = path / ".series-worker-generation.json"
    if marker_path.exists() or marker_path.is_symlink():
        return False, "series_marker_leaked"
    journal_path = SERIES_REPORT_ROOT / job_id / "journal.json"
    try:
        journal_info = journal_path.lstat()
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "series_journal_unreadable"
    if not stat.S_ISREG(journal_info.st_mode) or journal_path.is_symlink():
        return False, "series_journal_unsafe"
    if not isinstance(journal, dict) or not isinstance(journal.get("details"), dict):
        return False, "series_journal_mismatch"
    details = journal["details"]
    published_manifest = contract["published_manifest"]
    try:
        final_series_root = Path(str(details.get("final_series_root") or ""))
        final_matches = (
            final_series_root.is_absolute()
            and final_series_root.resolve(strict=False) == path.resolve(strict=False)
        )
    except (OSError, RuntimeError):
        final_matches = False
    if (
        journal.get("state") != "COMMITTED"
        or details.get("job_id") != job_id
        or details.get("generation") != contract.get("generation")
        or details.get("cleanup_complete") is not True
        or details.get("marker_retired") is not True
        or details.get("published_manifest_digest") != published_manifest.get("digest")
        or details.get("published_manifest_entries") != len(published_manifest.get("entries", []))
        or not final_matches
    ):
        return False, "series_journal_mismatch"
    actual_entries = []
    actual_directories = []
    try:
        for current, directories, filenames in os.walk(path, followlinks=False):
            current_path = Path(current)
            for name in directories:
                directory = current_path / name
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
                    return False, "series_tree_has_unsafe_directory"
                actual_directories.append(directory.relative_to(TV_FINAL_ROOT).as_posix())
            for name in filenames:
                file_path = current_path / name
                info = file_path.lstat()
                if not stat.S_ISREG(info.st_mode) or file_path.is_symlink():
                    return False, "series_tree_has_unsafe_file"
                digest = hashlib.sha256()
                with file_path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                actual_entries.append(
                    {
                        "path": file_path.relative_to(TV_FINAL_ROOT).as_posix(),
                        "size": info.st_size,
                        "content_sha256": digest.hexdigest(),
                    }
                )
    except OSError:
        return False, "series_tree_scan_failed"
    expected_entries = contract["published_manifest"]["entries"]
    expected_directories = sorted({str(Path(item["path"]).parent).replace("\\", "/") for item in expected_entries})
    actual_entries.sort(key=lambda item: (unicodedata.normalize("NFKC", item["path"]).casefold(), item["path"]))
    if actual_entries != expected_entries:
        return False, "series_tree_manifest_mismatch"
    if sorted(set(actual_directories)) != expected_directories:
        return False, "series_tree_directory_mismatch"
    return True, "owned_new_generation_verified"


def expected_probe_outcome(inspection):
    category = category_key(inspection.get("category"))
    state = str(inspection.get("state") or "")
    error_code = str(inspection.get("last_error_code") or "")
    phases = set(inspection.get("phases") or [])
    event_types = set(inspection.get("event_types") or [])
    if category == "movies":
        expected_state = "manual_review"
        expected_error = "identity_suspicious"
        required_phases = {
            "received",
            "stable_wait",
            "staging",
            "extract",
            "settings",
            "identity",
        }
        required_event_types = {"started", "finished", "decision", "error"}
    elif category == "tv":
        expected_state = "done"
        expected_error = ""
        required_phases = {
            "received",
            "stable_wait",
            "staging",
            "extract",
            "settings",
            "identity",
            "filebot",
            "verify",
            "series",
            "cleanup",
        }
        required_event_types = {
            "started",
            "finished",
            "decision",
            "command",
        }
    else:
        return [f"unexpected_category:{category or 'empty'}"]
    errors = []
    if state != expected_state:
        errors.append(f"unexpected_state:{category}:{state}:expected:{expected_state}")
    if error_code != expected_error:
        errors.append(
            f"unexpected_error:{category}:{error_code}:expected:{expected_error}"
        )
    missing_phases = sorted(required_phases - phases)
    if missing_phases:
        errors.append(f"missing_phases:{category}:" + ",".join(missing_phases))
    missing_types = sorted(required_event_types - event_types)
    if missing_types:
        errors.append(f"missing_event_types:{category}:" + ",".join(missing_types))
    if category == "tv":
        if inspection.get("origin") != "fs":
            errors.append(f"series_not_discovered_by_fs_watcher:{inspection.get('origin')}")
        expected_source = str(SERIES_PROBE_SOURCE)
        if inspection.get("received_source_path") != expected_source:
            errors.append("series_not_created_from_complete_tv")
        pipeline = inspection.get("series_pipeline")
        pipeline = pipeline if isinstance(pipeline, dict) else {}
        if pipeline.get("route") != "series-worker":
            errors.append(f"series_route_not_worker:{pipeline.get('route')}")
        if pipeline.get("configured_mode") not in {"canary", "active"}:
            errors.append(
                f"series_snapshot_mode_invalid:{pipeline.get('configured_mode')}"
            )
        if pipeline.get("canary_eligible") is not True:
            errors.append("series_canary_snapshot_not_eligible")
        required_states = [
            "waiting_stable",
            "ready_stage",
            "staging",
            "ready_extract",
            "extracting",
            "ready_filebot",
            "filebot_running",
            "series_postprocess_ready",
            "series_postprocess_running",
            "ready_cleanup",
            "done",
        ]
        state_sequence = inspection.get("state_sequence") or []
        positions = []
        for required_state in required_states:
            try:
                positions.append(state_sequence.index(required_state))
            except ValueError:
                errors.append(f"series_missing_state:{required_state}")
        if len(positions) == len(required_states) and positions != sorted(positions):
            errors.append("series_state_order_invalid")
        command_phases = set(inspection.get("command_phases") or [])
        missing_commands = sorted({"filebot", "series"} - command_phases)
        if missing_commands:
            errors.append("series_missing_command_phases:" + ",".join(missing_commands))
    return errors


def job_is_probe(job):
    return PROBE_ID in "\n".join(
        str(job.get(field) or "") for field in ("name", "source_path", "stage_path")
    )


def possible_worker_kinds(job):
    category = category_key(job.get("category"))
    kinds = set()
    if category == "movies":
        kinds.update(("movie", "bluray"))
    elif category == "tv":
        kinds.add("series")
    elif category == "trailers":
        kinds.add("trailer")
    event_text = json.dumps(job.get("events") or [], ensure_ascii=False).lower()
    if "media worker" in event_text or "media_worker" in event_text:
        if "trailer" in event_text:
            kinds.add("trailer")
        elif category == "movies":
            kinds.add("movie")
    if "bluray" in event_text or "blu-ray" in event_text:
        kinds.add("bluray")
    if "series worker" in event_text or "series_worker" in event_text:
        kinds.add("series")
    return sorted(kinds)


def assess_cleanup(job):
    job_id = str(job.get("job_id") or "")
    assessment = {
        "job_id": job_id,
        "state": str(job.get("state") or ""),
        "safe": False,
        "worker_status": {},
        "terminal_worker_kinds": [],
        "reason": "",
    }
    if assessment["state"] not in TERMINAL:
        assessment["reason"] = "job_not_terminal"
        return assessment
    if not job_is_probe(job):
        assessment["reason"] = "job_not_identified_as_probe"
        return assessment
    unsafe = []
    for kind in possible_worker_kinds(job):
        status = worker_status(job_id, kind)
        state = status.get("status")
        assessment["worker_status"][kind] = {
            key: value for key, value in status.items() if key != "payload"
        }
        if state == "terminal":
            assessment["terminal_worker_kinds"].append(kind)
            result = status.get("payload", {}).get("result")
            if kind == "series":
                contract = validate_series_cleanup_contract(result, job_id)
                if contract is None:
                    unsafe.append("series:unexpected_terminal_result")
                else:
                    assessment["series_root"] = contract["series_root"]
                    assessment["series_cleanup_contract"] = contract
        elif state in {"active", "unavailable"}:
            unsafe.append(f"{kind}:{state}")
    if unsafe:
        assessment["reason"] = "worker_cleanup_deferred:" + ",".join(unsafe)
        return assessment
    assessment["safe"] = True
    assessment["reason"] = "terminal_and_worker_safe"
    return assessment


def path_inside(path, roots):
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    return any(resolved != root.resolve() and resolved.is_relative_to(root.resolve()) for root in roots)


def remove_exact_probe_paths(candidates):
    removed = []
    refused = []
    if KEEP_PROBE_FILES:
        return removed, refused
    unique = {}
    for source, job_id in candidates:
        text = str(source or "").strip()
        if text:
            unique[text] = str(job_id or "")
    for source in sorted(unique, key=len):
        path = Path(source)
        job_id = unique[source]
        if not path.exists():
            continue
        if path.is_symlink() or not path_inside(path, ALLOWED_CLEANUP_ROOTS):
            refused.append({"path": str(path), "reason": "outside_allowed_or_symlink"})
            continue
        exact_series_final = (
            job_id == SERIES_JOB_ID
            and SERIES_FINAL_ROOT is not None
            and path.resolve(strict=False) == SERIES_FINAL_ROOT.resolve(strict=False)
            and series_name_key(path.name) == SERIES_EXPECTED_KEY
        )
        if exact_series_final:
            verified, reason = verify_owned_series_root(path, job_id)
            if not verified:
                refused.append({"path": str(path), "reason": reason})
                continue
        if PROBE_ID not in path.name and path.name != job_id and not exact_series_final:
            refused.append({"path": str(path), "reason": "not_exact_probe_or_job"})
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        removed.append(str(path))
    return sorted(set(removed)), refused


def run_media_worker_smoke(worker_cleanup_requests):
    source = WORKSHOP_ROOT / f"{MEDIA_SMOKE_JOB_ID}_source"
    review_target = REVIEW_ROOT / source.name
    durable = WORKER_REPORT_ROOT / MEDIA_SMOKE_JOB_ID / WORKER_TERMINAL_FILES["movie"]
    result = {
        "ok": False,
        "job_id": MEDIA_SMOKE_JOB_ID,
        "kind": "movie",
        "initial_posts": 0,
        "replay_posts": 0,
        "durable_result": f"<CONFIG>/media-worker/{MEDIA_SMOKE_JOB_ID}/media_result.json",
        "cleanup_removed": [],
        "cleanup_deferred": "",
    }
    terminal_confirmed = False
    try:
        preflight = worker_status(MEDIA_SMOKE_JOB_ID, "movie")
        if preflight.get("status") != "not_found":
            raise RuntimeError(f"worker_smoke_preflight_{preflight.get('status')}")
        if source.exists() or review_target.exists() or durable.exists():
            raise RuntimeError("worker_smoke_paths_already_exist")
        source.mkdir(parents=True, exist_ok=False)
        (source / f"{MEDIA_SMOKE_JOB_ID}_marker.txt").write_text(
            f"{MEDIA_SMOKE_JOB_ID}\n",
            encoding="utf-8",
        )
        payload = {
            "job_id": MEDIA_SMOKE_JOB_ID,
            "source_path": str(source),
            "final_root": str(MOVIES_FINAL_ROOT),
            "review_root": str(REVIEW_ROOT),
            "reports_root": str(WORKER_REPORT_ROOT),
            "callback_url": "",
        }
        first_status, first = request_json(
            f"{WORKER_BASE_URL}/process-movie",
            method="POST",
            payload=payload,
            timeout=30,
        )
        result["initial_posts"] = 1
        if first_status != 200 or str(first.get("status") or "") not in {"done", "review"}:
            raise RuntimeError(f"worker_smoke_first_http_{first_status}")
        durable_deadline = time.time() + 3
        while not durable.exists() and time.time() < durable_deadline:
            time.sleep(0.05)
        if not durable.exists():
            raise RuntimeError("worker_smoke_durable_missing")
        before_bytes = durable.read_bytes()
        before_mtime = durable.stat().st_mtime_ns
        durable_payload = json.loads(before_bytes.decode("utf-8"))
        if durable_payload != first:
            raise RuntimeError("worker_smoke_durable_mismatch")
        terminal = worker_status(MEDIA_SMOKE_JOB_ID, "movie")
        if terminal.get("status") != "terminal":
            raise RuntimeError(f"worker_smoke_get_{terminal.get('status')}")
        if terminal["payload"].get("result") != first:
            raise RuntimeError("worker_smoke_get_result_mismatch")
        replay_status, replay = request_json(
            f"{WORKER_BASE_URL}/process-movie",
            method="POST",
            payload=payload,
            timeout=30,
        )
        result["replay_posts"] = 1
        if replay_status != first_status or replay != first:
            raise RuntimeError("worker_smoke_replay_mismatch")
        if durable.read_bytes() != before_bytes or durable.stat().st_mtime_ns != before_mtime:
            raise RuntimeError("worker_smoke_replay_rewrote_durable")
        terminal_confirmed = True
        result.update(
            {
                "ok": True,
                "first_http_status": first_status,
                "get_status": "terminal",
                "replay_http_status": replay_status,
                "terminal_status": str(first.get("status") or ""),
                "execution_evidence": "durable_unchanged_after_replay",
            }
        )
    except Exception as error:
        result["error"] = type(error).__name__ + ":" + str(error)[:300]
    finally:
        final_status = worker_status(MEDIA_SMOKE_JOB_ID, "movie")
        if final_status.get("status") == "terminal":
            terminal_confirmed = True
        if terminal_confirmed:
            cleanup_candidates = [
                (str(source), MEDIA_SMOKE_JOB_ID),
                (str(review_target), MEDIA_SMOKE_JOB_ID),
            ]
            removed, refused = remove_exact_probe_paths(cleanup_candidates)
            result["cleanup_removed"] = removed
            if KEEP_PROBE_FILES:
                result["cleanup_preserved"] = [
                    str(source),
                    str(review_target),
                    str(durable.parent),
                ]
            if refused:
                result["cleanup_refused"] = refused
                result["ok"] = False
            if not KEEP_PROBE_FILES:
                worker_cleanup_requests.append(
                    {
                        "job_id": MEDIA_SMOKE_JOB_ID,
                        "terminal_kinds": ["movie"],
                        "probe_id": PROBE_ID,
                    }
                )
        else:
            result["cleanup_deferred"] = str(final_status.get("status") or "unavailable")
            result["ok"] = False
    return result


worker_cleanup_requests = []
series_cleanup_requests = []
media_worker_smoke = run_media_worker_smoke(worker_cleanup_requests)

created = [
    {
        "category": "tv",
        "path": str(SERIES_PROBE_SOURCE),
    }
]
for category, root, suffix, filename in (
    ("movies", MOVIES_PROBE_ROOT, "pelicula", f"{PROBE_ID}_pelicula_2026.mkv"),
):
    folder = root / f"{PROBE_ID}_{suffix}"
    folder.mkdir(parents=True, exist_ok=False)
    media = folder / filename
    media.write_bytes((f"{PROBE_ID}\n{category}\n").encode("utf-8"))
    marker = folder / "CODEx_PROBE_DO_NOT_KEEP.txt"
    marker.write_text(f"{PROBE_ID}\ncategory={category}\n", encoding="utf-8")
    created.append({"category": category, "path": str(folder)})

deadline = time.time() + TIMEOUT
jobs = []
while time.time() < deadline:
    jobs = fetch_jobs()
    if len(jobs) >= 2 and all(str(job.get("state")) in TERMINAL for job in jobs):
        time.sleep(ARTIFACT_GRACE_SECONDS)
        jobs = fetch_jobs()
        break
    time.sleep(3)

series_jobs = [job for job in jobs if category_key(job.get("category")) == "tv"]
if len(series_jobs) == 1:
    SERIES_JOB_ID = str(series_jobs[0].get("job_id") or "")

inspections, inspection_attempts = inspect_jobs_with_retry(jobs)
assessments = [assess_cleanup(job) for job in jobs]
assessment_by_job = {item["job_id"]: item for item in assessments}
cleanup_candidates = []
cleanup_deferred = []
for job in jobs:
    job_id = str(job.get("job_id") or "")
    assessment = assessment_by_job[job_id]
    if not assessment["safe"]:
        cleanup_deferred.append({"job_id": job_id, "reason": assessment["reason"]})
        continue
    if assessment.get("series_root"):
        SERIES_FINAL_ROOT = TV_FINAL_ROOT / str(assessment["series_root"])
        SERIES_CLEANUP_CONTRACT = assessment.get("series_cleanup_contract")
        cleanup_candidates.append((str(SERIES_FINAL_ROOT), job_id))
    cleanup_candidates.extend(
        (
            (str(job.get("source_path") or ""), job_id),
            (str(job.get("stage_path") or ""), job_id),
            (str(WORKSHOP_ROOT / job_id), job_id),
        )
    )
    terminal_kinds = assessment["terminal_worker_kinds"]
    media_kinds = [kind for kind in terminal_kinds if kind != "series"]
    if media_kinds and not KEEP_PROBE_FILES:
        worker_cleanup_requests.append(
            {
                "job_id": job_id,
                "terminal_kinds": media_kinds,
                "probe_id": PROBE_ID,
            }
        )
    if "series" in terminal_kinds and not KEEP_PROBE_FILES:
        contract = assessment["series_cleanup_contract"]
        series_cleanup_requests.append(
            {
                "job_id": job_id,
                "terminal_kinds": ["series"],
                "probe_id": PROBE_ID,
                "expected_series_key": SERIES_EXPECTED_KEY,
                "generation": contract["generation"],
                "published_manifest_digest": contract["published_manifest"]["digest"],
            }
        )

for source in created:
    matching = [
        job for job in jobs
        if category_key(job.get("category")) == source["category"]
    ]
    if matching and all(
        assessment_by_job[str(job.get("job_id") or "")]["safe"]
        for job in matching
    ):
        cleanup_candidates.append((source["path"], str(matching[0].get("job_id") or "")))
    else:
        cleanup_deferred.append(
            {"source": source["path"], "reason": "category_not_terminal_or_worker_unsafe"}
        )

cleanup_removed, cleanup_refused = remove_exact_probe_paths(cleanup_candidates)
all_noise = [item for job in inspections for item in job["noise"]]
expected_outcome_errors = [
    error for inspection in inspections for error in expected_probe_outcome(inspection)
]
category_counts = {
    "movies": len([job for job in jobs if category_key(job.get("category")) == "movies"]),
    "tv": len([job for job in jobs if category_key(job.get("category")) == "tv"]),
}
category_shape_ok = len(jobs) == 2 and category_counts == {"movies": 1, "tv": 1}
if not category_shape_ok:
    expected_outcome_errors.append(
        "unexpected_probe_job_shape:"
        + json.dumps(category_counts, sort_keys=True, separators=(",", ":"))
    )
all_terminal = category_shape_ok and all(
    str(job.get("state")) in TERMINAL for job in jobs
)
cleanup_safe = not cleanup_refused and not (
    cleanup_deferred and not KEEP_PROBE_FILES
)
ok = (
    all_terminal
    and not all_noise
    and not expected_outcome_errors
    and media_worker_smoke["ok"]
    and cleanup_safe
)
report = {
    "ok": ok,
    "probe_id": PROBE_ID,
    "created_sources": [item["path"] for item in created],
    "terminal_jobs": len([job for job in jobs if str(job.get("state")) in TERMINAL]),
    "jobs_seen": len(jobs),
    "category_counts": category_counts,
    "jobs": inspections,
    "artifact_inspection_attempts": inspection_attempts,
    "media_worker_smoke": media_worker_smoke,
    "cleanup_assessments": assessments,
    "cleanup_removed": cleanup_removed,
    "cleanup_deferred": cleanup_deferred,
    "cleanup_refused": cleanup_refused,
    "noise": all_noise,
    "expected_outcome_errors": expected_outcome_errors,
}
report = report_safe(report)
print("ARR_REAL_FLOW_WORKER_CLEANUP_JSON " + json.dumps(worker_cleanup_requests, separators=(",", ":")))
print("ARR_REAL_FLOW_SERIES_CLEANUP_JSON " + json.dumps(series_cleanup_requests, separators=(",", ":")))
print("ARR_REAL_FLOW_JSON_START")
print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
print("ARR_REAL_FLOW_JSON_END")
if not ok:
    raise SystemExit("ARR_REAL_FLOW_PROBE_FAILED")
print("ARR_REAL_FLOW_OK")
PY
)"
probe_exit=$?
set -e
printf '%s\n' "$probe_output"

cleanup_json="$(printf '%s\n' "$probe_output" | sed -n 's/^ARR_REAL_FLOW_WORKER_CLEANUP_JSON //p' | tail -n 1)"
series_cleanup_json="$(printf '%s\n' "$probe_output" | sed -n 's/^ARR_REAL_FLOW_SERIES_CLEANUP_JSON //p' | tail -n 1)"
cleanup_exit=0
if [ -n "$cleanup_json" ] && [ "$cleanup_json" != "[]" ]; then
  set +e
  cleanup_output="$(sudo docker exec -i \
    -e ARR_WORKER_CLEANUP_JSON="$cleanup_json" \
    __WORKER_CONTAINER__ python3 - <<'PY'
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path("/config/media-worker").resolve()
KINDS = ("movie", "trailer", "bluray")
FILENAMES = {
    "movie": "media_result.json",
    "trailer": "trailer_result.json",
    "bluray": "bluray_result.json",
}
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def status(job_id, kind):
    url = (
        f"http://127.0.0.1:8790/jobs/{urllib.parse.quote(job_id, safe='')}/status"
        f"?kind={urllib.parse.quote(kind, safe='')}"
    )
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            http_status = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        http_status = error.code
        raw = error.read()
    except Exception as error:
        return "unavailable", {"error_type": type(error).__name__}
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return "unavailable", {"error_type": "invalid_json"}
    state = str(payload.get("status") or "") if isinstance(payload, dict) else ""
    if http_status == 200 and state in {"active", "terminal"}:
        return state, payload
    if http_status == 404 and state == "not_found":
        return "not_found", payload
    return "unavailable", {"http_status": http_status, "state": state}


items = json.loads(os.environ.get("ARR_WORKER_CLEANUP_JSON", "[]"))
if not isinstance(items, list):
    raise SystemExit("ARR_WORKER_CLEANUP_INVALID_MANIFEST")
removed = []
deferred = []
for item in items:
    if not isinstance(item, dict):
        raise SystemExit("ARR_WORKER_CLEANUP_INVALID_ITEM")
    job_id = str(item.get("job_id") or "")
    expected = sorted(set(item.get("terminal_kinds") or []))
    if not JOB_ID_RE.fullmatch(job_id) or not expected or any(kind not in KINDS for kind in expected):
        raise SystemExit("ARR_WORKER_CLEANUP_INVALID_TARGET")
    job_dir = (ROOT / job_id).resolve()
    if job_dir.parent != ROOT or job_dir.name != job_id:
        raise SystemExit("ARR_WORKER_CLEANUP_OUTSIDE_ROOT")
    if not job_dir.exists():
        removed.append({"job_id": job_id, "status": "already_absent"})
        continue
    statuses = {}
    unsafe = []
    for kind in KINDS:
        state, payload = status(job_id, kind)
        statuses[kind] = state
        if state in {"active", "unavailable"}:
            unsafe.append(f"{kind}:{state}")
        if state == "terminal":
            result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(result, dict) or str(result.get("job_id") or "") != job_id or str(result.get("kind") or "") != kind:
                unsafe.append(f"{kind}:foreign_terminal")
    if unsafe:
        deferred.append({"job_id": job_id, "reason": ",".join(unsafe)})
        continue
    for kind in expected:
        if statuses.get(kind) != "terminal":
            deferred.append({"job_id": job_id, "reason": f"{kind}:terminal_missing"})
            break
        terminal_path = job_dir / FILENAMES[kind]
        terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
        if str(terminal.get("job_id") or "") != job_id or str(terminal.get("kind") or "") != kind:
            raise SystemExit("ARR_WORKER_CLEANUP_TERMINAL_MISMATCH")
    else:
        shutil.rmtree(job_dir)
        removed.append({"job_id": job_id, "status": "removed_exact_job_dir"})

print("ARR_WORKER_CLEANUP_JSON " + json.dumps({"removed": removed, "deferred": deferred}, ensure_ascii=False, sort_keys=True))
if deferred:
    raise SystemExit("ARR_WORKER_CLEANUP_DEFERRED")
print("ARR_WORKER_CLEANUP_OK")
PY
)"
  cleanup_exit=$?
  set -e
  printf '%s\n' "$cleanup_output"
else
  printf '%s\n' "ARR_WORKER_CLEANUP_NO_REQUESTS"
  printf '%s\n' "ARR_WORKER_CLEANUP_OK"
fi

series_cleanup_exit=0
if [ -n "$series_cleanup_json" ] && [ "$series_cleanup_json" != "[]" ]; then
  set +e
  series_cleanup_output="$(sudo docker exec -i \
    -e ARR_SERIES_CLEANUP_JSON="$series_cleanup_json" \
    __SERIES_WORKER_CONTAINER__ python3 - <<'PY'
import json
import os
import re
import shutil
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path("/config/series-worker").resolve()
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def series_name_key(value):
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", str(value or "")).casefold()
        if character.isalnum() and not unicodedata.combining(character)
    )


def terminal_status(job_id):
    url = (
        f"http://127.0.0.1:8791/jobs/{urllib.parse.quote(job_id, safe='')}/status"
        "?kind=series"
    )
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    if status != 200 or payload.get("status") != "terminal":
        raise RuntimeError(f"series_terminal_missing:{status}:{payload.get('status')}")
    result = payload.get("result")
    if (
        not isinstance(result, dict)
        or str(result.get("job_id") or "") != job_id
        or result.get("kind") != "series"
        or result.get("status") not in {"done", "review", "failed"}
    ):
        raise RuntimeError("series_terminal_mismatch")
    return result


items = json.loads(os.environ.get("ARR_SERIES_CLEANUP_JSON", "[]"))
if not isinstance(items, list):
    raise SystemExit("ARR_SERIES_CLEANUP_INVALID_MANIFEST")
removed = []
for item in items:
    if not isinstance(item, dict):
        raise SystemExit("ARR_SERIES_CLEANUP_INVALID_ITEM")
    job_id = str(item.get("job_id") or "")
    probe_id = str(item.get("probe_id") or "")
    expected_series_key = str(item.get("expected_series_key") or "")
    expected_generation = str(item.get("generation") or "")
    expected_manifest_digest = str(item.get("published_manifest_digest") or "")
    if (
        not JOB_ID_RE.fullmatch(job_id)
        or not probe_id.startswith("codex_live_flow_probe_")
        or item.get("terminal_kinds") != ["series"]
        or expected_series_key != "treme"
        or not re.fullmatch(
            r"(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})",
            expected_generation,
        )
        or not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_digest)
    ):
        raise SystemExit("ARR_SERIES_CLEANUP_INVALID_TARGET")
    job_dir = (ROOT / job_id).resolve()
    if job_dir.parent != ROOT or job_dir.name != job_id:
        raise SystemExit("ARR_SERIES_CLEANUP_OUTSIDE_ROOT")
    if not job_dir.exists():
        removed.append({"job_id": job_id, "status": "already_absent"})
        continue
    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest_series = str(manifest.get("series_name") or "")
    if series_name_key(manifest_series) != expected_series_key:
        raise SystemExit("ARR_SERIES_CLEANUP_PROBE_MISMATCH")
    result = terminal_status(job_id)
    delivery = result.get("delivery")
    published_manifest = result.get("published_manifest")
    if (
        result.get("status") != "done"
        or result.get("series_root") != manifest_series
        or series_name_key(result.get("series_root")) != expected_series_key
        or not isinstance(delivery, dict)
        or delivery.get("mode") != "new"
        or delivery.get("cleanup_pending") is not False
        or delivery.get("generation") != expected_generation
        or not isinstance(published_manifest, dict)
        or published_manifest.get("digest") != expected_manifest_digest
    ):
        raise SystemExit("ARR_SERIES_CLEANUP_RESULT_MISMATCH")
    shutil.rmtree(job_dir)
    removed.append({"job_id": job_id, "status": "removed_exact_job_dir"})

print("ARR_SERIES_CLEANUP_JSON " + json.dumps({"removed": removed}, ensure_ascii=False, sort_keys=True))
print("ARR_SERIES_CLEANUP_OK")
PY
)"
  series_cleanup_exit=$?
  set -e
  printf '%s\n' "$series_cleanup_output"
else
  printf '%s\n' "ARR_SERIES_CLEANUP_NO_REQUESTS"
  printf '%s\n' "ARR_SERIES_CLEANUP_OK"
fi

if [ "$probe_exit" -ne 0 ]; then
  exit "$probe_exit"
fi
if [ "$cleanup_exit" -ne 0 ]; then
  exit "$cleanup_exit"
fi
if [ "$series_cleanup_exit" -ne 0 ]; then
  exit "$series_cleanup_exit"
fi
'@

$remote = $remote.Replace("__REMOTE_DIR__", $RemoteDir)
$remote = $remote.Replace("__CONTAINER__", $ContainerName)
$remote = $remote.Replace("__WORKER_CONTAINER__", $WorkerContainerName)
$remote = $remote.Replace("__SERIES_WORKER_CONTAINER__", $SeriesWorkerContainerName)
$remote = $remote.Replace("__TIMEOUT__", [string]$TimeoutSeconds)
$remote = $remote.Replace("__KEEP_PROBE__", ($(if ($KeepProbeFiles) { "1" } else { "0" })))

$output = [System.Collections.Generic.List[string]]::new()
$exitCode = -1
$previousErrorActionPreference = $ErrorActionPreference
$nativePreferenceExists = Test-Path -LiteralPath Variable:PSNativeCommandUseErrorActionPreference
if ($nativePreferenceExists) {
  $previousNativePreference = $PSNativeCommandUseErrorActionPreference
}
try {
  $ErrorActionPreference = "Continue"
  if ($nativePreferenceExists) {
    $PSNativeCommandUseErrorActionPreference = $false
  }
  $remote | ssh $HostName "tr -d '\r' | sh -s" 2>&1 | ForEach-Object {
    $line = [string]$_
    $output.Add($line)
    Write-Host $line
  }
  if ($null -ne $LASTEXITCODE) {
    $exitCode = [int]$LASTEXITCODE
  }
} catch {
  $output.Add("LOCAL_SSH_EXCEPTION $($_.Exception.GetType().Name): $($_.Exception.Message)")
  if ($null -ne $LASTEXITCODE) {
    $exitCode = [int]$LASTEXITCODE
  }
} finally {
  if ($nativePreferenceExists) {
    $PSNativeCommandUseErrorActionPreference = $previousNativePreference
  }
  $ErrorActionPreference = $previousErrorActionPreference
  if ($output.Count -eq 0) {
    "" | Set-Content -LiteralPath $transcriptPath -Encoding UTF8
  } else {
    $output | Set-Content -LiteralPath $transcriptPath -Encoding UTF8
  }
}

$joined = $output -join "`n"
$match = [regex]::Match($joined, "ARR_REAL_FLOW_JSON_START\s*(?<json>\{[\s\S]*?\})\s*ARR_REAL_FLOW_JSON_END")
if ($match.Success) {
  $summaryPath = Join-Path $artifactDir "summary.json"
  $match.Groups["json"].Value | Set-Content -LiteralPath $summaryPath -Encoding UTF8
}

if ($exitCode -ne 0) {
  throw "ARR_REAL_FLOW_FAILED exit=$exitCode artifact=$artifactDir transcript=$transcriptPath"
}
if ($joined -notmatch "ARR_REAL_FLOW_OK") {
  throw "ARR_REAL_FLOW_NO_OK artifact=$artifactDir transcript=$transcriptPath"
}
if ($joined -notmatch "ARR_WORKER_CLEANUP_OK") {
  throw "ARR_REAL_FLOW_WORKER_CLEANUP_NO_OK artifact=$artifactDir transcript=$transcriptPath"
}

Write-Host "ARR_REAL_FLOW_ARTIFACT $artifactDir"
Write-Host "ARR_REAL_FLOW_DONE"
