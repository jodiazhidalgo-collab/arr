"""Construccion y orden de candidatos de titulo para el resolver."""

import re
from typing import Dict, List, Mapping, Sequence

from ..parser_models import TitleEvidence
from ..parser_titles import is_editorial_title_auxiliary
from ...name_parser import parse_release_name
from ..parser_rules import parser_pattern, resolve_parser_rules
from ..resolver_defaults import DEFAULT_SERIES_CANDIDATES
from .text import clean_release_name, normalize_title, unique

TV_IDENTITY_PATTERNS = (
    "series_sxe",
    "series_x",
    "explicit_season",
    "season_pack",
    "chapter",
    "episode_word",
)

TITLE_EVIDENCE_ROLE_PRIORITY = {
    "alternate": 10,
    "derived_primary": 20,
    "composite": 30,
    "primary": 40,
    "configured_primary": 50,
}


def ordered_title_candidates(
    parser_candidates: Sequence[str],
    fallback_title: str,
    derived_candidates: Sequence[str] = (),
) -> List[str]:
    """Conserva primero el orden del parser y despues anade los derivados."""

    primary = list(parser_candidates) or [fallback_title]
    return unique([*primary, *derived_candidates])


def ordered_title_evidence(
    parser_evidence: Sequence[object],
    fallback_title: str,
    derived_candidates: Sequence[str] = (),
) -> List[TitleEvidence]:
    result: List[TitleEvidence] = []
    for item in parser_evidence:
        evidence = _coerce_title_evidence(item)
        if evidence is not None:
            _merge_title_evidence(result, evidence)

    primary = str(fallback_title or "").strip()
    if primary and not any(
        item.role in {"primary", "configured_primary"} for item in result
    ):
        result.insert(
            0,
            TitleEvidence(
                value=primary,
                role="primary",
                source="legacy",
                group_id="legacy:0",
            ),
        )

    for index, value in enumerate(derived_candidates):
        _merge_title_evidence(
            result,
            TitleEvidence(
                value=str(value or "").strip(),
                role="derived_primary",
                source="series_prefix",
                group_id=f"series:{index}",
            ),
        )
    return result


def ensure_title_evidence(guessed: Mapping[str, object]) -> List[TitleEvidence]:
    supplied = guessed.get("_title_evidence")
    values = supplied if isinstance(supplied, list) else []
    result = ordered_title_evidence(
        values,
        str(guessed.get("title") or guessed.get("_display_title") or ""),
    )
    if values:
        return result

    primary = str(guessed.get("title") or guessed.get("_display_title") or "").strip()
    primary_key = normalize_title(primary)
    display = str(guessed.get("_display_title") or "").strip()
    display_key = normalize_title(display)
    if display_key and display_key != primary_key:
        display_role = (
            "composite" if "(" in display and ")" in display else "alternate"
        )
        if display_role != "alternate" or not is_editorial_title_auxiliary(display):
            _merge_title_evidence(
                result,
                TitleEvidence(
                    value=display,
                    role=display_role,
                    source="legacy",
                    group_id="legacy:0",
                ),
            )
    candidates = guessed.get("_title_candidates")
    values = (
        candidates
        if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes))
        else []
    )
    for value in values:
        text = str(value or "").strip()
        key = normalize_title(text)
        if not key or key in {primary_key, display_key}:
            continue
        role = legacy_title_role(text, primary)
        if role == "alternate" and is_editorial_title_auxiliary(text):
            continue
        _merge_title_evidence(
            result,
            TitleEvidence(
                value=text,
                role=role,
                source="legacy",
                group_id="legacy:0",
            ),
        )
    return result


def legacy_title_role(value: str, primary: str) -> str:
    """Distingue la forma combinada legacy de un titulo atomico auxiliar."""

    text = str(value or "").strip()
    if ("(" in text and ")" in text) or re.search(r"\s+/\s+", text):
        return "composite"
    normalized = normalize_title(text)
    normalized_primary = normalize_title(primary)
    value_tokens = set(normalized.split())
    primary_tokens = set(normalized_primary.split())
    if (
        normalized
        and normalized_primary
        and len(value_tokens) > len(primary_tokens)
        and primary_tokens.issubset(value_tokens)
    ):
        return "composite"
    return "alternate"


def merge_title_evidence(
    values: Sequence[object], additions: Sequence[object]
) -> List[TitleEvidence]:
    result: List[TitleEvidence] = []
    for item in [*values, *additions]:
        evidence = _coerce_title_evidence(item)
        if evidence is not None:
            _merge_title_evidence(result, evidence)
    return result


def series_title_candidates(
    evidence: Sequence[str], policy: Dict[str, object]
) -> List[str]:
    """Extrae el titulo anterior al primer marcador de episodio configurado."""

    settings = (
        policy.get("series_candidates")
        if isinstance(policy.get("series_candidates"), dict)
        else {}
    )
    default_enabled = bool(DEFAULT_SERIES_CANDIDATES["title_before_episode_marker"])
    if not bool(settings.get("title_before_episode_marker", default_enabled)):
        return []
    default_minimum = int(DEFAULT_SERIES_CANDIDATES["min_title_words"])
    minimum_words = _positive_int(
        settings.get("min_title_words", default_minimum), default_minimum
    )

    rules = resolve_parser_rules(rules=policy.get("parser"))
    result: List[str] = []
    for value in evidence:
        text = str(value or "").strip()
        matches = []
        for name in TV_IDENTITY_PATTERNS:
            pattern = parser_pattern(rules, name)
            if pattern and (match := re.search(pattern, text, flags=re.IGNORECASE)):
                matches.append(match)
        if not matches:
            continue
        first = min(matches, key=lambda match: match.start())
        prefix = text[: first.start()].strip(" ._-")
        if not prefix:
            continue
        parsed = parse_release_name(prefix, "tv", rules=rules)
        for candidate in (parsed.display_title, clean_release_name(prefix)):
            candidate = candidate.strip()
            if len(normalize_title(candidate).split()) >= minimum_words:
                result.append(candidate)
    return unique(result)


def _positive_int(value: object, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _coerce_title_evidence(value: object) -> TitleEvidence | None:
    if isinstance(value, TitleEvidence):
        item = value
    elif isinstance(value, Mapping):
        item = TitleEvidence(
            value=str(value.get("value") or "").strip(),
            role=str(value.get("role") or "").strip(),
            source=str(value.get("source") or "").strip(),
            group_id=str(value.get("group_id") or "").strip(),
        )
    else:
        return None
    if not item.value or not item.role or not item.source or not item.group_id:
        return None
    return item


def _merge_title_evidence(
    result: List[TitleEvidence], evidence: TitleEvidence
) -> None:
    key = normalize_title(evidence.value)
    if not key:
        return
    for index, current in enumerate(result):
        if normalize_title(current.value) != key:
            continue
        current_priority = TITLE_EVIDENCE_ROLE_PRIORITY.get(current.role, 0)
        incoming_priority = TITLE_EVIDENCE_ROLE_PRIORITY.get(evidence.role, 0)
        if incoming_priority > current_priority:
            result[index] = evidence
        return
    result.append(evidence)
