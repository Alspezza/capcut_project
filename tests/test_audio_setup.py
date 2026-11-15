import json
from pathlib import Path
from unittest.mock import patch

import pytest

import setup_audio


def _make_sample_draft(video_durations=(5_000_000, 7_000_000), voice_name="Voice.ru.mp3"):
    segs = []
    cur = 0
    for idx, dur in enumerate(video_durations):
        segs.append({
            "id": f"VSEG_{idx}",
            "material_id": f"VMAT_{idx}",
            "target_timerange": {"start": cur, "duration": dur},
            "extra_material_refs": [],
        })
        cur += dur

    return {
        "duration": cur,
        "materials": {
            "videos": [{"id": f"VMAT_{i}", "duration": d} for i, d in enumerate(video_durations)],
            "audios": [
                {
                    "id": "AMAT_VOICE",
                    "name": voice_name,
                    "path": "C:/fake/Voice.ru.mp3",
                    "duration": cur,
                }
            ],
            "loudnesses": [],
            "material_animations": [],
        },
        "tracks": [
            {"id": "VTRACK_0", "type": "video", "segments": segs},
            {
                "id": "ATRACK_VOICE",
                "type": "audio",
                "segments": [
                    {
                        "id": "ASEG_VOICE",
                        "material_id": "AMAT_VOICE",
                        "target_timerange": {"start": 0, "duration": cur},
                        "extra_material_refs": [],
                    }
                ],
            },
        ],
    }


def test_db_to_linear():
    assert setup_audio.db_to_linear(-7.0) == pytest.approx(10 ** (-7.0 / 20.0), rel=1e-6)
    assert setup_audio.db_to_linear(0.0) == pytest.approx(1.0)


def test_add_intro_and_ambient_placement_and_fades(tmp_path):
    draft = _make_sample_draft(video_durations=(10_000_000, 10_000_000))  # 20s total
    intro = tmp_path / "intro.mp3"
    ambient = tmp_path / "ambient.mp3"
    intro.write_bytes(b"fake")
    ambient.write_bytes(b"fake")

    durations = {intro: 6_000_000, ambient: 60_000_000}

    with patch("setup_audio.probe_audio_duration_us", side_effect=lambda p: durations[p]):
        res = setup_audio.add_intro_and_ambient(draft, intro, ambient, volume_db=-7.0, fade_us=4_000_000)

    assert res["video_duration_us"] == 20_000_000
    assert res["intro"] == {"start_us": 0, "duration_us": 6_000_000, "fade_in_us": 4_000_000}
    assert res["ambient"] == {"start_us": 6_000_000, "duration_us": 14_000_000, "fade_out_us": 4_000_000}

    auto_tracks = [t for t in draft["tracks"] if t.get("name") == setup_audio.AUTO_AUDIO_TRACK_NAME]
    assert len(auto_tracks) == 1
    segs = auto_tracks[0]["segments"]
    assert len(segs) == 2

    expected_vol = setup_audio.db_to_linear(-7.0)
    assert segs[0]["volume"] == pytest.approx(expected_vol)
    assert segs[1]["volume"] == pytest.approx(expected_vol)
    assert segs[0]["target_timerange"] == {"start": 0, "duration": 6_000_000}
    assert segs[1]["target_timerange"] == {"start": 6_000_000, "duration": 14_000_000}

    fades = {f["id"]: f for f in draft["materials"]["audio_fades"]}
    intro_fade = next(fades[r] for r in segs[0]["extra_material_refs"] if r in fades)
    ambient_fade = next(fades[r] for r in segs[1]["extra_material_refs"] if r in fades)
    assert intro_fade["fade_in_duration"] == 4_000_000
    assert intro_fade["fade_out_duration"] == 0
    assert ambient_fade["fade_in_duration"] == 0
    assert ambient_fade["fade_out_duration"] == 4_000_000

    # Повторный запуск не должен дублировать сегменты на авто-дорожке
    with patch("setup_audio.probe_audio_duration_us", side_effect=lambda p: durations[p]):
        setup_audio.add_intro_and_ambient(draft, intro, ambient, volume_db=-7.0, fade_us=4_000_000)
    assert len(auto_tracks[0]["segments"]) == 2


def test_enable_voice_normalization():
    draft = _make_sample_draft()
    fake_measured = {"avg_loudness": -18.5, "peak_loudness": -1.2}
    with patch("setup_audio.measure_loudness_ebur128", return_value=fake_measured):
        updated = setup_audio.enable_voice_normalization(draft, voice_pattern="voice.ru", target_lufs=-23.0)

    assert len(updated) == 1
    assert updated[0]["target_lufs"] == -23.0
    assert updated[0]["measured"] == fake_measured

    louds = draft["materials"]["loudnesses"]
    assert len(louds) == 1
    assert louds[0]["enable"] is True
    assert louds[0]["target_loudness"] == -23.0
    assert louds[0]["loudness_param"] == fake_measured
    voice_seg = draft["tracks"][1]["segments"][0]
    assert louds[0]["id"] in voice_seg["extra_material_refs"]


def test_apply_last_clip_fade_out():
    draft = _make_sample_draft(video_durations=(5_000_000, 7_000_000))
    res = setup_audio.apply_last_clip_fade_out(draft, fade_us=4_000_000)
    assert res == {"seg_duration_us": 7_000_000, "start_us": 3_000_000, "duration_us": 4_000_000}

    last_seg = draft["tracks"][0]["segments"][-1]
    anims = {a["id"]: a for a in draft["materials"]["material_animations"]}
    linked = [anims[r] for r in last_seg["extra_material_refs"] if r in anims]
    assert len(linked) == 1
    out_anim = linked[0]["animations"][0]
    assert out_anim["id"] == setup_audio.FADE_OUT_EFFECT_ID
    assert out_anim["type"] == "out"
    assert out_anim["start"] == 3_000_000
    assert out_anim["duration"] == 4_000_000


def test_process_project_end_to_end(tmp_path):
    proj_dir = tmp_path / "Proj"
    proj_dir.mkdir()
    draft_file = proj_dir / "draft_content.json"
    draft_file.write_text(json.dumps(_make_sample_draft((6_000_000, 8_000_000))), encoding="utf-8")

    intro = tmp_path / "intro.mp3"
    ambient = tmp_path / "ambient.mp3"
    intro.write_bytes(b"x")
    ambient.write_bytes(b"y")

    with (
        patch("setup_audio.capcut_is_running", return_value=False),
        patch("setup_audio.probe_audio_duration_us", side_effect=lambda p: 5_000_000 if p == intro else 30_000_000),
        patch("setup_audio.measure_loudness_ebur128", return_value={"avg_loudness": -19.0, "peak_loudness": -2.0}),
    ):
        res = setup_audio.process_project(
            proj_dir,
            intro_path=intro,
            ambient_path=ambient,
            voice_pattern="voice",
            volume_db=-7.0,
            fade_sec=4.0,
            dry_run=False,
        )

    assert Path(res["backup"]).is_file()
    saved = json.loads(draft_file.read_text(encoding="utf-8"))
    assert len(saved["tracks"]) == 3
    assert len(saved["materials"]["loudnesses"]) == 1
    assert saved["materials"]["loudnesses"][0]["enable"] is True
    assert len(saved["materials"]["material_animations"]) == 1


def test_rerun_cleans_orphaned_materials(tmp_path):
    draft = _make_sample_draft(video_durations=(10_000_000,))
    intro = tmp_path / "intro.mp3"
    ambient = tmp_path / "ambient.mp3"
    intro.write_bytes(b"a")
    ambient.write_bytes(b"b")
    durations = {intro: 4_000_000, ambient: 20_000_000}

    with patch("setup_audio.probe_audio_duration_us", side_effect=lambda p: durations[p]):
        setup_audio.add_intro_and_ambient(draft, intro, ambient)
        assert len(draft["materials"]["audios"]) == 3  # voice + intro + ambient
        assert len(draft["materials"]["audio_fades"]) == 2

        setup_audio.add_intro_and_ambient(draft, intro, ambient)
        assert len(draft["materials"]["audios"]) == 3
        assert len(draft["materials"]["audio_fades"]) == 2


def test_intro_longer_than_video_skips_ambient(tmp_path):
    draft = _make_sample_draft(video_durations=(5_000_000,))
    intro = tmp_path / "intro.mp3"
    ambient = tmp_path / "ambient.mp3"
    intro.write_bytes(b"a")
    ambient.write_bytes(b"b")

    with patch("setup_audio.probe_audio_duration_us", side_effect=lambda p: 12_000_000 if p == intro else 30_000_000):
        res = setup_audio.add_intro_and_ambient(draft, intro, ambient)

    assert res["intro"] == {"start_us": 0, "duration_us": 5_000_000, "fade_in_us": 4_000_000}
    assert res["ambient"] is None


def test_dry_run_and_capcut_running_guard(tmp_path):
    proj_dir = tmp_path / "Proj"
    proj_dir.mkdir()
    draft_file = proj_dir / "draft_content.json"
    original = json.dumps(_make_sample_draft((6_000_000,)))
    draft_file.write_text(original, encoding="utf-8")

    with patch("setup_audio.capcut_is_running", return_value=True):
        with pytest.raises(setup_audio.DraftError):
            setup_audio.process_project(proj_dir, dry_run=False)

        # dry_run=True не падает даже если CapCut запущен и не меняет файл
        res = setup_audio.process_project(proj_dir, dry_run=True, normalize_voice=False)
        assert "backup" not in res
        assert draft_file.read_text(encoding="utf-8") == original


def test_gui_dispatch_worker_audio(monkeypatch):
    import sys
    import gui

    called = []
    monkeypatch.setattr(gui.setup_audio, "main", lambda: called.append(sys.argv[:]))
    monkeypatch.setattr(sys, "argv", ["gui.py", "--run-worker", "audio", "proj_dir", "--dry-run"])
    assert gui._dispatch_worker() is True
    assert called == [["gui.py", "proj_dir", "--dry-run"]]
