import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .parser_cleaning import append_unique, fold, smart_title
from .parser_models import ParsedName
from .parser_rules import parser_pattern, regex_items
from .parser_trace import ParserTrace
from .parser_tv import remove_tv_tokens


def extract_year(
    text: str,
    rules: Mapping[str, Any],
    trace: Optional[ParserTrace] = None,
) -> Optional[int]:
    year_rules = rules.get("year") if isinstance(rules.get("year"), Mapping) else {}
    pattern = str(year_rules.get("pattern") or r"(?<!\d)((?:19|20)\d{2})(?!\d)")
    minimum = _integer(year_rules.get("min"), 1900)
    maximum = _integer(year_rules.get("max"), 2099)
    matches = []
    for match in re.finditer(pattern, text):
        try:
            value = int(match.group(1) if match.lastindex else match.group(0))
        except (TypeError, ValueError):
            continue
        if minimum <= value <= maximum:
            matches.append(value)
    mode = str(year_rules.get("multiple") or "first").strip().lower()
    year = matches[-1] if matches and mode == "last" else (matches[0] if matches else None)
    if trace is not None:
        trace.record("year.extract", text, year)
    return year


def title_candidates(
    cleaned: str,
    year: Optional[int],
    tv: Mapping[str, object],
    rules: Mapping[str, Any],
    trace: Optional[ParserTrace] = None,
) -> List[str]:
    title = title_from_cleaned(cleaned, year, tv, rules, trace)
    candidates: List[str] = []
    if title:
        bilingual = [part.strip() for part in re.split(r"\s+/\s+", title) if part.strip()]
        variants = bilingual if len(bilingual) > 1 else [title]
        for variant in variants:
            outer, inner = split_parenthesized_title(variant, rules)
            for value in (outer, inner, variant):
                append_unique(candidates, value)
        # El nombre completo conserva contexto y sirve como candidato adicional.
        append_unique(candidates, title)
    if trace is not None:
        trace.record("title.candidates", title, candidates)
    return candidates


def split_parenthesized_title(title: str, rules: Mapping[str, Any]) -> Tuple[str, str]:
    pattern = parser_pattern(rules, "parenthesized_title", r"^(.*?)\s*\(([^()]+)\)\s*$")
    match = re.match(pattern, title)
    if not match:
        return title, ""
    outer = str(match.group(1) or "").strip()
    inner = str(match.group(2) or "").strip()
    year_rules = rules.get("year") if isinstance(rules.get("year"), Mapping) else {}
    year_pattern = str(year_rules.get("pattern") or r"(?<!\d)((?:19|20)\d{2})(?!\d)")
    if re.fullmatch(year_pattern, inner):
        return outer, ""
    return outer, inner


def title_from_cleaned(
    cleaned: str,
    year: Optional[int],
    tv: Mapping[str, object],
    rules: Mapping[str, Any],
    trace: Optional[ParserTrace] = None,
) -> str:
    del tv  # Las señales TV se eliminan por reglas; no se muta el resultado detectado.
    text = cleaned
    if year:
        prefix = title_prefix_before_year(text, year, rules)
        if prefix:
            text = _changed(trace, "title.before_year", text, prefix)
        else:
            before = text
            text = re.sub(rf"\s*[\[(]\s*{year}\s*[\])]\s*", " ", text, count=1)
            text = re.sub(rf"(?<!\d){year}(?!\d)", " ", text, count=1)
            _record(trace, "title.remove_year", before, text)
    before = text
    text = re.sub(r"\(\s*\)", " ", text)
    _record(trace, "title.empty_parentheses", before, text)
    text = remove_tv_tokens(text, rules, trace)
    compact_web = parser_pattern(rules, "compact_web", r"(?i)\b(?:4k)?web(?:rip|dl)\d{3,4}p?\b")
    before = text
    text = re.sub(compact_web, " ", text) if compact_web else text
    _record(trace, "title.compact_web", before, text)
    text = strip_title_tail_noise(text, rules, trace)

    tokens = regex_items(
        [
            *(rules.get("technical_tokens") or []),
            *(rules.get("video_markers") or []),
        ]
    )
    if tokens:
        marker = re.search(rf"(?i)\b(?:{tokens})\b", text)
        if marker:
            text = _changed(trace, "title.technical_tail", text, text[: marker.start()])

    languages = [re.escape(str(item)) for item in rules.get("language_tokens") or [] if str(item)]
    if languages:
        before = text
        text = re.sub(rf"\b(?:{'|'.join(languages)})\b", " ", text, flags=re.IGNORECASE)
        _record(trace, "title.language_tokens", before, text)
    text = strip_title_tail_noise(text, rules, trace)
    before = text
    text = re.sub(r"\s+", " ", text)
    _record(trace, "title.whitespace", before, text)
    text = text.strip(" -_.,")
    normalization = rules.get("normalization") if isinstance(rules.get("normalization"), Mapping) else {}
    result = smart_title(text, rules) if normalization.get("smart_title", True) else text
    _record(trace, "title.smart", text, result, changed_only=False)
    return result


def title_prefix_before_year(text: str, year: int, rules: Mapping[str, Any]) -> str:
    match = re.search(rf"(?<!\d){year}(?!\d)", text)
    if not match:
        return ""
    prefix = text[: match.start()]
    prefix = re.sub(r"\s*[\[(]\s*$", "", prefix)
    prefix = strip_title_tail_noise(prefix, rules)
    return prefix if fold(prefix) else ""


def strip_title_tail_noise(
    text: str,
    rules: Mapping[str, Any],
    trace: Optional[ParserTrace] = None,
) -> str:
    original = text
    current = re.sub(r"\s+", " ", text or "").strip(" -_.,")
    current = trim_unbalanced_parentheses(current)
    tokens = [re.escape(str(item)) for item in rules.get("tail_noise_tokens") or [] if str(item)]
    if tokens:
        normalization = rules.get("normalization") if isinstance(rules.get("normalization"), Mapping) else {}
        passes = max(1, _integer(normalization.get("tail_noise_passes"), 4))
        pattern = rf"(?i)(?:\s+|[-_.])\b(?:{'|'.join(tokens)})\b\s*$"
        for _ in range(passes):
            updated = re.sub(pattern, "", current).strip(" -_.,")
            updated = trim_unbalanced_parentheses(updated)
            if updated == current:
                break
            current = updated
    _record(trace, "title.tail_noise", original, current)
    return current


def trim_unbalanced_parentheses(text: str) -> str:
    current = text.strip(" -_.,")
    if current.count("(") > current.count(")"):
        current = re.sub(r"\s*\([^()]*$", "", current).strip(" -_.,")
    if current.count(")") > current.count("("):
        current = re.sub(r"\s*\)+\s*$", "", current).strip(" -_.,")
    return current


def manual_name(
    cleaned: str,
    title: str,
    rules: Mapping[str, Any],
    *,
    tv_strong: bool = False,
) -> bool:
    normalized = fold(f"{cleaned} {title}")
    normalization = rules.get("normalization") if isinstance(rules.get("normalization"), Mapping) else {}
    allow_tv_year_range = bool(
        tv_strong
        and normalization.get("allow_tv_year_range", False)
        and single_year_range_covers_all_years(cleaned, rules)
    )
    if not allow_tv_year_range and multiple_years_require_manual(cleaned, rules):
        return True
    if collection_like_manual(
        cleaned, rules, allow_year_range=allow_tv_year_range
    ) or collection_like_manual(title, rules, allow_year_range=allow_tv_year_range):
        return True
    keywords = [re.escape(fold(str(item))) for item in rules.get("manual_keywords") or [] if fold(str(item))]
    if keywords and re.search(rf"\b(?:{'|'.join(keywords)})\b", normalized):
        return True
    lone = normalized.strip()
    exact_names = {fold(str(item)) for item in rules.get("manual_exact_names") or [] if fold(str(item))}
    return lone in exact_names or fold(cleaned) in exact_names


def multiple_years_require_manual(value: str, rules: Mapping[str, Any]) -> bool:
    year_rules = rules.get("year") if isinstance(rules.get("year"), Mapping) else {}
    if str(year_rules.get("multiple") or "first").strip().lower() != "manual":
        return False
    return len(_matched_years(value, rules)) > 1


def _matched_years(value: str, rules: Mapping[str, Any]) -> List[int]:
    year_rules = rules.get("year") if isinstance(rules.get("year"), Mapping) else {}
    pattern = str(year_rules.get("pattern") or r"(?<!\d)((?:19|20)\d{2})(?!\d)")
    minimum = _integer(year_rules.get("min"), 1900)
    maximum = _integer(year_rules.get("max"), 2099)
    years: List[int] = []
    for match in re.finditer(pattern, value):
        try:
            year = int(match.group(1) if match.lastindex else match.group(0))
        except (TypeError, ValueError):
            continue
        if minimum <= year <= maximum:
            years.append(year)
    return years


def single_year_range_covers_all_years(value: str, rules: Mapping[str, Any]) -> bool:
    range_pattern = parser_pattern(rules, "year_range")
    if not range_pattern:
        return False
    ranges = list(re.finditer(range_pattern, value, flags=re.IGNORECASE))
    if len(ranges) != 1:
        return False
    all_years = _matched_years(value, rules)
    range_years = _matched_years(ranges[0].group(0), rules)
    return len(all_years) == 2 and len(range_years) == 2 and all_years == range_years


def collection_like_manual(
    value: str,
    rules: Mapping[str, Any],
    *,
    allow_year_range: bool = False,
) -> bool:
    normalized = fold(value)
    keywords = [re.escape(fold(str(item))) for item in rules.get("collection_keywords") or [] if fold(str(item))]
    if keywords and re.search(rf"\b(?:{'|'.join(keywords)})\b", normalized):
        return True
    count_pattern = parser_pattern(rules, "collection_count")
    if count_pattern and re.search(count_pattern, normalized, flags=re.IGNORECASE):
        return True
    part_pattern = parser_pattern(rules, "collection_part")
    if part_pattern and re.search(part_pattern, normalized, flags=re.IGNORECASE):
        return True
    if allow_year_range:
        return False
    range_pattern = parser_pattern(rules, "year_range")
    return bool(range_pattern and re.search(range_pattern, value, flags=re.IGNORECASE))


def guessit_input(title: str, year: Optional[int], tv: Mapping[str, object]) -> str:
    parts = [title]
    if year:
        parts.append(str(year))
    season = tv.get("season")
    episodes = tv.get("episodes") or []
    if season and episodes:
        parts.append(f"S{int(season):02d}E{int(episodes[0]):02d}")
    elif season:
        parts.append(f"Season {int(season)}")
    elif tv.get("absolute_episode"):
        parts.append(f"Episode {int(tv['absolute_episode'])}")
    return " ".join(part for part in parts if part).strip()


def episode_hint(parsed: ParsedName) -> Dict[str, object]:
    hint: Dict[str, object] = {}
    if parsed.season is not None:
        hint["season"] = parsed.season
    if parsed.episodes:
        hint["episodes"] = list(parsed.episodes)
    if parsed.episode_range is not None:
        hint["episode_range"] = list(parsed.episode_range)
    if parsed.absolute_episode is not None:
        hint["absolute_episode"] = parsed.absolute_episode
    if parsed.season_pack is not None:
        hint["season_pack"] = parsed.season_pack
    return hint


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _changed(trace: Optional[ParserTrace], rule: str, before: str, after: str) -> str:
    _record(trace, rule, before, after)
    return after


def _record(
    trace: Optional[ParserTrace],
    rule: str,
    before: Any,
    after: Any,
    *,
    changed_only: bool = True,
) -> None:
    if trace is not None:
        trace.record(rule, before, after, changed_only=changed_only)
