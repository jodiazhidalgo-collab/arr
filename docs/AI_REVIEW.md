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

El camino normal de peliculas termina en Media Worker. En modo Series activo,
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
