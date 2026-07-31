"""Procesado audiovisual secuencial y aislado para paquetes de series."""

from __future__ import annotations

import html
import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Protocol, Sequence

from .manifest import ManifestEntry, ManifestSidecar, SeriesManifest, validate_relative_path
from .rules import RulesSnapshot


BASE_TOOLS = ("ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit")
OCR_TOOLS = ("seconv", "tesseract", "mkvextract", "vobsubocr")
TOOL_SMOKE_ARGS = {
    "ffmpeg": ("-version",),
    "ffprobe": ("-version",),
    "mkvmerge": ("--version",),
    "mkvpropedit": ("--version",),
    "seconv": ("--version",),
    "tesseract": ("--version",),
    "mkvextract": ("--version",),
    "vobsubocr": ("--help",),
}
SUPPORTED_INPUTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov",
    ".wmv", ".ts", ".m2ts", ".mts", ".webm",
}
SRT_TIMING_RE = re.compile(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->")


class ProcessingError(RuntimeError):
    """El episodio no se puede transformar con seguridad."""


class ToolUnavailableError(ProcessingError):
    """Falta una herramienta requerida por la ruta seleccionada."""


class VerificationError(ProcessingError):
    """La salida provisional no supera la verificación completa."""


class ReviewRequiredError(ProcessingError):
    """El contenido debe conservarse completo para revisión humana."""

    code = "series_review_required"


class EpisodeProcessingError(ProcessingError):
    """Fallo fail-closed con las salidas previas todavía provisionales."""

    def __init__(
        self,
        message: str,
        *,
        entry: ManifestEntry,
        partial_results: Sequence["ProcessedEpisode"],
    ) -> None:
        super().__init__(message)
        self.entry = entry
        self.partial_results = tuple(partial_results)


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]: ...

    def which(self, executable: str) -> str | None: ...


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def which(self, executable: str) -> str | None:
        return shutil.which(executable)


@dataclass(frozen=True)
class SubtitleChoice:
    mode: str
    stream_index: int | None = None
    external_path: Path | None = None
    codec: str = ""
    cues: int | None = None
    title: str = ""


@dataclass(frozen=True)
class EpisodePlan:
    source: Path
    output: Path
    video: dict[str, Any]
    audio: dict[str, Any]
    subtitle: SubtitleChoice | None
    audio_mode: str
    rules_fingerprint: str
    duration: float


@dataclass(frozen=True)
class ProcessedEpisode:
    source_relpath: str
    target_relpath: str
    provisional_relpath: str
    output_size: int
    audio_mode: str
    subtitle_mode: str
    verification: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProcessingResult:
    status: str
    manifest_digest: str
    rules_fingerprint: str
    episodes: tuple[ProcessedEpisode, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "manifest_digest": self.manifest_digest,
            "rules_fingerprint": self.rules_fingerprint,
            "episodes": [episode.to_dict() for episode in self.episodes],
        }


def missing_tools(
    runner: Runner | None = None,
    names: Iterable[str] = BASE_TOOLS,
) -> list[str]:
    active = runner or SubprocessRunner()
    return sorted(name for name in names if not active.which(name))


def unavailable_tools(
    runner: Runner | None = None,
    names: Iterable[str] = BASE_TOOLS,
    *,
    timeout: int = 15,
    parallel: bool = False,
) -> list[str]:
    """Detecta binarios ausentes y binarios instalados que no pueden arrancar."""

    active = runner or SubprocessRunner()
    unavailable: set[str] = set()
    available: list[str] = []
    for name in names:
        if not active.which(name):
            unavailable.add(name)
        else:
            available.append(name)

    def probe(name: str) -> str | None:
        argv = [name, *TOOL_SMOKE_ARGS.get(name, ("--version",))]
        try:
            result = active.run(argv, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return name
        return name if result.returncode != 0 else None

    if parallel and len(available) > 1:
        with ThreadPoolExecutor(max_workers=len(available)) as executor:
            outcomes = executor.map(probe, available)
            unavailable.update(name for name in outcomes if name is not None)
    else:
        unavailable.update(
            name for name in (probe(candidate) for candidate in available) if name is not None
        )
    return sorted(unavailable)


def require_tools(runner: Runner, names: Iterable[str]) -> None:
    missing = missing_tools(runner, names)
    if missing:
        raise ToolUnavailableError("Faltan herramientas: " + ", ".join(missing))


def _run_checked(
    runner: Runner,
    argv: Sequence[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    label: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner.run(argv, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as error:
        raise ProcessingError(f"Tiempo agotado en {label}.") from error
    except OSError as error:
        raise ProcessingError(f"No se pudo ejecutar {label}: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "sin detalle").strip()[-1600:]
        raise ProcessingError(f"Falló {label}: {detail}")
    return result


def _probe(path: Path, runner: Runner, *, count_packets: bool = False) -> dict[str, Any]:
    argv = ["ffprobe", "-v", "error"]
    if count_packets:
        argv += ["-count_packets"]
    argv += ["-show_format", "-show_streams", "-print_format", "json", str(path)]
    result = _run_checked(runner, argv, timeout=900, label="ffprobe")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise ProcessingError("ffprobe devolvió JSON inválido.") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise ProcessingError("ffprobe no devolvió pistas.")
    return payload


def probe_media(path: Path | str, runner: Runner | None = None) -> dict[str, Any]:
    """API pequeña para diagnóstico y pruebas reales de fixtures."""

    active = runner or SubprocessRunner()
    require_tools(active, ("ffprobe",))
    return _probe(Path(path), active)


def _text(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def _language(stream: dict[str, Any]) -> str:
    tags = stream.get("tags") or {}
    for key, value in tags.items():
        if str(key).casefold() == "language":
            return str(value or "").strip().casefold()
    return ""


def _tag_text(stream: dict[str, Any]) -> str:
    return " ".join(_text(value) for value in (stream.get("tags") or {}).values())


def _is_spanish(stream: dict[str, Any], accepted: set[str]) -> bool:
    language = _language(stream)
    text = _tag_text(stream)
    return language in accepted or any(
        marker in text
        for marker in ("espanol", "español", "spanish", "castellano", "latino")
    )


def _duration(probe: dict[str, Any]) -> float:
    try:
        return max(0.0, float((probe.get("format") or {}).get("duration") or 0))
    except (TypeError, ValueError):
        return 0.0


def _disposition(enabled_default: bool, enabled_forced: bool) -> str:
    values = []
    if enabled_default:
        values.append("default")
    if enabled_forced:
        values.append("forced")
    return "+".join(values) or "0"


def _select_video_and_audio(
    probe: dict[str, Any], rules: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], str]:
    streams = probe.get("streams") or []
    videos = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video"
        and int((stream.get("disposition") or {}).get("attached_pic", 0) or 0) != 1
    ]
    expected = int(rules["video"]["pistas_exactas"])
    if len(videos) != expected or not videos:
        raise ProcessingError(f"Se esperaban {expected} pistas de vídeo y hay {len(videos)}.")

    audio_rules = rules["audio"]
    accepted_audio = {str(value).casefold() for value in audio_rules["idiomas_aceptados"]}
    direct = [
        stream
        for stream in streams
        if stream.get("codec_type") == "audio"
        and int(stream.get("channels") or 0) > 0
        and _is_spanish(stream, accepted_audio)
    ]

    video = videos[0]
    video_rules = rules["video"]
    video_language = _language(video)
    video_accepted = {str(value).casefold() for value in video_rules["idiomas_aceptados"]}
    video_indeterminate = {
        str(value).casefold() for value in video_rules["idiomas_indeterminados_como_es"]
    }
    video_correctible = {
        str(value).casefold() for value in video_rules["idiomas_corregibles_por_audio_es"]
    }
    video_ok = (
        video_language in video_accepted
        or video_language in video_indeterminate
        or (
            bool(video_rules["aceptar_por_audio_es"])
            and bool(direct)
            and video_language in video_correctible
        )
    )
    if not video_ok:
        raise ProcessingError("La pista de vídeo no puede etiquetarse como española.")

    candidates = list(direct)
    if not candidates and audio_rules["aceptar_indeterminado_si_video_es"]:
        conditional = {
            str(value).casefold()
            for value in audio_rules["idiomas_condicionales_si_video_es"]
        }
        candidates = [
            stream
            for stream in streams
            if stream.get("codec_type") == "audio"
            and int(stream.get("channels") or 0) > 0
            and (_language(stream) == "" or _language(stream) in conditional)
        ]
    if not candidates:
        raise ProcessingError("No hay audio español válido.")

    codec_priority = audio_rules["codec_prioridad"]

    def audio_score(stream: dict[str, Any]) -> tuple[int, int, int, int]:
        channels = int(stream.get("channels") or 0)
        codec = str(stream.get("codec_name") or "").casefold()
        try:
            bitrate = int(stream.get("bit_rate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        is_ready_ac3 = int(codec == "ac3" and channels >= int(audio_rules["canales_convertir_ac3_desde"]))
        return (
            is_ready_ac3,
            channels,
            int(codec_priority.get(codec, 100)),
            bitrate,
        )

    audio = max(candidates, key=audio_score)
    channels = int(audio.get("channels") or 0)
    codec = str(audio.get("codec_name") or "").casefold()
    threshold = int(audio_rules["canales_convertir_ac3_desde"])
    audio_mode = "convert_ac3_5_1" if channels >= threshold and codec != "ac3" else "copy"
    return video, audio, audio_mode


def _subtitle_cues_from_text(value: str) -> int:
    count = len(SRT_TIMING_RE.findall(value or ""))
    return count if count else (value or "").count("-->")


def _stream_cues(source: Path, index: int, runner: Runner) -> int | None:
    try:
        result = _run_checked(
            runner,
            [
                "ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
                "-map", f"0:{index}", "-f", "srt", "-",
            ],
            timeout=900,
            label="conteo de subtítulo",
        )
    except ProcessingError:
        return None
    cues = _subtitle_cues_from_text(result.stdout or "")
    return cues if cues > 0 else None


def _sidecar_language_allowed(source: Path, sidecar: Path) -> bool:
    video_stem = source.stem.casefold()
    sidecar_stem = sidecar.stem.casefold()
    if sidecar_stem == video_stem:
        return True
    if not sidecar_stem.startswith(video_stem + "."):
        return False
    suffix = sidecar_stem[len(video_stem) + 1 :]
    tokens = tuple(token for token in suffix.split(".") if token)
    if not tokens:
        return False
    language = tokens[0]
    qualifiers = {"forced", "forzado", "forzados"}
    if language in {"es", "spa", "es-es"}:
        return set(tokens[1:]) <= qualifiers
    return set(tokens) <= qualifiers


def _external_subtitles(source: Path, frozen: Sequence[Path]) -> list[Path]:
    return sorted(
        (path for path in frozen if _sidecar_language_allowed(source, path)),
        key=lambda path: path.name.casefold(),
    )


def _external_cues(path: Path) -> int | None:
    if path.suffix.casefold() != ".srt":
        return None
    try:
        cues = _subtitle_cues_from_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return cues if cues > 0 else None


def _select_subtitle(
    source: Path,
    probe: dict[str, Any],
    rules: dict[str, Any],
    runner: Runner,
    external_subtitles: Sequence[Path],
) -> tuple[SubtitleChoice | None, dict[str, Any] | None]:
    subtitle_rules = rules["subtitulos"]
    accepted = {str(value).casefold() for value in subtitle_rules["idiomas_aceptados"]}
    text_codecs = {
        str(value).casefold() for value in subtitle_rules["formatos_texto_aceptados"]
    }
    image_codecs = {
        str(value).casefold()
        for value in subtitle_rules["formatos_imagen_no_aceptados"]
    }
    minimum = int(subtitle_rules["frases_descartar_hasta"])
    maximum = int(subtitle_rules["frases_maximo_unico_forzado"])
    delay = subtitle_rules["delay_audio"]
    delay_title = _text(delay["texto_titulo"])
    text_choices: list[tuple[tuple[int, int, int], SubtitleChoice]] = []
    image_candidates: list[dict[str, Any]] = []

    for stream in probe.get("streams") or []:
        if stream.get("codec_type") != "subtitle" or not _is_spanish(stream, accepted):
            continue
        codec = str(stream.get("codec_name") or "").casefold()
        if codec in image_codecs:
            image_candidates.append(stream)
            continue
        if codec not in text_codecs:
            raise ReviewRequiredError(
                f"Codec de subtítulo desconocido para automatización: {codec or '-'}"
            )
        index = int(stream["index"])
        cues = _stream_cues(source, index, runner)
        if cues is None or cues <= minimum:
            continue
        title = str((stream.get("tags") or {}).get("title") or "")
        is_delay = bool(delay["activo"] and delay_title and delay_title in _text(title))
        if is_delay and cues > int(delay["frases_maximo"]):
            is_delay = False
        forced = int((stream.get("disposition") or {}).get("forced", 0) or 0) == 1
        if cues <= maximum or subtitle_rules["unico_es_modo"] == "aceptar_siempre" or is_delay:
            choice = SubtitleChoice(
                mode="internal_delay" if is_delay else "internal_text",
                stream_index=index,
                codec=codec,
                cues=cues,
                title=title,
            )
            text_choices.append(((0 if is_delay else 1, 0 if forced else 1, cues), choice))

    for external in _external_subtitles(source, external_subtitles):
        name = _text(external.name)
        cues = _external_cues(external)
        if cues is not None and cues <= minimum:
            continue
        forced = any(marker in name for marker in ("forced", "forzado", "forzados"))
        score_cues = cues if cues is not None else maximum
        if cues is None or cues <= maximum or subtitle_rules["unico_es_modo"] == "aceptar_siempre":
            text_choices.append(
                (
                    (1, 0 if forced else 1, score_cues),
                    SubtitleChoice(
                        mode="external_text",
                        external_path=external,
                        codec=external.suffix.casefold().lstrip("."),
                        cues=cues,
                        title=external.name,
                    ),
                )
            )

    if text_choices:
        return min(text_choices, key=lambda item: item[0])[1], None
    if image_candidates:
        return None, image_candidates[0]
    if str(subtitle_rules["sin_subtitulos_modo"]).strip().casefold() == "procesar_sin_subtitulos":
        return None, None
    raise ProcessingError("No hay subtítulo español procesable.")


def _safe_output(job_root: Path, target_relpath: str) -> Path:
    relative = validate_relative_path(target_relpath)
    path = job_root / "series_work" / "processed" / Path(*PurePosixPath(relative).parts)
    path = path.with_suffix(".mkv")
    resolved_job = job_root.resolve()
    resolved = path.resolve(strict=False)
    if resolved != resolved_job and not resolved.is_relative_to(resolved_job):
        raise ProcessingError("La salida provisional escapa de job_root.")
    return path


def _srt_valid(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 20 and _subtitle_cues_from_text(
            path.read_text(encoding="utf-8", errors="replace")
        ) > 0
    except OSError:
        return False


def _ocr_subtitle(
    source: Path,
    stream: dict[str, Any],
    workspace: Path,
    runner: Runner,
) -> Path:
    codec = str(stream.get("codec_name") or "").casefold()
    index = int(stream["index"])
    workspace.mkdir(parents=True, exist_ok=True)
    output = workspace / "subtitle_ocr.srt"
    output.unlink(missing_ok=True)

    if codec == "dvd_subtitle":
        require_tools(runner, ("mkvextract", "vobsubocr", "seconv"))
        idx = workspace / "vobsub.idx"
        _run_checked(
            runner,
            ["mkvextract", str(source), "tracks", f"{index}:{idx}"],
            timeout=14400,
            cwd=workspace,
            label="extracción VobSub",
        )
        raw = workspace / "vobsub_ocr.srt"
        _run_checked(
            runner,
            ["vobsubocr", "--lang", "spa", "--output", str(raw), str(idx)],
            timeout=14400,
            cwd=workspace,
            label="OCR VobSub",
        )
        _run_checked(
            runner,
            [
                "seconv", raw.name, "subrip", f"--output-filename:{output.name}",
                "--overwrite", "--fix-common-errors", "--quiet",
            ],
            timeout=14400,
            cwd=workspace,
            label="normalización OCR",
        )
    else:
        require_tools(runner, ("seconv", "tesseract"))
        _run_checked(
            runner,
            [
                "seconv", str(source), "subrip", f"--track-number:{index + 1}",
                f"--output-folder:{workspace}", f"--output-filename:{output.name}",
                "--ocr-engine:tesseract", "--ocr-language:spa", "--remove-text-for-hi",
                "--overwrite", "--quiet",
            ],
            timeout=14400,
            cwd=workspace,
            label="OCR de subtítulo",
        )
    if not _srt_valid(output):
        raise ProcessingError("OCR no produjo un SRT válido.")
    return output


def analyze_episode(
    source: Path | str,
    output: Path | str,
    rules_snapshot: RulesSnapshot,
    runner: Runner | None = None,
    *,
    ocr_workspace: Path | None = None,
    external_subtitles: Sequence[Path] = (),
) -> EpisodePlan:
    """Analiza una entrada usando exclusivamente el snapshot recibido."""

    if not isinstance(rules_snapshot, RulesSnapshot):
        raise ProcessingError("rules_snapshot explícito es obligatorio.")
    active = runner or SubprocessRunner()
    require_tools(active, BASE_TOOLS)
    source_path = Path(source)
    if source_path.suffix.casefold() not in SUPPORTED_INPUTS:
        raise ProcessingError(f"Formato de entrada no soportado: {source_path.suffix}")
    if source_path.is_symlink() or not source_path.is_file():
        raise ProcessingError("La entrada no es un archivo físico regular.")
    probe = _probe(source_path, active)
    video, audio, audio_mode = _select_video_and_audio(probe, rules_snapshot.rules)
    subtitle, image_stream = _select_subtitle(
        source_path, probe, rules_snapshot.rules, active, external_subtitles
    )
    if image_stream is not None:
        workspace = ocr_workspace or Path(output).parent / ".ocr"
        external = _ocr_subtitle(source_path, image_stream, workspace, active)
        cues = _external_cues(external)
        subtitle_rules = rules_snapshot.rules["subtitulos"]
        minimum = int(subtitle_rules["frases_descartar_hasta"])
        maximum = int(subtitle_rules["frases_maximo_unico_forzado"])
        accepted = cues is not None and minimum < cues <= maximum
        if not accepted and subtitle_rules["unico_es_modo"] == "aceptar_siempre":
            accepted = cues is not None and cues > minimum
        if accepted:
            subtitle = SubtitleChoice(
                mode="ocr",
                external_path=external,
                codec="srt",
                cues=cues,
                title=subtitle_rules["titulo_final"],
            )
        elif str(subtitle_rules["sin_subtitulos_modo"]).casefold() == "procesar_sin_subtitulos":
            subtitle = None
        else:
            raise ReviewRequiredError("El SRT OCR queda fuera de los límites de frases.")
    return EpisodePlan(
        source=source_path,
        output=Path(output),
        video=video,
        audio=audio,
        subtitle=subtitle,
        audio_mode=audio_mode,
        rules_fingerprint=rules_snapshot.fingerprint,
        duration=_duration(probe),
    )


def _content_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_sidecars(
    source_root: Path,
    sidecars: Sequence[ManifestSidecar],
) -> tuple[Path, ...]:
    result: list[Path] = []
    for sidecar in sidecars:
        relative = validate_relative_path(sidecar.source_relpath)
        path = source_root / Path(*PurePosixPath(relative).parts)
        if path.is_symlink():
            raise ProcessingError("Un sidecar congelado se convirtió en symlink.")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(source_root) or not resolved.is_file():
            raise ProcessingError("Sidecar congelado fuera de source_root.")
        info = resolved.stat()
        if info.st_size != sidecar.size or info.st_mtime_ns != sidecar.mtime_ns:
            raise ProcessingError("Un sidecar cambió después de congelar el manifiesto.")
        if _content_sha256(resolved) != sidecar.content_sha256:
            raise ProcessingError("El hash de un sidecar ya no coincide con el manifiesto.")
        result.append(resolved)
    return tuple(result)


def _audio_title(audio: dict[str, Any], rules: dict[str, Any], mode: str) -> str:
    audio_rules = rules["audio"]
    if mode == "convert_ac3_5_1" or (
        str(audio.get("codec_name") or "").casefold() == "ac3"
        and int(audio.get("channels") or 0)
        >= int(audio_rules["canales_convertir_ac3_desde"])
    ):
        return str(audio_rules["titulo_ac3_convertido"])
    codec = str(audio.get("codec_name") or "").casefold()
    codec_title = str(audio_rules["titulos_codec"].get(codec, codec.upper() or "Audio"))
    channels = int(audio.get("channels") or 0)
    channel_title = "5.1" if channels >= 6 else ("1.0" if channels == 1 else f"{channels}.0")
    return f"{codec_title} {channel_title}".strip()


def _chapter_time(seconds: float) -> str:
    nanoseconds = max(0, int(round(seconds * 1_000_000_000)))
    total_seconds, nanos = divmod(nanoseconds, 1_000_000_000)
    minutes_total, second = divmod(total_seconds, 60)
    hours, minute = divmod(minutes_total, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}.{nanos:09d}"


def _write_chapters(path: Path, duration: float, interval: int) -> int:
    if duration <= 0 or interval <= 0:
        return 0
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<Chapters>",
        "  <EditionEntry>",
    ]
    start = 0.0
    number = 1
    while start < duration:
        end = min(start + interval, duration)
        lines.extend(
            [
                "    <ChapterAtom>",
                f"      <ChapterTimeStart>{_chapter_time(start)}</ChapterTimeStart>",
                f"      <ChapterTimeEnd>{_chapter_time(end)}</ChapterTimeEnd>",
                "      <ChapterDisplay>",
                f"        <ChapterString>{html.escape(f'Capítulo {number:02d}')}</ChapterString>",
                "        <ChapterLanguage>spa</ChapterLanguage>",
                "      </ChapterDisplay>",
                "    </ChapterAtom>",
            ]
        )
        number += 1
        start += interval
    lines.extend(["  </EditionEntry>", "</Chapters>"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return number - 1


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_parent(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path.parent, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ffmpeg_command(plan: EpisodePlan, temporary: Path, rules: dict[str, Any]) -> list[str]:
    subtitle = plan.subtitle
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-fflags", "+genpts", "-i", str(plan.source),
    ]
    external_input = subtitle is not None and subtitle.external_path is not None
    if external_input:
        command += ["-i", str(subtitle.external_path)]
    command += [
        "-map", f"0:{int(plan.video['index'])}",
        "-map", f"0:{int(plan.audio['index'])}",
    ]
    if subtitle is not None:
        command += [
            "-map",
            "1:0" if external_input else f"0:{int(subtitle.stream_index)}",
        ]
    if rules["limpieza"]["borrar_metadata_original"]:
        command += ["-map_metadata", "-1"]
    if rules["limpieza"]["crear_capitulos"]:
        command += ["-map_chapters", "-1"]
    command += ["-c:v", "copy"]
    if plan.audio_mode == "convert_ac3_5_1":
        command += [
            "-c:a", "ac3", "-b:a", str(rules["audio"]["bitrate_ac3"]), "-ac", "6"
        ]
    else:
        command += ["-c:a", "copy"]
    if subtitle is not None:
        command += ["-c:s", "srt"]
    command += [
        "-metadata:s:v:0", f"language={rules['video']['idioma_final']}",
        "-metadata:s:a:0", f"language={rules['audio']['idioma_final_condicional']}",
        "-metadata:s:a:0", f"title={_audio_title(plan.audio, rules, plan.audio_mode)}",
        "-disposition:v:0",
        _disposition(rules["video"]["marcar_default"], rules["video"]["marcar_forzado"]),
        "-disposition:a:0",
        _disposition(rules["audio"]["marcar_default"], rules["audio"]["marcar_forzado"]),
    ]
    if subtitle is not None:
        command += [
            "-metadata:s:s:0", "language=es",
            "-metadata:s:s:0", f"title={rules['subtitulos']['titulo_final']}",
            "-disposition:s:0",
            _disposition(
                rules["subtitulos"]["interno_default"],
                rules["subtitulos"]["interno_forzado"],
            ),
        ]
    command.append(str(temporary))
    return command


def _verify_output(
    path: Path,
    plan: EpisodePlan,
    runner: Runner,
) -> dict[str, Any]:
    if path.suffix.casefold() != ".mkv" or not path.is_file() or path.stat().st_size <= 0:
        raise VerificationError("La salida MKV provisional no existe o está vacía.")
    probe = _probe(path, runner, count_packets=True)
    format_name = str((probe.get("format") or {}).get("format_name") or "").casefold()
    if "matroska" not in format_name:
        raise VerificationError("ffprobe no reconoce la salida como Matroska.")
    streams = probe.get("streams") or []
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    if len(videos) != 1 or len(audios) != 1:
        raise VerificationError("La salida no tiene exactamente un vídeo y un audio.")
    if len(subtitles) != (1 if plan.subtitle is not None else 0):
        raise VerificationError("La salida no conserva la decisión de subtítulos.")
    if _language(videos[0]) not in {"es", "spa"}:
        raise VerificationError("La pista de vídeo final no está etiquetada en español.")
    if _language(audios[0]) not in {"es", "spa"}:
        raise VerificationError("La pista de audio final no está etiquetada en español.")
    if subtitles and _language(subtitles[0]) not in {"es", "spa"}:
        raise VerificationError("El subtítulo final no está etiquetado en español.")
    output_duration = _duration(probe)
    if output_duration <= 0:
        raise VerificationError("La salida no tiene duración válida.")
    tolerance = max(2.0, plan.duration * 0.01)
    if plan.duration > 0 and abs(output_duration - plan.duration) > tolerance:
        raise VerificationError("La duración de salida no coincide con la entrada.")

    mkvmerge = _run_checked(
        runner, ["mkvmerge", "-J", str(path)], timeout=900, label="mkvmerge -J"
    )
    try:
        mkv = json.loads(mkvmerge.stdout or "{}")
    except json.JSONDecodeError as error:
        raise VerificationError("mkvmerge devolvió JSON inválido.") from error
    tracks = mkv.get("tracks") if isinstance(mkv, dict) else None
    if not isinstance(tracks, list):
        raise VerificationError("mkvmerge no pudo enumerar las pistas.")
    if sum(track.get("type") == "video" for track in tracks) != 1:
        raise VerificationError("mkvmerge no confirma una única pista de vídeo.")
    if sum(track.get("type") == "audio" for track in tracks) != 1:
        raise VerificationError("mkvmerge no confirma una única pista de audio.")
    if sum(track.get("type") == "subtitles" for track in tracks) != len(subtitles):
        raise VerificationError("mkvmerge no confirma los subtítulos esperados.")

    _run_checked(
        runner,
        [
            "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
            "-map", "0", "-c", "copy", "-f", "null", "-",
        ],
        timeout=14400,
        label="lectura completa FFmpeg",
    )
    return {
        "ffprobe": "ok",
        "mkvmerge": "ok",
        "full_read": "ok",
        "duration": output_duration,
        "tracks": {
            "video": len(videos),
            "audio": len(audios),
            "subtitles": len(subtitles),
        },
    }


def _export_subtitle(
    plan: EpisodePlan,
    output: Path,
    rules: dict[str, Any],
    runner: Runner,
) -> Path | None:
    if plan.subtitle is None or not rules["limpieza"]["exportar_srt_externo"]:
        return None
    suffix = str(rules["subtitulos"]["sufijo_srt_externo"])
    destination = output.with_name(f"{output.stem}{suffix}")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp.srt")
    try:
        if plan.subtitle.external_path is not None and plan.subtitle.external_path.suffix.casefold() == ".srt":
            shutil.copyfile(plan.subtitle.external_path, temporary)
        elif plan.subtitle.external_path is not None:
            _run_checked(
                runner,
                ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(plan.subtitle.external_path), str(temporary)],
                timeout=900,
                label="exportación de subtítulo externo",
            )
        else:
            _run_checked(
                runner,
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(plan.source),
                    "-map", f"0:{int(plan.subtitle.stream_index)}", "-c:s", "srt", str(temporary),
                ],
                timeout=900,
                label="exportación de subtítulo interno",
            )
        if not _srt_valid(temporary):
            raise ProcessingError("El subtítulo externo provisional no es válido.")
        _fsync_file(temporary)
        os.replace(temporary, destination)
        _fsync_parent(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def _execute_plan(
    plan: EpisodePlan,
    rules_snapshot: RulesSnapshot,
    runner: Runner,
) -> dict[str, Any]:
    output = plan.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.processing.mkv")
    chapter_file = output.with_name(f".{output.name}.{uuid.uuid4().hex}.chapters.xml")
    try:
        _run_checked(
            runner,
            _ffmpeg_command(plan, temporary, rules_snapshot.rules),
            timeout=14400,
            label="remux FFmpeg",
        )
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise ProcessingError("FFmpeg no produjo un MKV provisional.")
        if rules_snapshot.rules["limpieza"]["limpiar_tags_mkv"]:
            _run_checked(
                runner,
                ["mkvpropedit", str(temporary), "--tags", "all:"],
                timeout=900,
                label="limpieza de tags MKV",
            )
        chapters = 0
        if rules_snapshot.rules["limpieza"]["crear_capitulos"]:
            chapters = _write_chapters(
                chapter_file,
                plan.duration,
                int(rules_snapshot.rules["limpieza"]["capitulo_cada_segundos"]),
            )
            if chapters:
                _run_checked(
                    runner,
                    ["mkvpropedit", str(temporary), "--chapters", str(chapter_file)],
                    timeout=900,
                    label="capítulos MKV",
                )
        verification = _verify_output(temporary, plan, runner)
        verification["chapters"] = chapters
        _export_subtitle(plan, output, rules_snapshot.rules, runner)
        _fsync_file(temporary)
        os.replace(temporary, output)
        _fsync_parent(output)
        return verification
    finally:
        temporary.unlink(missing_ok=True)
        chapter_file.unlink(missing_ok=True)


class SeriesProcessor:
    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner or SubprocessRunner()

    def process(
        self,
        *,
        manifest: SeriesManifest,
        source_root: Path | str,
        job_root: Path | str,
        rules_snapshot: RulesSnapshot,
    ) -> ProcessingResult:
        if not isinstance(rules_snapshot, RulesSnapshot):
            raise ProcessingError("rules_snapshot explícito es obligatorio.")
        if not manifest.ready:
            raise ProcessingError("Solo se procesa un manifiesto ready de una única serie.")
        require_tools(self.runner, BASE_TOOLS)
        lexical_source = Path(source_root)
        lexical_job = Path(job_root)
        if lexical_source.is_symlink() or lexical_job.is_symlink():
            raise ProcessingError("job_root y source_root deben ser carpetas físicas.")
        source = lexical_source.resolve(strict=True)
        job = lexical_job.resolve(strict=True)
        if not source.is_dir():
            raise ProcessingError("source_root no es una carpeta física válida.")
        expected_source = job / "series_filebot_output"
        if source != expected_source.resolve(strict=False):
            raise ProcessingError("source_root debe ser <job_root>/series_filebot_output.")

        completed: list[ProcessedEpisode] = []
        for entry in manifest.entries:
            try:
                source_relative = validate_relative_path(entry.source_relpath)
                input_path = source / Path(*PurePosixPath(source_relative).parts)
                if input_path.is_symlink():
                    raise ProcessingError("La entrada del manifiesto es un enlace simbólico.")
                resolved_input = input_path.resolve(strict=True)
                if not resolved_input.is_file():
                    raise ProcessingError("La entrada del manifiesto no es un archivo regular.")
                if not resolved_input.is_relative_to(source):
                    raise ProcessingError("La entrada del manifiesto escapa de source_root.")
                stat = resolved_input.stat()
                if stat.st_size != entry.size or stat.st_mtime_ns != entry.mtime_ns:
                    raise ProcessingError("La entrada cambió después de congelar el manifiesto.")
                if _content_sha256(resolved_input) != entry.content_sha256:
                    raise ProcessingError(
                        "El hash de la entrada ya no coincide con el manifiesto."
                    )
                output = _safe_output(job, entry.target_relpath)
                ocr_workspace = (
                    job
                    / "series_work"
                    / "ocr"
                    / entry.source_fingerprint[:16]
                )
                frozen_sidecars = _validated_sidecars(
                    source,
                    entry.subtitle_sidecars,
                )
                plan = analyze_episode(
                    resolved_input,
                    output,
                    rules_snapshot,
                    self.runner,
                    ocr_workspace=ocr_workspace,
                    external_subtitles=frozen_sidecars,
                )
                verification = _execute_plan(plan, rules_snapshot, self.runner)
                completed.append(
                    ProcessedEpisode(
                        source_relpath=entry.source_relpath,
                        target_relpath=entry.target_relpath,
                        provisional_relpath=output.relative_to(job).as_posix(),
                        output_size=output.stat().st_size,
                        audio_mode=plan.audio_mode,
                        subtitle_mode=plan.subtitle.mode if plan.subtitle else "none",
                        verification=verification,
                    )
                )
            except Exception as error:
                if isinstance(error, EpisodeProcessingError):
                    raise
                raise EpisodeProcessingError(
                    f"Falló {entry.source_relpath}: {error}",
                    entry=entry,
                    partial_results=completed,
                ) from error
        return ProcessingResult(
            status="verified",
            manifest_digest=manifest.digest,
            rules_fingerprint=rules_snapshot.fingerprint,
            episodes=tuple(completed),
        )


def process_manifest(
    manifest: SeriesManifest,
    source_root: Path | str,
    job_root: Path | str,
    rules_snapshot: RulesSnapshot,
    runner: Runner | None = None,
) -> ProcessingResult:
    return SeriesProcessor(runner).process(
        manifest=manifest,
        source_root=source_root,
        job_root=job_root,
        rules_snapshot=rules_snapshot,
    )


__all__ = [
    "BASE_TOOLS",
    "OCR_TOOLS",
    "EpisodePlan",
    "EpisodeProcessingError",
    "ProcessedEpisode",
    "ProcessingError",
    "ProcessingResult",
    "ReviewRequiredError",
    "SeriesProcessor",
    "SubprocessRunner",
    "ToolUnavailableError",
    "VerificationError",
    "analyze_episode",
    "missing_tools",
    "unavailable_tools",
    "probe_media",
    "process_manifest",
    "require_tools",
]
