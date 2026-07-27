#!/usr/bin/env python3
import os
import json
import html
import re
import subprocess
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path

from .reglas import REGLAS_ACTUALES

TALLER = Path(os.environ.get("MEDIA_AUTO_TALLER", "/taller"))
WORK = TALLER / "work"
ENTRADA = WORK / "entrada"
REPORTES = Path(os.environ.get("MEDIA_AUTO_REPORTES", "/reportes"))
LOGS = Path(os.environ.get("MEDIA_AUTO_LOGS", "/logs"))

REGLAS = REGLAS_ACTUALES
AUDIO_ACCION_CONVERTIR_AC3_5_1 = "convertir_ac3_5_1"
AUDIO_ACCION_COPIAR_AC3_5_1 = "copiar_ac3_5_1"
AUDIO_ACCION_COPIAR_ORIGINAL = "copiar_original"

def run(cmd, timeout=120):
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    return p.returncode, p.stdout, p.stderr

def normalizar_txt(v):
    return str(v or "").strip().lower()

def normalizar_busqueda(v):
    txt = unicodedata.normalize("NFKD", str(v or ""))
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
    return txt.strip().lower()

def tags_txt(stream):
    tags = stream.get("tags") or {}
    return " ".join(normalizar_txt(v) for v in tags.values())

def tag_sin_importar_mayusculas(stream, nombre):
    tags = stream.get("tags") or {}
    buscado = str(nombre or "").lower()
    for clave, valor in tags.items():
        if str(clave or "").lower() == buscado:
            return valor
    return None

def es_espanol(stream):
    tags = stream.get("tags") or {}
    lang = normalizar_txt(tags.get("language"))
    txt = tags_txt(stream)
    return (
        lang in {"es", "spa", "esp", "esl", "spanish", "castilian"}
        or "español" in txt
        or "espanol" in txt
        or "spanish" in txt
        or "castellano" in txt
        or "latino" in txt
    )

def es_ingles(stream):
    tags = stream.get("tags") or {}
    lang = normalizar_txt(tags.get("language"))
    txt = tags_txt(stream)
    return lang in {"en", "eng", "english"} or "english" in txt or "inglés" in txt or "ingles" in txt

def tiene_forzado(stream):
    disp = stream.get("disposition") or {}
    txt = tags_txt(stream)
    return (
        int(disp.get("forced", 0) or 0) == 1
        or "forced" in txt
        or "forzado" in txt
        or "forzados" in txt
        or "force" in txt
    )

def titulo(stream):
    tags = stream.get("tags") or {}
    return tags.get("title") or tags.get("handler_name") or ""

def idioma(stream):
    return tag_sin_importar_mayusculas(stream, "language") or "-"

def hay_audio_es_valido(streams):
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        channels = int(s.get("channels") or 0)
        if channels > 0 and idioma_audio_permitido(s):
            return True
    return False

def decision_idioma_video(stream, audio_es_valido=False):
    tags = stream.get("tags") or {}
    lang = normalizar_txt(tags.get("language"))
    video_rules = REGLAS.get("video", {})
    idiomas_es = set(video_rules.get("idiomas_aceptados", []))
    idiomas_indeterminados = set(video_rules.get("idiomas_indeterminados_como_es", []))
    idiomas_por_audio = {
        str(value or "").strip().lower()
        for value in video_rules.get("idiomas_corregibles_por_audio_es", [])
    }
    idioma_final = video_rules.get("idioma_final", "es")
    if lang in idiomas_es:
        return True, idioma_final, "OK: idioma de video espanol"
    if lang in idiomas_indeterminados:
        return True, idioma_final, "CORREGIR A ES: idioma de video indeterminado"
    if video_rules.get("aceptar_por_audio_es", False) and audio_es_valido and (lang == "" or lang in idiomas_por_audio):
        final_por_audio = str(video_rules.get("idioma_final_por_audio_es", "es") or "es").strip() or "es"
        return True, final_por_audio, f"CORREGIR A ES: idioma de video permitido por audio espanol ({lang or '-'})"
    return False, lang or "-", f"CUARENTENA: idioma de video no permitido ({lang or '-'})"

def video_queda_en_es(streams, audio_es_valido=False):
    idioma_final_esperado = normalizar_txt(REGLAS.get("video", {}).get("idioma_final", "es"))
    for s in streams:
        if s.get("codec_type") != "video":
            continue
        if int((s.get("disposition") or {}).get("attached_pic", 0) or 0) == 1:
            continue
        ok, idioma_final, _ = decision_idioma_video(s, audio_es_valido)
        if ok and normalizar_txt(idioma_final) == idioma_final_esperado:
            return True
    return False

def archivos_video_entrada():
    video_exts = set(REGLAS.get("entrada", {}).get("extensiones_video", []))
    return sorted(
        p for p in ENTRADA.rglob("*")
        if p.is_file() and p.suffix.lower() in video_exts
    )

def idioma_audio_permitido(stream):
    tags = stream.get("tags") or {}
    lang = normalizar_txt(tags.get("language"))
    return lang in set(REGLAS.get("audio", {}).get("idiomas_aceptados", []))

def idioma_audio_condicional_por_video(stream, video_queda_es):
    audio_rules = REGLAS.get("audio", {})
    if not audio_rules.get("aceptar_indeterminado_si_video_es", False) or not video_queda_es:
        return False
    tags = stream.get("tags") or {}
    lang = normalizar_txt(tags.get("language"))
    condicionales = {
        str(value or "").strip().lower()
        for value in audio_rules.get("idiomas_condicionales_si_video_es", [])
    }
    return lang == "" or lang in condicionales

def idioma_sub_permitido(stream):
    tags = stream.get("tags") or {}
    lang = normalizar_txt(tags.get("language"))
    return lang in set(REGLAS.get("subtitulos", {}).get("idiomas_aceptados", []))

def subtitulo_delay_audio_aceptado(stream, esp, codec, frases):
    subtitle_rules = REGLAS.get("subtitulos", {})
    delay_rules = subtitle_rules.get("delay_audio", {})
    delay_text = str(delay_rules.get("texto_titulo", "ESPAÑOL delay audio") or "").strip()
    text_subs = set(subtitle_rules.get("formatos_texto_aceptados", []))
    spam_max = int(subtitle_rules.get("frases_descartar_hasta", 1))
    delay_max = int(delay_rules.get("frases_maximo", 150) or 150)
    if not delay_rules.get("activo", True) or not delay_text:
        return False
    if not esp or codec not in text_subs or frases is None:
        return False
    frases_num = int(frases or 0)
    if frases_num <= spam_max:
        return False
    if frases_num > delay_max:
        return False
    return normalizar_busqueda(delay_text) in normalizar_busqueda(titulo(stream))

def int_seguro(v, default=0):
    try:
        return int(v or default)
    except Exception:
        return default

def canales_convertir_desde():
    return int_seguro(REGLAS.get("audio", {}).get("canales_convertir_ac3_desde", 6), 6)

def es_ac3_5_1(codec, channels):
    return str(codec or "").lower() == "ac3" and int_seguro(channels) >= canales_convertir_desde()

def prioridad_audio(codec, channels, bit_rate):
    channels = int_seguro(channels)
    codec_normalizado = str(codec or "").lower()
    codec_rank = REGLAS.get("audio", {}).get("codec_prioridad", {}).get(str(codec or "").lower(), 100)
    if channels >= 6:
        if es_ac3_5_1(codec_normalizado, channels):
            return 200000 + (channels * 1000) + codec_rank + min(bit_rate // 10000, 999)
        return 100000 + (channels * 1000) + codec_rank + min(bit_rate // 10000, 999)
    if channels > 0:
        return 50000 + (channels * 1000) + codec_rank + min(bit_rate // 10000, 999)
    return 0

def contar_frases_metadata(stream):
    tags = (stream or {}).get("tags") or {}
    for key in ("NUMBER_OF_FRAMES", "NUMBER_OF_BLOCKS"):
        valor = str(tags.get(key) or "").strip()
        if not valor:
            continue
        try:
            numero = int(valor)
        except Exception:
            continue
        if numero >= 0:
            return numero
    return None

def contar_frases_subtitulo(ruta, stream_index, stream=None):
    cues_metadata = contar_frases_metadata(stream)
    if cues_metadata is not None:
        return cues_metadata, None

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v", "error",
        "-i", str(ruta),
        "-map", f"0:{stream_index}",
        "-f", "srt",
        "-"
    ]
    try:
        code, out, err = run(cmd, timeout=360)
        if code != 0 or not out.strip():
            return None, (err.strip() or "No se pudo extraer como SRT")[:800]
        cues = len(re.findall(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->", out))
        if cues == 0:
            cues = out.count("-->")
        return cues, None
    except subprocess.TimeoutExpired:
        return None, "Tiempo agotado extrayendo subtítulo"
    except Exception as e:
        return None, str(e)

def int_no_negativo(v):
    try:
        numero = int(str(v or "").strip())
    except Exception:
        return None
    return numero if numero >= 0 else None

def contar_eventos_subtitulos(ruta):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-count_packets",
        "-show_entries",
        "stream=index,codec_type,nb_read_packets,nb_read_frames,nb_frames:stream_tags=NUMBER_OF_FRAMES,NUMBER_OF_BLOCKS",
        "-print_format", "json",
        str(ruta),
    ]
    try:
        code, out, _err = run(cmd, timeout=240)
        if code != 0:
            return {}
        data = json.loads(out or "{}")
    except Exception:
        return {}

    eventos = {}
    for st in data.get("streams", []) or []:
        if st.get("codec_type") != "subtitle":
            continue
        idx = st.get("index")
        tags = st.get("tags") or {}
        candidatos = [
            tags.get("NUMBER_OF_FRAMES"),
            tags.get("NUMBER_OF_BLOCKS"),
            st.get("nb_read_packets"),
            st.get("nb_read_frames"),
            st.get("nb_frames"),
        ]
        for candidato in candidatos:
            numero = int_no_negativo(candidato)
            if numero is not None:
                eventos[idx] = numero
                break
    return eventos

def analizar_archivo(ruta):
    code, out, err = run([
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(ruta)
    ], timeout=120)

    if code != 0:
        return {
            "archivo": ruta.name,
            "ruta": str(ruta),
            "estado": "ERROR",
            "motivo": err.strip()[:1000],
            "videos": [],
            "audios": [],
            "subtitulos": [],
        }

    data = json.loads(out or "{}")
    streams = data.get("streams", [])
    audio_rules = REGLAS.get("audio", {})
    subtitle_rules = REGLAS.get("subtitulos", {})
    text_subs = set(subtitle_rules.get("formatos_texto_aceptados", []))
    image_subs = set(subtitle_rules.get("formatos_imagen_no_aceptados", []))
    spam_max = int(subtitle_rules.get("frases_descartar_hasta", 1))
    sub_mode = str(subtitle_rules.get("sin_subtitulos_modo", "cuarentena")).strip().lower()
    unique_max = int(subtitle_rules.get("frases_maximo_unico_forzado", 150))
    unique_mode = str(subtitle_rules.get("unico_es_modo", "aplicar_limite")).strip().lower()
    final_conditional_audio = str(audio_rules.get("idioma_final_condicional", "es") or "es").strip() or "es"

    videos = []
    audios = []
    subtitulos = []
    eventos_subtitulos = None
    audio_es_valido = hay_audio_es_valido(streams)
    video_queda_es = video_queda_en_es(streams, audio_es_valido)

    for s in streams:
        stype = s.get("codec_type")
        idx = s.get("index")
        codec = s.get("codec_name") or "-"
        disp = s.get("disposition") or {}

        if stype == "video":
            if int(disp.get("attached_pic", 0) or 0) == 1:
                continue
            video_ok, idioma_final, decision = decision_idioma_video(s, audio_es_valido)
            videos.append({
                "index": idx,
                "codec": codec,
                "width": s.get("width"),
                "height": s.get("height"),
                "language": idioma(s),
                "language_final": idioma_final,
                "default": int(disp.get("default", 0) or 0),
                "forced": int(disp.get("forced", 0) or 0),
                "decision": decision,
                "prioridad": 100 if video_ok else 0,
            })

        elif stype == "audio":
            channels = int(s.get("channels") or 0)
            bit_rate = int_seguro(s.get("bit_rate"))
            audio_ok_directo = idioma_audio_permitido(s)
            audio_ok_condicional = idioma_audio_condicional_por_video(s, video_queda_es)
            audio_ok = audio_ok_directo or audio_ok_condicional
            idioma_final_audio = "es" if audio_ok_directo else (final_conditional_audio if audio_ok_condicional else "-")

            if not audio_ok:
                decision = "DESCARTAR: idioma de audio no permitido"
                prioridad = 0
                audio_accion = "descartar"
            elif channels >= 6:
                prioridad = prioridad_audio(codec, channels, bit_rate)
                if es_ac3_5_1(codec, channels):
                    if audio_ok_condicional and not audio_ok_directo:
                        decision = "CANDIDATO FINAL: audio AC3 5.1 sin idioma claro aceptado porque el video queda en ES; copiar sin reconvertir"
                    else:
                        decision = "CANDIDATO FINAL: audio es/spa AC3 5.1 ya preparado; copiar sin reconvertir"
                    audio_accion = AUDIO_ACCION_COPIAR_AC3_5_1
                elif audio_ok_condicional and not audio_ok_directo:
                    decision = "CANDIDATO FINAL: audio sin idioma claro aceptado porque el video queda en ES; convertir a AC3 5.1"
                    audio_accion = AUDIO_ACCION_CONVERTIR_AC3_5_1
                else:
                    decision = "CANDIDATO FINAL: audio es/spa 5.1 o superior; convertir a AC3 5.1"
                    audio_accion = AUDIO_ACCION_CONVERTIR_AC3_5_1
            elif channels > 0:
                if audio_ok_condicional and not audio_ok_directo:
                    decision = "CANDIDATO FINAL: audio sin idioma claro aceptado porque el video queda en ES; copiar sin subir a 5.1"
                else:
                    decision = "CANDIDATO FINAL: audio es/spa inferior a 5.1; copiar sin subir a 5.1"
                prioridad = prioridad_audio(codec, channels, bit_rate)
                audio_accion = AUDIO_ACCION_COPIAR_ORIGINAL
            else:
                decision = "CUARENTENA: audio permitido sin canales claros"
                prioridad = 0
                audio_accion = "sin_canales"

            audios.append({
                "index": idx,
                "codec": codec,
                "channels": channels,
                "channel_layout": s.get("channel_layout") or "-",
                "bit_rate": bit_rate,
                "language": idioma(s),
                "language_final": idioma_final_audio,
                "title": titulo(s),
                "default": int(disp.get("default", 0) or 0),
                "forced": int(disp.get("forced", 0) or 0),
                "decision": decision,
                "audio_accion": audio_accion,
                "prioridad": prioridad,
            })

        elif stype == "subtitle":
            esp = idioma_sub_permitido(s)
            forced = tiene_forzado(s)
            cues = None
            eventos = None
            error_extra = None

            if codec in text_subs:
                cues, error_extra = contar_frases_subtitulo(ruta, idx, s)
            elif codec in image_subs or codec not in text_subs:
                eventos = contar_frases_metadata(s)
                if eventos is None:
                    if eventos_subtitulos is None:
                        eventos_subtitulos = contar_eventos_subtitulos(ruta)
                    eventos = eventos_subtitulos.get(idx)

            delay_audio_ok = subtitulo_delay_audio_aceptado(s, esp, codec, cues)

            if not esp:
                decision = "DESCARTAR: no es español"
                prioridad = 0
            elif codec in image_subs:
                decision = "CUARENTENA: subtítulo de imagen/OCR"
                prioridad = 0
            elif codec not in text_subs:
                decision = "CUARENTENA: formato de subtítulo no controlado"
                prioridad = 0
            elif cues is None:
                decision = "DESCARTAR: subtitulo largo sin conteo"
                prioridad = 0
            elif cues <= spam_max:
                decision = "DESCARTAR: posible promo/trampa"
                prioridad = 0
            elif delay_audio_ok:
                decision = "CANDIDATO FORZADO REAL"
                prioridad = max(1, 200000 - int(cues or 0))
            elif cues > spam_max:
                decision = "CANDIDATO FORZADO REAL"
                prioridad = max(1, 100000 - int(cues or 0))
            elif False:
                decision = "DESCARTAR: subtítulo completo"
                prioridad = 10
            else:
                decision = "CUARENTENA: subtítulo dudoso"
                prioridad = 30

            subtitulos.append({
                "index": idx,
                "codec": codec,
                "language": idioma(s),
                "language_final": "es" if esp else "-",
                "title": titulo(s),
                "default": int(disp.get("default", 0) or 0),
                "forced": int((s.get("disposition") or {}).get("forced", 0) or 0),
                "nombre_forzado": forced,
                "delay_audio_aceptado": delay_audio_ok,
                "frases": cues,
                "eventos": eventos,
                "error_extra": error_extra,
                "decision": decision,
                "prioridad": prioridad,
            })

    pistas_video_esperadas = int(REGLAS.get("video", {}).get("pistas_exactas", 1) or 1)
    video_ok = len(videos) == pistas_video_esperadas and all(int(v.get("prioridad", 0)) >= 100 for v in videos)
    audio_ok = any(int(a.get("prioridad", 0)) > 0 for a in audios)
    sub_candidatos = [s for s in subtitulos if s["decision"] == "CANDIDATO FORZADO REAL"]
    sub_cero_ok = len(subtitulos) == 0 and sub_mode == "procesar_sin_subtitulos"
    aceptar_unico_es_siempre = unique_mode == "aceptar_siempre"
    sub_unico_ok = (
        len(sub_candidatos) == 1
        and int(sub_candidatos[0].get("frases") or 999999) <= unique_max
    )
    sub_unico_largo_sin_subtitulos_ok = (
        sub_mode == "procesar_sin_subtitulos"
        and aceptar_unico_es_siempre
        and len(sub_candidatos) == 1
        and int(sub_candidatos[0].get("frases") or 999999) > unique_max
    )
    subtitulos_es = [s for s in subtitulos if s.get("language_final") == "es"]
    sub_unico_promo_trampa_ok = (
        sub_mode == "procesar_sin_subtitulos"
        and len(subtitulos_es) == 1
        and subtitulos_es[0].get("codec") in text_subs
        and subtitulos_es[0].get("decision") == "DESCARTAR: posible promo/trampa"
    )
    sub_solo_no_es_ok = (
        sub_mode == "procesar_sin_subtitulos"
        and len(subtitulos) > 0
        and not subtitulos_es
    )
    sub_ok = sub_cero_ok or sub_solo_no_es_ok or sub_unico_promo_trampa_ok or sub_unico_largo_sin_subtitulos_ok or len(sub_candidatos) >= 2 or sub_unico_ok
    sub_delay_audio_ok = any(s.get("delay_audio_aceptado") for s in subtitulos)
    hay_sub_imagen = any("imagen" in s["decision"].lower() or "ocr" in s["decision"].lower() for s in subtitulos) and not sub_delay_audio_ok
    hay_sub_dudoso = any("CUARENTENA" in s["decision"] for s in subtitulos) and not sub_delay_audio_ok

    if not videos:
        estado = "ERROR: sin vídeo"
    elif len(videos) != pistas_video_esperadas:
        estado = f"CUARENTENA: debe haber exactamente {pistas_video_esperadas} pista de video"
    elif not video_ok:
        estado = "CUARENTENA: idioma de video no permitido"
    elif not audio_ok:
        estado = "CUARENTENA: no hay audio es/spa valido"
    elif sub_ok and not hay_sub_imagen and not hay_sub_dudoso:
        estado = "APTO PARA PROCESO AUTOMÁTICO"
    elif sub_ok:
        estado = "REVISAR: hay candidato, pero también pistas dudosas"
    else:
        estado = "CUARENTENA: no hay subtítulo forzado claro"

    if videos and len(videos) == pistas_video_esperadas and video_ok and audio_ok:
        if hay_sub_imagen or hay_sub_dudoso:
            estado = "CUARENTENA: hay subtitulo es/spa no convertible o dudoso"
        elif sub_ok:
            estado = "APTO PARA PROCESO AUTOMATICO"
        elif len(sub_candidatos) == 1:
            estado = f"CUARENTENA: unico subtitulo es/spa valido supera {unique_max} frases"
        else:
            estado = "CUARENTENA: no hay subtitulo es/spa valido"

    if not audio_ok and videos and len(videos) == pistas_video_esperadas and video_ok:
        estado = "CUARENTENA: no hay audio es/spa valido"

    return {
        "archivo": ruta.name,
        "ruta": str(ruta),
        "estado": estado,
        "videos": videos,
        "audios": sorted(audios, key=lambda x: x["prioridad"], reverse=True),
        "subtitulos": sorted(subtitulos, key=lambda x: x["prioridad"], reverse=True),
    }
