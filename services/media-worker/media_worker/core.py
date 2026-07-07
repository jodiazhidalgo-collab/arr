import json
import re
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .legacy import detector, planificador, procesador, rescate_subtitulos, trailer_runner, verificador
from .legacy.reglas import valor


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mts", ".webm"}


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
    if folder.is_file() and folder.suffix.lower() in VIDEO_EXTENSIONS:
        return [folder]
    return sorted(
        p for p in folder.rglob("*")
        if p.is_file()
        and p.suffix.lower() in VIDEO_EXTENSIONS
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


def _is_rescue_candidate(plan: Dict[str, object], analysis: Dict[str, object]) -> bool:
    text = " ".join(str(x) for x in plan.get("problemas", []))
    text += " " + str(analysis.get("estado", ""))
    lowered = text.lower()
    return (
        "subtitulo" in lowered
        and (
            "imagen" in lowered
            or "ocr" in lowered
            or "convertible" in lowered
            or "dudoso" in lowered
        )
    )


def _rescue_in_place(folder: Path, video: Path, job_id: str, reports_root: Path) -> Dict[str, object]:
    tmp_dir = _reports(job_id, reports_root) / "rescue_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    data = rescate_subtitulos.ffprobe_json(video)
    pistas = rescate_subtitulos.completar_eventos_pistas_imagen(
        video, rescate_subtitulos.pistas_imagen_es(data)
    )
    pistas_texto = rescate_subtitulos.pistas_texto_mkv_es(video)
    pistas_texto_validas = rescate_subtitulos.pistas_texto_rescatables(video, pistas_texto)

    if pistas_texto_validas and pistas:
        final, original_eliminado, log = rescate_subtitulos.remux_con_texto_existente(
            video, pistas_texto_validas, folder
        )
        return {
            "status": "rescued",
            "mode": "texto_existente",
            "video": str(final),
            "original_removed": str(original_eliminado),
            "pistas_texto_validas": pistas_texto_validas,
            "log": log[-3000:],
        }

    if not pistas:
        raise RuntimeError("No hay subtitulo de imagen espanol para OCR.")

    pistas_rescatables = [
        pista for pista in pistas
        if rescate_subtitulos.subtitulo_imagen_rescatable(pista)
    ]
    if not pistas_rescatables:
        final, original_eliminado, log = rescate_subtitulos.remux_sin_subtitulos(video)
        return {
            "status": "rescued",
            "mode": "sin_subtitulos",
            "video": str(final),
            "original_removed": str(original_eliminado),
            "pistas_descartadas": pistas,
            "log": log[-3000:],
        }

    pista = pistas_rescatables[0]
    if rescate_subtitulos.subtitulo_imagen_largo(pista):
        final, original_eliminado, log = rescate_subtitulos.remux_sin_subtitulos(video)
        return {
            "status": "rescued",
            "mode": "sin_subtitulos",
            "video": str(final),
            "original_removed": str(original_eliminado),
            "pista_descartada": pista,
            "log": log[-3000:],
        }

    srt, cues, method = rescate_subtitulos.ejecutar_seconv(video, pista, tmp_dir)
    final, original_eliminado, log = rescate_subtitulos.remux_con_srt(video, srt, folder)
    return {
        "status": "rescued",
        "mode": "ocr",
        "method": method,
        "video": str(final),
        "original_removed": str(original_eliminado),
        "cues": cues,
        "log": log[-3000:],
    }


def _build_plan(video: Path) -> Tuple[Dict[str, object], Dict[str, object]]:
    analysis = detector.analizar_archivo(video)
    plan = planificador.crear_plan(analysis)
    output = video.with_name(f"{video.stem}.limpio.mkv")
    if output.exists():
        output = output.with_name(f"{output.stem}.{int(time.time())}{output.suffix}")
    plan["entrada"] = str(video)
    plan["salida"] = str(output)
    return analysis, plan


def _int_or_none(value: object) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _subtitle_rescuable_by_rules(events: Optional[int]) -> bool:
    if events is None:
        return True
    min_events = int(valor("subtitulos.frases_descartar_hasta", 1) or 1)
    max_events = int(valor("subtitulos.frases_maximo_unico_forzado", 150) or 150)
    return min_events < events <= max_events


def _discardable_image_subtitles(analysis: Dict[str, object], video: Path) -> Optional[List[Dict[str, object]]]:
    mode = str(valor("subtitulos.sin_subtitulos_modo", "cuarentena") or "").strip().lower()
    if mode != "procesar_sin_subtitulos":
        return None

    subtitles = analysis.get("subtitulos") or []
    if not isinstance(subtitles, list):
        return None

    text_codecs = set(valor("subtitulos.formatos_texto_aceptados", []) or [])
    image_codecs = set(valor("subtitulos.formatos_imagen_no_aceptados", []) or [])

    spanish_image = [
        s for s in subtitles
        if isinstance(s, dict)
        and s.get("language_final") == "es"
        and str(s.get("codec") or "").lower() in image_codecs
    ]
    if not spanish_image:
        return None

    if any(_int_or_none(subtitle.get("eventos")) is None for subtitle in spanish_image):
        try:
            data = rescate_subtitulos.ffprobe_json(video)
            image_tracks = rescate_subtitulos.pistas_imagen_es(data)
            by_index = {
                int(track.get("index")): track
                for track in rescate_subtitulos.completar_eventos_pistas_imagen(video, image_tracks)
                if track.get("index") is not None
            }
            for subtitle in spanish_image:
                idx = _int_or_none(subtitle.get("index"))
                if idx is not None and idx in by_index:
                    subtitle["eventos"] = by_index[idx].get("eventos")
        except Exception:
            return None

    for subtitle in spanish_image:
        events = _int_or_none(subtitle.get("eventos"))
        if _subtitle_rescuable_by_rules(events):
            return None

    return spanish_image


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

    output = video.with_name(f"{video.stem}.limpio.mkv")
    if output.exists():
        output = output.with_name(f"{output.stem}.{int(time.time())}{output.suffix}")
    plan["entrada"] = str(video)
    plan["salida"] = str(output)
    plan["rescate_una_pasada"] = True
    plan["subtitulos_descartados"] = discarded_subtitles
    return adjusted_analysis, plan


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

    videos = _video_files(source)
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
    _emit_event(
        callback_url,
        "media_analysis",
        "started",
        "Analisis de pistas iniciado",
        {"video": str(video)},
    )
    analysis, plan = _build_plan(video)
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

    if plan.get("estado") != "PLAN APTO" and _is_rescue_candidate(plan, analysis):
        _emit_event(
            callback_url,
            "media_rescue",
            "decision",
            "Rescate de subtitulos necesario",
            {"problemas": plan.get("problemas"), "estado": analysis.get("estado")},
        )
        discarded_subtitles = _discardable_image_subtitles(analysis, video)
        one_pass_plan = (
            _build_one_pass_no_subtitles_plan(video, analysis, discarded_subtitles)
            if discarded_subtitles
            else None
        )
        if one_pass_plan:
            analysis, plan = one_pass_plan
            _emit_event(
                callback_url,
                "media_rescue",
                "skipped",
                "Remux de rescate omitido: los subtitulos se descartan en la pasada final",
                {
                    "mode": "sin_subtitulos_una_pasada",
                    "video": str(video),
                    "pistas_descartadas": discarded_subtitles,
                },
            )
        else:
            try:
                _emit_event(
                    callback_url,
                    "media_rescue",
                    "started",
                    "Rescate de subtitulos iniciado",
                    {"video": str(video)},
                )
                rescue_result = _rescue_in_place(source, video, job_id, reports_root)
                _emit_event(
                    callback_url,
                    "media_rescue",
                    "finished",
                    f"Rescate terminado: {rescue_result.get('mode')}",
                    {
                        "mode": rescue_result.get("mode"),
                        "video": rescue_result.get("video"),
                        "cues": rescue_result.get("cues"),
                        "original_removed": rescue_result.get("original_removed"),
                    },
                )
                videos = _video_files(source)
                video = Path(str(rescue_result.get("video") or (videos[0] if videos else video)))
                _emit_event(
                    callback_url,
                    "media_analysis",
                    "started",
                    "Reanalisis tras rescate iniciado",
                    {"video": str(video)},
                )
                analysis, plan = _build_plan(video)
                _emit_event(
                    callback_url,
                    "media_analysis",
                    "finished",
                    f"Reanalisis terminado: {plan.get('estado')}",
                    {"estado": plan.get("estado"), "problemas": plan.get("problemas")},
                )
            except Exception as error:
                _emit_event(
                    callback_url,
                    "media_rescue",
                    "error",
                    "Rescate de subtitulos fallido",
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

    _emit_event(
        callback_url,
        "media_ffmpeg",
        "started",
        "FFmpeg iniciado",
        {
            "entrada": plan.get("entrada"),
            "salida": plan.get("salida"),
            "audio_modo": plan.get("audio_modo"),
        },
    )
    process_result = procesador.ejecutar_ffmpeg(plan)
    _write_json(reports_dir / "media_process.json", process_result)
    if not process_result.get("ok"):
        _emit_event(
            callback_url,
            "media_ffmpeg",
            "error",
            "FFmpeg fallo",
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
        "media_ffmpeg",
        "finished",
        "FFmpeg terminado",
        {
            "salida": process_result.get("salida"),
            "tamano_salida": process_result.get("tamano_salida"),
            "audio_modo": process_result.get("audio_modo"),
            "capitulos": process_result.get("capitulos"),
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
    verification = verificador.verificar_archivo(clean_video)
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
        "final": final,
        "reports_dir": str(reports_dir),
    }
    _write_json(reports_dir / "media_result.json", result)
    _emit_event(
        callback_url,
        "media",
        "finished",
        "Media Worker terminado correctamente",
        {"reports_dir": str(reports_dir), "final": final},
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
