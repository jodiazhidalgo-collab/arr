"""Lectura segura del título validado que acompaña a una descarga."""

from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Mapping

from .parser_engine import parse_release_name
from .parser_rules import resolve_parser_rules
from .parser_titles import collection_like_manual, single_year_range_covers_all_years
from .resolver.models import ResolverAmbiguous
from .resolver.text import normalize_title
from .resolver_defaults import DEFAULT_SOURCE_TITLE_FALLBACK
from ..source_context.contract import (
    SourceContextContractError,
    validate_source_title,
)
from ..source_context.policy import (
    CONTEXT_TTL_SECONDS,
    MAX_SOURCE_TITLES,
    USABLE_DELIVERY_STATES,
)


INFOHASH_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class SourceTitleContext:
    event_id: str
    source: str
    infohash: str
    destination: str
    source_title: str
    route: str
    delivery_state: str
    created_at: str

    def public(self) -> Dict[str, str]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "infohash": self.infohash,
            "destination": self.destination,
            "source_title": self.source_title,
            "route": self.route,
            "delivery_state": self.delivery_state,
            "created_at": self.created_at,
        }


def source_fallback_settings(
    rules_snapshot: object,
    category: str,
) -> Dict[str, object]:
    """Completa el bloque v1 sin mutar snapshots antiguos."""

    settings = copy.deepcopy(DEFAULT_SOURCE_TITLE_FALLBACK)
    document = rules_snapshot if isinstance(rules_snapshot, Mapping) else {}
    resolver = document.get("resolver")
    supplied = resolver.get("source_title_fallback") if isinstance(resolver, Mapping) else None
    if isinstance(supplied, Mapping):
        for key in settings:
            if key in supplied:
                settings[key] = copy.deepcopy(supplied[key])
    settings["category_enabled"] = bool(
        settings.get("enabled", True)
        and category in {"movies", "tv"}
        and settings.get(category, True)
    )
    return settings


def source_title_contexts(
    job: Mapping[str, object],
    rules_snapshot: object,
) -> List[SourceTitleContext]:
    """Devuelve como máximo tres títulos compatibles con el trabajo actual."""

    category = str(job.get("category") or "").strip().lower()
    if not source_fallback_settings(rules_snapshot, category)["category_enabled"]:
        return []
    meta = _source_meta(job.get("source_meta_json"))
    raw_contexts = meta.get("source_contexts")
    if not isinstance(raw_contexts, list):
        return []
    job_hash = str(job.get("infohash") or job.get("qbt_hash") or "").strip().lower()
    result: List[SourceTitleContext] = []
    seen = set()
    now = time.time()
    cutoff = now - CONTEXT_TTL_SECONDS
    for raw in raw_contexts:
        if not isinstance(raw, Mapping):
            continue
        try:
            title = validate_source_title(raw.get("source_title"))
        except SourceContextContractError:
            continue
        source = str(raw.get("source") or "").strip().lower()
        event_id = str(raw.get("event_id") or "").strip()
        infohash = str(raw.get("infohash") or "").strip().lower()
        destination = str(raw.get("destination") or "").strip().lower()
        delivery_state = str(raw.get("delivery_state") or "").strip().lower()
        try:
            received_at = float(raw.get("received_at") or 0)
        except (TypeError, ValueError):
            received_at = 0
        if (
            not source
            or not event_id
            or destination != category
            or delivery_state not in USABLE_DELIVERY_STATES
            or received_at < cutoff
            or received_at > now + 60
            or not INFOHASH_PATTERN.fullmatch(job_hash)
            or infohash != job_hash
        ):
            continue
        title_key = normalize_title(title)
        if not title_key:
            continue
        dedupe_key = (source, event_id, title_key)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        result.append(
            SourceTitleContext(
                event_id=event_id,
                source=source,
                infohash=infohash,
                destination=destination,
                source_title=title,
                route=str(raw.get("route") or "").strip(),
                delivery_state=delivery_state,
                created_at=str(raw.get("created_at") or "").strip(),
            )
        )
        if len(result) >= MAX_SOURCE_TITLES:
            break
    return result


def fallback_job(
    job: Mapping[str, object],
    context: SourceTitleContext,
) -> Dict[str, object]:
    """Crea una vista de resolución sin sustituir la identidad física del job."""

    candidate = dict(job)
    candidate["name"] = context.source_title
    candidate["_source_context_only"] = True
    candidate["_source_context"] = context.public()
    candidate["_source_primary_name"] = str(job.get("name") or "")
    return candidate


def source_fallback_block_reason(
    job: Mapping[str, object],
    rules_snapshot: object,
) -> str:
    """Bloquea respaldos que convertirian un pack en una obra individual."""

    category = str(job.get("category") or "").strip().lower()
    if category not in {"movies", "tv"}:
        return "category_not_resolvable"
    document = rules_snapshot if isinstance(rules_snapshot, Mapping) else {}
    parser_rules = document.get("parser") if isinstance(document.get("parser"), Mapping) else None
    rules = resolve_parser_rules(rules=parser_rules)
    parsed = parse_release_name(
        str(job.get("name") or ""),
        category,
        rules=rules,
    )
    tv_strong = bool(
        parsed.season is not None
        or parsed.episodes
        or parsed.absolute_episode is not None
    )
    normalization = (
        rules.get("normalization")
        if isinstance(rules.get("normalization"), Mapping)
        else {}
    )
    allow_tv_year_range = bool(
        category == "tv"
        and tv_strong
        and normalization.get("allow_tv_year_range", False)
        and single_year_range_covers_all_years(parsed.cleaned, rules)
    )
    if collection_like_manual(
        parsed.cleaned,
        rules,
        allow_year_range=allow_tv_year_range,
    ) or collection_like_manual(
        parsed.display_title,
        rules,
        allow_year_range=allow_tv_year_range,
    ):
        return "ambiguous_collection"
    return ""


def recoverable_resolution_error(error: ResolverAmbiguous) -> bool:
    details = error.details if isinstance(error.details, dict) else {}
    reason = str(details.get("reason_code") or "")
    identity_source = str(details.get("identity_source") or "")
    if identity_source in {"tmdb_id", "imdb_id", "forced_match"}:
        return False
    return reason in {"empty_title", "no_candidates"} or details.get("top_score") is not None


def _source_meta(value: object) -> Dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}
