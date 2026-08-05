"""Recogida de evidencias locales y selección del mejor GuessIt."""

import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Sequence

from guessit import guessit

from ...filesystem import media_files
from ...name_parser import parse_release_name
from .text import clean_release_name, prefer_parser_title, unique
from .models import EpisodeIntent
from .title_candidates import (
    ordered_title_candidates,
    ordered_title_evidence,
    series_title_candidates,
)


TECHNICAL_NAMES = {"original", "filebot_input", "filebot_output", "extracted"}


def collect_name_evidence(
    value: str, category: str, policy: Dict[str, object]
) -> List[str]:
    """Construye evidencia solo desde texto, sin consultar el sistema de archivos."""

    text = str(value or "").strip()
    if not text:
        return []
    parsed = parse_release_name(text, category, rules=policy.get("parser"))
    return unique(
        item.strip()
        for item in [
            text,
            parsed.cleaned,
            parsed.display_title,
            parsed.guessit_input,
            *parsed.title_candidates,
        ]
        if item.strip()
    )


def collect_evidence(
    job: Dict[str, object], input_root: Path, policy: Dict[str, object]
) -> List[str]:
    values: List[str] = []
    settings = policy.get("evidence") if isinstance(policy.get("evidence"), dict) else {}

    def add_name(value: str) -> None:
        values.extend(
            collect_name_evidence(
                value,
                str(job.get("category") or ""),
                policy,
            )
        )

    if bool(settings.get("use_job_name", True)):
        add_name(str(job.get("name") or ""))
    if bool(settings.get("use_folder_name", True)) and input_root.name.lower() not in TECHNICAL_NAMES:
        add_name(input_root.name)
    if bool(settings.get("use_media_files", True)):
        files = media_files(input_root)
        if bool(settings.get("sort_largest_first", True)):
            files.sort(
                key=lambda path: path.stat().st_size if path.exists() else 0,
                reverse=True,
            )
        maximum = max(0, int(settings.get("max_media_files", 20)))
        for path in files[:maximum]:
            add_name(path.stem)
    return unique(value.strip() for value in values if value.strip())


def best_guess(
    evidence: Sequence[str], media_type: str, policy: Dict[str, object]
) -> Dict[str, object]:
    """Elige un parse base por orden explicito y conserva toda la evidencia.

    No hay pesos ni suma de puntos: para TV se prefiere una fuente con marcador
    episodico y despues se respeta el orden estable de las fuentes. La confianza
    estructural solo desempata una misma posicion. Los anos se aplican cuando todas las fuentes
    que aportan uno coinciden.
    """

    expected = "movie" if media_type == "movie" else "episode"
    guesses: List[tuple[tuple[object, ...], Dict[str, object]]] = []
    all_candidates: List[str] = []
    all_title_evidence: List[Dict[str, object]] = []
    explicit_years: set[int] = set()
    series_titles = (
        series_title_candidates(evidence, policy) if media_type == "tv" else []
    )
    for index, value in enumerate(evidence):
        parsed_name = parse_release_name(
            value,
            "movies" if media_type == "movie" else "tv",
            rules=policy.get("parser"),
        )
        cleaned = parsed_name.guessit_input or parsed_name.cleaned or clean_release_name(value)
        parsed = dict(guessit(cleaned, {"type": expected}))
        title = str(parsed.get("title") or "").strip()
        if not title and parsed_name.display_title:
            parsed["title"] = parsed_name.display_title
            title = parsed_name.display_title
        elif prefer_parser_title(parsed_name.display_title, title):
            parsed["title"] = parsed_name.display_title
            title = parsed_name.display_title
        if not title:
            continue
        if parsed_name.year and not parsed.get("year"):
            parsed["year"] = parsed_name.year
        if media_type == "tv":
            if parsed_name.season is not None and parsed.get("season") is None:
                parsed["season"] = parsed_name.season
            if parsed_name.episodes and not parsed.get("episode"):
                parsed["episode"] = parsed_name.episodes
            if parsed_name.absolute_episode is not None:
                parsed["absolute_episode"] = parsed_name.absolute_episode
        local_candidates = ordered_title_candidates(
            parsed_name.title_candidates,
            title,
        )
        parsed["_title_candidates"] = local_candidates
        local_evidence = []
        for item in ordered_title_evidence(parsed_name.title_evidence, title):
            serialized = item.to_dict()
            original_group = str(serialized.get("group_id") or "parser:0")
            serialized["group_id"] = f"source:{index}:{original_group}"[:48]
            local_evidence.append(serialized)
        parsed["_title_evidence"] = local_evidence
        parsed["_display_title"] = parsed_name.display_title
        parsed["_guessit_input"] = cleaned
        if isinstance(parsed.get("year"), int):
            explicit_years.add(int(parsed["year"]))
        all_candidates.extend(local_candidates)
        all_title_evidence.extend(local_evidence)
        guesses.append(
            (
                _guess_order_key(
                    parsed,
                    media_type,
                    str(parsed_name.confidence or ""),
                    index,
                ),
                parsed,
            )
        )
    if not guesses:
        return {}
    selected = dict(min(guesses, key=lambda item: item[0])[1])
    if len(explicit_years) == 1:
        selected["year"] = next(iter(explicit_years))
    elif len(explicit_years) > 1:
        selected.pop("year", None)
        selected["_year_conflict_in_sources"] = sorted(explicit_years)
    if series_titles:
        all_candidates.extend(series_titles)
        all_title_evidence.extend(
            item.to_dict()
            for item in ordered_title_evidence([], "", series_titles)
        )
    selected["_title_candidates"] = unique(all_candidates)
    selected["_title_evidence"] = all_title_evidence
    return selected


def _guess_order_key(
    parsed: Dict[str, object],
    media_type: str,
    confidence: str,
    index: int,
) -> tuple[object, ...]:
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    has_episode_intent = bool(
        parsed.get("season") is not None
        or parsed.get("episode")
        or parsed.get("absolute_episode") is not None
    )
    return (
        media_type == "tv" and not has_episode_intent,
        index,
        confidence_order.get(confidence, 3),
    )


def probe_media_runtimes(
    input_root: Path,
    policy: Dict[str, object],
    runner: Callable[..., object] | None = None,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> List[Dict[str, object]]:
    """Mide duracion real con ffprobe; un fallo deja la familia UNKNOWN."""

    settings = policy.get("evidence") if isinstance(policy.get("evidence"), dict) else {}
    if not bool(settings.get("use_media_files", True)) or not input_root.exists():
        return []
    files = media_files(input_root)
    if bool(settings.get("sort_largest_first", True)):
        files.sort(
            key=lambda path: path.stat().st_size if path.exists() else 0,
            reverse=True,
        )
    maximum = max(0, int(settings.get("max_media_files", 20)))
    execute = runner or subprocess.run
    result: List[Dict[str, object]] = []
    for path in files[:maximum]:
        remaining = None if deadline is None else deadline - clock()
        if remaining is not None and remaining <= 0:
            break
        try:
            completed = execute(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=min(5.0, remaining) if remaining is not None else 5,
                check=False,
            )
            if int(getattr(completed, "returncode", 1)) != 0:
                continue
            seconds = float(str(getattr(completed, "stdout", "") or "").strip())
        except (OSError, TypeError, ValueError, subprocess.SubprocessError):
            continue
        if seconds <= 0:
            continue
        result.append(
            {
                "source": path.name,
                "runtime_minutes": round(seconds / 60.0, 2),
            }
        )
    return result


def collect_episode_intents(
    evidence: Sequence[str],
    policy: Dict[str, object],
    runtime_evidence: Sequence[Dict[str, object]] = (),
) -> List[Dict[str, object]]:
    """Conserva todas las intenciones TV compatibles, una por fuente real."""

    runtimes: Dict[str, object] = {}
    for item in runtime_evidence:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        if source:
            runtimes[source.casefold()] = item.get("runtime_minutes")
            runtimes[Path(source).stem.casefold()] = item.get("runtime_minutes")
    result: List[EpisodeIntent] = []
    seen: set[tuple[object, ...]] = set()
    parser_rules = policy.get("parser") if isinstance(policy.get("parser"), dict) else None
    for value in evidence:
        parsed = parse_release_name(str(value), "tv", rules=parser_rules)
        season = parsed.season if parsed.season is not None else parsed.season_pack
        episodes = [int(item) for item in parsed.episodes]
        if season is None and not episodes and parsed.absolute_episode is None:
            continue
        key = (
            season,
            tuple(episodes),
            parsed.absolute_episode,
            parsed.season_pack is not None,
        )
        if key in seen:
            continue
        seen.add(key)
        value_text = str(value).strip()
        raw_runtime = runtimes.get(value_text.casefold())
        if raw_runtime is None:
            raw_runtime = runtimes.get(Path(value_text).stem.casefold())
        runtime = None
        try:
            runtime = int(round(float(raw_runtime))) if raw_runtime else None
        except (TypeError, ValueError):
            runtime = None
        result.append(
            EpisodeIntent(
                source=str(value)[:160],
                season=season,
                episodes=episodes,
                absolute_episode=parsed.absolute_episode,
                is_season_pack=parsed.season_pack is not None,
                is_special=season == 0,
                runtime_minutes=runtime,
            )
        )
    return [item.to_dict() for item in result]


def collect_file_episode_intents(
    input_root: Path,
    policy: Dict[str, object],
    runtime_evidence: Sequence[Dict[str, object]] = (),
) -> List[Dict[str, object]]:
    """Una intencion por archivo fisico, ligada a su basename completo."""

    settings = policy.get("evidence") if isinstance(policy.get("evidence"), dict) else {}
    if not bool(settings.get("use_media_files", True)) or not input_root.exists():
        return []
    files = media_files(input_root)
    if bool(settings.get("sort_largest_first", True)):
        files.sort(
            key=lambda path: path.stat().st_size if path.exists() else 0,
            reverse=True,
        )
    runtimes: Dict[str, object] = {}
    for item in runtime_evidence:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        if source:
            runtimes[source.casefold()] = item.get("runtime_minutes")
            runtimes[Path(source).stem.casefold()] = item.get("runtime_minutes")
    parser_rules = policy.get("parser") if isinstance(policy.get("parser"), dict) else None
    result: List[Dict[str, object]] = []
    # En TV cada archivo fisico puede aportar una temporada o episodio que no
    # aparece en el nombre del pack. El limite de evidencia textual/runtime no
    # puede recortar la validacion episodica del manifiesto.
    for path in files:
        parsed = parse_release_name(path.stem, "tv", rules=parser_rules)
        season = parsed.season if parsed.season is not None else parsed.season_pack
        episodes = [int(item) for item in parsed.episodes]
        if season is None and not episodes and parsed.absolute_episode is None:
            continue
        raw_runtime = runtimes.get(path.name.casefold())
        if raw_runtime is None:
            raw_runtime = runtimes.get(path.stem.casefold())
        try:
            runtime = int(round(float(raw_runtime))) if raw_runtime else None
        except (TypeError, ValueError):
            runtime = None
        result.append(
            EpisodeIntent(
                source=path.name,
                season=season,
                episodes=episodes,
                absolute_episode=parsed.absolute_episode,
                is_season_pack=parsed.season_pack is not None,
                is_special=season == 0,
                runtime_minutes=runtime,
            ).to_dict()
        )
    return result
