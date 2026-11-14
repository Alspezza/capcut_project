#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автотесты для capcut_common, make_transitions и stock_fetcher."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import capcut_common
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
            draft = Path(td) / "draft_content.json"
            draft.write_text('{"v": 0}', encoding="utf-8")

            for i in range(1, 5):
                capcut_common.save_draft_with_backup(draft, {"v": i}, prefix="test", max_backups=2)
                time.sleep(0.02)

            backups = list((Path(td) / ".backups").glob("draft_content.test_*.json"))
            self.assertLessEqual(len(backups), 2)

            # Портим текущий файл и откатываем
            draft.write_text('{"v": 999}', encoding="utf-8")
            restored = capcut_common.restore_latest_backup(draft)
            self.assertTrue(restored.exists())
            self.assertEqual(json.loads(draft.read_text(encoding="utf-8"))["v"], 3)

    def test_pick_main_track_with_empty_video_track(self):
        tracks = [
            {"type": "audio", "segments": [{"id": "A1"}]},
            {"type": "video", "segments": []},
            {"type": "video", "segments": [{"id": "V1"}, {"id": "V2"}]},
        ]
        idx, tr = capcut_common.pick_main_track(tracks)
        self.assertEqual(idx, 1)
        self.assertEqual(len(tr["segments"]), 2)

        idx0, tr0 = capcut_common.pick_main_track(tracks, index=0)
        self.assertEqual(idx0, 0)
        self.assertEqual(tr0["segments"], [])

        idx1, tr1 = capcut_common.pick_main_track(tracks, index=1)
        self.assertEqual(idx1, 1)
        self.assertEqual(len(tr1["segments"]), 2)


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
            # Переход вставлен после SPD1 на втором сегменте (S2)
            s2_refs = updated["tracks"][0]["segments"][1]["extra_material_refs"]
            self.assertEqual(len(s2_refs), 1)
            self.assertEqual(s2_refs[0], updated["materials"]["transitions"][0]["id"])


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


if __name__ == "__main__":
    unittest.main()
