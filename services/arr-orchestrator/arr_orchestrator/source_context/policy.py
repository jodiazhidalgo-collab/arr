"""Límites compartidos de retención y uso del contexto de origen."""

CONTEXT_TTL_SECONDS = 24 * 60 * 60
MAX_SOURCE_TITLES = 3
USABLE_DELIVERY_STATES = {"intent", "accepted", "already_present"}
DELIVERY_STATE_ORDER = {
    "intent": 0,
    "accepted": 1,
    "already_present": 1,
    "failed": 2,
}
