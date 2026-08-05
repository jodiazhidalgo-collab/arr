"""Conversión, fusión y normalización de candidatos TMDb."""

from typing import Dict, Optional

from .models import ResolverCandidate
from .text import as_int, date_year, normalize_title, unique


_SEARCH_PHASES = frozenset({"primary", "composite", "alternate"})
_SEARCH_SOURCES = frozenset(
    {"primary", "configured", "composite", "alternate", "legacy"}
)


def merge_search_payload(
    media_type: str,
    existing: Optional[Dict[str, object]],
    incoming: Dict[str, object],
    *,
    provenance: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    title_key = "title" if media_type == "movie" else "name"
    original_key = "original_title" if media_type == "movie" else "original_name"
    date_key = "release_date" if media_type == "movie" else "first_air_date"
    merged = dict(existing or incoming)
    aliases = []
    for payload in (existing or {}, incoming):
        aliases.extend(
            [
                str(payload.get(title_key) or ""),
                str(payload.get(original_key) or ""),
                *(str(item) for item in payload.get("_search_aliases") or []),
            ]
        )
    merged["_search_aliases"] = unique(aliases)
    for key in (title_key, original_key, date_key, "number_of_seasons"):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    if not merged.get("original_language") and incoming.get("original_language"):
        merged["original_language"] = incoming["original_language"]
    phases = _safe_search_metadata(
        [
            *_metadata_items((existing or {}).get("_search_phases")),
            *_metadata_items(incoming.get("_search_phases")),
            (provenance or {}).get("phase"),
        ],
        _SEARCH_PHASES,
    )
    sources = _safe_search_metadata(
        [
            *_metadata_items((existing or {}).get("_search_sources")),
            *_metadata_items(incoming.get("_search_sources")),
            (provenance or {}).get("source"),
        ],
        _SEARCH_SOURCES,
    )
    exact_phases = _safe_search_metadata(
        [
            *_metadata_items((existing or {}).get("_search_exact_phases")),
            *_metadata_items(incoming.get("_search_exact_phases")),
        ],
        _SEARCH_PHASES,
    )
    exact_sources = _safe_search_metadata(
        [
            *_metadata_items((existing or {}).get("_search_exact_sources")),
            *_metadata_items(incoming.get("_search_exact_sources")),
        ],
        _SEARCH_SOURCES,
    )
    if _query_matches_payload(media_type, incoming, (provenance or {}).get("query")):
        exact_phases = _safe_search_metadata(
            [*exact_phases, (provenance or {}).get("phase")], _SEARCH_PHASES
        )
        exact_sources = _safe_search_metadata(
            [*exact_sources, (provenance or {}).get("source")], _SEARCH_SOURCES
        )
    for key in (
        "_search_phases",
        "_search_sources",
        "_search_exact_phases",
        "_search_exact_sources",
        "_search_hit_count",
    ):
        merged.pop(key, None)
    if phases:
        merged["_search_phases"] = phases
    if sources:
        merged["_search_sources"] = sources
    if exact_phases:
        merged["_search_exact_phases"] = exact_phases
    if exact_sources:
        merged["_search_exact_sources"] = exact_sources
    merged["_search_hit_count"] = _safe_hit_count(
        (existing or {}).get("_search_hit_count")
    ) + 1
    return merged


def _safe_search_metadata(values: object, allowed: frozenset[str]) -> list[str]:
    items = values if isinstance(values, (list, tuple, set)) else [values]
    return unique(
        text
        for value in items
        if (text := str(value or "").strip().lower()) in allowed
    )


def _metadata_items(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _query_matches_payload(
    media_type: str,
    payload: Dict[str, object],
    query: object,
) -> bool:
    normalized_query = normalize_title(str(query or ""))
    if not normalized_query:
        return False
    title_key = "title" if media_type == "movie" else "name"
    original_key = "original_title" if media_type == "movie" else "original_name"
    values = [
        payload.get(title_key),
        payload.get(original_key),
        *_metadata_items(payload.get("_search_aliases")),
    ]
    return normalized_query in {
        normalize_title(str(value or "")) for value in values if str(value or "").strip()
    }

def _safe_hit_count(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def candidate_from_payload(
    media_type: str, payload: Dict[str, object]
) -> ResolverCandidate:
    title_key = "title" if media_type == "movie" else "name"
    original_key = "original_title" if media_type == "movie" else "original_name"
    date_key = "release_date" if media_type == "movie" else "first_air_date"
    aliases = [str(payload.get(title_key) or ""), str(payload.get(original_key) or "")]
    alternatives = payload.get("alternative_titles") or {}
    alternative_items = (
        alternatives.get("titles") or alternatives.get("results") or []
        if isinstance(alternatives, dict)
        else []
    )
    aliases.extend(str(item.get("title") or "") for item in alternative_items)
    translations = (payload.get("translations") or {}).get("translations") or []
    for item in translations:
        data = item.get("data") or {}
        aliases.extend(str(data.get(key) or "") for key in ("title", "name") if data.get(key))
    aliases.extend(str(value) for value in payload.get("_search_aliases") or [])
    release_timeline = _release_timeline(payload)
    seasons = payload.get("seasons") if isinstance(payload.get("seasons"), list) else []
    season_episode_counts = {
        season_number: episode_count
        for item in seasons
        if isinstance(item, dict)
        and (season_number := as_int(item.get("season_number"))) is not None
        and (episode_count := as_int(item.get("episode_count"))) is not None
    }
    known_episodes = _known_episodes(payload.get("_season_details"))
    runtime = as_int(payload.get("runtime"))
    episode_runtimes = [
        value
        for item in payload.get("episode_run_time") or []
        if (value := as_int(item)) is not None and value > 0
    ]
    return ResolverCandidate(
        tmdb_id=int(payload["id"]),
        media_type=media_type,
        title=str(payload.get(title_key) or payload.get(original_key) or ""),
        original_title=str(payload.get(original_key) or payload.get(title_key) or ""),
        year=date_year(payload.get(date_key)),
        original_language=str(payload.get("original_language") or "").strip().lower(),
        aliases=unique(value for value in aliases if value),
        season_count=as_int(payload.get("number_of_seasons")),
        search_provenance=_search_provenance(payload),
        popularity=_safe_float(payload.get("popularity")),
        vote_count=max(0, as_int(payload.get("vote_count")) or 0),
        runtime_minutes=runtime if runtime is not None and runtime > 0 else None,
        episode_runtime_minutes=episode_runtimes,
        release_years=sorted(
            {
                int(item["year"])
                for item in release_timeline
                if isinstance(item.get("year"), int)
            }
        ),
        release_timeline=release_timeline,
        season_episode_counts=season_episode_counts,
        known_episodes=known_episodes,
    )


def _safe_float(value: object) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _release_timeline(payload: Dict[str, object]) -> list[Dict[str, object]]:
    releases = payload.get("release_dates")
    results = releases.get("results") if isinstance(releases, dict) else []
    timeline: list[Dict[str, object]] = []
    seen: set[tuple[str, str, int]] = set()
    for country in results or []:
        if not isinstance(country, dict):
            continue
        region = str(country.get("iso_3166_1") or "").upper()
        for item in country.get("release_dates") or []:
            if not isinstance(item, dict):
                continue
            date = str(item.get("release_date") or "")[:10]
            year = date_year(date)
            release_type = as_int(item.get("type")) or 0
            if year is None:
                continue
            key = (region, date, release_type)
            if key in seen:
                continue
            seen.add(key)
            timeline.append(
                {
                    "region": region,
                    "date": date,
                    "year": year,
                    "type": release_type,
                }
            )
    timeline.sort(key=lambda item: (str(item["date"]), str(item["region"]), int(item["type"])))
    return timeline[:64]


def _known_episodes(value: object) -> Dict[int, list[int]]:
    details = value if isinstance(value, dict) else {}
    result: Dict[int, list[int]] = {}
    for raw_season, payload in details.items():
        season = as_int(raw_season)
        if season is None or not isinstance(payload, dict):
            continue
        episodes = sorted(
            {
                episode
                for item in payload.get("episodes") or []
                if isinstance(item, dict)
                and (episode := as_int(item.get("episode_number"))) is not None
            }
        )
        result[season] = episodes
    return result


def _search_provenance(payload: Dict[str, object]) -> Dict[str, object]:
    """Expone solo metadatos de búsqueda acotados; nunca conserva la query."""

    return {
        "sources": _safe_search_metadata(
            _metadata_items(payload.get("_search_sources")), _SEARCH_SOURCES
        ),
        "phases": _safe_search_metadata(
            _metadata_items(payload.get("_search_phases")), _SEARCH_PHASES
        ),
        "exact_sources": _safe_search_metadata(
            _metadata_items(payload.get("_search_exact_sources")), _SEARCH_SOURCES
        ),
        "exact_phases": _safe_search_metadata(
            _metadata_items(payload.get("_search_exact_phases")), _SEARCH_PHASES
        ),
        "hits": _safe_hit_count(payload.get("_search_hit_count")),
    }
