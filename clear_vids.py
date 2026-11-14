#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clear_vids.py — подготовка ИИ-клипов в проекте CapCut:

  1) ставит масштаб 115% на все клипы основной видеодорожки;
  2) ищет вотермарку Gemini (4-лучевая звёздочка в правом нижнем углу) через OpenCV
     и у клипов, где она найдена, выравнивает клип по левому краю кадра
     (clip сдвигается вправо так, чтобы его левый край совпал с левым краем кадра,
     а правый край с вотермаркой ушёл за кадр).

Два вида звёздочек
------------------
В Gemini/Veo-видео бывают БОЛЬШАЯ звёздочка (≈48 px при ширине кадра 1280, чуть выше) и
МАЛЕНЬКАЯ (≈22 px, в самом углу; переходит с картинки). Бывает одна из них или обе сразу.
Каждый вид проверяется ОТДЕЛЬНО (свой размер, место и порог). Клип считается чистым, только
если не найдено НИ ОДНОЙ из двух; нашлась хотя бы одна — клип выравнивается по левому краю.
Один и тот же набор подходит для всех видео: все кадры приводятся к ширине 1280, поэтому
отдельные шаблоны под каждый клип не нужны.

Как проверяется каждый вид
--------------------------
Звёздочка полупрозрачная (~28%), и на «рваном» фоне (камни, листва) в одном кадре её почти не
видно. Но вотермарка стоит на месте, а фон двигается, поэтому берётся несколько кадров
(--probe-times). Вид считается найденным, если:
  • на ОДНОМ из кадров (в том числе через 0.1 с) звезда видна чётко (score ≥ --threshold-single), или
  • на СРЕДНЕМ кадре (фон размазан, звезда осталась чёткой) score ≥ порога вида (--threshold).

Использование
-------------
  # пробный прогон (ничего не пишет, только отчёт + картинки для проверки)
  python clear_vids.py "D:\\Для работы\\CapCut Drafts\\dfd_cat_1790873381_bdf368e2" --dry-run --debug-dir debug

  # боевой запуск (CapCut должен быть ЗАКРЫТ; делается бэкап draft_content.json)
  python clear_vids.py "D:\\Для работы\\CapCut Drafts\\dfd_cat_1790873381_bdf368e2"

Зависимости: pip install opencv-python numpy
"""

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from capcut_common import (
    DraftError,
    capcut_is_running,
    find_draft_file,
    load_draft,
    pick_main_track,
    save_draft_with_backup,
)

REF_W = 1280  # все кадры приводятся к ширине 1280

# Места вотермарок: (x центра, y центра, ширина звезды) — доли ширины/высоты кадра 16:9.
# Измерено на реальном Gemini-видео 1280x720: большая ≈ (1160, 600) 48 px; маленькая ≈ (1236, 669) 22 px.
DEFAULT_MARKS = [
    (0.906, 0.833, 0.0371),  # большая
    (0.966, 0.929, 0.0176),  # маленькая
]
MARK_NAMES = ["большая", "маленькая"]
# Порог по СРЕДНЕМУ кадру для каждого вида (подобран на реальных и смоделированных клипах);
# у маленькой выше, потому что мелкая форма чаще случайно «совпадает» с текстурой фона.
DEFAULT_STACK_THR = [0.46, 0.50]
DEFAULT_SINGLE_THR = 0.70  # порог для одного кадра (одиночный кадр легче даёт ложные совпадения)
SIZE_STEPS = (0.8, 0.9, 1.0, 1.1, 1.25)  # допуск по размеру звезды
STAR_EXP = 0.8                            # форма: |x|^p + |y|^p <= 1 (подогнано по реальной звёздочке)

_star_cache = {}
_star_lock = threading.Lock()


# ----------------------------------------------------------------------------
# Работа с видео и CV
# ----------------------------------------------------------------------------
def norm_path(p):
    """Путь из драфта (может быть с / или \\) -> путь ОС."""
    if os.name == "nt":
        return os.path.normpath(p)
    return os.path.normpath(p.replace("\\", "/"))


def read_frame(cap, t_sec):
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t_sec) * 1000.0)
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    h, w = frame.shape[:2]
    if w != REF_W:
        frame = cv2.resize(frame, (REF_W, int(round(h * REF_W / w))), interpolation=cv2.INTER_AREA)
    return frame


def star_template(size):
    """Шаблон: белая 4-лучевая звезда шириной size px на чёрном поле с запасом вокруг."""
    key = round(size, 1)
    with _star_lock:
        if key in _star_cache:
            return _star_cache[key]
    pad = 0.35
    k = 1 + 2 * pad
    n = int(round(size * k)) | 1
    N, ss = n * 4, 4
    yy, xx = np.mgrid[-1:1:complex(N), -1:1:complex(N)]
    m = ((np.abs(xx * k) ** STAR_EXP + np.abs(yy * k) ** STAR_EXP) <= 1).astype(np.float32)
    t = cv2.resize(m, (n, n), interpolation=cv2.INTER_AREA)
    with _star_lock:
        _star_cache[key] = t
    return t


def blur_gray(gray_f32):
    return cv2.GaussianBlur(gray_f32, (0, 0), 0.8)


def score_marks(gray, marks, margin):
    """
    Лучшее совпадение звезды вокруг каждого известного места.
    gray — float32 (кадр или усреднённый кадр). Возвращает (score, номер метки, (x, y, size)).
    """
    h, w = gray.shape[:2]
    g = blur_gray(gray)
    best = (-1.0, -1, None)
    for mi, (fx, fy, fs) in enumerate(marks):
        cx, cy, size = fx * w, fy * h, fs * w
        for rel in SIZE_STEPS:
            t = star_template(size * rel)
            n = t.shape[0]
            half = n // 2
            x0, y0 = int(cx - margin - half), int(cy - margin - half)
            x1, y1 = int(cx + margin + half + 1), int(cy + margin + half + 1)
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
            sub = g[y0:y1, x0:x1]
            if sub.shape[0] < n or sub.shape[1] < n:
                continue
            res = cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED)
            res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if mx > best[0]:
                best = (float(mx), mi, (x0 + loc[0], y0 + loc[1], n))
    return best


# ----------------------------------------------------------------------------
# Работа с драфтом CapCut
# ----------------------------------------------------------------------------
def left_align_shift(mat_w, mat_h, canvas_w, canvas_h, scale):
    """
    Значение clip.transform.x, при котором левый край клипа совпадает с левым
    краем кадра. В CapCut x измеряется в половинах ширины холста
    (x = 1.0 — сдвиг на пол-кадра вправо).
    Для 16:9 на холсте 16:9 получается scale - 1 (при 1.15 → 0.15).
    """
    if mat_h <= 0 or canvas_h <= 0 or canvas_w <= 0:
        return 0.0
    ar_clip, ar_canvas = mat_w / mat_h, canvas_w / canvas_h
    disp_w_frac = scale if ar_clip >= ar_canvas else scale * ar_clip / ar_canvas  # ширина клипа / ширина холста
    return max(0.0, disp_w_frac - 1.0)


def detect_watermark(mat, seg, marks, stack_thr, single_thr, margin, probe_times, debug_dir, tag, cache):
    """
    Читает кадры через probe_times секунд от начала клипа и проверяет КАЖДЫЙ вид звёздочки отдельно
    (по одиночным кадрам и по среднему кадру).
    Возвращает (найдена ли хоть одна, [результат по каждому виду]) либо (None, []) если видео не прочиталось.
    Результат вида: dict(name, found, single, stack).
    """
    path = norm_path(mat["path"])
    src = seg["source_timerange"]
    key = (path, src["start"], src["duration"])
    if key in cache:
        return cache[key]

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        cache[key] = (None, [])
        return cache[key]

    start = src["start"] / 1e6
    dur = src["duration"] / 1e6
    if not probe_times:
        eff_probes = [0.0]
    elif dur > 0 and len(probe_times) > 1 and dur < max(probe_times):
        # Для коротких клипов равномерно распределяем кадры по длине сегмента
        n_pts = min(len(probe_times), max(3, int(round(dur * 5))))
        eff_probes = [dur * (0.05 + 0.85 * idx / (n_pts - 1)) for idx in range(n_pts)]
    else:
        eff_probes = [
            min(pt, dur * 0.95) if dur > 0 else 0.0
            for i, pt in enumerate(probe_times)
            if i == 0 or pt <= dur * 0.95
        ]

    frames = []
    for pt in eff_probes:
        fr = read_frame(cap, start + pt)
        if fr is not None:
            frames.append(fr)
    cap.release()

    if not frames:
        cache[key] = (None, [])
        return cache[key]

    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames]
    stack = np.mean(grays, axis=0) if len(grays) > 1 else None

    results, vis_boxes = [], []
    for mi, mark in enumerate(marks):
        # лучший одиночный кадр
        single, single_box, single_i = -1.0, None, 0
        for gi, g in enumerate(grays):
            sc, _, box = score_marks(g, [mark], margin)
            if sc > single:
                single, single_box, single_i = sc, box, gi
        # средний кадр
        if stack is not None:
            st, _, st_box = score_marks(stack, [mark], margin)
        else:
            st, st_box = single, single_box
        found_single = single >= single_thr
        found_stack = st >= stack_thr[mi]
        results.append(dict(name=MARK_NAMES[mi] if mi < len(MARK_NAMES) else f"вид {mi + 1}",
                            found=found_single or found_stack, single=single, stack=st,
                            via=("кадр" if found_single else "среднее") if (found_single or found_stack) else ""))
        # какую рамку рисовать на картинке
        if found_single or (single / single_thr >= st / stack_thr[mi]):
            vis_boxes.append((single_box, frames[single_i], results[-1]))
        else:
            vis_boxes.append((st_box, np.clip(stack, 0, 255).astype(np.uint8), results[-1]))

    any_found = any(r["found"] for r in results)

    if debug_dir:
        base = frames[0].copy()
        for box, _, r in vis_boxes:
            if box is None:
                continue
            x, y, n = box
            color = (0, 255, 0) if r["found"] else (0, 0, 255)
            cv2.rectangle(base, (x, y), (x + n, y + n), color, 2)
        line = "  ".join(f"{r['name']} {r['single']:.2f}/{r['stack']:.2f}{'*' if r['found'] else ''}" for r in results)
        cv2.putText(base, line, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        h, w = base.shape[:2]
        os.makedirs(debug_dir, exist_ok=True)
        corner = base[int(h * 0.55):, int(w * 0.55):]
        if stack is not None:
            st_img = cv2.cvtColor(np.clip(stack, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)[int(h * 0.55):, int(w * 0.55):]
            corner = np.hstack([corner, st_img])  # справа — средний кадр
        cv2.imwrite(os.path.join(debug_dir, f"{tag}_{'WM' if any_found else 'clean'}.jpg"), corner)
    cache[key] = (any_found, results)
    return cache[key]


def parse_marks(items):
    marks = []
    for it in items:
        try:
            x, y, s = [float(v) for v in it.split(",")]
        except ValueError:
            sys.exit(f"--marks: неверный формат '{it}', нужно X,Y,РАЗМЕР (доли кадра), напр. 0.906,0.833,0.037")
        marks.append((x, y, s))
    return marks


def main():
    ap = argparse.ArgumentParser(description="CapCut: масштаб 115% + выравнивание клипов с вотермаркой Gemini по левому краю")
    ap.add_argument("project", help="папка проекта CapCut (или путь к draft_content.json)")
    ap.add_argument("--scale", type=float, default=1.15, help="масштаб клипов (по умолч. 1.15)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="порог по СРЕДНЕМУ кадру, 0..1, для обоих видов (по умолч.: большая 0.46, маленькая 0.50). "
                         "Ниже — чувствительнее, но больше лишних выравниваний")
    ap.add_argument("--threshold-single", type=float, default=DEFAULT_SINGLE_THR,
                    help="порог по ОДНОМУ кадру (по умолч. 0.70)")
    ap.add_argument("--probe-times", type=float, nargs="+", default=[0.1, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0], metavar="SEC",
                    help="через сколько секунд от начала клипа брать кадры (по умолч. 0.1 0.5 1.0 ... 4.0). "
                         "Совпадение на любом кадре или на среднем кадре = вотермарка. "
                         "Только один кадр: --probe-times 0.1")
    ap.add_argument("--margin", type=int, default=12,
                    help="на сколько px (при ширине кадра 1280) искать звезду в стороне от ожидаемого места (по умолч. 12)")
    ap.add_argument("--marks", nargs="+", metavar="X,Y,РАЗМЕР",
                    help="свои места вотермарок в долях кадра, напр. 0.906,0.833,0.037 0.966,0.929,0.018")
    ap.add_argument("--track", type=int, default=None, help="индекс видеодорожки (по умолч. первая — основная)")
    ap.add_argument("--size", default=None, help="обрабатывать только клипы с таким разрешением, напр. 1280x720")
    ap.add_argument("--reset-clean", action="store_true",
                    help="у клипов БЕЗ вотермарки сбрасывать сдвиг по X в 0 (по умолч. не трогать)")
    ap.add_argument("--dry-run", action="store_true", help="ничего не записывать, только отчёт")
    ap.add_argument("--force", action="store_true", help="не проверять, запущен ли CapCut")
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1),
                    help="число потоков для анализа видео (по умолч. до 4)")
    ap.add_argument("--debug-dir", default=None, help="папка для картинок проверки (угол кадра + рамка найденной звезды)")
    args = ap.parse_args()

    marks = parse_marks(args.marks) if args.marks else DEFAULT_MARKS
    if args.threshold is not None:
        stack_thr = [args.threshold] * len(marks)
    elif args.marks:
        stack_thr = [0.48] * len(marks)
    else:
        stack_thr = list(DEFAULT_STACK_THR)

    try:
        draft_path = find_draft_file(args.project)
        if not args.dry_run and not args.force and capcut_is_running():
            raise DraftError(
                "CapCut запущен. Закрой его полностью (иначе он перезапишет изменения) "
                "или запусти с --force."
            )
        draft = load_draft(draft_path)
        track_idx = 0 if args.track is None else args.track
        idx, track = pick_main_track(draft["tracks"], track_idx)
    except DraftError as e:
        sys.exit(f"Ошибка: {e}")

    canvas_w = draft["canvas_config"]["width"]
    canvas_h = draft["canvas_config"]["height"]
    materials = {m["id"]: m for m in draft["materials"]["videos"]}
    segs = track["segments"]
    print(f"Проект: {draft.get('name') or draft_path.parent.name}")
    print(f"Холст: {canvas_w}x{canvas_h} | видеодорожка #{idx}: {len(segs)} сегментов | масштаб {args.scale:.2f} | "
          f"пороги (среднее/кадр): {'/'.join(f'{t:.2f}' for t in stack_thr)} / {args.threshold_single:.2f} | кадры: {', '.join(str(t) for t in args.probe_times)} с")

    want_size = None
    if args.size:
        try:
            parts = [int(v) for v in args.size.lower().split("x")]
            if len(parts) != 2 or parts[0] <= 0 or parts[1] <= 0:
                raise ValueError
            want_size = (parts[0], parts[1])
        except ValueError:
            sys.exit(f"--size: неверный формат '{args.size}', ожидается ШИРИНАxВЫСОТА, напр. 1280x720")

    cache = {}
    n_wm = n_clean = n_skip = n_err = 0
    t0 = time.time()
    order = sorted(range(len(segs)), key=lambda i: segs[i]["target_timerange"]["start"])

    # Предварительно отбираем сегменты и параллельно анализируем уникальные клипы
    active_items = []
    unique_tasks = {}
    for num, i in enumerate(order, 1):
        seg = segs[i]
        mat = materials.get(seg.get("material_id"))
        tag = f"{num:04d}"
        if mat is None or mat.get("type") not in ("video", None):
            n_skip += 1
            continue
        if want_size and (mat["width"], mat["height"]) != want_size:
            n_skip += 1
            continue
        if seg.get("common_keyframes") or seg.get("keyframe_refs"):
            print(f"[{tag}] пропуск: у сегмента есть ключевые кадры")
            n_skip += 1
            continue
        active_items.append((tag, seg, mat))
        src = seg["source_timerange"]
        key = (norm_path(mat["path"]), src["start"], src["duration"])
        if key not in unique_tasks:
            unique_tasks[key] = (mat, seg, tag)

    workers = max(1, args.workers)
    if workers > 1 and len(unique_tasks) > 1:
        def _run_one(item):
            k, (m, s, tg) = item
            return k, detect_watermark(
                m, s, marks, stack_thr, args.threshold_single, args.margin, args.probe_times,
                args.debug_dir, tg, {})

        with ThreadPoolExecutor(max_workers=min(workers, len(unique_tasks))) as pool:
            for k, res in pool.map(_run_one, unique_tasks.items()):
                cache[k] = res

    for tag, seg, mat in active_items:
        found, results = detect_watermark(
            mat, seg, marks, stack_thr, args.threshold_single, args.margin, args.probe_times,
            args.debug_dir, tag, cache)

        if found is None:
            print(f"[{tag}] ОШИБКА: не удалось прочитать видео {mat['path']}")
            n_err += 1
            # масштаб всё равно ставим — это от вотермарки не зависит
        clip = seg["clip"]
        clip["scale"]["x"] = args.scale
        clip["scale"]["y"] = args.scale

        if found is None:
            continue
        detail = " | ".join(
            f"{r['name']}: {'ЕСТЬ' if r['found'] else 'нет'} ({r['single']:.2f}/{r['stack']:.2f})" for r in results)
        if found:
            shift = left_align_shift(mat["width"], mat["height"], canvas_w, canvas_h, args.scale)
            clip["transform"]["x"] = shift
            n_wm += 1
            warn = " [сдвиг 0: клип уже холста]" if shift == 0.0 else ""
            print(f"[{tag}] ВОТЕРМАРКА  x={shift:.4f}{warn}  {detail}  {os.path.basename(mat['path'])}")
        else:
            if args.reset_clean:
                clip["transform"]["x"] = 0.0
            n_clean += 1
            print(f"[{tag}] чисто       {detail}  {os.path.basename(mat['path'])}")

    print("-" * 60)
    print(f"С вотермаркой: {n_wm} | чисто: {n_clean} | пропущено: {n_skip} | ошибок чтения: {n_err} | {time.time() - t0:.1f} c")

    if args.dry_run:
        print("--dry-run: файл не изменён.")
        return

    backup = save_draft_with_backup(draft_path, draft, prefix="clear_vids")
    print(f"Готово. Бэкап: {backup.name}")


if __name__ == "__main__":
    main()