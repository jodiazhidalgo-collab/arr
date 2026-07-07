#!/usr/bin/env python3
import os
import json
import html
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path

from .reglas import cargar_reglas

TALLER = Path(os.environ.get("MEDIA_AUTO_TALLER", "/taller"))
WORK = TALLER / "work"
REPORTES = Path(os.environ.get("MEDIA_AUTO_REPORTES", "/reportes"))
LOGS = Path(os.environ.get("MEDIA_AUTO_LOGS", "/logs"))
TEMP = Path(os.environ.get("MEDIA_AUTO_TEMP", "/temp/core"))
PLAN_JSON = REPORTES / "ultimo_plan.json"
REGLAS = cargar_reglas()
CHAPTER_TITLE_PREFIX = "Cap\u00edtulo"

def run(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace"
    )

def obtener_duracion_segundos(ruta):
    r = run([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(ruta)
    ])
    try:
        return float((r.stdout or "").strip())
    except Exception:
        return 0.0

def formato_duracion(segundos):
    segundos = int(segundos or 0)
    minutos, seg = divmod(segundos, 60)
    horas, minutos = divmod(minutos, 60)
    if horas:
        return f"{horas}h {minutos}m {seg}s"
    if minutos:
        return f"{minutos}m {seg}s"
    return f"{seg}s"

def formato_tiempo_mkvtoolnix(segundos):
    nanosegundos = max(0, int(round(float(segundos or 0) * 1_000_000_000)))
    total_segundos, ns = divmod(nanosegundos, 1_000_000_000)
    minutos_total, seg = divmod(total_segundos, 60)
    horas, minutos = divmod(minutos_total, 60)
    return f"{horas:02d}:{minutos:02d}:{seg:02d}.{ns:09d}"

def crear_capitulos_10min(ruta_entrada, ruta_capitulos, duracion=None):
    duracion = obtener_duracion_segundos(ruta_entrada) if duracion is None else float(duracion or 0)
    paso = int(REGLAS.get("limpieza", {}).get("capitulo_cada_segundos", 600) or 600)

    if duracion <= 0 or not REGLAS.get("limpieza", {}).get("crear_capitulos", True):
        ruta_capitulos.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<Chapters />\n',
            encoding="utf-8",
        )
        return 0

    lineas = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<Chapters>",
        "  <EditionEntry>",
    ]
    inicio = 0
    numero = 1

    while inicio < duracion:
        fin = min(inicio + paso, duracion)
        titulo = html.escape(f"{CHAPTER_TITLE_PREFIX} {numero:02d}", quote=False)
        lineas.append("    <ChapterAtom>")
        lineas.append(f"      <ChapterTimeStart>{formato_tiempo_mkvtoolnix(inicio)}</ChapterTimeStart>")
        lineas.append(f"      <ChapterTimeEnd>{formato_tiempo_mkvtoolnix(fin)}</ChapterTimeEnd>")
        lineas.append("      <ChapterDisplay>")
        lineas.append(f"        <ChapterString>{titulo}</ChapterString>")
        lineas.append("        <ChapterLanguage>spa</ChapterLanguage>")
        lineas.append("      </ChapterDisplay>")
        lineas.append("    </ChapterAtom>")
        inicio += paso
        numero += 1

    lineas.extend(["  </EditionEntry>", "</Chapters>"])
    ruta_capitulos.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return numero - 1

def ejecutar_ffmpeg_con_latido(cmd, duracion_total, archivo):
    inicio = time.time()
    ultimo_latido = inicio
    out_time = 0.0
    speed = ""
    lineas = []

    print(f"FFmpeg iniciado: {archivo}", flush=True)
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1
    )

    if p.stdout:
        for linea in p.stdout:
            linea = linea.rstrip()
            if linea:
                lineas.append(linea)
            if linea.startswith("out_time_ms="):
                try:
                    out_time = int(linea.split("=", 1)[1]) / 1_000_000
                except Exception:
                    pass
            elif linea.startswith("out_time="):
                partes = linea.split("=", 1)[1].split(":")
                try:
                    if len(partes) == 3:
                        out_time = int(partes[0]) * 3600 + int(partes[1]) * 60 + float(partes[2])
                except Exception:
                    pass
            elif linea.startswith("speed="):
                speed = linea.split("=", 1)[1].strip()

            ahora_ts = time.time()
            if ahora_ts - ultimo_latido >= 120:
                transcurrido = formato_duracion(ahora_ts - inicio)
                if duracion_total and out_time:
                    porcentaje = min(99.9, max(0.0, (out_time / float(duracion_total)) * 100))
                    velocidad = speed or "-"
                    print(f"Procesando video: {porcentaje:.0f}% | {transcurrido} | velocidad {velocidad}", flush=True)
                else:
                    print(f"Procesando video: activo desde hace {transcurrido}", flush=True)
                ultimo_latido = ahora_ts

    returncode = p.wait()
    estado = "OK" if returncode == 0 else "ERROR"
    print(f"FFmpeg terminado {estado} en {formato_duracion(time.time() - inicio)}.", flush=True)

    salida = "\n".join(lineas)
    return subprocess.CompletedProcess(cmd, returncode, stdout=salida, stderr="")

def etiqueta_codec_audio(codec):
    c = str(codec or "").lower()
    nombres = REGLAS.get("audio", {}).get("titulos_codec", {})
    return nombres.get(c, c.upper() if c else "Audio")

def etiqueta_canales(channels):
    try:
        channels = int(channels or 0)
    except Exception:
        channels = 0
    if channels >= 6:
        return "5.1"
    if channels == 1:
        return "1.0"
    if channels > 1:
        return f"{channels}.0"
    return ""

def titulo_audio_original(codec, channels):
    codec_txt = etiqueta_codec_audio(codec)
    canales_txt = etiqueta_canales(channels)
    return f"{codec_txt} {canales_txt}".strip()

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

def limpiar_tags_mkv(ruta):
    return run(["mkvpropedit", str(ruta), "--tags", "all:"])

def aplicar_capitulos_mkv(ruta, ruta_capitulos):
    return run(["mkvpropedit", str(ruta), "--chapters", str(ruta_capitulos)])

def ejecutar_ffmpeg(plan):
    entrada = Path(plan["entrada"])
    salida = Path(plan["salida"])
    salida.parent.mkdir(parents=True, exist_ok=True)
    TEMP.mkdir(parents=True, exist_ok=True)

    video = plan["video"]
    audio = plan["audio"]
    sub = plan.get("subtitulo")
    sin_subtitulos = bool(plan.get("sin_subtitulos")) or not sub

    audio_codec = str(audio.get("codec", "")).lower()
    audio_channels = int(audio.get("channels", 0) or 0)
    audio_accion = str(audio.get("audio_accion") or "")
    video_language_final = str(video.get("language_final") or REGLAS.get("video", {}).get("idioma_final", "es"))
    audio_language_final = str(audio.get("language_final") or video_language_final)
    sub_language_final = str(sub.get("language_final") or video_language_final) if sub else video_language_final
    sub_salida_normal = bool(plan.get("subtitulo_salida_normal"))
    sub_title = str(plan.get("subtitulo_titulo") if plan.get("subtitulo_titulo") is not None else ("" if sub_salida_normal else REGLAS.get("subtitulos", {}).get("titulo_final", "Forzados")))
    sub_default = False if sub_salida_normal else REGLAS.get("subtitulos", {}).get("interno_default", False)
    sub_forced = False if sub_salida_normal else REGLAS.get("subtitulos", {}).get("interno_forzado", False)
    sub_exportar_srt = False if sin_subtitulos else bool(plan.get("subtitulo_exportar_srt", REGLAS.get("limpieza", {}).get("exportar_srt_externo", True)))
    sub_sufijo_srt = str(plan.get("subtitulo_sufijo_srt") or REGLAS.get("subtitulos", {}).get("sufijo_srt_externo", ".es.forced.srt"))

    tmp = salida.with_suffix(".procesando.tmp.mkv")
    metadata_chapters = TEMP / f"chapters_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.xml"

    if tmp.exists():
        tmp.unlink()

    duracion_total = obtener_duracion_segundos(entrada)
    total_capitulos = crear_capitulos_10min(entrada, metadata_chapters, duracion_total)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-progress", "pipe:1",
        "-y",
        "-fflags", "+genpts",
        "-i", str(entrada),

        "-map", f"0:{video['index']}",
        "-map", f"0:{audio['index']}",
        "-c:v", "copy",
    ]

    if not sin_subtitulos:
        cmd += ["-map", f"0:{sub['index']}"]

    if REGLAS.get("limpieza", {}).get("borrar_metadata_original", True):
        cmd += ["-map_metadata", "-1"]
    if REGLAS.get("limpieza", {}).get("crear_capitulos", True):
        cmd += ["-map_chapters", "-1"]

    if debe_convertir_ac3(audio_codec, audio_channels, audio_accion):
        cmd += ["-c:a", "ac3", "-b:a", str(REGLAS.get("audio", {}).get("bitrate_ac3", "640k")), "-ac", "6"]
        audio_modo = "Audio convertido a AC3 5.1"
        audio_title = str(REGLAS.get("audio", {}).get("titulo_ac3_convertido", "AC3 5.1"))
    elif audio_accion == "copiar_ac3_5_1" or es_ac3_5_1(audio_codec, audio_channels):
        cmd += ["-c:a", "copy"]
        audio_modo = "Audio AC3 5.1 copiado sin reconvertir"
        audio_title = str(REGLAS.get("audio", {}).get("titulo_ac3_convertido", "AC3 5.1"))
    else:
        cmd += ["-c:a", "copy"]
        audio_modo = "Audio inferior a 5.1 copiado sin subir canales"
        audio_title = titulo_audio_original(audio_codec, audio_channels)

    if not sin_subtitulos:
        cmd += ["-c:s", "srt"]

    cmd += [
        "-metadata:s:v:0", f"language={video_language_final}",
        "-metadata:s:a:0", f"language={audio_language_final}",

        "-disposition:v:0", disposicion(REGLAS.get("video", {}).get("marcar_default", True), REGLAS.get("video", {}).get("marcar_forzado", False)),
        "-disposition:a:0", disposicion(REGLAS.get("audio", {}).get("marcar_default", True), REGLAS.get("audio", {}).get("marcar_forzado", False)),
    ]

    if not sin_subtitulos:
        cmd += [
            "-metadata:s:s:0", f"language={sub_language_final}",
            "-metadata:s:s:0", f"title={sub_title}",
            "-disposition:s:0", disposicion(sub_default, sub_forced),
        ]

    cmd += [str(tmp)]

    cmd[-1:-1] = ["-metadata:s:a:0", f"title={audio_title}"]

    p = ejecutar_ffmpeg_con_latido(cmd, duracion_total, plan["archivo"])

    ok = p.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0

    if ok:
        if salida.exists():
            backup = salida.with_name(f"{salida.stem}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{salida.suffix}")
            salida.rename(backup)
        tmp.rename(salida)

        if REGLAS.get("limpieza", {}).get("limpiar_tags_mkv", True):
            r_clean = limpiar_tags_mkv(salida)
            if r_clean.returncode != 0:
                ok = False

        if ok and total_capitulos > 0:
            r_chapters = aplicar_capitulos_mkv(salida, metadata_chapters)
            if r_chapters.returncode != 0:
                ok = False

        if sub_exportar_srt and not sin_subtitulos:
            srt_externo = salida.with_name(f"{salida.stem.replace('.limpio', '')}{sub_sufijo_srt}")
            r_srt = run([
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-y",
                "-fflags", "+genpts",
                "-i", str(entrada),
                "-map", f"0:{sub['index']}",
                "-c:s", "srt",
                str(srt_externo)
            ])

            if r_srt.returncode != 0 or not srt_externo.exists():
                ok = False
    else:
        if tmp.exists():
            tmp.unlink()

    try:
        metadata_chapters.unlink(missing_ok=True)
    except Exception:
        pass

    log_ffmpeg = (p.stderr or p.stdout or "").strip()
    lineas = log_ffmpeg.splitlines()
    log_limpio = "\n".join(lineas[-80:]) if lineas else "Sin mensajes."

    return {
        "archivo": plan["archivo"],
        "entrada": str(entrada),
        "salida": str(salida),
        "returncode": p.returncode,
        "ok": ok and salida.exists(),
        "audio_modo": audio_modo,
        "audio_titulo": audio_title,
        "capitulos": total_capitulos,
        "limpieza": "Metadatos originales, capítulos originales, adjuntos y carátulas descartados",
        "log": log_limpio,
        "tamano_salida": salida.stat().st_size if salida.exists() else 0,
    }
