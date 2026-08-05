import json
import re
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import bluray
from .legacy import detector, planificador, procesador, rescate_subtitulos, trailer_runner, verificador
from .legacy.reglas import valor


def _safe_folder_name(value: str) -> str:
    text = re.sub(r"[\\/]+", " ", value or "").strip()
    text = re.sub(r"[\x00-\x1f]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:180] or "item"


def _numbered_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10000):
        candidate = path.with_name(f"{path.name} ({index})")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.name} ({int(time.time())})")


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _video_files(folder: Path) -> List[Path]:
    video_extensions = set(valor("entrada.extensiones_video", []) or [])
    if folder.is_file() and folder.suffix.lower() in video_extensions:
        return [folder]
    return sorted(
        p for p in folder.rglob("*")
        if p.is_file()
        and p.suffix.lower() in video_extensions
        and not p.name.endswith(".procesando.tmp.mkv")
        and ".limpio" not in p.stem
    )


def _move_to_review(
    source: Path,
    review_root: Path,
    job_id: str,
    reason_file: str,
    lines: List[str],
    payload: Dict[str, object],
) -> Dict[str, object]:
    review_root.mkdir(parents=True, exist_ok=True)
    name = source.name if source.exists() else payload.get("name") or "item"
    destination = _numbered_path(review_root / _safe_folder_name(str(name)))
    if source.exists():
        shutil.move(str(source), str(destination))
    else:
        destination.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "review_path": str(destination), "reason_file": reason_file}
    _write_json(destination / "reason.json", payload)
    reason_text = [reason_file.removesuffix(".txt")]
    reason_text.extend(str(line) for line in lines if str(line).strip())
    (destination / reason_file).write_text("\n".join(reason_text).strip() + "\n", encoding="utf-8")
    return {
        "status": "review",
        "review_path": str(destination),
        "reason_file": reason_file,
        "reason": reason_text,
    }


def _reports(job_id: str, reports_root: Path) -> Path:
    path = reports_root / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_bluray(payload: Dict[str, object]) -> Dict[str, object]:
    job_id = str(payload["job_id"])
    source = Path(str(payload["source_path"]))
    reports_root = Path(str(payload.get("reports_root") or "/logs/media-worker"))
    callback_url = str(payload.get("callback_url") or "")
    reports_dir = _reports(job_id, reports_root)

    def record(
        event_name: str,
        event_type: str,
        message: str,
        structured: Dict[str, object],
    ) -> None:
        _emit_event(
            callback_url,
            "bluray",
            event_type,
            message,
            {"job_id": job_id, "event_name": event_name, **structured},
        )

    try:
        result = bluray.normalize_bluray_folder(source, event_callback=record)
    except Exception as error:
        result = {
            "status": "unexpected_error",
            "normalized": False,
            "source_removed": False,
            "reason": str(error),
        }
        record(
            "bluray_normalization_failed",
            "error",
            "Error inesperado normalizando Blu-ray",
            result,
        )
    _write_json(reports_dir / "bluray_result.json", result)
    return {**result, "job_id": job_id, "reports_dir": str(reports_dir)}


def _emit_event(
    callback_url: str,
    phase: str,
    event_type: str,
    message: str,
    structured: Optional[Dict[str, object]] = None,
) -> None:
    if not callback_url:
        return
    payload = json.dumps(
        {
            "phase": phase,
            "event_type": event_type,
            "message": message,
            "structured": structured or {},
        },
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    request = urllib.request.Request(
        callback_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2):
            pass
    except Exception:
        pass


def _bind_plan_to_video(plan: Dict[str, object], video: Path) -> Dict[str, object]:
    output = video.with_name(f"{video.stem}.limpio.mkv")
    if output.exists():
        output = output.with_name(f"{output.stem}.{int(time.time())}{output.suffix}")
    plan["entrada"] = str(video)
    plan["salida"] = str(output)
    return plan


def _plan_with_external_subtitle(
    analysis: Dict[str, object],
    video: Path,
    subtitle: Dict[str, object],
    srt: Path,
    *,
    mode: str,
    cues: int,
) -> Tuple[Dict[str, object], Dict[str, object]]:
    selected = dict(subtitle)
    selected.update(
        {
            "decision": "CANDIDATO FORZADO REAL",
            "frases": cues,
            "prioridad": 200000 - cues,
        }
    )
    adjusted = dict(analysis)
    adjusted["estado"] = "APTO PARA PROCESO AUTOMATICO"
    adjusted["subtitulos"] = [selected]
    plan = _bind_plan_to_video(planificador.crear_plan(adjusted), video)
    if plan.get("estado") != "PLAN APTO":
        raise RuntimeError("No se pudo construir el plan final con el SRT elegido.")
    plan["subtitulo_externo"] = str(srt)
    plan["subtitulo_origen_interno"] = mode == "internal_text"
    plan["processing_mode"] = "ocr_single_remux" if mode == "ocr" else "single_remux"
    plan["duration"] = float(analysis.get("duration") or 0)
    plan["stream_counts"] = dict(analysis.get("stream_counts") or {})
    return adjusted, plan


def _prepare_subtitle_plan(
    analysis: Dict[str, object],
    plan: Dict[str, object],
    video: Path,
    job_id: str,
    reports_root: Path,
) -> Tuple[Dict[str, object], Dict[str, object], Optional[Dict[str, object]]]:
    tmp_dir = _reports(job_id, reports_root) / "rescue_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    subtitle_rules = {
        "text": set(valor("subtitulos.formatos_texto_aceptados", []) or []),
        "image": set(valor("subtitulos.formatos_imagen_no_aceptados", []) or []),
        "minimum": int(valor("subtitulos.frases_descartar_hasta", 1) or 1),
        "maximum": int(valor("subtitulos.frases_maximo_unico_forzado", 150) or 150),
        "delay_maximum": int(valor("subtitulos.delay_audio.frases_maximo", 150) or 150),
        "without": str(valor("subtitulos.sin_subtitulos_modo", "cuarentena") or "").strip().lower(),
        "unique": str(valor("subtitulos.unico_es_modo", "aplicar_limite") or "").strip().lower(),
        "ocr": str(valor("subtitulos.ocr_imagen_modo", "solo_forzados_cortos") or "").strip().lower(),
    }
    spanish = [
        dict(item) for item in analysis.get("subtitulos", [])
        if isinstance(item, dict) and item.get("language_final") == "es"
    ]
    text_tracks = [item for item in spanish if str(item.get("codec") or "").lower() in subtitle_rules["text"]]
    image_tracks = [item for item in spanish if str(item.get("codec") or "").lower() in subtitle_rules["image"]]

    if text_tracks:
        selected = sorted(
            text_tracks,
            key=lambda item: (
                0 if item.get("delay_audio_aceptado") else 1,
                0 if item.get("forced") or item.get("nombre_forzado") else 1,
                int(item.get("frases") or subtitle_rules["maximum"]),
                int(item.get("index") or 0),
            ),
        )[0]
        srt, cues = rescate_subtitulos.extraer_texto_srt(video, selected, tmp_dir)
        selected_maximum = (
            subtitle_rules["delay_maximum"]
            if selected.get("delay_audio_aceptado")
            else subtitle_rules["maximum"]
        )
        accepted = cues > subtitle_rules["minimum"] and (
            cues <= selected_maximum
            or (
                not selected.get("delay_audio_aceptado")
                and subtitle_rules["unique"] == "aceptar_siempre"
            )
        )
        if accepted:
            adjusted, prepared = _plan_with_external_subtitle(
                analysis, video, selected, srt, mode="internal_text", cues=cues
            )
            return adjusted, prepared, {
                "status": "prepared",
                "mode": "texto_extraido_una_vez",
                "cues": cues,
            }
        if subtitle_rules["without"] == "procesar_sin_subtitulos":
            discarded = [selected]
            one_pass = _build_one_pass_no_subtitles_plan(video, analysis, discarded)
            if one_pass:
                adjusted, prepared = one_pass
                return adjusted, prepared, {
                    "status": "prepared",
                    "mode": "texto_largo_sin_ocr",
                    "cues": cues,
                }
        return analysis, plan, None

    if not image_tracks:
        return analysis, plan, None
    if subtitle_rules["ocr"] == "desactivado":
        selected = image_tracks[0]
        one_pass = _build_one_pass_no_subtitles_plan(video, analysis, image_tracks)
        if one_pass:
            adjusted, prepared = one_pass
            return adjusted, prepared, {"status": "prepared", "mode": "ocr_desactivado"}
        return analysis, plan, None

    selected = sorted(
        image_tracks,
        key=lambda item: (
            0 if item.get("forced") or item.get("nombre_forzado") else 1,
            int(item.get("eventos") or 999999999),
            int(item.get("index") or 0),
        ),
    )[0]
    events = _int_or_none(selected.get("eventos"))
    if events is None:
        events = rescate_subtitulos.eventos_subtitulo_pista(video, int(selected["index"]))
        selected["eventos"] = events
    plausible = (
        events is not None
        and subtitle_rules["minimum"] < events <= subtitle_rules["maximum"]
        and (
            bool(selected.get("forced") or selected.get("nombre_forzado"))
            or len(image_tracks) == 1
        )
    )
    if not plausible:
        one_pass = _build_one_pass_no_subtitles_plan(video, analysis, image_tracks)
        if one_pass:
            adjusted, prepared = one_pass
            return adjusted, prepared, {
                "status": "prepared",
                "mode": "imagen_no_plausible_sin_ocr",
                "events": events,
            }
        return analysis, plan, None

    srt, cues, method = rescate_subtitulos.ejecutar_seconv(video, selected, tmp_dir)
    if not (subtitle_rules["minimum"] < cues <= subtitle_rules["maximum"]):
        one_pass = _build_one_pass_no_subtitles_plan(video, analysis, image_tracks)
        if one_pass:
            adjusted, prepared = one_pass
            return adjusted, prepared, {
                "status": "prepared",
                "mode": "ocr_fuera_limite_sin_subtitulos",
                "cues": cues,
            }
        return analysis, plan, None
    adjusted, prepared = _plan_with_external_subtitle(
        analysis, video, selected, srt, mode="ocr", cues=cues
    )
    return adjusted, prepared, {
        "status": "prepared",
        "mode": "ocr",
        "method": method,
        "cues": cues,
    }


def _build_plan(video: Path) -> Tuple[Dict[str, object], Dict[str, object]]:
    analysis = detector.analizar_archivo(video)
    plan = planificador.crear_plan(analysis)
    _bind_plan_to_video(plan, video)
    plan["duration"] = float(analysis.get("duration") or 0)
    plan["stream_counts"] = dict(analysis.get("stream_counts") or {})
    return analysis, plan


def _int_or_none(value: object) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _build_one_pass_no_subtitles_plan(
    video: Path,
    analysis: Dict[str, object],
    discarded_subtitles: List[Dict[str, object]],
) -> Optional[Tuple[Dict[str, object], Dict[str, object]]]:
    adjusted_analysis = dict(analysis)
    adjusted_analysis["estado"] = "APTO PARA PROCESO AUTOMATICO"
    discarded_indexes = {
        _int_or_none(subtitle.get("index"))
        for subtitle in discarded_subtitles
        if _int_or_none(subtitle.get("index")) is not None
    }
    adjusted_analysis["subtitulos"] = [
        subtitle for subtitle in analysis.get("subtitulos", [])
        if not isinstance(subtitle, dict) or _int_or_none(subtitle.get("index")) not in discarded_indexes
    ]
    adjusted_analysis["subtitulos_descartados_una_pasada"] = discarded_subtitles

    plan = planificador.crear_plan(adjusted_analysis)
    if plan.get("estado") != "PLAN APTO":
        return None

    _bind_plan_to_video(plan, video)
    plan["duration"] = float(analysis.get("duration") or 0)
    plan["stream_counts"] = dict(analysis.get("stream_counts") or {})
    plan["rescate_una_pasada"] = True
    plan["subtitulos_descartados"] = discarded_subtitles
    return adjusted_analysis, plan


def _apply_movie_processing_mode(
    analysis: Dict[str, object],
    plan: Dict[str, object],
    video: Path,
) -> None:
    counts = dict(analysis.get("stream_counts") or {})
    audio = plan.get("audio") if isinstance(plan.get("audio"), dict) else {}
    subtitle = plan.get("subtitulo") if isinstance(plan.get("subtitulo"), dict) else None
    audio_action = str(audio.get("audio_accion") or "")
    metadata_only = (
        video.suffix.lower() == ".mkv"
        and int(counts.get("video") or 0) == 1
        and int(counts.get("audio") or 0) == 1
        and int(counts.get("attachment") or 0) == 0
        and int(counts.get("data") or 0) == 0
        and audio_action != "convertir_ac3_5_1"
        and (
            (subtitle is None and int(counts.get("subtitle") or 0) == 0)
            or (
                subtitle is not None
                and int(counts.get("subtitle") or 0) == 1
                and str(subtitle.get("codec") or "").lower() in {"subrip", "srt"}
                and bool(plan.get("subtitulo_origen_interno"))
            )
        )
    )
    if metadata_only:
        plan["processing_mode"] = "metadata_only"
    elif str(plan.get("processing_mode") or "") != "ocr_single_remux":
        plan["processing_mode"] = "single_remux"


def _finalize_movie(
    folder: Path,
    original_video: Path,
    clean_video: Path,
    final_root: Path,
) -> Dict[str, object]:
    final_dir = final_root / folder.name
    if final_dir.exists():
        raise FileExistsError(f"Ya existe destino final: {final_dir}")
    final_dir.mkdir(parents=True, exist_ok=False)
    final_video = final_dir / original_video.name
    shutil.move(str(clean_video), str(final_video))

    suffix = str(valor("subtitulos.sufijo_srt_externo", ".es.forced.srt"))
    clean_srt = clean_video.with_name(f"{clean_video.stem.replace('.limpio', '')}{suffix}")
    final_srt = final_video.with_name(f"{final_video.stem}{suffix}")
    if clean_srt.exists():
        shutil.move(str(clean_srt), str(final_srt))

    shutil.rmtree(folder, ignore_errors=True)
    return {
        "final_dir": str(final_dir),
        "final_video": str(final_video),
        "final_srt": str(final_srt) if final_srt.exists() else "",
    }


def _review_if_final_exists(
    source: Path,
    final_root: Path,
    review_root: Path,
    job_id: str,
    phase: str,
) -> Optional[Dict[str, object]]:
    final_dir = final_root / source.name
    if not final_dir.exists():
        return None
    return _move_to_review(
        source,
        review_root,
        job_id,
        "Pelicula repetida.txt",
        [
            f"Ya existe destino final: {final_dir}",
            "Se corta antes de crear .limpio.mkv para evitar escritura innecesaria.",
        ],
        {
            "job_id": job_id,
            "phase": phase,
            "source": str(source),
            "final_dir": str(final_dir),
            "reason": "destination_exists_before_processing",
        },
    )


def process_movie(payload: Dict[str, object]) -> Dict[str, object]:
    job_id = str(payload["job_id"])
    source = Path(str(payload["source_path"]))
    final_root = Path(str(payload["final_root"]))
    review_root = Path(str(payload["review_root"]))
    reports_root = Path(str(payload.get("reports_root") or "/logs/media-worker"))
    callback_url = str(payload.get("callback_url") or "")
    reports_dir = _reports(job_id, reports_root)
    _emit_event(
        callback_url,
        "media",
        "started",
        "Media Worker recibido",
        {"source": str(source), "reports_dir": str(reports_dir)},
    )

    if not source.exists():
        _emit_event(
            callback_url,
            "media",
            "error",
            "No existe la carpeta de media",
            {"source": str(source)},
        )
        raise FileNotFoundError(f"No existe la carpeta de media: {source}")
    if not source.is_dir():
        _emit_event(
            callback_url,
            "media",
            "error",
            "La entrada de media no es una carpeta",
            {"source": str(source)},
        )
        return _move_to_review(
            source,
            review_root,
            job_id,
            "Error de proceso.txt",
            ["La entrada de media no es una carpeta."],
            {"job_id": job_id, "phase": "media_core", "source": str(source)},
        )

    bluray_result = (
        normalize_bluray(payload)
        if bluray.find_full_bluray_folders(source)
        else {"status": "not_bluray", "normalized": False, "source_removed": False}
    )
    normalized_video: Optional[Path] = None
    if bluray_result.get("status") == "normalized":
        normalized_video = Path(str(bluray_result.get("result_file") or ""))
        if not normalized_video.exists() or normalized_video.suffix.lower() != ".mkv":
            bluray_result = {
                **bluray_result,
                "status": "verification_failed",
                "normalized": False,
                "source_removed": False,
                "reason": "El MKV normalizado no esta disponible para continuar",
            }
    if bluray_result.get("status") not in {"not_bluray", "normalized"}:
        status = str(bluray_result.get("status") or "unexpected_error")
        ambiguity = status in {"ambiguous", "no_safe_playlist"}
        reason_file = "Revision manual.txt" if ambiguity else "Error de proceso.txt"
        return _move_to_review(
            source,
            review_root,
            job_id,
            reason_file,
            [
                "No se normalizo automaticamente la estructura Blu-ray.",
                str(bluray_result.get("reason") or status),
                "El origen BDMV se conserva para revision.",
            ],
            {
                "job_id": job_id,
                "phase": "bluray",
                "source": str(source),
                "bluray": bluray_result,
            },
        )

    early_duplicate = _review_if_final_exists(
        source, final_root, review_root, job_id, "media_prefilter"
    )
    if early_duplicate:
        _emit_event(
            callback_url,
            "media",
            "skipped",
            "Pelicula repetida detectada antes de procesar",
            early_duplicate,
        )
        return early_duplicate

    videos = [normalized_video] if normalized_video else _video_files(source)
    if len(videos) != 1:
        _emit_event(
            callback_url,
            "media_analysis",
            "error",
            f"Video no valido: {len(videos)} videos",
            {"videos": [str(v) for v in videos]},
        )
        return _move_to_review(
            source,
            review_root,
            job_id,
            "Video no valido.txt",
            [f"Debe haber exactamente 1 video y hay {len(videos)}."],
            {"job_id": job_id, "phase": "media_core", "videos": [str(v) for v in videos]},
        )

    video = videos[0]
    rescue_result: Optional[Dict[str, object]] = None
    timings_ms: Dict[str, int] = {"analysis": 0, "ocr": 0, "remux": 0, "verification": 0}
    _emit_event(
        callback_url,
        "media_analysis",
        "started",
        "Analisis de pistas iniciado",
        {"video": str(video)},
    )
    analysis_started = time.monotonic()
    analysis, plan = _build_plan(video)
    timings_ms["analysis"] = round((time.monotonic() - analysis_started) * 1000)
    _emit_event(
        callback_url,
        "media_analysis",
        "finished",
        f"Analisis terminado: {plan.get('estado')}",
        {
            "video": str(video),
            "estado": plan.get("estado"),
            "problemas": plan.get("problemas"),
            "audio_modo": plan.get("audio_modo"),
            "subtitulo_titulo": plan.get("subtitulo_titulo"),
        },
    )

    try:
        subtitle_started = time.monotonic()
        analysis, plan, rescue_result = _prepare_subtitle_plan(
            analysis,
            plan,
            video,
            job_id,
            reports_root,
        )
        subtitle_elapsed = round((time.monotonic() - subtitle_started) * 1000)
        if rescue_result and str(rescue_result.get("mode") or "") == "ocr":
            timings_ms["ocr"] = subtitle_elapsed
        _apply_movie_processing_mode(analysis, plan, video)
        if rescue_result is not None:
            _emit_event(
                callback_url,
                "media_rescue",
                "finished",
                f"Subtitulo preparado: {rescue_result.get('mode')}",
                {
                    "mode": rescue_result.get("mode"),
                    "cues": rescue_result.get("cues"),
                    "events": rescue_result.get("events"),
                    "processing_mode": plan.get("processing_mode"),
                },
            )
    except Exception as error:
        _emit_event(
            callback_url,
            "media_rescue",
            "error",
            "Preparacion de subtitulos fallida",
            {"error": str(error), "video": str(video)},
        )
        return _move_to_review(
            source,
            review_root,
            job_id,
            "OCR subtitulo fallido.txt",
            [str(error)],
            {
                "job_id": job_id,
                "phase": "media_rescue",
                "source": str(source),
                "analysis": analysis,
                "plan": plan,
                "error": str(error),
            },
        )

    if plan.get("estado") != "PLAN APTO":
        reason_file = "Error de proceso.txt"
        problems = [str(x) for x in plan.get("problemas", [])] or [str(analysis.get("estado", ""))]
        if any("duplic" in p.lower() or "ya existe" in p.lower() for p in problems):
            reason_file = "Pelicula repetida.txt"
        elif any("audio" in p.lower() for p in problems):
            reason_file = "Audio no valido.txt"
        elif any("subtitulo" in p.lower() for p in problems):
            reason_file = "Subtitulo no convertible.txt"
        elif any("video" in p.lower() for p in problems):
            reason_file = "Video no valido.txt"
        _emit_event(
            callback_url,
            "media_analysis",
            "error",
            f"Plan no apto: {reason_file}",
            {"problemas": problems, "reason_file": reason_file},
        )
        return _move_to_review(
            source,
            review_root,
            job_id,
            reason_file,
            problems,
            {
                "job_id": job_id,
                "phase": "media_core",
                "source": str(source),
                "analysis": analysis,
                "plan": plan,
                "rescue": rescue_result,
            },
        )

    late_duplicate = _review_if_final_exists(
        source, final_root, review_root, job_id, "media_preprocess"
    )
    if late_duplicate:
        _emit_event(
            callback_url,
            "media",
            "skipped",
            "Pelicula repetida detectada antes de FFmpeg",
            late_duplicate,
        )
        return late_duplicate

    processing_mode = str(plan.get("processing_mode") or "single_remux")
    process_phase = "media_metadata" if processing_mode == "metadata_only" else "media_remux"
    process_label = {
        "metadata_only": "Vía rápida",
        "ocr_single_remux": "OCR y remux único",
    }.get(processing_mode, "Remux único")
    _emit_event(
        callback_url,
        process_phase,
        "started",
        f"{process_label} iniciada",
        {
            "entrada": plan.get("entrada"),
            "salida": plan.get("salida"),
            "audio_modo": plan.get("audio_modo"),
            "processing_mode": plan.get("processing_mode"),
        },
    )
    process_started = time.monotonic()
    process_result = procesador.ejecutar_ffmpeg(plan)
    process_elapsed = round((time.monotonic() - process_started) * 1000)
    if processing_mode != "metadata_only":
        timings_ms["remux"] = process_elapsed
    _write_json(reports_dir / "media_process.json", process_result)
    if not process_result.get("ok"):
        _emit_event(
            callback_url,
            process_phase,
            "error",
            f"{process_label} falló",
            {
                "returncode": process_result.get("returncode"),
                "salida": process_result.get("salida"),
                "log_tail": str(process_result.get("log") or "")[-1200:],
            },
        )
        return _move_to_review(
            source,
            review_root,
            job_id,
            "Error de proceso.txt",
            [str(process_result.get("log") or "FFmpeg no produjo salida valida.")[-3000:]],
            {
                "job_id": job_id,
                "phase": "media_core",
                "source": str(source),
                "analysis": analysis,
                "plan": plan,
                "process": process_result,
                "rescue": rescue_result,
            },
        )
    _emit_event(
        callback_url,
        process_phase,
        "finished",
        f"{process_label} terminada",
        {
            "salida": process_result.get("salida"),
            "tamano_salida": process_result.get("tamano_salida"),
            "audio_modo": process_result.get("audio_modo"),
            "capitulos": process_result.get("capitulos"),
            "processing_mode": processing_mode,
            "elapsed_ms": process_elapsed,
        },
    )

    clean_video = Path(str(process_result["salida"]))
    _emit_event(
        callback_url,
        "media_verify",
        "started",
        "Verificacion iniciada",
        {"video": str(clean_video)},
    )
    verification_started = time.monotonic()
    verification = verificador.verificar_archivo(clean_video)
    timings_ms["verification"] = round(
        (time.monotonic() - verification_started) * 1000
    )
    _write_json(reports_dir / "media_verify.json", verification)
    if verification.get("estado") != "LIMPIO OK":
        _emit_event(
            callback_url,
            "media_verify",
            "error",
            f"Verificacion fallida: {verification.get('estado')}",
            {
                "estado": verification.get("estado"),
                "problemas": verification.get("problemas"),
            },
        )
        return _move_to_review(
            source,
            review_root,
            job_id,
            "Error de proceso.txt",
            [str(x) for x in verification.get("problemas", [])],
            {
                "job_id": job_id,
                "phase": "media_verify",
                "source": str(source),
                "analysis": analysis,
                "plan": plan,
                "process": process_result,
                "verification": verification,
                "rescue": rescue_result,
            },
        )
    _emit_event(
        callback_url,
        "media_verify",
        "finished",
        "Verificacion OK",
        {"estado": verification.get("estado"), "pistas": verification.get("pistas")},
    )

    try:
        _emit_event(
            callback_url,
            "media_finalize",
            "started",
            "Movimiento final iniciado",
            {"source": str(source), "final_root": str(final_root)},
        )
        final = _finalize_movie(source, video, clean_video, final_root)
    except FileExistsError as error:
        _emit_event(
            callback_url,
            "media_finalize",
            "error",
            "Destino final ya existe",
            {"error": str(error), "source": str(source)},
        )
        return _move_to_review(
            source,
            review_root,
            job_id,
            "Pelicula repetida.txt",
            [str(error)],
            {
                "job_id": job_id,
                "phase": "media_finalize",
                "source": str(source),
                "error": str(error),
                "verification": verification,
            },
        )
    _emit_event(
        callback_url,
        "media_finalize",
        "finished",
        "Movimiento final terminado",
        final,
    )

    result = {
        "status": "done",
        "job_id": job_id,
        "source": str(source),
        "analysis": analysis,
        "plan": plan,
        "process": process_result,
        "verification": verification,
        "rescue": rescue_result,
        "processing_mode": processing_mode,
        "timings_ms": timings_ms,
        "final": final,
        "reports_dir": str(reports_dir),
    }
    _write_json(reports_dir / "media_result.json", result)
    _emit_event(
        callback_url,
        "media",
        "finished",
        "Media Worker terminado correctamente",
        {
            "reports_dir": str(reports_dir),
            "final": final,
            "processing_mode": processing_mode,
            "timings_ms": timings_ms,
        },
    )
    return result


def _trailer_package(source: Path) -> Tuple[Path, Path, Optional[Path]]:
    package = source if source.is_dir() else source.parent
    metas = sorted(package.glob("*.json"))
    if source.is_file() and source.suffix.lower() == ".json":
        metas = [source]
    if not metas:
        raise FileNotFoundError("No hay JSON de trailer en el paquete.")
    meta_path = metas[0]
    meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    wanted = str(meta.get("video_file") or "").strip()
    video = package / wanted if wanted else None
    if not video or not video.exists():
        candidates = [
            p for p in package.iterdir()
            if p.is_file() and p.suffix.lower() in trailer_runner.video_exts()
        ]
        video = candidates[0] if candidates else None
    if not video or not video.exists():
        raise FileNotFoundError("No hay video de trailer junto al JSON.")
    return package, meta_path, video


def process_trailer(payload: Dict[str, object]) -> Dict[str, object]:
    job_id = str(payload["job_id"])
    source = Path(str(payload["source_path"]))
    movies_root = Path(str(payload["movies_root"]))
    review_root = Path(str(payload["review_root"]))
    reports_root = Path(str(payload.get("reports_root") or "/logs/media-worker"))
    callback_url = str(payload.get("callback_url") or "")
    reports_dir = _reports(job_id, reports_root)
    _emit_event(
        callback_url,
        "trailer",
        "started",
        "Trailer Worker recibido",
        {"source": str(source), "reports_dir": str(reports_dir)},
    )

    try:
        _emit_event(
            callback_url,
            "trailer",
            "decision",
            "Leyendo paquete de trailer",
            {"source": str(source)},
        )
        package, meta_path, video = _trailer_package(source)
        meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
        title = str(meta.get("title") or meta.get("original_title") or "").strip()
        year = str(meta.get("year") or trailer_runner.pick_year(title) or "").strip()
        _emit_event(
            callback_url,
            "trailer",
            "decision",
            "Buscando carpeta destino de trailer",
            {"title": title, "year": year, "video": str(video), "meta": str(meta_path)},
        )
        trailer_runner.MOVIES = movies_root
        folder, score = trailer_runner.buscar_carpeta(title, year)
        if not folder:
            _emit_event(
                callback_url,
                "trailer",
                "warning",
                "Trailer sin coincidencia",
                {"title": title, "year": year, "score": round(score, 3)},
            )
            return _move_to_review(
                package,
                review_root,
                job_id,
                "Trailer sin coincidencia.txt",
                [f"No encuentro carpeta para {title} ({year}) score={score:.2f}"],
                {
                    "job_id": job_id,
                    "phase": "trailer",
                    "source": str(source),
                    "title": title,
                    "year": year,
                    "score": score,
                },
            )

        destination = trailer_runner.destino_trailer_final(folder, video.suffix)
        _emit_event(
            callback_url,
            "trailer",
            "started",
            "Moviendo trailer a destino",
            {"destination": str(destination), "matched_folder": folder.name, "score": round(score, 3)},
        )
        shutil.move(str(video), str(destination))
        meta["moved_to"] = str(destination)
        meta["matched_folder"] = folder.name
        meta["match_score"] = round(score, 3)
        meta["moved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(reports_dir / meta_path.name, meta)
        try:
            meta_path.unlink(missing_ok=True)
        except OSError:
            pass
        if package.is_dir():
            shutil.rmtree(package, ignore_errors=True)
        _emit_event(
            callback_url,
            "trailer",
            "finished",
            "Trailer terminado correctamente",
            {
                "destination": str(destination),
                "matched_folder": folder.name,
                "score": round(score, 3),
                "reports_dir": str(reports_dir),
            },
        )
        return {
            "status": "done",
            "job_id": job_id,
            "title": title,
            "year": year,
            "destination": str(destination),
            "matched_folder": folder.name,
            "score": round(score, 3),
            "reports_dir": str(reports_dir),
        }
    except Exception as error:
        _emit_event(
            callback_url,
            "trailer",
            "error",
            "Trailer fallo",
            {"source": str(source), "error": str(error)},
        )
        if source.exists():
            return _move_to_review(
                source if source.is_dir() else source.parent,
                review_root,
                job_id,
                "Trailer error.txt",
                [str(error)],
                {
                    "job_id": job_id,
                    "phase": "trailer",
                    "source": str(source),
                    "error": str(error),
                },
            )
        raise
