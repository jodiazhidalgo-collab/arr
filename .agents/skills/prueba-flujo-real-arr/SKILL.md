---
name: prueba-flujo-real-arr
description: Ejecutar una prueba real completa del flujo ARR cuando el usuario pida pruebas reales, flujo real, mirar trazas/diagnosticos/ruido, o validar pelicula y serie en el motor vivo. Usa el contenedor real arr-orchestrator, activa pruebas live internas, crea probes controlados en movies y en la entrada normal complete/tv, revisa job_events, diagnostics/arr, ZIP Codex y limpia lo identificable.
---

# Prueba Flujo Real ARR

## Uso

Usar esta skill como boton unico cuando el usuario pida prueba real de ARR, flujo real, validacion profunda, revisar ruido en diagnosticos, trazas o Informe Codex.

## Que hace

1. Ejecuta pruebas live internas dentro de `arr-orchestrator`, incluido FileBot TV real hacia `series_filebot_output`.
2. Crea una pelicula probe en `/data/downloads/torrents/complete/movies`.
3. Crea un episodio MKV real en `/data/downloads/torrents/complete/tv/codex_live_flow_probe_*`; el vigilante debe descubrirlo y crear por si solo el `job_id` y su snapshot canary.
4. Exige el recorrido completo `complete/tv -> taller/<job_id> -> FileBot -> series_filebot_output -> series-worker -> TV -> limpieza`, sin inyectar filas ni saltarse estados.
5. Revisa `job_events`, `diagnostics/arr`, `summary.json`, `meta.json`, `related_files.json`, `config_snapshot` y ZIP Codex.
6. Limpia origen, taller, entrega TV e informes durables de los probes cuando su terminalidad esta confirmada.

## Comando

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .agents\skills\prueba-flujo-real-arr\scripts\run_real_flow_probe.ps1
```

## Reglas

- Es la skill principal para pruebas reales. No pedir permiso extra si el usuario ya pidio flujo real.
- No tocar peliculas del usuario: solo crear probes con nombre `codex_live_flow_probe_*`.
- Si falta traza viva, ZIP Codex, `config_snapshot` o hay listas/rutas ruidosas, la prueba falla.
- Si las pruebas live internas tienen `skipped`, la prueba falla.
- Dejar evidencia local en `_codex_runtime/artifacts/real-flow/`.
