"""Representacion segura del titulo de origen fuera de ``source_meta_json``.

El texto validado del buscador solo se conserva en ``source_contexts``. El
resto de artefactos duraderos trabaja con una huella estable y la procedencia.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from typing import Iterable, Mapping


SOURCE_TITLE_KEYS = {"source_title", "_source_context_title"}


def source_title_fingerprint(value: object) -> str:
    normalized = " ".join(
        unicodedata.normalize("NFKC", str(value or "")).casefold().split()
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def source_titles_from_meta(value: object) -> list[str]:
    if isinstance(value, Mapping):
        meta = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        meta = parsed if isinstance(parsed, Mapping) else {}
    else:
        return []
    contexts = meta.get("source_contexts")
    if not isinstance(contexts, list):
        return []
    return _unique_titles(
        item.get("source_title")
        for item in contexts
        if isinstance(item, Mapping)
    )


def sanitize_persistent_payload(
    value: object,
    source_titles: Iterable[object] = (),
) -> object:
    """Elimina el texto de origen de un payload que va a persistirse."""

    titles = _unique_titles(
        [*source_titles, *_source_values_embedded_in(value)]
    )
    return _sanitize(value, titles)


def sanitize_persistent_json(
    value: object,
    source_titles: Iterable[object] = (),
) -> object:
    """Sanea un JSON serializado conservando su tipo de entrada."""

    if not isinstance(value, str) or not value.strip():
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _sanitize_text(value, _unique_titles(source_titles))
    sanitized = sanitize_persistent_payload(parsed, source_titles)
    return json.dumps(sanitized, ensure_ascii=False, default=str)


def _sanitize(value: object, titles: list[str]) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in SOURCE_TITLE_KEYS:
                title = str(item or "").strip()
                if title:
                    fingerprint_key = (
                        "source_title_fingerprint"
                        if key == "source_title"
                        else "_source_context_title_fingerprint"
                    )
                    result[fingerprint_key] = source_title_fingerprint(title)
                continue
            result[key] = _sanitize(item, titles)
        return result
    if isinstance(value, list):
        return [_sanitize(item, titles) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, titles) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, titles)
    return copy.deepcopy(value)


def _sanitize_text(value: str, titles: list[str]) -> str:
    result = value
    for title in sorted(titles, key=len, reverse=True):
        if title and title in result:
            marker = f"<source-title:{source_title_fingerprint(title)[:12]}>"
            result = result.replace(title, marker)
    return result


def _source_values_embedded_in(value: object) -> list[str]:
    result: list[str] = []
    if isinstance(value, Mapping):
        has_source_marker = any(str(key) in SOURCE_TITLE_KEYS for key in value)
        for key, item in value.items():
            if str(key) in SOURCE_TITLE_KEYS and isinstance(item, str):
                result.append(item)
            result.extend(_source_values_embedded_in(item))
        if has_source_marker:
            for key in ("query", "title", "raw", "cleaned", "display_title"):
                item = value.get(key)
                if isinstance(item, str):
                    result.append(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.extend(_source_values_embedded_in(item))
    return _unique_titles(result)


def _unique_titles(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        title = str(value or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        result.append(title)
    return result
