"""Series Worker: reglas y manifiestos del procesado de episodios."""

from .core import SeriesCoordinator, validate_payload
from .manifest import ManifestError, ManifestSidecar, SeriesManifest, discover_manifest
from .processing import (
    EpisodeProcessingError,
    ProcessingError,
    ProcessingResult,
    SeriesProcessor,
    process_manifest,
)
from .rules import RulesConflictError, RulesSnapshot, RulesStore, RulesValidationError

__all__ = [
    "ManifestError",
    "ManifestSidecar",
    "EpisodeProcessingError",
    "ProcessingError",
    "ProcessingResult",
    "RulesConflictError",
    "RulesSnapshot",
    "RulesStore",
    "RulesValidationError",
    "SeriesCoordinator",
    "SeriesManifest",
    "SeriesProcessor",
    "discover_manifest",
    "process_manifest",
    "validate_payload",
]
