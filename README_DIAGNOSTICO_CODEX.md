# Diagnostico Codex ARR

Este archivo es solo un puente rapido. Las normas operativas locales pueden vivir en `AGENTS.md`, fuera del Git publico si contienen infraestructura privada.

## Orden de verdad

1. `config/arr-orchestrator/orchestrator.db`
2. tabla `job_events`
3. `job_detail()`
4. traza viva `diagnostics/arr/...`
5. ZIP final `diagnosticos_codex/*.zip`

No uses el ZIP como unica fuente si existe traza viva o `job_events`.

## Panel

```text
ARR_PANEL_URL
```

En `Motor` o `Historial`, usa `Informe Codex` para generar o descargar el ZIP del job.

## Traza viva

Ruta del host:

```text
${ARR_ROOT}/diagnostics/arr
```

Rutas principales:

```text
jobs/YYYY-MM-DD/<job_id>/
search/YYYY-MM-DD/<trace_id>/
download/YYYY-MM-DD/<trace_id>/
monitor/YYYY-MM-DD/<trace_id>/
```

Artefactos esperados:

```text
meta.json
summary.json
events.jsonl
warnings.jsonl
errors.jsonl
timeline.md
human_follow.json
related_files.json
```

`meta.json` y `summary.json` incluyen `config_snapshot`: una foto pequena de la configuracion operativa del job o traza. No guarda archivos enteros; solo valores utiles de entorno, rutas con alias y banderas de credenciales presentes.

## Informe ZIP

Rutas:

```text
Windows: <ARR_ROOT_WIN>\diagnosticos_codex
Host:    ${ARR_ROOT}/diagnosticos_codex
Docker:  /diagnosticos_codex
```

Subcarpetas:

```text
movies
tv
trailers
repetidas_vs_error
```

Contenido minimo:

```text
LEEME_PRIMERO.txt
resumen.txt
job.json
timeline.json
timings.json
decisiones.json
errores.txt
logs_filtrados.txt
health_contenedores.json
rutas.txt
detalle_completo.json
archivos_relacionados/
```

Si existe traza viva del job, el ZIP tambien puede incluir:

```text
traza_viva/
```

## Orden recomendado para una IA o Codex

1. Leer `LEEME_PRIMERO.txt`.
2. Revisar `timeline.json`, `decisiones.json` y `errores.txt`.
3. Mirar `traza_viva/` si aparece dentro del ZIP.
4. Si no hay ZIP, revisar `diagnostics/arr/...`.
5. Si hace falta mas detalle, consultar `orchestrator.db` y `job_events`.
6. Solo despues mirar logs sueltos o codigo.

## Regla importante

El diagnostico no cambia el flujo de peliculas, series ni trailers. Solo registra y empaqueta datos para entender que paso.

## Seguridad de exportacion

Los informes humanos y ZIPs deben salir saneados:

- sin tokens, passwords, auth, magnets ni `download_url`
- sin rutas absolutas completas
- con aliases como `<DATA_DOWNLOADS>`, `<DATA_MEDIA>`, `<CONFIG>`, `<DIAGNOSTICS>`, `<CODEX_DIAGS>`, `<ARR_ROOT>`, `<ARR_ROOT_WIN>`, `<APP_DATA>` y `<APP_LOGS>`
- con limites de strings/listas
- con `related_files.json` limitado a rutas utiles
- con `config_snapshot` limitado a configuracion operativa pequena
