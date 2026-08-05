"""Orquestación estable del resolver; las piezas puras viven en submódulos."""

import copy
import logging
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from ...diagnostic_sanitizer import sanitize_for_export
from ...filesystem import media_files
from ...name_parser import parse_release_name
from .cache import (
    RESOLVER_CACHE_VERSION,
    cache_key as _cache_key,
    decode_cache_payload,
    encode_cache_payload,
)
from .candidate_data import (
    candidate_from_payload as _candidate_from_payload,
    merge_search_payload as _merge_search_payload,
)
from .candidate_search import (
    MAX_DETAIL_CANDIDATES,
    MAX_TMDB_SEARCHES,
    fetch_details,
    find_imdb,
)
from .evidence import (
    TECHNICAL_NAMES,
    best_guess,
    collect_episode_intents,
    collect_evidence,
    collect_file_episode_intents,
    collect_name_evidence,
    probe_media_runtimes,
)
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
from .phased import adjudicate_candidates
from .phased_search import SearchCoverage, discover_and_enrich
from .rules import (
    IMDB_ID_PATTERN,
    TMDB_ID_PATTERN,
    apply_query_aliases as _apply_query_aliases,
    first_match as _first_match,
    matching_forced_rule as _matching_forced_rule,
    parse_forced_matches as _parse_forced_matches,
    parse_query_aliases as _parse_query_aliases,
)
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
RESOLVER_ALGORITHM_VERSION = "phased-er-v2"


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
        probe_runner: object = None,
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
        self.probe_runner = probe_runner
        self._deadline = 0.0
        self._rules_snapshot: Dict[str, object] = {}
        self._active_policy: Dict[str, object] = {}
        self._preview_mode = False
        self._trace: Dict[str, object] = {}
        self._search_coverage = SearchCoverage()

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
        *,
        defer_episode_conflicts: bool = False,
    ) -> ResolvedIdentity:
        self._trace = {
            "queries": [],
            "candidates": [],
            "cache_hit": False,
            "resolver_algorithm_version": RESOLVER_ALGORITHM_VERSION,
        }
        self._search_coverage = SearchCoverage()
        if not self.enabled:
            details = _failure_details("RETRY_PROVIDER", "token_missing", retryable=True)
            self._trace["decision"] = _failure_decision("RETRY_PROVIDER", "token_missing")
            raise ResolverUnavailable("TMDB_API_TOKEN no configurado", details)

        category = str(job.get("category") or "")
        active_snapshot = self._rules_snapshot if rules_snapshot is None else rules_snapshot
        parser_rules = (
            active_snapshot.get("parser")
            if isinstance(active_snapshot, dict)
            and isinstance(active_snapshot.get("parser"), dict)
            else None
        )
        parsed = parse_release_name(
            str(job.get("name") or input_root.name), category, rules=parser_rules
        )
        self._trace["parser"] = parsed.to_dict()
        if parsed.category_conflict or category not in {"movies", "tv"}:
            reason = "category_conflict" if parsed.category_conflict else "category_not_resolvable"
            decision = _failure_decision("BLOCKED_HARD", reason)
            self._trace["decision"] = decision
            raise ResolverAmbiguous(
                "Conflicto fuerte entre categoria y nombre"
                if parsed.category_conflict
                else "Categoria manual o no audiovisual; no se consulta TMDb",
                {
                    **_failure_details("BLOCKED_HARD", reason, retryable=False),
                    "parser": parsed.to_dict(),
                    "category": category,
                },
            )

        media_type = "movie" if category == "movies" else "tv"
        rules = self._effective_rules(category, active_snapshot)
        self._active_policy = rules
        http_rules = rules.get("http") if isinstance(rules.get("http"), dict) else {}
        coverage_rules = (
            rules.get("coverage") if isinstance(rules.get("coverage"), dict) else {}
        )
        effective_timeout_ms = max(
            MIN_HTTP_TIMEOUT_MS, int(http_rules.get("timeout_ms") or self.http_timeout_ms)
        )
        effective_budget_ms = max(
            effective_timeout_ms,
            int(coverage_rules.get("total_budget_ms") or self.total_budget_ms),
        )
        self.http_timeout = effective_timeout_ms / 1000
        self.total_budget = effective_budget_ms / 1000
        # El presupuesto v2 abarca evidencias locales, cache y TMDb. Empezarlo
        # despues de ffprobe permitia superar ampliamente el limite configurado.
        self._deadline = time.monotonic() + self.total_budget
        evidence = self._evidence(job, input_root)
        guessed = self._best_guess(evidence, media_type)
        if media_type == "tv":
            guessed["_episode_intents"] = (
                collect_episode_intents(evidence, rules)
                if self._preview_mode
                else collect_file_episode_intents(input_root, rules)
            )
            if defer_episode_conflicts:
                guessed["_defer_episode_conflicts"] = True
        query = str(guessed.get("title") or "").strip()
        if not query:
            reason = "empty_title"
            self._trace["decision"] = _failure_decision("BLOCKED_HARD", reason)
            raise ResolverAmbiguous(
                "No se pudo extraer un titulo util",
                {
                    **_failure_details("BLOCKED_HARD", reason, retryable=False),
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

        # La duracion es evidencia local de identidad. Debe medirse antes de
        # consultar la cache: dos archivos con el mismo nombre pero distinto
        # metraje no pueden compartir una adjudicacion.
        runtime_evidence = (
            []
            if self._preview_mode
            else probe_media_runtimes(  # type: ignore[arg-type]
                input_root,
                rules,
                self.probe_runner,
                deadline=self._deadline,
            )
        )
        if runtime_evidence:
            guessed["_runtime_evidence"] = [dict(item) for item in runtime_evidence]
        if media_type == "tv":
            guessed["_episode_intents"] = (
                collect_episode_intents(evidence, rules, runtime_evidence)
                if self._preview_mode
                else collect_file_episode_intents(input_root, rules, runtime_evidence)
            )
        media_manifest = [] if self._preview_mode else _local_media_manifest(input_root)
        cache_key = self._cache_key(
            media_type,
            evidence,
            guessed,
            direct_tmdb,
            direct_imdb,
            forced_tmdb,
            str(rules["fingerprint"]),
            runtime_evidence,
            media_manifest,
        )
        cache_rules = rules.get("cache") if isinstance(rules.get("cache"), dict) else {}
        cache_enabled = bool(cache_rules.get("enabled", True))
        cached = (
            self.db.get_resolver_cache(cache_key)
            if cache_enabled
            and bool(cache_rules.get("read_enabled", True))
            and not self._preview_mode
            else None
        )
        if cached:
            cached_payload = decode_cache_payload(cached.get("payload_json"))
            if cached_payload is not None:
                try:
                    identity = ResolvedIdentity.from_dict(
                        dict(cached_payload["identity"])
                    )
                except (KeyError, TypeError, ValueError):
                    cached_payload = None
            if cached_payload is not None:
                self._trace["cache_hit"] = True
                identity.source = "cache"
                decision = copy.deepcopy(dict(cached_payload["decision"]))
                origin_source = str(decision.get("source") or "")
                decision["source"] = "cache"
                decision["cache_reused"] = True
                if origin_source:
                    decision["origin_source"] = origin_source
                alternatives = copy.deepcopy(decision.get("alternatives") or [])
                evidence_summary = copy.deepcopy(decision.get("evidence") or [])
                phase_counts = copy.deepcopy(decision.get("phase_counts") or {})
                self._trace.update(
                    {
                        "decision": decision,
                        "candidates": alternatives,
                        "alternatives": copy.deepcopy(alternatives),
                        "evidence": evidence_summary,
                        "phase_counts": phase_counts,
                        "coverage_limited": bool(
                            decision.get("coverage_limited", False)
                        ),
                    }
                )
                return identity
            self._trace["cache_payload_ignored"] = True

        source = "search"
        try:
            if direct_tmdb:
                candidates = [
                    self._details(media_type, int(direct_tmdb), str(rules["language"]))
                ]
                source = "tmdb_id"
                self._search_coverage = SearchCoverage(
                    candidates=list(candidates), discovered=1, enriched=1
                )
            elif direct_imdb:
                candidates = self._find_imdb(
                    media_type, direct_imdb, str(rules["language"])
                )
                source = "imdb_id"
                self._search_coverage = SearchCoverage(
                    candidates=list(candidates),
                    discovered=len(candidates),
                    enriched=len(candidates),
                )
            elif forced_match:
                candidates = [
                    self._validated_forced_candidate(
                        media_type, forced_match, str(rules["language"])
                    )
                ]
                source = "forced_match"
                self._search_coverage = SearchCoverage(
                    candidates=list(candidates), discovered=1, enriched=1
                )
            else:
                candidates = self._search_candidates(
                    media_type,
                    query,
                    guessed,
                    str(rules["language"]),
                    str(rules["region"]),
                )
        except ResolverUnavailable as error:
            reason = "provider_unavailable"
            self._trace["decision"] = _failure_decision("RETRY_PROVIDER", reason)
            raise ResolverUnavailable(
                str(error),
                {
                    **dict(error.details),
                    **_failure_details("RETRY_PROVIDER", reason, retryable=True),
                },
            ) from error
        except ResolverAmbiguous as error:
            reason = str(error.details.get("reason_code") or "forced_identity_conflict")
            self._trace["decision"] = _failure_decision("BLOCKED_HARD", reason)
            raise ResolverAmbiguous(
                str(error),
                {
                    **dict(error.details),
                    **_failure_details("BLOCKED_HARD", reason, retryable=False),
                },
            ) from error
        except ResolutionError as error:
            reason = "explicit_identity_invalid"
            self._trace["decision"] = _failure_decision("BLOCKED_HARD", reason)
            raise ResolverAmbiguous(
                str(error),
                {
                    **dict(error.details),
                    **_failure_details("BLOCKED_HARD", reason, retryable=False),
                },
            ) from error

        coverage = self._search_coverage
        self._trace["search_strategy"] = coverage.trace()
        if not candidates:
            status = "RETRY_PROVIDER" if coverage.provider_failures else "BLOCKED_HARD"
            reason = "provider_unavailable" if coverage.provider_failures else "no_candidates"
            self._trace["decision"] = _failure_decision(status, reason)
            error_type = ResolverUnavailable if status == "RETRY_PROVIDER" else ResolverAmbiguous
            raise error_type(
                "TMDb no devolvio candidatos utilizables",
                {
                    **_failure_details(status, reason, retryable=status == "RETRY_PROVIDER"),
                    "evidence": evidence,
                    "guess": guessed,
                    "query": query,
                    "search_strategy": coverage.trace(),
                },
            )

        outcome = adjudicate_candidates(
            candidates,
            guessed,
            media_type,
            rules,
            source=source,
            runtime_evidence=runtime_evidence,
            discovered=coverage.discovered or len(candidates),
            enriched=coverage.enriched,
            coverage_limited=coverage.coverage_limited,
            provider_failures=coverage.provider_failures,
        )
        outcome.decision["source"] = source
        self._trace.update(
            {
                "decision": copy.deepcopy(outcome.decision),
                "candidates": copy.deepcopy(outcome.decision["alternatives"]),
                "alternatives": copy.deepcopy(outcome.decision["alternatives"]),
                "evidence": copy.deepcopy(outcome.decision["evidence"]),
                "phase_counts": copy.deepcopy(outcome.decision["phase_counts"]),
                "coverage_limited": coverage.coverage_limited,
            }
        )
        if outcome.selected is None:
            details = {
                **_failure_details(
                    outcome.status,
                    str(outcome.decision.get("fallback_reason") or "identity_conflict"),
                    retryable=outcome.status == "RETRY_PROVIDER",
                ),
                "decision": copy.deepcopy(outcome.decision),
                "guess": guessed,
                "query": query,
                "candidates": copy.deepcopy(outcome.decision["alternatives"]),
            }
            if outcome.status == "RETRY_PROVIDER":
                raise ResolverUnavailable("TMDb no pudo completar la cobertura", details)
            raise ResolverAmbiguous("Todas las identidades contradicen la evidencia", details)

        selected = outcome.selected
        identity = ResolvedIdentity(
            media_type=media_type,
            tmdb_id=selected.tmdb_id,
            title=selected.title,
            original_title=selected.original_title,
            year=selected.year,
            original_language=selected.original_language,
            aliases=_unique([selected.title, selected.original_title, *selected.aliases]),
            query=query,
            guess=_json_safe(guessed),
            source=source,
            season=_as_int(guessed.get("season")),
            episodes=(
                _as_int_list(guessed.get("episode"))
                if _as_int(guessed.get("season")) is not None
                else []
            ),
            resolver_algorithm_version=RESOLVER_ALGORITHM_VERSION,
            decision_status=outcome.status,
            coverage_limited=coverage.coverage_limited,
            evidence_summary=[dict(item) for item in selected.evidence],
            episode_intents=[
                dict(item)
                for item in guessed.get("_episode_intents") or []
                if isinstance(item, dict)
            ],
            season_count=selected.season_count,
            season_episode_counts=dict(selected.season_episode_counts),
            known_episodes={
                int(season): list(episodes)
                for season, episodes in selected.known_episodes.items()
            },
        )
        if (
            cache_enabled
            and bool(cache_rules.get("write_enabled", True))
            and not self._preview_mode
        ):
            self.db.set_resolver_cache(
                cache_key,
                media_type,
                encode_cache_payload(identity.to_dict(), outcome.decision),
                max(1, int(cache_rules.get("ttl_seconds") or 30 * 24 * 3600)),
            )
        self.log.info(
            "Identidad v2 resuelta: %s -> TMDb %s %s (%s), %s",
            query,
            identity.tmdb_id,
            identity.title,
            identity.year or "sin ano",
            outcome.status,
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
            probe_runner=self.probe_runner,
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
            status = identity.decision_status or "ACCEPTED_CONFIDENT"
            return {
                "ok": True,
                "status": status,
                "identity": identity.to_dict(),
                **copy.deepcopy(preview._trace),
                "decision": _preview_decision(status, preview._trace),
            }
        except ResolverAmbiguous as error:
            details = sanitize_for_export(copy.deepcopy(error.details))
            if not isinstance(details, dict):
                details = {}
            traced_decision = preview._trace.get("decision")
            traced_status = (
                str(traced_decision.get("status") or "")
                if isinstance(traced_decision, dict)
                else ""
            )
            status = traced_status or str(details.get("status") or "BLOCKED_HARD")
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
            status = str(error.details.get("status") or "RETRY_PROVIDER")
            return {
                "ok": True,
                "status": status,
                "message": str(error),
                "details": details if isinstance(details, dict) else {},
                **copy.deepcopy(preview._trace),
                "decision": _preview_decision(status, preview._trace),
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

    def trace_snapshot(self) -> Dict[str, object]:
        """Devuelve la última traza saneada para job_events e Informe Codex."""

        snapshot = sanitize_for_export(copy.deepcopy(self._trace))
        return dict(snapshot) if isinstance(snapshot, dict) else {}

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
        coverage = discover_and_enrich(
            media_type,
            query,
            guessed,
            str(language or self.language),
            str(region or self.region),
            self._active_policy,
            self._get,
            self._details,
        )
        self._search_coverage = coverage
        self._trace["search_strategy"] = coverage.trace()
        return list(coverage.candidates)

    def _find_imdb(
        self, media_type: str, imdb_id: str, language: Optional[str] = None
    ) -> List[ResolverCandidate]:
        return find_imdb(media_type, imdb_id, language, self._get, self._details)

    def _details(
        self, media_type: str, tmdb_id: int, language: Optional[str] = None
    ) -> ResolverCandidate:
        return fetch_details(media_type, tmdb_id, language, self.language, self._get)

    _candidate_from_payload = staticmethod(_candidate_from_payload)

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


def _local_media_manifest(input_root: Path) -> List[Dict[str, object]]:
    """Huella barata del contenido local que evita cache cruzada por basename."""

    try:
        paths = media_files(input_root)
    except OSError:
        return []
    result: List[Dict[str, object]] = []
    for path in sorted(paths, key=lambda item: str(item).casefold()):
        try:
            stat = path.stat()
            source = (
                path.name
                if input_root.is_file()
                else path.relative_to(input_root).as_posix()
            )
            result.append(
                {
                    "source": source,
                    "size_bytes": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                }
            )
        except (OSError, ValueError):
            # Un archivo que no puede inventariarse no debe compartir una
            # entrada aparentemente valida con otro lote.
            result.append({"source": path.name, "unreadable": True})
    return result


def _preview_decision(status: str, trace: Dict[str, object]) -> Dict[str, object]:
    existing = trace.get("decision")
    if isinstance(existing, dict):
        decision = copy.deepcopy(existing)
        decision["status"] = status
        decision["accepted"] = status.startswith("ACCEPTED_")
        decision["has_scoring"] = False
        return decision
    return {
        "status": status,
        "accepted": False,
        "has_scoring": False,
        "confidence": "none",
        "fallback_reason": None,
        "coverage_limited": False,
        "selected": None,
        "alternatives": [],
        "evidence": [],
        "phase_counts": {
            "discovered": 0,
            "enriched": 0,
            "eliminated": 0,
            "plausible": 0,
        },
    }


def _failure_details(status: str, reason: str, *, retryable: bool) -> Dict[str, object]:
    return {
        "status": status,
        "reason_code": reason,
        "retryable": retryable,
        "resolver_algorithm_version": RESOLVER_ALGORITHM_VERSION,
    }


def _failure_decision(status: str, reason: str) -> Dict[str, object]:
    return {
        "status": status,
        "accepted": False,
        "confidence": "none",
        "fallback_reason": reason,
        "coverage_limited": status == "RETRY_PROVIDER",
        "selected": None,
        "selected_tmdb_id": None,
        "alternatives": [],
        "evidence": [],
        "phase_counts": {
            "discovered": 0,
            "enriched": 0,
            "eliminated": 0,
            "plausible": 0,
        },
        "has_scoring": False,
        "resolver_algorithm_version": RESOLVER_ALGORITHM_VERSION,
    }
