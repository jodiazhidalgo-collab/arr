# Ejemplos rapidos del buscador puente arr

## Buscar una pelicula en todos los trackers

```bash
curl -X POST http://localhost:9003/api/search \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"matrix 1999 4k espanol\",\"category\":\"auto\",\"tracker\":\"all\"}"
```

## Buscar solo en Wolfmax

```bash
curl -X POST http://localhost:9003/api/search \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"return to silent hill 2026 spanish\",\"category\":\"movies\",\"tracker\":\"wolfmax4k\"}"
```

## Buscar en varios trackers

```bash
curl -X POST http://localhost:9003/api/search \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"cenicienta 2015 1080p\",\"category\":\"auto\",\"trackers\":[\"wolfmax4k\",\"dontorrent\",\"elitetorrent-wf\"]}"
```

## Descargar por result_id

```bash
curl -X POST http://localhost:9003/api/download \
  -H "Content-Type: application/json" \
  -d "{\"result_id\":\"PEGAR_ID_AQUI\",\"category\":\"auto\"}"
```

## Descargar mandando el resultado completo

```bash
curl -X POST http://localhost:9003/api/download \
  -H "Content-Type: application/json" \
  -d "{\"category\":\"auto\",\"result\":{\"title\":\"Matrix (1999) [4K][Esp]\",\"download_url\":\"PEGAR_URL_AQUI\",\"tracker\":\"Wolfmax 4k\",\"tracker_id\":\"wolfmax4k\"}}"
```

## Probar si Auto detecta serie

```bash
curl -X POST http://localhost:9003/api/classify \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"Berlin S01E06 WEBRip 2160p SPANISH\"}"
```

Resultado esperado:

```json
{
  "ok": true,
  "category": "tv",
  "title": "Berlin S01E06 WEBRip 2160p SPANISH"
}
```

## Probar si Auto detecta pelicula

```bash
curl -X POST http://localhost:9003/api/classify \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"Matrix 1999 BluRay 2160p SPANISH\"}"
```

Resultado esperado:

```json
{
  "ok": true,
  "category": "movies",
  "title": "Matrix 1999 BluRay 2160p SPANISH"
}
```
