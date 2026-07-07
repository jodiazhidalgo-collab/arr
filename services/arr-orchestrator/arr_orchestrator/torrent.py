import hashlib
from pathlib import Path
from typing import Any, Dict, Tuple


def _parse(data: bytes, position: int = 0) -> Tuple[Any, int]:
    token = data[position : position + 1]
    if token == b"i":
        end = data.index(b"e", position)
        return int(data[position + 1 : end]), end + 1
    if token == b"l":
        output = []
        position += 1
        while data[position : position + 1] != b"e":
            value, position = _parse(data, position)
            output.append(value)
        return output, position + 1
    if token == b"d":
        output: Dict[bytes, Any] = {}
        position += 1
        while data[position : position + 1] != b"e":
            key, position = _parse(data, position)
            value, position = _parse(data, position)
            output[key] = value
        return output, position + 1
    if token.isdigit():
        colon = data.index(b":", position)
        length = int(data[position:colon])
        start = colon + 1
        end = start + length
        return data[start:end], end
    raise ValueError(f"bencode inválido en posición {position}")


def torrent_info(path: Path) -> Tuple[str, str]:
    data = path.read_bytes()
    if data[:1] != b"d":
        raise ValueError("el archivo no es un torrent bencode válido")
    position = 1
    info_raw = None
    info_object = None
    while data[position : position + 1] != b"e":
        key, position = _parse(data, position)
        if key == b"info":
            start = position
            info_object, position = _parse(data, position)
            info_raw = data[start:position]
        else:
            _, position = _parse(data, position)
    if not info_raw or not isinstance(info_object, dict):
        raise ValueError("el torrent no contiene bloque info")
    infohash = hashlib.sha1(info_raw).hexdigest().lower()
    raw_name = info_object.get(b"name") or path.stem.encode()
    name = raw_name.decode("utf-8", errors="replace") if isinstance(raw_name, bytes) else str(raw_name)
    return infohash, name
