#!/usr/bin/env python3
import os
import json
import html
import subprocess
from datetime import datetime
from pathlib import Path

from .reglas import cargar_reglas

TALLER = Path(os.environ.get("MEDIA_AUTO_TALLER", "/taller"))
WORK = TALLER / "work"
TERMINADO = WORK / "terminado"
REPORTES = Path(os.environ.get("MEDIA_AUTO_REPORTES", "/reportes"))
PROCESO_JSON = REPORTES / "ultimo_proceso.json"
REGLAS = cargar_reglas()
CHAPTER_TITLE_PREFIX = "Cap\u00edtulo"

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    return p.returncode, p.stdout, p.stderr

def ffprobe(ruta):
    code, out, err = run([
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(ruta)
    ])
    if code != 0:
        return None, err
    return json.loads(out), ""

def idioma(s):
    lang = ((s.get("tags") or {}).get("language", "-") or "-").lower()
    aceptados = set(REGLAS.get("video", {}).get("idiomas_aceptados", ["spa", "es", "esp"]))
    if lang in aceptados:
        return "ES"
    return lang

def title(s):
    return (s.get("tags") or {}).get("title", "")

def titulo_capitulo_esperado(numero):
    return f"{CHAPTER_TITLE_PREFIX} {numero:02d}"

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

def titulo_audio_esperado(codec, channels):
    channels = int(channels or 0)
    convertir_desde = int(REGLAS.get("audio", {}).get("canales_convertir_ac3_desde", 6) or 6)
    if channels >= convertir_desde:
        return str(REGLAS.get("audio", {}).get("titulo_ac3_convertido", "AC3 5.1"))
    return f"{etiqueta_codec_audio(codec)} {etiqueta_canales(channels)}".strip()

def esperado_bool(seccion, clave, defecto):
    return 1 if REGLAS.get(seccion, {}).get(clave, defecto) else 0

def permite_salida_sin_subtitulos():
    modo = str(REGLAS.get("subtitulos", {}).get("sin_subtitulos_modo", "cuarentena")).strip().lower()
    return modo == "procesar_sin_subtitulos"

def archivos_a_verificar():
    if PROCESO_JSON.exists():
        try:
            data = json.loads(PROCESO_JSON.read_text(encoding="utf-8"))
            salidas = []
            for r in data.get("resultados", []):
                if r.get("ok"):
                    p = Path(r.get("salida", ""))
                    if p.exists():
                        salidas.append(p)
            return salidas, "último proceso"
        except Exception:
            return [], "último proceso"

    return sorted(TERMINADO.rglob("*.mkv")), "carpeta TERMINADO"


def verificar_archivo(ruta):
    data, err = ffprobe(ruta)
    if not data:
        return {
            "archivo": ruta.name,
            "ruta": str(ruta),
            "estado": "ERROR",
            "motivo": err,
            "pistas": []
        }

    streams = data.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    adjuntos = [s for s in streams if s.get("codec_type") == "attachment"]
    chapters = data.get("chapters") or []

    pistas = []
    for s in streams:
        disp = s.get("disposition") or {}
        pistas.append({
            "index": s.get("index"),
            "tipo": s.get("codec_type"),
            "codec": s.get("codec_name"),
            "canales": s.get("channels", "-"),
            "idioma": idioma(s),
            "titulo": title(s),
            "default": int(disp.get("default", 0) or 0),
            "forced": int(disp.get("forced", 0) or 0),
        })

    problemas = []

    if len(videos) != 1:
        problemas.append(f"Debe haber 1 vídeo y hay {len(videos)}")

    if len(videos) == 1:
        v = videos[0]
        disp = v.get("disposition") or {}
        if idioma(v) != "ES":
            problemas.append("El video no esta marcado como ES")
        if int(disp.get("default", 0) or 0) != esperado_bool("video", "marcar_default", True):
            problemas.append("El video no tiene la marca predefinida esperada")
        if int(disp.get("forced", 0) or 0) != esperado_bool("video", "marcar_forzado", False):
            problemas.append("El video no tiene la marca forzada esperada")

    if len(audios) != 1:
        problemas.append(f"Debe haber 1 audio y hay {len(audios)}")
    else:
        a = audios[0]
        canales = int(a.get("channels") or 0)
        disp = a.get("disposition") or {}
        if canales <= 0:
            problemas.append("El audio no tiene canales claros")
        convertir_desde = int(REGLAS.get("audio", {}).get("canales_convertir_ac3_desde", 6) or 6)
        if canales >= convertir_desde and a.get("codec_name") != "ac3":
            problemas.append("El audio no es AC3")
        if idioma(a) != "ES":
            problemas.append("El audio no está marcado como ES")

        esperado = titulo_audio_esperado(a.get("codec_name"), canales)
        if title(a) != esperado:
            problemas.append(f"El audio debe tener titulo {esperado}")

        if int(disp.get("default", 0) or 0) != esperado_bool("audio", "marcar_default", True):
            problemas.append("El audio no tiene la marca predefinida esperada")
        if int(disp.get("forced", 0) or 0) != esperado_bool("audio", "marcar_forzado", False):
            problemas.append("El audio no tiene la marca forzada esperada")

    sin_subtitulos_ok = len(subs) == 0 and permite_salida_sin_subtitulos()

    if sin_subtitulos_ok:
        pass
    elif len(subs) != 1:
        problemas.append(f"Debe haber 1 subtítulo y hay {len(subs)}")
    else:
        s = subs[0]
        titulo_sub = str(REGLAS.get("subtitulos", {}).get("titulo_final", "Forzados"))
        if title(s) != titulo_sub:
            problemas.append(f"El subtitulo debe tener titulo {titulo_sub}")
        disp = s.get("disposition") or {}
        if s.get("codec_name") not in set(REGLAS.get("subtitulos", {}).get("formatos_texto_aceptados", ["subrip", "srt"])):
            problemas.append("El subtítulo no es SRT/SubRip")
        if idioma(s) != "ES":
            problemas.append("El subtítulo no está marcado como ES")
        if int(disp.get("forced", 0) or 0) != esperado_bool("subtitulos", "interno_forzado", False):
            problemas.append("El subtítulo interno no tiene la marca forzada esperada")
        if int(disp.get("default", 0) or 0) != esperado_bool("subtitulos", "interno_default", False):
            problemas.append("El subtítulo interno no tiene la marca predefinida esperada")

    if adjuntos:
        problemas.append(f"No debe haber adjuntos/caratulas y hay {len(adjuntos)}")

    if REGLAS.get("limpieza", {}).get("crear_capitulos", True):
        if not chapters:
            problemas.append("No hay capitulos generados")
        else:
            for numero, chapter in enumerate(chapters, start=1):
                esperado = titulo_capitulo_esperado(numero)
                actual = title(chapter)
                if actual != esperado:
                    problemas.append(f"El capitulo {numero:02d} debe tener titulo {esperado}")
                    break

    if subs and REGLAS.get("limpieza", {}).get("exportar_srt_externo", True):
        sufijo = str(REGLAS.get("subtitulos", {}).get("sufijo_srt_externo", ".es.forced.srt"))
        srt_externo = ruta.with_name(f"{ruta.stem.replace('.limpio', '')}{sufijo}")
        if not srt_externo.exists():
            problemas.append(f"No existe el subtítulo externo {sufijo}")

    estado = "LIMPIO OK" if not problemas else "REVISAR"

    return {
        "archivo": ruta.name,
        "ruta": str(ruta),
        "estado": estado,
        "problemas": problemas,
        "pistas": pistas,
    }
