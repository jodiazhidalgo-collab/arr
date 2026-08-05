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

El motor activo es `phased-er-v2`: descubre candidatos, los enriquece, elimina
contradicciones materiales, compara familias de evidencia y decide. No usa
pesos, score minimo, margen ni preferencia por antiguedad. Cada familia
(`title`, `year`, `runtime` y `episode`) produce una sola conclusion `AGREE`,
`DISAGREE` o `UNKNOWN`; la ausencia de un dato nunca se convierte en conflicto.

La seleccion restante es determinista: ano explicito exacto, mas evidencias
independientes concordantes, menos contradicciones, popularidad TMDb, votos,
estreno mas reciente y TMDb ID menor. Una ambiguedad normal se acepta como
`ACCEPTED_FALLBACK` y conserva alternativas visibles. Solo una contradiccion
dura produce `BLOCKED_HARD`; un fallo parcial o total de TMDb produce
`RETRY_PROVIDER` y nunca se cachea.

La cobertura amplia permite hasta 12 variantes, 60 IDs unicos, lotes de 8 y
40 peticiones de detalle dentro de un presupuesto comun de 20 segundos. Llegar
a un tope interno marca `coverage_limited=true` y acepta la opcion plausible
mas probable. Las peliculas comparan la cronologia completa de estrenos y la
duracion local; Series conserva una `EpisodeIntent` por cada archivo fisico y
comprueba temporadas, episodios, absolutos, especiales, dobles y packs.

Toda identidad aceptada entrega su TMDb ID explicito a FileBot. La cache usa
`resolver_cache_version=4` y las decisiones nuevas registran
`resolver_algorithm_version=phased-er-v2`. Common, Movies y TV se guardan en
ambitos v2 separados; al crear el trabajo se compone y congela la politica
efectiva con revisiones y huellas. Las configuraciones antiguas se migran una
sola vez y no pueden volver a ejecutar el motor anterior. Los diagnosticos
historicos siguen siendo legibles de forma pasiva.

La preview y los eventos de `job_events` exponen `decision.status`, candidato
elegido, alternativas, evidencias, contadores de fases y cobertura saneada. El
campo HTTP `ok` solo indica que la peticion se proceso; la decision funcional
siempre se interpreta mediante `decision.status`.

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
