# Evidencia pytest y validacion local

## Motivo

Este archivo existe para que cualquier revision externa vea claramente que ARR trae base pytest desde la raiz, igual que BTDigg + RD, pero adaptada a que ARR es multi-servicio.

Si una sandbox externa dice que no puede ejecutar pytest por falta de dependencias, eso describe una limitacion de esa sandbox. No significa que el proyecto no tenga pytest preparado.

## Archivos que prueban que pytest esta integrado

- `requirements-dev.txt`
  - Incluye `pytest>=8.4,<9`.
  - Declara una capa local compatible para probar `arr-orchestrator` y `buscador-puente-arr`.
  - No incluye literalmente los requirements de los servicios porque ARR fija dependencias por imagen Docker.
- `pytest.ini`
  - `minversion = 8.4`.
  - `testpaths` apunta a `tests`, `services/arr-orchestrator/tests` y `services/buscador-puente-arr/tests`.
  - `python_files = test_*.py`.
  - `addopts = -ra`.
  - `pythonpath` mete los dos servicios Python en import path.
- `conftest.py`
  - Fija rutas seguras bajo `_codex_runtime/test-data/pytest-session`.
  - Expone fixtures aisladas para ARR y buscador.
  - Evita que las pruebas sinteticas toquen `config/`, `diagnosticos_codex/`, `diagnostics/` o datos reales.
- `tests/conftest.py`
  - Mantiene la simetria visible con BTDigg + RD.
- `tests/test_pytest_contracts.py`
  - Comprueba configuracion pytest raiz.
  - Comprueba dependencias dev.
  - Comprueba traza blackbox de job ARR en runtime aislado.
  - Comprueba runtime aislado del buscador.

## Tests pytest visibles en el repo

- `tests/test_pytest_contracts.py`
- `services/arr-orchestrator/tests/test_arr_blackbox.py`
- `services/arr-orchestrator/tests/test_arr_follow.py`
- `services/arr-orchestrator/tests/test_core.py`
- `services/arr-orchestrator/tests/test_live_engine.py`
- `services/arr-orchestrator/tests/test_live_filebot.py`
- `services/arr-orchestrator/tests/test_live_resolver.py`
- `services/arr-orchestrator/tests/test_name_parser.py`
- `services/arr-orchestrator/tests/test_name_resolver.py`
- `services/buscador-puente-arr/tests/test_app_tracing.py`
- `services/buscador-puente-arr/tests/test_arr_trace.py`

## Comandos para reproducir desde cero

Desde la raiz del proyecto:

```powershell
python -m venv _codex_runtime\tmp\venv_arr_pytest
.\_codex_runtime\tmp\venv_arr_pytest\Scripts\python.exe -m pip install --upgrade pip
.\_codex_runtime\tmp\venv_arr_pytest\Scripts\python.exe -m pip install -r requirements-dev.txt
.\_codex_runtime\tmp\venv_arr_pytest\Scripts\python.exe -m compileall -q conftest.py tests services\arr-orchestrator services\buscador-puente-arr services\media-panel
.\_codex_runtime\tmp\venv_arr_pytest\Scripts\python.exe -m pytest -q
```

La entrada principal es `pytest`, porque `pytest.ini` declara el `pythonpath` de los servicios. Si se quieren ejecutar los `unittest` de cada servicio directamente, hay que declarar el `PYTHONPATH` de ese servicio, ya que `unittest` no lee `pytest.ini`:

```powershell
$env:PYTHONPATH = "services\arr-orchestrator"
.\_codex_runtime\tmp\venv_arr_pytest\Scripts\python.exe -m unittest discover -s services\arr-orchestrator\tests -v

$env:PYTHONPATH = "services\buscador-puente-arr"
.\_codex_runtime\tmp\venv_arr_pytest\Scripts\python.exe -m unittest discover -s services\buscador-puente-arr\tests -v
Remove-Item Env:\PYTHONPATH
```

Los tests live de FileBot, motor completo y TMDb siguen protegidos por variables de entorno (`RUN_FILEBOT_LIVE_TESTS`, `RUN_ENGINE_LIVE_TESTS`, `TMDB_API_TOKEN`) y no se fuerzan en la suite segura.

## Evidencia del entorno real local

Fecha de verificacion local: 2026-07-03.

```text
python --version
Python 3.14.2

python -m pytest --version
pytest 8.4.2

python -m pip show pytest Flask watchdog guessit requests
Name: pytest
Version: 8.4.2
Name: Flask
Version: 3.0.3
Name: watchdog
Version: 6.0.0
Name: guessit
Version: 3.8.0
Name: requests
Version: 2.32.5
```

## Resultado de validacion local

Ejecucion realizada en un venv temporal bajo `_codex_runtime/tmp/`:

```text
python -m compileall -q conftest.py tests services\arr-orchestrator services\buscador-puente-arr services\media-panel
OK

python -m pytest -q
89 passed, 8 skipped in 7.93s

$env:PYTHONPATH = "services\arr-orchestrator"
python -m unittest discover -s services\arr-orchestrator\tests -v
Ran 88 tests in 3.746s
OK (skipped=8)

$env:PYTHONPATH = "services\buscador-puente-arr"
python -m unittest discover -s services\buscador-puente-arr\tests -v
Ran 5 tests in 0.540s
OK
```

## Interpretacion correcta para revisiones externas

- ARR si trae pytest integrado desde la raiz.
- Pytest no se incluye como binario dentro del repo; se declara en `requirements-dev.txt`.
- La suite pytest raiz ejecuta contratos propios de ARR y los tests de los servicios.
- `unittest` directo por servicio tambien funciona, pero necesita `PYTHONPATH` porque no consume la configuracion de `pytest.ini`.
- Los tests sinteticos usan `_codex_runtime/test-data/pytest-session` o `tmp_path`.
- Los tests live quedan saltados salvo que se activen variables explicitas.
- Si una sandbox externa no instala dependencias, su limitacion debe quedar anotada como limitacion de entorno, no como defecto del repo.
