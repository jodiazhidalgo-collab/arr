import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from guessit import guessit

from .filesystem import MEDIA_EXTENSIONS, media_files
from .name_parser import parse_release_name


TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_ID_PATTERN = re.compile(r"(?:tmdb|themoviedb)[-_. ]?(\d+)", re.IGNORECASE)
IMDB_ID_PATTERN = re.compile(r"\b(tt\d{7,10})\b", re.IGNORECASE)
TECHNICAL_NAMES = {"original", "filebot_input", "filebot_output", "extracted"}


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
    reasons: List[str] = field(default_factory=list)
    season_count: Optional[int] = None

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
    season: Optional[int] = None
    episodes: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "ResolvedIdentity":
        return cls(
            media_type=str(payload["media_type"]),
            tmdb_id=int(payload["tmdb_id"]),
            title=str(payload["title"]),
            original_title=str(payload.get("original_title") or payload["title"]),
            year=_as_int(payload.get("year")),
            aliases=[str(value) for value in payload.get("aliases") or []],
            score=float(payload.get("score") or 0),
            margin=float(payload.get("margin") or 0),
            query=str(payload.get("query") or ""),
            guess=dict(payload.get("guess") or {}),
            source=str(payload.get("source") or "cache"),
            season=_as_int(payload.get("season")),
            episodes=[int(value) for value in payload.get("episodes") or []],
        )


class NameResolver:
    def __init__(
        self,
        token: str,
        language: str,
        region: str,
        http_timeout_ms: int,
        total_budget_ms: int,
        database: object,
        logger: Optional[logging.Logger] = None,
        session: Optional[requests.Session] = None,
    ):
        self.token = token.strip()
        self.language = language.strip() or "es-ES"
        self.region = region.strip() or "ES"
        self.http_timeout = max(0.5, http_timeout_ms / 1000)
        self.total_budget = max(self.http_timeout, total_budget_ms / 1000)
        self.db = database
        self.log = logger or logging.getLogger("arr-orchestrator.name-resolver")
        self.session = session or requests.Session()
        self._deadline = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def resolve(self, job: Dict[str, object], input_root: Path) -> ResolvedIdentity:
        if not self.enabled:
            raise ResolverUnavailable("TMDB_API_TOKEN no configurado")

        category = str(job.get("category") or "")
        parsed = parse_release_name(str(job.get("name") or input_root.name), category)
        if parsed.category_conflict:
            raise ResolverAmbiguous(
                "Conflicto fuerte entre categoria y nombre",
                {"parser": parsed.to_dict(), "category": category},
            )
        if category not in {"movies", "tv"}:
            raise ResolverAmbiguous(
                "Categoria manual o no audiovisual; no se consulta TMDb",
                {"parser": parsed.to_dict(), "category": category},
            )

        media_type = "movie" if job.get("category") == "movies" else "tv"
        evidence = self._evidence(job, input_root)
        guessed = self._best_guess(evidence, media_type)
        query = str(guessed.get("title") or "").strip()
        if not query:
            raise ResolverAmbiguous(
                "GuessIt no pudo extraer un titulo util",
                {"evidence": evidence, "guess": guessed},
            )

        direct_tmdb = self._first_match(TMDB_ID_PATTERN, evidence)
        direct_imdb = self._first_match(IMDB_ID_PATTERN, evidence)
        cache_key = self._cache_key(media_type, evidence, guessed, direct_tmdb, direct_imdb)
        cached = self.db.get_resolver_cache(cache_key)
        if cached:
            identity = ResolvedIdentity.from_dict(json.loads(str(cached["payload_json"])))
            identity.source = "cache"
            return identity

        self._deadline = time.monotonic() + self.total_budget
        candidates: List[ResolverCandidate]
        source = "search"
        if direct_tmdb:
            candidate = self._details(media_type, int(direct_tmdb))
            candidates = [candidate]
            source = "tmdb_id"
        elif direct_imdb:
            candidates = self._find_imdb(media_type, direct_imdb)
            source = "imdb_id"
        else:
            candidates = self._search_candidates(media_type, query, guessed)

        if not candidates:
            raise ResolverAmbiguous(
                "TMDb no devolvio candidatos",
                {"evidence": evidence, "guess": guessed, "query": query},
            )

        direct_identity = source in {"tmdb_id", "imdb_id"}
        ranked = self._rank_candidates(candidates, guessed, evidence, direct_identity)
        top = ranked[0]
        second_score = ranked[1].score if len(ranked) > 1 else 0.0
        margin = top.score - second_score
        if not direct_identity and (top.score < 75 or margin < 12):
            raise ResolverAmbiguous(
                "La identidad no supera el umbral de seguridad",
                {
                    "evidence": evidence,
                    "guess": guessed,
                    "query": query,
                    "top_score": top.score,
                    "margin": margin,
                    "candidates": [candidate.to_dict() for candidate in ranked[:5]],
                },
            )

        identity = ResolvedIdentity(
            media_type=media_type,
            tmdb_id=top.tmdb_id,
            title=top.title,
            original_title=top.original_title,
            year=top.year,
            aliases=_unique([top.title, top.original_title, *top.aliases]),
            score=top.score,
            margin=margin,
            query=query,
            guess=_json_safe(guessed),
            source=source,
            season=_as_int(guessed.get("season")),
            episodes=(
                _as_int_list(guessed.get("episode"))
                if _as_int(guessed.get("season")) is not None
                else []
            ),
        )
        self.db.set_resolver_cache(
            cache_key,
            media_type,
            json.dumps(identity.to_dict(), ensure_ascii=False),
            30 * 24 * 3600,
        )
        self.log.info(
            "Identidad resuelta: %s -> TMDb %s %s (%s), score %.1f, margen %.1f",
            query,
            identity.tmdb_id,
            identity.title,
            identity.year or "sin ano",
            identity.score,
            identity.margin,
        )
        return identity

    def output_matches(self, identity: ResolvedIdentity, output_names: Iterable[str]) -> bool:
        aliases = {_normalize_title(value) for value in identity.aliases if value}
        for output_name in output_names:
            title, year = _split_output_name(output_name)
            normalized = _normalize_title(title)
            if normalized not in aliases:
                return False
            if identity.year and year and abs(identity.year - year) > 1:
                return False
        return True

    def _evidence(self, job: Dict[str, object], input_root: Path) -> List[str]:
        values: List[str] = []

        def add_name(value: str) -> None:
            text = str(value or "").strip()
            if not text:
                return
            parsed = parse_release_name(text, str(job.get("category") or ""))
            values.extend(
                [
                    text,
                    parsed.cleaned,
                    parsed.display_title,
                    parsed.guessit_input,
                    *parsed.title_candidates,
                ]
            )

        add_name(str(job.get("name") or ""))
        if input_root.name.lower() not in TECHNICAL_NAMES:
            add_name(input_root.name)
        files = media_files(input_root)
        files.sort(key=lambda path: path.stat().st_size if path.exists() else 0, reverse=True)
        for path in files[:20]:
            add_name(path.stem)
        return _unique(value.strip() for value in values if value.strip())

    def _best_guess(self, evidence: Sequence[str], media_type: str) -> Dict[str, object]:
        expected = "movie" if media_type == "movie" else "episode"
        guesses: List[Tuple[int, Dict[str, object]]] = []
        for index, value in enumerate(evidence):
            parsed_name = parse_release_name(
                value, "movies" if media_type == "movie" else "tv"
            )
            cleaned = parsed_name.guessit_input or parsed_name.cleaned or _clean_release_name(value)
            parsed = dict(guessit(cleaned, {"type": expected}))
            title = str(parsed.get("title") or "").strip()
            if not title and parsed_name.display_title:
                parsed["title"] = parsed_name.display_title
                title = parsed_name.display_title
            elif _prefer_parser_title(parsed_name.display_title, title):
                parsed["title"] = parsed_name.display_title
                title = parsed_name.display_title
            if not title:
                continue
            if parsed_name.year and not parsed.get("year"):
                parsed["year"] = parsed_name.year
            if media_type == "tv":
                if parsed_name.season is not None and parsed.get("season") is None:
                    parsed["season"] = parsed_name.season
                if parsed_name.episodes and not parsed.get("episode"):
                    parsed["episode"] = parsed_name.episodes
                if parsed_name.absolute_episode is not None:
                    parsed["absolute_episode"] = parsed_name.absolute_episode
            parsed["_title_candidates"] = parsed_name.title_candidates or [title]
            parsed["_display_title"] = parsed_name.display_title
            parsed["_guessit_input"] = cleaned
            quality = 100 - index
            if parsed.get("year"):
                quality += 20
            if media_type == "tv" and parsed.get("season") is not None:
                quality += 15
            if parsed_name.confidence == "high":
                quality += 10
            guesses.append((quality, parsed))
        return max(guesses, key=lambda item: item[0])[1] if guesses else {}

    def _search_candidates(
        self, media_type: str, query: str, guessed: Dict[str, object]
    ) -> List[ResolverCandidate]:
        year = _as_int(guessed.get("year"))
        title_candidates = [str(value) for value in guessed.get("_title_candidates") or []]
        guessit_title = str(guessed.get("title") or "")
        queries = _search_query_variants([query, *title_candidates, guessit_title])
        searches: List[Tuple[str, Optional[int], str]] = []
        for search_query in queries:
            if media_type == "movie":
                searches.append((search_query, year, self.language))
                searches.append((search_query, None, self.language))
                if self.language.lower() != "en-us":
                    searches.append((search_query, year, "en-US"))
                    searches.append((search_query, None, "en-US"))
            else:
                searches.append((search_query, None, self.language))
                if self.language.lower() != "en-us":
                    searches.append((search_query, None, "en-US"))
        searches = searches[:8]

        raw: Dict[int, Dict[str, object]] = {}
        search_count = 0
        for search_query, search_year, language in searches:
            if search_count >= 8:
                break
            endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
            params: Dict[str, object] = {"query": search_query, "language": language}
            if media_type == "movie":
                params["region"] = self.region
                if search_year:
                    params["year"] = search_year
            elif search_year:
                params["first_air_date_year"] = search_year
            payload = self._get(endpoint, params)
            search_count += 1
            for item in list(payload.get("results") or [])[:10]:
                candidate_id = _as_int(item.get("id"))
                if candidate_id:
                    raw.setdefault(candidate_id, dict(item))
            if raw:
                ranked = self._rank_candidates(
                    [self._candidate_from_payload(media_type, item) for item in raw.values()],
                    guessed,
                    [],
                    False,
                )
                margin = ranked[0].score - (ranked[1].score if len(ranked) > 1 else 0)
                if ranked[0].score >= 75 and margin >= 12:
                    break

        initial = [self._candidate_from_payload(media_type, item) for item in raw.values()]
        initial = self._rank_candidates(initial, guessed, [], False)
        enriched: List[ResolverCandidate] = []
        for candidate in initial[:3]:
            try:
                enriched.append(self._details(media_type, candidate.tmdb_id))
            except ResolverUnavailable:
                if not enriched:
                    enriched.append(candidate)
                break
        return enriched or initial

    def _find_imdb(self, media_type: str, imdb_id: str) -> List[ResolverCandidate]:
        payload = self._get(f"/find/{imdb_id}", {"external_source": "imdb_id"})
        key = "movie_results" if media_type == "movie" else "tv_results"
        candidates = [
            self._candidate_from_payload(media_type, dict(item))
            for item in list(payload.get(key) or [])
        ]
        if not candidates:
            return []
        return [self._details(media_type, candidates[0].tmdb_id)]

    def _details(self, media_type: str, tmdb_id: int) -> ResolverCandidate:
        endpoint = f"/movie/{tmdb_id}" if media_type == "movie" else f"/tv/{tmdb_id}"
        payload = self._get(
            endpoint,
            {"language": self.language, "append_to_response": "translations,alternative_titles"},
        )
        return self._candidate_from_payload(media_type, payload)

    def _candidate_from_payload(
        self, media_type: str, payload: Dict[str, object]
    ) -> ResolverCandidate:
        title_key = "title" if media_type == "movie" else "name"
        original_key = "original_title" if media_type == "movie" else "original_name"
        date_key = "release_date" if media_type == "movie" else "first_air_date"
        aliases = [str(payload.get(title_key) or ""), str(payload.get(original_key) or "")]
        alternatives = payload.get("alternative_titles") or {}
        alternative_items = alternatives.get("titles") or alternatives.get("results") or []
        aliases.extend(str(item.get("title") or "") for item in alternative_items)
        translations = (payload.get("translations") or {}).get("translations") or []
        for item in translations:
            data = item.get("data") or {}
            aliases.extend(
                str(data.get(key) or "")
                for key in ("title", "name")
                if data.get(key)
            )
        return ResolverCandidate(
            tmdb_id=int(payload["id"]),
            media_type=media_type,
            title=str(payload.get(title_key) or payload.get(original_key) or ""),
            original_title=str(payload.get(original_key) or payload.get(title_key) or ""),
            year=_year(payload.get(date_key)),
            aliases=_unique(value for value in aliases if value),
            season_count=_as_int(payload.get("number_of_seasons")),
        )

    def _rank_candidates(
        self,
        candidates: Sequence[ResolverCandidate],
        guessed: Dict[str, object],
        evidence: Sequence[str],
        direct_identity: bool,
    ) -> List[ResolverCandidate]:
        for candidate in candidates:
            candidate.score, candidate.reasons = self._score_candidate(
                candidate, guessed, evidence, direct_identity
            )
        return sorted(candidates, key=lambda item: item.score, reverse=True)

    def _score_candidate(
        self,
        candidate: ResolverCandidate,
        guessed: Dict[str, object],
        evidence: Sequence[str],
        direct_identity: bool,
    ) -> Tuple[float, List[str]]:
        if direct_identity:
            return 200.0, ["identificador externo confirmado"]

        query = str(guessed.get("title") or "")
        query_norm = _normalize_title(query)
        aliases = [_normalize_title(value) for value in candidate.aliases if value]
        ratios = [SequenceMatcher(None, query_norm, alias).ratio() for alias in aliases]
        ratio = max(ratios or [0.0])
        exact = query_norm in aliases
        tokens = set(query_norm.split())
        token_overlap = max(
            (
                len(tokens & set(alias.split())) / max(1, len(tokens | set(alias.split())))
                for alias in aliases
            ),
            default=0.0,
        )
        score = (35 if exact else 0) + ratio * 20 + token_overlap * 5
        reasons = [f"titulo ratio={ratio:.2f}", f"tokens={token_overlap:.2f}"]
        if exact:
            reasons.append("titulo exacto")
        elif any(_normalize_title(value) in aliases for value in _spanish_missing_c_variants(query)):
            score += 20
            reasons.append("titulo corregido exacto")

        title_candidates = [
            _normalize_title(str(value))
            for value in guessed.get("_title_candidates") or []
            if str(value or "").strip()
        ]
        candidate_ratios = [
            SequenceMatcher(None, candidate_title, alias).ratio()
            for candidate_title in title_candidates
            for alias in aliases
        ]
        best_candidate_ratio = max(candidate_ratios or [0.0])
        if any(candidate_title in aliases for candidate_title in title_candidates):
            score += 20
            reasons.append("alias del parser exacto")
        elif best_candidate_ratio >= 0.86:
            score += 12
            reasons.append("alias del parser cercano")

        guessed_year = _as_int(guessed.get("year"))
        if guessed_year and candidate.year:
            difference = abs(guessed_year - candidate.year)
            if difference == 0:
                score += 20
                reasons.append("ano exacto")
            elif difference == 1:
                score += 8
                reasons.append("ano +/-1")
            else:
                score -= 25
                reasons.append("ano contradictorio")

        score += 10
        reasons.append("categoria correcta")

        if evidence and any(
            _normalize_title(
                str(dict(guessit(_clean_release_name(value))).get("title") or "")
            )
            in aliases
            for value in evidence
        ):
            score += 15
            reasons.append("evidencia de origen")

        if candidate.media_type == "tv":
            season = _as_int(guessed.get("season"))
            if season is not None and candidate.season_count is not None:
                if 0 <= season <= candidate.season_count:
                    score += 20
                    reasons.append("temporada existente")
                else:
                    score -= 100
                    reasons.append("temporada inexistente")
        return round(score, 2), reasons

    def _get(self, endpoint: str, params: Dict[str, object]) -> Dict[str, object]:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise ResolverUnavailable("Presupuesto de tiempo TMDb agotado")
        timeout = min(self.http_timeout, remaining)
        try:
            response = self.session.get(
                f"{TMDB_BASE_URL}{endpoint}",
                params=params,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                },
                timeout=timeout,
            )
        except requests.RequestException as error:
            raise ResolverUnavailable(f"TMDb no disponible: {error}") from error
        if response.status_code == 429 or response.status_code >= 500:
            raise ResolverUnavailable(f"TMDb respondio HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ResolutionError(f"TMDb rechazo la consulta: HTTP {response.status_code}")
        try:
            return dict(response.json())
        except (TypeError, ValueError) as error:
            raise ResolverUnavailable("TMDb devolvio JSON invalido") from error

    @staticmethod
    def _first_match(pattern: re.Pattern[str], values: Sequence[str]) -> Optional[str]:
        for value in values:
            match = pattern.search(value)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _cache_key(
        media_type: str,
        evidence: Sequence[str],
        guessed: Dict[str, object],
        tmdb_id: Optional[str],
        imdb_id: Optional[str],
    ) -> str:
        payload = json.dumps(
            {
                "media_type": media_type,
                "evidence": list(evidence),
                "guess": _json_safe(guessed),
                "tmdb_id": tmdb_id,
                "imdb_id": imdb_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_title(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value.casefold()))


def _prefer_parser_title(parser_title: str, guessit_title: str) -> bool:
    parser_norm = _normalize_title(parser_title)
    guessit_norm = _normalize_title(guessit_title)
    if not parser_norm or not guessit_norm or parser_norm == guessit_norm:
        return False
    parser_tokens = parser_norm.split()
    guessit_tokens = guessit_norm.split()
    if len(parser_tokens) <= len(guessit_tokens):
        return False
    return bool(set(guessit_tokens).issubset(set(parser_tokens)))


def _search_query_variants(values: Sequence[str]) -> List[str]:
    base = _unique(values)
    expanded: List[str] = []
    for value in base:
        expanded.append(value)
        stripped = _strip_query_tail_noise(value)
        if stripped != value:
            expanded.append(stripped)
        for variant in _spanish_missing_c_variants(value):
            expanded.append(variant)
        if stripped != value:
            for variant in _spanish_missing_c_variants(stripped):
                expanded.append(variant)
    return _unique(expanded)


def _strip_query_tail_noise(value: str) -> str:
    current = re.sub(r"\s+", " ", value or "").strip(" -_.,")
    for _ in range(4):
        updated = re.sub(
            r"(?i)(?:\s+|[-_.])\b(?:pm|ts|hdts|hdtc|tc|cam|hdcam|"
            r"telesync|telecine|screener|dvdscreener|workprint|line|"
            r"proper|repack)\b\s*$",
            "",
            current,
        ).strip(" -_.,")
        if updated == current:
            break
        current = updated
    return current


def _spanish_missing_c_variants(value: str) -> List[str]:
    variants: List[str] = []
    words = str(value or "").split()
    for index, word in enumerate(words):
        if re.search(r"(?i)[a-z]{5,}acion$", word) and not re.search(r"(?i)ccion$", word):
            updated = list(words)
            updated[index] = re.sub(r"(?i)acion$", "accion", word)
            variants.append(" ".join(updated))
    return _unique(variants)


def _clean_release_name(value: str) -> str:
    path = Path(value)
    text = path.stem if path.suffix.lower() in MEDIA_EXTENSIONS else value
    marker = re.search(
        r"(?i)(?:4k|2160p?|1080p?|720p?|webrip|web[-_. ]?dl|bluray|brrip|"
        r"remux|microhd|dvdrip|uhd|hdr|x26[45]|h26[45])",
        text,
    )
    if marker:
        prefix = text[: marker.start()].strip(" ._-[]()")
        if len(_normalize_title(prefix).split()) >= 2:
            text = prefix
    text = re.sub(r"(?i)\b(?:www\.)?[a-z0-9-]+\.(?:com|net|org|li|tv|bz)\b", " ", text)
    return " ".join(text.replace("_", " ").replace(".", " ").split())


def _split_output_name(value: str) -> Tuple[str, Optional[int]]:
    match = re.match(r"^(.*?)\s*\((\d{4})\)\s*$", value.strip())
    if not match:
        return value, None
    return match.group(1).strip(), int(match.group(2))


def _year(value: object) -> Optional[int]:
    match = re.match(r"^(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def _as_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return _as_int(value[0]) if value else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_int_list(value: object) -> List[int]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [number for item in values if (number := _as_int(item)) is not None]


def _unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _json_safe(value: Dict[str, object]) -> Dict[str, object]:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
