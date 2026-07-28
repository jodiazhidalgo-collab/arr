import json
import time

from arr_orchestrator.identity.defaults import factory_identity_rules
from arr_orchestrator.identity.resolver.models import ResolverAmbiguous
from arr_orchestrator.identity.source_fallback import (
    fallback_job,
    recoverable_resolution_error,
    source_fallback_block_reason,
    source_title_contexts,
)
from arr_orchestrator.source_context.policy import CONTEXT_TTL_SECONDS


INFOHASH = "a" * 40


def _context(
    event_id: str,
    title: str,
    *,
    source: str = "buscador-pro",
    destination: str = "movies",
    infohash: str = INFOHASH,
    state: str = "accepted",
) -> dict:
    return {
        "event_id": event_id,
        "source": source,
        "infohash": infohash,
        "destination": destination,
        "source_title": title,
        "route": "RD_VERIFIED_MAGNET_NATIVE",
        "delivery_state": state,
        "created_at": "2026-07-28T00:00:00Z",
        "received_at": time.time(),
    }


def _job(*contexts: dict, category: str = "movies", infohash: str = INFOHASH) -> dict:
    return {
        "name": "nombre fisico inutilizable",
        "category": category,
        "infohash": infohash,
        "source_meta_json": json.dumps({"source_contexts": list(contexts)}),
    }


def test_contexts_require_the_exact_full_job_infohash() -> None:
    rules = factory_identity_rules()

    assert source_title_contexts(_job(_context("one", "Título válido")), rules)
    assert not source_title_contexts(
        _job(_context("one", "Título válido"), infohash=""), rules
    )
    assert not source_title_contexts(
        _job(_context("one", "Título válido", infohash="b" * 40)), rules
    )


def test_contexts_honor_category_switches_and_never_enable_manual() -> None:
    rules = factory_identity_rules()
    rules["resolver"]["source_title_fallback"]["movies"] = False

    assert not source_title_contexts(_job(_context("one", "Película")), rules)
    assert not source_title_contexts(
        _job(
            _context("two", "Entrada manual", destination="manual"),
            category="manual",
        ),
        factory_identity_rules(),
    )


def test_contexts_filter_failed_entries_and_cap_distinct_evidence() -> None:
    contexts = source_title_contexts(
        _job(
            _context("one", "Título uno"),
            _context("one", "Título uno"),
            _context("failed", "No debe usarse", state="failed"),
            _context("two", "Título dos", source="buscador-jackett"),
            _context("three", "Título tres"),
            _context("four", "Título cuatro"),
        ),
        factory_identity_rules(),
    )

    assert [item.source_title for item in contexts] == [
        "Título uno",
        "Título dos",
        "Título tres",
    ]


def test_expired_context_is_never_used_by_identity() -> None:
    expired = _context("old", "Título caducado")
    expired["received_at"] = time.time() - CONTEXT_TTL_SECONDS - 1

    assert not source_title_contexts(
        _job(expired),
        factory_identity_rules(),
    )


def test_fallback_job_never_replaces_the_primary_job_mapping() -> None:
    original = _job(_context("one", "El regreso de la momia 2001"))
    context = source_title_contexts(original, factory_identity_rules())[0]

    candidate = fallback_job(original, context)

    assert original["name"] == "nombre fisico inutilizable"
    assert candidate["name"] == "El regreso de la momia 2001"
    assert candidate["_source_primary_name"] == original["name"]
    assert candidate["_source_context_only"] is True


def test_only_identity_rejections_are_recoverable() -> None:
    assert recoverable_resolution_error(
        ResolverAmbiguous("sin candidatos", {"reason_code": "no_candidates"})
    )
    assert recoverable_resolution_error(
        ResolverAmbiguous("puntuación", {"top_score": 63, "margin": 20})
    )
    for reason in (
        "category_conflict",
        "category_not_resolvable",
        "forced_target_invalid",
        "forced_title_mismatch",
        "source_context_conflict",
    ):
        assert not recoverable_resolution_error(
            ResolverAmbiguous("bloqueado", {"reason_code": reason})
        )


def test_collections_packs_trilogies_and_year_ranges_never_use_fallback() -> None:
    rules = factory_identity_rules()
    for name in (
        "Saga Collection 1999",
        "Saga Pack 1999",
        "Saga Trilogy 1999",
        "Saga 1999-2021",
    ):
        job = _job(_context("one", "Obra individual 1999"))
        job["name"] = name
        assert source_fallback_block_reason(job, rules) == "ambiguous_collection"


def test_tv_broadcast_year_range_keeps_the_existing_parser_exception() -> None:
    rules = factory_identity_rules()
    job = _job(
        _context("one", "Serie S01E01", destination="tv"),
        category="tv",
    )
    job["name"] = "Serie S01 2010-2020"

    assert source_fallback_block_reason(job, rules) == ""
