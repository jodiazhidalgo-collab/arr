"""Huella estable de todos los campos que afectan a la identidad."""

from __future__ import annotations

import hashlib
import json
from typing import Dict

from .validation import normalize_identity_rules


def identity_fingerprint(rules: Dict[str, object]) -> str:
    normalized = normalize_identity_rules(rules)
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


__all__ = ["identity_fingerprint"]
