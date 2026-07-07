# Legacy Vivo Del Media Worker

Esta carpeta conserva piezas del motor antiguo, pero ya no ejecuta runners de
carpetas ni polling.

El flujo actual entra por `arr-orchestrator` y llama al worker por HTTP:

- `POST /process-movie`
- `POST /process-trailer`

`media_worker/core.py` usa estos modulos como libreria interna:

- `detector.py`: analiza pistas de video, audio y subtitulos.
- `planificador.py`: decide el plan de limpieza/remux.
- `procesador.py`: ejecuta FFmpeg y genera el archivo limpio.
- `verificador.py`: verifica que el resultado final cumple reglas.
- `rescate_subtitulos.py`: OCR/remux de subtitulos de imagen cuando toca.
- `trailer_runner.py`: helpers de matching y nombre final del trailer.

Lo que se retiro de estos archivos:

- `main()` antiguos.
- reportes HTML/JSON batch.
- bucles sobre `work/entrada`, `work/terminado` o `cuarentena`.
- runner antiguo de trailers basado en inbox propio.

No borrar archivos enteros de esta carpeta sin comprobar primero las llamadas
desde `media_worker/core.py`.
