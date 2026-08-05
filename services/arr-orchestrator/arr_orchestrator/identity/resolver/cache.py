"""Contrato estable y autocontenido de la caché del resolver v2."""

import copy
import hashlib
import json
from typing import Dict, Optional, Sequence

from .text import json_safe


RESOLVER_CACHE_VERSION = 5
RESOLVER_ALGORITHM_VERSION = "phased-er-v2"
CACHE_PAYLOAD_FORMAT = "arr-resolver-cache-v2"


def cache_key(
    media_type: str,
    evidence: Sequence[str],
    guessed: Dict[str, object],
    tmdb_id: Optional[str],
    imdb_id: Optional[str],
    forced_tmdb_id: Optional[str] = None,
    resolution_fingerprint: str = "",
    runtime_evidence: Sequence[Dict[str, object]] = (),
    media_manifest: Sequence[Dict[str, object]] = (),
) -> str:
    normalized_runtimes = sorted(
        (json_safe(dict(item)) for item in runtime_evidence if isinstance(item, dict)),
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )
    normalized_manifest = sorted(
        (json_safe(dict(item)) for item in media_manifest if isinstance(item, dict)),
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )
    payload = json.dumps(
        {
            "resolver_cache_version": RESOLVER_CACHE_VERSION,
            "resolver_algorithm_version": RESOLVER_ALGORITHM_VERSION,
            "media_type": media_type,
            "evidence": list(evidence),
            "guess": json_safe(guessed),
            "tmdb_id": tmdb_id,
            "imdb_id": imdb_id,
            "forced_tmdb_id": forced_tmdb_id,
            "resolution_fingerprint": resolution_fingerprint,
            "runtime_evidence": normalized_runtimes,
            "media_manifest": normalized_manifest,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def encode_cache_payload(
    identity: Dict[str, object], decision: Dict[str, object]
) -> str:
    """Serializa identidad y decisión completa sin campos ejecutables v1."""

    identity_payload = copy.deepcopy(json_safe(identity))
    decision_payload = copy.deepcopy(json_safe(decision))
    if not isinstance(identity_payload, dict) or not isinstance(decision_payload, dict):
        raise ValueError("Payload de cache v2 no valido")
    if _contains_legacy_scoring(identity_payload) or _contains_legacy_scoring(
        decision_payload
    ):
        raise ValueError("La cache v2 no admite score, margen ni breakdown")
    if (
        str(identity_payload.get("resolver_algorithm_version") or "")
        != RESOLVER_ALGORITHM_VERSION
    ):
        raise ValueError("La identidad de cache no pertenece a phased-er-v2")
    if str(decision_payload.get("status") or "") not in {
        "ACCEPTED_CONFIDENT",
        "ACCEPTED_FALLBACK",
    }:
        raise ValueError("Solo se cachean decisiones aceptadas")
    decision_payload["resolver_algorithm_version"] = RESOLVER_ALGORITHM_VERSION
    decision_payload["has_scoring"] = False
    return json.dumps(
        {
            "format": CACHE_PAYLOAD_FORMAT,
            "resolver_cache_version": RESOLVER_CACHE_VERSION,
            "resolver_algorithm_version": RESOLVER_ALGORITHM_VERSION,
            "identity": identity_payload,
            "decision": decision_payload,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def decode_cache_payload(raw: object) -> Optional[Dict[str, object]]:
    """Lee exclusivamente el contrato v5; cualquier residuo anterior es miss."""

    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("format") != CACHE_PAYLOAD_FORMAT:
        return None
    try:
        payload_version = int(payload.get("resolver_cache_version") or 0)
    except (TypeError, ValueError):
        return None
    if payload_version != RESOLVER_CACHE_VERSION:
        return None
    if payload.get("resolver_algorithm_version") != RESOLVER_ALGORITHM_VERSION:
        return None
    identity = payload.get("identity")
    decision = payload.get("decision")
    if not isinstance(identity, dict) or not isinstance(decision, dict):
        return None
    if _contains_legacy_scoring(identity) or _contains_legacy_scoring(decision):
        return None
    if identity.get("resolver_algorithm_version") != RESOLVER_ALGORITHM_VERSION:
        return None
    status = str(decision.get("status") or "")
    if status not in {"ACCEPTED_CONFIDENT", "ACCEPTED_FALLBACK"}:
        return None
    if decision.get("accepted") is not True:
        return None
    try:
        identity_id = int(identity.get("tmdb_id") or 0)
        selected_id = int(decision.get("selected_tmdb_id") or 0)
    except (TypeError, ValueError):
        return None
    if identity_id <= 0 or selected_id != identity_id:
        return None
    alternatives = decision.get("alternatives")
    if not isinstance(alternatives, list) or not any(
        _matches_tmdb_id(item, identity_id)
        for item in alternatives
    ):
        return None
    return {
        "identity": copy.deepcopy(identity),
        "decision": copy.deepcopy(decision),
    }


def _contains_legacy_scoring(value: object) -> bool:
    forbidden = {"score", "margin", "breakdown", "scoring"}
    if isinstance(value, dict):
        if any(str(key).casefold() in forbidden for key in value):
            return True
        return any(_contains_legacy_scoring(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_legacy_scoring(item) for item in value)
    return False


def _matches_tmdb_id(value: object, tmdb_id: int) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        return int(value.get("tmdb_id") or 0) == tmdb_id
    except (TypeError, ValueError):
        return False
