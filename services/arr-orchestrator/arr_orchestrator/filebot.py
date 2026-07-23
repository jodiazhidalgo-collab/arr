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

MOVIE_FORMATS = {
    "title_year": "{n} ({y})/{n} ({y})",
    "title_year_quality": "{n} ({y})/{n} ({y}) [{vf}]",
}
TV_FORMATS = {
    "series_sxxexx": "{n}/Season {s.pad(2)}/{n} - {s00e00}",
    "series_sxxexx_title": "{n}/Season {s.pad(2)}/{n} - {s00e00} - {t}",
}
TV_ORDERS = {"Airdate", "DVD", "Absolute"}


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
        self._rules_snapshot: Dict[str, object] = {}

    def configure_rules(self, rules: Optional[Mapping[str, object]]) -> None:
        """Capture one normalized snapshot before previewing/running a job.

        Engine processes jobs serially. Keeping the snapshot here preserves the
        historical ``run`` signature used by existing integrations and tests,
        while ensuring a concurrent settings save cannot alter a running job.
        """

        self._rules_snapshot = json.loads(
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
            "identity": identity.to_dict() if identity else None,
            "mode": "guided" if identity else "legacy_amc",
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
        log_file = self.log_dir / f"filebot-{job_id}.log"
        command = (
            self._guided_command(category, input_path, output_root, log_file, identity)
            if identity
            else self._legacy_amc_command(category, input_path, output_root, log_file)
        )
        return {
            "argv": command,
            "mode": "guided" if identity else "legacy_amc",
            "cwd": str(input_path),
            "log_file": str(log_file),
            "timeout_sec": FILEBOT_TIMEOUT_SECONDS,
            "rules": self._command_rules_summary(category),
        }

    def _legacy_amc_command(
        self, category: str, input_path: Path, output_root: Path, log_file: Path
    ) -> List[str]:
        rules = self._category_rules(category)
        language = _filebot_language(str(rules.get("language") or "es-ES"))
        command: List[str] = [
            self.binary,
            "-no-xattr",
            "-script",
            "fn:amc",
            str(input_path),
            "--log-file",
            str(log_file),
            "--output",
            str(output_root),
            "--action",
            "move",
            "--conflict",
            "skip",
            "-non-strict",
            "--lang",
            language,
            "--def",
            "clean=y",
            "music=n",
            "artwork=n",
            "excludeList=/dev/null",
        ]
        if category == "movies":
            output_format = MOVIE_FORMATS.get(
                str(rules.get("filename_style") or "title_year"),
                MOVIE_FORMATS["title_year"],
            )
            command.extend(
                [
                    "ut_label=movie",
                    f"movieFormat={output_format}",
                ]
            )
        elif category == "tv":
            output_format = TV_FORMATS.get(
                str(rules.get("filename_style") or "series_sxxexx"),
                TV_FORMATS["series_sxxexx"],
            )
            command.extend(
                [
                    "ut_label=TV",
                    "minLengthMS=300000",
                    f"seriesFormat={output_format}",
                ]
            )
        return command

    def _guided_command(
        self,
        category: str,
        input_path: Path,
        output_root: Path,
        log_file: Path,
        identity: ResolvedIdentity,
    ) -> List[str]:
        rules = self._category_rules(category)
        database = "TheMovieDB" if category == "movies" else "TheMovieDB::TV"
        output_format = (
            MOVIE_FORMATS.get(
                str(rules.get("filename_style") or "title_year"),
                MOVIE_FORMATS["title_year"],
            )
            if category == "movies"
            else TV_FORMATS.get(
                str(rules.get("filename_style") or "series_sxxexx"),
                TV_FORMATS["series_sxxexx"],
            )
        )
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
            _filebot_language(str(rules.get("language") or "es-ES")),
            "--output",
            str(output_root),
            "--action",
            "move",
            "--conflict",
            "skip",
            "-non-strict",
            "--format",
            output_format,
        ]
        order = str(rules.get("episode_order") or "Airdate")
        if category == "tv" and order in TV_ORDERS and order != "Airdate":
            command.extend(["--order", order])
        return command

    def _category_rules(self, category: str) -> Dict[str, object]:
        value = self._rules_snapshot.get(category)
        return dict(value) if isinstance(value, dict) else {}

    def _command_rules_summary(self, category: str) -> Dict[str, object]:
        rules = self._category_rules(category)
        return {
            "language": str(rules.get("language") or "es-ES"),
            "region": (
                str(rules.get("region") or "ES") if category == "movies" else None
            ),
            "filename_style": str(
                rules.get("filename_style")
                or ("title_year" if category == "movies" else "series_sxxexx")
            ),
            "episode_order": (
                str(rules.get("episode_order") or "Airdate")
                if category == "tv"
                else None
            ),
            "safety": {
                "action": "move",
                "conflict": "skip",
                "strictness": "non-strict",
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
