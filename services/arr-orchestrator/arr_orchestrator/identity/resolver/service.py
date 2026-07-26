"""Orquestación estable del resolver; las piezas puras viven en submódulos."""

import copy
import json
import logging
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from ...diagnostic_sanitizer import sanitize_for_export
from ...name_parser import parse_release_name
from .cache import RESOLVER_CACHE_VERSION, cache_key as _cache_key
from .candidate_data import (
    candidate_from_payload as _candidate_from_payload,
    merge_search_payload as _merge_search_payload,
    rank_candidates as _rank_candidates,
)
from .candidate_search import (
    MAX_DETAIL_CANDIDATES,
    MAX_TMDB_SEARCHES,
    fetch_details,
    find_imdb,
    search_candidates,
)
from .evidence import TECHNICAL_NAMES, best_guess, collect_evidence, collect_name_evidence
from .forced import FORCED_TITLE_SIMILARITY, validate_forced_candidate
from .http_client import TMDB_BASE_URL, get_json
from .models import (
    ResolutionError,
    ResolvedIdentity,
    ResolverAmbiguous,
    ResolverCandidate,
    ResolverUnavailable,
)
from .policy import effective_policy
from .rules import (
    IMDB_ID_PATTERN,
    TMDB_ID_PATTERN,
    apply_query_aliases as _apply_query_aliases,
    first_match as _first_match,
    matching_forced_rule as _matching_forced_rule,
    parse_forced_matches as _parse_forced_matches,
    parse_query_aliases as _parse_query_aliases,
)
from .scoring import score_candidate
from .text import (
    as_int as _as_int,
    as_int_list as _as_int_list,
    clean_release_name as _clean_release_name,
    date_year as _year,
    json_safe as _json_safe,
    normalize_title as _normalize_title,
    prefer_parser_title as _prefer_parser_title,
    search_query_variants as _search_query_variants,
    spanish_missing_c_variants as _spanish_missing_c_variants,
    split_output_name as _split_output_name,
    strip_query_tail_noise as _strip_query_tail_noise,
    unique as _unique,
)


MIN_HTTP_TIMEOUT_MS = 100


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
        self.http_timeout_ms = max(MIN_HTTP_TIMEOUT_MS, int(http_timeout_ms))
        self.total_budget_ms = max(self.http_timeout_ms, int(total_budget_ms))
        self.http_timeout = self.http_timeout_ms / 1000
        self.total_budget = self.total_budget_ms / 1000
        self.db = database
        self.log = logger or logging.getLogger("arr-orchestrator.name-resolver")
        self.session = session or requests.Session()
        self._deadline = 0.0
        self._rules_snapshot: Dict[str, object] = {}
        self._active_policy: Dict[str, object] = {}
        self._preview_mode = False
        self._trace: Dict[str, object] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def configure_rules(self, rules_snapshot: Optional[Dict[str, object]]) -> None:
        snapshot = rules_snapshot if isinstance(rules_snapshot, dict) else {}
        self._rules_snapshot = copy.deepcopy(snapshot)

    def resolve(
        self,
        job: Dict[str, object],
        input_root: Path,
        rules_snapshot: Optional[Dict[str, object]] = None,
    ) -> ResolvedIdentity:
        self._trace = {"queries": [], "candidates": [], "cache_hit": False}
        if not self.enabled:
            raise ResolverUnavailable("TMDB_API_TOKEN no configurado")

        category = str(job.get("category") or "")
        parser_rules = None
        active_snapshot = self._rules_snapshot if rules_snapshot is None else rules_snapshot
        if isinstance(active_snapshot, dict) and isinstance(active_snapshot.get("parser"), dict):
            parser_rules = active_snapshot.get("parser")
        parsed = parse_release_name(
            str(job.get("name") or input_root.name), category, rules=parser_rules
        )
        self._trace["parser"] = parsed.to_dict()
        if parsed.category_conflict:
            raise ResolverAmbiguous(
                "Conflicto fuerte entre categoria y nombre",
                {
                    "reason_code": "category_conflict",
                    "parser": parsed.to_dict(),
                    "category": category,
                },
            )
        if category not in {"movies", "tv"}:
            raise ResolverAmbiguous(
                "Categoria manual o no audiovisual; no se consulta TMDb",
                {
                    "reason_code": "category_not_resolvable",
                    "parser": parsed.to_dict(),
                    "category": category,
                },
            )

        media_type = "movie" if job.get("category") == "movies" else "tv"
        rules = self._effective_rules(category, active_snapshot)
        self._active_policy = rules
        http_rules = rules.get("http") if isinstance(rules.get("http"), dict) else {}
        effective_timeout_ms = max(
            MIN_HTTP_TIMEOUT_MS,
            int(http_rules.get("timeout_ms") or self.http_timeout_ms),
        )
        effective_budget_ms = max(
            effective_timeout_ms,
            int(http_rules.get("total_budget_ms") or self.total_budget_ms),
        )
        self.http_timeout = effective_timeout_ms / 1000
        self.total_budget = effective_budget_ms / 1000
        evidence = self._evidence(job, input_root)
        guessed = self._best_guess(evidence, media_type)
        query = str(guessed.get("title") or "").strip()
        if not query:
            raise ResolverAmbiguous(
                "GuessIt no pudo extraer un titulo util",
                {
                    "reason_code": "empty_title",
                    "evidence": evidence,
                    "guess": guessed,
                },
            )

        direct_tmdb = self._first_match(TMDB_ID_PATTERN, evidence)
        direct_imdb = self._first_match(IMDB_ID_PATTERN, evidence)
        forced_match = None
        if not direct_tmdb and not direct_imdb:
            forced_match = self._matching_forced_rule(guessed, rules["forced_matches"])
        guessed = self._apply_query_aliases(guessed, rules["query_aliases"])
        forced_tmdb = str(forced_match[2]) if forced_match else None
        cache_key = self._cache_key(
            media_type,
            evidence,
            guessed,
            direct_tmdb,
            direct_imdb,
            forced_tmdb,
            str(rules["fingerprint"]),
        )
        cache_rules = rules.get("cache") if isinstance(rules.get("cache"), dict) else {}
        cache_enabled = bool(cache_rules.get("enabled", True))
        cache_read_enabled = bool(cache_rules.get("read_enabled", True))
        cached = (
            self.db.get_resolver_cache(cache_key)
            if cache_enabled and cache_read_enabled and not self._preview_mode
            else None
        )
        if cached:
            self._trace["cache_hit"] = True
            identity = ResolvedIdentity.from_dict(json.loads(str(cached["payload_json"])))
            identity.source = "cache"
            return identity

        self._deadline = time.monotonic() + self.total_budget
        source = "search"
        if direct_tmdb:
            candidates = [self._details(media_type, int(direct_tmdb), str(rules["language"]))]
            source = "tmdb_id"
        elif direct_imdb:
            candidates = self._find_imdb(media_type, direct_imdb, str(rules["language"]))
            source = "imdb_id"
        elif forced_match:
            candidates = [
                self._validated_forced_candidate(media_type, forced_match, str(rules["language"]))
            ]
            source = "forced_match"
        else:
            candidates = self._search_candidates(
                media_type,
                query,
                guessed,
                str(rules["language"]),
                str(rules["region"]),
            )
        if not candidates:
            raise ResolverAmbiguous(
                "TMDb no devolvio candidatos",
                {
                    "reason_code": "no_candidates",
                    "evidence": evidence,
                    "guess": guessed,
                    "query": query,
                },
            )

        direct_identity = source in {"tmdb_id", "imdb_id", "forced_match"}
        ranked = self._rank_candidates(candidates, guessed, evidence, direct_identity)
        acceptance = rules.get("acceptance") if isinstance(rules.get("acceptance"), dict) else {}
        min_score = float(acceptance.get("min_score", 75))
        min_margin = float(acceptance.get("min_margin", 12))
        bypass = (
            source in {"tmdb_id", "imdb_id"}
            and bool(acceptance.get("direct_ids_bypass", True))
        ) or (source == "forced_match" and bool(acceptance.get("forced_bypass", True)))
        normal_top = ranked[0]
        normal_second_score = ranked[1].score if len(ranked) > 1 else 0.0
        normal_margin = normal_top.score - normal_second_score
        ranked, preference_applied = _prefer_original_language_candidate(
            ranked,
            source=source,
            min_score=min_score,
            min_margin=min_margin,
            normal_score_passed=normal_top.score >= min_score,
            normal_margin_passed=normal_margin >= min_margin,
            preference=rules.get("original_language_preference"),
        )
        top = ranked[0]
        second_score = ranked[1].score if len(ranked) > 1 else 0.0
        margin = top.score - second_score
        score_passed = top.score >= min_score
        margin_passed = margin >= min_margin
        self._trace["candidates"] = [candidate.to_dict() for candidate in ranked]
        preference_rules = (
            rules.get("original_language_preference")
            if isinstance(rules.get("original_language_preference"), dict)
            else {}
        )
        preference_language = str(preference_rules.get("language") or "en")
        preference_decision = {
            "applied": preference_applied,
            "enabled": bool(preference_rules.get("enabled", True)),
            "language": preference_language,
            "selected_original_language": top.original_language or None,
        }
        decision_status = (
            "ACCEPTED"
            if bypass or (score_passed and margin_passed) or preference_applied
            else "REJECTED_SCORE"
            if not score_passed
            else "REJECTED_MARGIN"
        )
        self._trace["decision"] = {
            "status": decision_status,
            "accepted": decision_status == "ACCEPTED",
            "has_scoring": True,
            "source": source,
            "bypass": bypass,
            "score": top.score,
            "second_score": second_score,
            "has_second_candidate": len(ranked) > 1,
            "min_score": min_score,
            "score_passed": score_passed,
            "margin": margin,
            "min_margin": min_margin,
            "margin_passed": margin_passed,
            "original_language_preference": preference_decision,
        }
        if not bypass and not preference_applied and (
            top.score < min_score or margin < min_margin
        ):
            raise ResolverAmbiguous(
                "La identidad no supera el umbral de seguridad",
                {
                    "evidence": evidence,
                    "guess": guessed,
                    "query": query,
                    "top_score": top.score,
                    "margin": margin,
                    "min_score": min_score,
                    "min_margin": min_margin,
                    "candidates": [candidate.to_dict() for candidate in ranked[:5]],
                },
            )

        identity = ResolvedIdentity(
            media_type=media_type,
            tmdb_id=top.tmdb_id,
            title=top.title,
            original_title=top.original_title,
            year=top.year,
            original_language=top.original_language,
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
        if cache_enabled and bool(cache_rules.get("write_enabled", True)) and not self._preview_mode:
            self.db.set_resolver_cache(
                cache_key,
                media_type,
                json.dumps(identity.to_dict(), ensure_ascii=False),
                max(1, int(cache_rules.get("ttl_seconds") or 30 * 24 * 3600)),
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

    def preview(
        self,
        name: str,
        category: str,
        rules_snapshot: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        """Resuelve un titulo sin crear jobs ni tocar la cache productiva."""

        preview = NameResolver(
            self.token,
            self.language,
            self.region,
            self.http_timeout_ms,
            self.total_budget_ms,
            self.db,
            self.log,
            session=self.session,
        )
        preview._preview_mode = True
        snapshot = copy.deepcopy(
            rules_snapshot if isinstance(rules_snapshot, dict) else self._rules_snapshot
        )
        try:
            identity = preview.resolve(
                {"name": str(name or "").strip(), "category": str(category or "").strip()},
                Path("preview"),
                snapshot,
            )
            return {
                "ok": True,
                "status": "ACCEPTED",
                "identity": identity.to_dict(),
                **copy.deepcopy(preview._trace),
                "decision": _preview_decision("ACCEPTED", preview._trace),
            }
        except ResolverAmbiguous as error:
            details = sanitize_for_export(copy.deepcopy(error.details))
            if not isinstance(details, dict):
                details = {}
            status = (
                "NO_CANDIDATES"
                if details.get("reason_code") == "no_candidates"
                else "REJECTED"
            )
            if details.get("top_score") is not None:
                score = float(details.get("top_score") or 0)
                margin = float(details.get("margin") or 0)
                min_score = (
                    75.0
                    if details.get("min_score") is None
                    else float(details["min_score"])
                )
                min_margin = (
                    12.0
                    if details.get("min_margin") is None
                    else float(details["min_margin"])
                )
                if score < min_score:
                    status = "REJECTED_SCORE"
                elif margin < min_margin:
                    status = "REJECTED_MARGIN"
            return {
                "ok": True,
                "status": status,
                "message": str(error),
                "details": details,
                **copy.deepcopy(preview._trace),
                "decision": _preview_decision(status, preview._trace),
            }
        except ResolverUnavailable as error:
            details = sanitize_for_export(copy.deepcopy(error.details))
            return {
                "ok": False,
                "status": "TMDB_UNAVAILABLE",
                "message": str(error),
                "details": details if isinstance(details, dict) else {},
                **copy.deepcopy(preview._trace),
                "decision": _preview_decision("TMDB_UNAVAILABLE", preview._trace),
            }
        except ResolutionError as error:
            sanitized = sanitize_for_export(copy.deepcopy(error.details))
            return {
                "ok": False,
                "status": "TMDB_ERROR",
                "message": str(error),
                "details": sanitized if isinstance(sanitized, dict) else {},
                **copy.deepcopy(preview._trace),
                "decision": _preview_decision("TMDB_ERROR", preview._trace),
            }

    def output_matches(self, identity: ResolvedIdentity, output_names: Iterable[str]) -> bool:
        aliases = {_normalize_title(value) for value in identity.aliases if value}
        validation = (
            self._active_policy.get("output_validation")
            if isinstance(self._active_policy.get("output_validation"), dict)
            else {}
        )
        require_title_alias = bool(validation.get("require_title_alias", True))
        year_tolerance = max(0, int(validation.get("year_tolerance", 1)))
        for output_name in output_names:
            title, year = _split_output_name(output_name)
            if require_title_alias and _normalize_title(title) not in aliases:
                return False
            if identity.year and year and abs(identity.year - year) > year_tolerance:
                return False
        return True

    def _effective_rules(
        self, category: str, rules_snapshot: Optional[Dict[str, object]]
    ) -> Dict[str, object]:
        policy = effective_policy(
            rules_snapshot,
            category,
            default_language=self.language,
            default_region=self.region,
            default_http_timeout_ms=self.http_timeout_ms,
            default_total_budget_ms=self.total_budget_ms,
        )
        policy["language"] = str(policy.get("language") or self.language).strip() or self.language
        policy["region"] = str(policy.get("region") or self.region).strip().upper() or self.region
        policy["query_aliases"] = _parse_query_aliases(policy.get("query_aliases"))
        policy["forced_matches"] = _parse_forced_matches(policy.get("forced_matches"))
        return policy

    _apply_query_aliases = staticmethod(_apply_query_aliases)
    _matching_forced_rule = staticmethod(_matching_forced_rule)

    def _validated_forced_candidate(
        self,
        media_type: str,
        forced_match: Tuple[str, Optional[int], int],
        language: str,
    ) -> ResolverCandidate:
        return validate_forced_candidate(
            media_type,
            forced_match,
            language,
            self._details,
            self._active_policy,
        )

    def _evidence(self, job: Dict[str, object], input_root: Path) -> List[str]:
        if self._preview_mode:
            return collect_name_evidence(
                str(job.get("name") or ""),
                str(job.get("category") or ""),
                self._active_policy,
            )
        return collect_evidence(job, input_root, self._active_policy)

    def _best_guess(self, evidence: Sequence[str], media_type: str) -> Dict[str, object]:
        return best_guess(evidence, media_type, self._active_policy)

    def _search_candidates(
        self,
        media_type: str,
        query: str,
        guessed: Dict[str, object],
        language: Optional[str] = None,
        region: Optional[str] = None,
    ) -> List[ResolverCandidate]:
        return search_candidates(
            media_type,
            query,
            guessed,
            str(language or self.language),
            str(region or self.region),
            self._active_policy,
            self._get,
            self._details,
            self._rank_candidates,
        )

    def _find_imdb(
        self, media_type: str, imdb_id: str, language: Optional[str] = None
    ) -> List[ResolverCandidate]:
        return find_imdb(media_type, imdb_id, language, self._get, self._details)

    def _details(
        self, media_type: str, tmdb_id: int, language: Optional[str] = None
    ) -> ResolverCandidate:
        return fetch_details(media_type, tmdb_id, language, self.language, self._get)

    _candidate_from_payload = staticmethod(_candidate_from_payload)

    def _rank_candidates(
        self,
        candidates: Sequence[ResolverCandidate],
        guessed: Dict[str, object],
        evidence: Sequence[str],
        direct_identity: bool,
    ) -> List[ResolverCandidate]:
        return _rank_candidates(
            candidates,
            guessed,
            evidence,
            direct_identity,
            self._active_policy.get("scoring"),
        )

    def _score_candidate(
        self,
        candidate: ResolverCandidate,
        guessed: Dict[str, object],
        evidence: Sequence[str],
        direct_identity: bool,
    ) -> Tuple[float, List[Dict[str, object]]]:
        return score_candidate(
            candidate,
            guessed,
            evidence,
            direct_identity,
            self._active_policy.get("scoring"),
        )

    def _get(self, endpoint: str, params: Dict[str, object]) -> Dict[str, object]:
        return get_json(
            self.session,
            self.token,
            endpoint,
            params,
            self._deadline,
            self.http_timeout,
            self._trace,
        )

    _first_match = staticmethod(_first_match)
    _cache_key = staticmethod(_cache_key)


def _preview_decision(status: str, trace: Dict[str, object]) -> Dict[str, object]:
    existing = trace.get("decision")
    if isinstance(existing, dict):
        decision = copy.deepcopy(existing)
        decision["status"] = status
        decision["accepted"] = status == "ACCEPTED"
        decision["has_scoring"] = True
        return decision
    return {
        "status": status,
        "accepted": False,
        "has_scoring": False,
        "bypass": False,
    }


def _prefer_original_language_candidate(
    ranked: Sequence[ResolverCandidate],
    *,
    source: str,
    min_score: float,
    min_margin: float,
    normal_score_passed: bool,
    normal_margin_passed: bool,
    preference: object,
) -> Tuple[List[ResolverCandidate], bool]:
    """Promueve un unico idioma preferido solo dentro de la zona ambigua."""

    ordered = list(ranked)
    settings = preference if isinstance(preference, dict) else {}
    enabled = bool(settings.get("enabled", True))
    language = _base_language(str(settings.get("language") or "en"))
    if (
        not ordered
        or source != "search"
        or not enabled
        or not language
        or not normal_score_passed
        or normal_margin_passed
    ):
        return ordered, False

    best_score = ordered[0].score
    ambiguous = [
        candidate
        for candidate in ordered
        if candidate.score >= min_score
        and best_score - candidate.score < min_margin
    ]
    if len(ambiguous) < 2:
        return ordered, False
    preferred = [
        candidate
        for candidate in ambiguous
        if _base_language(candidate.original_language) == language
    ]
    if len(preferred) != 1:
        return ordered, False

    selected = preferred[0]
    return [selected, *(candidate for candidate in ordered if candidate is not selected)], True


def _base_language(value: str) -> str:
    return str(value or "").strip().split("-", 1)[0].casefold()
