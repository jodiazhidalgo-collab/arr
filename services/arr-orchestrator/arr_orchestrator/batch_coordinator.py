"""Contratos puros para coordinar padres e hijos de lotes ARR."""

from __future__ import annotations

import copy
from typing import Dict, Iterable, List, Optional, Tuple

from .name_resolver import ResolvedIdentity


TERMINAL_CHILD_STATES = {
    "done",
    "done_with_warnings",
    "manual_review",
    "duplicate",
    "error_terminal",
    "discarded",
}
ISSUE_CHILD_STATES = {
    "done_with_warnings",
    "manual_review",
    "duplicate",
    "error_terminal",
}


def child_source_meta(
    parent_meta: Dict[str, object],
    *,
    parent_job_id: str,
    item_key: str,
    index: int,
    total: int,
    episode_validation: str,
    episode_reason: str,
    inherit_identity: bool,
) -> Dict[str, object]:
    result = copy.deepcopy(parent_meta)
    result["batch"] = {
        "schema": "arr-batch-v1",
        "role": "child",
        "parent_job_id": parent_job_id,
        "item_key": item_key,
        "index": int(index),
        "total": int(total),
        "episode_validation": episode_validation,
        "episode_reason": episode_reason,
        "inherit_identity": bool(inherit_identity),
    }
    result.pop("series_worker", None)
    return result


def validate_episode_intent(
    identity: ResolvedIdentity,
    intent: Optional[Dict[str, object]],
    tv_rules: Optional[Dict[str, object]],
) -> Tuple[str, str]:
    if not isinstance(intent, dict):
        return "UNKNOWN", "episode_intent_missing"
    rules = tv_rules if isinstance(tv_rules, dict) else {}
    season = _optional_int(intent.get("season"))
    episodes = _int_list(intent.get("episodes"))
    absolute = _optional_int(intent.get("absolute_episode"))
    is_pack = bool(intent.get("is_season_pack"))
    is_special = bool(intent.get("is_special")) or season == 0

    if len(episodes) > 1 and not bool(rules.get("allow_multi_episode", True)):
        return "DISAGREE", "multi_episode_disabled"
    if absolute is not None and not bool(rules.get("allow_absolute_episode", True)):
        return "DISAGREE", "absolute_episode_disabled"
    if is_special and not bool(rules.get("allow_specials", True)):
        return "DISAGREE", "special_disabled"
    if is_pack and not bool(rules.get("allow_season_packs", True)):
        return "DISAGREE", "season_pack_disabled"

    if season is not None:
        season_state = _season_state(identity, season)
        if season_state is False:
            return "DISAGREE", "season_not_found"
        if is_pack:
            return ("AGREE", "season_found") if season_state else ("UNKNOWN", "season_unknown")
        if episodes:
            episode_state = _episode_state(identity, season, episodes)
            if episode_state is False:
                return "DISAGREE", "episode_not_found"
            if episode_state is True:
                return "AGREE", "episode_found"
            return "UNKNOWN", "episode_unknown"

    if absolute is not None:
        count = sum(
            int(value)
            for key, value in identity.season_episode_counts.items()
            if int(key) > 0 and int(value) >= 0
        )
        if count <= 0:
            count = sum(
                len(values)
                for key, values in identity.known_episodes.items()
                if int(key) > 0
            )
        if count <= 0:
            return "UNKNOWN", "absolute_episode_unknown"
        return (
            ("AGREE", "absolute_episode_found")
            if 1 <= absolute <= count
            else ("DISAGREE", "absolute_episode_not_found")
        )

    return "UNKNOWN", "episode_intent_missing"


def narrowed_identity(
    identity: ResolvedIdentity,
    intent: Dict[str, object],
    validation: str,
) -> ResolvedIdentity:
    payload = identity.to_dict()
    payload["source"] = "batch_parent"
    payload["season"] = _optional_int(intent.get("season"))
    payload["episodes"] = _int_list(intent.get("episodes"))
    payload["episode_intents"] = [copy.deepcopy(intent)]
    if validation == "UNKNOWN" or str(payload.get("decision_status") or "").upper() != "ACCEPTED_CONFIDENT":
        payload["decision_status"] = "ACCEPTED_FALLBACK"
    evidence = [
        dict(item)
        for item in payload.get("evidence_summary") or []
        if isinstance(item, dict) and item.get("family") != "batch_episode"
    ]
    evidence.append(
        {
            "family": "batch_episode",
            "state": validation,
            "verdict": validation,
            "value": {"source": intent.get("source")},
        }
    )
    payload["evidence_summary"] = evidence
    return ResolvedIdentity.from_dict(payload)


def batch_counts(children: Iterable[Dict[str, object]]) -> Dict[str, int]:
    items = list(children)
    return {
        "total": len(items),
        "completed": sum(str(item.get("state") or "") in TERMINAL_CHILD_STATES for item in items),
        "succeeded": sum(str(item.get("state") or "") == "done" for item in items),
        "issues": sum(str(item.get("state") or "") in ISSUE_CHILD_STATES for item in items),
    }


def _season_state(identity: ResolvedIdentity, season: int) -> Optional[bool]:
    if season in identity.known_episodes:
        return bool(identity.known_episodes[season])
    if season in identity.season_episode_counts:
        return int(identity.season_episode_counts[season]) > 0
    if season > 0 and identity.season_count is not None:
        return season <= int(identity.season_count)
    return None


def _episode_state(
    identity: ResolvedIdentity, season: int, episodes: List[int]
) -> Optional[bool]:
    if season in identity.known_episodes:
        known = set(identity.known_episodes[season])
        return bool(known) and all(episode in known for episode in episodes)
    if season in identity.season_episode_counts:
        count = int(identity.season_episode_counts[season])
        return count > 0 and all(1 <= episode <= count for episode in episodes)
    return None


def _optional_int(value: object) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int_list(value: object) -> List[int]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: List[int] = []
    for raw in values:
        parsed = _optional_int(raw)
        if parsed is not None and parsed not in result:
            result.append(parsed)
    return result


__all__ = [
    "ISSUE_CHILD_STATES",
    "TERMINAL_CHILD_STATES",
    "batch_counts",
    "child_source_meta",
    "narrowed_identity",
    "validate_episode_intent",
]
