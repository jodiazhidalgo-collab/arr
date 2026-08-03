# Revision IA de ARR

Este documento es la guia publica y segura para revisar ARR desde GitHub, ChatGPT, Codex o cualquier sandbox externa.

## Mapa tecnico del proyecto

ARR es el proyecto de automatizacion local para busquedas, descargas, diagnostico y postproceso de peliculas, series y trailers.

Puntos de entrada:

- Panel: definido por `ARR_PANEL_URL` en `.env`
- Compose local: `${ARR_ROOT}/docker-compose.yaml`
- Orquestador: `services/arr-orchestrator`
- Buscador puente: `services/buscador-puente-arr`
- Panel web: `services/media-panel`
- Worker media: `services/media-worker`
- Worker de series: `services/series-worker`

La unica entrada automatica de peliculas es `complete/movies`: pasa por taller,
identidad, FileBot y termina en Media Worker. En modo Series activo,
FileBot escribe primero en una salida provisional privada del trabajo y Series
Worker congela reglas y manifiesto, procesa los episodios, verifica la
publicacion y deja un resultado durable que vuelve a validar el orquestador.

La verdad canonica del motor es:

1. `config/arr-orchestrator/orchestrator.db`
2. tabla `job_events`
3. `job_detail()`
4. traza viva `diagnostics/arr/...`
5. ZIP final `diagnosticos_codex/*.zip`

No se deben crear fuentes paralelas para estados, tiempos, decisiones o errores si pueden derivarse de `job_events`.

Para revisar un fallo, empieza por el Informe Codex del job y despues contrasta con la traza viva y `job_events`.

## Que debe mirar primero una IA

1. `README.md`: portada minima del repositorio.
2. `AGENTS.md`: reglas operativas locales del proyecto.
3. `README_DIAGNOSTICO_CODEX.md`: orden recomendado para leer informes Codex, trazas y errores.
4. `.github/workflows/ci.yml`: pruebas automaticas que GitHub ejecuta en cada push o pull request.
5. Artefactos `arr-pytest-evidence-windows-latest` y `arr-pytest-evidence-ubuntu-latest` de GitHub Actions: informes JUnit y validaciones estaticas descargables.
6. `docs/evidencia-pytest-y-validacion-local.md`: como reproducir las pruebas desde cero.

## Verdad tecnica del flujo

La fuente principal de estados, tiempos, decisiones y errores debe salir de:

1. `config/arr-orchestrator/orchestrator.db`
2. tabla `job_events`
3. `job_detail()`
4. traza viva `diagnostics/arr/...`
5. ZIP final `diagnosticos_codex/*.zip`

No se deben inventar fuentes paralelas si esos datos ya pueden derivarse de `job_events`.

## Identidad TMDb y evidencia de titulo

El parser conserva `title_candidates` por compatibilidad y añade
`title_evidence`, donde cada titulo lleva `value`, `role`, `source` y
`group_id`. Los roles separan titulo primario, alternativo, compuesto,
derivado de serie y alias configurado. Marcadores editoriales como
`Extended Edition` se eliminan como descriptor: no se convierten en un titulo
alternativo ni pueden activar un fallback, tampoco al reconstruir snapshots
legacy.

El resolver separa puntuacion y elegibilidad. Un candidato puede sumar puntos
por similitud sin quedar autorizado para decidir. En entradas con varios
titulos se aplican estas barreras:

- La corroboracion exige coincidencia exacta normalizada de las dos mitades del mismo grupo.
- Un alias configurado exacto conserva autoridad explicita salvo que contradiga un ano presente en la entrada.
- Un titulo primario rival solo cuenta como confirmacion si su coincidencia es exacta.
- Un alternativo aislado solo puede ganar como fallback estricto si es unico, tiene el ano exacto y, en TV, la temporada solicitada existe.
- `allow_omitted_part_number` conserva puntos y `matching_rules`, pero nunca cuenta como coincidencia exacta de identidad, corroboracion ni fallback; esa diferencia queda visible en `title_matches.identity_exact`.
- Conflictos de evidencia, seleccion incompleta o temporada imposible pasan a revision; TV no los oculta continuando por la senal local.
- IDs directos y reglas forzadas conservan sus validaciones y bypass configurados.

Las consultas usan estrategia `phased_round_robin`: primero reparten el
presupuesto entre evidencia primaria, compuesta y alternativa, y despues
relajan ano o idioma. El limite duro es 8 busquedas y 3 fichas detalladas. Los
candidatos de procedencia fuerte se reservan antes de rellenar huecos con
resultados solo alternativos. Si quedan rivales fuertes o exactos fuera del
limite, o falla una ficha detallada, `selection_uncertain` bloquea cualquier
conclusion que dependa de una falsa unicidad. La excepcion segura es una ficha
ya detallada que confirma exactamente todos los titulos atomicos del mismo
grupo: esa corroboracion tiene prioridad si los unicos rivales omitidos son
solo alternativos, no fallo ningun detalle y no existe un alias configurado
valido.

Si la propia ficha TMDb devuelve `Exterior (Interior)` y confirma por separado
el titulo interior, el resolver puede extraer el exterior para comparar ambos
atomos. No se hace la inferencia inversa: el literal compuesto aislado no basta
y un calificador interior nunca se convierte en titulo por esta via.

En una entrada realmente multiatomica, si la primera consulta principal
devuelve de forma exacta el titulo alternativo, el resolver puede adelantar una
sola ficha para comprobar sus alias. Ese intento se descuenta del mismo limite
de 3, se reutiliza al final y nunca se ejecuta para titulos simples. Un fallo o
un presupuesto agotado marca detalle incompleto y fuerza revision.
`search_strategy` deja esta ruta auditable mediante `early_detail_attempted`,
`early_detail_reused`, `detail_requests` y, cuando corresponda,
`detail_incomplete`.

La cache usa `resolver_cache_version=3` y cada identidad nueva registra
`resolver_algorithm_version=title-evidence-v1`. La preview y el evento
existente de fase `identity` (emitido como `resolved` y normalizado a
`decision`) exponen la decision, elegibilidad y procedencia saneadas.
`search_provenance` no conserva la consulta cruda; los detalles humanos siguen
pasando por el saneador antes de llegar a `job_events` o al Informe Codex.
Cuando la barrera de evidencia bloquea la identidad, preview y API conservan
el estado compatible `REJECTED` en el nivel superior y en `decision.status`.
La causa nueva queda diferenciada mediante `eligibility_blocked` y
`eligibility_reason`, sin reclasificarla despues como score o margen.

Pruebas focales desde `services/arr-orchestrator`:

```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_name_parser.py tests/test_resolver_policy.py tests/test_name_resolver.py -q
python -m pytest tests/test_identity_controller.py tests/test_identity_health.py -q
```

## Proteccion del vigilante

Los eventos del filesystem entran en una bandeja acotada y se agrupan por item
superior. El primer evento util se procesa sin debounce; los eventos repetidos
de una carpeta no crean trabajo adicional. Si la bandeja alcanza su limite, el
orquestador solicita una reconciliacion inmediata con filesystem, RDT y qB para
recuperar cualquier item sin bloquear el flujo normal.

`/health` conserva `queue_size` y expone tambien `watcher_events` con capacidad,
recibidos, agrupados, desbordados, pendientes, maximo alcanzado y si queda una
reconciliacion solicitada.

## Pruebas seguras

Desde la raiz del repo:

```powershell
python -m pip install -r requirements-dev.txt
python -m compileall -q conftest.py services tests
node --check services/media-panel/media_panel/web/static/js/panel.js
python -m pytest -q --junitxml _codex_runtime/artifacts/pytest-junit.xml --durations=20
```

Los tests live quedan desactivados salvo que se definan variables explicitas como `RUN_ENGINE_LIVE_TESTS`, `RUN_FILEBOT_LIVE_TESTS` o `TMDB_API_TOKEN`.

## Que no esta en Git

Por seguridad, el repositorio publico no debe incluir:

- `.env`
- `config/`
- `diagnostics/`
- `diagnosticos_codex/`
- `_codex_runtime/`
- `backups/`
- bases de datos, logs, caches o ZIPs generados

Si una IA necesita diagnosticar un fallo real, hay que darle el ZIP del Informe Codex o el artefacto de GitHub Actions correspondiente, no secretos ni datos privados sueltos.
