"""Estados canónicos del ciclo de vida de un trabajo ARR."""

from __future__ import annotations


TERMINAL_JOB_STATES = (
    "done",
    "manual_review",
    "duplicate",
    "error_terminal",
    "discarded",
)

PROCESSABLE_JOB_STATES = frozenset(
    {
        "waiting_stable",
        "ready_stage",
        "staging",
        "ready_extract",
        "extracting",
        "ready_filebot",
        "identity_retry",
        "filebot_running",
        "bluray_running",
        "media_postprocess_ready",
        "media_postprocess_running",
        "trailer_ready",
        "trailer_running",
        "verifying_output",
        "ready_cleanup",
    }
)

# Orden de avance usado únicamente para conservar el trabajo más adelantado
# al reparar duplicados legacy de un mismo infohash.
JOB_STATE_PROGRESS = {
    "source_submitted": 5,
    "received": 10,
    "waiting_materialization": 15,
    "waiting_stable": 20,
    "retry_wait": 25,
    "ready_stage": 30,
    "staging": 35,
    "ready_extract": 40,
    "extracting": 45,
    "ready_filebot": 50,
    "identity_retry": 50,
    "bluray_running": 55,
    "filebot_running": 60,
    "media_postprocess_ready": 65,
    "media_postprocess_running": 70,
    "trailer_ready": 75,
    "trailer_running": 80,
    "verifying_output": 85,
    "ready_cleanup": 90,
    "dry_run_ready": 20,
}
