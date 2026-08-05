"""Metadatos visuales del editor de identidad.

Los paths son relativos al objeto ``rules``. El frontend no necesita conocer la
implementacion del motor: puede construir grupos y controles con este contrato.
"""

from __future__ import annotations

import copy
from typing import Dict, List


def _control(
    path: str,
    control_type: str,
    label: str,
    help_text: str,
    **metadata: object,
) -> Dict[str, object]:
    result: Dict[str, object] = {
        "path": path,
        "type": control_type,
        "label": label,
        "help": help_text,
    }
    result.update(metadata)
    return result


def _group(
    group_id: str,
    title: str,
    description: str,
    controls: List[Dict[str, object]],
) -> Dict[str, object]:
    return {
        "id": group_id,
        "title": title,
        "description": description,
        "controls": controls,
    }


IDENTITY_SETTINGS_SCHEMA: Dict[str, object] = {
    "parser": {
        "title": "Lectura del nombre",
        "groups": [
            _group(
                "parser_cleanup",
                "Limpieza inicial",
                "Elementos que se eliminan antes de detectar titulo, año y episodios.",
                [
                    _control("parser.extensions", "tags", "Extensiones conocidas", "Se quita una extension final coincidente."),
                    _control("parser.site_words", "tags", "Marcas de sitios", "Palabras de origen que no forman parte del titulo."),
                    _control("parser.domain_tlds", "tags", "Dominios", "Sufijos usados para reconocer dominios incrustados."),
                    _control("parser.technical_tokens", "tags", "Etiquetas tecnicas", "Calidad, codec, fuente y audio que cortan el titulo."),
                    _control("parser.tail_noise_tokens", "tags", "Ruido final", "Tokens tecnicos que se eliminan del final del titulo."),
                    _control("parser.language_tokens", "tags", "Etiquetas de idioma", "Palabras de idioma que no pertenecen al titulo."),
                    _control(
                        "parser.ocr_replacements",
                        "regex_pairs",
                        "Correcciones OCR",
                        "Pares expresion regular y reemplazo aplicados en orden.",
                        columns=["pattern", "replacement"],
                    ),
                ],
            ),
            _group(
                "parser_series",
                "Series y episodios",
                "Formas humanas de reconocer temporadas, episodios y rangos de emision.",
                [
                    _control("parser.season_pack_markers", "tags", "Pack de temporada", "Marcas que convierten una temporada sin episodios en pack."),
                    _control(
                        "parser.season_number_words",
                        "mapping_rules",
                        "Numeros de temporada escritos",
                        "Convierte palabras solo cuando acompañan a Temporada o Season.",
                        format="palabra | numero",
                    ),
                    _control("parser.normalization.allow_tv_year_range", "toggle", "Permitir rango de años en series", "Un rango de emision no fuerza revision manual cuando el nombre ya contiene una señal clara de serie."),
                    _control("parser.normalization.max_episode_range", "number", "Maximo intervalo de episodios", "0 no limita; un valor positivo limita y recorta intervalos mayores.", min=0, max=1000, step=1),
                    *[
                        _control(f"parser.patterns.{key}", "regex", label, help_text)
                        for key, label, help_text in (
                            ("series_sxe", "Serie SxxExx", "Temporada y episodio en formato SxxExx."),
                            ("series_x", "Serie 1x02", "Temporada y episodio en formato 1x02."),
                            ("explicit_season", "Temporada escrita", "Reconoce Temporada, Season, Temp, Sezon, expresiones como 1 série y miniseries."),
                            ("season_pack", "Pack de temporada", "Temporada compacta en formato Sxx o Txx."),
                            ("chapter", "Capítulo", "Capítulo simple o intervalo."),
                            ("episode_word", "Episodio", "Texto Episodio o Episode seguido de número."),
                        )
                    ],
                ],
            ),
            _group(
                "parser_classification",
                "Clasificación automática",
                "Señales de vídeo usadas para decidir película o revisión manual cuando falta el año.",
                [
                    _control("parser.video_extensions", "tags", "Extensiones de vídeo", "Extensiones que aportan una señal audiovisual aunque falte el año."),
                    _control("parser.video_markers", "tags", "Marcas de vídeo", "Calidades, fuentes y codecs que permiten reconocer una película sin año."),
                    _control("parser.non_video_markers", "tags", "Marcas no audiovisuales", "Bloquean la clasificación automática de paquetes que no son películas ni series."),
                    _control("parser.normalization.movie_without_year_from_video", "toggle", "Película sin año por señal de vídeo", "Permite clasificar una película sin año cuando existe una señal de vídeo y no hay señales de serie, colección o contenido no audiovisual."),
                ],
            ),
            _group(
                "parser_manual",
                "Derivación manual",
                "Barreras que evitan consultar TMDb para nombres no audiovisuales o colecciones.",
                [
                    _control("parser.manual_keywords", "tags", "Palabras manuales", "Una coincidencia fuerza revision manual."),
                    _control("parser.manual_exact_names", "tags", "Nombres manuales exactos", "Titulos completos que no deben auto-clasificarse sin una categoria de origen fiable."),
                    _control("parser.collection_keywords", "tags", "Indicadores de coleccion", "Saga, pack o filmografia no se tratan como una sola obra."),
                ],
            ),
            _group(
                "parser_advanced_patterns",
                "Patrones avanzados",
                "Años, colecciones y limpieza especial mediante expresiones regulares validadas.",
                [
                    _control("parser.year.pattern", "regex", "Año", "Patron con el año en el primer grupo capturado."),
                    _control("parser.year.min", "number", "Año minimo", "Primer año aceptado.", min=1800, max=2200, step=1),
                    _control("parser.year.max", "number", "Año maximo", "Ultimo año aceptado.", min=1800, max=2200, step=1),
                    _control(
                        "parser.year.multiple",
                        "select",
                        "Varios años",
                        "Que coincidencia usar si aparecen varios años.",
                        options=[
                            {"value": "first", "label": "Primero"},
                            {"value": "last", "label": "Ultimo"},
                            {"value": "manual", "label": "Revision manual"},
                        ],
                    ),
                    *[
                        _control(f"parser.patterns.{key}", "regex", label, help_text)
                        for key, label, help_text in (
                            ("collection_count", "Cantidad de peliculas", "Colecciones expresadas como numero de peliculas."),
                            ("collection_part", "Parte de coleccion", "Expresiones tipo parte 1 de 3."),
                            ("year_range", "Intervalo de años", "Intervalos que indican coleccion o filmografia."),
                            ("domain", "Dominio web", "Dominio incrustado en el release."),
                            ("parenthesized_title", "Título entre paréntesis", "Separa títulos alternativos escritos entre paréntesis."),
                            ("compact_web", "Etiqueta WEB compacta", "Elimina marcas unidas como WEBRip1080p o 4KWEBDL2160p."),
                        )
                    ],
                ],
            ),
            _group(
                "parser_normalization",
                "Normalización",
                "Orden de limpieza estable antes de producir candidatos.",
                [
                    *[
                        _control(f"parser.normalization.{key}", "toggle", label, help_text)
                        for key, label, help_text in (
                            ("strip_extension", "Quitar extension", "Elimina una extension conocida al final."),
                            ("strip_duplicate_suffix", "Quitar sufijo duplicado", "Elimina sufijos de copia como (1) o identificadores internos."),
                            ("normalize_ocr", "Corregir OCR", "Aplica las correcciones OCR configuradas."),
                            ("normalize_dashes", "Normalizar guiones", "Unifica guiones tipograficos y separadores."),
                            ("replace_dots_underscores", "Puntos y guiones bajos", "Los convierte en espacios."),
                            ("strip_brackets", "Quitar corchetes", "Retira corchetes y llaves del release."),
                            ("collapse_whitespace", "Compactar espacios", "Reduce espacios repetidos."),
                        )
                    ],
                    _control("parser.normalization.tail_noise_passes", "number", "Pasadas de ruido final", "Número máximo de capas de ruido final eliminadas.", min=1, max=100, step=1),
                ],
            ),
        ],
    },
    "resolver": {"title": "", "groups": []},
}


_PARSER_SETTINGS_SCHEMA = copy.deepcopy(IDENTITY_SETTINGS_SCHEMA["parser"])


def _common_resolver_schema() -> Dict[str, object]:
    return {
        "title": "Identidad por evidencias",
        "groups": [
            _group(
                "resolver_algorithm",
                "Algoritmo",
                "Resolver v2 por fases, sin pesos ni margenes numericos.",
                [
                    _control(
                        "resolver.algorithm",
                        "select",
                        "Algoritmo activo",
                        "Contrato estable del resolver.",
                        options=[{"value": "phased-er-v2", "label": "Phased ER v2"}],
                    ),
                    _control("resolver.locales.fallback_language", "language", "Idioma alternativo", "Segundo idioma de busqueda."),
                    _control("resolver.locales.use_fallback", "toggle", "Usar idioma alternativo", "Amplia cobertura cuando el idioma principal no basta."),
                    _control("resolver.locales.movies.language", "language", "Idioma peliculas", "Idioma principal de peliculas."),
                    _control("resolver.locales.movies.region", "region", "Region peliculas", "Region principal de peliculas."),
                    _control("resolver.locales.tv.language", "language", "Idioma series", "Idioma principal de series."),
                    _control("resolver.aliases.movies", "mapping_rules", "Alias de peliculas", "Formato origen | destino.", format="origen | destino"),
                    _control("resolver.aliases.tv", "mapping_rules", "Alias de series", "Formato origen | destino.", format="origen | destino"),
                    _control("resolver.forced_matches.movies", "mapping_rules", "TMDb forzado peliculas", "Formato titulo | año | tmdb_id.", format="titulo | año | tmdb_id"),
                    _control("resolver.forced_matches.tv", "mapping_rules", "TMDb forzado series", "Formato titulo | tmdb_id o titulo | año | tmdb_id.", format="titulo | tmdb_id"),
                ],
            ),
            _group(
                "resolver_evidence",
                "Fuentes de evidencia",
                "Cada familia se cuenta una sola vez como acuerdo, desacuerdo o desconocida.",
                [
                    _control("resolver.evidence.use_job_name", "toggle", "Nombre del trabajo", "Usa el nombre recibido por ARR."),
                    _control("resolver.evidence.use_folder_name", "toggle", "Nombre de carpeta", "Usa la carpeta de entrada."),
                    _control("resolver.evidence.use_media_files", "toggle", "Archivos multimedia", "Usa cada archivo como evidencia independiente."),
                    _control("resolver.evidence.max_media_files", "number", "Maximo de archivos", "Limite de archivos examinados.", min=0, max=1000, step=1),
                    _control("resolver.evidence.sort_largest_first", "toggle", "Mayores primero", "Prioriza los archivos principales."),
                ],
            ),
            _group(
                "resolver_queries",
                "Consultas y comparacion",
                "Variantes de descubrimiento y equivalencias de titulo.",
                [
                    *[
                        _control(f"resolver.query_variants.{key}", "toggle", label, help_text)
                        for key, label, help_text in (
                            ("with_year", "Buscar con año", "Incluye el año cuando existe."),
                            ("without_year", "Buscar sin año", "Amplia cobertura sin forzar el año."),
                            ("use_parser_candidates", "Titulos del parser", "Consulta los titulos estructurados del parser."),
                            ("use_guessit", "Titulo GuessIt", "Incluye el titulo reconocido por GuessIt."),
                            ("use_tail_cleanup", "Limpiar ruido final", "Reintenta sin etiquetas tecnicas finales."),
                            ("use_spanish_correction", "Correccion española", "Prueba variantes ortograficas seguras."),
                        )
                    ],
                    _control("resolver.title_matching.roman_arabic_equivalence", "toggle", "Romanos y arabigos", "Considera equivalentes numeros romanos y arabigos."),
                    _control("resolver.title_matching.allow_omitted_part_number", "toggle", "Parte omitida", "Permite omitir un numero de saga bajo reglas seguras."),
                    _control("resolver.title_matching.omitted_part_min_words", "number", "Palabras minimas", "Minimo para una parte omitida.", min=1, max=20, step=1),
                    _control("resolver.title_matching.supplemental_min_chars", "number", "Longitud alternativa", "Longitud minima de un titulo alternativo.", min=1, max=100, step=1),
                ],
            ),
            _group(
                "resolver_coverage",
                "Cobertura adaptativa",
                "Topes duros para descubrir y enriquecer candidatos.",
                [
                    _control("resolver.coverage.max_searches", "number", "Busquedas", "Maximo 12 consultas TMDb.", min=1, max=12, step=1),
                    _control("resolver.coverage.max_candidates", "number", "IDs candidatos", "Maximo 60 IDs unicos.", min=1, max=60, step=1),
                    _control("resolver.coverage.batch_size", "number", "Lote", "Candidatos por lote adaptativo.", min=1, max=8, step=1),
                    _control("resolver.coverage.max_details", "number", "Detalles", "Maximo 40 fichas enriquecidas.", min=1, max=40, step=1),
                    _control("resolver.coverage.total_budget_ms", "number", "Presupuesto", "Tiempo total del resolver.", min=100, max=300000, step=100),
                ],
            ),
            _group(
                "resolver_adjudication",
                "Adjudicacion",
                "La ambiguedad normal elige la identidad mas probable con orden estable.",
                [
                    _control("resolver.adjudication.mode", "select", "Modo", "Modo v2 de adjudicacion.", options=[{"value": "most_probable", "label": "Mas probable"}]),
                    _control("resolver.adjudication.tie_breakers", "ordered_tags", "Desempates", "Orden canonico: año, acuerdos, desacuerdos, popularidad, votos, año nuevo e ID menor.", readonly=True),
                ],
            ),
            _group(
                "resolver_operations",
                "Red, reintentos y cache",
                "Limites operativos del resolver.",
                [
                    _control("resolver.http.timeout_ms", "number", "Timeout HTTP", "Timeout por llamada TMDb.", min=100, max=60000, step=100),
                    _control("resolver.retry.base_seconds", "number", "Reintento base", "Espera inicial.", min=1, max=86400, step=1),
                    _control("resolver.retry.multiplier", "number", "Multiplicador", "Factor exponencial.", min=1, max=10, step=1),
                    _control("resolver.retry.max_exponent", "number", "Exponente maximo", "Limite del crecimiento.", min=0, max=16, step=1),
                    _control("resolver.retry.max_seconds", "number", "Espera maxima", "Tope entre reintentos.", min=1, max=604800, step=1),
                    _control("resolver.retry.max_attempts", "number", "Intentos maximos", "Tras tres fallos queda pendiente manualmente.", min=1, max=10, step=1),
                    _control("resolver.cache.enabled", "toggle", "Cache activa", "Interruptor principal."),
                    _control("resolver.cache.ttl_seconds", "number", "Vida de cache", "Segundos hasta caducar.", min=60, max=31536000, step=60),
                    _control("resolver.cache.read_enabled", "toggle", "Leer cache", "Reutiliza identidades v2."),
                    _control("resolver.cache.write_enabled", "toggle", "Escribir cache", "Guarda identidades v2."),
                    _control("resolver.output_validation.require_title_alias", "toggle", "Validar salida", "La salida debe coincidir con la identidad."),
                    _control("resolver.output_validation.year_tolerance", "number", "Tolerancia de salida", "Diferencia de año permitida.", min=0, max=10, step=1),
                ],
            ),
        ],
    }


def _category_resolver_schema(profile: str) -> Dict[str, object]:
    if profile == "movies":
        controls = [
            _control("resolver.movies.year_tolerance", "number", "Tolerancia de año", "Diferencia permitida en el timeline.", min=0, max=5, step=1),
            _control("resolver.movies.use_release_timeline", "toggle", "Timeline de estrenos", "Compara todos los estrenos conocidos."),
            _control("resolver.movies.hard_year_conflict", "toggle", "Año contradictorio", "Elimina candidatos con contradiccion real de año."),
            _control("resolver.movies.runtime_tolerance_minutes", "number", "Tolerancia minutos", "Margen absoluto de duracion.", min=0, max=120, step=1),
            _control("resolver.movies.runtime_tolerance_percent", "number", "Tolerancia porcentual", "Margen relativo de duracion.", min=0, max=100, step=1),
            _control("resolver.movies.short_runtime_minutes", "number", "Corto", "Umbral maximo de cortometraje.", min=1, max=180, step=1),
            _control("resolver.movies.feature_runtime_minutes", "number", "Largometraje", "Umbral minimo de largometraje.", min=1, max=300, step=1),
        ]
        title = "Reglas de peliculas"
    else:
        controls = [
            *[
                _control(f"resolver.tv.{key}", "toggle", label, help_text)
                for key, label, help_text in (
                    ("validate_season", "Validar temporada", "Comprueba que la temporada exista."),
                    ("validate_episode", "Validar episodio", "Comprueba episodios bajo demanda."),
                    ("allow_absolute_episode", "Episodio absoluto", "Conserva numeracion absoluta."),
                    ("allow_specials", "Especiales", "Permite temporada 0."),
                    ("allow_season_packs", "Pack de temporada", "Permite lotes sin episodio concreto."),
                    ("allow_multi_episode", "Varios episodios", "Conserva rangos y episodios multiples."),
                )
            ],
            _control("resolver.tv.runtime_tolerance_minutes", "number", "Tolerancia minutos", "Margen absoluto por episodio.", min=0, max=120, step=1),
            _control("resolver.tv.runtime_tolerance_percent", "number", "Tolerancia porcentual", "Margen relativo por episodio.", min=0, max=100, step=1),
        ]
        title = "Reglas de series"
    return {
        "title": title,
        "groups": [
            _group(
                f"resolver_{profile}",
                title,
                "Overrides efectivos del perfil; Common aporta el resto.",
                controls,
            )
        ],
    }


def identity_settings_schema(profile: str = "common") -> Dict[str, object]:
    """Schema v2 filtrado: Common compartido y perfiles solo overrides."""

    normalized = str(profile or "common").strip().lower()
    if normalized not in {"common", "movies", "tv"}:
        normalized = "common"
    result: Dict[str, object] = {
        "schema_version": 2,
        "profile": normalized,
        "resolver": (
            _common_resolver_schema()
            if normalized == "common"
            else _category_resolver_schema(normalized)
        ),
    }
    if normalized == "common":
        result["parser"] = copy.deepcopy(_PARSER_SETTINGS_SCHEMA)
    return result


# Export estable para consumidores que importaban la constante directamente.
IDENTITY_SETTINGS_SCHEMA = identity_settings_schema("common")
