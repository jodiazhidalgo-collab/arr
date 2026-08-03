from __future__ import annotations

from arr_orchestrator.engine import (
    SERIES_REVIEW_REASON_FILES,
    SERIES_REVIEW_REASON_SPECS,
    SERIES_WORKER_REVIEW_CODES_V2,
)
from arr_orchestrator.filesystem import REASON_TEXT_FILES
from media_panel.server import REVIEW_REASON_KINDS
from series_worker import core as series_core


def test_series_review_contract_matches_across_services() -> None:
    worker_by_kind = {
        reason_kind: {
            "reason_code": reason_code,
            "reason_file": reason_file,
            "reason_title": reason_title,
            "summary": series_core._REVIEW_SUMMARIES[reason_kind],
        }
        for reason_code, (
            reason_kind,
            reason_file,
            reason_title,
        ) in series_core._REVIEW_TYPES.items()
    }

    assert set(worker_by_kind) == set(SERIES_WORKER_REVIEW_CODES_V2)
    for reason_kind, reason_code in SERIES_WORKER_REVIEW_CODES_V2.items():
        reason_file, reason_title, summary = SERIES_REVIEW_REASON_SPECS[reason_kind]
        assert worker_by_kind[reason_kind] == {
            "reason_code": reason_code,
            "reason_file": reason_file,
            "reason_title": reason_title,
            "summary": summary,
        }

    assert set(SERIES_REVIEW_REASON_FILES) == set(series_core._REVIEW_MARKERS)
    assert set(SERIES_REVIEW_REASON_FILES) <= REASON_TEXT_FILES
    assert set(SERIES_REVIEW_REASON_SPECS) <= REVIEW_REASON_KINDS
