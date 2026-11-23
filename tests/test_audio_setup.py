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
            "videos": [
                {"id": f"VMAT_{i}", "duration": d, "material_name": f"{i + 1}.mp4"}
                for i, d in enumerate(video_durations)
            ],
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


def _prompts_file(tmp_path, blocks):
    path = tmp_path / "prompts.txt"
    path.write_text(
        "\n".join(
            f"[PROMPT #{num}]\nFrame description {num}.\n[{script}]\n" for num, script in blocks
        ),
        encoding="utf-8",
    )
    return path


def test_db_to_linear():
    assert setup_audio.db_to_linear(-7.0) == pytest.approx(10 ** (-7.0 / 20.0), rel=1e-6)
    assert setup_audio.db_to_linear(0.0) == pytest.approx(1.0)


def test_add_intro_and_ambient_placement_and_fades(tmp_path):
    draft = _make_sample_draft(video_durations=(10_000_000, 10_000_000))  # 20s total
    intro = tmp_path / "intro.mp3"
    ambient = tmp_path / "ambient.mp3"
    intro.write_bytes(b"fake")
    ambient.write_bytes(b"fake")

    # 12 c intro, последние 6 c не используем -> 6 c звука
    durations = {intro: 12_000_000, ambient: 60_000_000}

    with patch("setup_audio.probe_audio_duration_us", side_effect=lambda p: durations[p]):
        res = setup_audio.add_intro_and_ambient(draft, intro, ambient, volume_db=-7.0, fade_us=4_000_000)

    assert res["video_duration_us"] == 20_000_000
    assert res["intro"] == {
        "start_us": 0,
        "duration_us": 6_000_000,
        "source_start_us": 0,
        "source_duration_us": 6_000_000,
        "speed": 1.0,
        "fade_in_us": 0,  # в начале интро не должно быть «нарастания»
        "fade_out_us": 4_500_000,
        "trimmed_tail_us": 6_000_000,
    }
    assert res["ambient"] == {
        "start_us": 6_000_000,
        "duration_us": 14_000_000,
        "fade_in_us": 0,
        "fade_out_us": 4_000_000,
    }

    auto_tracks = [t for t in draft["tracks"] if t.get("name") == setup_audio.AUTO_AUDIO_TRACK_NAME]
    assert len(auto_tracks) == 1
    segs = auto_tracks[0]["segments"]
    assert len(segs) == 2

    expected_vol = setup_audio.db_to_linear(-7.0)
    assert segs[0]["volume"] == pytest.approx(expected_vol)
    assert segs[1]["volume"] == pytest.approx(expected_vol)
    assert segs[0]["target_timerange"] == {"start": 0, "duration": 6_000_000}
    assert segs[0]["source_timerange"] == {"start": 0, "duration": 6_000_000}
    assert segs[1]["target_timerange"] == {"start": 6_000_000, "duration": 14_000_000}

    fades = {f["id"]: f for f in draft["materials"]["audio_fades"]}
    intro_fade = next(fades[r] for r in segs[0]["extra_material_refs"] if r in fades)
    ambient_fade = next(fades[r] for r in segs[1]["extra_material_refs"] if r in fades)
    assert intro_fade["fade_in_duration"] == 0
    assert intro_fade["fade_out_duration"] == 4_500_000
    assert ambient_fade["fade_in_duration"] == 0
    assert ambient_fade["fade_out_duration"] == 4_000_000

    # Повторный запуск не должен дублировать сегменты на авто-дорожке
    with patch("setup_audio.probe_audio_duration_us", side_effect=lambda p: durations[p]):
        setup_audio.add_intro_and_ambient(draft, intro, ambient, volume_db=-7.0, fade_us=4_000_000)
    assert len(auto_tracks[0]["segments"]) == 2


def test_intro_is_end_anchored_and_trimmed_from_end(tmp_path):
    draft = _make_sample_draft(video_durations=(10_000_000, 10_000_000))
    intro = tmp_path / "intro.mp3"
    ambient = tmp_path / "ambient.mp3"
    intro.write_bytes(b"a")
    ambient.write_bytes(b"b")

    durations = {intro: 20_000_000, ambient: 60_000_000}  # 14 c после отрезания хвоста
    with patch("setup_audio.probe_audio_duration_us", side_effect=lambda p: durations[p]):
        res = setup_audio.add_intro_and_ambient(
            draft, intro, ambient, intro_anchor_us=8_000_000, fade_us=4_000_000
        )

    # Конец интро ровно на 8 c: играет с начала (0..8 c), начало не обрезается
    assert res["intro"]["start_us"] == 0
    assert res["intro"]["duration_us"] == 8_000_000
    assert res["intro"]["source_start_us"] == 0
    assert res["intro"]["source_duration_us"] == 8_000_000
    assert res["ambient"]["start_us"] == 8_000_000
    assert res["ambient"]["duration_us"] == 12_000_000

    segs = [t for t in draft["tracks"] if t.get("name") == setup_audio.AUTO_AUDIO_TRACK_NAME][0]["segments"]
    assert segs[0]["source_timerange"] == {"start": 0, "duration": 8_000_000}


def test_intro_shorter_than_anchor_is_slowed_down(tmp_path):
    draft = _make_sample_draft(video_durations=(10_000_000, 10_000_000))
    intro = tmp_path / "intro.mp3"
    ambient = tmp_path / "ambient.mp3"
    intro.write_bytes(b"a")
    ambient.write_bytes(b"b")

    durations = {intro: 12_000_000, ambient: 60_000_000}  # 6 c после отрезания хвоста
    with patch("setup_audio.probe_audio_duration_us", side_effect=lambda p: durations[p]):
        res = setup_audio.add_intro_and_ambient(
            draft, intro, ambient, intro_anchor_us=15_000_000, fade_us=4_000_000
        )

    # 6 c звука растягиваются ровно до 15 c => скорость 0.4x
    assert res["intro"]["speed"] == pytest.approx(0.4)
    assert res["intro"]["start_us"] == 0
    assert res["intro"]["duration_us"] == 15_000_000
    assert res["intro"]["source_start_us"] == 0
    assert res["intro"]["source_duration_us"] == 6_000_000
    assert res["ambient"]["start_us"] == 15_000_000
    assert not res["warnings"]

    segs = [t for t in draft["tracks"]
            if t.get("name") == setup_audio.AUTO_AUDIO_TRACK_NAME][0]["segments"]
    assert segs[0]["speed"] == pytest.approx(0.4)
    assert segs[0]["source_timerange"] == {"start": 0, "duration": 6_000_000}
    assert segs[0]["target_timerange"] == {"start": 0, "duration": 15_000_000}

    speeds = {m["id"]: m for m in draft["materials"]["speeds"]}
    linked = [speeds[r] for r in segs[0]["extra_material_refs"] if r in speeds]
    assert len(linked) == 1
    assert linked[0]["speed"] == pytest.approx(0.4)


def test_intro_slowdown_is_capped_and_warns(tmp_path):
    draft = _make_sample_draft(video_durations=(30_000_000, 30_000_000))
    intro = tmp_path / "intro.mp3"
    intro.write_bytes(b"a")

    # нужно замедление в 5 раз, но предел — в 4 раза (0.25x)
    durations = {intro: 12_000_000}  # 6 c
    with patch("setup_audio.probe_audio_duration_us", side_effect=lambda p: durations[p]):
        res = setup_audio.add_intro_and_ambient(
            draft, intro, None, intro_anchor_us=30_000_000, fade_us=4_000_000
        )

    assert res["intro"]["speed"] == pytest.approx(setup_audio.MIN_INTRO_SPEED)
    assert res["intro"]["duration_us"] == 24_000_000  # 6 c / 0.25
    assert res["warnings"]


def test_intro_shorter_than_tail_trim_raises(tmp_path):
    draft = _make_sample_draft(video_durations=(10_000_000,))
    intro = tmp_path / "intro.mp3"
    intro.write_bytes(b"a")

    with patch("setup_audio.probe_audio_duration_us", return_value=4_000_000):
        with pytest.raises(setup_audio.DraftError):
            setup_audio.add_intro_and_ambient(draft, intro, None)


def test_parse_prompt_scripts(tmp_path):
    path = _prompts_file(tmp_path, [
        (1, "Один рейс этого самосвала заменяет два десятка обычных."),
        (2, "поместились бы две двухкомнатные квартиры."),
        (3, "Начнём с самого льда и с того, почему острый нос"),
    ])
    scripts = setup_audio.parse_prompt_scripts(path)

    assert [n for n, _ in scripts] == [1, 2, 3]
    assert scripts[2][1].startswith("Начнём с")


def test_parse_prompt_scripts_keeps_brackets_out_of_prompt_text(tmp_path):
    path = tmp_path / "p.txt"
    path.write_text(
        "[PROMPT #1]\nA truck [with a label] on it.\n[первый фрагмент]\n"
        "[PROMPT #2]\nДругой кадр.\n[второй фрагмент]\n",
        encoding="utf-8",
    )
    scripts = setup_audio.parse_prompt_scripts(path)
    assert scripts == [(1, "первый фрагмент"), (2, "второй фрагмент")]


def test_find_intro_end_script_ignores_similar_words():
    scripts = [
        (1, "а начинал с грузовиков, которые везли меньше."),
        (2, "История этого рекорда начинается в цеху."),
        (3, "цикл начинается заново."),
        (4, "мастера начали вручную собирать то, чего не было."),
        (5, "Начнём с самого льда."),
    ]
    assert setup_audio.find_intro_end_script(scripts) == (5, "Начнём с самого льда.")


def test_find_intro_end_script_matches_nachalos():
    scripts = [
        (1, "мастера начали вручную собирать салон."),
        (2, "А началось всё в середине девяностых."),
    ]
    assert setup_audio.find_intro_end_script(scripts) == (2, "А началось всё в середине девяностых.")


def test_find_prompt_segment_start_us_exact_and_inferred(tmp_path):
    draft = _make_sample_draft(video_durations=(4_000_000, 5_000_000, 6_000_000))
    assert setup_audio.find_prompt_segment_start_us(draft, 1) == 0
    assert setup_audio.find_prompt_segment_start_us(draft, 3) == 9_000_000

    # Второй клип «заменён» стоковым видео: позиция вычисляется по соседям
    draft["materials"]["videos"][1]["material_name"] = "stock_clip.mp4"
    assert setup_audio.find_prompt_segment_start_us(draft, 2) == 4_000_000


def test_resolve_intro_anchor_from_prompts(tmp_path):
    draft = _make_sample_draft(video_durations=(6_000_000, 8_000_000))
    prompts = _prompts_file(tmp_path, [
        (1, "Первая фраза."),
        (2, "Начинаем с главного."),
    ])
    anchor = setup_audio.resolve_intro_anchor(draft, prompts_path=prompts)
    assert anchor["prompt"] == 2
    assert anchor["start_us"] == 6_000_000


def test_resolve_intro_anchor_manual_prompt(tmp_path):
    draft = _make_sample_draft(video_durations=(6_000_000, 8_000_000))
    anchor = setup_audio.resolve_intro_anchor(draft, anchor_prompt=2)
    assert anchor["prompt"] == 2
    assert anchor["start_us"] == 6_000_000


def test_resolve_intro_anchor_marker_not_found(tmp_path):
    draft = _make_sample_draft(video_durations=(6_000_000,))
    prompts = _prompts_file(tmp_path, [(1, "Здесь нет нужной фразы.")])
    with pytest.raises(setup_audio.DraftError):
        setup_audio.resolve_intro_anchor(draft, prompts_path=prompts)


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
    prompts = _prompts_file(tmp_path, [(1, "Хук."), (2, "Начнём с главного.")])

    with (
        patch("setup_audio.capcut_is_running", return_value=False),
        patch("setup_audio.probe_audio_duration_us",
              side_effect=lambda p: 14_000_000 if p == intro else 30_000_000),
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
            prompts_path=prompts,
        )

    assert Path(res["backup"]).is_file()
    assert res["anchor"]["prompt"] == 2
    assert res["anchor"]["start_us"] == 6_000_000
    assert res["audio"]["intro"]["duration_us"] == 6_000_000

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
    durations = {intro: 10_000_000, ambient: 20_000_000}

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

    assert res["intro"]["start_us"] == 0
    assert res["intro"]["duration_us"] == 5_000_000
    assert res["intro"]["fade_out_us"] == 4_500_000
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


def test_plan_subscribe_placements_first_last_and_comment_near_middle(tmp_path):
    draft = _make_sample_draft(video_durations=(60_000_000,) * 20)  # 20 минут
    prompts = _prompts_file(tmp_path, [
        (1, "Вступление без призывов."),
        (2, "Подпишитесь на канал и поставьте лайк."),
        (10, "Напишите в комментариях, что вы думаете."),
        (19, "Подписка и лайк под этим видео."),
    ])
    scripts = setup_audio.parse_prompt_scripts(prompts)

    plan = setup_audio.plan_subscribe_placements(draft, scripts, clip_duration_us=8_000_000)

    assert [p["reason"] for p in plan["placements"]] == ["subscribe", "comment", "subscribe"]
    assert [p["start_us"] for p in plan["placements"]] == [
        60_000_000,   # кадр #2
        540_000_000,  # кадр #10 — в пределах 3 минут от середины (600 с)
        1_080_000_000,  # кадр #19
    ]


def test_plan_subscribe_placements_uses_middle_when_comment_is_far(tmp_path):
    draft = _make_sample_draft(video_durations=(60_000_000,) * 20)
    prompts = _prompts_file(tmp_path, [
        (1, "Вступление."),
        (2, "Подпишитесь на канал."),
        (4, "Напишите в комментариях, что думаете."),  # 180 с: далеко от 600 с
        (19, "Подписка и лайк."),
    ])
    scripts = setup_audio.parse_prompt_scripts(prompts)

    plan = setup_audio.plan_subscribe_placements(draft, scripts, clip_duration_us=8_000_000)

    reasons = [p["reason"] for p in plan["placements"]]
    assert reasons == ["subscribe", "middle", "subscribe"]
    middle = plan["placements"][1]
    # Оверлей центрируется относительно середины ролика
    assert middle["start_us"] == 600_000_000 - 4_000_000


def test_add_subscribe_video_creates_overlay_track_above_main(tmp_path):
    draft = _make_sample_draft(video_durations=(60_000_000,) * 20)
    video = tmp_path / "Subscribe.mp4"
    video.write_bytes(b"fake")
    prompts = _prompts_file(tmp_path, [
        (1, "Вступление."),
        (2, "Подпишитесь на канал."),
        (10, "Напишите в комментариях."),
        (19, "Подписка и лайк."),
    ])
    video_info = {"duration_us": 8_100_000, "width": 1920, "height": 1080}
    with (
        patch("setup_audio.probe_video_info", return_value=video_info),
        patch("setup_audio._guess_chroma_resource_path", return_value="C:/CapCut/Resources/Chroma2"),
    ):
        res = setup_audio.add_subscribe_video(draft, video, prompts)

    assert [p["reason"] for p in res["placements"]] == ["subscribe", "comment", "subscribe"]
    assert draft["tracks"][0]["type"] == "video"
    overlay = draft["tracks"][1]
    assert overlay["name"] == setup_audio.SUBSCRIBE_TRACK_NAME
    assert len(overlay["segments"]) == 3

    seg = overlay["segments"][0]
    assert seg["clip"]["scale"]["x"] == pytest.approx(setup_audio.SUBSCRIBE_CLIP_SCALE)
    assert seg["clip"]["transform"]["y"] == pytest.approx(setup_audio.SUBSCRIBE_CLIP_Y)
    assert seg["target_timerange"] == {"start": 60_000_000, "duration": 8_100_000}

    chromas = {c["id"]: c for c in draft["materials"]["chromas"]}
    chroma = next(chromas[r] for r in seg["extra_material_refs"] if r in chromas)
    assert chroma["color"] == setup_audio.SUBSCRIBE_CHROMA_COLOR
    assert chroma["path"] == "C:/CapCut/Resources/Chroma2"

    videos_before = len(draft["materials"]["videos"])

    # Повторный запуск заменяет оверлеи и не плодит материалы
    with (
        patch("setup_audio.probe_video_info", return_value=video_info),
        patch("setup_audio._guess_chroma_resource_path", return_value="C:/CapCut/Resources/Chroma2"),
    ):
        setup_audio.add_subscribe_video(draft, video, prompts)

    assert len(draft["tracks"][1]["segments"]) == 3
    assert len(draft["materials"]["videos"]) == videos_before
    assert len(draft["materials"]["chromas"]) == 3


def test_gui_dispatch_worker_audio(monkeypatch):
    import sys
    import gui

    called = []
    monkeypatch.setattr(gui.setup_audio, "main", lambda: called.append(sys.argv[:]))
    monkeypatch.setattr(sys, "argv", ["gui.py", "--run-worker", "audio", "proj_dir", "--dry-run"])
    assert gui._dispatch_worker() is True
    assert called == [["gui.py", "proj_dir", "--dry-run"]]
