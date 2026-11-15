#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_audio.py — автоматическая настройка аудио и финального Fade Out в проекте CapCut:
  1. Добавляет intro и ambient, громкость -7 дБ для обоих.
     Конец intro выравнивается по кадру видео, в сценарии которого звучит фраза-маркер
     («начнём с…», «начинаем…» и т. п.). Файл промптов кадров — обычный .txt вида
     «[PROMPT #N] <описание кадра> [<фрагмент сценария>]»; номер промпта связывается с
     клипом видео <N>.mp4 на основной дорожке проекта. Хвост intro (последние 6 сек)
     не используется — там плохой звук.
     Переход intro → ambient: Fade Out у intro 4.5 сек, Fade In у ambient 3 сек.
  2. Включает нормализацию звука (materials.loudnesses, -23 LUFS) на дорожке озвучки (по умолчанию 'voice').
  3. Добавляет анимацию Fade Out (4 сек) на последний видеоклип основной дорожки.
"""
import argparse
import copy
import math
import re
import subprocess
import sys
import uuid
from pathlib import Path

from capcut_common import (
    DraftError,
    capcut_is_running,
    default_cache_effect_dir,
    find_draft_file,
    load_draft,
    pick_main_track,
    save_draft_with_backup,
)

AUTO_AUDIO_TRACK_NAME = "CapCutAutomation Audio"
FADE_OUT_EFFECT_ID = "6798320902548230669"
FADE_OUT_FALLBACK_HASH = "c6f05ce62355b537be762550040bfc08"

# Интро: последние 6 сек не используем (плохой звук), переход в ambient — 4.5 / 3 сек.
INTRO_TAIL_TRIM_US = 6_000_000
INTRO_FADE_OUT_US = 4_500_000
AMBIENT_FADE_IN_US = 3_000_000
# Максимальное замедление интро, если оно короче кадра-якоря (0.25 = в 4 раза медленнее).
MIN_INTRO_SPEED = 0.25

# Фразы-маркеры конца интро во фрагментах сценария.
# «начнём», «начнем», «начинаем» — но НЕ «начинается», «начинал», «начинают».
DEFAULT_ANCHOR_MARKERS = r"\bнач(?:нём|нем|инаем)\b"

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


def _cleanup_orphaned_materials(data: dict, candidate_ids: set[str]) -> None:
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
    for key in (
        "audios", "speeds", "placeholder_infos", "audio_fades",
        "beats", "sound_channel_mappings", "vocal_separations",
    ):
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


def parse_prompt_scripts(path: Path) -> list[tuple[int, str]]:
    """
    Читает файл промптов кадров и возвращает [(номер_промпта, фрагмент_сценария), ...].

    Формат файла:
        [PROMPT #1]
        <описание кадра>
        [<фрагмент сценария>]
    """
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


def find_prompt_segment_start_us(data: dict, prompt_num: int) -> int:
    """
    Время (мкс) начала клипа, соответствующего промпту N.

    Клипы нумеруются по имени файла (1.mp4, 2.mp4 …). Если клип промпта заменён
    стоковым видео без номера, позиция вычисляется по соседним нумерованным клипам.
    """
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

    timerange = segments[idx].get("target_timerange") or {}
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
    intro_fade_out_us: int = INTRO_FADE_OUT_US,
    ambient_fade_in_us: int = AMBIENT_FADE_IN_US,
) -> dict:
    """
    Добавляет intro и ambient на авто-дорожку.

    intro_anchor_us — время, на котором интро должно закончиться (кадр «начнём с…»).
    Если якорь задан, интро играет так, чтобы его конец совпал с этим кадром:
    длинное интро подрезается с начала, а короткое замедляется (speed < 1),
    чтобы растянуться ровно до кадра. Последние intro_tail_trim_us секунд
    файла intro не используются. Переход: Fade Out у intro, Fade In у ambient.
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
                # Звук длиннее кадра: отрезаем начало, конец ровно в кадр.
                source_start_us = usable_us - anchor_us
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
            fade_in = min(int(fade_us), seg_dur)
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
    intro_fade_out_sec: float = INTRO_FADE_OUT_US / 1_000_000,
    ambient_fade_in_sec: float = AMBIENT_FADE_IN_US / 1_000_000,
) -> dict:
    """Выполняет настройку аудио и финального Fade Out для проекта CapCut."""
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

    if not dry_run:
        backup = save_draft_with_backup(draft_path, work, prefix="audio")
        result["backup"] = str(backup)

    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Настройка intro, ambient, нормализации озвучки и Fade Out в CapCut")
    ap.add_argument("project", help="Папка проекта CapCut или путь к draft_content.json")
    ap.add_argument("--intro", default=None, help="Путь к аудиофайлу intro")
    ap.add_argument("--ambient", default=None, help="Путь к аудиофайлу ambient")
    ap.add_argument("--prompts", default=None,
                    help="Файл промптов кадров (.txt со сценарием в [...] под каждым [PROMPT #N])")
    ap.add_argument("--anchor-markers", default=None,
                    help=f"Regex фразы конца интро (по умолч. {DEFAULT_ANCHOR_MARKERS})")
    ap.add_argument("--anchor-prompt", type=int, default=None,
                    help="Номер промпта/кадра, на котором должно закончиться интро (вместо поиска по фразе)")
    ap.add_argument("--intro-tail", type=float, default=INTRO_TAIL_TRIM_US / 1_000_000,
                    help="Сколько секунд хвоста intro не использовать (по умолч. 6.0)")
    ap.add_argument("--intro-fade-out", type=float, default=INTRO_FADE_OUT_US / 1_000_000,
                    help="Fade Out intro на переходе в ambient, сек (по умолч. 4.5)")
    ap.add_argument("--ambient-fade-in", type=float, default=AMBIENT_FADE_IN_US / 1_000_000,
                    help="Fade In ambient после intro, сек (по умолч. 3.0)")
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
            intro_fade_out_sec=args.intro_fade_out,
            ambient_fade_in_sec=args.ambient_fade_in,
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
    if args.dry_run:
        print("Пробный прогон: файл проекта не изменён.")
    else:
        print(f"Готово! Бэкап: {res.get('backup')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
