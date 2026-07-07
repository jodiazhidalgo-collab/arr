# Como enganchar una web nueva al buscador puente arr

Este documento es para cualquier web futura: Wolfmax, otro tracker, un buscador con caratulas o una portada personalizada.

## Idea simple

La web nueva no debe tener motor de descarga.

La web nueva solo debe:

1. Pintar bonito.
2. Pedir resultados al motor.
3. Mandar al motor el resultado elegido.

El buscador puente arr hace lo delicado.

## No duplicar estos engranajes

No implementar en la web nueva:

- login de RDT-Client
- API directa de Real-Debrid
- qBittorrent
- seleccion de archivos RD
- fallback a qB
- limpieza de RD/RDT
- logica de peliculas/series
- lectura directa de Jackett salvo que sea solo para decorar

Todo eso ya vive en `http://localhost:9003`.

## Flujo recomendado

```text
Usuario pulsa buscar
        |
        v
Web bonita llama a POST /api/search
        |
        v
buscador puente arr busca en Jackett y devuelve resultados normalizados
        |
        v
Web bonita muestra tarjetas, caratulas, botones, filtros
        |
        v
Usuario pulsa una tarjeta
        |
        v
Web bonita llama a POST /api/download
        |
        v
buscador puente arr decide Auto, prueba RD/RDT y pasa a qB si no queda listo a tiempo
```

## Categoria

La decision mas segura:

```json
{
  "category": "auto"
}
```

Usar `auto` cuando:

- la web mezcla peliculas y series
- no esta claro si es peli o serie
- quieres que mande el motor

Usar `movies` cuando:

- la seccion es solo peliculas
- por ejemplo una pagina llamada "Peliculas 4K"

Usar `tv` cuando:

- la seccion es solo series
- por ejemplo una pagina llamada "Series 1080p"

Usar `manual` solo cuando:

- quieres que no se clasifique como movies/tv
- es una descarga especial

## Ejemplo minimo de busqueda desde JavaScript

```js
async function buscar(query) {
  const response = await fetch("http://localhost:9003/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      category: "auto",
      tracker: "all"
    })
  });
  return await response.json();
}
```

## Ejemplo minimo de descarga desde JavaScript

```js
async function descargar(result) {
  const response = await fetch("http://localhost:9003/api/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      result_id: result.id,
      category: "auto"
    })
  });
  return await response.json();
}
```

## Si la web ya trae su propia ficha

Si una web bonita scrapea caratulas o novedades, puede usar su titulo para buscar en el motor.

Ejemplo:

```json
{
  "query": "Return to Silent Hill 2026 4K SPANISH",
  "category": "movies",
  "tracker": "wolfmax4k"
}
```

Luego muestra los resultados y descarga por `result_id`.

## Regla para no meter la pata

Si dudas, manda siempre:

```json
{
  "category": "auto",
  "tracker": "all"
}
```

El motor ya decide.

## Mensaje para otra conversacion

Puedes decirle esto a cualquier otro proyecto:

```text
Lee docs/API.md y docs/COMO_ENGANCHAR_UNA_WEB.md.
No hagas motor de Real-Debrid, RDT ni qBittorrent.
Usa la API del buscador puente arr:
- POST /api/search para buscar
- POST /api/download para enviar
Por defecto manda category auto.
Si la seccion es solo peliculas, manda movies.
Si la seccion es solo series, manda tv.
```
