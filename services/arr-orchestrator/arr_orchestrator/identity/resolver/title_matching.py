"""Comparacion configurable de titulos y trazabilidad sin puntos propios."""

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ..resolver_defaults import DEFAULT_TITLE_MATCHING
from .text import normalize_title

_ORDINAL_TOKENS = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
    **{str(value): value for value in range(1, 11)},
}

_SINGLE_I_PART_MARKERS = {
    "book",
    "capitulo",
    "chapter",
    "ep",
    "episode",
    "episodio",
    "libro",
    "part",
    "parte",
    "pt",
    "tome",
    "tomo",
    "vol",
    "volume",
    "volumen",
}


@dataclass(frozen=True)
class TitleForm:
    """Una forma comparable y las reglas configurables usadas para crearla."""

    text: str
    source_tokens: Tuple[str, ...]
    has_part_number: bool
    omitted_part_number: bool


@dataclass(frozen=True)
class TitlePairMatch:
    """Un par concreto de formas y las reglas que realmente usa."""

    exact: bool
    ratio: float
    token_overlap: float
    left_value: str
    right_value: str
    used_omitted_part_number: bool = False
    roman_equivalences: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TitleMatch:
    """Mejores pares independientes para cada metrica historica."""

    exact: bool = False
    ratio: float = 0.0
    token_overlap: float = 0.0
    exact_pair: Optional[TitlePairMatch] = None
    ratio_pair: Optional[TitlePairMatch] = None
    token_overlap_pair: Optional[TitlePairMatch] = None


def resolve_title_matching(settings: object = None) -> Dict[str, object]:
    """Completa y sanea una politica parcial sin alterar el documento origen."""

    resolved = dict(DEFAULT_TITLE_MATCHING)
    supplied = settings if isinstance(settings, Mapping) else {}
    for key in (
        "score_parser_candidates",
        "roman_arabic_equivalence",
        "allow_omitted_part_number",
    ):
        if isinstance(supplied.get(key), bool):
            resolved[key] = supplied[key]
    for key in ("omitted_part_min_words", "supplemental_min_chars"):
        value = supplied.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            resolved[key] = max(0, value)
    return resolved


def unique_title_values(values: Sequence[object]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def supplemental_title_candidates(
    values: Sequence[object],
    settings: object = None,
) -> List[str]:
    """Filtra candidatos auxiliares demasiado cortos antes de puntuar."""

    resolved = resolve_title_matching(settings)
    minimum = int(resolved["supplemental_min_chars"])
    result = []
    for value in unique_title_values(values):
        normalized = normalize_title(value)
        if normalized and len(normalized.replace(" ", "")) >= minimum:
            result.append(value)
    return result


def scoring_title_values(
    primary_title: str,
    parser_candidates: Sequence[str],
    settings: object = None,
) -> List[str]:
    resolved = resolve_title_matching(settings)
    values: List[object] = [primary_title]
    if bool(resolved["score_parser_candidates"]):
        values.extend(parser_candidates)
    return unique_title_values(values)


def best_title_match(
    left_values: Sequence[str],
    right_values: Sequence[str],
    settings: object = None,
) -> TitleMatch:
    """Calcula exactitud, similitud y solape sobre todas las formas compatibles."""

    resolved = resolve_title_matching(settings)
    left_forms = [
        (str(value), form)
        for value in left_values
        for form in _title_forms(str(value), resolved)
    ]
    right_forms = [
        (str(value), form)
        for value in right_values
        for form in _title_forms(str(value), resolved)
    ]
    exact_pair: Optional[TitlePairMatch] = None
    ratio_pair: Optional[TitlePairMatch] = None
    overlap_pair: Optional[TitlePairMatch] = None
    for left_value, left in left_forms:
        for right_value, right in right_forms:
            if not _compatible_forms(left, right):
                continue
            pair = _pair_match(
                left_value,
                left,
                right_value,
                right,
                resolved,
            )
            if pair.exact and _prefer_pair(pair, exact_pair, "ratio"):
                exact_pair = pair
            if _prefer_pair(pair, ratio_pair, "ratio"):
                ratio_pair = pair
            if _prefer_pair(pair, overlap_pair, "token_overlap"):
                overlap_pair = pair

    return TitleMatch(
        exact=exact_pair is not None,
        ratio=ratio_pair.ratio if ratio_pair is not None else 0.0,
        token_overlap=(
            overlap_pair.token_overlap if overlap_pair is not None else 0.0
        ),
        exact_pair=exact_pair,
        ratio_pair=ratio_pair,
        token_overlap_pair=overlap_pair,
    )


def matching_rules_for_pairs(
    pairs: Sequence[Optional[TitlePairMatch]],
) -> List[Dict[str, str]]:
    """Expone solo reglas configurables que participaron en la coincidencia."""

    rules: List[Dict[str, str]] = []
    for pair in pairs:
        if pair is None:
            continue
        if pair.roman_equivalences:
            detail = "; ".join(
                f"{roman} = {arabic}"
                for roman, arabic in pair.roman_equivalences
            )
            rules.append(
                {
                    "path": "resolver.title_matching.roman_arabic_equivalence",
                    "detail": detail,
                }
            )
        if pair.used_omitted_part_number:
            rules.append(
                {
                    "path": "resolver.title_matching.allow_omitted_part_number",
                    "detail": "Número de saga omitido",
                }
            )
    return merge_matching_rules(rules)


def parser_candidate_rules_for_pairs(
    pairs: Sequence[Optional[TitlePairMatch]],
    parser_candidates: Sequence[str],
    primary_title: str,
) -> List[Dict[str, str]]:
    """Traza auxiliares solo si un par ganador los usa de verdad."""

    return parser_candidate_rules(
        [pair.left_value for pair in pairs if pair is not None],
        parser_candidates,
        primary_title,
    )


def parser_candidate_rules(
    values: Sequence[str],
    parser_candidates: Sequence[str],
    primary_title: str,
) -> List[Dict[str, str]]:
    primary = normalize_title(primary_title)
    candidates = {
        normalize_title(value): str(value)
        for value in parser_candidates
        if normalize_title(value) and normalize_title(value) != primary
    }
    rules = []
    for value in values:
        normalized = normalize_title(value)
        if normalized not in candidates:
            continue
        rules.append(parser_candidate_rule(candidates[normalized]))
    return merge_matching_rules(rules)


def parser_candidate_rule(value: str) -> Dict[str, str]:
    title = _compact_trace_value(value)
    return {
        "path": "resolver.title_matching.score_parser_candidates",
        "detail": f"Título auxiliar del parser: {title}",
    }


def merge_matching_rules(*groups: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen = set()
    for group in groups:
        for item in group:
            path = str(item.get("path") or "")
            detail = str(item.get("detail") or "")
            key = (path, detail)
            if path and detail and key not in seen:
                seen.add(key)
                result.append({"path": path, "detail": detail})
    return result


def _title_forms(value: str, settings: Mapping[str, object]) -> List[TitleForm]:
    normalized = normalize_title(value)
    if not normalized:
        return []
    tokens = normalized.split()
    part_numbers = [_part_number(tokens, index) for index in range(len(tokens))]
    has_part_number = any(number is not None for number in part_numbers)
    canonical: List[str] = []
    for token, number in zip(tokens, part_numbers):
        if number is not None and token.isalpha() and bool(
            settings["roman_arabic_equivalence"]
        ):
            canonical.append(str(number))
        else:
            canonical.append(token)

    forms = [
        TitleForm(
            " ".join(canonical),
            tuple(tokens),
            has_part_number,
            False,
        )
    ]
    concrete_numbers = [number for number in part_numbers if number is not None]
    without_part_numbers = [
        token
        for token, number in zip(canonical, part_numbers)
        if number is None
    ]
    without_part_sources = [
        token
        for token, number in zip(tokens, part_numbers)
        if number is None
    ]
    if (
        bool(settings["allow_omitted_part_number"])
        and len(concrete_numbers) == 1
        and len(without_part_numbers) >= int(settings["omitted_part_min_words"])
    ):
        forms.append(
            TitleForm(
                " ".join(without_part_numbers),
                tuple(without_part_sources),
                True,
                True,
            )
        )

    expanded: List[TitleForm] = []
    for form in forms:
        expanded.append(form)
        form_tokens = form.text.split()
        for index, token in enumerate(form_tokens):
            if token != "s" or index == 0:
                continue
            joined = [*form_tokens]
            joined[index - 1 : index + 1] = [f"{form_tokens[index - 1]}s"]
            joined_sources = list(form.source_tokens)
            joined_sources[index - 1 : index + 1] = [
                f"{form.source_tokens[index - 1]}s"
            ]
            expanded.append(
                TitleForm(
                    " ".join(joined),
                    tuple(joined_sources),
                    form.has_part_number,
                    form.omitted_part_number,
                )
            )
    return list(dict.fromkeys(expanded))


def _compatible_forms(left: TitleForm, right: TitleForm) -> bool:
    if left.omitted_part_number or right.omitted_part_number:
        return (
            left.omitted_part_number != right.omitted_part_number
            and left.has_part_number != right.has_part_number
        )
    return True


def _part_number(tokens: Sequence[str], index: int) -> Optional[int]:
    token = tokens[index]
    number = _ORDINAL_TOKENS.get(token)
    if token != "i" or number is None:
        return number
    neighbours = {
        tokens[position]
        for position in (index - 1, index + 1)
        if 0 <= position < len(tokens)
    }
    return number if neighbours & _SINGLE_I_PART_MARKERS else None


def _pair_match(
    left_value: str,
    left: TitleForm,
    right_value: str,
    right: TitleForm,
    settings: Mapping[str, object],
) -> TitlePairMatch:
    left_tokens = set(left.text.split())
    right_tokens = set(right.text.split())
    omitted = left.omitted_part_number or right.omitted_part_number
    roman_equivalences = (
        _aligned_roman_equivalences(left, right)
        if bool(settings["roman_arabic_equivalence"]) and not omitted
        else ()
    )
    return TitlePairMatch(
        exact=left.text == right.text,
        ratio=SequenceMatcher(None, left.text, right.text).ratio(),
        token_overlap=(
            len(left_tokens & right_tokens)
            / max(1, len(left_tokens | right_tokens))
        ),
        left_value=left_value,
        right_value=right_value,
        used_omitted_part_number=omitted,
        roman_equivalences=roman_equivalences,
    )


def _prefer_pair(
    candidate: TitlePairMatch,
    current: Optional[TitlePairMatch],
    metric: str,
) -> bool:
    if current is None:
        return True
    candidate_value = float(getattr(candidate, metric))
    current_value = float(getattr(current, metric))
    if candidate_value != current_value:
        return candidate_value > current_value
    return _pair_rule_count(candidate) < _pair_rule_count(current)


def _pair_rule_count(pair: TitlePairMatch) -> int:
    return int(pair.used_omitted_part_number) + len(pair.roman_equivalences)


def _aligned_roman_equivalences(
    left: TitleForm,
    right: TitleForm,
) -> Tuple[Tuple[str, str], ...]:
    left_tokens = left.text.split()
    right_tokens = right.text.split()
    equivalences: List[Tuple[str, str]] = []
    matcher = SequenceMatcher(None, left_tokens, right_tokens)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            left_index = block.a + offset
            right_index = block.b + offset
            canonical = left_tokens[left_index]
            left_source = left.source_tokens[left_index]
            right_source = right.source_tokens[right_index]
            if left_source == right_source or not canonical.isdigit():
                continue
            left_number = _ORDINAL_TOKENS.get(left_source)
            right_number = _ORDINAL_TOKENS.get(right_source)
            if left_number is None or left_number != right_number:
                continue
            roman = (
                left_source
                if left_source.isalpha()
                else right_source
                if right_source.isalpha()
                else ""
            )
            if roman:
                equivalences.append((roman.upper(), str(left_number)))
    return tuple(dict.fromkeys(equivalences))


def _compact_trace_value(value: object, limit: int = 80) -> str:
    text = "".join(
        character if character.isprintable() else " "
        for character in str(value or "")
    )
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"
