import json
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from .filesystem import MEDIA_EXTENSIONS
from .name_resolver import ResolvedIdentity


MOVE_PATTERN = re.compile(
    r"^\[(?:MOVE|COPY|HARDLINK|CLONE)\] from \[(.+?)\] to \[(.+?)\]$",
    re.MULTILINE | re.IGNORECASE,
)
MAX_RAW_LOG_BYTES = 250_000
FILEBOT_TIMEOUT_SECONDS = 14400

MOVIE_FORMAT = "{n} ({y})/{n} ({y})"
TV_FORMAT = "{n}/Season {s.pad(2)}/{n} - {s00e00}"


def is_duplicate_output(output: str, moves: List[Dict[str, str]]) -> bool:
    lowered = output.lower()
    return (
        not moves
        and "[skip] skipped" in lowered
        and "already exists" in lowered
        and "processed 0 files" in lowered
    )


def trim_raw_log(path: Path, max_bytes: int = MAX_RAW_LOG_BYTES) -> bool:
    if not path.exists() or path.stat().st_size <= max_bytes:
        return False
    keep = max_bytes - 512
    with path.open("rb") as handle:
        handle.seek(-keep, 2)
        tail = handle.read()
    header = (
        b"[arr-orchestrator] Log FileBot recortado: se conserva el final "
        b"para evitar ruido excesivo.\n\n"
    )
    path.write_bytes(header + tail)
    return True


class FileBotRunner:
    def __init__(self, binary: str, log_dir: Path):
        self.binary = binary
        self.log_dir = log_dir
        self._identity_rules_snapshot: Dict[str, object] = {}

    def configure_identity_rules(self, rules: Optional[Mapping[str, object]]) -> None:
        """Captura el locale del resolver del mismo snapshot que gobierna el job."""

        self._identity_rules_snapshot = json.loads(
            json.dumps(dict(rules or {}), ensure_ascii=False, default=str)
        )

    def run(
        self,
        job_id: str,
        category: str,
        input_path: Path,
        output_root: Path,
        identity: Optional[ResolvedIdentity] = None,
    ) -> Dict[str, object]:
        identity = self._require_guided_identity(category, identity)
        preview = self.preview_command(job_id, category, input_path, output_root, identity)
        log_file = Path(str(preview["log_file"]))
        command = list(preview["argv"])
        started = time.time()
        timed_out = False
        timeout_message = ""
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=int(preview["timeout_sec"]),
                check=False,
            )
            exit_code = completed.returncode
            combined = (completed.stdout or "") + "\n" + (completed.stderr or "")
        except subprocess.TimeoutExpired as error:
            timed_out = True
            exit_code = 124
            stdout = _timeout_text(error.stdout)
            stderr = _timeout_text(error.stderr)
            timeout_message = (
                f"FileBot agoto el timeout de {int(preview['timeout_sec'])} segundos"
            )
            combined = f"{stdout}\n{stderr}\n{timeout_message}"
            log_tail = _read_text_tail(log_file, MAX_RAW_LOG_BYTES)
            if log_tail:
                combined = f"{combined}\n{log_tail}"
        moves = [
            {"source": source, "destination": destination}
            for source, destination in MOVE_PATTERN.findall(combined)
        ]
        moved_media = [
            item["destination"]
            for item in moves
            if Path(item["destination"]).suffix.lower() in MEDIA_EXTENSIONS
            and Path(item["destination"]).exists()
        ]
        # El log de FileBot es la fuente rapida y exacta. Solo recorremos el
        # output compartido cuando no hay ningun movimiento multimedia
        # confirmado (por ejemplo, un timeout antes de vaciar stdout).
        scanned_media = [] if moved_media else [
            str(path)
            for path in output_root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in MEDIA_EXTENSIONS
            and path.stat().st_mtime >= started - 2
        ]
        output_media = list(dict.fromkeys([*moved_media, *scanned_media]))
        raw_log_truncated = trim_raw_log(log_file)
        payload = {
            "exit_code": exit_code,
            "moves": moves,
            "output_media": output_media,
            "duplicate": is_duplicate_output(combined, moves),
            "timed_out": timed_out,
            "timeout_message": timeout_message,
            "started_at": started,
            "stdout_tail": combined[-6000:],
            "log_file": str(log_file),
            "raw_log_truncated": raw_log_truncated,
            "identity": identity.to_dict(),
            "mode": "guided",
            "command_preview": preview,
        }
        result_file = self.log_dir / f"filebot-{job_id}.json"
        result_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return payload

    def preview_command(
        self,
        job_id: str,
        category: str,
        input_path: Path,
        output_root: Path,
        identity: Optional[ResolvedIdentity] = None,
    ) -> Dict[str, object]:
        identity = self._require_guided_identity(category, identity)
        log_file = self.log_dir / f"filebot-{job_id}.log"
        command = self._guided_command(category, input_path, output_root, log_file, identity)
        return {
            "argv": command,
            "mode": "guided",
            "cwd": str(input_path),
            "log_file": str(log_file),
            "timeout_sec": FILEBOT_TIMEOUT_SECONDS,
            "rules": self._command_rules_summary(category),
            "tmdb_id": identity.tmdb_id,
        }

    @staticmethod
    def _require_guided_identity(
        category: str,
        identity: Optional[ResolvedIdentity],
    ) -> ResolvedIdentity:
        if identity is None:
            raise ValueError(
                "FileBot requiere una identidad TMDb aceptada; AMC sin identidad esta desactivado"
            )
        expected_media_type = "movie" if category == "movies" else "tv" if category == "tv" else ""
        if not expected_media_type or identity.media_type != expected_media_type:
            raise ValueError("La identidad TMDb no coincide con la categoria de FileBot")
        if int(identity.tmdb_id) <= 0:
            raise ValueError("La identidad TMDb no contiene un identificador valido")
        if str(identity.resolver_algorithm_version or "") != "phased-er-v2":
            raise ValueError(
                "FileBot solo puede ejecutar identidades creadas por phased-er-v2"
            )
        if str(identity.decision_status or "").upper() not in {
            "ACCEPTED_CONFIDENT",
            "ACCEPTED_FALLBACK",
        }:
            raise ValueError(
                "FileBot requiere una decision v2 aceptada de forma explicita"
            )
        return identity

    def _guided_command(
        self,
        category: str,
        input_path: Path,
        output_root: Path,
        log_file: Path,
        identity: ResolvedIdentity,
    ) -> List[str]:
        database = "TheMovieDB" if category == "movies" else "TheMovieDB::TV"
        output_format = MOVIE_FORMAT if category == "movies" else TV_FORMAT
        command = [
            self.binary,
            "-no-xattr",
            "-rename",
            "-r",
            str(input_path),
            "--log-file",
            str(log_file),
            "--db",
            database,
            "--q",
            str(identity.tmdb_id),
            "--lang",
            self._guided_language(category),
            "--output",
            str(output_root),
            "--action",
            "move",
            "--conflict",
            "skip",
            "--format",
            output_format,
        ]
        return command

    def _guided_locale(self, category: str) -> str:
        resolver = self._identity_rules_snapshot.get("resolver")
        locales = resolver.get("locales") if isinstance(resolver, dict) else None
        category_locale = locales.get(category) if isinstance(locales, dict) else None
        language = (
            category_locale.get("language")
            if isinstance(category_locale, dict)
            else None
        )
        return str(language or "es-ES")

    def _guided_language(self, category: str) -> str:
        return _filebot_language(self._guided_locale(category))

    def _command_rules_summary(self, category: str) -> Dict[str, object]:
        language = self._guided_locale(category)
        return {
            "language": language,
            "format": MOVIE_FORMAT if category == "movies" else TV_FORMAT,
            "safety": {
                "action": "move",
                "conflict": "skip",
                "identity": "tmdb-id-required",
            },
        }


def _filebot_language(value: str) -> str:
    language = str(value or "es-ES").strip().replace("_", "-")
    primary = language.split("-", 1)[0].lower()
    return primary if re.fullmatch(r"[a-z]{2,3}", primary) else "es"


def _timeout_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _read_text_tail(path: Path, max_bytes: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(-max_bytes, 2)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
