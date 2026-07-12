# API buscador puente arr

Esta API es el buscador puente central. Cualquier web bonita futura debe usar esto y no tocar RDT-Client ni qBittorrent directamente.

Base en el NAS:

```text
http://localhost:9003
```

## Regla principal

La web externa solo hace dos cosas:

1. Buscar con `/api/search`.
2. Enviar el resultado elegido con `/api/download`.

Esos dos endpoints sincronicos se mantienen por compatibilidad con webs externas como Wolfmax.

Las búsquedas del historial conservan su origen. Las iniciadas por Wolfmax se identifican como `wolfmax` y las realizadas desde Puente ARR como `bridge`.
La UI incluida en este proyecto usa los endpoints asincronos `/api/jobs/search` y `/api/jobs/download`
para no bloquear la pantalla mientras busca o envia.

El buscador puente arr se encarga de:

- consultar Jackett
- normalizar resultados
- clasificar `auto` como `movies`, `tv` o `manual`
- mandar a RDT-Client, que gestiona Real-Debrid
- esperar unos segundos a que RD/RDT quede listo
- pasar a qBittorrent si RD/RDT falla o no queda listo a tiempo
- limpiar RDT cuando toque

## Categorias

Valores admitidos:

```text
auto
movies
tv
manual
```

Uso recomendado:

- Si la web es mixta: mandar siempre `auto`.
- Si la pantalla es solo peliculas: mandar `movies`.
- Si la pantalla es solo series: mandar `tv`.
- Si la web no esta segura: mandar `auto`.

Con `auto`, el motor decide mirando el titulo:

- `S01E06`, `1x06`, `Temporada`, `Capitulo`, `Episode` -> `tv`
- anos tipo `1999`, `2024` y pistas como `1080p`, `BluRay`, `HDRip`, `WEB-DL` -> `movies`
- si no lo ve claro -> usa el valor configurado en Ajustes > qBittorrent > Si Auto duda

## Buscar

### GET /api/search

Compatible con la web principal actual.

```http
GET /api/search?q=matrix%201999%204k&indexers=wolfmax4k&category=auto
```

Parametros:

- `q`: texto de busqueda.
- `indexer` o `indexers`: uno o varios trackers de Jackett separados por coma.
- `category`: `auto`, `movies`, `tv` o `manual`.

Si no se manda `indexer/indexers`, busca en todos los trackers configurados.

### POST /api/search

Recomendado para webs nuevas.

```json
{
  "query": "matrix 1999 4k espanol",
  "category": "auto",
  "tracker": "wolfmax4k"
}
```

Buscar en todos:

```json
{
  "query": "matrix 1999 4k espanol",
  "category": "auto",
  "tracker": "all"
}
```

Buscar en varios:

```json
{
  "query": "matrix 1999 4k espanol",
  "category": "auto",
  "trackers": ["wolfmax4k", "mejortorrent", "dontorrent"]
}
```

Respuesta:

```json
{
  "ok": true,
  "query": "matrix 1999 4k espanol",
  "category": "auto",
  "indexers": ["wolfmax4k"],
  "count": 2,
  "results": [
    {
      "id": "4f9fb7a1c0bb4a4b76d2b2a1",
      "title": "Matrix (1999) [4K][Esp]",
      "tracker": "Wolfmax 4k",
      "tracker_id": "wolfmax4k",
      "size": "66571993088",
      "size_text": "62.00 GB",
      "seeders": 12,
      "peers": 4,
      "leechers": 4,
      "type": "torrent",
      "download_url": "http://...",
      "magnet": null,
      "is_magnet": false
    }
  ]
}
```

El campo importante para descargar despues es `id`.

## Descargar

### POST /api/download

Forma recomendada: mandar el `id` recibido en `/api/search`.

```json
{
  "result_id": "4f9fb7a1c0bb4a4b76d2b2a1",
  "category": "auto"
}
```

Tambien se puede mandar el resultado completo:

```json
{
  "category": "auto",
  "result": {
    "title": "Matrix (1999) [4K][Esp]",
    "download_url": "http://...",
    "tracker": "Wolfmax 4k",
    "tracker_id": "wolfmax4k"
  }
}
```

O lo minimo:

```json
{
  "title": "Matrix (1999) [4K][Esp]",
  "download_url": "http://...",
  "category": "auto"
}
```

Respuesta correcta:

```json
{
  "ok": true,
  "title": "Matrix (1999) [4K][Esp]",
  "category": "movies",
  "requested_category": "auto",
  "engine": "RDT-Client",
  "source_result_id": "4f9fb7a1c0bb4a4b76d2b2a1",
  "source_tracker": "Wolfmax 4k",
  "source_tracker_id": "wolfmax4k"
}
```

Si RDT falla y el fallback esta activo, puede responder:

```json
{
  "ok": true,
  "title": "Matrix (1999) [4K][Esp]",
  "category": "movies",
  "requested_category": "auto",
  "engine": "qBittorrent",
  "fallback_from": "RDT sin progreso..."
}
```

## Estados y errores

Estados utiles:

- `ok: true`: aceptado por el motor.
- `engine: RDT-Client`: ha entrado por RDT-Client.
- `engine: qBittorrent`: ha ido por fallback o descarga torrent normal.
- `fallback_from`: explica por que salto a qB.

Errores posibles:

```json
{
  "ok": false,
  "error": "busqueda vacia"
}
```

```json
{
  "ok": false,
  "error": "falta result_id o download_url"
}
```

```json
{
  "ok": false,
  "error": "No he podido descargar el .torrent..."
}
```

## Cache de resultados

Cuando `/api/search` devuelve resultados, el motor guarda los `id` durante unas horas para que `/api/download` pueda recibir solo `result_id`.

Si una web tarda demasiado o el motor se reinicia, puede mandar el resultado completo en vez de solo `result_id`.

## Historial

`GET /api/history/searches` devuelve las búsquedas agrupadas por día e incluye el campo `source`.

`GET /api/history/searches/<search_id>/results?page=1` devuelve resultados paginados. Los magnets se entregan como `copy_value`; los enlaces internos de Jackett se sustituyen por una URL segura de Puente ARR, sin exponer el host Docker ni la clave de Jackett.

`GET /api/history/results/<result_id>/torrent` recupera y entrega el `.torrent` correspondiente mientras la entrada siga conservada en el historial.

`POST /api/history/results/<result_id>/magnet` convierte bajo demanda un torrent público en un magnet mínimo (`btih` y nombre). La conversión se ejecuta solo al pulsar copiar, realiza una única recuperación del torrent y guarda el resultado en la misma fila del historial. Los torrents privados no se convierten y conservan como alternativa su URL segura `.torrent`.

## Jobs asincronos

Estos endpoints son los recomendados para interfaces interactivas:

```text
POST /api/jobs/search
GET  /api/jobs/search/{job_id}
POST /api/jobs/download
GET  /api/jobs/download/{job_id}
```

Estados posibles:

```text
queued
running
done
error
interrupted
```

La huella de descarga incluye la categoria pedida. Por eso un mismo `result_id` enviado como
`movies`, `tv` o `manual` no reutiliza por error un job anterior de otra categoria.

## Libreta anti-duplicados

El motor guarda los envios en `data/submissions.sqlite3`.

Si llega el mismo resultado con la misma categoria poco despues, responde `ok` pero con:

```json
{
  "duplicate_guard": true,
  "message": "Ya estaba enviado o vigilado; no lo repito."
}
```

Eso evita repetir envios por doble clic, reconexion de una web externa o reinicio raro.

## Endpoints auxiliares

### GET /api/indexers

Devuelve trackers configurados en Jackett:

```json
{
  "ok": true,
  "indexers": [
    {
      "id": "wolfmax4k",
      "title": "Wolfmax 4k"
    }
  ]
}
```

### GET /api/engine-status

Endpoint de lectura para paneles externos de seguimiento. Devuelve el estado corto del monitor RDT del puente.
No usarlo para enviar descargas.

### POST /api/classify

Prueba la clasificacion Auto:

```json
{
  "title": "Berlin S01E06 WEBRip 2160p SPANISH"
}
```

Respuesta:

```json
{
  "ok": true,
  "category": "tv",
  "title": "Berlin S01E06 WEBRip 2160p SPANISH"
}
```
