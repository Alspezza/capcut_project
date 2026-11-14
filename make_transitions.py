"""
make_transitions.py — расставляет переходы по паттерну между всеми клипами
основной видеодорожки CapCut-проекта, правя draft_content.json НАПРЯМУЮ.
Никакого capcut-mate и сторонних библиотек: только стандартная библиотека Python.

Запуск (CapCut ДОЛЖЕН быть закрыт):
    python make_transitions.py "D:\\Для работы\\CapCut Drafts\\dfd_cat_1792929295_d460a13e"
    python make_transitions.py <папка проекта> --dry-run
    python make_transitions.py <папка проекта> --skip-overlays

Из другого скрипта / GUI:
    from make_transitions import apply_transitions
    stats = apply_transitions(project_path, dry_run=False, skip_overlays=False, log=print)
"""
import argparse
import json
import math
import sys
import uuid
from pathlib import Path

from capcut_common import (
    DraftError as TransitionsError,
    capcut_is_running,
    default_cache_effect_dir,
    find_draft_file,
    load_draft,
    pick_main_track,
    save_draft_with_backup,
)

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------
DEFAULT_DURATIONS_MAP = {
    "7159450506648097281": int(1.0 * 1000000),  # Размытие шаром (1с)
    "7140916992118100482": int(1.0 * 1000000),  # Вертикальное размытие (1с)
    "7184693655641264641": int(0.9 * 1000000),  # Шестиугольный зум (0.9с)
    "7140916996262072833": int(0.8 * 1000000),  # Горизонтальное размытие (0.8с)
}


def load_transition_pattern() -> tuple[dict[str, int], list[str]]:
    """Читает transitions_config.json (если есть) или возвращает настройки по умолчанию."""
    base_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    cfg_path = base_dir / "transitions_config.json"
    durations = dict(DEFAULT_DURATIONS_MAP)
    pattern = list(durations.keys())
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(cfg.get("durations_sec"), dict) and cfg["durations_sec"]:
                parsed = {}
                for k, v in cfg["durations_sec"].items():
                    if isinstance(v, bool):
                        continue
                    sec = float(v)
                    if not math.isfinite(sec):
                        continue
                    dur_us = round(sec * 1_000_000)
                    if dur_us > 0:
                        parsed[str(k)] = dur_us
                if parsed:
                    durations = parsed
            if isinstance(cfg.get("pattern"), list) and cfg["pattern"]:
                pattern = [str(x) for x in cfg["pattern"] if str(x) in durations]
            else:
                pattern = [x for x in pattern if x in durations]
        except Exception as exc:
            print(f"[!] Ошибка чтения transitions_config.json ({exc}), использую настройки по умолчанию.")
    pattern = [x for x in pattern if x in durations]
    return durations, pattern or list(durations.keys())


DURATIONS_MAP, PATTERN = load_transition_pattern()

_CACHE_EFFECT_DIR = default_cache_effect_dir()


def _effect_path(effect_id: str, hash_dir: str) -> str:
    return (_CACHE_EFFECT_DIR / effect_id / hash_dir).as_posix()


# Запасные шаблоны. Если в проекте уже есть такой же переход — берутся свежие
# данные из проекта (имя, путь в кэше CapCut, is_overlap).
FALLBACK_TEMPLATES = {
    "7159450506648097281": {
        "name": "Размытие (шар)", "is_overlap": True,
        "path": _effect_path("7159450506648097281", "10478b300821fa6eccadf67b07b63208"),
    },
    "7140916992118100482": {
        "name": "Размытие по вертикали", "is_overlap": False,
        "path": _effect_path("7140916992118100482", "f55306a27ff8e0a8ae29584bb661ad73"),
    },
    "7184693655641264641": {
        "name": "Шестиугольный зум", "is_overlap": False,
        "path": _effect_path("7184693655641264641", "acfd328bcace433d08e7d64ae91261e3"),
    },
    "7140916996262072833": {
        "name": "Горизонтальное размытие", "is_overlap": True,
        "path": _effect_path("7140916996262072833", "0a5742430e336b3a1e1b6ff9983c5d25"),
    },
}

# Допуск при сравнении стыков (микросекунды)
EPS = 1000


# ---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНОЕ
# ---------------------------------------------------------------------------
def new_id() -> str:
    return str(uuid.uuid4()).upper()


def build_transition(effect_id: str, duration: int, known: dict) -> dict:
    """Собирает объект перехода в точности как это делает CapCut."""
    src = known.get(effect_id) or FALLBACK_TEMPLATES.get(effect_id)
    if src is None:
        raise TransitionsError(f"Нет данных для перехода {effect_id}: добавь его в FALLBACK_TEMPLATES.")
    return {
        "id": new_id(),
        "type": "transition",
        "name": src["name"],
        "effect_id": effect_id,
        "resource_id": effect_id,
        "third_resource_id": effect_id,
        "source_platform": 1,
        "sub_type": 0,
        "adjust_params": [],
        "path": src["path"],
        "duration": duration,
        "is_overlap": src["is_overlap"],
        "platform": "all",
        "category_id": "100000",
        "category_name": "",
        "request_id": "",
        "is_ai_transition": False,
        "video_path": "",
        "task_id": "",
    }


def overlay_starts(tracks: list, main: dict) -> list:
    """Времена начала клипов на остальных видеодорожках (оверлеи)."""
    starts = []
    for t in tracks:
        if t.get("type") == "video" and t is not main:
            starts += [s["target_timerange"]["start"] for s in t["segments"]]
    return starts


# ---------------------------------------------------------------------------
# ОСНОВНАЯ ЛОГИКА
# ---------------------------------------------------------------------------
def apply_transitions(project, dry_run=False, skip_overlays=False,
                      force=False, log=print) -> dict:
    path = find_draft_file(Path(project))

    if not dry_run and not force and capcut_is_running():
        raise TransitionsError("CapCut запущен. Закрой его полностью (иначе он перезапишет "
                               "изменения) или запусти с --force.")

    durations_map, pattern = load_transition_pattern()

    data = load_draft(path)
    mats = data["materials"]
    trans_list = mats.setdefault("transitions", [])

    # свежие данные о переходах из самого проекта
    known = {t["effect_id"]: {"name": t["name"], "path": t["path"],
                              "is_overlap": t["is_overlap"]}
             for t in trans_list}

    _, main_track = pick_main_track(data["tracks"])
    # работаем с отсортированной копией, порядок сегментов в файле не трогаем
    segs = sorted(main_track["segments"], key=lambda s: s["target_timerange"]["start"])
    skip_times = overlay_starts(data["tracks"], main_track) if skip_overlays else []

    # материалы, после которых CapCut вставляет переход в extra_material_refs
    before_ids = {x["id"] for x in mats.get("speeds", [])} | \
                 {x["id"] for x in mats.get("placeholder_infos", [])}

    # 1) убираем старые переходы с основной дорожки
    trans_ids = {t["id"] for t in trans_list}
    removed = set()
    for s in segs:
        keep = []
        for ref in s["extra_material_refs"]:
            if ref in trans_ids:
                removed.add(ref)
            else:
                keep.append(ref)
        s["extra_material_refs"] = keep
    mats["transitions"] = [t for t in trans_list if t["id"] not in removed]

    # 2) ставим новые по паттерну (переход живёт на ПЕРВОМ из двух клипов стыка)
    placed = skipped = 0
    k = 0
    for i, s in enumerate(segs[:-1]):
        nxt = segs[i + 1]
        end = s["target_timerange"]["start"] + s["target_timerange"]["duration"]
        if abs(nxt["target_timerange"]["start"] - end) > EPS:   # дыра / наложение клипов
            skipped += 1
            continue
        if any(abs(end - t) < EPS for t in skip_times):         # на стыке оверлей
            skipped += 1
            continue

        effect_id = pattern[k % len(pattern)]
        k += 1
        limit = min(s["target_timerange"]["duration"], nxt["target_timerange"]["duration"]) // 2
        duration = min(durations_map[effect_id], limit)

        tr = build_transition(effect_id, duration, known)
        mats["transitions"].append(tr)

        refs = s["extra_material_refs"]
        pos = 0
        for j, ref in enumerate(refs):
            if ref in before_ids:
                pos = j + 1
        refs.insert(pos, tr["id"])
        placed += 1

    stats = {"clips": len(segs), "removed": len(removed),
             "placed": placed, "skipped": skipped, "backup": None}
    log(f"Файл: {path}")
    log(f"Основная дорожка: {stats['clips']} клипов")
    log(f"Старых переходов удалено: {stats['removed']}")
    log(f"Новых поставлено: {placed}, стыков пропущено: {skipped}")

    if dry_run:
        log("--dry-run: файл не изменён.")
        return stats

    backup = save_draft_with_backup(path, data, prefix="transitions")
    stats["backup"] = backup.name
    log(f"Готово. Бэкап: {backup.name}")
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="папка проекта CapCut или путь к draft_content.json")
    ap.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    ap.add_argument("--skip-overlays", action="store_true",
                    help="не ставить переход там, где на стыке начинается клип с верхней дорожки")
    ap.add_argument("--force", action="store_true",
                    help="не проверять, запущен ли CapCut")
    args = ap.parse_args()
    try:
        apply_transitions(args.project, args.dry_run, args.skip_overlays, args.force)
    except TransitionsError as e:
        sys.exit(f"Ошибка: {e}")


if __name__ == "__main__":
    main()