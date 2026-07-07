# ARR

ARR es el proyecto de automatizacion local para busquedas, descargas, diagnostico y postproceso de peliculas, series y trailers.

## Puntos de entrada

- Panel: definido por `ARR_PANEL_URL` en `.env`
- Compose local: `${ARR_ROOT}/docker-compose.yaml`
- Orquestador: `services/arr-orchestrator`
- Buscador puente: `services/buscador-puente-arr`
- Panel web: `services/media-panel`
- Worker media: `services/media-worker`

## Verdad del flujo

La verdad canonica del motor es:

1. `config/arr-orchestrator/orchestrator.db`
2. tabla `job_events`
3. `job_detail()`
4. traza viva `diagnostics/arr/...`
5. ZIP final `diagnosticos_codex/*.zip`

No se deben crear fuentes paralelas para estados, tiempos, decisiones o errores si pueden derivarse de `job_events`.

## Diagnostico

Para revisar un fallo, empieza por el Informe Codex del job y despues contrasta con la traza viva y `job_events`.

Lee tambien:

- `AGENTS.md`: reglas operativas locales, si existen en tu entorno privado.
- `README_DIAGNOSTICO_CODEX.md`: puente rapido para informes y trazas.
- `docs/AI_REVIEW.md`: guia publica para revision IA, GitHub Actions y evidencias pytest.

## Desarrollo local

```powershell
python -m venv _codex_runtime\tmp\venv-arr
_codex_runtime\tmp\venv-arr\Scripts\python.exe -m pip install -r requirements-dev.txt
_codex_runtime\tmp\venv-arr\Scripts\python.exe -m pytest
```

Los datos reales, backups, diagnosticos, runtime y caches quedan fuera de Git por `.gitignore`.
