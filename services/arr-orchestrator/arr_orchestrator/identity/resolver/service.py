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
from ..source_fallback import (
    fallback_job,
    recoverable_resolution_error,
    source_fallback_block_reason,
    source_title_contexts,
)
from ..source_privacy import source_title_fingerprint
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
SCORE_TIE_EPSILON = 1e-9


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
        """Resuelve primero el nombre físico y solo después un respaldo válido."""

        self._deadline = 0.0
        active_snapshot = self._rules_snapshot if rules_snapshot is None else rules_snapshot
        try:
            return self._resolve_once(job, input_root, active_snapshot)
        except ResolverAmbiguous as primary_error:
            if not recoverable_resolution_error(primary_error):
                raise
            block_reason = source_fallback_block_reason(job, active_snapshot)
            if block_reason:
                details = copy.deepcopy(
                    primary_error.details
                    if isinstance(primary_error.details, dict)
                    else {}
                )
                details["source_fallback_block_reason"] = block_reason
                primary_error.details = details
                raise
            contexts = source_title_contexts(job, active_snapshot)
            if not contexts:
                raise
            primary_trace = copy.deepcopy(self._trace)
            attempts: List[Dict[str, object]] = []
            accepted: List[
                Tuple[ResolvedIdentity, Dict[str, object], Dict[str, object]]
            ] = []
            rejected: List[
                Tuple[ResolverAmbiguous, Dict[str, object], Dict[str, object]]
            ] = []
            for context in contexts:
                try:
                    identity = self._resolve_once(
                        fallback_job(job, context),
                        input_root,
                        active_snapshot,
                    )
                    trace = copy.deepcopy(self._trace)
                    attempts.append(
                        {
                            "source": context.source,
                            "event_id": context.event_id,
                            "source_title": context.source_title,
                            "status": "ACCEPTED",
                            "tmdb_id": identity.tmdb_id,
                            "score": identity.score,
                        }
                    )
                    accepted.append((identity, context.public(), trace))
                except ResolverAmbiguous as fallback_error:
                    details = fallback_error.details if isinstance(fallback_error.details, dict) else {}
                    rejected.append(
                        (
                            fallback_error,
                            context.public(),
                            copy.deepcopy(self._trace),
                        )
                    )
                    attempts.append(
                        {
                            "source": context.source,
                            "event_id": context.event_id,
                            "source_title": context.source_title,
                            "status": _ambiguous_status(details),
                            "reason_code": details.get("reason_code"),
                            "score": details.get("top_score"),
                            "margin": details.get("margin"),
                        }
                    )

            if accepted:
                tmdb_ids = {identity.tmdb_id for identity, _context, _trace in accepted}
                if len(tmdb_ids) > 1:
                    self._trace = primary_trace
                    self._trace["source_fallback"] = {
                        "applied": False,
                        "status": "SOURCE_CONTEXT_CONFLICT",
                        "attempts": attempts,
                    }
                    raise ResolverAmbiguous(
                        "Los títulos de origen conducen a identidades diferentes",
                        {
                            "reason_code": "source_context_conflict",
                            "primary_error": str(primary_error),
                            "source_fallback_attempts": attempts,
                        },
                    )
                identity, context_payload, chosen_trace = max(
                    accepted,
                    key=lambda item: (item[0].score, item[0].margin),
                )
                identity.source = "source_title_fallback"
                identity.source_context = copy.deepcopy(context_payload)
                fallback_trace = {
                    "applied": True,
                    "status": "ACCEPTED",
                    "source": context_payload.get("source"),
                    "event_id": context_payload.get("event_id"),
                    "source_title": context_payload.get("source_title"),
                    "primary_status": _ambiguous_status(primary_error.details),
                    "primary_message": str(primary_error),
                    "attempts": attempts,
                }
                chosen_trace["primary_attempt"] = primary_trace
                chosen_trace["source_fallback"] = fallback_trace
                decision = chosen_trace.get("decision")
                if isinstance(decision, dict):
                    decision["source_fallback"] = copy.deepcopy(fallback_trace)
                self._trace = chosen_trace
                return identity

            if rejected and all(
                _ambiguous_status(error.details) == "REJECTED_SOURCE_TITLE"
                for error, _context, _trace in rejected
            ):
                fallback_error, context_payload, rejected_trace = max(
                    rejected,
                    key=lambda item: float(
                        item[0].details.get("top_score") or 0
                        if isinstance(item[0].details, dict)
                        else 0
                    ),
                )
                fallback_trace = {
                    "applied": False,
                    "status": "REJECTED_SOURCE_TITLE",
                    "source": context_payload.get("source"),
                    "event_id": context_payload.get("event_id"),
                    "source_title": context_payload.get("source_title"),
                    "primary_status": _ambiguous_status(primary_error.details),
                    "primary_message": str(primary_error),
                    "attempts": attempts,
                }
                rejected_trace["primary_attempt"] = primary_trace
                rejected_trace["source_fallback"] = fallback_trace
                decision = rejected_trace.get("decision")
                if isinstance(decision, dict):
                    decision["source_fallback"] = copy.deepcopy(fallback_trace)
                self._trace = rejected_trace
                details = copy.deepcopy(
                    fallback_error.details
                    if isinstance(fallback_error.details, dict)
                    else {}
                )
                details.update(
                    {
                        "reason_code": "source_title_policy",
                        "primary_error": str(primary_error),
                        "primary_status": _ambiguous_status(primary_error.details),
                        "primary_details": copy.deepcopy(primary_error.details),
                        "source_fallback_attempts": attempts,
                    }
                )
                raise ResolverAmbiguous(
                    "Ningún título validado del buscador supera sus límites de seguridad",
                    details,
                ) from None

            self._trace = primary_trace
            self._trace["source_fallback"] = {
                "applied": False,
                "status": "REJECTED",
                "attempts": attempts,
            }
            details = copy.deepcopy(
                primary_error.details if isinstance(primary_error.details, dict) else {}
            )
            details["source_fallback_attempts"] = attempts
            primary_error.details = details
            raise primary_error

    def _resolve_once(
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
        source_context = job.get("_source_context")
        if isinstance(source_context, dict):
            guessed["_source_context_title"] = str(
                guessed.get("title") or source_context.get("source_title") or ""
            ).strip()
            guessed["_source_context_source"] = str(
                source_context.get("source") or ""
            ).strip()
            if media_type == "tv":
                primary = parse_release_name(
                    str(job.get("_source_primary_name") or ""),
                    category,
                    rules=parser_rules,
                )
                _merge_tv_coordinates(guessed, primary)
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

        source_attempt = isinstance(source_context, dict)
        direct_tmdb = None if source_attempt else self._first_match(TMDB_ID_PATTERN, evidence)
        direct_imdb = None if source_attempt else self._first_match(IMDB_ID_PATTERN, evidence)
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

        if self._deadline <= 0:
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
                    "identity_source": source,
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
        oldest_preference_enabled = bool(
            acceptance.get("prefer_oldest_exact_title_without_year", False)
        )
        oldest_preference_applied = False
        if not preference_applied:
            ranked, oldest_preference_applied = _prefer_oldest_exact_title_movie_candidate(
                ranked,
                media_type=media_type,
                source=source,
                guessed=guessed,
                min_score=min_score,
                min_margin=min_margin,
                normal_score_passed=normal_top.score >= min_score,
                normal_margin_passed=normal_margin >= min_margin,
                enabled=oldest_preference_enabled,
                search_selection=self._trace.get("oldest_exact_title_search"),
            )
        top = ranked[0]
        second_score = ranked[1].score if len(ranked) > 1 else 0.0
        margin = top.score - second_score
        score_passed = top.score >= min_score
        margin_passed = margin >= min_margin
        source_title_policy_passed = not source_attempt or bool(
            getattr(top, "source_title_qualified", False)
        )
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
        oldest_preference_decision = {
            "applied": oldest_preference_applied,
            "enabled": oldest_preference_enabled,
            "selected_year": top.year if oldest_preference_applied else None,
            "reason_code": (
                "oldest_exact_title_without_year" if oldest_preference_applied else None
            ),
        }
        decision_status = (
            "ACCEPTED"
            if source_title_policy_passed
            and (
                bypass
                or (score_passed and margin_passed)
                or preference_applied
                or oldest_preference_applied
            )
            else "REJECTED_SOURCE_TITLE"
            if not source_title_policy_passed
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
            "source_title_policy_passed": source_title_policy_passed,
            "original_language_preference": preference_decision,
            "oldest_exact_title_preference": oldest_preference_decision,
        }
        if (
            not source_title_policy_passed
            or (
                not bypass
                and not preference_applied
                and not oldest_preference_applied
                and (top.score < min_score or margin < min_margin)
            )
        ):
            raise ResolverAmbiguous(
                (
                    "El título de origen no supera sus límites de seguridad"
                    if not source_title_policy_passed
                    else "La identidad no supera el umbral de seguridad"
                ),
                {
                    "reason_code": (
                        "source_title_policy"
                        if not source_title_policy_passed
                        else "score_or_margin"
                    ),
                    "evidence": evidence,
                    "guess": guessed,
                    "query": query,
                    "identity_source": source,
                    "top_score": top.score,
                    "margin": margin,
                    "min_score": min_score,
                    "min_margin": min_margin,
                    "source_title_policy_passed": source_title_policy_passed,
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
            cache_source_titles = []
            if isinstance(source_context, dict):
                cache_source_titles.extend(
                    [
                        str(source_context.get("source_title") or ""),
                        str(guessed.get("_source_context_title") or ""),
                        query,
                    ]
                )
            persistent_identity = identity.to_persistent_dict(cache_source_titles)
            self.db.set_resolver_cache(
                cache_key,
                media_type,
                json.dumps(persistent_identity, ensure_ascii=False),
                max(1, int(cache_rules.get("ttl_seconds") or 30 * 24 * 3600)),
            )
        self.log.info(
            "Identidad resuelta: %s -> TMDb %s %s (%s), score %.1f, margen %.1f",
            (
                f"<source-title:{source_title_fingerprint(query)[:12]}>"
                if source_attempt
                else query
            ),
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
        source_title: str = "",
        source: str = "preview",
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
        preview_hash = "0" * 40
        preview_job: Dict[str, object] = {
            "name": str(name or "").strip(),
            "category": str(category or "").strip(),
        }
        clean_source_title = str(source_title or "").strip()
        if clean_source_title:
            preview_now = time.time()
            preview_job["infohash"] = preview_hash
            preview_job["source_meta_json"] = json.dumps(
                {
                    "source_contexts": [
                        {
                            "event_id": "preview",
                            "source": str(source or "preview").strip() or "preview",
                            "infohash": preview_hash,
                            "destination": str(category or "").strip(),
                            "source_title": clean_source_title,
                            "route": "PREVIEW",
                            "delivery_state": "accepted",
                            "created_at": preview_now,
                            "received_at": preview_now,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        try:
            identity = preview.resolve(
                preview_job,
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
                else "REJECTED_SOURCE_TITLE"
                if details.get("reason_code") == "source_title_policy"
                else "REJECTED"
            )
            if (
                details.get("top_score") is not None
                and details.get("reason_code") != "source_title_policy"
            ):
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
        if self._preview_mode or bool(job.get("_source_context_only")):
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
            selection_trace=self._trace,
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
            self._active_policy.get("title_matching"),
            self._active_policy.get("source_title_fallback"),
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
            self._active_policy.get("title_matching"),
            self._active_policy.get("source_title_fallback"),
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


def _ambiguous_status(details: object) -> str:
    payload = details if isinstance(details, dict) else {}
    if payload.get("reason_code") == "no_candidates":
        return "NO_CANDIDATES"
    if payload.get("reason_code") == "source_title_policy":
        return "REJECTED_SOURCE_TITLE"
    if payload.get("top_score") is not None:
        score = float(payload.get("top_score") or 0)
        margin = float(payload.get("margin") or 0)
        min_score_value = payload.get("min_score")
        min_margin_value = payload.get("min_margin")
        min_score = float(75 if min_score_value is None else min_score_value)
        min_margin = float(12 if min_margin_value is None else min_margin_value)
        return "REJECTED_SCORE" if score < min_score else (
            "REJECTED_MARGIN" if margin < min_margin else "REJECTED"
        )
    return "REJECTED"


def _merge_tv_coordinates(guessed: Dict[str, object], primary: object) -> None:
    """Combina por campo; cada coordenada fisica gana si esta disponible."""

    physical_season = _as_int(getattr(primary, "season", None))
    physical_episodes = _as_int_list(getattr(primary, "episodes", None))
    physical_absolute = _as_int(getattr(primary, "absolute_episode", None))
    source_season = _as_int(guessed.get("season"))
    source_episodes = _as_int_list(guessed.get("episode"))
    source_absolute = _as_int(guessed.get("absolute_episode"))

    final_season = physical_season if physical_season is not None else source_season
    if physical_episodes:
        final_episodes = physical_episodes
    elif physical_absolute is not None and final_season is not None:
        final_episodes = [physical_absolute]
    elif source_episodes:
        final_episodes = source_episodes
    elif source_absolute is not None and final_season is not None:
        final_episodes = [source_absolute]
    else:
        final_episodes = []

    if final_season is not None:
        guessed["season"] = final_season
    else:
        guessed.pop("season", None)
    if final_episodes:
        guessed["episode"] = final_episodes
        guessed.pop("absolute_episode", None)
        return
    guessed.pop("episode", None)
    final_absolute = (
        physical_absolute if physical_absolute is not None else source_absolute
    )
    if final_absolute is not None:
        guessed["absolute_episode"] = final_absolute
    else:
        guessed.pop("absolute_episode", None)


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


def _prefer_oldest_exact_title_movie_candidate(
    ranked: Sequence[ResolverCandidate],
    *,
    media_type: str,
    source: str,
    guessed: Dict[str, object],
    min_score: float,
    min_margin: float,
    normal_score_passed: bool,
    normal_margin_passed: bool,
    enabled: bool,
    search_selection: object,
) -> Tuple[List[ResolverCandidate], bool]:
    """Promueve el unico año minimo solo en una ambiguedad exacta y comprobada."""

    ordered = list(ranked)
    selection = search_selection if isinstance(search_selection, dict) else {}
    expected_tmdb_id = _as_int(selection.get("tmdb_id"))
    if (
        len(ordered) < 2
        or media_type != "movie"
        or source != "search"
        or not enabled
        or _as_int(guessed.get("year")) is not None
        or not normal_score_passed
        or normal_margin_passed
        or abs(ordered[0].score - ordered[1].score) > SCORE_TIE_EPSILON
        or selection.get("eligible") is not True
        or expected_tmdb_id is None
    ):
        return ordered, False

    best_score = ordered[0].score
    ambiguous = [
        candidate
        for candidate in ordered
        if candidate.score >= min_score
        and best_score - candidate.score < min_margin
    ]
    query_normalized = _normalize_title(str(guessed.get("title") or ""))
    if (
        len(ambiguous) < 2
        or not query_normalized
        or any(
            abs(best_score - candidate.score) > SCORE_TIE_EPSILON
            for candidate in ambiguous
        )
        or any(
            _normalize_title(candidate.title) != query_normalized
            for candidate in ambiguous
        )
        or any(candidate.year is None for candidate in ambiguous)
    ):
        return ordered, False

    oldest_year = min(
        int(candidate.year) for candidate in ambiguous if candidate.year is not None
    )
    oldest = [candidate for candidate in ambiguous if candidate.year == oldest_year]
    if len(oldest) != 1 or oldest[0].tmdb_id != expected_tmdb_id:
        return ordered, False

    selected = oldest[0]
    return [selected, *(candidate for candidate in ordered if candidate is not selected)], True
