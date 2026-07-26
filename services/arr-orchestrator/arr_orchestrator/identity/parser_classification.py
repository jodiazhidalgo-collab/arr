import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .parser_cleaning import fold
from .parser_trace import ParserTrace


_WEAK_VIDEO_MARKERS = {
    "4k",
    "2160",
    "2160p",
    "1080",
    "1080p",
    "720",
    "720p",
    "576",
    "576p",
    "480",
    "480p",
    "uhd",
    "hdr",
    "hdr10",
    "dv",
    "dovi",
    "aac",
    "ac3",
    "eac3",
    "dts",
    "truehd",
    "castellano",
    "spanish",
    "dual",
    "sub",
    "subs",
    "multi",
}


def classification_evidence(
    raw: str,
    cleaned: str,
    rules: Mapping[str, Any],
    trace: Optional[ParserTrace] = None,
) -> Dict[str, object]:
    combined = f"{raw} {cleaned}".strip()
    suffix = _final_suffix(raw)
    video_extensions = {
        _extension(item) for item in rules.get("video_extensions") or [] if _extension(item)
    }
    video_markers = _matched_markers(combined, rules.get("video_markers") or [])
    non_video_markers = _matched_markers(combined, rules.get("non_video_markers") or [])
    strong_markers = [
        marker for marker in video_markers if fold(marker) not in {fold(item) for item in _WEAK_VIDEO_MARKERS}
    ]
    result: Dict[str, object] = {
        "suffix": suffix,
        "video_extension": bool(suffix and suffix in video_extensions),
        "video_markers": video_markers,
        "strong_video_markers": strong_markers,
        "non_video_markers": non_video_markers,
        "strong_video": bool((suffix and suffix in video_extensions) or strong_markers),
        "non_video": bool(non_video_markers),
    }
    if trace is not None:
        trace.record(
            "classification.video_evidence",
            {"raw": raw, "cleaned": cleaned},
            result,
            changed_only=False,
        )
    return result


def _matched_markers(value: str, markers: object) -> list[str]:
    normalized = fold(value)
    found: list[str] = []
    for item in markers if isinstance(markers, (list, tuple, set)) else []:
        marker = str(item or "").strip()
        needle = fold(marker)
        if needle and re.search(rf"(?:^|\s){re.escape(needle)}(?:\s|$)", normalized):
            if needle not in {fold(existing) for existing in found}:
                found.append(marker)
    return found


def _final_suffix(value: str) -> str:
    leaf = re.split(r"[\\/]", str(value or "").rstrip("\\/"))[-1]
    return Path(leaf).suffix.lower()


def _extension(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text if text.startswith(".") else f".{text}"
