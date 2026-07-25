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
                "parser_manual",
                "Derivacion manual",
                "Barreras que evitan consultar TMDb para nombres no audiovisuales o colecciones.",
                [
                    _control("parser.manual_keywords", "tags", "Palabras manuales", "Una coincidencia fuerza revision manual."),
                    _control("parser.manual_exact_names", "tags", "Nombres manuales exactos", "Titulos completos que no deben auto-clasificarse sin una categoria de origen fiable."),
                    _control("parser.collection_keywords", "tags", "Indicadores de coleccion", "Saga, pack o filmografia no se tratan como una sola obra."),
                    _control("parser.season_pack_markers", "tags", "Pack de temporada", "Marcas que convierten una temporada sin episodios en pack."),
                ],
            ),
            _group(
                "parser_patterns",
                "Patrones",
                "Expresiones regulares compiladas y validadas antes de guardar.",
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
                            ("series_sxe", "Serie SxxExx", "Temporada y episodio en formato SxxExx."),
                            ("series_x", "Serie 1x02", "Temporada y episodio en formato 1x02."),
                            ("explicit_season", "Temporada explicita", "Texto Temporada o Season seguido de numero."),
                            ("season_pack", "Pack Txx", "Temporada compacta en formato Txx."),
                            ("chapter", "Capitulo", "Capitulo simple o intervalo."),
                            ("episode_word", "Episodio", "Texto Episodio o Episode seguido de numero."),
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
                "Normalizacion",
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
                            ("smart_title", "Conservar titulo", "Mantiene el texto util sin capitalizacion destructiva."),
                        )
                    ],
                    _control(
                        "parser.normalization.tail_noise_passes",
                        "number",
                        "Pasadas de ruido final",
                        "Número máximo de capas de ruido final eliminadas.",
                        min=1,
                        max=100,
                        step=1,
                    ),
                    _control(
                        "parser.normalization.max_episode_range",
                        "number",
                        "Maximo intervalo de episodios",
                        "0 no limita; un valor positivo limita y recorta intervalos mayores.",
                        min=0,
                        max=1000,
                        step=1,
                    ),
                ],
            ),
        ],
    },
    "resolver": {
        "title": "Resolucion TMDb",
        "groups": [
            _group(
                "resolver_locales",
                "Idiomas y reglas directas",
                "Preferencias de consulta y excepciones expresamente aprobadas.",
                [
                    _control("resolver.locales.movies.language", "language", "Idioma peliculas", "Locale principal para peliculas."),
                    _control("resolver.locales.movies.region", "region", "Region peliculas", "Region de resultados TMDb."),
                    _control("resolver.locales.tv.language", "language", "Idioma series", "Locale principal para series."),
                    _control("resolver.locales.fallback_language", "language", "Idioma alternativo", "Segundo idioma de busqueda."),
                    _control("resolver.locales.use_fallback", "toggle", "Usar idioma alternativo", "Consulta el idioma alternativo cuando aporta candidatos."),
                    _control("resolver.aliases.movies", "mapping_rules", "Alias de peliculas", "Formato: origen | destino.", format="origen | destino"),
                    _control("resolver.aliases.tv", "mapping_rules", "Alias de series", "Formato: origen | destino.", format="origen | destino"),
                    _control("resolver.forced_matches.movies", "mapping_rules", "Coincidencias forzadas de peliculas", "Formato: titulo | año | tmdb_id.", format="titulo | año | tmdb_id"),
                    _control("resolver.forced_matches.tv", "mapping_rules", "Coincidencias forzadas de series", "Formato: titulo | tmdb_id o titulo | año | tmdb_id.", format="titulo | tmdb_id"),
                ],
            ),
            _group(
                "resolver_evidence",
                "Evidencias y candidato inicial",
                "Fuentes y pesos usados para construir el nombre consultado.",
                [
                    *[
                        _control(f"resolver.evidence.{key}", "toggle", label, help_text)
                        for key, label, help_text in (
                            ("use_job_name", "Nombre del trabajo", "Usa el nombre recibido por ARR."),
                            ("use_folder_name", "Nombre de carpeta", "Usa el nombre de la carpeta de entrada."),
                            ("use_media_files", "Nombres de archivos", "Usa archivos multimedia como evidencia adicional."),
                            ("sort_largest_first", "Archivos grandes primero", "Prioriza los archivos mas representativos."),
                        )
                    ],
                    _control("resolver.evidence.max_media_files", "number", "Maximo de archivos", "Limite de nombres de archivos examinados.", min=0, max=1000, step=1),
                    *[
                        _control(f"resolver.guess_selection.{key}", "number", label, help_text, min=minimum, max=maximum, step=1)
                        for key, label, help_text, minimum, maximum in (
                            ("base", "Base", "Puntuacion base de una evidencia.", 0, 1000),
                            ("index_penalty", "Penalizacion por orden", "Resta por cada evidencia posterior.", 0, 100),
                            ("year_bonus", "Bonus de año", "Suma cuando GuessIt obtiene año.", -500, 500),
                            ("season_bonus", "Bonus de temporada", "Suma cuando obtiene temporada.", -500, 500),
                            ("parser_high_bonus", "Bonus parser alto", "Suma si el parser tiene confianza alta.", -500, 500),
                        )
                    ],
                ],
            ),
            _group(
                "resolver_search",
                "Busqueda",
                "Variantes y limites duros de llamadas a TMDb.",
                [
                    *[
                        _control(f"resolver.query_variants.{key}", "toggle", label, help_text)
                        for key, label, help_text in (
                            ("with_year", "Buscar con año", "Incluye el año cuando existe."),
                            ("without_year", "Buscar sin año", "Prueba tambien una consulta mas amplia."),
                            ("use_parser_candidates", "Candidatos del parser", "Consulta las variantes de titulo del parser."),
                            ("use_guessit", "Titulo de GuessIt", "Incluye el titulo inferido por GuessIt."),
                            ("use_tail_cleanup", "Limpiar cola", "Prueba una variante sin ruido final."),
                            ("use_spanish_correction", "Correccion española", "Prueba la correccion conservadora de titulos españoles."),
                        )
                    ],
                    *[
                        _control(f"resolver.search_limits.{key}", "number", label, help_text, min=1, max=maximum, step=1)
                        for key, label, help_text, maximum in (
                            ("max_searches", "Maximo de consultas", "Tope total de busquedas TMDb.", 32),
                            ("results_per_search", "Resultados por consulta", "Resultados leidos de cada respuesta.", 100),
                            ("detail_candidates", "Detalles de candidatos", "Maximo de fichas completas descargadas.", 20),
                            ("initial_candidates", "Candidatos iniciales", "Primeros candidatos que pasan a detalle.", 20),
                        )
                    ],
                    _control("resolver.search_limits.include_exact_year_candidate", "toggle", "Incluir año exacto", "Reserva un candidato con el año exacto si no quedo arriba."),
                ],
            ),
            _group(
                "resolver_scoring",
                "Puntuacion",
                "Pesos del ranking. Los negativos penalizan contradicciones.",
                [
                    *[
                        _control(f"resolver.scoring.{key}", control_type, label, help_text, min=minimum, max=maximum, step=step)
                        for key, control_type, label, help_text, minimum, maximum, step in (
                            ("direct_identity", "number", "Identificador directo", "Puntuacion de TMDb/IMDb confirmado.", 0, 1000, 1),
                            ("title_exact", "number", "Titulo exacto", "Bonus por titulo exacto.", 0, 500, 1),
                            ("title_similarity_max", "number", "Similitud de titulo", "Peso maximo de similitud.", 0, 500, 1),
                            ("token_overlap_max", "number", "Coincidencia de palabras", "Peso maximo de tokens compartidos.", 0, 500, 1),
                            ("spanish_correction", "number", "Correccion española", "Bonus por correccion exacta.", 0, 500, 1),
                            ("parser_exact", "number", "Alias parser exacto", "Bonus por candidato exacto del parser.", 0, 500, 1),
                            ("parser_near", "number", "Alias parser cercano", "Bonus por candidato cercano.", 0, 500, 1),
                            ("parser_near_min", "decimal", "Minimo de cercania", "Similitud minima entre 0 y 1.", 0, 1, 0.01),
                            ("configured_alias", "number", "Alias configurado", "Bonus por alias aprobado.", 0, 500, 1),
                            ("year_exact", "number", "Año exacto", "Bonus por año igual.", 0, 500, 1),
                            ("year_near", "number", "Año cercano", "Bonus dentro de tolerancia.", 0, 500, 1),
                            ("year_tolerance", "number", "Tolerancia de año", "Diferencia maxima considerada cercana.", 0, 10, 1),
                            ("year_contradiction", "number", "Año contradictorio", "Penalizacion por año distinto.", -1000, 0, 1),
                            ("missing_movie_year", "number", "Año ausente", "Penalizacion si falta el año de pelicula.", -1000, 0, 1),
                            ("category", "number", "Categoria correcta", "Bonus por tipo de medio correcto.", 0, 500, 1),
                            ("origin_evidence", "number", "Evidencia de origen", "Bonus si otra evidencia confirma el titulo.", 0, 500, 1),
                            ("season_valid", "number", "Temporada valida", "Bonus si la temporada existe.", 0, 500, 1),
                            ("season_invalid", "number", "Temporada imposible", "Penalizacion si la temporada no existe.", -1000, 0, 1),
                        )
                    ],
                ],
            ),
            _group(
                "resolver_acceptance",
                "Aceptacion y validacion",
                "Umbrales que deciden si una identidad es segura y si la salida coincide.",
                [
                    *[
                        _control(f"resolver.acceptance.{key}", "number", label, help_text, min=minimum, max=1000, step=1)
                        for key, label, help_text, minimum in (
                            ("min_score", "Puntuacion minima", "Minimo para aceptar el primer candidato.", -1000),
                            ("min_margin", "Margen minimo", "Ventaja minima sobre el segundo candidato.", 0),
                            ("early_stop_score", "Corte temprano", "Puntuacion para dejar de buscar.", -1000),
                            ("early_stop_margin", "Margen de corte", "Margen para dejar de buscar.", 0),
                        )
                    ],
                    *[
                        _control(f"resolver.acceptance.{key}", "toggle", label, help_text)
                        for key, label, help_text in (
                            ("early_stop_require_exact_movie_year", "Exigir año exacto", "El corte temprano de peliculas exige año exacto."),
                            ("direct_ids_bypass", "ID directo evita umbrales", "TMDb/IMDb confirmado no compite por score."),
                            ("forced_bypass", "Forzado evita umbrales", "Una regla forzada validada no compite por score."),
                        )
                    ],
                    _control("resolver.forced_validation.min_title_similarity", "decimal", "Similitud forzada", "Minimo para validar el titulo de una regla forzada.", min=0, max=1, step=0.01),
                    _control("resolver.forced_validation.require_year", "toggle", "Validar año forzado", "Comprueba el año si la regla lo incluye."),
                    _control("resolver.output_validation.require_title_alias", "toggle", "Validar titulo de salida", "La salida debe coincidir con un alias resuelto."),
                    _control("resolver.output_validation.year_tolerance", "number", "Tolerancia de salida", "Diferencia de año permitida en la salida.", min=0, max=10, step=1),
                ],
            ),
            _group(
                "resolver_operations",
                "Red, reintentos y cache",
                "Limites operativos del resolver; no contiene tokens ni rutas.",
                [
                    _control("resolver.http.timeout_ms", "number", "Timeout HTTP", "Timeout de una llamada TMDb en milisegundos.", min=100, max=60000, step=100),
                    _control("resolver.http.total_budget_ms", "number", "Presupuesto total", "Tiempo total permitido por resolucion.", min=100, max=300000, step=100),
                    _control("resolver.retry.base_seconds", "number", "Reintento base", "Espera inicial tras indisponibilidad.", min=1, max=86400, step=1),
                    _control("resolver.retry.multiplier", "number", "Multiplicador", "Factor exponencial entre reintentos.", min=1, max=10, step=1),
                    _control("resolver.retry.max_exponent", "number", "Exponente maximo", "Limite del crecimiento exponencial.", min=0, max=16, step=1),
                    _control("resolver.retry.max_seconds", "number", "Espera maxima", "Tope absoluto entre reintentos.", min=1, max=604800, step=1),
                    _control("resolver.cache.enabled", "toggle", "Cache activa", "Interruptor principal de cache."),
                    _control("resolver.cache.ttl_seconds", "number", "Vida de cache", "Segundos hasta caducar una identidad.", min=60, max=31536000, step=60),
                    _control("resolver.cache.read_enabled", "toggle", "Leer cache", "Permite reutilizar identidades vigentes."),
                    _control("resolver.cache.write_enabled", "toggle", "Escribir cache", "Guarda resoluciones nuevas."),
                ],
            ),
        ],
    },
}


def identity_settings_schema() -> Dict[str, object]:
    """Devuelve una copia para que el frontend no mute el contrato global."""

    return copy.deepcopy(IDENTITY_SETTINGS_SCHEMA)
