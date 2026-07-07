#!/usr/bin/env python3
import os
import json
import html
import shlex
from datetime import datetime
from pathlib import Path

from .reglas import cargar_reglas

TALLER = Path(os.environ.get("MEDIA_AUTO_TALLER", "/taller"))
WORK = TALLER / "work"
REPORTES = Path(os.environ.get("MEDIA_AUTO_REPORTES", "/reportes"))

REPORTE_JSON = REPORTES / "ultimo_reporte.json"
REGLAS = cargar_reglas()

def q(v):
    return shlex.quote(str(v))

def salida_para(ruta_original):
    entrada = Path(ruta_original)
    try:
        rel = entrada.relative_to(WORK / "entrada")
        destino = WORK / "terminado" / rel.parent / f"{entrada.stem}.limpio.mkv"
    except Exception:
        destino = WORK / "terminado" / f"{entrada.stem}.limpio.mkv"

    if destino.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destino = destino.with_name(f"{destino.stem}.{stamp}{destino.suffix}")

    return destino


def elegir_audio(audios):
    candidatos = [a for a in audios if int(a.get("prioridad", 0)) > 0]
    if not candidatos:
        return None
    return sorted(candidatos, key=lambda x: int(x.get("prioridad", 0)), reverse=True)[0]

def elegir_sub(subs):
    candidatos = [s for s in subs if s.get("decision") == "CANDIDATO FORZADO REAL"]
    if not candidatos:
        return None
    return sorted(candidatos, key=lambda x: (
        0 if x.get("delay_audio_aceptado") else 1,
        int(x.get("frases") or 999999)
    ))[0]

def frases_maximo_unico():
    try:
        return int(REGLAS.get("subtitulos", {}).get("frases_maximo_unico_forzado", 150) or 150)
    except Exception:
        return 150

def frases_subtitulo(sub):
    try:
        return int(sub.get("frases") or 999999)
    except Exception:
        return 999999

def subtitulo_unico_es_normal(subs):
    return False

def subtitulo_unico_es_largo(subs):
    modo = str(REGLAS.get("subtitulos", {}).get("unico_es_modo", "aplicar_limite")).strip().lower()
    candidatos = [s for s in subs if s.get("decision") == "CANDIDATO FORZADO REAL"]
    if any(s.get("delay_audio_aceptado") for s in candidatos):
        return False
    return modo == "aceptar_siempre" and len(candidatos) == 1 and frases_subtitulo(candidatos[0]) > frases_maximo_unico()

def subtitulo_unico_es_promo_trampa(subs):
    formatos_texto = set(REGLAS.get("subtitulos", {}).get("formatos_texto_aceptados", []))
    subtitulos_es = [s for s in subs if s.get("language_final") == "es"]
    return (
        len(subtitulos_es) == 1
        and subtitulos_es[0].get("codec") in formatos_texto
        and subtitulos_es[0].get("decision") == "DESCARTAR: posible promo/trampa"
    )

def subtitulos_solo_no_es(subs):
    return len(subs) > 0 and not any(s.get("language_final") == "es" for s in subs)

def procesar_sin_subtitulos(subs):
    modo = str(REGLAS.get("subtitulos", {}).get("sin_subtitulos_modo", "cuarentena")).strip().lower()
    return modo == "procesar_sin_subtitulos" and (
        len(subs) == 0
        or subtitulos_solo_no_es(subs)
        or subtitulo_unico_es_promo_trampa(subs)
        or subtitulo_unico_es_largo(subs)
    )

def etiqueta_codec_audio(codec):
    c = str(codec or "").lower()
    nombres = REGLAS.get("audio", {}).get("titulos_codec", {})
    return nombres.get(c, c.upper() if c else "Audio")

def etiqueta_canales(channels):
    channels = int(channels or 0)
    if channels >= 6:
        return "5.1"
    if channels == 1:
        return "1.0"
    if channels > 1:
        return f"{channels}.0"
    return ""

def titulo_audio_original(codec, channels):
    return f"{etiqueta_codec_audio(codec)} {etiqueta_canales(channels)}".strip()

def int_seguro(v, default=0):
    try:
        return int(v or default)
    except Exception:
        return default

def canales_convertir_desde():
    return int_seguro(REGLAS.get("audio", {}).get("canales_convertir_ac3_desde", 6), 6)

def es_ac3_5_1(codec, channels):
    return str(codec or "").lower() == "ac3" and int_seguro(channels) >= canales_convertir_desde()

def debe_convertir_ac3(codec, channels, audio_accion):
    accion = str(audio_accion or "")
    if accion == "convertir_ac3_5_1":
        return True
    if accion in {"copiar_ac3_5_1", "copiar_original"}:
        return False
    return int_seguro(channels) >= canales_convertir_desde() and str(codec or "").lower() != "ac3"

def disposicion(default=False, forced=False):
    flags = []
    if default:
        flags.append("default")
    if forced:
        flags.append("forced")
    return "+".join(flags) if flags else "0"

def crear_plan(r):
    videos = r.get("videos", [])
    audios = r.get("audios", [])
    subs = r.get("subtitulos", [])

    video = videos[0] if videos else None
    audio = elegir_audio(audios)
    sub = elegir_sub(subs)
    sin_subtitulos = procesar_sin_subtitulos(subs)
    if sin_subtitulos:
        sub = None

    entrada = Path(r["ruta"])
    destino = salida_para(r["ruta"])

    problemas = []
    estado_detector = str(r.get("estado") or "")
    if estado_detector.startswith("CUARENTENA") or estado_detector.startswith("ERROR"):
        problemas.append(estado_detector)
    if not video:
        problemas.append("No hay vídeo")
    if not audio:
        audios_es_invalidos = [
            a for a in audios
            if "CUARENTENA" in str(a.get("decision", ""))
        ]
        if audios_es_invalidos:
            problemas.append("Hay audio es/spa, pero no tiene canales claros")
        else:
            problemas.append("No hay audio es/spa valido")
    if not audio:
        problemas = [p for p in problemas if "audio" not in p.lower()]
        problemas.append("No hay audio valido con idioma es/spa")
    if not sub and not sin_subtitulos:
        problemas.append("No hay subtítulo forzado claro")

    if problemas:
        return {
            "archivo": r["archivo"],
            "estado": "NO APTO PARA PROCESADO",
            "problemas": problemas,
            "entrada": str(entrada),
            "salida": str(destino),
            "video": video,
            "audio": audio,
            "subtitulo": sub,
            "comando": "",
        }

    audio_codec = str(audio.get("codec", "")).lower()
    audio_channels = int(audio.get("channels", 0) or 0)
    audio_accion = str(audio.get("audio_accion") or "")
    audio_idx = audio["index"]
    video_idx = video["index"]
    sub_idx = sub["index"] if sub else None
    video_language_final = str(video.get("language_final") or REGLAS.get("video", {}).get("idioma_final", "es"))
    audio_language_final = str(audio.get("language_final") or video_language_final)
    sub_language_final = str(sub.get("language_final") or video_language_final) if sub else video_language_final
    sub_salida_normal = False if sin_subtitulos else subtitulo_unico_es_normal(subs)
    sub_title = "" if sub_salida_normal or sin_subtitulos else REGLAS.get("subtitulos", {}).get("titulo_final", "Forzados")
    sub_default = False if sub_salida_normal or sin_subtitulos else REGLAS.get("subtitulos", {}).get("interno_default", False)
    sub_forced = False if sub_salida_normal or sin_subtitulos else REGLAS.get("subtitulos", {}).get("interno_forzado", False)
    sub_exportar_srt = False if sub_salida_normal or sin_subtitulos else REGLAS.get("limpieza", {}).get("exportar_srt_externo", True)

    if debe_convertir_ac3(audio_codec, audio_channels, audio_accion):
        audio_args = f"-c:a ac3 -b:a {REGLAS.get('audio', {}).get('bitrate_ac3', '640k')} -ac 6"
        audio_modo = "convertir audio a AC3 5.1"
        audio_title_args = f"-metadata:s:a:0 title='{REGLAS.get('audio', {}).get('titulo_ac3_convertido', 'AC3 5.1')}' "
    elif audio_accion == "copiar_ac3_5_1" or es_ac3_5_1(audio_codec, audio_channels):
        audio_args = "-c:a copy"
        audio_modo = "copiar AC3 5.1 existente sin reconvertir"
        audio_title_args = f"-metadata:s:a:0 title='{REGLAS.get('audio', {}).get('titulo_ac3_convertido', 'AC3 5.1')}' "
    else:
        audio_args = "-c:a copy"
        audio_modo = "copiar audio inferior a 5.1 sin subir canales"
        audio_title_args = f"-metadata:s:a:0 title='{titulo_audio_original(audio_codec, audio_channels)}' "

    destino.parent.mkdir(parents=True, exist_ok=True)

    metadata_args = "-map_metadata -1 " if REGLAS.get("limpieza", {}).get("borrar_metadata_original", True) else ""
    sub_map_args = f"-map 0:{sub_idx} " if sub_idx is not None else ""
    sub_codec_args = "-c:s srt " if sub_idx is not None else ""
    sub_metadata_args = ""
    if sub_idx is not None:
        sub_metadata_args = (
            f"-metadata:s:s:0 language={sub_language_final} "
            f"-metadata:s:s:0 title={q(sub_title)} "
            f"-disposition:s:0 {disposicion(sub_default, sub_forced)} "
        )

    comando = (
        "ffmpeg -hide_banner -y "
        "-fflags +genpts "
        f"-i {q(entrada)} "
        f"-map 0:{video_idx} -map 0:{audio_idx} {sub_map_args}"
        f"{metadata_args}"
        "-c:v copy "
        f"{audio_args} "
        f"{sub_codec_args}"
        f"-metadata:s:v:0 language={video_language_final} "
        f"-metadata:s:a:0 language={audio_language_final} "
        f"{audio_title_args}"
        f"-disposition:v:0 {disposicion(REGLAS.get('video', {}).get('marcar_default', True), REGLAS.get('video', {}).get('marcar_forzado', False))} "
        f"-disposition:a:0 {disposicion(REGLAS.get('audio', {}).get('marcar_default', True), REGLAS.get('audio', {}).get('marcar_forzado', False))} "
        f"{sub_metadata_args}"
        f"{q(destino)}"
    )

    return {
        "archivo": r["archivo"],
        "estado": "PLAN APTO",
        "problemas": [],
        "entrada": str(entrada),
        "salida": str(destino),
        "video": video,
        "audio": audio,
        "subtitulo": sub,
        "sin_subtitulos": sin_subtitulos,
        "subtitulo_salida_normal": sub_salida_normal,
        "subtitulo_titulo": sub_title,
        "subtitulo_exportar_srt": sub_exportar_srt,
        "subtitulo_sufijo_srt": "" if sub_salida_normal else REGLAS.get("subtitulos", {}).get("sufijo_srt_externo", ".es.forced.srt"),
        "audio_modo": audio_modo,
        "comando": comando,
    }
