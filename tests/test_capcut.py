#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автотесты для capcut_common, clear_vids, make_transitions, stock_fetcher и gui."""

import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import capcut_common
import clear_vids
import gui
import make_transitions
import stock_fetcher


def make_sample_draft(with_overlay: bool = False, with_old_transition: bool = True) -> dict:
    old_tid = "OLD-TRANS-1"
    segs = [
        {
            "id": "S1",
            "material_id": "V1",
            "target_timerange": {"start": 0, "duration": 3_000_000},
            "source_timerange": {"start": 0, "duration": 3_000_000},
            "extra_material_refs": ["SPD1", old_tid] if with_old_transition else ["SPD1"],
        },
        {
            "id": "S2",
            "material_id": "V2",
            "target_timerange": {"start": 3_000_000, "duration": 3_000_000},
            "source_timerange": {"start": 0, "duration": 3_000_000},
            "extra_material_refs": [],
        },
        {
            "id": "S3",
            "material_id": "V3",
            "target_timerange": {"start": 6_000_000, "duration": 2_000_000},
            "source_timerange": {"start": 0, "duration": 2_000_000},
            "extra_material_refs": [],
        },
    ]
    tracks = [{"type": "video", "segments": segs}]
    if with_overlay:
        tracks.append({
            "type": "video",
            "segments": [
                {"id": "OV1", "target_timerange": {"start": 3_000_000, "duration": 1_000_000}}
            ],
        })
    transitions = (
        [{"id": old_tid, "effect_id": "7159450506648097281", "name": "Old", "path": "/x", "is_overlap": True}]
        if with_old_transition else []
    )
    return {
        "name": "TestDraft",
        "canvas_config": {"width": 1280, "height": 720},
        "tracks": tracks,
        "materials": {
            "videos": [],
            "speeds": [{"id": "SPD1"}],
            "placeholder_infos": [],
            "transitions": transitions,
        },
    }


class TestCapCutCommon(unittest.TestCase):
    def test_find_and_load_draft_variants(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p1 = root / "proj1"
            p1.mkdir()
            dc = p1 / "draft_content.json"
            dc.write_bytes(b"\xef\xbb\xbf" + json.dumps({"ok": 1}).encode("utf-8"))
            self.assertEqual(capcut_common.find_draft_file(p1), dc)
            self.assertEqual(capcut_common.load_draft(dc), {"ok": 1})

            p2 = root / "proj2"
            tl = p2 / "Timelines" / "001"
            tl.mkdir(parents=True)
            di = tl / "draft_info.json"
            di.write_text('{"ok": 2}', encoding="utf-8")
            self.assertEqual(capcut_common.find_draft_file(p2), di)

            bad = p1 / "bad.json"
            bad.write_bytes(b"\x00\xff\xfeencrypted")
            with self.assertRaises(capcut_common.DraftError):
                capcut_common.load_draft(bad)

    def test_backup_rotation_and_restore(self):
        with tempfile.TemporaryDirectory() as td:
            draft = Path(td) / "draft[1].json"
            draft.write_text('{"v": 0}', encoding="utf-8")

            for i in range(1, 5):
                capcut_common.save_draft_with_backup(draft, {"v": i}, prefix="test", max_backups=2)
                time.sleep(0.01)

            backups = list((Path(td) / ".backups").glob("draft*.test_*.json"))
            self.assertEqual(len(backups), 2)

            # Портим текущий файл и откатываем
            draft.write_text('{"v": 999}', encoding="utf-8")
            restored = capcut_common.restore_latest_backup(draft)
            self.assertTrue(restored.exists())
            self.assertEqual(json.loads(draft.read_text(encoding="utf-8"))["v"], 3)

    def test_pick_main_track_with_empty_video_track(self):
        tracks = [
            {"type": "audio", "segments": [{"id": "A1"}]},
            {"type": "video", "segments": None},
            {"type": "video", "segments": [{"id": "V1"}, {"id": "V2"}]},
        ]
        idx, tr = capcut_common.pick_main_track(tracks)
        self.assertEqual(idx, 1)
        self.assertEqual(len(tr["segments"]), 2)

        idx0, tr0 = capcut_common.pick_main_track(tracks, index=0)
        self.assertEqual(idx0, 0)
        self.assertIsNone(tr0["segments"])

        idx1, tr1 = capcut_common.pick_main_track(tracks, index=1)
        self.assertEqual(idx1, 1)
        self.assertEqual(len(tr1["segments"]), 2)

    def test_find_draft_file_empty_path(self):
        with self.assertRaises(capcut_common.DraftError):
            capcut_common.find_draft_file("   ")


class TestMakeTransitions(unittest.TestCase):
    def test_apply_transitions_and_skip_overlays(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "proj"
            proj.mkdir()
            dc = proj / "draft_content.json"
            dc.write_text(json.dumps(make_sample_draft(with_overlay=True)), encoding="utf-8")

            stats = make_transitions.apply_transitions(
                proj, dry_run=False, skip_overlays=True, force=True, log=lambda *_: None
            )
            self.assertEqual(stats["clips"], 3)
            self.assertEqual(stats["removed"], 1)
            self.assertEqual(stats["skipped"], 1)  # стык на 3s пропущен из-за оверлея
            self.assertEqual(stats["placed"], 1)   # стык на 6s поставлен

            updated = json.loads(dc.read_text(encoding="utf-8"))
            self.assertEqual(len(updated["materials"]["transitions"]), 1)
            # Переход на стыке S2-S3 вставлен на втором сегменте (S2)
            s2_refs = updated["tracks"][0]["segments"][1]["extra_material_refs"]
            self.assertEqual(len(s2_refs), 1)
            self.assertEqual(s2_refs[0], updated["materials"]["transitions"][0]["id"])

    def test_apply_transitions_frequency_params(self):
        def make_5clip_draft():
            d = make_sample_draft(with_overlay=False)
            # 5 клипов подряд: 2s, 1s, 3s, 3s, 3s
            durs = [2_000_000, 1_000_000, 3_000_000, 3_000_000, 3_000_000]
            segs = []
            cur = 0
            for idx, dur in enumerate(durs):
                segs.append({
                    "id": f"S{idx + 1}",
                    "material_id": "M1",
                    "target_timerange": {"start": cur, "duration": dur},
                    "extra_material_refs": [],
                })
                cur += dur
            d["tracks"][0]["segments"] = segs
            d["materials"]["transitions"] = []
            return d

        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "proj"
            proj.mkdir()
            dc = proj / "draft_content.json"
            dc.write_text(json.dumps(make_5clip_draft()), encoding="utf-8")

            # every=2 на 4 стыках -> стыки 0 и 2 (2 шт.)
            st = make_transitions.apply_transitions(proj, dry_run=True, force=True, every=2, log=lambda *_: None)
            self.assertEqual(st["placed"], 2)
            self.assertEqual(st["skipped"], 2)

            # skip_edges=True на 4 стыках -> остаются стыки 1 и 2 (2 шт.)
            st = make_transitions.apply_transitions(proj, dry_run=True, force=True, skip_edges=True, log=lambda *_: None)
            self.assertEqual(st["placed"], 2)
            self.assertEqual(st["skipped"], 2)

            # min_clip_sec=1.5 -> клип S2 (1s) отсекает стыки 0 (S1-S2) и 1 (S2-S3), остаются стыки 2 и 3 (2 шт.)
            st = make_transitions.apply_transitions(proj, dry_run=True, force=True, min_clip_sec=1.5, log=lambda *_: None)
            self.assertEqual(st["placed"], 2)
            self.assertEqual(st["skipped"], 2)

            # chance + seed -> воспроизводимость
            st1 = make_transitions.apply_transitions(proj, dry_run=True, force=True, chance=50.0, seed=42, log=lambda *_: None)
            st2 = make_transitions.apply_transitions(proj, dry_run=True, force=True, chance=50.0, seed=42, log=lambda *_: None)
            self.assertEqual(st1["placed"], st2["placed"])

            # chance=0 -> 0 переходов
            st0 = make_transitions.apply_transitions(proj, dry_run=True, force=True, chance=0.0, log=lambda *_: None)
            self.assertEqual(st0["placed"], 0)
            self.assertEqual(st0["skipped"], 4)

            # невалидные значения
            with self.assertRaises(make_transitions.TransitionsError):
                make_transitions.apply_transitions(proj, dry_run=True, force=True, every=0, log=lambda *_: None)
            with self.assertRaises(make_transitions.TransitionsError):
                make_transitions.apply_transitions(proj, dry_run=True, force=True, chance=120.0, log=lambda *_: None)
            with self.assertRaises(make_transitions.TransitionsError):
                make_transitions.apply_transitions(proj, dry_run=True, force=True, min_clip_sec=-1.0, log=lambda *_: None)

    def test_load_transition_pattern_partial_durations_and_rounding(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "transitions_config.json"
            cfg_path.write_text(
                json.dumps({
                    "durations_sec": {
                        "7159450506648097281": 0.29,
                        "7140916992118100482": 1.15,
                    }
                }),
                encoding="utf-8",
            )
            fake_file = Path(td) / "make_transitions.py"
            with mock.patch.object(make_transitions, "__file__", str(fake_file)):
                with mock.patch.object(sys, "frozen", False, create=True):
                    durs, pat = make_transitions.load_transition_pattern()
            self.assertEqual(durs["7159450506648097281"], 290_000)
            self.assertEqual(durs["7140916992118100482"], 1_150_000)
            self.assertEqual(pat, ["7159450506648097281", "7140916992118100482"])


class TestClearVids(unittest.TestCase):
    @staticmethod
    def _make_test_video(path: Path, with_watermark: bool, duration_sec: float = 2.0, fps: int = 10):
        import cv2
        import numpy as np

        w, h = 640, 360
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
        rng = np.random.default_rng(42)
        star = clear_vids.star_template(int(round(0.0371 * w)))
        sh, sw = star.shape
        cx, cy = int(0.906 * w), int(0.833 * h)
        y0, x0 = cy - sh // 2, cx - sw // 2
        n_frames = int(round(duration_sec * fps))
        for fi in range(n_frames):
            frame = rng.integers(30, 110, size=(h, w, 3), dtype=np.uint8)
            # сдвигающийся фон + полупрозрачная звёздочка
            cv2.rectangle(frame, ((fi * 25) % w, 40), ((fi * 25 + 80) % w, 200), (140, 140, 140), -1)
            if with_watermark:
                patch = frame[y0:y0 + sh, x0:x0 + sw].astype(np.float32)
                patch += (star[:, :, None] * 110.0)
                frame[y0:y0 + sh, x0:x0 + sw] = np.clip(patch, 0, 255).astype(np.uint8)
            writer.write(frame)
        writer.release()

    def test_left_align_shift(self):
        self.assertAlmostEqual(clear_vids.left_align_shift(1280, 720, 1280, 720, 1.15), 0.15)
        self.assertEqual(clear_vids.left_align_shift(720, 1280, 1280, 720, 1.15), 0.0)
        self.assertEqual(clear_vids.left_align_shift(1280, 0, 1280, 720, 1.15), 0.0)

    def test_detect_watermark_and_probe_times(self):
        with tempfile.TemporaryDirectory() as td:
            wm_vid = Path(td) / "wm.mp4"
            clean_vid = Path(td) / "clean.mp4"
            self._make_test_video(wm_vid, with_watermark=True, duration_sec=5.0, fps=6)
            self._make_test_video(clean_vid, with_watermark=False, duration_sec=2.0, fps=10)

            seg_long = {"source_timerange": {"start": 0, "duration": 5_000_000}}
            seen_times = []
            orig_read = clear_vids.read_frame

            def spy_read(cap, t_sec):
                seen_times.append(t_sec)
                return orig_read(cap, t_sec)

            with mock.patch.object(clear_vids, "read_frame", side_effect=spy_read):
                found_wm, _ = clear_vids.detect_watermark(
                    {"path": str(wm_vid)}, seg_long,
                    clear_vids.DEFAULT_MARKS, clear_vids.DEFAULT_STACK_THR,
                    clear_vids.DEFAULT_SINGLE_THR, 12,
                    [0.1, 0.5, 1.0, 2.0, 3.0, 4.0], None, "0001", {},
                )
            self.assertTrue(found_wm)
            self.assertIn(4.0, seen_times)  # не должно обрезаться к dur * 0.5 = 2.5

            seg_short = {"source_timerange": {"start": 0, "duration": 2_000_000}}
            found_clean, _ = clear_vids.detect_watermark(
                {"path": str(clean_vid)}, seg_short,
                clear_vids.DEFAULT_MARKS, clear_vids.DEFAULT_STACK_THR,
                clear_vids.DEFAULT_SINGLE_THR, 12,
                [0.1, 0.5, 1.0, 2.0, 3.0, 4.0], None, "0002", {},
            )
            self.assertFalse(found_clean)

    def test_default_track_zero_even_when_overlay_has_more_clips(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "proj"
            proj.mkdir()
            wm_vid = proj / "wm.mp4"
            self._make_test_video(wm_vid, with_watermark=True, duration_sec=1.5, fps=10)

            draft = {
                "name": "TrackTest",
                "canvas_config": {"width": 1280, "height": 720},
                "materials": {
                    "videos": [{
                        "id": "M0", "type": "video", "path": str(wm_vid),
                        "width": 1280, "height": 720,
                    }]
                },
                "tracks": [
                    {
                        "type": "video",
                        "segments": [{
                            "id": "MAIN1", "material_id": "M0",
                            "target_timerange": {"start": 0, "duration": 1_500_000},
                            "source_timerange": {"start": 0, "duration": 1_500_000},
                            "clip": {"scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0}},
                        }],
                    },
                    {
                        "type": "video",
                        "segments": [
                            {
                                "id": f"OV{i}", "material_id": "M0",
                                "target_timerange": {"start": i * 500_000, "duration": 500_000},
                                "source_timerange": {"start": 0, "duration": 500_000},
                                "clip": {"scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0}},
                            }
                            for i in range(3)
                        ],
                    },
                ],
            }
            dc = proj / "draft_content.json"
            dc.write_text(json.dumps(draft), encoding="utf-8")

            with mock.patch.object(sys, "argv", ["clear_vids.py", str(proj), "--force", "--workers", "1"]):
                clear_vids.main()

            updated = json.loads(dc.read_text(encoding="utf-8"))
            main_clip = updated["tracks"][0]["segments"][0]["clip"]
            ov_clip = updated["tracks"][1]["segments"][0]["clip"]
            self.assertAlmostEqual(main_clip["scale"]["x"], 1.15)
            self.assertAlmostEqual(main_clip["transform"]["x"], 0.15)
            self.assertAlmostEqual(ov_clip["scale"]["x"], 1.0)
            self.assertAlmostEqual(ov_clip["transform"]["x"], 0.0)

    def test_parallel_workers_and_debug_dir(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "proj"
            proj.mkdir()
            dbg = Path(td) / "debug"
            wm_vid = proj / "wm.mp4"
            clean_vid = proj / "clean.mp4"
            self._make_test_video(wm_vid, with_watermark=True, duration_sec=1.5, fps=10)
            self._make_test_video(clean_vid, with_watermark=False, duration_sec=1.5, fps=10)

            draft = {
                "name": "ParallelTest",
                "canvas_config": {"width": 1280, "height": 720},
                "materials": {
                    "videos": [
                        {"id": "M1", "type": "video", "path": str(wm_vid), "width": 1280, "height": 720},
                        {"id": "M2", "type": "video", "path": str(clean_vid), "width": 1280, "height": 720},
                    ]
                },
                "tracks": [
                    {
                        "type": "video",
                        "segments": [
                            {
                                "id": "S1", "material_id": "M1",
                                "target_timerange": {"start": 0, "duration": 1_500_000},
                                "source_timerange": {"start": 0, "duration": 1_500_000},
                                "clip": {"scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0}},
                            },
                            {
                                "id": "S2", "material_id": "M2",
                                "target_timerange": {"start": 1_500_000, "duration": 1_500_000},
                                "source_timerange": {"start": 0, "duration": 1_500_000},
                                "clip": {"scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.5, "y": 0.0}},
                            },
                        ],
                    }
                ],
            }
            dc = proj / "draft_content.json"
            dc.write_text(json.dumps(draft), encoding="utf-8")

            with mock.patch.object(
                sys, "argv",
                ["clear_vids.py", str(proj), "--force", "--workers", "2", "--reset-clean", "--debug-dir", str(dbg)],
            ):
                clear_vids.main()

            updated = json.loads(dc.read_text(encoding="utf-8"))
            s1, s2 = updated["tracks"][0]["segments"]
            self.assertAlmostEqual(s1["clip"]["transform"]["x"], 0.15)
            self.assertAlmostEqual(s2["clip"]["transform"]["x"], 0.0)
            self.assertTrue((dbg / "0001_WM.jpg").is_file())
            self.assertTrue((dbg / "0002_clean.jpg").is_file())


class TestStockFetcher(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(stock_fetcher.parse_youtube_timestamp("https://youtu.be/x?t=87s"), 87)
        self.assertEqual(stock_fetcher.parse_youtube_timestamp("https://youtu.be/x?t=1h2m3s"), 3723)
        self.assertIsNone(stock_fetcher.parse_youtube_timestamp("https://youtu.be/x"))
        self.assertEqual(stock_fetcher.sanitize_filename('bad:name/with*chars?'), "bad_name_with_chars")

    def test_parse_tags_file(self):
        with tempfile.TemporaryDirectory() as td:
            tf = Path(td) / "tags.txt"
            tf.write_text("# comment\nКорабли: cargo ship, port aerial\n\nПусто:\n", encoding="utf-8")
            cats = stock_fetcher.parse_tags_file(tf)
            self.assertEqual(len(cats), 1)
            self.assertEqual(cats[0]["category"], "Корабли")
            self.assertEqual(cats[0]["tags"], ["cargo ship", "port aerial"])

    def test_pixabay_min_per_page_and_empty_url_fallback(self):
        captured = {}

        def fake_get(url, headers=None, params=None):
            captured["params"] = params
            return {
                "hits": [
                    {
                        "id": 101,
                        "videos": {
                            "large": {"url": "", "width": 0, "height": 0},
                            "medium": {"url": "https://cdn.example/med.mp4", "width": 1280, "height": 720},
                        },
                    },
                    {
                        "id": 102,
                        "videos": {"large": {"url": "https://cdn.example/2.mp4", "width": 1920, "height": 1080}},
                    },
                ]
            }

        with mock.patch.object(stock_fetcher, "load_config", return_value={"pixabay_api_key": "k"}):
            with mock.patch.object(stock_fetcher, "http_get_json", side_effect=fake_get):
                res = stock_fetcher.search_pixabay_videos("ocean", count=1)

        self.assertEqual(captured["params"]["per_page"], 3)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["url"], "https://cdn.example/med.mp4")

    def test_youtube_timestamp_distinct_filenames(self):
        seen_opts = []

        class FakeYDL:
            def __init__(self, opts):
                seen_opts.append(opts)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def download(self, urls):
                return 0

        fake_yt_dlp = type(sys)("yt_dlp")
        fake_yt_dlp.YoutubeDL = FakeYDL
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}):
                self.assertTrue(stock_fetcher.download_youtube_url("https://youtu.be/abc?t=30", Path(td), duration=20))
                self.assertTrue(stock_fetcher.download_youtube_url("https://youtu.be/abc?t=120", Path(td), duration=20))
        self.assertIn("[30-50s]", seen_opts[0]["outtmpl"])
        self.assertIn("[120-140s]", seen_opts[1]["outtmpl"])

    def test_download_file_stream_cleans_part_on_error(self):
        class BrokenResp:
            headers = {"Content-Length": "100"}
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self, n):
                raise RuntimeError("network drop")

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "clip.mp4"
            with mock.patch("urllib.request.urlopen", return_value=BrokenResp()):
                ok = stock_fetcher.download_file_stream("https://example.com/v.mp4", dest)
            self.assertFalse(ok)
            self.assertFalse(dest.exists())
            self.assertFalse((Path(td) / "clip.mp4.part").exists())

    def test_cli_exits_nonzero_on_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.txt"
            with mock.patch.object(stock_fetcher, "load_config", return_value={"pexels_api_key": "k"}):
                with mock.patch.object(sys, "argv", ["stock_fetcher.py", "batch-tags", str(missing)]):
                    with self.assertRaises(SystemExit) as ctx:
                        stock_fetcher.main()
            self.assertNotEqual(ctx.exception.code, 0)


class TestGuiDispatch(unittest.TestCase):
    def test_dispatch_worker_false_by_default(self):
        with mock.patch.object(sys, "argv", ["gui.py"]):
            self.assertFalse(gui._dispatch_worker())

    def test_dispatch_worker_calls_trans(self):
        with mock.patch.object(sys, "argv", ["gui.py", "--run-worker", "trans", "--help"]):
            with mock.patch("sys.stdout", new=io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    gui._dispatch_worker()
            self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
