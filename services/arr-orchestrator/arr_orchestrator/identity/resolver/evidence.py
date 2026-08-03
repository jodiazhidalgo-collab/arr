"""Recogida de evidencias locales y selección del mejor GuessIt."""

from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from guessit import guessit

from ...filesystem import media_files
from ...name_parser import parse_release_name
from .text import clean_release_name, prefer_parser_title, unique
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
    expected = "movie" if media_type == "movie" else "episode"
    guesses: List[Tuple[int, Dict[str, object]]] = []
    settings = (
        policy.get("guess_selection")
        if isinstance(policy.get("guess_selection"), dict)
        else {}
    )
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
        parsed["_title_candidates"] = ordered_title_candidates(
            parsed_name.title_candidates,
            title,
            series_titles,
        )
        parsed["_title_evidence"] = [
            item.to_dict()
            for item in ordered_title_evidence(
                parsed_name.title_evidence,
                title,
                series_titles,
            )
        ]
        parsed["_display_title"] = parsed_name.display_title
        parsed["_guessit_input"] = cleaned
        quality = int(settings.get("base", 100)) - index * int(settings.get("index_penalty", 1))
        if parsed.get("year"):
            quality += int(settings.get("year_bonus", 20))
        if media_type == "tv" and parsed.get("season") is not None:
            quality += int(settings.get("season_bonus", 15))
        if parsed_name.confidence == "high":
            quality += int(settings.get("parser_high_bonus", 10))
        guesses.append((quality, parsed))
    return max(guesses, key=lambda item: item[0])[1] if guesses else {}
