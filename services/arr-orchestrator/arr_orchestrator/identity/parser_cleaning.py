import re
import unicodedata
from pathlib import Path
from typing import Any, List, Mapping, Optional

from .parser_rules import parser_pattern
from .parser_trace import ParserTrace


def preclean(value: str, rules: Mapping[str, Any], trace: Optional[ParserTrace] = None) -> str:
    normalization = _normalization(rules)
    original = str(value or "")
    text = release_leaf_name(original, rules).strip()
    _record(trace, "clean.basename", original, text)

    suffix = Path(text).suffix.lower()
    extensions = {str(item).lower() for item in rules.get("extensions") or []}
    if normalization.get("strip_extension", True) and suffix in extensions:
        text = _replace(trace, "clean.extension", text, text[: -len(suffix)])

    if normalization.get("strip_duplicate_suffix", True):
        text = _sub(trace, "clean.timestamp_suffix", r"__\d{8,}$", "", text)
        while True:
            new = re.sub(r"\s*\(\d{1,2}\)\s*$", "", text).strip()
            if new == text:
                break
            text = _replace(trace, "clean.duplicate_suffix", text, new)

    text = _replace(
        trace,
        "clean.apostrophes",
        text,
        text.replace("`", " ").replace("´", " ").replace("’", "'"),
    )
    if normalization.get("normalize_dashes", True):
        text = _sub(trace, "clean.dashes", r"[–—]+", "-", text)
    if normalization.get("normalize_ocr", True):
        text = normalize_release_ocr_tokens(text, rules, trace, "clean.ocr.initial")
    text = normalize_season_number_words(text, rules, trace)

    tlds = "|".join(re.escape(str(item)) for item in rules.get("domain_tlds") or [] if str(item))
    if tlds:
        prefix_pattern = rf"(?i)\bwww\.[a-z0-9-]+\.(?:{tlds})\s*[-_]*"
        text = _sub(trace, "clean.domain_www", prefix_pattern, " ", text)
    domain_pattern = parser_pattern(rules, "domain")
    if domain_pattern:
        if "{domain_tlds}" in domain_pattern:
            domain_pattern = domain_pattern.replace("{domain_tlds}", tlds) if tlds else ""
        if domain_pattern:
            text = _sub(
                trace,
                "clean.domain",
                domain_pattern,
                " ",
                text,
                flags=re.IGNORECASE,
            )

    for word in rules.get("site_words") or []:
        escaped = re.escape(str(word or ""))
        if escaped:
            text = _sub(trace, f"clean.site_word:{word}", rf"(?i)\b{escaped}\b", " ", text)

    text = _sub(
        trace,
        "clean.sxe_range_separator",
        r"(?i)\b(S\d{1,2}\s*E\d{1,3})[_-](\d{1,3})\b",
        r"\1-\2",
        text,
    )
    text = _sub(
        trace,
        "clean.x_range_separator",
        r"(?i)\b(\d{1,2}x\d{1,3})[_-](\d{1,3})\b",
        r"\1-\2",
        text,
    )
    text = _sub(
        trace,
        "clean.chapter_range_separator",
        r"(?i)\b(cap(?:[íi]tulo)?\.?\s*\d{1,4})[_-](\d{1,4})\b",
        r"\1-\2",
        text,
    )
    text = _sub(trace, "clean.letter_before_x", r"(?i)([a-záéíóúñ])(\d+x\d+)", r"\1 \2", text)
    text = _sub(
        trace,
        "clean.word_number_spacing",
        r"(?i)\b(temporada|season|temp|sezon|capitulo|capítulo|episode|episodio|cap)\s*([0-9])",
        r"\1 \2",
        text,
    )
    before_t = text
    text = re.sub(r"(?i)\bT\s*([0-9]{1,2})\b", lambda match: f"T{int(match.group(1)):02d}", text)
    _record(trace, "clean.season_pack_padding", before_t, text)

    if normalization.get("replace_dots_underscores", True):
        text = _sub(trace, "clean.dots_underscores", r"[._]+", " ", text)
    if normalization.get("strip_brackets", True):
        text = _sub(trace, "clean.brackets", r"[\[\]{}]+", " ", text)
    text = _sub(trace, "clean.hyphen_spacing", r"\s*-\s*", " - ", text)
    if normalization.get("normalize_ocr", True):
        text = normalize_release_ocr_tokens(text, rules, trace, "clean.ocr.final")
    if normalization.get("collapse_whitespace", True):
        text = _sub(trace, "clean.whitespace", r"\s+", " ", text)
    text = _replace(trace, "clean.trim", text, text.strip(" -_.,"))
    return text


def release_leaf_name(value: str, rules: Mapping[str, Any]) -> str:
    """Quita una ruta real sin confundir separadores internos del release.

    Los nombres de torrent pueden contener ``Titulo / Original`` o cadenas de
    idiomas como ``CZ/SK/EN``. Solo una ruta absoluta/explicita, o una relativa
    terminada en una extension conocida, se reduce a su ultimo componente.
    """

    text = str(value or "").strip()
    # Algunos nombres escapados llegan como ``Bean\'s``. Esa barra inversa
    # pertenece al apostrofo y no representa un separador de ruta Windows.
    text = text.replace("\\'", "'")
    if not text or not re.search(r"[\\/]", text):
        return text

    absolute_or_explicit = bool(
        re.match(r"^(?:[a-zA-Z]:[\\/]|[/\\]{2}|[/\\]|\.{1,2}[\\/])", text)
    )
    relative_known_extension = _has_known_final_extension(text, rules)
    release_separator = re.search(r"\s/\s", text)
    language_chain = re.search(
        r"(?i)(?:^|[\s._/\-\[(])(?:[a-z]{2,3}/){1,}[a-z]{2,3}(?=$|[\s._\-\])])",
        text,
    )
    protected_release_separator = bool(
        release_separator and not _path_separator_before(text, release_separator.start())
    )
    protected_language_chain = bool(
        language_chain and not _path_separator_before(text, language_chain.start())
    )
    if not absolute_or_explicit and (
        not relative_known_extension
        or protected_release_separator
        or protected_language_chain
    ):
        return text

    return re.split(r"[\\/]", text.rstrip("\\/"))[-1]


def normalize_season_number_words(
    text: str,
    rules: Mapping[str, Any],
    trace: Optional[ParserTrace] = None,
) -> str:
    current = text
    entries = []
    for item in rules.get("season_number_words") or []:
        word, separator, raw_number = str(item or "").partition("|")
        word = word.strip()
        raw_number = raw_number.strip()
        if not separator or not word or not raw_number.isdigit():
            continue
        entries.append((word, int(raw_number)))
    for word, number in sorted(entries, key=lambda entry: len(entry[0]), reverse=True):
        before = current
        current = re.sub(
            rf"(?i)(?<![a-z0-9])(temporada|season|temp|sezon)([\s._-]+){re.escape(word)}\b",
            lambda match: f"{match.group(1)} {number}",
            current,
        )
        _record(trace, f"clean.season_number_word:{word}", before, current)
    return current


def normalize_release_ocr_tokens(
    text: str,
    rules: Mapping[str, Any],
    trace: Optional[ParserTrace] = None,
    rule_prefix: str = "clean.ocr",
) -> str:
    current = text
    for index, replacement in enumerate(rules.get("ocr_replacements") or []):
        if not isinstance(replacement, Mapping):
            continue
        pattern = str(replacement.get("pattern") or "")
        if not pattern:
            continue
        target = str(replacement.get("replacement") or "")
        current = _sub(trace, f"{rule_prefix}.{index}", pattern, target, current, flags=re.IGNORECASE)
    return current


def _has_known_final_extension(text: str, rules: Mapping[str, Any]) -> bool:
    final_component = re.split(r"[\\/]", text.rstrip("\\/"))[-1]
    suffix = Path(final_component).suffix.lower()
    extensions = {str(item or "").strip().lower() for item in rules.get("extensions") or []}
    return bool(suffix and suffix in extensions)


def _path_separator_before(text: str, index: int) -> bool:
    return bool(re.search(r"[\\/]", text[:index]))


def smart_title(value: str, rules: Optional[Mapping[str, Any]] = None) -> str:
    text = str(value or "").replace("...", " ... ")
    if rules is None or _normalization(rules).get("collapse_whitespace", True):
        text = re.sub(r"\s+", " ", text)
    return text.strip()


def append_unique(values: List[str], value: str) -> None:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" -_.,")
    if not text:
        return
    key = fold(text)
    if key and key not in {fold(item) for item in values}:
        values.append(text)


def fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text.casefold()))


def _normalization(rules: Mapping[str, Any]) -> Mapping[str, Any]:
    value = rules.get("normalization")
    return value if isinstance(value, Mapping) else {}


def _sub(
    trace: Optional[ParserTrace],
    rule: str,
    pattern: str,
    replacement: Any,
    text: str,
    *,
    flags: int = 0,
) -> str:
    before = text
    after = re.sub(pattern, replacement, before, flags=flags)
    _record(trace, rule, before, after)
    return after


def _replace(trace: Optional[ParserTrace], rule: str, before: str, after: str) -> str:
    _record(trace, rule, before, after)
    return after


def _record(trace: Optional[ParserTrace], rule: str, before: Any, after: Any) -> None:
    if trace is not None:
        trace.record(rule, before, after, changed_only=True)
