#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capcut_common.py — общие утилиты для работы с черновиками CapCut:
поиск draft_content.json / draft_info.json, чтение с проверкой шифрования,
проверка запущенного CapCut, выбор основной дорожки и атомарная запись с ротацией бэкапов.
"""
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

IS_WIN = os.name == "nt"


class DraftError(Exception):
    """Понятная ошибка для пользователя (CLI печатает её, GUI может показать)."""


def _has_drafts(root: Path) -> bool:
    """True, если в папке root есть хотя бы один проект CapCut."""
    if not root.is_dir():
        return False
    try:
        for d in root.iterdir():
            if not d.is_dir():
                continue
            if (d / "draft_content.json").is_file() or (d / "draft_info.json").is_file():
                return True
            timelines = d / "Timelines"
            if timelines.is_dir() and any(timelines.glob("*/draft_info.json")):
                return True
    except OSError:
        return False
    return False


def default_drafts_dir() -> Path:
    """Возвращает папку черновиков CapCut по умолчанию для текущего пользователя."""
    candidates = [
        Path(r"D:\Для работы\CapCut Drafts"),
        Path(r"D:\Capcut\CapCut Drafts"),
    ]
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(Path(local_app) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft")
    home_drafts = Path.home() / "CapCut Drafts"
    candidates.append(home_drafts)

    for cand in candidates:
        if _has_drafts(cand):
            return cand
    for cand in candidates:
        if cand.is_dir():
            return cand
    return home_drafts


def app_config_dir(create: bool = False) -> Path:
    """Возвращает папку конфигурации приложения (%APPDATA%\\CapCutAutomation на Windows)."""
    if IS_WIN:
        app_data = os.environ.get("APPDATA")
        base = Path(app_data) if app_data else (Path.home() / "AppData" / "Roaming")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else (Path.home() / ".config")
    cfg_dir = base / "CapCutAutomation"
    if create:
        cfg_dir.mkdir(parents=True, exist_ok=True)
    return cfg_dir


def default_cache_effect_dir() -> Path:
    """Возвращает стандартную папку кэша эффектов CapCut."""
    local_app = os.environ.get("LOCALAPPDATA")
    base = Path(local_app) if local_app else (Path.home() / "AppData" / "Local")
    return base / "CapCut" / "User Data" / "Cache" / "effect"


def capcut_is_running() -> bool:
    """True, если запущен CapCut (Windows). На других ОС возвращает False."""
    if not IS_WIN:
        return False
    try:
        r = subprocess.run(
            ["tasklist"],
            capture_output=True,
            text=True,
            encoding="cp866",
            errors="ignore",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        out = r.stdout.lower()
    except Exception:
        return False
    return "capcut.exe" in out or "capcutbox.exe" in out or "capcutlauncher" in out


def find_draft_file(project: Path | str) -> Path:
    """Ищет файл таймлайна: сам файл, draft_content.json, draft_info.json или Timelines/*/draft_info.json."""
    if not str(project).strip():
        raise DraftError("Путь к проекту не указан.")
    p = Path(project)
    if p.is_file():
        return p
    for name in ("draft_content.json", "draft_info.json"):
        direct = p / name
        if direct.is_file():
            return direct
    timelines = p / "Timelines"
    if timelines.is_dir():
        cands = sorted(timelines.glob("*/draft_info.json"))
        if cands:
            return cands[0]
    raise DraftError(f"Не найден draft_content.json (или draft_info.json) в: {p}")


def load_draft(path: Path) -> dict:
    """Читает JSON-файл черновика с поддержкой UTF-8 BOM и диагностикой шифрования."""
    raw = path.read_bytes()
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DraftError(
            f"{path.name} не читается как JSON — вероятно, он зашифрован этой версией "
            "CapCut. Скрипт работает только с обычным (нешифрованным) файлом проекта."
        ) from exc


def pick_main_track(tracks: list, index: int | None = None) -> tuple[int, dict]:
    """
    Выбирает видеодорожку: по явному индексу или дорожку с наибольшим числом сегментов.
    Возвращает (индекс_среди_видеодорожек, объект_дорожки).
    """
    video = [t for t in tracks if isinstance(t, dict) and t.get("type") == "video"]
    if not video:
        raise DraftError("В проекте нет видеодорожек.")

    if index is not None:
        if index < 0 or index >= len(video):
            raise DraftError(f"Видеодорожек всего {len(video)}, индекс {index} не существует.")
        return index, video[index]

    best_idx = max(range(len(video)), key=lambda i: len(video[i].get("segments") or []))
    if not (video[best_idx].get("segments") or []):
        raise DraftError("В проекте нет видеодорожек с клипами.")
    return best_idx, video[best_idx]


TIMELINES_DIR_NAME = "Timelines"


def _matching_backups(backups_dir: Path, path: Path, prefix: str | None = None) -> list[Path]:
    """Возвращает список файлов бэкапов для path без уязвимости к спецсимволам в имени."""
    if not backups_dir.is_dir():
        return []
    head = f"{path.stem}.{prefix}_" if prefix else f"{path.stem}."
    tail = path.suffix
    return [
        p for p in backups_dir.iterdir()
        if p.is_file() and p.name.startswith(head) and (not tail or p.name.endswith(tail))
    ]


def _timelines_main_id(timelines: Path) -> str:
    """Возвращает id основной временной шкалы из Timelines/project.json (если он есть)."""
    try:
        meta = json.loads((timelines / "project.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    main_id = meta.get("main_timeline_id")
    if main_id:
        return str(main_id)
    items = meta.get("timelines")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return str(items[0].get("id") or "")
    return ""


def timeline_mirror_targets(path: Path, data: dict | None = None) -> list[Path]:
    """
    Файл-зеркало в <проект>/Timelines/<uuid>/ с тем же именем, что и path.

    В новых версиях CapCut актуальный таймлайн хранится ещё и в
    <проект>/Timelines/<uuid>/draft_content.json. Если обновить только корневой
    draft_content.json, при открытии CapCut может загрузить старый таймлайн и
    перезаписать изменения. Поэтому копия основной временной шкалы обновляется
    вместе с корневым файлом. Второстепенные таймлайны не трогаем.

    Основная шкала определяется по id загруженного драфта (data["id"]) или по
    main_timeline_id из Timelines/project.json. Если id определить не удалось,
    используется единственная подпапка с файлом-зеркалом.

    Если папки Timelines нет (или path уже лежит внутри неё) — возвращает [].
    """
    project_dir = path.parent
    if project_dir.name.lower() == TIMELINES_DIR_NAME.lower():
        return []
    timelines = project_dir / TIMELINES_DIR_NAME
    if not timelines.is_dir():
        return []

    main_id = str((data or {}).get("id") or "") or _timelines_main_id(timelines)
    if main_id:
        target = timelines / main_id / path.name
        try:
            if target.is_file() and target.resolve() != path.resolve():
                return [target]
        except OSError:
            pass
        return []

    # Нет id — обновляем только если в Timelines ровно одна подпапка с таким файлом.
    targets: list[Path] = []
    try:
        subdirs = [d for d in timelines.iterdir() if d.is_dir()]
    except OSError:
        return []
    for sub in subdirs:
        target = sub / path.name
        try:
            if target.is_file() and target.resolve() != path.resolve():
                targets.append(target)
        except OSError:
            continue
    return targets if len(targets) == 1 else []


def _atomic_write_json(path: Path, data: dict) -> None:
    """Атомарно записывает JSON в path (через временный файл рядом)."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _atomic_copy_file(src: Path, dst: Path) -> None:
    """Атомарно копирует src в dst (через временный файл рядом)."""
    fd, tmp_name = tempfile.mkstemp(dir=str(dst.parent), suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp_path)
        os.replace(tmp_path, dst)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _save_backup_copy(path: Path, prefix: str, max_backups: int = 5) -> Path:
    """Копирует path в .backups рядом с файлом и ротирует старые копии."""
    backups_dir = path.parent / ".backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = backups_dir / f"{path.stem}.{prefix}_{stamp}{path.suffix}"
    counter = 1
    while backup.exists():
        backup = backups_dir / f"{path.stem}.{prefix}_{stamp}_{counter:02d}{path.suffix}"
        counter += 1
    shutil.copyfile(path, backup)

    if max_backups > 0:
        old = sorted(_matching_backups(backups_dir, path, prefix=prefix), key=lambda p: p.name, reverse=True)
        for obsolete in old[max_backups:]:
            try:
                obsolete.unlink()
            except OSError:
                pass
    return backup


def save_draft_with_backup(path: Path, data: dict, prefix: str = "backup", max_backups: int = 5) -> Path:
    """
    Сохраняет резервную копию в подпапку .backups (хранит до max_backups последних копий)
    и атомарно записывает обновлённый JSON в path.

    Дополнительно обновляет копии в <проект>/Timelines/<uuid>/draft_content.json,
    если такие файлы уже есть, — иначе CapCut при открытии может загрузить старый
    таймлайн и потерять изменения.
    """
    backup = _save_backup_copy(path, prefix, max_backups)
    _atomic_write_json(path, data)

    for target in timeline_mirror_targets(path, data):
        _save_backup_copy(target, prefix, max_backups)
        _atomic_write_json(target, data)
    return backup


def restore_latest_backup(path: Path) -> Path:
    """
    Восстанавливает самую свежую резервную копию из .backups и возвращает путь к ней.
    Обновляет и зеркала в <проект>/Timelines/<uuid>/, если они есть.
    """
    backups_dir = path.parent / ".backups"
    if not backups_dir.is_dir():
        raise DraftError("Папка .backups не найдена — откатывать нечего.")
    candidates = sorted(
        _matching_backups(backups_dir, path),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )
    if not candidates:
        raise DraftError("Резервные копии в .backups не найдены.")

    latest = candidates[0]
    restored = load_draft(latest)  # проверяем, что бэкап валидный JSON

    _atomic_copy_file(latest, path)
    for target in timeline_mirror_targets(path, restored):
        _atomic_copy_file(latest, target)
    return latest
