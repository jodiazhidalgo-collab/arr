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
    season_count: Optional[int] = None
    original_language: str = ""
    matching_rules: List[Dict[str, str]] = field(default_factory=list)
    title_match_level: str = "none"
    title_matches: List[Dict[str, object]] = field(default_factory=list)
    title_identity_exact_roles: List[str] = field(default_factory=list)
    search_provenance: Dict[str, object] = field(default_factory=dict)
    popularity: float = 0.0
    vote_count: int = 0
    runtime_minutes: Optional[int] = None
    episode_runtime_minutes: List[int] = field(default_factory=list)
    release_years: List[int] = field(default_factory=list)
    release_timeline: List[Dict[str, object]] = field(default_factory=list)
    season_episode_counts: Dict[int, int] = field(default_factory=dict)
    known_episodes: Dict[int, List[int]] = field(default_factory=dict)
    evidence: List[Dict[str, object]] = field(default_factory=list)
    agree_count: int = 0
    disagree_count: int = 0
    unknown_count: int = 0
    eliminated: bool = False
    elimination_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class EpisodeIntent:
    """Intencion episodica conservada por fuente, sin colapsar el lote TV."""

    source: str
    season: Optional[int] = None
    episodes: List[int] = field(default_factory=list)
    absolute_episode: Optional[int] = None
    is_season_pack: bool = False
    is_special: bool = False
    runtime_minutes: Optional[int] = None

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
    query: str
    guess: Dict[str, object]
    source: str
    # Compatibilidad pasiva: solo se rellenan al leer identidades historicas v1.
    score: Optional[float] = field(default=None, repr=False)
    margin: Optional[float] = field(default=None, repr=False)
    original_language: str = ""
    season: Optional[int] = None
    episodes: List[int] = field(default_factory=list)
    season_count: Optional[int] = None
    season_episode_counts: Dict[int, int] = field(default_factory=dict)
    known_episodes: Dict[int, List[int]] = field(default_factory=dict)
    resolver_algorithm_version: str = ""
    decision_status: str = ""
    coverage_limited: bool = False
    evidence_summary: List[Dict[str, object]] = field(default_factory=list)
    episode_intents: List[Dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        payload = asdict(self)
        if self.resolver_algorithm_version == "phased-er-v2":
            payload.pop("score", None)
            payload.pop("margin", None)
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "ResolvedIdentity":
        is_v2 = str(payload.get("resolver_algorithm_version") or "") == "phased-er-v2"
        return cls(
            media_type=str(payload["media_type"]),
            tmdb_id=int(payload["tmdb_id"]),
            title=str(payload["title"]),
            original_title=str(payload.get("original_title") or payload["title"]),
            year=_optional_int(payload.get("year")),
            original_language=str(payload.get("original_language") or ""),
            aliases=[str(value) for value in payload.get("aliases") or []],
            score=None if is_v2 else float(payload.get("score") or 0),
            margin=None if is_v2 else float(payload.get("margin") or 0),
            query=str(payload.get("query") or ""),
            guess=dict(payload.get("guess") or {}),
            source=str(payload.get("source") or "cache"),
            season=_optional_int(payload.get("season")),
            episodes=[int(value) for value in payload.get("episodes") or []],
            season_count=_optional_int(payload.get("season_count")),
            season_episode_counts={
                int(key): int(value)
                for key, value in dict(payload.get("season_episode_counts") or {}).items()
            },
            known_episodes={
                int(key): [int(value) for value in values or []]
                for key, values in dict(payload.get("known_episodes") or {}).items()
            },
            resolver_algorithm_version=str(
                payload.get("resolver_algorithm_version") or ""
            ),
            decision_status=str(payload.get("decision_status") or ""),
            coverage_limited=bool(payload.get("coverage_limited", False)),
            evidence_summary=[
                dict(item)
                for item in payload.get("evidence_summary") or []
                if isinstance(item, dict)
            ],
            episode_intents=[
                dict(item)
                for item in payload.get("episode_intents") or []
                if isinstance(item, dict)
            ],
        )

    @classmethod
    def from_legacy_dict(cls, payload: Dict[str, object]) -> "ResolvedIdentity":
        """Lector explicito de snapshots v1; nunca participa en adjudicacion."""

        return cls.from_dict(payload)


def _optional_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
