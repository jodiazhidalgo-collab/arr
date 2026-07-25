from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class ParsedName:
    raw: str
    cleaned: str
    display_title: str
    title_candidates: List[str] = field(default_factory=list)
    year: Optional[int] = None
    media_hint: str = "manual"
    confidence: str = "low"
    season: Optional[int] = None
    episodes: List[int] = field(default_factory=list)
    episode_range: Optional[Tuple[int, int]] = None
    absolute_episode: Optional[int] = None
    season_pack: Optional[int] = None
    guessit_input: str = ""
    category_conflict: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class MediaDecision:
    media_type: str
    confidence: str
    reason_codes: List[str] = field(default_factory=list)
    episode_hint: Dict[str, object] = field(default_factory=dict)
    allow_external_lookup: bool = False
    block_reason: Optional[str] = None
    parsed: Optional[ParsedName] = None

    def to_dict(self) -> Dict[str, object]:
        payload = asdict(self)
        if self.parsed:
            payload["parsed"] = self.parsed.to_dict()
        return payload
