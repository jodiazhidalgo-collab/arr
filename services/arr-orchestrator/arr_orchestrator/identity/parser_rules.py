import re
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Iterable, Mapping, Optional


_PARSER_RULES_TEMPLATE: Dict[str, Any] = {
    "extensions": [
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
    ],
    "site_words": [
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
    ],
    "domain_tlds": ["com", "net", "org", "li", "tv", "bz"],
    # Etiquetas literales editables; las variantes expanden los regex históricos.
    "technical_tokens": [
        "4k",
        "2160",
        "2160p",
        "1080",
        "1080p",
        "720",
        "720p",
        "576",
        "576p",
        "480",
        "480p",
        "uhd",
        "hdr",
        "hdr10",
        "dv",
        "dovi",
        "bluray",
        "blu-ray",
        "bdrip",
        "bdremux",
        "remux",
        "webdl",
        "web-dl",
        "web dl",
        "webrip",
        "hdtv",
        "dvdrip",
        "hdrip",
        "microhd",
        "cam",
        "hdcam",
        "ts",
        "hdts",
        "tc",
        "hdtc",
        "telesync",
        "telecine",
        "screener",
        "dvdscreener",
        "workprint",
        "line",
        "amzn",
        "nf",
        "netflix",
        "hmax",
        "dsnp",
        "itunes",
        "ac3",
        "eac3",
        "dts",
        "dts-hd",
        "truehd",
        "x264",
        "x265",
        "h264",
        "h265",
        "hevc",
        "avc",
        "aac",
        "dd1",
        "dd.1",
        "dd51",
        "dd5.1",
        "ddp1",
        "ddp.1",
        "ddp51",
        "ddp5.1",
        "castellano",
        "spanish",
        "dual",
        "sub",
        "subs",
        "es-en",
        "multi",
        "proper",
        "repack",
    ],
    "tail_noise_tokens": [
        "pm",
        "ts",
        "hdts",
        "hdtc",
        "tc",
        "cam",
        "hdcam",
        "telesync",
        "telecine",
        "screener",
        "dvdscreener",
        "workprint",
        "line",
        "proper",
        "repack",
    ],
    "language_tokens": ["cast", "latino", "spanish", "español", "espanol"],
    "ocr_replacements": [
        {"pattern": r"\b1[o0]8[o0]p\b", "replacement": "1080p"},
        {"pattern": r"\b72[o0]p\b", "replacement": "720p"},
        {"pattern": r"\b2[1l]6[o0]p\b", "replacement": "2160p"},
        {"pattern": r"\b4[l1i]k\b", "replacement": "4k"},
        {"pattern": r"\b4kk\b", "replacement": "4k"},
    ],
    "manual_keywords": ["lynda", "course", "collection", "linux", "ubuntu", "shell", "cli"],
    "manual_exact_names": ["wasabi", "doraemon", "bluey", "la reina del flow"],
    "collection_keywords": [
        "collection",
        "coleccion",
        "saga",
        "pack",
        "trilogia",
        "tetralogia",
        "filmografia",
    ],
    "season_pack_markers": ["completa", "complete", "extras"],
    "year": {
        "pattern": r"(?<!\d)((?:19|20)\d{2})(?!\d)",
        "min": 1900,
        "max": 2099,
        "multiple": "first",
    },
    "patterns": {
        "series_sxe": r"(?i)\bS0?(\d{1,2})\s*E0?(\d{1,3})(?:\s*(?:-|_|E)\s*0?(\d{1,3}))?\b",
        "series_x": r"(?i)\b(\d{1,2})x0?(\d{1,3})(?:\s*(?:-|_)\s*0?(\d{1,3}))?\b",
        "explicit_season": r"(?i)\b(?:temporada|season)\s*0?(\d{1,2})\b",
        "season_pack": r"(?i)(?:^|\s)T0?(\d{1,2})(?:\b|[- ]|$)",
        "chapter": r"(?i)\bcap(?:[íi]tulo)?\.?\s*0?(\d{1,4})(?:\s*(?:-|_)\s*0?(\d{1,4}))?\b",
        "episode_word": r"(?i)\b(?:episode|episodio)\s*0?(\d{1,3})\b",
        "collection_count": r"\b\d+\s*(?:movies|peliculas|films)\b",
        "collection_part": r"\bparte\s+\d+\s+de\s+\d+\b",
        "year_range": r"(?<!\d)(?:19|20)\d{2}\s*(?:-|/|\ba\b|\bto\b)\s*(?:19|20)\d{2}(?!\d)",
        "domain": r"\b[a-z0-9-]+\.(?:{domain_tlds})\b",
        "parenthesized_title": r"^(.*?)\s*\(([^()]+)\)\s*$",
        "compact_web": r"(?i)\b(?:4k)?web(?:rip|dl)\d{3,4}p?\b",
    },
    "normalization": {
        "strip_extension": True,
        "strip_duplicate_suffix": True,
        "normalize_ocr": True,
        "normalize_dashes": True,
        "replace_dots_underscores": True,
        "strip_brackets": True,
        "collapse_whitespace": True,
        "smart_title": True,
        "tail_noise_passes": 4,
        # 0 reproduce el comportamiento histórico: no limita el rango.
        "max_episode_range": 0,
    },
}

DEFAULT_PARSER_RULES: Dict[str, Any] = deepcopy(_PARSER_RULES_TEMPLATE)


_ALIASES = {
    "known_extensions": "extensions",
    "tech_tokens": "technical_tokens",
    "tech_token_patterns": "technical_tokens",
    "manual_exact_titles": "manual_exact_names",
}


def factory_parser_rules() -> Dict[str, Any]:
    return deepcopy(_PARSER_RULES_TEMPLATE)


def resolve_parser_rules(rules: Any = None, config: Any = None) -> Dict[str, Any]:
    """Combina defaults con config global y reglas explícitas, sin dependencia circular."""

    merged = factory_parser_rules()
    # La regla explícita manda sobre la configuración persistida.
    for source in (config, rules):
        block = _parser_block(source)
        if block:
            _deep_merge(merged, block)
    _normalize_in_place(merged)
    return merged


def parser_pattern(rules: Mapping[str, Any], name: str, fallback: str = "") -> str:
    patterns = rules.get("patterns")
    if isinstance(patterns, Mapping):
        value = patterns.get(name, fallback)
        return str(value or fallback)
    return fallback


def regex_items(values: Iterable[Any]) -> str:
    patterns = []
    for value in values or []:
        if isinstance(value, Mapping):
            text = str(value.get("pattern") or "").strip()
            if text:
                patterns.append(text)
            continue
        text = str(value or "").strip()
        if text:
            patterns.append(re.escape(text))
    return "|".join(patterns)


def _parser_block(source: Any) -> Dict[str, Any]:
    value = _as_mapping(source)
    if not value:
        return {}
    if "parser" in value:
        return _as_mapping(value.get("parser"))
    for container in ("identity", "rules", "config"):
        nested = _as_mapping(value.get(container))
        if nested and "parser" in nested:
            return _as_mapping(nested.get("parser"))
    return value


def _as_mapping(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return dict(asdict(value))
    for method_name in ("to_dict", "model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            candidate = method()
            if isinstance(candidate, Mapping):
                return dict(candidate)
    parser = getattr(value, "parser", None)
    if parser is not None:
        return {"parser": parser}
    return {}


def _deep_merge(target: Dict[str, Any], override: Mapping[str, Any]) -> None:
    for raw_key, value in override.items():
        key = _ALIASES.get(str(raw_key), str(raw_key))
        if isinstance(value, Mapping) and isinstance(target.get(key), Mapping):
            nested = dict(target[key])
            _deep_merge(nested, value)
            target[key] = nested
        else:
            target[key] = deepcopy(value)


def _normalize_in_place(rules: Dict[str, Any]) -> None:
    extensions = []
    for value in rules.get("extensions") or []:
        text = str(value or "").strip().lower()
        if text:
            extensions.append(text if text.startswith(".") else f".{text}")
    rules["extensions"] = extensions
    for key in (
        "site_words",
        "domain_tlds",
        "technical_tokens",
        "tail_noise_tokens",
        "language_tokens",
        "manual_keywords",
        "manual_exact_names",
        "collection_keywords",
        "season_pack_markers",
    ):
        values = rules.get(key)
        if isinstance(values, str):
            values = [values]
        rules[key] = list(values or [])
    if not isinstance(rules.get("ocr_replacements"), list):
        rules["ocr_replacements"] = deepcopy(_PARSER_RULES_TEMPLATE["ocr_replacements"])
    for key in ("year", "patterns", "normalization"):
        if not isinstance(rules.get(key), Mapping):
            rules[key] = deepcopy(_PARSER_RULES_TEMPLATE[key])
        else:
            rules[key] = dict(rules[key])
