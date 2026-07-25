"""Fachada estable del resolver de identidad ARR.

La implementacion vive en ``arr_orchestrator.identity.resolver`` para que este
punto de entrada historico no vuelva a convertirse en un archivo monolitico.
"""

from .identity.resolver.service import (
    FORCED_TITLE_SIMILARITY,
    IMDB_ID_PATTERN,
    MAX_DETAIL_CANDIDATES,
    MAX_TMDB_SEARCHES,
    MISSING_MOVIE_YEAR_PENALTY,
    RESOLVER_CACHE_VERSION,
    TMDB_BASE_URL,
    TMDB_ID_PATTERN,
    NameResolver,
    ResolutionError,
    ResolvedIdentity,
    ResolverAmbiguous,
    ResolverCandidate,
    ResolverUnavailable,
)

__all__ = [
    "FORCED_TITLE_SIMILARITY",
    "IMDB_ID_PATTERN",
    "MAX_DETAIL_CANDIDATES",
    "MAX_TMDB_SEARCHES",
    "MISSING_MOVIE_YEAR_PENALTY",
    "RESOLVER_CACHE_VERSION",
    "TMDB_BASE_URL",
    "TMDB_ID_PATTERN",
    "NameResolver",
    "ResolutionError",
    "ResolvedIdentity",
    "ResolverAmbiguous",
    "ResolverCandidate",
    "ResolverUnavailable",
]
