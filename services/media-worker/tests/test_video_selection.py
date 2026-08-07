import json
from copy import deepcopy
from pathlib import Path

import pytest

from media_worker.legacy import detector, planificador, reglas
from media_worker.video_selection import VideoSelectionError, select_video_stream


def _video(
    index: int,
    *,
    width: int = 1920,
    height: int = 1080,
    duration: str = "01:00:00.000",
    default: int = 0,
    dependent: int = 0,
) -> dict:
    return {
        "index": index,
        "codec_type": "video",
        "codec_name": "h264",
        "profile": "High",
        "pix_fmt": "yuv420p",
        "width": width,
        "height": height,
        "tags": {"language": "spa", "DURATION": duration, "BPS": "4000000"},
        "disposition": {
            "attached_pic": 0,
            "default": default,
            "dependent": dependent,
        },
    }


def test_multiple_video_tracks_are_rejected_when_selection_is_disabled() -> None:
    with pytest.raises(VideoSelectionError, match="hay 2"):
        select_video_stream([_video(0), _video(1)], enabled=False)


def test_real_4k_hdr_track_wins_over_the_secondary_720p_track() -> None:
    main = _video(0, width=3840, height=1600, default=1)
    main.update(
        {
            "codec_name": "hevc",
            "profile": "Main 10",
            "pix_fmt": "yuv420p10le",
            "color_transfer": "smpte2084",
            "tags": {"language": "spa", "DURATION": "01:12:16.584", "BPS": "18658829"},
        }
    )
    secondary = _video(1, width=1248, height=520, default=1)
    secondary["tags"].update({"DURATION": "01:12:16.584", "BPS": "2319335"})

    selected, details = select_video_stream(
        [main, secondary], enabled=True, eligible_indexes={0, 1}
    )

    assert selected["index"] == 0
    assert details["selected_index"] == 0
    assert details["discarded_indexes"] == [1]
    assert details["candidates"][0]["hdr"] is True
    assert details["candidates"][0]["bit_depth"] == 10


def test_full_duration_and_stable_tie_breakers_are_respected() -> None:
    short_4k = _video(0, width=3840, height=2160, duration="00:05:00.000")
    full_hd = _video(1, width=1920, height=1080, duration="01:00:00.000")
    selected, _details = select_video_stream([short_4k, full_hd], enabled=True)
    assert selected["index"] == 1

    first = _video(3, default=0)
    preferred = _video(4, default=1)
    selected, _details = select_video_stream([first, preferred], enabled=True)
    assert selected["index"] == 4

    preferred["disposition"]["default"] = 0
    selected, _details = select_video_stream([first, preferred], enabled=True)
    assert selected["index"] == 3


def test_dependent_video_structure_is_not_guessed() -> None:
    with pytest.raises(VideoSelectionError, match="estructura especial"):
        select_video_stream(
            [_video(0), _video(1, dependent=1)],
            enabled=True,
        )


def test_movie_detector_and_plan_use_the_selected_track(monkeypatch) -> None:
    defaults = json.loads(reglas.DEFAULT_PATH.read_text(encoding="utf-8"))
    active = deepcopy(defaults)
    active["video"]["seleccionar_mejor_si_hay_varias"] = True
    snapshot = reglas.RulesSnapshot(active, reglas._fingerprint(active))
    main = _video(0, width=3840, height=1600, default=1)
    secondary = _video(1, width=1248, height=520, default=1)
    payload = {
        "format": {"duration": "4336.584"},
        "streams": [
            main,
            secondary,
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "ac3",
                "channels": 6,
                "bit_rate": "640000",
                "tags": {"language": "spa"},
                "disposition": {"default": 1},
            },
        ],
    }
    monkeypatch.setattr(
        detector,
        "run",
        lambda _command, timeout=120: (0, json.dumps(payload), ""),
    )

    with reglas.usar_reglas(snapshot):
        analysis = detector.analizar_archivo(Path("episodio.mkv"))
        plan = planificador.crear_plan(analysis)

    assert analysis["estado"] == "APTO PARA PROCESO AUTOMATICO"
    assert analysis["videos"][0]["index"] == 0
    assert analysis["video_selection"]["discarded_indexes"] == [1]
    assert plan["estado"] == "PLAN APTO"
    assert plan["video"]["index"] == 0
    assert "-map 0:0" in plan["comando"]
