# Source Context v1

Contrato canónico para entregar a ARR el título validado de una tarjeta de búsqueda sin acoplar ARR al buscador que la originó.

## Entrada pública

```http
POST http://192.168.1.159:5830/api/source-context/events
Authorization: Bearer <token>
Content-Type: application/json
```

El panel valida tamaño, tipo de contenido y credencial, y reenvía dentro de la red Docker a `POST /internal/source-context/events` del orquestador. El puerto interno `8787` no se publica en la LAN.

## Cuerpo exacto

```json
{
  "schema_version": 1,
  "event_id": "trace_id_del_click",
  "source": "buscador-pro",
  "infohash": "0123456789abcdef0123456789abcdef01234567",
  "destination": "movies",
  "source_title": "Título exacto mostrado en la tarjeta",
  "route": "RD_VERIFIED_MAGNET_NATIVE",
  "delivery_state": "intent",
  "created_at": "2026-07-28T00:00:00Z"
}
```

- No se admiten campos adicionales ni ausentes.
- `infohash` es siempre el SHA1 BTIH hexadecimal completo de 40 caracteres.
- `destination`: `movies` o `tv`; `manual` no publica contexto.
- `delivery_state`: `intent`, `accepted`, `already_present` o `failed`.
- `route` conserva el identificador seguro de la ruta real.
- `created_at` exige ISO 8601 con zona horaria.
- Nunca se envían magnets, URLs, credenciales, rutas ni la consulta libre del usuario.

Respuestas normales: `201` al crear, `200` al actualizar o deduplicar, `400` si el contrato es inválido, `401` si falla la autenticación y `409` ante conflicto.

## Persistencia y correlación

No existe una tabla paralela en ARR. El receptor usa `jobs`, guarda un máximo de tres títulos normalizados en `source_meta_json.source_contexts` y registra las decisiones en `job_events`. La correlación se realiza exclusivamente mediante `infohash`; una coincidencia de ruta solo sirve para recuperar ese hash canónico y nunca sustituye la unión por hash.

Un primer `intent` crea un trabajo `source_submitted` con nombre neutro. Al materializarse qB/RDT, el trabajo adopta el nombre físico real. Si la descarga llega primero, el contexto posterior se adjunta al trabajo activo del mismo hash. Los contextos pendientes caducan a las 24 horas; una ficha tardía queda trazada y nunca reabre un estado terminal.

Las rutas RDT integradas conservan durante 1.440 minutos la fila terminada que relaciona `infohash` y `content_path`, manteniendo su acción final de eliminación. ARR puede así adoptar por ruta exacta el hash en un trabajo físico que haya aparecido antes que la ficha, incluso después de reiniciar. Cuando ARR termina el trabajo, aplica su limpieza normal; si no vuelve a intervenir, RDT retira la fila al vencer esas 24 horas.

## Uso en identidad

El nombre físico siempre se intenta primero. Solo un rechazo recuperable permite ejecutar Parser y Resolver con `source_title`. El respaldo no actúa en categoría manual, conflicto de categoría, identificadores directos contradictorios ni errores técnicos de TMDb.

La política se controla en `resolver.source_title_fallback` desde la pestaña Resolver del panel. Si varios títulos de origen conducen a TMDb distintos, ARR rechaza el respaldo con `source_context_conflict`.

## Configuración

El mismo secreto debe existir como `ARR_SOURCE_CONTEXT_TOKEN` en el `.env` ignorado de ARR y en cada buscador autorizado. Los buscadores apuntan a la entrada pública de `5830`; nunca al puerto interno `8787`.
