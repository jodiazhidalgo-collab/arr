"""Seleccion determinista de una pista de video entre varias."""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Any


class VideoSelectionError(ValueError):
    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.details = details


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _tag(stream: dict[str, Any], name: str) -> Any:
    wanted = name.casefold()
    for key, value in (stream.get("tags") or {}).items():
        if str(key).casefold() == wanted:
            return value
    return None


def _duration(stream: dict[str, Any]) -> float:
    direct = _number(stream.get("duration"))
    if direct > 0:
        return direct
    text = str(_tag(stream, "duration") or "").strip()
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", text)
    if not match:
        return 0.0
    return (int(match.group(1)) * 3600) + (int(match.group(2)) * 60) + float(match.group(3))


def _bit_depth(stream: dict[str, Any]) -> int:
    direct = _integer(stream.get("bits_per_raw_sample"))
    if direct > 0:
        return direct
    pix_fmt = str(stream.get("pix_fmt") or "").casefold()
    match = re.search(r"p(\d{2})(?:le|be)?$", pix_fmt)
    if match:
        return int(match.group(1))
    profile = str(stream.get("profile") or "").casefold()
    match = re.search(r"\b(10|12)\b", profile)
    return int(match.group(1)) if match else 8


def _is_hdr(stream: dict[str, Any]) -> bool:
    transfer = str(stream.get("color_transfer") or "").casefold()
    if transfer in {"smpte2084", "arib-std-b67"}:
        return True
    for item in stream.get("side_data_list") or []:
        if "dovi" in str((item or {}).get("side_data_type") or "").casefold():
            return True
    return False


def _bitrate(stream: dict[str, Any]) -> int:
    return max(_integer(stream.get("bit_rate")), _integer(_tag(stream, "bps")))


def _codec_priority(stream: dict[str, Any]) -> int:
    codec = str(stream.get("codec_name") or "").casefold()
    return {
        "av1": 600,
        "hevc": 500,
        "h265": 500,
        "vp9": 450,
        "h264": 400,
        "avc": 400,
        "mpeg4": 300,
        "mpeg2video": 200,
        "vc1": 150,
    }.get(codec, 100)


def _unsafe_multiview(stream: dict[str, Any]) -> bool:
    disposition = stream.get("disposition") or {}
    if _integer(disposition.get("dependent")) == 1:
        return True
    stereo_mode = str(_tag(stream, "stereo_mode") or "").strip().casefold()
    if stereo_mode and stereo_mode not in {"mono", "2d"}:
        return True
    for item in stream.get("side_data_list") or []:
        side_type = str((item or {}).get("side_data_type") or "").casefold()
        if "stereo 3d" in side_type or "multiview" in side_type:
            return True
    return False


def _summary(stream: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": _integer(stream.get("index")),
        "codec": str(stream.get("codec_name") or ""),
        "width": _integer(stream.get("width")),
        "height": _integer(stream.get("height")),
        "duration": round(_duration(stream), 3),
        "hdr": _is_hdr(stream),
        "bit_depth": _bit_depth(stream),
        "default": _integer((stream.get("disposition") or {}).get("default")),
        "bit_rate": _bitrate(stream),
    }


def _details(
    streams: Sequence[dict[str, Any]],
    *,
    enabled: bool,
    selected_index: int | None,
    reason: str,
) -> dict[str, Any]:
    indexes = [_integer(stream.get("index")) for stream in streams]
    return {
        "enabled": bool(enabled),
        "input_count": len(streams),
        "selected_index": selected_index,
        "discarded_indexes": [index for index in indexes if index != selected_index],
        "reason": reason,
        "candidates": [_summary(stream) for stream in streams],
    }


def select_video_stream(
    streams: Sequence[dict[str, Any]],
    *,
    enabled: bool,
    eligible_indexes: Collection[int] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates_all = list(streams)
    if not candidates_all:
        message = "Se esperaban 1 pistas de vídeo y hay 0."
        raise VideoSelectionError(
            message,
            _details(candidates_all, enabled=enabled, selected_index=None, reason=message),
        )
    if len(candidates_all) > 1 and not enabled:
        message = f"Se esperaban 1 pistas de vídeo y hay {len(candidates_all)}."
        raise VideoSelectionError(
            message,
            _details(candidates_all, enabled=enabled, selected_index=None, reason=message),
        )

    eligible = (
        candidates_all
        if eligible_indexes is None
        else [
            stream
            for stream in candidates_all
            if _integer(stream.get("index")) in eligible_indexes
        ]
    )
    if not eligible:
        message = "Ninguna pista de vídeo tiene un idioma válido."
        raise VideoSelectionError(
            message,
            _details(candidates_all, enabled=enabled, selected_index=None, reason=message),
        )

    if len(candidates_all) > 1:
        if any(_unsafe_multiview(stream) for stream in candidates_all):
            message = "Las pistas de vídeo forman una estructura especial que requiere revisión."
            raise VideoSelectionError(
                message,
                _details(candidates_all, enabled=enabled, selected_index=None, reason=message),
            )
        if len(eligible) > 1 and any(
            _integer(stream.get("width")) <= 0 or _integer(stream.get("height")) <= 0
            for stream in eligible
        ):
            message = "No hay datos suficientes para elegir la mejor pista de vídeo."
            raise VideoSelectionError(
                message,
                _details(candidates_all, enabled=enabled, selected_index=None, reason=message),
            )

    ranked = list(eligible)
    known_durations = [_duration(stream) for stream in ranked if _duration(stream) > 0]
    if len(ranked) > 1 and known_durations:
        minimum_full_duration = max(known_durations) * 0.95
        full_duration = [stream for stream in ranked if _duration(stream) >= minimum_full_duration]
        if full_duration:
            ranked = full_duration

    def score(stream: dict[str, Any]) -> tuple[int, int, int, int, int, int, int]:
        width = _integer(stream.get("width"))
        height = _integer(stream.get("height"))
        return (
            width * height,
            int(_is_hdr(stream)),
            _bit_depth(stream),
            _integer((stream.get("disposition") or {}).get("default")),
            _bitrate(stream),
            _codec_priority(stream),
            -_integer(stream.get("index")),
        )

    selected = max(ranked, key=score)
    selected_index = _integer(selected.get("index"))
    reason = (
        "Única pista de vídeo."
        if len(candidates_all) == 1
        else "Elegida por duración, resolución y calidad técnica."
    )
    return selected, _details(
        candidates_all,
        enabled=enabled,
        selected_index=selected_index,
        reason=reason,
    )
