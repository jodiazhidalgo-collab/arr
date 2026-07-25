"""Validacion estricta del bloque editable del parser."""

from __future__ import annotations

import re
from typing import Dict

from ..parser_rules import factory_parser_rules
from .common import (
    EXTENSION_RE,
    TLD_RE,
    IdentityRulesValidationError,
    boolean,
    choice,
    exact_keys,
    expect_object,
    integer,
    ocr_replacements,
    regex,
    string_list,
    text,
)


def normalize_parser(value: object) -> Dict[str, object]:
    parser = expect_object(value, "rules.parser")
    defaults = factory_parser_rules()
    exact_keys(parser, set(defaults), "rules.parser")

    year = expect_object(parser.get("year"), "rules.parser.year")
    exact_keys(year, set(defaults["year"]), "rules.parser.year")
    minimum_year = integer(year.get("min"), "rules.parser.year.min", 1800, 2200)
    maximum_year = integer(year.get("max"), "rules.parser.year.max", 1800, 2200)
    if minimum_year > maximum_year:
        raise IdentityRulesValidationError("rules.parser.year.min no puede superar max.")

    patterns = expect_object(parser.get("patterns"), "rules.parser.patterns")
    pattern_keys = list(defaults["patterns"])
    exact_keys(patterns, set(pattern_keys), "rules.parser.patterns")

    normalization = expect_object(
        parser.get("normalization"), "rules.parser.normalization"
    )
    normalization_defaults = defaults["normalization"]
    exact_keys(
        normalization,
        set(normalization_defaults),
        "rules.parser.normalization",
    )
    normalized_normalization: Dict[str, object] = {}
    for key, default in normalization_defaults.items():
        label = f"rules.parser.normalization.{key}"
        if isinstance(default, bool):
            normalized_normalization[key] = boolean(normalization.get(key), label)
        elif isinstance(default, int):
            normalized_normalization[key] = integer(
                normalization.get(key), label, 0, 1000
            )
        elif isinstance(default, list):
            normalized_normalization[key] = string_list(
                normalization.get(key), label, lowercase=True
            )
        elif isinstance(default, str):
            normalized_normalization[key] = text(normalization.get(key), label)
        else:
            raise IdentityRulesValidationError(
                f"{label} usa un tipo de default no soportado."
            )

    normalized: Dict[str, object] = {
        "extensions": string_list(
            parser.get("extensions"),
            "rules.parser.extensions",
            lowercase=True,
            validator=EXTENSION_RE,
        ),
        "site_words": string_list(
            parser.get("site_words"), "rules.parser.site_words", lowercase=True
        ),
        "domain_tlds": string_list(
            parser.get("domain_tlds"),
            "rules.parser.domain_tlds",
            lowercase=True,
            validator=TLD_RE,
        ),
        "technical_tokens": string_list(
            parser.get("technical_tokens"),
            "rules.parser.technical_tokens",
            lowercase=True,
        ),
        "tail_noise_tokens": string_list(
            parser.get("tail_noise_tokens"),
            "rules.parser.tail_noise_tokens",
            lowercase=True,
        ),
        "language_tokens": string_list(
            parser.get("language_tokens"),
            "rules.parser.language_tokens",
            lowercase=True,
        ),
        "ocr_replacements": ocr_replacements(
            parser.get("ocr_replacements"), "rules.parser.ocr_replacements"
        ),
        "manual_keywords": string_list(
            parser.get("manual_keywords"),
            "rules.parser.manual_keywords",
            lowercase=True,
        ),
        "manual_exact_names": string_list(
            parser.get("manual_exact_names"),
            "rules.parser.manual_exact_names",
            lowercase=True,
        ),
        "collection_keywords": string_list(
            parser.get("collection_keywords"),
            "rules.parser.collection_keywords",
            lowercase=True,
        ),
        "season_pack_markers": string_list(
            parser.get("season_pack_markers"),
            "rules.parser.season_pack_markers",
            lowercase=True,
        ),
        "year": {
            "pattern": regex(year.get("pattern"), "rules.parser.year.pattern"),
            "min": minimum_year,
            "max": maximum_year,
            "multiple": choice(
                year.get("multiple"),
                "rules.parser.year.multiple",
                ("first", "last", "manual"),
            ),
        },
        "patterns": {
            key: _pattern(patterns.get(key), key) for key in pattern_keys
        },
        "normalization": normalized_normalization,
    }
    return normalized


def _pattern(value: object, key: str) -> str:
    label = f"rules.parser.patterns.{key}"
    template = text(value, label, maximum=2000)
    compiled_value = (
        template.replace("{domain_tlds}", "com|net")
        if key == "domain"
        else template
    )
    normalized = regex(compiled_value, label)
    required_groups = {
        "series_sxe": 2,
        "series_x": 2,
        "explicit_season": 1,
        "season_pack": 1,
        "chapter": 1,
        "episode_word": 1,
        "parenthesized_title": 2,
    }.get(key, 0)
    if required_groups and re.compile(normalized, re.IGNORECASE).groups < required_groups:
        raise IdentityRulesValidationError(
            f"{label} requiere al menos {required_groups} grupos capturados."
        )
    return template
