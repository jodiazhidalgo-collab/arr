import pytest

from arr_orchestrator.identity.defaults import factory_identity_rules
from arr_orchestrator.identity.resolver.policy import effective_policy
from arr_orchestrator.identity.schema import identity_settings_schema
from arr_orchestrator.identity.validation import (
    IdentityRulesValidationError,
    normalize_identity_rules,
)


def test_runtime_budget_initializes_only_coverage_contract():
    rules = factory_identity_rules(
        resolver_http_timeout_ms=1_400,
        resolver_total_budget_ms=6_600,
    )
    resolver = rules["resolver"]

    assert resolver["http"] == {"timeout_ms": 1_400}
    assert resolver["coverage"]["total_budget_ms"] == 6_600
    assert "total_budget_ms" not in resolver["http"]


def test_budget_must_cover_each_http_request():
    rules = factory_identity_rules()
    rules["resolver"]["http"]["timeout_ms"] = 5_000
    rules["resolver"]["coverage"]["total_budget_ms"] = 4_999

    with pytest.raises(
        IdentityRulesValidationError,
        match=r"coverage\.total_budget_ms.*http\.timeout_ms",
    ):
        normalize_identity_rules(rules)


def test_legacy_duplicate_http_budget_is_not_part_of_v2_schema():
    rules = factory_identity_rules()
    rules["resolver"]["http"]["total_budget_ms"] = 20_000

    with pytest.raises(
        IdentityRulesValidationError,
        match=r"rules\.resolver\.http contiene campos no permitidos",
    ):
        normalize_identity_rules(rules)


def test_effective_policy_uses_runtime_budget_as_coverage_default():
    policy = effective_policy(
        None,
        "movies",
        default_http_timeout_ms=1_400,
        default_total_budget_ms=6_600,
    )

    assert policy["http"] == {"timeout_ms": 1_400}
    assert policy["coverage"]["total_budget_ms"] == 6_600

    snapshot = factory_identity_rules()
    snapshot["resolver"]["coverage"]["total_budget_ms"] = 7_700
    configured = effective_policy(
        snapshot,
        "movies",
        default_total_budget_ms=6_600,
    )
    assert configured["coverage"]["total_budget_ms"] == 7_700


def test_panel_exposes_the_budget_control_exactly_once():
    schema = identity_settings_schema("common")
    paths = [
        control["path"]
        for group in schema["resolver"]["groups"]
        for control in group["controls"]
    ]

    assert paths.count("resolver.coverage.total_budget_ms") == 1
    assert "resolver.http.total_budget_ms" not in paths
