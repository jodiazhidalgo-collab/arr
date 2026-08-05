#!/usr/bin/env python3
import json
import re
import shutil
import subprocess
from pathlib import Path

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


def extraer_texto_srt(video, pista, tmp_dir):
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    salida = tmp_dir / "subtitle_selected.srt"
    salida.unlink(missing_ok=True)
    pista_id = int(pista.get("index", pista.get("id")))
    r = run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video),
            "-map", f"0:{pista_id}", "-c:s", "srt", str(salida),
        ],
        timeout=900,
    )
    ok, cues = srt_valido(salida)
    if r.returncode != 0 or not ok:
        salida.unlink(missing_ok=True)
        error = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()
        raise RuntimeError(
            "No se pudo extraer la pista de texto elegida como SRT. " + error[-1000:]
        )
    return salida, cues


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
