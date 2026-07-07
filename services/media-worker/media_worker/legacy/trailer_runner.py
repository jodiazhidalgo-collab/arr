#!/usr/bin/env python3
import re
import unicodedata
import difflib
from pathlib import Path

from .reglas import flotante, lista, valor

MOVIES = Path('/media/movies')


def video_exts():
    return {str(ext).lower() for ext in lista('trailers.extensiones_video', ['.mp4', '.mkv'])}

def norm(value):
    txt = str(value or '').lower()
    txt = unicodedata.normalize('NFKD', txt)
    txt = ''.join(ch for ch in txt if not unicodedata.combining(ch))
    txt = re.sub(r'[^a-z0-9]+', ' ', txt)
    return re.sub(r'\s+', ' ', txt).strip()

def pick_year(value):
    m = re.search(r'\b(19\d{2}|20\d{2})\b', str(value or ''))
    return m.group(1) if m else ''

def clean_title(value):
    txt = str(value or '')
    txt = re.sub(r'\[[^\]]*\]', ' ', txt)
    txt = re.sub(r'\((19\d{2}|20\d{2})\)', ' ', txt)
    txt = re.sub(r'\b(19\d{2}|20\d{2})\b', ' ', txt)
    ruido = []
    for palabra in lista('trailers.palabras_ruido_titulo', []):
        parte = re.escape(str(palabra).strip()).replace(r'\ ', r'\s*')
        if parte:
            ruido.append(parte)
    if ruido:
        txt = re.sub(r'\b(?:' + '|'.join(ruido) + r')\b', ' ', txt, flags=re.I)
    return norm(txt)

def parse_folder(folder):
    return clean_title(folder.name), pick_year(folder.name)

def score_folder(movie_title, movie_year, folder):
    f_title, f_year = parse_folder(folder)
    wanted = clean_title(movie_title)
    if not wanted or not f_title:
        return 0.0
    year_mismatch = bool(movie_year and f_year and movie_year != f_year)
    ratio = difflib.SequenceMatcher(None, wanted, f_title).ratio()
    wt = [x for x in wanted.split() if len(x) > 1]
    ft = set(f_title.split())
    hits = sum(1 for x in wt if x in ft)
    token_score = hits / max(1, len(wt))
    score = ratio * 0.55 + token_score * 0.45
    if movie_year and f_year == movie_year:
        score += 0.18
    elif year_mismatch and (wanted == f_title or wanted in f_title or f_title in wanted):
        score -= 0.08
    elif year_mismatch:
        score -= 0.25
    if wanted in f_title:
        score += 0.12
    return min(score, 1.0)

def buscar_carpeta(movie_title, movie_year):
    if not MOVIES.exists():
        return None, 0.0
    mejores = []
    for folder in MOVIES.iterdir():
        if not folder.is_dir() or folder.name.startswith('.'):
            continue
        sc = score_folder(movie_title, movie_year, folder)
        if sc > 0:
            mejores.append((sc, folder))
    if not mejores:
        return None, 0.0
    mejores.sort(key=lambda x: x[0], reverse=True)
    best_score, best_folder = mejores[0]
    minimo = flotante('trailers.score_minimo_con_ano', 0.62) if movie_year else flotante('trailers.score_minimo_sin_ano', 0.78)
    if best_score >= minimo:
        return best_folder, best_score
    return None, best_score

def limpiar_trailers_previos(folder):
    permitidas = video_exts()
    nombre_base = str(valor('trailers.nombre_final', 'trailer')).strip() or 'trailer'
    for p in folder.iterdir():
        if p.is_file() and p.stem.lower() == nombre_base.lower() and p.suffix.lower() in permitidas:
            p.unlink(missing_ok=True)

def trailer_stem_ocupado(folder, stem):
    permitidas = video_exts()
    objetivo = str(stem or '').lower()
    return any(
        p.is_file() and p.stem.lower() == objetivo and p.suffix.lower() in permitidas
        for p in folder.iterdir()
    )

def destino_trailer_final(folder, suffix):
    nombre_base = str(valor('trailers.nombre_final', 'trailer')).strip() or 'trailer'
    suffix = str(suffix or '.mp4').lower()
    modo = str(valor('trailers.si_existe', 'renombrar_sin_borrar')).strip().lower()

    if modo == 'sustituir_anterior':
        limpiar_trailers_previos(folder)
        return folder / f'{nombre_base}{suffix}'

    if not trailer_stem_ocupado(folder, nombre_base):
        return folder / f'{nombre_base}{suffix}'

    contador = 1
    while trailer_stem_ocupado(folder, f'{nombre_base} ({contador})'):
        contador += 1
    return folder / f'{nombre_base} ({contador}){suffix}'
