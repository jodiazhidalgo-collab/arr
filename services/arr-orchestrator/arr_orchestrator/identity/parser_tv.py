import re
from copy import deepcopy
from typing import Any, Dict, Mapping, Optional, Tuple

from .parser_rules import parser_pattern
from .parser_trace import ParserTrace


def parse_tv(text: str, rules: Mapping[str, Any], trace: Optional[ParserTrace] = None) -> Dict[str, object]:
    result: Dict[str, object] = {
        "strong": False,
        "season": None,
        "episodes": [],
        "episode_range": None,
        "absolute_episode": None,
        "season_pack": None,
    }
    initial = deepcopy(result)
    season = None

    explicit_season = _search(rules, "explicit_season", text)
    explicit_season_value = _captured_int(explicit_season, 1)
    if explicit_season and explicit_season_value is not None:
        before = deepcopy(result)
        season = explicit_season_value
        result["strong"] = True
        _record(trace, "tv.explicit_season", before, result)

    t_pack = _search(rules, "season_pack", text)
    t_pack_value = _captured_int(t_pack, 1)
    if t_pack and t_pack_value is not None and not _search(rules, "series_sxe", text):
        before = deepcopy(result)
        season = t_pack_value
        result["season_pack"] = season
        result["strong"] = True
        _record(trace, "tv.season_pack", before, result)

    sxe = _search(rules, "series_sxe", text)
    sxe_season = _captured_int(sxe, 1)
    sxe_first = _captured_int(sxe, 2)
    if sxe and sxe_season is not None and sxe_first is not None:
        before = deepcopy(result)
        season = sxe_season
        second = _captured_int(sxe, 3)
        set_episode_result(result, sxe_first, second, rules)
        result["strong"] = True
        _record(trace, "tv.series_sxe", before, result)

    x_pattern = _search(rules, "series_x", text)
    x_season = _captured_int(x_pattern, 1)
    x_first = _captured_int(x_pattern, 2)
    if x_pattern and x_season is not None and x_first is not None:
        before = deepcopy(result)
        season = x_season
        second = _captured_int(x_pattern, 3)
        set_episode_result(result, x_first, second, rules)
        result["strong"] = True
        _record(trace, "tv.series_x", before, result)

    chapter = _search(rules, "chapter", text)
    first_raw = _group(chapter, 1) if chapter else None
    if chapter and first_raw and first_raw.isdigit():
        before = deepcopy(result)
        second_raw = _group(chapter, 2)
        if second_raw and not second_raw.isdigit():
            second_raw = None
        if explicit_season:
            result["absolute_episode"] = None
            first = episode_part(first_raw)
            second = episode_part(second_raw) if second_raw else None
            set_episode_result(result, first, second, rules)
        elif len(first_raw) >= 3:
            chapter_season, first = split_cap_number(first_raw)
            season = chapter_season
            second = episode_part(second_raw) if second_raw else None
            set_episode_result(result, first, second, rules)
        else:
            result["absolute_episode"] = int(first_raw)
        result["strong"] = True
        _record(trace, "tv.chapter", before, result)

    episode = _search(rules, "episode_word", text)
    episode_value = _captured_int(episode, 1)
    if episode and episode_value is not None and not result["episodes"]:
        before = deepcopy(result)
        result["absolute_episode"] = episode_value
        result["strong"] = True
        _record(trace, "tv.episode_word", before, result)

    if season is not None:
        before = deepcopy(result)
        result["season"] = season
        if season_pack_marker(text, rules) and not result["episodes"]:
            result["season_pack"] = season
        _record(trace, "tv.finalize_season", before, result)

    _record(trace, "tv.parse", initial, result, changed_only=False)
    return result


def remove_tv_tokens(
    text: str,
    rules: Mapping[str, Any],
    trace: Optional[ParserTrace] = None,
) -> str:
    current = text
    for name in ("series_sxe", "series_x", "explicit_season", "season_pack", "chapter", "episode_word"):
        pattern = parser_pattern(rules, name)
        if not pattern:
            continue
        before = current
        current = re.sub(pattern, " ", current, flags=re.IGNORECASE)
        _record(trace, f"title.remove_{name}", before, current)
    markers = [re.escape(str(item)) for item in rules.get("season_pack_markers") or [] if str(item)]
    if markers:
        before = current
        current = re.sub(rf"(?i)\b(?:{'|'.join(markers)})\b", " ", current)
        _record(trace, "title.remove_season_pack_marker", before, current)
    return current


def set_episode_result(
    result: Dict[str, object],
    first: int,
    second: Optional[int],
    rules: Optional[Mapping[str, Any]] = None,
) -> None:
    if second is None or second == first:
        result["episodes"] = [first]
        return
    start, end = sorted((first, second))
    normalization = rules.get("normalization") if isinstance(rules, Mapping) else {}
    max_range = normalization.get("max_episode_range", 0) if isinstance(normalization, Mapping) else 0
    try:
        limit = int(max_range or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0 and end - start + 1 > limit:
        end = start + limit - 1
    result["episode_range"] = (start, end)
    result["episodes"] = list(range(start, end + 1))


def split_cap_number(value: str) -> Tuple[int, int]:
    digits = str(value)
    return int(digits[:-2]), int(digits[-2:])


def episode_part(value: Optional[str]) -> int:
    digits = str(value or "0")
    if len(digits) >= 3:
        return int(digits[-2:])
    return int(digits)


def season_pack_marker(text: str, rules: Mapping[str, Any]) -> bool:
    markers = [re.escape(str(item)) for item in rules.get("season_pack_markers") or [] if str(item)]
    if not markers:
        return False
    return bool(re.search(rf"(?i)\b(?:{'|'.join(markers)})\b", text))


def _search(rules: Mapping[str, Any], name: str, text: str) -> Optional[re.Match[str]]:
    pattern = parser_pattern(rules, name)
    return re.search(pattern, text, flags=re.IGNORECASE) if pattern else None


def _group(match: re.Match[str], index: int) -> Optional[str]:
    try:
        return match.group(index)
    except IndexError:
        return None


def _captured_int(match: Optional[re.Match[str]], index: int) -> Optional[int]:
    if match is None:
        return None
    value = _group(match, index)
    if value is None or not value.isdigit():
        return None
    return int(value)


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
