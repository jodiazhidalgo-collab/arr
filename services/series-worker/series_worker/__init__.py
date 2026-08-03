"""Series Worker: reglas y manifiestos del procesado de episodios."""

from .core import SeriesCoordinator, validate_payload
from .manifest import ManifestError, ManifestSidecar, SeriesManifest, discover_manifest
from .processing import (
    AudioInvalidError,
    EpisodeProcessingError,
    OCRSubtitleError,
    ProcessingError,
    ProcessingResult,
    SeriesProcessor,
    SubtitleNotConvertibleError,
    VideoInvalidError,
    process_manifest,
)
from .rules import RulesConflictError, RulesSnapshot, RulesStore, RulesValidationError

__all__ = [
    "AudioInvalidError",
    "ManifestError",
    "ManifestSidecar",
    "EpisodeProcessingError",
    "OCRSubtitleError",
    "ProcessingError",
    "ProcessingResult",
    "RulesConflictError",
    "RulesSnapshot",
    "RulesStore",
    "RulesValidationError",
    "SeriesCoordinator",
    "SeriesManifest",
    "SeriesProcessor",
    "SubtitleNotConvertibleError",
    "VideoInvalidError",
    "discover_manifest",
    "process_manifest",
    "validate_payload",
]
