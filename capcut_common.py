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


def default_drafts_dir() -> Path:
    """Возвращает папку черновиков CapCut по умолчанию для текущего пользователя."""
    preferred = Path(r"D:\Для работы\CapCut Drafts")
    if preferred.is_dir():
        return preferred
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        cand = Path(local_app) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
        if cand.is_dir():
            return cand
    return Path.home() / "CapCut Drafts"


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
    """Ищет файл таймлайна: сам файл, draft_content.json или Timelines/*/draft_info.json."""
    p = Path(project)
    if p.is_file():
        return p
    direct = p / "draft_content.json"
    if direct.exists():
        return direct
    timelines = p / "Timelines"
    if timelines.exists():
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
    video = [t for t in tracks if t.get("type") == "video" and t.get("segments")]
    if not video:
        # Если нет дорожек с сегментами, пробуем любые видеодорожки (для корректной ошибки индекса)
        video = [t for t in tracks if t.get("type") == "video"]
    if not video:
        raise DraftError("В проекте нет видеодорожек.")

    if index is not None:
        if index < 0 or index >= len(video):
            raise DraftError(f"Видеодорожек всего {len(video)}, индекс {index} не существует.")
        return index, video[index]

    best_idx = max(range(len(video)), key=lambda i: len(video[i].get("segments", [])))
    if not video[best_idx].get("segments"):
        raise DraftError("В проекте нет видеодорожек с клипами.")
    return best_idx, video[best_idx]


def save_draft_with_backup(path: Path, data: dict, prefix: str = "backup", max_backups: int = 5) -> Path:
    """
    Сохраняет резервную копию в подпапку .backups (хранит до max_backups последних копий)
    и атомарно записывает обновлённый JSON в path.
    """
    backups_dir = path.parent / ".backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    backup = backups_dir / f"{path.stem}.{prefix}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}"
    shutil.copy2(path, backup)

    # Ротация старых бэкапов данного типа
    if max_backups > 0:
        old = sorted(backups_dir.glob(f"{path.stem}.{prefix}_*{path.suffix}"), reverse=True)
        for obsolete in old[max_backups:]:
            try:
                obsolete.unlink()
            except OSError:
                pass

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
    return backup


def restore_latest_backup(path: Path) -> Path:
    """
    Восстанавливает самую свежую резервную копию из .backups и возвращает путь к ней.
    """
    backups_dir = path.parent / ".backups"
    if not backups_dir.is_dir():
        raise DraftError("Папка .backups не найдена — откатывать нечего.")
    candidates = sorted(
        backups_dir.glob(f"{path.stem}.*{path.suffix}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise DraftError("Резервные копии в .backups не найдены.")

    latest = candidates[0]
    load_draft(latest)  # проверяем, что бэкап валидный JSON

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        shutil.copy2(latest, tmp_path)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return latest
