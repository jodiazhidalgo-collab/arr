"""Identidad estable de la caché del resolver."""

import hashlib
import json
from typing import Dict, Optional, Sequence

from .text import json_safe


RESOLVER_CACHE_VERSION = 3


def cache_key(
    media_type: str,
    evidence: Sequence[str],
    guessed: Dict[str, object],
    tmdb_id: Optional[str],
    imdb_id: Optional[str],
    forced_tmdb_id: Optional[str] = None,
    resolution_fingerprint: str = "",
) -> str:
    payload = json.dumps(
        {
            "resolver_cache_version": RESOLVER_CACHE_VERSION,
            "media_type": media_type,
            "evidence": list(evidence),
            "guess": json_safe(guessed),
            "tmdb_id": tmdb_id,
            "imdb_id": imdb_id,
            "forced_tmdb_id": forced_tmdb_id,
            "resolution_fingerprint": resolution_fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
