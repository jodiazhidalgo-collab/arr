from pathlib import Path
from types import SimpleNamespace

import pytest

from media_worker import core
from media_worker.legacy import procesador


def _metadata_plan(source: Path, output: Path):
    return {
        "archivo": source.name,
        "entrada": str(source),
        "salida": str(output),
        "duration": 120.0,
        "processing_mode": "metadata_only",
        "video": {"index": 0, "language_final": "es"},
        "audio": {
            "index": 1,
            "codec": "aac",
            "channels": 2,
            "language_final": "es",
            "audio_accion": "copiar",
        },
        "subtitulo": None,
    }


def test_metadata_only_moves_without_ffmpeg(monkeypatch, tmp_path: Path):
    source = tmp_path / "movie.mkv"
    output = tmp_path / "movie.limpio.mkv"
    source.write_bytes(b"mkv")
    calls = []

    monkeypatch.setattr(procesador, "crear_capitulos_10min", lambda *_args: 0)
    monkeypatch.setattr(
        procesador,
        "run",
        lambda command, **_kwargs: calls.append(command) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        procesador,
        "ejecutar_ffmpeg_con_latido",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("FFmpeg no debe ejecutarse")),
    )

    result = procesador.ejecutar_ffmpeg(_metadata_plan(source, output))

    assert result["ok"] is True
    assert result["processing_mode"] == "metadata_only"
    assert not source.exists()
    assert output.read_bytes() == b"mkv"
    assert len(calls) == 1 and calls[0][0] == "mkvpropedit"


def test_metadata_only_restores_source_after_metadata_failure(monkeypatch, tmp_path: Path):
    source = tmp_path / "movie.mkv"
    output = tmp_path / "movie.limpio.mkv"
    source.write_bytes(b"mkv")
    monkeypatch.setattr(procesador, "crear_capitulos_10min", lambda *_args: 0)
    monkeypatch.setattr(
        procesador,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stderr="fallo", stdout=""
        ),
    )

    result = procesador.ejecutar_ffmpeg(_metadata_plan(source, output))

    assert result["ok"] is False
    assert source.read_bytes() == b"mkv"
    assert not output.exists()


def test_long_text_is_extracted_once_and_never_falls_through_to_ocr(
    monkeypatch, tmp_path: Path
):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"mkv")
    analysis = {
        "duration": 100,
        "stream_counts": {"video": 1, "audio": 1, "subtitle": 1},
        "subtitulos": [
            {"index": 2, "codec": "subrip", "language_final": "es", "frases": None}
        ],
    }
    plan = {"estado": "PLAN NO APTO"}
    selected_srt = tmp_path / "selected.srt"
    selected_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHola\n", encoding="utf-8")
    calls = {"extract": 0, "ocr": 0}

    def extract(*_args):
        calls["extract"] += 1
        return selected_srt, 999

    monkeypatch.setattr(core.rescate_subtitulos, "extraer_texto_srt", extract)
    monkeypatch.setattr(
        core.rescate_subtitulos,
        "ejecutar_seconv",
        lambda *_args: calls.__setitem__("ocr", calls["ocr"] + 1),
    )
    monkeypatch.setattr(
        core,
        "_build_one_pass_no_subtitles_plan",
        lambda *_args: (analysis, {"estado": "PLAN APTO", "processing_mode": "single_remux"}),
    )
    monkeypatch.setattr(core, "valor", lambda path, default=None: {
        "subtitulos.formatos_texto_aceptados": ["subrip"],
        "subtitulos.formatos_imagen_no_aceptados": ["hdmv_pgs_subtitle"],
        "subtitulos.frases_descartar_hasta": 1,
        "subtitulos.frases_maximo_unico_forzado": 150,
        "subtitulos.sin_subtitulos_modo": "procesar_sin_subtitulos",
        "subtitulos.unico_es_modo": "aplicar_limite",
        "subtitulos.ocr_imagen_modo": "solo_forzados_cortos",
    }.get(path, default))

    _analysis, prepared, result = core._prepare_subtitle_plan(
        analysis, plan, video, "job", tmp_path
    )

    assert calls == {"extract": 1, "ocr": 0}
    assert prepared["estado"] == "PLAN APTO"
    assert result["mode"] == "texto_largo_sin_ocr"


def test_short_forced_image_runs_one_ocr_and_selects_single_remux(
    monkeypatch, tmp_path: Path
):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"mkv")
    selected_srt = tmp_path / "ocr.srt"
    selected_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHola\n", encoding="utf-8")
    analysis = {
        "duration": 100,
        "stream_counts": {"video": 1, "audio": 1, "subtitle": 1},
        "subtitulos": [
            {
                "index": 2,
                "codec": "hdmv_pgs_subtitle",
                "language_final": "es",
                "eventos": 20,
                "forced": True,
            }
        ],
    }
    calls = {"ocr": 0}
    monkeypatch.setattr(core, "valor", lambda path, default=None: {
        "subtitulos.formatos_texto_aceptados": ["subrip"],
        "subtitulos.formatos_imagen_no_aceptados": ["hdmv_pgs_subtitle"],
        "subtitulos.frases_descartar_hasta": 1,
        "subtitulos.frases_maximo_unico_forzado": 150,
        "subtitulos.sin_subtitulos_modo": "procesar_sin_subtitulos",
        "subtitulos.unico_es_modo": "aplicar_limite",
        "subtitulos.ocr_imagen_modo": "solo_forzados_cortos",
    }.get(path, default))
    monkeypatch.setattr(
        core.rescate_subtitulos,
        "ejecutar_seconv",
        lambda *_args: (
            calls.__setitem__("ocr", calls["ocr"] + 1) or selected_srt,
            20,
            "test",
        ),
    )
    monkeypatch.setattr(
        core.planificador,
        "crear_plan",
        lambda _analysis: {
            "estado": "PLAN APTO",
            "video": {"index": 0},
            "audio": {"index": 1},
            "subtitulo": {"index": 2},
        },
    )

    _analysis, prepared, result = core._prepare_subtitle_plan(
        analysis, {"estado": "PLAN NO APTO"}, video, "job", tmp_path
    )

    assert calls["ocr"] == 1
    assert prepared["processing_mode"] == "ocr_single_remux"
    assert result["mode"] == "ocr"


@pytest.mark.parametrize("events", [None, 999])
def test_unknown_or_long_image_never_runs_ocr(monkeypatch, tmp_path: Path, events):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"mkv")
    analysis = {
        "duration": 100,
        "stream_counts": {"video": 1, "audio": 1, "subtitle": 1},
        "subtitulos": [
            {
                "index": 2,
                "codec": "hdmv_pgs_subtitle",
                "language_final": "es",
                "eventos": events,
                "forced": True,
            }
        ],
    }
    monkeypatch.setattr(core, "valor", lambda path, default=None: {
        "subtitulos.formatos_texto_aceptados": ["subrip"],
        "subtitulos.formatos_imagen_no_aceptados": ["hdmv_pgs_subtitle"],
        "subtitulos.frases_descartar_hasta": 1,
        "subtitulos.frases_maximo_unico_forzado": 150,
        "subtitulos.sin_subtitulos_modo": "procesar_sin_subtitulos",
        "subtitulos.unico_es_modo": "aplicar_limite",
        "subtitulos.ocr_imagen_modo": "solo_forzados_cortos",
    }.get(path, default))
    monkeypatch.setattr(
        core.rescate_subtitulos,
        "eventos_subtitulo_pista",
        lambda *_args: events,
    )
    monkeypatch.setattr(
        core.rescate_subtitulos,
        "ejecutar_seconv",
        lambda *_args: (_ for _ in ()).throw(AssertionError("OCR injustificado")),
    )
    monkeypatch.setattr(
        core,
        "_build_one_pass_no_subtitles_plan",
        lambda *_args: (analysis, {"estado": "PLAN APTO", "processing_mode": "single_remux"}),
    )

    _analysis, prepared, result = core._prepare_subtitle_plan(
        analysis, {"estado": "PLAN NO APTO"}, video, "job", tmp_path
    )

    assert prepared["processing_mode"] == "single_remux"
    assert result["mode"] == "imagen_no_plausible_sin_ocr"
