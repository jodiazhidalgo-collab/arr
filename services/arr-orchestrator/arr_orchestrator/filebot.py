import json
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from .filesystem import MEDIA_EXTENSIONS
from .name_resolver import ResolvedIdentity


MOVE_PATTERN = re.compile(
    r"^\[(?:MOVE|COPY|HARDLINK|CLONE)\] from \[(.+?)\] to \[(.+?)\]$",
    re.MULTILINE | re.IGNORECASE,
)
MAX_RAW_LOG_BYTES = 250_000
FILEBOT_TIMEOUT_SECONDS = 14400


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
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=int(preview["timeout_sec"]),
            check=False,
        )
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
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
        scanned_media = [
            str(path)
            for path in output_root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in MEDIA_EXTENSIONS
            and path.stat().st_mtime >= started - 2
        ]
        output_media = list(dict.fromkeys([*moved_media, *scanned_media]))
        raw_log_truncated = trim_raw_log(log_file)
        payload = {
            "exit_code": result.returncode,
            "moves": moves,
            "output_media": output_media,
            "duplicate": is_duplicate_output(combined, moves),
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
        }

    def _legacy_amc_command(
        self, category: str, input_path: Path, output_root: Path, log_file: Path
    ) -> List[str]:
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
            "es",
            "--def",
            "clean=y",
            "music=n",
            "artwork=n",
            "excludeList=/dev/null",
        ]
        if category == "movies":
            command.extend(
                [
                    "ut_label=movie",
                    "movieFormat={n} ({y})/{n} ({y})",
                ]
            )
        elif category == "tv":
            command.extend(
                [
                    "ut_label=TV",
                    "minLengthMS=300000",
                    "seriesFormat={n}/Season {s.pad(2)}/{n} - {s00e00}",
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
        database = "TheMovieDB" if category == "movies" else "TheMovieDB::TV"
        output_format = (
            "{n} ({y})/{n} ({y})"
            if category == "movies"
            else "{n}/Season {s.pad(2)}/{n} - {s00e00}"
        )
        return [
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
            "es",
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
