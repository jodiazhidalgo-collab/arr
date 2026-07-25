"""Normalizacion textual compartida por busqueda, puntuacion y validacion."""

import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ...filesystem import MEDIA_EXTENSIONS


def normalize_title(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(char for char in ascii_value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value.casefold()))


def prefer_parser_title(parser_title: str, guessit_title: str) -> bool:
    parser_norm = normalize_title(parser_title)
    guessit_norm = normalize_title(guessit_title)
    if not parser_norm or not guessit_norm or parser_norm == guessit_norm:
        return False
    parser_tokens = parser_norm.split()
    guessit_tokens = guessit_norm.split()
    return len(parser_tokens) > len(guessit_tokens) and set(guessit_tokens).issubset(parser_tokens)


def search_query_variants(values: Sequence[str], *, cleanup_passes: int = 4) -> List[str]:
    base = unique(values)
    expanded: List[str] = []
    for value in base:
        expanded.append(value)
        stripped = strip_query_tail_noise(value, passes=cleanup_passes)
        if stripped != value:
            expanded.append(stripped)
        expanded.extend(spanish_missing_c_variants(value))
        if stripped != value:
            expanded.extend(spanish_missing_c_variants(stripped))
    return unique(expanded)


def strip_query_tail_noise(value: str, *, passes: int = 4) -> str:
    current = re.sub(r"\s+", " ", value or "").strip(" -_.,")
    for _ in range(max(0, int(passes))):
        updated = re.sub(
            r"(?i)(?:\s+|[-_.])\b(?:pm|ts|hdts|hdtc|tc|cam|hdcam|"
            r"telesync|telecine|screener|dvdscreener|workprint|line|"
            r"proper|repack)\b\s*$",
            "",
            current,
        ).strip(" -_.,")
        if updated == current:
            break
        current = updated
    return current


def spanish_missing_c_variants(value: str) -> List[str]:
    variants: List[str] = []
    words = str(value or "").split()
    for index, word in enumerate(words):
        if re.search(r"(?i)[a-z]{5,}acion$", word) and not re.search(r"(?i)ccion$", word):
            updated = list(words)
            updated[index] = re.sub(r"(?i)acion$", "accion", word)
            variants.append(" ".join(updated))
    return unique(variants)


def clean_release_name(value: str) -> str:
    path = Path(value)
    text = path.stem if path.suffix.lower() in MEDIA_EXTENSIONS else value
    marker = re.search(
        r"(?i)(?:4k|2160p?|1080p?|720p?|webrip|web[-_. ]?dl|bluray|brrip|"
        r"remux|microhd|dvdrip|uhd|hdr|x26[45]|h26[45])",
        text,
    )
    if marker:
        prefix = text[: marker.start()].strip(" ._-[]()")
        if len(normalize_title(prefix).split()) >= 2:
            text = prefix
    text = re.sub(r"(?i)\b(?:www\.)?[a-z0-9-]+\.(?:com|net|org|li|tv|bz)\b", " ", text)
    return " ".join(text.replace("_", " ").replace(".", " ").split())


def split_output_name(value: str) -> Tuple[str, Optional[int]]:
    match = re.match(r"^(.*?)\s*\((\d{4})\)\s*$", value.strip())
    if not match:
        return value, None
    return match.group(1).strip(), int(match.group(2))


def date_year(value: object) -> Optional[int]:
    match = re.match(r"^(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def as_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return as_int(value[0]) if value else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_int_list(value: object) -> List[int]:
    values = value if isinstance(value, list) else ([] if value is None else [value])
    return [number for item in values if (number := as_int(item)) is not None]


def unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def json_safe(value: Dict[str, object]) -> Dict[str, object]:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
