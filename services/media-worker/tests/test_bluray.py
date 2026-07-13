import json
import subprocess
from pathlib import Path

import pytest

from media_worker import bluray, core, server


def probe_payload(
    duration=5400.0,
    videos=1,
    audios=2,
    subtitles=1,
    chapters=8,
):
    streams = []
    for index in range(videos):
        streams.append({"index": index, "id": f"0x{0x1011 + index:04x}", "codec_type": "video", "codec_name": "h264"})
    for index in range(audios):
        streams.append(
            {
                "index": 10 + index,
                "id": f"0x{0x1100 + index:04x}",
                "codec_type": "audio",
                "codec_name": "dts",
                "channels": 6,
            }
        )
    for index in range(subtitles):
        streams.append(
            {
                "index": 20 + index,
                "id": f"0x{0x1200 + index:04x}",
                "codec_type": "subtitle",
                "codec_name": "hdmv_pgs_subtitle",
            }
        )
    return {
        "format": {"duration": str(duration)},
        "streams": streams,
        "chapters": [
            {"id": index, "start_time": str(index * 600), "end_time": str(min(duration, (index + 1) * 600))}
            for index in range(chapters)
        ],
    }


class FakeRunner:
    def __init__(
        self,
        playlists=None,
        output_probe=None,
        remux_code=0,
        output_probe_code=0,
        read_code=0,
        create_output=True,
    ):
        self.playlists = playlists or {1: probe_payload()}
        self.output_probe = output_probe or probe_payload()
        self.remux_code = remux_code
        self.output_probe_code = output_probe_code
        self.read_code = read_code
        self.create_output = create_output
        self.calls = []

    def __call__(self, argv, _timeout_seconds):
        self.calls.append(list(argv))
        if argv[0] == "ffprobe" and str(argv[-1]).startswith("bluray:"):
            playlist_id = int(argv[argv.index("-playlist") + 1])
            payload = self.playlists.get(playlist_id)
            if isinstance(payload, tuple):
                return subprocess.CompletedProcess(argv, payload[0], payload[1], payload[2])
            if payload is None:
                return subprocess.CompletedProcess(argv, 1, "", "playlist unreadable")
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        if argv[0] == "ffmpeg" and "-playlist" in argv:
            output = Path(argv[-1])
            if self.create_output:
                output.write_bytes(b"x" * (bluray.MIN_REMUX_SIZE_BYTES + 1))
            return subprocess.CompletedProcess(argv, self.remux_code, "remux stdout", "remux stderr")
        if argv[0] == "ffprobe":
            return subprocess.CompletedProcess(
                argv,
                self.output_probe_code,
                json.dumps(self.output_probe) if self.output_probe_code == 0 else "",
                "probe failed" if self.output_probe_code else "",
            )
        if argv[0] == "ffmpeg" and "-xerror" in argv:
            return subprocess.CompletedProcess(
                argv,
                self.read_code,
                "",
                "truncated" if self.read_code else "",
            )
        raise AssertionError(f"Orden inesperada: {argv}")


def make_bluray(root: Path, playlists=(1,), certificate=True) -> Path:
    playlist_dir = root / "BDMV" / "PLAYLIST"
    stream_dir = root / "BDMV" / "STREAM"
    playlist_dir.mkdir(parents=True)
    stream_dir.mkdir(parents=True)
    for playlist_id in playlists:
        (playlist_dir / f"{playlist_id:05d}.mpls").write_bytes(b"playlist")
    (stream_dir / "00001.m2ts").write_bytes(b"stream")
    if certificate:
        (root / "CERTIFICATE").mkdir()
    return root


def mpls_stream_record(pid, codec_type, language):
    stream_entry = bytes([1]) + int(pid).to_bytes(2, "big") + (b"\x00" * 6)
    coding_type = 0x86 if codec_type == "audio" else 0x90
    coding_info = (
        bytes([coding_type, 0x61]) + language.encode("ascii")
        if codec_type == "audio"
        else bytes([coding_type]) + language.encode("ascii")
    )
    return bytes([len(stream_entry)]) + stream_entry + bytes([len(coding_info)]) + coding_info


def write_mpls_languages(path, entries):
    path.write_bytes(
        b"MPLS0200"
        + b"\x00" * 24
        + b"".join(mpls_stream_record(*entry) for entry in entries)
    )


def test_detecta_bdmv_valido_y_certificate_opcional(tmp_path):
    with_certificate = make_bluray(tmp_path / "con-certificado")
    without_certificate = make_bluray(tmp_path / "sin-certificado", certificate=False)
    assert bluray.is_full_bluray_folder(with_certificate)
    assert bluray.is_full_bluray_folder(without_certificate)


def test_no_detecta_m2ts_suelto(tmp_path):
    (tmp_path / "pelicula.m2ts").write_bytes(b"video")
    assert not bluray.is_full_bluray_folder(tmp_path)
    assert bluray.find_full_bluray_folders(tmp_path) == []


def test_localiza_bdmv_en_unica_subcarpeta_con_sidecars(tmp_path):
    release = make_bluray(tmp_path / "Release")
    (tmp_path / "info.nfo").write_text("sidecar", encoding="utf-8")
    assert bluray.find_full_bluray_folder(tmp_path) == release


def test_selecciona_unica_playlist_valida(tmp_path):
    root = make_bluray(tmp_path / "Release", playlists=(1,))
    result = bluray.select_main_playlist(root, runner=FakeRunner({1: probe_payload(5400)}))
    assert result["status"] == "selected"
    assert result["selected"]["playlist_id"] == 1


def test_descarta_playlist_corta_y_elige_pelicula(tmp_path):
    root = make_bluray(tmp_path / "Release", playlists=(1, 2))
    runner = FakeRunner({1: probe_payload(600), 2: probe_payload(5400)})
    result = bluray.select_main_playlist(root, runner=runner)
    assert result["status"] == "selected"
    assert result["selected"]["playlist_id"] == 2
    assert result["valid_candidates"] == 1


def test_elige_pelicula_frente_a_extra_largo(tmp_path):
    root = make_bluray(tmp_path / "Release", playlists=(1, 2))
    runner = FakeRunner({1: probe_payload(5400), 2: probe_payload(3000)})
    result = bluray.select_main_playlist(root, runner=runner)
    assert result["status"] == "selected"
    assert result["selected"]["playlist_id"] == 1


def test_marca_ambiguedad_con_dos_cortes_cercanos(tmp_path):
    root = make_bluray(tmp_path / "Release", playlists=(1, 2))
    runner = FakeRunner({1: probe_payload(7200), 2: probe_payload(7100)})
    result = bluray.select_main_playlist(root, runner=runner)
    assert result["status"] == "ambiguous"
    assert result["duration_difference_seconds"] == 100


def test_playlist_first_no_inspecciona_el_m2ts_mas_grande(tmp_path):
    root = make_bluray(tmp_path / "Release", playlists=(1,))
    (root / "BDMV" / "STREAM" / "99999.m2ts").write_bytes(b"x" * 5000)
    runner = FakeRunner({1: probe_payload(5400)})
    bluray.select_main_playlist(root, runner=runner)
    assert all(".m2ts" not in " ".join(call) for call in runner.calls)


def test_lee_idiomas_de_playlist_mpls_por_pid(tmp_path):
    root = make_bluray(tmp_path / "Release", playlists=(1,))
    playlist = root / "BDMV" / "PLAYLIST" / "00001.mpls"
    write_mpls_languages(
        playlist,
        [
            (0x1100, "audio", "eng"),
            (0x1101, "audio", "spa"),
            (0x1200, "subtitle", "eng"),
            (0x1201, "subtitle", "spa"),
        ],
    )
    summary = bluray._probe_summary(probe_payload(audios=2, subtitles=2))
    result = bluray.read_playlist_stream_languages(playlist, summary["streams"])
    assert result["status"] == "parsed"
    assert result["conflicts"] == []
    assert [(item["pid"], item["language"]) for item in result["streams"]] == [
        ("0x1100", "eng"),
        ("0x1101", "spa"),
        ("0x1200", "eng"),
        ("0x1201", "spa"),
    ]


def test_no_etiqueta_pid_mpls_con_idiomas_conflictivos(tmp_path):
    root = make_bluray(tmp_path / "Release", playlists=(1,))
    playlist = root / "BDMV" / "PLAYLIST" / "00001.mpls"
    write_mpls_languages(
        playlist,
        [(0x1100, "audio", "eng"), (0x1100, "audio", "spa")],
    )
    summary = bluray._probe_summary(probe_payload(audios=1, subtitles=0))
    result = bluray.read_playlist_stream_languages(playlist, summary["streams"])
    assert result["status"] == "no_languages"
    assert result["streams"] == []
    assert result["conflicts"] == [
        {"pid": "0x1100", "languages": ["eng", "spa"]}
    ]


def test_orden_ffmpeg_usa_temporal_stream_copy_y_mapas(tmp_path):
    root = make_bluray(tmp_path / "Release")
    temporary = root / "Release.arr-bluray.tmp.mkv"
    command = bluray.build_remux_command(root, 1, temporary)
    assert command[0] == "ffmpeg"
    assert command[command.index("-playlist") + 1] == "1"
    assert command[command.index("-c") + 1] == "copy"
    assert "0:v?" in command
    assert "0:a?" in command
    assert "0:s?" in command
    assert command[command.index("-map_chapters") + 1] == "0"
    assert command[-1].endswith(".arr-bluray.tmp.mkv")


def test_orden_ffmpeg_escribe_idiomas_en_audio_y_subtitulos(tmp_path):
    root = make_bluray(tmp_path / "Release")
    temporary = root / "Release.arr-bluray.tmp.mkv"
    languages = [
        {"codec_type": "audio", "type_index": 0, "pid": "0x1100", "language": "eng"},
        {"codec_type": "audio", "type_index": 1, "pid": "0x1101", "language": "spa"},
        {"codec_type": "subtitle", "type_index": 0, "pid": "0x1200", "language": "spa"},
    ]
    command = bluray.build_remux_command(root, 1, temporary, languages)
    assert [
        "-metadata:s:a:0",
        "language=eng",
        "-metadata:s:a:1",
        "language=spa",
        "-metadata:s:s:0",
        "language=spa",
    ] == command[command.index("-metadata:s:a:0") : -1]


@pytest.mark.parametrize(
    "output_probe,probe_code,expected_problem",
    [
        (probe_payload(videos=0), 0, "video_stream_missing"),
        (probe_payload(), 1, "ffprobe_failed"),
        (probe_payload(duration=4000), 0, "duration_mismatch"),
    ],
)
def test_verificador_rechaza_salida_invalida(tmp_path, output_probe, probe_code, expected_problem):
    output = tmp_path / "temp.mkv"
    output.write_bytes(b"x" * (bluray.MIN_REMUX_SIZE_BYTES + 1))
    runner = FakeRunner(output_probe=output_probe, output_probe_code=probe_code)
    result = bluray.verify_bluray_remux(output, 5400, 8, runner=runner)
    assert result["status"] == "verification_failed"
    assert expected_problem in result["problems"]


def test_verificador_lee_hasta_el_final(tmp_path):
    output = tmp_path / "temp.mkv"
    output.write_bytes(b"x" * (bluray.MIN_REMUX_SIZE_BYTES + 1))
    result = bluray.verify_bluray_remux(output, 5400, 8, runner=FakeRunner(read_code=1))
    assert result["status"] == "verification_failed"
    assert "full_read_failed" in result["problems"]


def test_verificador_exige_los_idiomas_declarados_en_mpls(tmp_path):
    output = tmp_path / "temp.mkv"
    output.write_bytes(b"x" * (bluray.MIN_REMUX_SIZE_BYTES + 1))
    expected = [
        {"codec_type": "audio", "type_index": 0, "pid": "0x1100", "language": "eng"},
        {"codec_type": "subtitle", "type_index": 0, "pid": "0x1200", "language": "spa"},
    ]
    missing = bluray.verify_bluray_remux(
        output,
        5400,
        8,
        expected_stream_languages=expected,
        runner=FakeRunner(output_probe=probe_payload(audios=1, subtitles=1)),
    )
    assert missing["status"] == "verification_failed"
    assert missing["language_check"] == "mismatch"
    assert "stream_language_mismatch" in missing["problems"]

    tagged_probe = probe_payload(audios=1, subtitles=1)
    tagged_probe["streams"][1]["tags"] = {"language": "eng"}
    tagged_probe["streams"][2]["tags"] = {"language": "spa"}
    tagged = bluray.verify_bluray_remux(
        output,
        5400,
        8,
        expected_stream_languages=expected,
        runner=FakeRunner(output_probe=tagged_probe),
    )
    assert tagged["status"] == "verification_passed"
    assert tagged["language_check"] == "preserved"
    assert all(item["passed"] for item in tagged["language_checks"])


def test_normalizacion_exitosa_conserva_sidecars_y_emite_eventos(tmp_path):
    release = make_bluray(tmp_path / "Release", playlists=(1,))
    (release / "info.nfo").write_text("nfo", encoding="utf-8")
    (release / "poster.jpg").write_bytes(b"image")
    events = []
    result = bluray.normalize_bluray_folder(
        release,
        runner=FakeRunner({1: probe_payload()}),
        event_callback=lambda *event: events.append(event),
    )
    assert result["status"] == "normalized"
    assert (release / "Release.mkv").exists()
    assert not (release / "BDMV").exists()
    assert not (release / "CERTIFICATE").exists()
    assert (release / "info.nfo").exists()
    assert (release / "poster.jpg").exists()
    assert release.exists()
    assert [path.name for path in release.glob("*.mkv")] == ["Release.mkv"]
    assert result["remux"]["command_preview"]["stream_copy"] is True
    assert result["verification"]["chapter_check"] == "preserved"
    names = {event[0] for event in events}
    assert {
        "bluray_detected",
        "bluray_playlists_scanned",
        "bluray_playlist_selected",
        "bluray_remux_started",
        "bluray_remux_finished",
        "bluray_verification_started",
        "bluray_verification_passed",
        "bluray_source_removed",
        "bluray_normalization_completed",
    } <= names


def test_normalizacion_mpls_conserva_idiomas_antes_de_borrar(tmp_path):
    release = make_bluray(tmp_path / "Release", playlists=(1,))
    write_mpls_languages(
        release / "BDMV" / "PLAYLIST" / "00001.mpls",
        [(0x1100, "audio", "eng"), (0x1200, "subtitle", "spa")],
    )
    source_probe = probe_payload(audios=1, subtitles=1)
    output_probe = probe_payload(audios=1, subtitles=1)
    output_probe["streams"][1]["tags"] = {"language": "eng"}
    output_probe["streams"][2]["tags"] = {"language": "spa"}
    runner = FakeRunner(playlists={1: source_probe}, output_probe=output_probe)

    result = bluray.normalize_bluray_folder(release, runner=runner)

    assert result["status"] == "normalized"
    assert result["verification"]["language_check"] == "preserved"
    assert not (release / "BDMV").exists()
    remux_call = next(call for call in runner.calls if call[0] == "ffmpeg" and "-playlist" in call)
    assert "-metadata:s:a:0" in remux_call
    assert "language=eng" in remux_call
    assert "-metadata:s:s:0" in remux_call
    assert "language=spa" in remux_call


def test_normalizacion_no_borra_bdmv_si_el_mkv_pierde_idiomas(tmp_path):
    release = make_bluray(tmp_path / "Release", playlists=(1,))
    write_mpls_languages(
        release / "BDMV" / "PLAYLIST" / "00001.mpls",
        [(0x1100, "audio", "eng"), (0x1200, "subtitle", "spa")],
    )
    runner = FakeRunner(
        playlists={1: probe_payload(audios=1, subtitles=1)},
        output_probe=probe_payload(audios=1, subtitles=1),
    )

    result = bluray.normalize_bluray_folder(release, runner=runner)

    assert result["status"] == "verification_failed"
    assert result["verification"]["language_check"] == "mismatch"
    assert (release / "BDMV").exists()
    assert (release / "CERTIFICATE").exists()
    assert not (release / "Release.mkv").exists()


def test_no_elimina_origen_si_ffmpeg_falla(tmp_path):
    release = make_bluray(tmp_path / "Release")
    result = bluray.normalize_bluray_folder(release, runner=FakeRunner(remux_code=1))
    assert result["status"] == "remux_failed"
    assert (release / "BDMV").exists()
    assert (release / "CERTIFICATE").exists()
    assert not (release / "Release.mkv").exists()


def test_no_elimina_origen_si_ffprobe_falla(tmp_path):
    release = make_bluray(tmp_path / "Release")
    result = bluray.normalize_bluray_folder(release, runner=FakeRunner(output_probe_code=1))
    assert result["status"] == "verification_failed"
    assert (release / "BDMV").exists()
    assert (release / "CERTIFICATE").exists()
    assert not (release / "Release.mkv").exists()


def test_no_sobrescribe_mkv_definitivo_preexistente(tmp_path):
    release = make_bluray(tmp_path / "Release")
    existing = release / "Release.mkv"
    existing.write_bytes(b"existing")
    result = bluray.normalize_bluray_folder(release, runner=FakeRunner())
    assert result["status"] == "finalization_failed"
    assert existing.read_bytes() == b"existing"
    assert (release / "BDMV").exists()


def test_interrupcion_con_temporal_incompleto_conserva_bdmv(tmp_path):
    release = make_bluray(tmp_path / "Release")
    temporary = release / "Release.arr-bluray.tmp.mkv"
    temporary.write_bytes(b"partial")
    result = bluray.normalize_bluray_folder(release, runner=FakeRunner())
    assert result["status"] == "remux_failed"
    assert result["reason"] == "temporary_output_exists"
    assert temporary.read_bytes() == b"partial"
    assert (release / "BDMV").exists()


def test_verificacion_exige_capitulos_si_el_origen_los_tenia(tmp_path):
    output = tmp_path / "temp.mkv"
    output.write_bytes(b"x" * (bluray.MIN_REMUX_SIZE_BYTES + 1))
    runner = FakeRunner(output_probe=probe_payload(chapters=0))
    result = bluray.verify_bluray_remux(output, 5400, 8, runner=runner)
    assert result["status"] == "verification_failed"
    assert "chapters_missing" in result["problems"]


def test_verificacion_documenta_si_ffprobe_no_expone_capitulos(tmp_path):
    output = tmp_path / "temp.mkv"
    output.write_bytes(b"x" * (bluray.MIN_REMUX_SIZE_BYTES + 1))
    runner = FakeRunner(output_probe=probe_payload(chapters=0))
    result = bluray.verify_bluray_remux(output, 5400, 0, runner=runner)
    assert result["status"] == "verification_passed"
    assert result["chapter_check"] == "not_enforced_source_ffprobe_reported_zero"


def test_no_elimina_origen_si_falla_renombrado_final(tmp_path, monkeypatch):
    release = make_bluray(tmp_path / "Release")
    original_rename = Path.rename

    def fail_final_rename(self, target):
        if self.name.endswith(".arr-bluray.tmp.mkv"):
            raise OSError("rename blocked")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_final_rename)
    result = bluray.normalize_bluray_folder(release, runner=FakeRunner())
    assert result["status"] == "finalization_failed"
    assert (release / "BDMV").exists()
    assert (release / "CERTIFICATE").exists()
    assert not (release / "Release.mkv").exists()


def test_fallo_de_borrado_restaura_nombres_y_no_deja_mkv_definitivo(tmp_path):
    release = make_bluray(tmp_path / "Release")

    def fail_remove(_path):
        raise PermissionError("delete blocked")

    result = bluray.normalize_bluray_folder(
        release,
        runner=FakeRunner(),
        remove_tree=fail_remove,
    )
    assert result["status"] == "finalization_failed"
    assert (release / "BDMV").exists()
    assert (release / "CERTIFICATE").exists()
    assert not (release / "Release.mkv").exists()
    assert (release / "Release.arr-bluray.tmp.mkv").exists()


def _patch_movie_success(monkeypatch, source: Path):
    clean = source / "clean.limpio.mkv"

    monkeypatch.setattr(core, "_build_plan", lambda _video: ({"estado": "ok"}, {"estado": "PLAN APTO"}))

    def process(_plan):
        clean.write_bytes(b"clean")
        return {"ok": True, "salida": str(clean), "returncode": 0, "tamano_salida": 5}

    monkeypatch.setattr(core.procesador, "ejecutar_ffmpeg", process)
    monkeypatch.setattr(core.verificador, "verificar_archivo", lambda _path: {"estado": "LIMPIO OK", "pistas": []})
    monkeypatch.setattr(
        core,
        "_finalize_movie",
        lambda _source, _video, _clean, final_root: {
            "final_dir": str(final_root / source.name),
            "final_video": str(final_root / source.name / "movie.mkv"),
            "final_srt": "",
        },
    )


def _movie_payload(tmp_path: Path, source: Path):
    return {
        "job_id": "job-bluray",
        "source_path": str(source),
        "final_root": str(tmp_path / "movies"),
        "review_root": str(tmp_path / "review"),
        "reports_root": str(tmp_path / "reports"),
        "callback_url": "",
    }


def test_process_movie_normaliza_antes_de_contar_videos(tmp_path, monkeypatch):
    source = make_bluray(tmp_path / "Release")
    normalized = source / "Release.mkv"

    def fake_normalize(_payload):
        normalized.write_bytes(b"movie")
        return {"status": "normalized", "result_file": str(normalized)}

    monkeypatch.setattr(core, "normalize_bluray", fake_normalize)
    monkeypatch.setattr(core, "_video_files", lambda _source: (_ for _ in ()).throw(AssertionError("conteo prematuro")))
    _patch_movie_success(monkeypatch, source)
    result = core.process_movie(_movie_payload(tmp_path, source))
    assert result["status"] == "done"


@pytest.mark.parametrize("extension", ["mkv", "mp4"])
def test_process_movie_normal_no_cambia_el_conteo_existente(tmp_path, monkeypatch, extension):
    source = tmp_path / "Release"
    source.mkdir()
    video = source / f"movie.{extension}"
    video.write_bytes(b"movie")
    calls = []
    monkeypatch.setattr(
        core,
        "normalize_bluray",
        lambda _payload: (_ for _ in ()).throw(AssertionError("no debe llamar al normalizador")),
    )
    monkeypatch.setattr(core, "_video_files", lambda folder: calls.append(folder) or [video])
    _patch_movie_success(monkeypatch, source)
    result = core.process_movie(_movie_payload(tmp_path, source))
    assert result["status"] == "done"
    assert calls == [source]


@pytest.mark.parametrize("status", ["ambiguous", "remux_failed", "verification_failed"])
def test_process_movie_fallo_bluray_conserva_origen_en_revision(tmp_path, monkeypatch, status):
    source = make_bluray(tmp_path / f"Release-{status}")
    monkeypatch.setattr(
        core,
        "normalize_bluray",
        lambda _payload: {"status": status, "reason": "controlled failure", "source_removed": False},
    )
    result = core.process_movie(_movie_payload(tmp_path, source))
    review = Path(result["review_path"])
    assert result["status"] == "review"
    assert (review / "BDMV").exists()
    assert (review / "CERTIFICATE").exists()


def test_endpoint_normalize_bluray_valida_raiz_permitida(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    source = make_bluray(allowed / "Release")
    monkeypatch.setenv("MEDIA_WORKER_ALLOWED_ROOTS", str(allowed))
    payload = server._validated_normalize_payload({"job_id": "job", "source_path": str(source)})
    assert Path(payload["source_path"]) == source.resolve()
    outside = make_bluray(tmp_path / "outside" / "Release")
    with pytest.raises(ValueError, match="fuera"):
        server._validated_normalize_payload({"job_id": "job", "source_path": str(outside)})
