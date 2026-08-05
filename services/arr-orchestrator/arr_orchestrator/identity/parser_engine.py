from typing import Any, Dict, Mapping, Optional, Tuple

from .parser_classification import classification_evidence
from .parser_cleaning import preclean
from .parser_models import MediaDecision, ParsedName
from .parser_rules import resolve_parser_rules
from .parser_titles import (
    episode_hint,
    extract_year,
    guessit_input,
    manual_name,
    title_evidence,
    title_from_cleaned,
)
from .parser_trace import ParserTrace
from .parser_tv import parse_tv


def parse_release_name(
    raw_name: str,
    explicit_category: str = "",
    rules: Any = None,
    config: Any = None,
) -> ParsedName:
    resolved = resolve_parser_rules(rules=rules, config=config)
    parsed, _ = _parse_release_name(raw_name, explicit_category, resolved)
    return parsed


def decide_media(
    raw_name: str,
    explicit_category: str = "",
    rules: Any = None,
    config: Any = None,
) -> MediaDecision:
    resolved = resolve_parser_rules(rules=rules, config=config)
    parsed, _ = _parse_release_name(raw_name, explicit_category, resolved)
    explicit = str(explicit_category or "").strip().lower()
    trusted = _trusted_categories(resolved)
    reason_codes = []
    if explicit in trusted:
        reason_codes.append(f"category_current_{explicit}")
    if parsed.media_hint == "tv":
        reason_codes.append("parser_tv_signal")
    elif parsed.media_hint == "movies":
        reason_codes.append("parser_movie_signal")
    elif parsed.media_hint == "manual":
        reason_codes.append("parser_manual_or_ambiguous")
    if parsed.year:
        reason_codes.append("year_detected")
    if parsed.category_conflict:
        reason_codes.append("category_conflict")
        return MediaDecision(
            media_type=parsed.media_hint,
            confidence=parsed.confidence,
            reason_codes=reason_codes,
            episode_hint=episode_hint(parsed),
            allow_external_lookup=False,
            block_reason="category_conflict",
            parsed=parsed,
        )
    if not parsed.display_title:
        reason_codes.append("no_usable_title")
        return MediaDecision(
            media_type="manual",
            confidence=_confidence(resolved, "low"),
            reason_codes=reason_codes,
            episode_hint=episode_hint(parsed),
            allow_external_lookup=False,
            block_reason="no_usable_title",
            parsed=parsed,
        )
    if parsed.media_hint in {"movies", "tv"}:
        return MediaDecision(
            media_type=parsed.media_hint,
            confidence=parsed.confidence,
            reason_codes=reason_codes,
            episode_hint=episode_hint(parsed),
            allow_external_lookup=True,
            parsed=parsed,
        )
    if explicit in trusted:
        reason_codes.append("trusted_existing_category")
        return MediaDecision(
            media_type=explicit,
            confidence=_confidence(resolved, "medium"),
            reason_codes=reason_codes,
            episode_hint=episode_hint(parsed),
            allow_external_lookup=True,
            parsed=parsed,
        )
    return MediaDecision(
        media_type="manual",
        confidence=_confidence(resolved, "low"),
        reason_codes=reason_codes,
        episode_hint=episode_hint(parsed),
        allow_external_lookup=False,
        block_reason="manual_or_ambiguous",
        parsed=parsed,
    )


def parse_with_trace(
    raw_name: str,
    explicit_category: str = "",
    rules: Any = None,
    config: Any = None,
) -> Dict[str, object]:
    """API pura de diagnóstico: no persiste ni consulta nada fuera del parser."""

    resolved = resolve_parser_rules(rules=rules, config=config)
    trace = ParserTrace()
    original = str(raw_name or "")
    parsed, tv = _parse_release_name(raw_name, explicit_category, resolved, trace)
    episode_range = tv.get("episode_range")
    return {
        "original": original,
        "cleaned": parsed.cleaned,
        "title": parsed.display_title,
        "candidates": list(parsed.title_candidates),
        "title_evidence": [item.to_dict() for item in parsed.title_evidence],
        "year": parsed.year,
        "category": parsed.media_hint,
        "confidence": parsed.confidence,
        "category_conflict": parsed.category_conflict,
        "tv": {
            "strong": bool(tv.get("strong")),
            "season": tv.get("season"),
            "episodes": list(tv.get("episodes") or []),
            "episode_range": list(episode_range) if episode_range is not None else None,
            "absolute_episode": tv.get("absolute_episode"),
            "season_pack": tv.get("season_pack"),
        },
        "guessit": parsed.guessit_input,
        "steps": trace.to_list(),
    }


def test_parser_title(
    raw_name: str,
    explicit_category: str = "",
    rules: Any = None,
    config: Any = None,
) -> Dict[str, object]:
    return parse_with_trace(raw_name, explicit_category, rules=rules, config=config)


# Evita que pytest lo recoja como test si otro módulo importa la API por nombre.
test_parser_title.__test__ = False


def _parse_release_name(
    raw_name: str,
    explicit_category: str,
    rules: Mapping[str, Any],
    trace: Optional[ParserTrace] = None,
) -> Tuple[ParsedName, Dict[str, object]]:
    original = str(raw_name or "")
    raw = original.strip()
    explicit = str(explicit_category or "").strip().lower()
    if trace is not None:
        trace.record("input.strip", original, raw, changed_only=True)
    cleaned = preclean(raw, rules, trace)
    year = extract_year(cleaned, rules, trace)
    tv = parse_tv(cleaned, rules, trace)
    title_items = title_evidence(cleaned, year, tv, rules, trace)
    candidates = [item.value for item in title_items]
    display_title = candidates[0] if candidates else title_from_cleaned(cleaned, year, tv, rules, trace)
    guessit = guessit_input(display_title, year, tv)
    if trace is not None:
        trace.record("guessit.build", display_title, guessit)

    is_manual = manual_name(
        cleaned,
        display_title,
        rules,
        tv_strong=bool(tv["strong"]),
    )
    evidence = classification_evidence(raw, cleaned, rules, trace)
    non_video = bool(evidence["non_video"])
    normalization = rules.get("normalization") if isinstance(rules.get("normalization"), Mapping) else {}
    movie_strong = bool(year and not tv["strong"] and not is_manual)
    movie_from_video = bool(
        normalization.get("movie_without_year_from_video", False)
        and not year
        and not tv["strong"]
        and not is_manual
        and not non_video
        and evidence["strong_video"]
    )
    tv_strong = bool(tv["strong"] and not is_manual)
    media_hint = "manual"
    confidence = _confidence(rules, "low")
    trusted = _trusted_categories(rules)
    if tv_strong:
        media_hint = "tv"
        confidence = _confidence(rules, "high")
    elif explicit in trusted and not is_manual:
        media_hint = explicit
        confidence = _confidence(rules, "medium")
    elif movie_strong:
        media_hint = "movies"
        confidence = _confidence(rules, "high")
    elif movie_from_video:
        media_hint = "movies"
        confidence = _confidence(rules, "high")

    category_conflict = None
    if explicit == "movies" and tv_strong:
        category_conflict = "movies_vs_tv"
    if trace is not None:
        trace.record(
            "category.classify",
            {
                "explicit": explicit,
                "manual": is_manual,
                "non_video": non_video,
                "movie_strong": movie_strong,
                "movie_from_video": movie_from_video,
                "tv_strong": tv_strong,
            },
            {
                "category": media_hint,
                "confidence": confidence,
                "conflict": category_conflict,
            },
        )

    parsed = ParsedName(
        raw=raw,
        cleaned=cleaned,
        display_title=display_title,
        title_candidates=candidates or ([display_title] if display_title else []),
        title_evidence=title_items,
        year=year,
        media_hint=media_hint,
        confidence=confidence,
        season=tv["season"],
        episodes=tv["episodes"],
        episode_range=tv["episode_range"],
        absolute_episode=tv["absolute_episode"],
        season_pack=tv["season_pack"],
        guessit_input=guessit,
        category_conflict=category_conflict,
    )
    return parsed, tv


def _trusted_categories(rules: Mapping[str, Any]) -> set:
    normalization = rules.get("normalization") if isinstance(rules.get("normalization"), Mapping) else {}
    categories = normalization.get("trusted_categories", ["movies", "tv"])
    return {str(item).strip().lower() for item in categories or [] if str(item).strip()}


def _confidence(rules: Mapping[str, Any], level: str) -> str:
    normalization = rules.get("normalization") if isinstance(rules.get("normalization"), Mapping) else {}
    return str(normalization.get(f"confidence_{level}") or level)
