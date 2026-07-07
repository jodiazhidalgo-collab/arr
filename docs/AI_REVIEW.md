# Revision IA de ARR

Este documento es la guia publica y segura para revisar ARR desde GitHub, ChatGPT, Codex o cualquier sandbox externa.

## Que debe mirar primero una IA

1. `README.md`: puntos de entrada y verdad canonica del flujo.
2. `README_DIAGNOSTICO_CODEX.md`: orden recomendado para leer informes Codex, trazas y errores.
3. `.github/workflows/ci.yml`: pruebas automaticas que GitHub ejecuta en cada push o pull request.
4. Artefacto `arr-pytest-evidence` de GitHub Actions: informe JUnit de pytest descargable.
5. `docs/evidencia-pytest-y-validacion-local.md`: como reproducir las pruebas desde cero.

## Verdad tecnica del flujo

La fuente principal de estados, tiempos, decisiones y errores debe salir de:

1. `config/arr-orchestrator/orchestrator.db`
2. tabla `job_events`
3. `job_detail()`
4. traza viva `diagnostics/arr/...`
5. ZIP final `diagnosticos_codex/*.zip`

No se deben inventar fuentes paralelas si esos datos ya pueden derivarse de `job_events`.

## Pruebas seguras

Desde la raiz del repo:

```powershell
python -m pip install -r requirements-dev.txt
python -m compileall -q conftest.py tests services/arr-orchestrator services/buscador-puente-arr services/media-panel
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
