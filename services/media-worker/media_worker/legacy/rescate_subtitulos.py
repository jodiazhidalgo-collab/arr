#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

try:
    from .reglas import entero as regla_entero, lista as regla_lista
except Exception:
    def regla_entero(ruta, defecto=0):
        if ruta == "subtitulos.frases_maximo_unico_forzado":
            try:
                return int(os.environ.get("MEDIA_RESCATE_MAX_EVENTOS_FORZADOS", defecto) or defecto)
            except Exception:
                return defecto
        if ruta == "subtitulos.frases_descartar_hasta":
            return 1
        return defecto

    def regla_lista(ruta, defecto=None):
        return defecto or []

TALLER = Path(os.environ.get("MEDIA_AUTO_TALLER", "/taller"))
WORK = TALLER / "work"
CUARENTENA = TALLER / "cuarentena"
SOURCE_ROOT = TALLER / "movies_automatizacion"
REPORTES = Path(os.environ.get("MEDIA_RESCATE_REPORTES", "/reportes"))
TEMP = Path(os.environ.get("MEDIA_RESCATE_TEMP", "/temp/subtitulos"))

MOTIVOS_RESCATE = {
    "Subtitulo imagen no convertible.txt",
    "Subtitulo no convertible.txt",
}
MOTIVO_FALLO = "OCR subtitulo fallido.txt"

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mts", ".webm"}
IMAGE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
TEXT_CODECS = {"subrip", "ass", "ssa", "webvtt", "mov_text"}
MKVMERGE_TEXT_CODECS = ("subrip", "srt", "substation", "webvtt", "quicktime text", "ssa", "ass")
MAX_EVENTOS_FORZADOS = int(os.environ.get("MEDIA_RESCATE_MAX_EVENTOS_FORZADOS", "150") or "150")


def ahora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def texto_busqueda(valor):
    txt = unicodedata.normalize("NFKD", str(valor or ""))
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
    return txt.strip().lower()


def idiomas_subtitulo_validos():
    validos = {
        texto_busqueda(x)
        for x in regla_lista("subtitulos.idiomas_aceptados", ["es", "spa"])
    }
    validos = validos.intersection({"es", "spa"})
    return validos or {"es", "spa"}


def frases_descartar_hasta():
    return max(0, regla_entero("subtitulos.frases_descartar_hasta", 1))


def frases_maximas_rescate():
    minimo = frases_descartar_hasta() + 1
    maximo = regla_entero("subtitulos.frases_maximo_unico_forzado", MAX_EVENTOS_FORZADOS)
    return max(minimo, maximo)


def cantidad_rescatable(cantidad):
    numero = entero_no_negativo(cantidad)
    return numero is not None and frases_descartar_hasta() < numero <= frases_maximas_rescate()


def destino_numerado(destino):
    destino = Path(destino)
    if not destino.exists():
        return destino
    for n in range(1, 10000):
        candidato = destino.with_name(f"{destino.name} ({n})")
        if not candidato.exists():
            return candidato
    return destino.with_name(f"{destino.name} ({stamp()})")


def run(cmd, timeout=7200, cwd=None):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        timeout=timeout,
        cwd=cwd,
    )


def comando_ok(nombre):
    return shutil.which(nombre) is not None


def video_principal(carpeta):
    videos = [p for p in carpeta.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    if not videos:
        return None
    return sorted(videos, key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)[0]


def ffprobe_json(ruta):
    r = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,nb_frames,disposition:stream_tags=language,title,NUMBER_OF_FRAMES,NUMBER_OF_BLOCKS",
            "-print_format",
            "json",
            str(ruta),
        ],
        timeout=240,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "ffprobe no pudo leer el video").strip()[-2000:])
    return json.loads(r.stdout or "{}")


def ffprobe_streams_json(ruta):
    r = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name:stream_tags=language,title",
            "-print_format",
            "json",
            str(ruta),
        ],
        timeout=240,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "ffprobe no pudo leer el video").strip()[-2000:])
    return json.loads(r.stdout or "{}")


def tag(stream, nombre):
    buscado = str(nombre or "").lower()
    for clave, valor in (stream.get("tags") or {}).items():
        if str(clave or "").lower() == buscado:
            return valor
    return ""


def idioma(stream):
    return str(tag(stream, "language") or "").strip()


def titulo(stream):
    return str(tag(stream, "title") or "").strip()


def es_espanol(stream):
    lang = texto_busqueda(idioma(stream))
    txt = texto_busqueda(" ".join(str(v) for v in (stream.get("tags") or {}).values()))
    return (
        lang in {"es", "spa", "esp", "esl", "spanish", "castilian"}
        or "espanol" in txt
        or "spanish" in txt
        or "castellano" in txt
        or "latino" in txt
    )


def entero_no_negativo(valor):
    try:
        numero = int(str(valor or "").strip())
    except Exception:
        return None
    return numero if numero >= 0 else None


def eventos_subtitulo(stream):
    tags = stream.get("tags") or {}
    for valor in (
        tags.get("NUMBER_OF_FRAMES"),
        tags.get("NUMBER_OF_BLOCKS"),
        stream.get("nb_read_packets"),
        stream.get("nb_read_frames"),
        stream.get("nb_frames"),
    ):
        numero = entero_no_negativo(valor)
        if numero is not None:
            return numero
    return None


def eventos_subtitulo_pista(video, pista_index):
    r = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            str(int(pista_index)),
            "-count_packets",
            "-show_entries",
            "stream=nb_read_packets,nb_read_frames,nb_frames:stream_tags=NUMBER_OF_FRAMES,NUMBER_OF_BLOCKS",
            "-print_format",
            "json",
            str(video),
        ],
        timeout=900,
    )
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
    except Exception:
        return None
    return eventos_subtitulo(stream)


def pistas_imagen_es(data):
    candidatos = []
    for stream in data.get("streams", []) or []:
        if stream.get("codec_type") != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "").strip().lower()
        if codec not in IMAGE_CODECS:
            continue
        if not es_espanol(stream):
            continue
        item = {
            "index": int(stream.get("index") or 0),
            "codec": codec,
            "idioma": idioma(stream) or "-",
            "titulo": titulo(stream),
            "eventos": eventos_subtitulo(stream),
        }
        candidatos.append(item)
    return sorted(candidatos, key=lambda s: (s["eventos"] is None, s["eventos"] or 999999999, s["index"]))


def completar_eventos_pistas_imagen(video, pistas):
    for pista in pistas:
        if pista.get("eventos") is not None:
            continue
        pista["eventos"] = eventos_subtitulo_pista(video, pista["index"])
    return sorted(pistas, key=lambda s: (s["eventos"] is None, s["eventos"] or 999999999, s["index"]))


def subtitulo_imagen_largo(pista):
    eventos = entero_no_negativo(pista.get("eventos"))
    return eventos is not None and eventos > frases_maximas_rescate()


def subtitulo_imagen_rescatable(pista):
    eventos = entero_no_negativo(pista.get("eventos"))
    return eventos is None or cantidad_rescatable(eventos)


def mkvmerge_json(ruta):
    r = run(["mkvmerge", "-J", str(ruta)], timeout=300)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "mkvmerge no pudo leer el video").strip()[-2000:])
    return json.loads(r.stdout or "{}")


def texto_track_es(track):
    props = track.get("properties") or {}
    lang = texto_busqueda(props.get("language"))
    return lang in idiomas_subtitulo_validos()


def pistas_texto_mkv_es(video):
    data = mkvmerge_json(video)
    pistas = []
    for track in data.get("tracks", []) or []:
        if track.get("type") != "subtitles":
            continue
        codec = str(track.get("codec") or "").strip()
        codec_norm = texto_busqueda(codec)
        if not any(tipo in codec_norm for tipo in MKVMERGE_TEXT_CODECS):
            continue
        if not texto_track_es(track):
            continue
        props = track.get("properties") or {}
        pistas.append({
            "id": int(track.get("id")),
            "codec": codec,
            "idioma": props.get("language") or "-",
            "titulo": props.get("track_name") or "",
        })
    return sorted(pistas, key=lambda p: p["id"])


def contar_frases_pista_texto(video, pista):
    pista_id = int(pista["id"])
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v", "error",
        "-i", str(video),
        "-map", f"0:{pista_id}",
        "-f", "srt",
        "-"
    ]
    try:
        r = run(cmd, timeout=900)
    except subprocess.TimeoutExpired:
        return None, "Tiempo agotado extrayendo subtitulo de texto"
    if r.returncode != 0 or not (r.stdout or "").strip():
        error = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()
        return None, (error or "No se pudo extraer subtitulo de texto como SRT")[-800:]
    cues = len(re.findall(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->", r.stdout))
    if cues == 0:
        cues = r.stdout.count("-->")
    return cues, None


def pistas_texto_rescatables(video, pistas):
    buenas = []
    for pista in pistas:
        frases, error = contar_frases_pista_texto(video, pista)
        pista["frases"] = frases
        pista["error_conteo"] = error or ""
        if cantidad_rescatable(frases):
            buenas.append(pista)
    return sorted(buenas, key=lambda p: (int(p.get("frases") or 999999), p["id"]))


def srt_valido(ruta):
    ruta = Path(ruta)
    if not ruta.exists() or ruta.stat().st_size <= 20:
        return False, 0
    texto = ruta.read_text(encoding="utf-8", errors="replace")
    cues = len(re.findall(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->", texto))
    if cues == 0:
        cues = texto.count("-->")
    return cues > 0, cues


def buscar_srt(tmp_dir, nombre_preferido):
    preferido = tmp_dir / nombre_preferido
    if preferido.exists():
        return preferido
    srts = sorted(tmp_dir.glob("*.srt"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return srts[0] if srts else preferido


def extraer_vobsub(video, pista, tmp_dir):
    idx = tmp_dir / "vobsub_extraido.idx"
    sub = tmp_dir / "vobsub_extraido.sub"
    idx.unlink(missing_ok=True)
    sub.unlink(missing_ok=True)

    r = run(
        [
            "mkvextract",
            str(video),
            "tracks",
            f"{int(pista['index'])}:{idx}",
        ],
        timeout=14400,
    )
    if r.returncode != 0 or not idx.exists() or not sub.exists() or sub.stat().st_size <= 0:
        error = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        raise RuntimeError("mkvextract no pudo extraer VobSub. " + error[-3000:])
    return idx


def normalizar_srt_ocr(srt, tmp_dir):
    salida = tmp_dir / "subtitulo_rescatado.srt"
    salida.unlink(missing_ok=True)
    r = run(
        [
            "seconv",
            Path(srt).name,
            "subrip",
            f"--output-filename:{salida.name}",
            "--overwrite",
            "--merge-same-texts",
            "--split-long-lines",
            "--fix-common-errors",
            "--apply-duration-limits",
            "--apply-min-gap:24",
            "--quiet",
        ],
        timeout=14400,
        cwd=tmp_dir,
    )
    ok, cues = srt_valido(salida)
    lint = run(["seconv", "lint", salida.name], timeout=300, cwd=tmp_dir)
    if r.returncode == 0 and ok and lint.returncode == 0:
        return salida, cues

    error = ((r.stdout or "") + "\n" + (r.stderr or "") + "\n" + (lint.stdout or "") + "\n" + (lint.stderr or "")).strip()
    raise RuntimeError("El SRT OCR no supero la validacion. " + error[-3000:])


def segundos_srt(segundos):
    ms = max(0, int(round(float(segundos) * 1000)))
    horas = ms // 3600000
    ms %= 3600000
    minutos = ms // 60000
    ms %= 60000
    seg = ms // 1000
    ms %= 1000
    return f"{horas:02d}:{minutos:02d}:{seg:02d},{ms:03d}"


def limpiar_texto_ocr(texto):
    texto = str(texto or "").replace("\x0c", "\n")
    texto = re.sub(r"[ \t]+", " ", texto)
    lineas = []
    for linea in texto.splitlines():
        linea = linea.strip(" .|_-\t")
        linea = re.sub(r"^[^\w\u00bf\u00a1]+", "", linea)
        linea = re.sub(r"[^\w\u00bf\u00a1,.!?;:()\"' -]+$", "", linea)
        linea = re.sub(r"\s+", " ", linea).strip()
        if len(linea) >= 2 and re.search(r"\w", linea):
            lineas.append(linea)
    return "\n".join(lineas).strip()


def tamano_video(video):
    r = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-print_format",
            "json",
            str(video),
        ],
        timeout=240,
    )
    if r.returncode != 0:
        return 1280, 720
    try:
        data = json.loads(r.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        ancho = int(stream.get("width") or 1280)
        alto = int(stream.get("height") or 720)
        if ancho > 0 and alto > 0:
            return ancho, alto
    except Exception:
        pass
    return 1280, 720


def eventos_dvb_subtitle(video, pista):
    r = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            str(int(pista["index"])),
            "-show_packets",
            "-show_entries",
            "packet=pts_time,size",
            "-print_format",
            "json",
            str(video),
        ],
        timeout=600,
    )
    if r.returncode != 0:
        raise RuntimeError("ffprobe no pudo leer paquetes DVBSUB. " + ((r.stderr or r.stdout or "").strip())[-3000:])

    paquetes = []
    for paquete in (json.loads(r.stdout or "{}").get("packets") or []):
        try:
            paquetes.append(
                {
                    "tiempo": float(paquete.get("pts_time")),
                    "tamano": int(paquete.get("size") or 0),
                }
            )
        except Exception:
            continue

    eventos = []
    for i, paquete in enumerate(paquetes):
        if paquete["tamano"] <= 100:
            continue
        inicio = paquete["tiempo"]
        fin = None
        for siguiente in paquetes[i + 1 :]:
            if siguiente["tiempo"] > inicio:
                fin = siguiente["tiempo"]
                break
        if fin is None:
            fin = inicio + 4.0
        if fin - inicio < 0.25:
            fin = inicio + 2.0
        if fin - inicio > 12.0:
            fin = inicio + 6.0
        eventos.append((inicio, fin))

    if not eventos:
        raise RuntimeError("No se encontraron eventos DVBSUB con imagen.")
    return eventos


def ocr_imagen_subtitulo(imagen):
    for psm in ("6", "7"):
        r = run(["tesseract", str(imagen), "stdout", "-l", "spa", "--psm", psm], timeout=120)
        texto = limpiar_texto_ocr(r.stdout)
        if r.returncode == 0 and texto:
            return texto
    return ""


def escribir_srt_desde_cues(cues, salida):
    with Path(salida).open("w", encoding="utf-8") as f:
        for n, cue in enumerate(cues, start=1):
            f.write(f"{n}\n")
            f.write(f"{segundos_srt(cue['inicio'])} --> {segundos_srt(cue['fin'])}\n")
            f.write(f"{cue['texto']}\n\n")


def ejecutar_dvb_ocr(video, pista, tmp_dir):
    if not comando_ok("ffmpeg"):
        raise RuntimeError("Falta ffmpeg en el contenedor.")
    if not comando_ok("tesseract"):
        raise RuntimeError("Falta tesseract en el contenedor.")

    ancho, alto = tamano_video(video)
    eventos = eventos_dvb_subtitle(video, pista)
    stream_index = int(pista["index"])
    cues = []

    for numero, (inicio, fin) in enumerate(eventos, start=1):
        muestra = inicio + min(max((fin - inicio) / 2.0, 0.25), 0.75)
        seek = max(0.0, muestra - 10.0)
        relativo = muestra - seek
        imagen = tmp_dir / f"dvb_cue_{numero:03d}.png"
        imagen_ocr = tmp_dir / f"dvb_cue_{numero:03d}_ocr.png"

        r = run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{seek:.3f}",
                "-i",
                str(video),
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={ancho}x{alto}:r=24000/1001:d=20",
                "-ss",
                f"{relativo:.3f}",
                "-filter_complex",
                f"[1:v][0:{stream_index}]overlay,format=gray",
                "-frames:v",
                "1",
                "-y",
                str(imagen),
            ],
            timeout=180,
        )
        if r.returncode != 0 or not imagen.exists() or imagen.stat().st_size <= 0:
            continue

        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(imagen),
                "-vf",
                "scale=iw*2:ih*2:flags=lanczos,format=gray",
                "-frames:v",
                "1",
                "-y",
                str(imagen_ocr),
            ],
            timeout=120,
        )
        texto = ocr_imagen_subtitulo(imagen_ocr if imagen_ocr.exists() else imagen)
        if texto:
            cues.append({"inicio": inicio, "fin": fin, "texto": texto})

    if not cues:
        raise RuntimeError("OCR DVBSUB no genero frases de texto.")

    bruto = tmp_dir / "dvb_ocr_bruto.srt"
    escribir_srt_desde_cues(cues, bruto)
    salida, cues_validados = normalizar_srt_ocr(bruto, tmp_dir)
    return salida, cues_validados, f"ffmpeg DVBSUB fondo negro + tesseract ({len(cues)} frases) + validacion seconv"


def ejecutar_vobsubocr(video, pista, tmp_dir):
    if not comando_ok("vobsubocr"):
        raise RuntimeError("Falta vobsubocr en el contenedor.")

    idx = extraer_vobsub(video, pista, tmp_dir)
    bruto = tmp_dir / "vobsub_ocr_bruto.srt"
    bruto.unlink(missing_ok=True)
    r = run(
        [
            "vobsubocr",
            "--lang",
            "spa",
            "--output",
            str(bruto),
            str(idx),
        ],
        timeout=14400,
        cwd=tmp_dir,
    )
    ok, _ = srt_valido(bruto)
    if r.returncode != 0 or not ok:
        error = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        raise RuntimeError("vobsubocr no genero un SRT valido. " + error[-3000:])

    salida, cues = normalizar_srt_ocr(bruto, tmp_dir)
    return salida, cues, "mkvextract VobSub + vobsubocr + validacion seconv"


def reemplazar_original_verificado(video, tmp_mkv, salida_final):
    video = Path(video)
    tmp_mkv = Path(tmp_mkv)
    salida_final = Path(salida_final)

    if not tmp_mkv.exists() or tmp_mkv.stat().st_size <= 0:
        raise RuntimeError("No existe MKV temporal verificado para reemplazar el original.")

    original_eliminado = ""
    if video.exists():
        original_eliminado = str(video)
        video.unlink()

    if salida_final.exists():
        salida_final.unlink()

    tmp_mkv.rename(salida_final)
    return salida_final, original_eliminado


def ejecutar_seconv(video, pista, tmp_dir):
    if pista["codec"] == "dvd_subtitle":
        return ejecutar_vobsubocr(video, pista, tmp_dir)
    if pista["codec"] == "dvb_subtitle":
        return ejecutar_dvb_ocr(video, pista, tmp_dir)

    salida_nombre = "subtitulo_rescatado.srt"
    salida = tmp_dir / salida_nombre
    for anterior in tmp_dir.glob("*.srt"):
        anterior.unlink(missing_ok=True)

    # seconv usa numeracion de pista 1-based; ffprobe usa index 0-based.
    track_number = int(pista["index"]) + 1
    cmd = [
        "seconv",
        str(video),
        "subrip",
        f"--track-number:{track_number}",
        f"--output-folder:{tmp_dir}",
        f"--output-filename:{salida_nombre}",
        "--ocr-engine:tesseract",
        "--ocr-language:spa",
        "--remove-text-for-hi",
        "--overwrite",
        "--quiet",
    ]
    r = run(cmd, timeout=14400, cwd=tmp_dir)
    candidata = buscar_srt(tmp_dir, salida_nombre)
    ok, cues = srt_valido(candidata)
    if r.returncode == 0 and ok:
        return candidata, cues, f"seconv track-number {track_number}"

    error = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    raise RuntimeError("OCR no genero un SRT valido. " + error[-3000:])


def remux_con_srt(video, srt, carpeta):
    salida_final = video if video.suffix.lower() == ".mkv" else video.with_suffix(".mkv")
    tmp_mkv = salida_final.with_name(f"{salida_final.stem}.rescate_subtitulos.tmp.mkv")
    tmp_mkv.unlink(missing_ok=True)

    cmd = [
        "mkvmerge",
        "-o",
        str(tmp_mkv),
        "--no-subtitles",
        str(video),
        "--language",
        "0:spa",
        "--track-name",
        "0:Forzados",
        "--default-track",
        "0:no",
        "--forced-display-flag",
        "0:no",
        str(srt),
    ]
    r = run(cmd, timeout=14400)
    log = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode != 0 or not tmp_mkv.exists() or tmp_mkv.stat().st_size <= 0:
        tmp_mkv.unlink(missing_ok=True)
        raise RuntimeError("mkvmerge no pudo remuxar el SRT. " + log[-3000:])

    probe = ffprobe_streams_json(tmp_mkv)
    subs = [
        s
        for s in probe.get("streams", []) or []
        if s.get("codec_type") == "subtitle" and str(s.get("codec_name") or "").lower() in TEXT_CODECS
    ]
    if not subs:
        tmp_mkv.unlink(missing_ok=True)
        raise RuntimeError("El MKV remuxado no contiene SRT de texto.")

    salida_final, original_eliminado = reemplazar_original_verificado(video, tmp_mkv, salida_final)
    return salida_final, original_eliminado, log[-3000:]


def remux_con_texto_existente(video, pistas, carpeta):
    salida_final = video if video.suffix.lower() == ".mkv" else video.with_suffix(".mkv")
    tmp_mkv = salida_final.with_name(f"{salida_final.stem}.rescate_texto.tmp.mkv")
    tmp_mkv.unlink(missing_ok=True)

    track_ids = ",".join(str(p["id"]) for p in pistas)
    cmd = [
        "mkvmerge",
        "-o",
        str(tmp_mkv),
        "--no-subtitles",
        str(video),
        "--no-video",
        "--no-audio",
        "--no-attachments",
        "--no-chapters",
        "--subtitle-tracks",
        track_ids,
        str(video),
    ]
    r = run(cmd, timeout=14400)
    log = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode != 0 or not tmp_mkv.exists() or tmp_mkv.stat().st_size <= 0:
        tmp_mkv.unlink(missing_ok=True)
        raise RuntimeError("mkvmerge no pudo limpiar subtitulos de imagen. " + log[-3000:])

    data = ffprobe_streams_json(tmp_mkv)
    subs_imagen = [
        s
        for s in data.get("streams", []) or []
        if s.get("codec_type") == "subtitle" and str(s.get("codec_name") or "").lower() in IMAGE_CODECS
    ]
    subs_texto = [
        s
        for s in data.get("streams", []) or []
        if s.get("codec_type") == "subtitle" and str(s.get("codec_name") or "").lower() in TEXT_CODECS
    ]
    if subs_imagen or not subs_texto:
        tmp_mkv.unlink(missing_ok=True)
        raise RuntimeError("La limpieza no dejo un MKV con subtitulo de texto limpio.")

    salida_final, original_eliminado = reemplazar_original_verificado(video, tmp_mkv, salida_final)
    return salida_final, original_eliminado, log[-3000:]


def remux_sin_subtitulos(video):
    salida_final = video if video.suffix.lower() == ".mkv" else video.with_suffix(".mkv")
    tmp_mkv = salida_final.with_name(f"{salida_final.stem}.rescate_sin_subtitulos.tmp.mkv")
    tmp_mkv.unlink(missing_ok=True)

    cmd = [
        "mkvmerge",
        "-o",
        str(tmp_mkv),
        "--no-subtitles",
        str(video),
    ]
    r = run(cmd, timeout=14400)
    log = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode != 0 or not tmp_mkv.exists() or tmp_mkv.stat().st_size <= 0:
        tmp_mkv.unlink(missing_ok=True)
        raise RuntimeError("mkvmerge no pudo quitar subtitulos de imagen largos. " + log[-3000:])

    data = ffprobe_streams_json(tmp_mkv)
    subs = [s for s in data.get("streams", []) or [] if s.get("codec_type") == "subtitle"]
    if subs:
        tmp_mkv.unlink(missing_ok=True)
        raise RuntimeError("El MKV remuxado seguia teniendo subtitulos.")

    salida_final, original_eliminado = reemplazar_original_verificado(video, tmp_mkv, salida_final)
    return salida_final, original_eliminado, log[-3000:]
