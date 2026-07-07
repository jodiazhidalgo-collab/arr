import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


KNOWN_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".wmv",
    ".ts",
    ".m2ts",
    ".mts",
    ".webm",
    ".zip",
    ".rar",
    ".7z",
}
SITE_WORDS = (
    "uindex",
    "wolfmax4k",
    "newpct1",
    "atomohd",
    "pctnew",
    "elitetorrent",
    "todotorrente",
    "pctmix",
    "pctreload",
    "descargas2020",
)
TECH_TOKENS_RE = re.compile(
    r"(?i)\b(?:"
    r"4k|2160p?|1080p?|720p?|576p?|480p?|uhd|hdr|hdr10|dv|dovi|"
    r"bluray|blu-ray|bdrip|bdremux|remux|web[- ]?dl|webrip|hdtv|dvdrip|"
    r"hdrip|microhd|cam|hdcam|ts|hdts|tc|hdtc|telesync|telecine|"
    r"screener|dvdscreener|workprint|line|"
    r"amzn|nf|netflix|hmax|dsnp|itunes|ac3|eac3|dts|dts-hd|truehd|"
    r"x26[45]|h26[45]|hevc|avc|aac|ddp?5?\.?1|castellano|spanish|"
    r"dual|sub[s]?|es-en|multi|proper|repack"
    r")\b"
)
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


@dataclass
class ParsedName:
    raw: str
    cleaned: str
    display_title: str
    title_candidates: List[str] = field(default_factory=list)
    year: Optional[int] = None
    media_hint: str = "manual"
    confidence: str = "low"
    season: Optional[int] = None
    episodes: List[int] = field(default_factory=list)
    episode_range: Optional[Tuple[int, int]] = None
    absolute_episode: Optional[int] = None
    season_pack: Optional[int] = None
    guessit_input: str = ""
    category_conflict: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class MediaDecision:
    media_type: str
    confidence: str
    reason_codes: List[str] = field(default_factory=list)
    episode_hint: Dict[str, object] = field(default_factory=dict)
    allow_external_lookup: bool = False
    block_reason: Optional[str] = None
    parsed: Optional[ParsedName] = None

    def to_dict(self) -> Dict[str, object]:
        payload = asdict(self)
        if self.parsed:
            payload["parsed"] = self.parsed.to_dict()
        return payload


def parse_release_name(raw_name: str, explicit_category: str = "") -> ParsedName:
    raw = str(raw_name or "").strip()
    explicit = str(explicit_category or "").strip().lower()
    cleaned = _preclean(raw)
    year = _extract_year(cleaned)
    tv = _parse_tv(cleaned)
    title_candidates = _title_candidates(cleaned, year, tv)
    display_title = title_candidates[0] if title_candidates else _title_from_cleaned(cleaned, year, tv)
    guessit_input = _guessit_input(display_title, year, tv)

    manual = _manual_name(cleaned, display_title)
    movie_strong = bool(year and not tv["strong"] and not manual)
    tv_strong = bool(tv["strong"] and not manual)

    media_hint = "manual"
    confidence = "low"
    if tv_strong:
        media_hint = "tv"
        confidence = "high"
    elif movie_strong:
        media_hint = "movies"
        confidence = "high"
    elif explicit in {"movies", "tv"} and not manual:
        media_hint = explicit
        confidence = "medium"

    category_conflict = None
    if explicit == "movies" and tv_strong:
        category_conflict = "movies_vs_tv"
    elif explicit == "tv" and movie_strong:
        category_conflict = "tv_vs_movies"

    return ParsedName(
        raw=raw,
        cleaned=cleaned,
        display_title=display_title,
        title_candidates=title_candidates or ([display_title] if display_title else []),
        year=year,
        media_hint=media_hint,
        confidence=confidence,
        season=tv["season"],
        episodes=tv["episodes"],
        episode_range=tv["episode_range"],
        absolute_episode=tv["absolute_episode"],
        season_pack=tv["season_pack"],
        guessit_input=guessit_input,
        category_conflict=category_conflict,
    )


def decide_media(raw_name: str, explicit_category: str = "") -> MediaDecision:
    explicit = str(explicit_category or "").strip().lower()
    parsed = parse_release_name(raw_name, explicit)
    reason_codes: List[str] = []
    if explicit in {"movies", "tv"}:
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
            episode_hint=_episode_hint(parsed),
            allow_external_lookup=False,
            block_reason="category_conflict",
            parsed=parsed,
        )
    if not parsed.display_title:
        reason_codes.append("no_usable_title")
        return MediaDecision(
            media_type="manual",
            confidence="low",
            reason_codes=reason_codes,
            episode_hint=_episode_hint(parsed),
            allow_external_lookup=False,
            block_reason="no_usable_title",
            parsed=parsed,
        )
    if parsed.media_hint in {"movies", "tv"}:
        return MediaDecision(
            media_type=parsed.media_hint,
            confidence=parsed.confidence,
            reason_codes=reason_codes,
            episode_hint=_episode_hint(parsed),
            allow_external_lookup=True,
            parsed=parsed,
        )
    if explicit in {"movies", "tv"}:
        reason_codes.append("trusted_existing_category")
        return MediaDecision(
            media_type=explicit,
            confidence="medium",
            reason_codes=reason_codes,
            episode_hint=_episode_hint(parsed),
            allow_external_lookup=True,
            parsed=parsed,
        )
    return MediaDecision(
        media_type="manual",
        confidence="low",
        reason_codes=reason_codes,
        episode_hint=_episode_hint(parsed),
        allow_external_lookup=False,
        block_reason="manual_or_ambiguous",
        parsed=parsed,
    )


def _episode_hint(parsed: ParsedName) -> Dict[str, object]:
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


def _preclean(value: str) -> str:
    text = Path(value).name.strip()
    suffix = Path(text).suffix.lower()
    if suffix in KNOWN_EXTENSIONS:
        text = text[: -len(suffix)]
    text = re.sub(r"__\d{8,}$", "", text)
    while True:
        new = re.sub(r"\s*\(\d{1,2}\)\s*$", "", text).strip()
        if new == text:
            break
    text = new
    text = text.replace("`", " ").replace("´", " ").replace("’", "'")
    text = re.sub(r"[–—]+", "-", text)
    text = _normalize_release_ocr_tokens(text)
    text = re.sub(r"(?i)\bwww\.[a-z0-9-]+\.(?:com|net|org|li|tv|bz)\s*[-_]*", " ", text)
    text = re.sub(r"(?i)\b[a-z0-9-]+\.(?:com|net|org|li|tv|bz)\b", " ", text)
    for word in SITE_WORDS:
        text = re.sub(rf"(?i)\b{re.escape(word)}\b", " ", text)
    text = re.sub(r"(?i)\b(S\d{1,2}\s*E\d{1,3})[_-](\d{1,3})\b", r"\1-\2", text)
    text = re.sub(r"(?i)\b(\d{1,2}x\d{1,3})[_-](\d{1,3})\b", r"\1-\2", text)
    text = re.sub(r"(?i)\b(cap(?:[íi]tulo)?\.?\s*\d{1,4})[_-](\d{1,4})\b", r"\1-\2", text)
    text = re.sub(r"(?i)([a-záéíóúñ])(\d+x\d+)", r"\1 \2", text)
    text = re.sub(r"(?i)\b(temporada|season|capitulo|capítulo|episode|episodio|cap)\s*([0-9])", r"\1 \2", text)
    text = re.sub(r"(?i)\bT\s*([0-9]{1,2})\b", lambda m: f"T{int(m.group(1)):02d}", text)
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"[\[\]{}]+", " ", text)
    text = re.sub(r"\s*-\s*", " - ", text)
    text = _normalize_release_ocr_tokens(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_.,")


def _normalize_release_ocr_tokens(text: str) -> str:
    text = re.sub(r"(?i)\b1[o0]8[o0]p\b", "1080p", text)
    text = re.sub(r"(?i)\b72[o0]p\b", "720p", text)
    text = re.sub(r"(?i)\b2[1l]6[o0]p\b", "2160p", text)
    text = re.sub(r"(?i)\b4[l1i]k\b", "4k", text)
    text = re.sub(r"(?i)\b4kk\b", "4k", text)
    return text


def _extract_year(text: str) -> Optional[int]:
    for match in YEAR_RE.finditer(text):
        year = int(match.group(1))
        if 1900 <= year <= 2099:
            return year
    return None


def _parse_tv(text: str) -> Dict[str, object]:
    result: Dict[str, object] = {
        "strong": False,
        "season": None,
        "episodes": [],
        "episode_range": None,
        "absolute_episode": None,
        "season_pack": None,
    }
    season = None
    explicit_season = re.search(r"(?i)\b(?:temporada|season)\s*0?(\d{1,2})\b", text)
    if explicit_season:
        season = int(explicit_season.group(1))
        result["strong"] = True
    t_pack = re.search(r"(?i)(?:^|\s)T0?(\d{1,2})(?:\b|[- ]|$)", text)
    if t_pack and not re.search(r"(?i)\bS\d{1,2}\s*E\d{1,3}\b", text):
        season = int(t_pack.group(1))
        result["season_pack"] = season
        result["strong"] = True
    sxe = re.search(r"(?i)\bS0?(\d{1,2})\s*E0?(\d{1,3})(?:\s*(?:-|_|E)\s*0?(\d{1,3}))?\b", text)
    if sxe:
        season = int(sxe.group(1))
        first = int(sxe.group(2))
        second = _optional_int(sxe.group(3))
        _set_episode_result(result, first, second)
        result["strong"] = True
    xpat = re.search(r"(?i)\b(\d{1,2})x0?(\d{1,3})(?:\s*(?:-|_)\s*0?(\d{1,3}))?\b", text)
    if xpat:
        season = int(xpat.group(1))
        first = int(xpat.group(2))
        second = _optional_int(xpat.group(3))
        _set_episode_result(result, first, second)
        result["strong"] = True

    cap = re.search(r"(?i)\bcap(?:[íi]tulo)?\.?\s*0?(\d{1,4})(?:\s*(?:-|_)\s*0?(\d{1,4}))?\b", text)
    if cap:
        first_raw = cap.group(1)
        second_raw = cap.group(2)
        if explicit_season:
            result["absolute_episode"] = None
            first = _episode_part(first_raw)
            second = _episode_part(second_raw) if second_raw else None
            _set_episode_result(result, first, second)
        elif len(first_raw) >= 3:
            cap_season, first = _split_cap_number(first_raw)
            season = cap_season
            second = _episode_part(second_raw) if second_raw else None
            _set_episode_result(result, first, second)
        else:
            result["absolute_episode"] = int(first_raw)
        result["strong"] = True

    episode = re.search(r"(?i)\b(?:episode|episodio)\s*0?(\d{1,3})\b", text)
    if episode and not result["episodes"]:
        result["absolute_episode"] = int(episode.group(1))
        result["strong"] = True

    if season is not None:
        result["season"] = season
        if _season_pack_marker(text) and not result["episodes"]:
            result["season_pack"] = season
    return result


def _set_episode_result(result: Dict[str, object], first: int, second: Optional[int]) -> None:
    if second is None or second == first:
        result["episodes"] = [first]
        return
    start, end = sorted((first, second))
    result["episode_range"] = (start, end)
    result["episodes"] = list(range(start, end + 1))


def _split_cap_number(value: str) -> Tuple[int, int]:
    digits = str(value)
    if len(digits) >= 4:
        return int(digits[:-2]), int(digits[-2:])
    return int(digits[:-2]), int(digits[-2:])


def _episode_part(value: Optional[str]) -> int:
    digits = str(value or "0")
    if len(digits) >= 3:
        return int(digits[-2:])
    return int(digits)


def _optional_int(value: Optional[str]) -> Optional[int]:
    return int(value) if value else None


def _season_pack_marker(text: str) -> bool:
    return bool(re.search(r"(?i)\b(?:completa|complete|extras)\b", text))


def _manual_name(cleaned: str, title: str) -> bool:
    normalized = _fold(f"{cleaned} {title}")
    if _collection_like_manual(cleaned) or _collection_like_manual(title):
        return True
    if re.search(r"\blynda\b|\bcourse\b|\bcollection\b|\blinux\b|\bubuntu\b|\bshell\b|\bcli\b", normalized):
        return True
    lone = normalized.strip()
    return lone in {"wasabi", "doraemon", "bluey", "la reina del flow"}


def _collection_like_manual(value: str) -> bool:
    normalized = _fold(value)
    if re.search(r"\b(?:collection|coleccion|saga|pack|trilogia|tetralogia|filmografia)\b", normalized):
        return True
    if re.search(r"\b\d+\s*(?:movies|peliculas|films)\b", normalized):
        return True
    if re.search(r"\bparte\s+\d+\s+de\s+\d+\b", normalized):
        return True
    return bool(re.search(r"(?<!\d)(?:19|20)\d{2}\s*(?:-|/|\ba\b|\bto\b)\s*(?:19|20)\d{2}(?!\d)", value, flags=re.IGNORECASE))


def _title_candidates(cleaned: str, year: Optional[int], tv: Dict[str, object]) -> List[str]:
    title = _title_from_cleaned(cleaned, year, tv)
    candidates: List[str] = []
    if not title:
        return candidates
    outer, inner = _split_parenthesized_title(title)
    for value in (outer, inner, title):
        _append_unique(candidates, value)
    return candidates


def _split_parenthesized_title(title: str) -> Tuple[str, str]:
    match = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", title)
    if not match:
        return title, ""
    outer = match.group(1).strip()
    inner = match.group(2).strip()
    if YEAR_RE.fullmatch(inner):
        return outer, ""
    return outer, inner


def _title_prefix_before_year(text: str, year: int) -> str:
    match = re.search(rf"(?<!\d){year}(?!\d)", text)
    if not match:
        return ""
    prefix = text[: match.start()]
    prefix = re.sub(r"\s*[\[(]\s*$", "", prefix)
    prefix = _strip_title_tail_noise(prefix)
    return prefix if _fold(prefix) else ""


def _strip_title_tail_noise(text: str) -> str:
    current = re.sub(r"\s+", " ", text or "").strip(" -_.,")
    current = _trim_unbalanced_parentheses(current)
    for _ in range(4):
        updated = re.sub(
            r"(?i)(?:\s+|[-_.])\b(?:pm|ts|hdts|hdtc|tc|cam|hdcam|"
            r"telesync|telecine|screener|dvdscreener|workprint|line|"
            r"proper|repack)\b\s*$",
            "",
            current,
        ).strip(" -_.,")
        updated = _trim_unbalanced_parentheses(updated)
        if updated == current:
            break
        current = updated
    return current


def _trim_unbalanced_parentheses(text: str) -> str:
    current = text.strip(" -_.,")
    if current.count("(") > current.count(")"):
        current = re.sub(r"\s*\([^()]*$", "", current).strip(" -_.,")
    if current.count(")") > current.count("("):
        current = re.sub(r"\s*\)+\s*$", "", current).strip(" -_.,")
    return current


def _title_from_cleaned(cleaned: str, year: Optional[int], tv: Dict[str, object]) -> str:
    text = cleaned
    if year:
        prefix = _title_prefix_before_year(text, year)
        if prefix:
            text = prefix
        else:
            text = re.sub(rf"\s*[\[(]\s*{year}\s*[\])]\s*", " ", text, count=1)
            text = re.sub(rf"(?<!\d){year}(?!\d)", " ", text, count=1)
    text = re.sub(r"\(\s*\)", " ", text)
    text = _remove_tv_tokens(text)
    text = re.sub(r"(?i)\b(?:4k)?web(?:rip|dl)\d{3,4}p?\b", " ", text)
    text = _strip_title_tail_noise(text)
    marker = TECH_TOKENS_RE.search(text)
    if marker:
        text = text[: marker.start()]
    text = re.sub(r"\b(?:cast|latino|spanish|español|espanol)\b", " ", text, flags=re.IGNORECASE)
    text = _strip_title_tail_noise(text)
    text = re.sub(r"\s+", " ", text)
    return _smart_title(text.strip(" -_.,"))


def _remove_tv_tokens(text: str) -> str:
    text = re.sub(r"(?i)\bS0?\d{1,2}\s*E0?\d{1,3}(?:\s*(?:-|_|E)\s*0?\d{1,3})?\b", " ", text)
    text = re.sub(r"(?i)\b\d{1,2}x0?\d{1,3}(?:\s*(?:-|_)\s*0?\d{1,3})?\b", " ", text)
    text = re.sub(r"(?i)\b(?:temporada|season)\s*0?\d{1,2}\b", " ", text)
    text = re.sub(r"(?i)(?:^|\s)T0?\d{1,2}(?:\b|[- ]|$)", " ", text)
    text = re.sub(r"(?i)\bcap(?:[íi]tulo)?\.?\s*0?\d{1,4}(?:\s*(?:-|_)\s*0?\d{1,4})?\b", " ", text)
    text = re.sub(r"(?i)\b(?:episode|episodio)\s*0?\d{1,3}\b", " ", text)
    text = re.sub(r"(?i)\b(?:completa|complete|extras)\b", " ", text)
    return text


def _guessit_input(title: str, year: Optional[int], tv: Dict[str, object]) -> str:
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


def _smart_title(value: str) -> str:
    text = value.replace("...", " ... ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    return text


def _append_unique(values: List[str], value: str) -> None:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" -_.,")
    if not text:
        return
    key = _fold(text)
    if key and key not in {_fold(item) for item in values}:
        values.append(text)


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text.casefold()))
