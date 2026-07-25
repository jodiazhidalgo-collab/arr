"""Componentes desacoplados del resolver TMDb de ARR."""

from .service import (
    NameResolver,
    ResolutionError,
    ResolvedIdentity,
    ResolverAmbiguous,
    ResolverCandidate,
    ResolverUnavailable,
)

__all__ = [
    "NameResolver",
    "ResolutionError",
    "ResolvedIdentity",
    "ResolverAmbiguous",
    "ResolverCandidate",
    "ResolverUnavailable",
]
