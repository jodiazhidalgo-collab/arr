"""Modelos y errores publicos del resolver ARR."""

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


class ResolutionError(RuntimeError):
    def __init__(self, message: str, details: Optional[Dict[str, object]] = None):
        super().__init__(message)
        self.details = details or {}


class ResolverUnavailable(ResolutionError):
    pass


class ResolverAmbiguous(ResolutionError):
    pass


@dataclass
class ResolverCandidate:
    tmdb_id: int
    media_type: str
    title: str
    original_title: str
    year: Optional[int]
    aliases: List[str] = field(default_factory=list)
    score: float = 0.0
    breakdown: List[Dict[str, object]] = field(default_factory=list)
    season_count: Optional[int] = None
    original_language: str = ""
    matching_rules: List[Dict[str, str]] = field(default_factory=list)
    eligible: bool = True
    eligibility_reasons: List[str] = field(default_factory=list)
    title_match_level: str = "none"
    title_matches: List[Dict[str, object]] = field(default_factory=list)
    title_identity_exact_roles: List[str] = field(default_factory=list)
    search_provenance: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ResolvedIdentity:
    media_type: str
    tmdb_id: int
    title: str
    original_title: str
    year: Optional[int]
    aliases: List[str]
    score: float
    margin: float
    query: str
    guess: Dict[str, object]
    source: str
    original_language: str = ""
    season: Optional[int] = None
    episodes: List[int] = field(default_factory=list)
    resolver_algorithm_version: str = ""

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "ResolvedIdentity":
        return cls(
            media_type=str(payload["media_type"]),
            tmdb_id=int(payload["tmdb_id"]),
            title=str(payload["title"]),
            original_title=str(payload.get("original_title") or payload["title"]),
            year=_optional_int(payload.get("year")),
            original_language=str(payload.get("original_language") or ""),
            aliases=[str(value) for value in payload.get("aliases") or []],
            score=float(payload.get("score") or 0),
            margin=float(payload.get("margin") or 0),
            query=str(payload.get("query") or ""),
            guess=dict(payload.get("guess") or {}),
            source=str(payload.get("source") or "cache"),
            season=_optional_int(payload.get("season")),
            episodes=[int(value) for value in payload.get("episodes") or []],
            resolver_algorithm_version=str(
                payload.get("resolver_algorithm_version") or ""
            ),
        )


def _optional_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
