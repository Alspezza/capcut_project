#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_audio.py — автоматическая настройка аудио и финального Fade Out в проекте CapCut:
  1. Добавляет intro и ambient, громкость -7 дБ для обоих.
     В начале intro и ambient НЕТ Fade In — добавляется только Fade Out.
     Конец intro выравнивается по кадру видео, в сценарии которого звучит фраза-маркер
     («начнём с…», «начинаем…» и т. п.). Файл промптов кадров — обычный .txt вида
     «[PROMPT #N] <описание кадра> [<фрагмент сценария>]»; номер промпта связывается с
     клипом видео <N>.mp4 на основной дорожке проекта. Хвост intro (последние 6 сек)
     не используется — там плохой звук; длинное intro не обрезается с начала, а подрезается с конца.
     Переход intro → ambient: Fade Out у intro 4.5 сек (без Fade In у ambient).
  2. Включает нормализацию звука (materials.loudnesses, -23 LUFS) на дорожке озвучки (по умолчанию 'voice').
  3. Добавляет анимацию Fade Out (4 сек) на последний видеоклип основной дорожки.
  4. Кладёт видео с призывом подписаться верхним слоем (на дорожку выше основной):
     два места ищутся по сценарию промптов (все формы слова «подпишись»), третье —
     ровно посередине ролика, либо на кадре с призывом «напишите в комментариях»,
     если этот кадр находится в пределах ±3 минут от середины.
     Положение, размер и удаление зелёного фона — как в эталонном проекте CapCut.
"""
import argparse
import copy
import json
import math
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from capcut_common import (
    DraftError,
    capcut_is_running,
    default_cache_effect_dir,
    default_drafts_dir,
    find_draft_file,
    load_draft,
    pick_main_track,
    save_draft_with_backup,
)

AUTO_AUDIO_TRACK_NAME = "CapCutAutomation Audio"
FADE_OUT_EFFECT_ID = "6798320902548230669"
FADE_OUT_FALLBACK_HASH = "c6f05ce62355b537be762550040bfc08"

# Интро: последние 6 сек не используем (плохой звук), Fade Out у intro — 4.5 сек.
# Fade In на intro и ambient отключён: добавляется только Fade Out.
INTRO_TAIL_TRIM_US = 6_000_000
INTRO_FADE_IN_US = 0
INTRO_FADE_OUT_US = 4_500_000
AMBIENT_FADE_IN_US = 0
# Максимальное замедление интро, если оно короче кадра-якоря (0.25 = в 4 раза медленнее).
MIN_INTRO_SPEED = 0.25

# --- Видео с призывом подписаться (зелёный фон) ----------------------------
# Дорожка-оверлей создаётся сразу над основной видеодорожкой.
SUBSCRIBE_TRACK_NAME = "CapCutAutomation Subscribe"
# Все формы слова «подпишись»: подпишись, подпишитесь, подписка, подписывайтесь и т. п.
# «подпиш-» — повелительное наклонение, «подпис-» — подписка/подписываться.
SUBSCRIBE_MARKER_RE = r"подпи[сш]\w*"
# Призыв к комментариям («напишите в комментариях», «поделитесь в комментариях»).
SUBSCRIBE_COMMENT_RE = r"\bкоммент\w*"
# Насколько близко к середине ролика должен быть кадр с призывом к комментариям.
SUBSCRIBE_MIDDLE_TOLERANCE_US = 3 * 60_000_000
# Если длительность видео с подпиской определить не удалось.
SUBSCRIBE_DEFAULT_DURATION_US = 8_100_000
# Геометрия оверлея как в эталонном draft_content.json: уменьшен и опущен вниз.
SUBSCRIBE_CLIP_SCALE = 0.4495192307692308
SUBSCRIBE_CLIP_X = 0.0
SUBSCRIBE_CLIP_Y = -0.5889423076923077
# Удаление зелёного фона (chroma key) как в эталонном проекте.
SUBSCRIBE_CHROMA_COLOR = "#25ff0eff"
SUBSCRIBE_CHROMA_INTENSITY = 0.53

# Фразы-маркеры конца интро во фрагментах сценария.
# «начнём», «начнем», «начинаем», «началось» — но НЕ «начинается», «начинал», «начинают», «начали».
DEFAULT_ANCHOR_MARKERS = r"\bнач(?:нём|нем|инаем|алось)\b"

_PROMPT_HEADER_RE = re.compile(r"\[\s*PROMPT\s*#\s*(\d+)\s*\]", re.IGNORECASE)
_SCRIPT_BLOCK_RE = re.compile(r"\[([^\[\]]+)\]")

_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def db_to_linear(db: float) -> float:
    """Переводит децибелы в линейный коэффициент громкости CapCut."""
    return 10.0 ** (db / 20.0)


def probe_audio_duration_us(path: Path) -> int:
    """Возвращает длительность аудиофайла в микросекундах через ffprobe."""
    if not path.is_file():
        raise DraftError(f"Аудиофайл не найден: {path}")
    try:
        res = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            errors="ignore",
            creationflags=_SUBPROCESS_FLAGS,
        )
    except FileNotFoundError as exc:
        raise DraftError("Не найден ffprobe. Установите FFmpeg и добавьте его в PATH.") from exc

    raw = res.stdout.strip()
    if res.returncode != 0 or not raw:
        raise DraftError(f"Не удалось определить длительность файла {path.name}: {res.stderr.strip()}")
    try:
        sec = float(raw)
    except ValueError as exc:
        raise DraftError(f"Некорректная длительность от ffprobe для {path.name}: {raw}") from exc
    if not math.isfinite(sec) or sec <= 0:
        raise DraftError(f"Длительность файла {path.name} должна быть > 0 (получено {sec}).")
    return round(sec * 1_000_000)


def probe_video_info(path: Path) -> dict:
    """Возвращает {'duration_us', 'width', 'height'} видеофайла через ffprobe."""
    if not path.is_file():
        raise DraftError(f"Видеофайл не найден: {path}")
    try:
        res = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration:format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            errors="ignore",
            creationflags=_SUBPROCESS_FLAGS,
        )
    except FileNotFoundError as exc:
        raise DraftError("Не найден ffprobe. Установите FFmpeg и добавьте его в PATH.") from exc

    raw = res.stdout.strip()
    if res.returncode != 0 or not raw:
        raise DraftError(f"Не удалось прочитать видео {path.name}: {res.stderr.strip()}")
    try:
        info = json.loads(raw)
    except ValueError as exc:
        raise DraftError(f"Некорректный ответ ffprobe для {path.name}.") from exc

    streams = info.get("streams") or []
    stream = streams[0] if streams else {}
    seconds = stream.get("duration") or (info.get("format") or {}).get("duration")
    try:
        sec = float(seconds)
    except (TypeError, ValueError) as exc:
        raise DraftError(f"Не удалось определить длительность видео {path.name}.") from exc
    if not math.isfinite(sec) or sec <= 0:
        raise DraftError(f"Длительность видео {path.name} должна быть > 0 (получено {sec}).")

    width = int(stream.get("width") or 1920)
    height = int(stream.get("height") or 1080)
    return {"duration_us": round(sec * 1_000_000), "width": width, "height": height}


def measure_loudness_ebur128(path: Path) -> dict | None:
    """Измеряет avg_loudness (LUFS) и peak_loudness (dBFS) через ffmpeg ebur128."""
    if not path.is_file():
        return None
    try:
        res = subprocess.run(
            [
                "ffmpeg",
                "-nostats",
                "-i", str(path),
                "-af", "ebur128=peak=true",
                "-f", "null",
                "-",
            ],
            capture_output=True,
            text=True,
            errors="ignore",
            creationflags=_SUBPROCESS_FLAGS,
        )
    except FileNotFoundError:
        return None

    if res.returncode != 0:
        return None

    i_matches = re.findall(r"^\s*I:\s*([+-]?\d+(?:\.\d+)?)\s+LUFS\b", res.stderr, re.MULTILINE)
    p_matches = re.findall(r"^\s*Peak:\s*([+-]?\d+(?:\.\d+)?)\s+dBFS\b", res.stderr, re.MULTILINE)
    if not i_matches or not p_matches:
        return None
    return {
        "avg_loudness": float(i_matches[-1]),
        "peak_loudness": float(p_matches[-1]),
    }


def get_video_duration_us(data: dict) -> int:
    """Возвращает длительность основной видеодорожки в микросекундах."""
    _, main_track = pick_main_track(data.get("tracks", []))
    segs = main_track.get("segments") or []
    end_us = 0
    for seg in segs:
        tr = seg.get("target_timerange") or {}
        end_us = max(end_us, int(tr.get("start", 0)) + int(tr.get("duration", 0)))
    if end_us <= 0:
        end_us = int(data.get("duration") or 0)
    if end_us <= 0:
        raise DraftError("Длительность видео в проекте равна 0.")
    return end_us


def _ensure_material_list(mats: dict, key: str) -> list:
    lst = mats.get(key)
    if not isinstance(lst, list):
        lst = []
        mats[key] = lst
    return lst


def _make_audio_material(path: Path, full_duration_us: int) -> dict:
    return {
        "id": _uid(),
        "type": "extract_music",
        "name": path.name,
        "duration": full_duration_us,
        "path": path.resolve().as_posix(),
        "category_name": "local",
        "wave_points": [],
        "music_id": "",
        "app_id": 0,
        "text_id": "",
        "tone_type": "",
        "source_platform": 0,
        "video_id": "",
        "effect_id": "",
        "resource_id": "",
        "third_resource_id": "",
        "category_id": "",
        "intensifies_path": "",
        "formula_id": "",
        "check_flag": 1,
        "team_id": "",
        "local_material_id": _uid().lower(),
        "music_source": "",
    }


def _create_audio_segment(
    mats: dict,
    path: Path,
    full_duration_us: int,
    target_start_us: int,
    target_duration_us: int,
    volume_linear: float,
    fade_in_us: int = 0,
    fade_out_us: int = 0,
    source_start_us: int = 0,
    source_duration_us: int | None = None,
    speed: float = 1.0,
) -> dict:
    audio_mat = _make_audio_material(path, full_duration_us)
    _ensure_material_list(mats, "audios").append(audio_mat)

    speed = float(speed)
    source_dur = target_duration_us if source_duration_us is None else max(0, int(source_duration_us))

    speed_id = _uid()
    _ensure_material_list(mats, "speeds").append({
        "id": speed_id, "type": "speed", "mode": 0, "speed": speed, "curve_speed": None,
    })

    placeholder_id = _uid()
    _ensure_material_list(mats, "placeholder_infos").append({
        "id": placeholder_id, "type": "placeholder_info", "meta_type": "none",
        "res_path": "", "res_text": "", "error_path": "", "error_text": "",
    })

    fade_id = _uid()
    _ensure_material_list(mats, "audio_fades").append({
        "id": fade_id,
        "type": "audio_fade",
        "fade_type": 0,
        "fade_in_duration": max(0, int(fade_in_us)),
        "fade_out_duration": max(0, int(fade_out_us)),
    })

    beats_id = _uid()
    _ensure_material_list(mats, "beats").append({
        "id": beats_id, "type": "beats", "enable_ai_beats": False, "gear": 404,
        "gear_count": 0, "mode": 404, "user_beats": [], "user_delete_ai_beats": None,
        "ai_beats": {
            "melody_url": "", "melody_path": "", "beats_url": "", "beats_path": "",
            "melody_percents": [0.0], "beat_speed_infos": [],
        },
    })

    scm_id = _uid()
    _ensure_material_list(mats, "sound_channel_mappings").append({
        "id": scm_id, "type": "none", "audio_channel_mapping": 0, "is_config_open": False,
    })

    vocal_id = _uid()
    _ensure_material_list(mats, "vocal_separations").append({
        "id": vocal_id, "type": "vocal_separation", "choice": 0, "removed_sounds": [],
        "time_range": None, "production_path": "", "final_algorithm": "", "enter_from": "",
    })

    return {
        "id": _uid(),
        "material_id": audio_mat["id"],
        "source_timerange": {"start": max(0, int(source_start_us)), "duration": source_dur},
        "target_timerange": {"start": target_start_us, "duration": target_duration_us},
        "render_timerange": {"start": 0, "duration": 0},
        "desc": "",
        "state": 0,
        "speed": speed,
        "is_loop": False,
        "is_tone_modify": False,
        "reverse": False,
        "intensifies_audio": False,
        "cartoon": False,
        "volume": volume_linear,
        "last_nonzero_volume": volume_linear,
        "clip": None,
        "uniform_scale": None,
        "extra_material_refs": [
            speed_id, placeholder_id, fade_id, beats_id, scm_id, vocal_id,
        ],
        "render_index": 0,
        "keyframe_refs": [],
        "enable_lut": False,
        "enable_adjust": False,
        "enable_hsl": False,
        "visible": True,
        "group_id": "",
        "track_render_index": 0,
        "track_attribute": 0,
        "is_placeholder": False,
        "common_keyframes": [],
        "source": "segments_merging",
    }


_AUDIO_MATERIAL_KEYS = (
    "audios", "speeds", "placeholder_infos", "audio_fades",
    "beats", "sound_channel_mappings", "vocal_separations",
)

_VIDEO_MATERIAL_KEYS = (
    "videos", "canvases", "chromas", "speeds", "placeholder_infos",
    "sound_channel_mappings", "material_colors", "vocal_separations",
)


def _cleanup_orphaned_materials(
    data: dict,
    candidate_ids: set[str],
    keys: tuple[str, ...] = _AUDIO_MATERIAL_KEYS,
) -> None:
    if not candidate_ids:
        return
    still_used: set[str] = set()
    for t in data.get("tracks", []):
        if not isinstance(t, dict):
            continue
        for seg in t.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            if seg.get("material_id"):
                still_used.add(seg["material_id"])
            for ref in seg.get("extra_material_refs") or []:
                still_used.add(ref)

    to_remove = candidate_ids - still_used
    if not to_remove:
        return
    mats = data.get("materials") or {}
    for key in keys:
        lst = mats.get(key)
        if isinstance(lst, list):
            mats[key] = [m for m in lst if not (isinstance(m, dict) and m.get("id") in to_remove)]


def _find_or_create_auto_audio_track(data: dict) -> dict:
    tracks = data.setdefault("tracks", [])
    for t in tracks:
        if isinstance(t, dict) and t.get("type") == "audio" and t.get("name") == AUTO_AUDIO_TRACK_NAME:
            old_ids: set[str] = set()
            for seg in t.get("segments") or []:
                if isinstance(seg, dict):
                    if seg.get("material_id"):
                        old_ids.add(seg["material_id"])
                    for ref in seg.get("extra_material_refs") or []:
                        old_ids.add(ref)
            t["segments"] = []
            _cleanup_orphaned_materials(data, old_ids)
            return t

    track = {
        "id": _uid(),
        "type": "audio",
        "flag": 0,
        "attribute": 0,
        "name": AUTO_AUDIO_TRACK_NAME,
        "is_default_name": False,
        "segments": [],
    }
    tracks.append(track)
    return track


# ---------------------------------------------------------------------------
# Видео с призывом подписаться (оверлей на дорожке выше основной)
# ---------------------------------------------------------------------------
def _guess_chroma_resource_path() -> str:
    """Ищет папку Resources/Chroma2 в установленном CapCut (для удаления зелёного фона)."""
    roots: list[Path] = []
    try:
        drafts = default_drafts_dir()
        roots.append(drafts.parent / "CapCut")
        roots.append(drafts.parent)
    except Exception:
        pass
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        roots.append(Path(local_app) / "CapCut")
    roots.extend([
        Path(r"D:\CapCut"),
        Path(r"C:\Program Files\CapCut"),
        Path(r"C:\Program Files (x86)\CapCut"),
    ])
    for root in roots:
        if not root.is_dir():
            continue
        try:
            versions = sorted(root.iterdir(), key=lambda p: p.name, reverse=True)
        except OSError:
            continue
        for ver in versions:
            chroma = ver / "Resources" / "Chroma2"
            if chroma.is_dir():
                return chroma.as_posix()
    return ""


def _chroma_resource_path(data: dict) -> str:
    """
    Путь к ресурсу Chroma2: сначала берём из самого проекта (если там уже есть
    удаление фона), потом ищем в установленном CapCut.
    """
    mats = data.get("materials") or {}
    for chroma in mats.get("chromas") or []:
        path = str((chroma or {}).get("path") or "")
        if path and Path(path).is_dir():
            return path
    for video in mats.get("videos") or []:
        for key in ("video_mask_stroke", "video_mask_shadow"):
            path = str(((video or {}).get(key) or {}).get("path") or "")
            if path and Path(path).is_dir():
                return path
    return _guess_chroma_resource_path()


def _make_video_material(
    path: Path,
    duration_us: int,
    width: int,
    height: int,
    chroma_path: str,
) -> dict:
    """Материал видео-оверлея (структура как у клипа с подпиской в эталонном проекте)."""
    return {
        "id": _uid(),
        "unique_id": uuid.uuid4().hex,
        "type": "video",
        "duration": max(1, int(duration_us)),
        "path": path.resolve().as_posix(),
        "media_path": "",
        "local_id": "",
        "has_audio": True,
        "reverse_path": "",
        "intensifies_path": "",
        "reverse_intensifies_path": "",
        "intensifies_audio_path": "",
        "cartoon_path": "",
        "width": int(width or 1920),
        "height": int(height or 1080),
        "category_id": "",
        "category_name": "local",
        "material_id": "",
        "material_name": path.name,
        "material_url": "",
        "crop": {
            "upper_left_x": 0.0,
            "upper_left_y": 0.0,
            "upper_right_x": 1.0,
            "upper_right_y": 0.0,
            "lower_left_x": 0.0,
            "lower_left_y": 1.0,
            "lower_right_x": 1.0,
            "lower_right_y": 1.0,
        },
        "crop_ratio": "free",
        "audio_fade": None,
        "crop_scale": 1.0,
        "extra_type_option": 0,
        "stable": {
            "stable_level": 0,
            "matrix_path": "",
            "time_range": {"start": 0, "duration": 0},
        },
        "matting": {
            "flag": 0,
            "path": "",
            "interactiveTime": [],
            "has_use_quick_brush": False,
            "strokes": [],
            "has_use_quick_eraser": False,
            "expansion": 0,
            "feather": 0,
            "reverse": False,
            "custom_matting_id": "",
            "enable_matting_stroke": False,
            "is_clould": False,
            "mask_video_path": "",
            "cloud_product_fps": 0.0,
            "usage": "",
        },
        "source": 0,
        "source_platform": 0,
        "formula_id": "",
        "check_flag": 62978047,
        "video_algorithm": {
            "algorithms": [],
            "time_range": None,
            "path": "",
            "gameplay_configs": [],
            "ai_in_painting_config": [],
            "complement_frame_config": None,
            "motion_blur_config": None,
            "deflicker": None,
            "noise_reduction": None,
            "quality_enhance": None,
            "super_resolution": None,
            "ai_background_configs": [],
            "smart_complement_frame": None,
            "aigc_generate": None,
            "aigc_generate_list": [],
            "mouth_shape_driver": None,
            "ai_expression_driven": None,
            "ai_motion_driven": None,
            "image_interpretation": None,
            "story_video_modify_video_config": {
                "task_id": "",
                "is_overwrite_last_video": False,
                "tracker_task_id": "",
                "generate_id": "",
                "generate_card_id": "",
            },
            "skip_algorithm_index": [],
        },
        "is_unified_beauty_mode": False,
        "is_set_beauty_mode": False,
        "object_locked": None,
        "smart_motion": None,
        "multi_camera_info": None,
        "freeze": None,
        "picture_from": "none",
        "picture_set_category_id": "",
        "picture_set_category_name": "",
        "team_id": "",
        "local_material_id": _uid().lower(),
        "origin_material_id": "",
        "request_id": "",
        "has_sound_separated": False,
        "is_text_edit_overdub": False,
        "is_ai_generate_content": False,
        "is_video_copilot_aigc_content": False,
        "aigc_type": "none",
        "is_copyright": False,
        "aigc_history_id": "",
        "aigc_item_id": "",
        "local_material_from": "",
        "smart_match_info": None,
        "beauty_face_preset_infos": [],
        "beauty_body_preset_id": "",
        "beauty_face_auto_preset": {
            "preset_id": "", "name": "", "rate_map": "", "scene": "",
        },
        "beauty_face_auto_preset_infos": [],
        "beauty_body_auto_preset": None,
        "live_photo_timestamp": -1,
        "live_photo_cover_path": "",
        "content_feature_info": None,
        "corner_pin": None,
        "surface_trackings": [],
        "video_mask_stroke": {
            "resource_id": "",
            "path": chroma_path,
            "type": "",
            "color": "",
            "size": 0.0,
            "alpha": 0.0,
            "distance": 0.0,
            "texture": 0.0,
            "horizontal_shift": 0.0,
            "vertical_shift": 0.0,
        },
        "video_mask_shadow": {
            "resource_id": "",
            "path": chroma_path,
            "color": "",
            "alpha": 0.0,
            "blur": 0.0,
            "distance": 0.0,
            "angle": 0.0,
        },
        "pre_applied_vip_materials": [],
        "workflow_node_id": "",
    }


def _create_video_segment(
    mats: dict,
    path: Path,
    info: dict,
    start_us: int,
    duration_us: int,
    chroma_path: str,
) -> dict:
    """Сегмент видео-оверлея с уменьшением, сдвигом вниз и удалением зелёного фона."""
    video_mat = _make_video_material(
        path,
        info["duration_us"],
        info["width"],
        info["height"],
        chroma_path,
    )
    _ensure_material_list(mats, "videos").append(video_mat)

    speed_id = _uid()
    _ensure_material_list(mats, "speeds").append({
        "id": speed_id, "type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None,
    })

    placeholder_id = _uid()
    _ensure_material_list(mats, "placeholder_infos").append({
        "id": placeholder_id, "type": "placeholder_info", "meta_type": "none",
        "res_path": "", "res_text": "", "error_path": "", "error_text": "",
    })

    chroma_id = _uid()
    _ensure_material_list(mats, "chromas").append({
        "id": chroma_id,
        "type": "chroma",
        "color": SUBSCRIBE_CHROMA_COLOR,
        "intensity_value": SUBSCRIBE_CHROMA_INTENSITY,
        "shadow_value": 0.0,
        "path": chroma_path,
        "resource_id": "",
        "should_transfer_color": True,
        "edge_smooth_value": 0.0,
        "spill_value": 0.0,
        "version": "v2",
    })

    canvas_id = _uid()
    _ensure_material_list(mats, "canvases").append({
        "id": canvas_id,
        "type": "canvas_color",
        "color": "",
        "blur": 0.0,
        "image": "",
        "album_image": "",
        "image_id": "",
        "image_name": "",
        "source_platform": 0,
        "team_id": "",
    })

    scm_id = _uid()
    _ensure_material_list(mats, "sound_channel_mappings").append({
        "id": scm_id, "type": "none", "audio_channel_mapping": 0, "is_config_open": False,
    })

    color_id = _uid()
    _ensure_material_list(mats, "material_colors").append({
        "id": color_id,
        "is_color_clip": False,
        "is_gradient": False,
        "solid_color": "",
        "gradient_colors": [],
        "gradient_percents": [],
        "gradient_angle": 90.0,
        "width": 0.0,
        "height": 0.0,
    })

    vocal_id = _uid()
    _ensure_material_list(mats, "vocal_separations").append({
        "id": vocal_id, "type": "vocal_separation", "choice": 0, "removed_sounds": [],
        "time_range": None, "production_path": "", "final_algorithm": "", "enter_from": "",
    })

    return {
        "id": _uid(),
        "source_timerange": {"start": 0, "duration": int(duration_us)},
        "target_timerange": {"start": int(start_us), "duration": int(duration_us)},
        "render_timerange": {"start": 0, "duration": 0},
        "desc": "",
        "state": 0,
        "speed": 1.0,
        "is_loop": False,
        "is_tone_modify": False,
        "reverse": False,
        "intensifies_audio": False,
        "cartoon": False,
        "volume": 1.0,
        "last_nonzero_volume": 1.0,
        "clip": {
            "scale": {"x": SUBSCRIBE_CLIP_SCALE, "y": SUBSCRIBE_CLIP_SCALE},
            "rotation": 0.0,
            "rotation3d": [],
            "transform": {"x": SUBSCRIBE_CLIP_X, "y": SUBSCRIBE_CLIP_Y},
            "flip": {"vertical": False, "horizontal": False},
            "alpha": 1.0,
        },
        "uniform_scale": {"on": True, "value": 1.0},
        "material_id": video_mat["id"],
        "extra_material_refs": [
            speed_id, placeholder_id, chroma_id, canvas_id,
            scm_id, color_id, vocal_id,
        ],
        "render_index": 1,
        "keyframe_refs": [],
        "enable_lut": True,
        "enable_adjust": True,
        "enable_hsl": False,
        "visible": True,
        "group_id": "",
        "enable_color_curves": True,
        "enable_hsl_curves": True,
        "track_render_index": 1,
        "hdr_settings": {"mode": 1, "intensity": 1.0, "nits": 1000},
        "hdr_vivid_settings": None,
        "enable_color_wheels": True,
        "track_attribute": 0,
        "is_placeholder": False,
        "template_id": "",
        "enable_smart_color_adjust": False,
        "template_scene": "default",
        "common_keyframes": [],
        "caption_info": None,
        "responsive_layout": {
            "enable": False,
            "target_follow": "",
            "size_layout": 0,
            "horizontal_pos_layout": 0,
            "vertical_pos_layout": 0,
        },
        "enable_color_match_adjust": False,
        "enable_color_correct_adjust": False,
        "enable_adjust_mask": False,
        "raw_segment_id": "",
        "lyric_keyframes": None,
        "enable_video_mask": True,
        "digital_human_template_group_id": "",
        "color_correct_alg_result": "",
        "source": "segmentsourcenormal",
        "enable_mask_stroke": False,
        "enable_mask_shadow": False,
        "enable_color_adjust_pro": False,
        "segment_color_tag": "",
        "lumi_occlusion": None,
    }


def _find_or_create_subscribe_track(data: dict) -> dict:
    """Возвращает дорожку оверлея сразу над основной видеодорожкой (и очищает старые клипы)."""
    tracks = data.setdefault("tracks", [])
    _, main_track = pick_main_track(tracks)
    main_pos = tracks.index(main_track)

    for pos, t in enumerate(tracks):
        if isinstance(t, dict) and t.get("type") == "video" and t.get("name") == SUBSCRIBE_TRACK_NAME:
            old_ids: set[str] = set()
            for seg in t.get("segments") or []:
                if isinstance(seg, dict):
                    if seg.get("material_id"):
                        old_ids.add(seg["material_id"])
                    for ref in seg.get("extra_material_refs") or []:
                        old_ids.add(ref)
            t["segments"] = []
            _cleanup_orphaned_materials(data, old_ids, keys=_VIDEO_MATERIAL_KEYS)
            if pos != main_pos + 1:
                tracks.remove(t)
                main_pos = tracks.index(main_track)
                tracks.insert(min(main_pos + 1, len(tracks)), t)
            return t

    track = {
        "id": _uid(),
        "type": "video",
        "flag": 2,
        "attribute": 0,
        "name": SUBSCRIBE_TRACK_NAME,
        "is_default_name": False,
        "segments": [],
    }
    tracks.insert(min(main_pos + 1, len(tracks)), track)
    return track


def plan_subscribe_placements(
    data: dict,
    scripts: list[tuple[int, str]],
    clip_duration_us: int,
    middle_tolerance_us: int = SUBSCRIBE_MIDDLE_TOLERANCE_US,
    comment_pattern: str = SUBSCRIBE_COMMENT_RE,
) -> dict:
    """
    Планирует три места для видео с подпиской:
      1-2. Первый и последний кадр сценария со словом «подпишись» (все формы).
      3.   Кадр с призывом к комментариям в пределах ±middle_tolerance от середины,
           иначе — ровно середина ролика (оверлей центрируется).
    """
    video_dur = get_video_duration_us(data)
    clip_dur = max(1, min(int(clip_duration_us or SUBSCRIBE_DEFAULT_DURATION_US), int(video_dur)))
    middle = video_dur // 2
    placements: list[dict] = []
    warnings: list[str] = []

    def prompt_time(num: int) -> tuple[int, int] | None:
        try:
            timerange = _resolve_prompt_segment(data, num).get("target_timerange") or {}
        except DraftError as exc:
            warnings.append(str(exc))
            return None
        return max(0, int(timerange.get("start") or 0)), max(0, int(timerange.get("duration") or 0))

    def add(start_us: int, reason: str, prompt: int | None = None, script: str = "") -> bool:
        start = max(0, min(int(start_us), max(0, video_dur - clip_dur)))
        for placed in placements:
            if abs(placed["start_us"] - start) < min(clip_dur, placed["duration_us"]):
                warnings.append(
                    "Места для видео с подпиской пересекаются — оставлен только один оверлей."
                )
                return False
        placements.append({
            "start_us": start,
            "duration_us": clip_dur,
            "reason": reason,
            "prompt": prompt,
            "script": script,
        })
        return True

    subscribe_hits: list[tuple[int, int, str]] = []
    for num, script in scripts:
        if script and re.search(SUBSCRIBE_MARKER_RE, script, re.IGNORECASE):
            timerange = prompt_time(num)
            if timerange is not None:
                subscribe_hits.append((timerange[0], num, script))
    subscribe_hits.sort()

    if subscribe_hits:
        add(subscribe_hits[0][0], "subscribe", subscribe_hits[0][1], subscribe_hits[0][2])
        if len(subscribe_hits) > 1:
            add(subscribe_hits[-1][0], "subscribe", subscribe_hits[-1][1], subscribe_hits[-1][2])
        else:
            warnings.append(
                "В сценарии найдено только одно упоминание подписки — "
                "поставлю один оверлей по нему и один дополнительный (середина/комментарии)."
            )
    else:
        warnings.append(
            "В сценарии не найдено слово «подпишись» — "
            "видео с подпиской будет поставлено только посередине ролика."
        )

    comment_hit: tuple[int, int, str] | None = None
    try:
        comment_rx = re.compile(comment_pattern or SUBSCRIBE_COMMENT_RE, re.IGNORECASE)
    except re.error as exc:
        raise DraftError(f"Некорректный шаблон призыва к комментариям: {comment_pattern} ({exc})") from exc

    for num, script in scripts:
        if not script or not comment_rx.search(script):
            continue
        timerange = prompt_time(num)
        if timerange is None:
            continue
        start = timerange[0]
        if abs(start - middle) <= max(0, int(middle_tolerance_us)):
            if comment_hit is None or abs(start - middle) < abs(comment_hit[0] - middle):
                comment_hit = (start, num, script)

    if comment_hit is not None:
        if add(comment_hit[0], "comment", comment_hit[1], comment_hit[2]):
            warnings.append(
                f"Третье место — кадр #{comment_hit[1]} с призывом к комментариям "
                f"({abs(comment_hit[0] - middle) / 1e6:.0f} с от середины)."
            )
    else:
        add(middle - clip_dur // 2, "middle")

    placements.sort(key=lambda p: p["start_us"])
    return {
        "video_duration_us": video_dur,
        "middle_us": middle,
        "placements": placements,
        "warnings": warnings,
    }


def add_subscribe_video(
    data: dict,
    video_path: Path,
    prompts_path: Path | str,
    middle_tolerance_us: int = SUBSCRIBE_MIDDLE_TOLERANCE_US,
    comment_pattern: str = SUBSCRIBE_COMMENT_RE,
) -> dict:
    """Добавляет видео с призывом подписаться верхним слоем (3 места)."""
    if video_path is None:
        raise DraftError("Не задан видеофайл с кнопкой подписки.")
    if not prompts_path:
        raise DraftError(
            "Для расстановки видео с подпиской нужен файл промптов кадров "
            "(в нём ищутся слова «подпишись» и «комментарии»)."
        )
    video_path = Path(video_path)
    scripts = parse_prompt_scripts(Path(prompts_path))

    # сначала запоминаем путь Chroma2, пока не очистили старые материалы оверлея
    chroma_path = _chroma_resource_path(data)
    info = probe_video_info(video_path)
    plan = plan_subscribe_placements(
        data,
        scripts,
        clip_duration_us=info["duration_us"],
        middle_tolerance_us=middle_tolerance_us,
        comment_pattern=comment_pattern,
    )

    if not plan["placements"]:
        plan["warnings"].append("Не удалось определить ни одного места для видео с подпиской.")
    else:
        track = _find_or_create_subscribe_track(data)
        mats = data.setdefault("materials", {})
        for placement in plan["placements"]:
            track["segments"].append(_create_video_segment(
                mats,
                video_path,
                info,
                start_us=placement["start_us"],
                duration_us=placement["duration_us"],
                chroma_path=chroma_path,
            ))

    return {
        "video_path": str(video_path),
        "duration_us": info["duration_us"],
        "track": SUBSCRIBE_TRACK_NAME,
        "placements": plan["placements"],
        "warnings": plan["warnings"],
    }


def parse_prompt_scripts(path: Path) -> list[tuple[int, str]]:
    """
    Читает файл промптов кадров и возвращает [(номер_промпта, фрагмент_сценария), ...].

    Формат файла:
        [PROMPT #1]
        <описание кадра>
        [<фрагмент сценария>]
    """
    path = Path(path)
    if not path.is_file():
        raise DraftError(f"Файл промптов не найден: {path}")

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    headers = list(_PROMPT_HEADER_RE.finditer(text))
    if not headers:
        raise DraftError(
            f"В файле {path.name} не найдено ни одного блока [PROMPT #N]. "
            "Нужен файл промптов кадров со сценарием в [...] под каждым промптом."
        )

    out: list[tuple[int, str]] = []
    for i, hdr in enumerate(headers):
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[hdr.end():body_end]
        brackets = _SCRIPT_BLOCK_RE.findall(body)
        script = brackets[-1].strip() if brackets else ""
        out.append((int(hdr.group(1)), script))
    return out


def find_intro_end_script(
    scripts: list[tuple[int, str]],
    marker_pattern: str = DEFAULT_ANCHOR_MARKERS,
) -> tuple[int, str] | None:
    """Первая пара (номер промпта, сценарий), в которой встречается фраза-маркер конца интро."""
    try:
        rx = re.compile(marker_pattern, re.IGNORECASE)
    except re.error as exc:
        raise DraftError(f"Некорректный шаблон маркера конца интро: {marker_pattern} ({exc})") from exc

    for num, script in scripts:
        if script and rx.search(script):
            return num, script
    return None


def _material_display_name(mat: dict) -> str:
    name = str(mat.get("material_name") or mat.get("name") or "")
    if name:
        return name
    path = str(mat.get("path") or "")
    return Path(path.replace("\\", "/")).name if path else ""


def _prompt_number_from_name(name: str) -> int | None:
    """«D:/.../videos/12.mp4» → 12; для произвольных имён → None."""
    if not name:
        return None
    stem = Path(name.replace("\\", "/")).stem.strip()
    return int(stem) if stem.isdigit() else None


def _numbered_clip_positions(data: dict) -> tuple[list[dict], dict[int, int]]:
    """Клипы основной видеодорожки и карта «номер промпта → индекс клипа»."""
    _, main_track = pick_main_track(data.get("tracks", []))
    segments = [s for s in (main_track.get("segments") or []) if isinstance(s, dict)]
    names = {
        m.get("id"): _material_display_name(m)
        for m in (data.get("materials", {}) or {}).get("videos", [])
        if isinstance(m, dict) and m.get("id")
    }

    positions: dict[int, int] = {}
    for idx, seg in enumerate(segments):
        num = _prompt_number_from_name(names.get(seg.get("material_id"), ""))
        if num is not None and num not in positions:
            positions[num] = idx
    return segments, positions


def _resolve_prompt_segment(data: dict, prompt_num: int) -> dict:
    """Находит клип основной видеодорожки, соответствующий промпту N."""
    segments, positions = _numbered_clip_positions(data)
    if not segments:
        raise DraftError("В проекте нет клипов на основной видеодорожке.")
    if not positions:
        raise DraftError(
            "Не удалось сопоставить промпты с клипами: на основной дорожке нет клипов "
            "с числовыми именами (1.mp4, 2.mp4, …)."
        )

    idx = positions.get(prompt_num)
    if idx is None:
        lower = [n for n in positions if n < prompt_num]
        upper = [n for n in positions if n > prompt_num]
        candidates: list[int] = []
        if lower:
            n_lo = max(lower)
            cand = positions[n_lo] + (prompt_num - n_lo)
            if 0 <= cand < len(segments) and (not upper or cand < positions[min(upper)]):
                candidates.append(cand)
        if upper:
            n_hi = min(upper)
            cand = positions[n_hi] - (n_hi - prompt_num)
            if 0 <= cand < len(segments) and (not lower or cand > positions[max(lower)]):
                candidates.append(cand)
        if not candidates:
            raise DraftError(
                f"Не удалось найти кадр для промпта #{prompt_num}: в проекте нет клипа "
                f"«{prompt_num}.mp4», а соседние клипы не позволяют вычислить позицию."
            )
        idx = candidates[0]

    return segments[idx]


def find_prompt_segment_start_us(data: dict, prompt_num: int) -> int:
    """
    Время (мкс) начала клипа, соответствующего промпту N.

    Клипы нумеруются по имени файла (1.mp4, 2.mp4 …). Если клип промпта заменён
    стоковым видео без номера, позиция вычисляется по соседним нумерованным клипам.
    """
    timerange = _resolve_prompt_segment(data, prompt_num).get("target_timerange") or {}
    return max(0, int(timerange.get("start") or 0))


def _suggest_anchor_candidates(data: dict, scripts: list[tuple[int, str]], limit: int = 5) -> list[str]:
    """Подсказки для пользователя: кадры, где встречается что-то похожее на «начн…»."""
    try:
        rx = re.compile(r"\bнач\w+", re.IGNORECASE)
    except re.error:
        return []

    lines: list[str] = []
    for num, script in scripts:
        if not script or not rx.search(script):
            continue
        try:
            start_us = find_prompt_segment_start_us(data, num)
            when = f"{start_us / 1e6:.2f} с"
        except DraftError:
            when = "? с"
        snippet = script.strip()
        if len(snippet) > 70:
            snippet = snippet[:70] + "…"
        lines.append(f"  кадр #{num} ({when}): «{snippet}»")
        if len(lines) >= limit:
            break
    return lines


def resolve_intro_anchor(
    data: dict,
    prompts_path: Path | str | None = None,
    marker_pattern: str = DEFAULT_ANCHOR_MARKERS,
    anchor_prompt: int | None = None,
) -> dict:
    """
    Определяет кадр, на котором должно закончиться интро.

    Возвращает {"prompt": N, "script": "...", "start_us": ...}.
    """
    prompt_num: int | None = None
    script = ""

    if anchor_prompt is not None:
        prompt_num = int(anchor_prompt)
        if prompt_num < 1:
            raise DraftError("Номер кадра (промпта) должен быть >= 1.")
        if prompts_path:
            try:
                for num, txt in parse_prompt_scripts(Path(prompts_path)):
                    if num == prompt_num:
                        script = txt
                        break
            except DraftError:
                pass
    elif prompts_path:
        scripts = parse_prompt_scripts(Path(prompts_path))
        found = find_intro_end_script(scripts, marker_pattern)
        if found is None:
            message = [
                "В файле промптов не найдена фраза конца интро "
                f"(шаблон поиска: {marker_pattern}).",
                "Укажите кадр вручную (поле «Кадр №» / --anchor-prompt) "
                "или свой шаблон (поле «Маркер» / --anchor-markers).",
            ]
            hints = _suggest_anchor_candidates(data, scripts)
            if hints:
                message.append("Похожие кадры — проверьте, какой из них нужен:")
                message.extend(hints)
            raise DraftError("\n".join(message))
        prompt_num, script = found

    if prompt_num is None:
        raise DraftError("Не задан ни файл промптов, ни номер кадра для конца интро.")

    return {
        "prompt": prompt_num,
        "script": script,
        "start_us": find_prompt_segment_start_us(data, prompt_num),
    }


def add_intro_and_ambient(
    data: dict,
    intro_path: Path | None,
    ambient_path: Path | None,
    volume_db: float = -7.0,
    fade_us: int = 4_000_000,
    intro_anchor_us: int | None = None,
    intro_tail_trim_us: int = INTRO_TAIL_TRIM_US,
    intro_fade_in_us: int = INTRO_FADE_IN_US,
    intro_fade_out_us: int = INTRO_FADE_OUT_US,
    ambient_fade_in_us: int = AMBIENT_FADE_IN_US,
) -> dict:
    """
    Добавляет intro и ambient на авто-дорожку.

    intro_anchor_us — время, на котором интро должно закончиться (кадр «начнём с…»).
    Если якорь задан, интро играет с самого начала (source_start_us = 0) так, чтобы его
    конец совпал с этим кадром: длинное интро подрезается с конца (начало не обрезается),
    а короткое замедляется (speed < 1), чтобы растянуться ровно до кадра.
    Последние intro_tail_trim_us секунд файла intro не используются.
    Для intro и ambient Fade In по умолчанию отключён (0): добавляется только Fade Out.
    """
    video_dur = get_video_duration_us(data)
    mats = data.setdefault("materials", {})
    track = _find_or_create_auto_audio_track(data)
    vol = db_to_linear(volume_db)

    summary = {
        "video_duration_us": video_dur,
        "anchor_us": intro_anchor_us,
        "intro": None,
        "ambient": None,
        "warnings": [],
    }
    cursor_us = 0

    if intro_path is not None:
        intro_full_dur = probe_audio_duration_us(intro_path)
        usable_us = intro_full_dur - max(0, int(intro_tail_trim_us))
        if usable_us <= 0:
            raise DraftError(
                f"Intro длится {intro_full_dur / 1e6:.2f} с — после отрезания хвоста "
                f"{max(0, int(intro_tail_trim_us)) / 1e6:.2f} с не остаётся звука. "
                "Уменьшите «Хвост intro» или проверьте файл."
            )

        anchor_us = None
        if intro_anchor_us is not None:
            anchor_us = max(0, min(int(video_dur), int(intro_anchor_us)))

        source_start_us = 0
        source_dur_us = usable_us
        seg_dur = min(usable_us, video_dur)
        seg_start = 0
        speed = 1.0

        if anchor_us is not None:
            if usable_us > anchor_us:
                # Звук длиннее кадра: начало не обрезаем, подрезаем конец ровно под кадр.
                source_start_us = 0
                source_dur_us = anchor_us
                seg_dur = anchor_us
            elif usable_us < anchor_us:
                # Звук короче кадра: замедляем, растягивая ровно до кадра.
                speed = usable_us / anchor_us
                if speed < MIN_INTRO_SPEED:
                    speed = MIN_INTRO_SPEED
                    seg_dur = round(source_dur_us / speed)
                    summary["warnings"].append(
                        f"Чтобы попасть в кадр, интро нужно замедлить в "
                        f"{anchor_us / usable_us:.1f} раза, но максимум — "
                        f"в {1 / MIN_INTRO_SPEED:.0f} раза: звук закончится раньше кадра "
                        f"на {max(0, anchor_us - seg_dur) / 1e6:.2f} с."
                    )
                else:
                    seg_dur = anchor_us
            else:
                seg_dur = anchor_us

        if seg_dur > 0:
            fade_in = min(max(0, int(intro_fade_in_us)), seg_dur)
            fade_out = min(max(0, int(intro_fade_out_us)), seg_dur)
            seg = _create_audio_segment(
                mats, intro_path, intro_full_dur,
                target_start_us=seg_start,
                target_duration_us=seg_dur,
                volume_linear=vol,
                fade_in_us=fade_in,
                fade_out_us=fade_out,
                source_start_us=source_start_us,
                source_duration_us=source_dur_us,
                speed=speed,
            )
            track["segments"].append(seg)
            cursor_us = seg_start + seg_dur
            summary["intro"] = {
                "start_us": seg_start,
                "duration_us": seg_dur,
                "source_start_us": source_start_us,
                "source_duration_us": source_dur_us,
                "speed": speed,
                "fade_in_us": fade_in,
                "fade_out_us": fade_out,
                "trimmed_tail_us": max(0, int(intro_tail_trim_us)),
            }

    if ambient_path is not None:
        ambient_full_dur = probe_audio_duration_us(ambient_path)
        remaining_us = max(0, video_dur - cursor_us)
        ambient_dur = min(ambient_full_dur, remaining_us)
        if ambient_dur > 0:
            fade_in = min(max(0, int(ambient_fade_in_us)), ambient_dur)
            fade_out = min(int(fade_us), ambient_dur)
            seg = _create_audio_segment(
                mats, ambient_path, ambient_full_dur,
                target_start_us=cursor_us,
                target_duration_us=ambient_dur,
                volume_linear=vol,
                fade_in_us=fade_in,
                fade_out_us=fade_out,
            )
            track["segments"].append(seg)
            summary["ambient"] = {
                "start_us": cursor_us,
                "duration_us": ambient_dur,
                "fade_in_us": fade_in,
                "fade_out_us": fade_out,
            }

    return summary


def enable_voice_normalization(
    data: dict,
    voice_pattern: str = "voice",
    target_lufs: float = -23.0,
) -> list[dict]:
    """Включает нормализацию (materials.loudnesses) для аудиосегментов озвучки."""
    mats = data.setdefault("materials", {})
    audios_by_id = {
        a["id"]: a for a in mats.get("audios", [])
        if isinstance(a, dict) and "id" in a
    }
    loudnesses = _ensure_material_list(mats, "loudnesses")
    loud_by_id = {l["id"]: l for l in loudnesses if isinstance(l, dict) and "id" in l}

    pat = voice_pattern.strip().lower()
    if not pat:
        return []

    updated = []
    for t in data.get("tracks", []):
        if not isinstance(t, dict) or t.get("type") != "audio":
            continue
        if t.get("name") == AUTO_AUDIO_TRACK_NAME:
            continue
        for seg in t.get("segments") or []:
            mat = audios_by_id.get(seg.get("material_id"))
            if not mat:
                continue
            haystack = f"{mat.get('name', '')} {mat.get('path', '')}".lower()
            if pat not in haystack:
                continue

            tr = seg.get("target_timerange") or {}
            seg_dur = int(tr.get("duration") or mat.get("duration") or 0)
            refs = seg.setdefault("extra_material_refs", [])

            loud_obj = None
            for ref in refs:
                if ref in loud_by_id:
                    loud_obj = loud_by_id[ref]
                    break

            if loud_obj is None:
                loud_obj = {
                    "id": _uid(),
                    "enable": True,
                    "time_range": {"start": 0, "duration": seg_dur},
                    "file_id": "",
                    "target_loudness": float(target_lufs),
                    "loudness_param": None,
                }
                loudnesses.append(loud_obj)
                loud_by_id[loud_obj["id"]] = loud_obj
                refs.append(loud_obj["id"])
            else:
                loud_obj["enable"] = True
                loud_obj["target_loudness"] = float(target_lufs)
                if not loud_obj.get("time_range"):
                    loud_obj["time_range"] = {"start": 0, "duration": seg_dur}

            src_path = Path(mat.get("path", "")) if mat.get("path") else None
            measured = measure_loudness_ebur128(src_path) if src_path else None
            if measured is not None:
                loud_obj["loudness_param"] = measured

            updated.append({
                "name": mat.get("name", ""),
                "target_lufs": float(target_lufs),
                "measured": measured,
            })

    return updated


def _resolve_fade_out_effect_path(data: dict) -> str:
    for anim_group in data.get("materials", {}).get("material_animations", []):
        if not isinstance(anim_group, dict):
            continue
        for anim in anim_group.get("animations") or []:
            if isinstance(anim, dict) and anim.get("id") == FADE_OUT_EFFECT_ID and anim.get("path"):
                return anim["path"]

    effect_dir = default_cache_effect_dir() / FADE_OUT_EFFECT_ID
    if effect_dir.is_dir():
        for sub in sorted(effect_dir.iterdir()):
            if sub.is_dir() and not sub.name.endswith("_tmp"):
                return sub.as_posix()

    return (effect_dir / FADE_OUT_FALLBACK_HASH).as_posix()


def apply_last_clip_fade_out(data: dict, fade_us: int = 4_000_000) -> dict | None:
    """Добавляет анимацию Fade Out (fade_us) на последний клип основной видеодорожки."""
    _, main_track = pick_main_track(data.get("tracks", []))
    segs = main_track.get("segments") or []
    if not segs:
        return None

    last_seg = segs[-1]
    seg_dur = int((last_seg.get("target_timerange") or {}).get("duration") or 0)
    if seg_dur <= 0:
        return None

    anim_dur = min(int(fade_us), seg_dur)
    anim_start = max(0, seg_dur - anim_dur)
    effect_path = _resolve_fade_out_effect_path(data)

    anim_entry = {
        "id": FADE_OUT_EFFECT_ID,
        "type": "out",
        "start": anim_start,
        "duration": anim_dur,
        "path": effect_path,
        "platform": "all",
        "resource_id": FADE_OUT_EFFECT_ID,
        "third_resource_id": FADE_OUT_EFFECT_ID,
        "source_platform": 1,
        "name": "Fade Out",
        "category_id": "2037708371",
        "category_name": "Trending-2",
        "panel": "video",
        "material_type": "video",
        "anim_adjust_params": None,
        "request_id": "",
    }

    mats = data.setdefault("materials", {})
    mat_anims = _ensure_material_list(mats, "material_animations")
    anims_by_id = {a["id"]: a for a in mat_anims if isinstance(a, dict) and "id" in a}
    refs = last_seg.setdefault("extra_material_refs", [])

    target_group = None
    for ref in refs:
        if ref in anims_by_id:
            target_group = anims_by_id[ref]
            break

    if target_group is None:
        target_group = {
            "id": _uid(),
            "type": "sticker_animation",
            "animations": [anim_entry],
            "multi_language_current": "none",
        }
        mat_anims.append(target_group)
        refs.append(target_group["id"])
    else:
        existing = [
            a for a in (target_group.get("animations") or [])
            if not (isinstance(a, dict) and a.get("type") == "out")
        ]
        existing.append(anim_entry)
        target_group["animations"] = existing

    return {"seg_duration_us": seg_dur, "start_us": anim_start, "duration_us": anim_dur}


def process_project(
    project: Path | str,
    intro_path: Path | None = None,
    ambient_path: Path | None = None,
    voice_pattern: str = "voice",
    volume_db: float = -7.0,
    fade_sec: float = 4.0,
    target_lufs: float = -23.0,
    normalize_voice: bool = True,
    video_fade_out: bool = True,
    dry_run: bool = False,
    prompts_path: Path | str | None = None,
    anchor_markers: str = DEFAULT_ANCHOR_MARKERS,
    anchor_prompt: int | None = None,
    intro_tail_sec: float = INTRO_TAIL_TRIM_US / 1_000_000,
    intro_fade_in_sec: float = INTRO_FADE_IN_US / 1_000_000,
    intro_fade_out_sec: float = INTRO_FADE_OUT_US / 1_000_000,
    ambient_fade_in_sec: float = AMBIENT_FADE_IN_US / 1_000_000,
    subscribe_path: Path | str | None = None,
    subscribe_middle_window_sec: float = SUBSCRIBE_MIDDLE_TOLERANCE_US / 1_000_000,
    subscribe_comment_markers: str = SUBSCRIBE_COMMENT_RE,
) -> dict:
    """Выполняет настройку аудио, оверлея с подпиской и финального Fade Out."""
    if not dry_run and capcut_is_running():
        raise DraftError("CapCut сейчас запущен! Закройте CapCut перед изменением проекта.")

    draft_path = find_draft_file(project)
    data = load_draft(draft_path)
    work = copy.deepcopy(data) if dry_run else data

    fade_us = round(fade_sec * 1_000_000)
    result = {
        "draft_path": str(draft_path),
        "audio": None,
        "voice": [],
        "video_fade": None,
        "anchor": None,
        "subscribe": None,
    }

    anchor_us = None
    if intro_path is not None and (prompts_path is not None or anchor_prompt is not None):
        anchor = resolve_intro_anchor(
            work,
            prompts_path=prompts_path,
            marker_pattern=anchor_markers or DEFAULT_ANCHOR_MARKERS,
            anchor_prompt=anchor_prompt,
        )
        result["anchor"] = anchor
        anchor_us = anchor["start_us"]

    if intro_path is not None or ambient_path is not None:
        result["audio"] = add_intro_and_ambient(
            work,
            intro_path=intro_path,
            ambient_path=ambient_path,
            volume_db=volume_db,
            fade_us=fade_us,
            intro_anchor_us=anchor_us,
            intro_tail_trim_us=round(max(0.0, intro_tail_sec) * 1_000_000),
            intro_fade_in_us=round(max(0.0, intro_fade_in_sec) * 1_000_000),
            intro_fade_out_us=round(max(0.0, intro_fade_out_sec) * 1_000_000),
            ambient_fade_in_us=round(max(0.0, ambient_fade_in_sec) * 1_000_000),
        )

    if normalize_voice:
        result["voice"] = enable_voice_normalization(
            work,
            voice_pattern=voice_pattern,
            target_lufs=target_lufs,
        )

    if video_fade_out:
        result["video_fade"] = apply_last_clip_fade_out(work, fade_us=fade_us)

    if subscribe_path is not None:
        result["subscribe"] = add_subscribe_video(
            work,
            video_path=Path(subscribe_path),
            prompts_path=prompts_path,
            middle_tolerance_us=round(max(0.0, subscribe_middle_window_sec) * 1_000_000),
            comment_pattern=subscribe_comment_markers or SUBSCRIBE_COMMENT_RE,
        )

    if not dry_run:
        backup = save_draft_with_backup(draft_path, work, prefix="audio")
        result["backup"] = str(backup)

    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Настройка intro, ambient, видео с подпиской, нормализации озвучки и Fade Out в CapCut"
    )
    ap.add_argument("project", help="Папка проекта CapCut или путь к draft_content.json")
    ap.add_argument("--intro", default=None, help="Путь к аудиофайлу intro")
    ap.add_argument("--ambient", default=None, help="Путь к аудиофайлу ambient")
    ap.add_argument("--subscribe", default=None,
                    help="Видео с кнопкой подписки (кладётся оверлеем в 3 местах)")
    ap.add_argument("--subscribe-middle-window", type=float,
                    default=SUBSCRIBE_MIDDLE_TOLERANCE_US / 1_000_000,
                    help="Допуск от середины для кадра с призывом к комментариям, сек (по умолч. 180)")
    ap.add_argument("--subscribe-comment-markers", default=None,
                    help=f"Regex призыва к комментариям (по умолч. {SUBSCRIBE_COMMENT_RE})")
    ap.add_argument("--prompts", default=None,
                    help="Файл промптов кадров (.txt со сценарием в [...] под каждым [PROMPT #N])")
    ap.add_argument("--anchor-markers", default=None,
                    help=f"Regex фразы конца интро (по умолч. {DEFAULT_ANCHOR_MARKERS})")
    ap.add_argument("--anchor-prompt", type=int, default=None,
                    help="Номер промпта/кадра, на котором должно закончиться интро (вместо поиска по фразе)")
    ap.add_argument("--intro-tail", type=float, default=INTRO_TAIL_TRIM_US / 1_000_000,
                    help="Сколько секунд хвоста intro не использовать (по умолч. 6.0)")
    ap.add_argument("--intro-fade-in", type=float, default=INTRO_FADE_IN_US / 1_000_000,
                    help="Fade In в начале intro, сек (по умолч. 0 — без нарастания)")
    ap.add_argument("--intro-fade-out", type=float, default=INTRO_FADE_OUT_US / 1_000_000,
                    help="Fade Out intro на переходе в ambient, сек (по умолч. 4.5)")
    ap.add_argument("--ambient-fade-in", type=float, default=AMBIENT_FADE_IN_US / 1_000_000,
                    help="Fade In ambient после intro, сек (по умолч. 0 — без нарастания)")
    ap.add_argument("--voice", default="voice", help="Подстрока для поиска дорожки озвучки (по умолч. voice)")
    ap.add_argument("--db", type=float, default=-7.0, help="Громкость intro/ambient в дБ (по умолч. -7.0)")
    ap.add_argument("--fade", type=float, default=4.0, help="Длительность fade-эффектов в сек (по умолч. 4.0)")
    ap.add_argument("--lufs", type=float, default=-23.0, help="Целевой уровень нормализации LUFS (по умолч. -23.0)")
    ap.add_argument("--no-norm", action="store_true", help="Не включать нормализацию озвучки")
    ap.add_argument("--no-video-fade", action="store_true", help="Не ставить Fade Out на последний видеоклип")
    ap.add_argument("--dry-run", action="store_true", help="Пробный прогон без записи файла")
    args = ap.parse_args(argv)

    intro = Path(args.intro) if args.intro else None
    ambient = Path(args.ambient) if args.ambient else None
    subscribe = Path(args.subscribe) if args.subscribe else None
    prompts = Path(args.prompts) if args.prompts else None

    try:
        res = process_project(
            args.project,
            intro_path=intro,
            ambient_path=ambient,
            voice_pattern=args.voice,
            volume_db=args.db,
            fade_sec=args.fade,
            target_lufs=args.lufs,
            normalize_voice=not args.no_norm,
            video_fade_out=not args.no_video_fade,
            dry_run=args.dry_run,
            prompts_path=prompts,
            anchor_markers=args.anchor_markers or DEFAULT_ANCHOR_MARKERS,
            anchor_prompt=args.anchor_prompt,
            intro_tail_sec=args.intro_tail,
            intro_fade_in_sec=args.intro_fade_in,
            intro_fade_out_sec=args.intro_fade_out,
            ambient_fade_in_sec=args.ambient_fade_in,
            subscribe_path=subscribe,
            subscribe_middle_window_sec=args.subscribe_middle_window,
            subscribe_comment_markers=args.subscribe_comment_markers or SUBSCRIBE_COMMENT_RE,
        )
    except DraftError as exc:
        print(f"[ОШИБКА] {exc}", file=sys.stderr)
        return 1

    print(f"Проект: {res['draft_path']}")
    if res.get("anchor"):
        an = res["anchor"]
        script = (an.get("script") or "").strip()
        if len(script) > 90:
            script = script[:90] + "…"
        print(f"  Конец интро: кадр #{an['prompt']} на {an['start_us']/1e6:.2f} с"
              + (f" — «{script}»" if script else ""))
    if res["audio"]:
        a = res["audio"]
        print(f"Видео: {a['video_duration_us'] / 1_000_000:.2f} с")
        if a["intro"]:
            i = a["intro"]
            speed = float(i.get("speed") or 1.0)
            speed_txt = f", скорость {speed:.3f}x (замедление)" if abs(speed - 1.0) > 1e-6 else ""
            print(f"  Intro: {i['start_us']/1e6:.2f}..{(i['start_us']+i['duration_us'])/1e6:.2f} с "
                  f"(отступ в файле {i['source_start_us']/1e6:.2f} с, хвост не используется: "
                  f"{i['trimmed_tail_us']/1e6:.2f} с{speed_txt}), "
                  f"громкость {args.db:+.1f} дБ, Fade In {i['fade_in_us']/1e6:.2f} с, "
                  f"Fade Out {i['fade_out_us']/1e6:.2f} с")
        if a["ambient"]:
            m = a["ambient"]
            print(f"  Ambient: {m['start_us']/1e6:.2f}..{(m['start_us']+m['duration_us'])/1e6:.2f} с, "
                  f"громкость {args.db:+.1f} дБ, Fade In {m['fade_in_us']/1e6:.2f} с, "
                  f"Fade Out {m['fade_out_us']/1e6:.2f} с")
        for warn in a.get("warnings") or []:
            print(f"  [!] {warn}")
    if not args.no_norm:
        if res["voice"]:
            for v in res["voice"]:
                meas = f" (avg={v['measured']['avg_loudness']:.1f} LUFS, peak={v['measured']['peak_loudness']:.1f} dBFS)" if v["measured"] else ""
                print(f"  Озвучка '{v['name']}': нормализация включена -> {v['target_lufs']} LUFS{meas}")
        else:
            print(f"  [!] Сегменты озвучки по маске '{args.voice}' не найдены.")
    if res["video_fade"]:
        vf = res["video_fade"]
        print(f"  Последний видеоклип: Fade Out {vf['duration_us']/1e6:.2f} с "
              f"(с {vf['start_us']/1e6:.2f} с клипа)")
    if res.get("subscribe"):
        sub = res["subscribe"]
        print(f"  Видео с подпиской: {Path(sub['video_path']).name} "
              f"({sub['duration_us']/1e6:.2f} с), дорожка «{sub['track']}»")
        reason_titles = {
            "subscribe": "по слову «подпишись»",
            "comment": "по призыву к комментариям",
            "middle": "ровно середина",
        }
        for p in sub["placements"]:
            prompt_txt = f", кадр #{p['prompt']}" if p.get("prompt") else ""
            print(f"    {p['start_us']/1e6:.2f}..{(p['start_us']+p['duration_us'])/1e6:.2f} с — "
                  f"{reason_titles.get(p['reason'], p['reason'])}{prompt_txt}")
        for warn in sub.get("warnings") or []:
            print(f"    [!] {warn}")
    if args.dry_run:
        print("Пробный прогон: файл проекта не изменён.")
    else:
        print(f"Готово! Бэкап: {res.get('backup')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
