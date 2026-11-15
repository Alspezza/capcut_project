#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py — оконный запуск make_transitions.py, clear_vids.py и stock_fetcher.py.

Запуск:
    python gui.py
Чтобы окно открывалось без консоли — переименуй в gui.pyw и запускай двойным кликом.
Зависимости самого GUI: только стандартная библиотека (tkinter).
Для clear_vids.py нужны: pip install opencv-python numpy
Для YouTube в stock_fetcher.py нужен: pip install yt-dlp
"""
import json
import math
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from capcut_common import (
    DraftError,
    app_config_dir,
    capcut_is_running,
    default_drafts_dir,
    find_draft_file,
    restore_latest_backup,
)
try:
    import clear_vids
    _CLEAR_VIDS_ERR = None
except ImportError as exc:
    clear_vids = None
    _CLEAR_VIDS_ERR = exc
import make_transitions
import setup_audio
import stock_fetcher

IS_FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parent
CONFIG_DIR = app_config_dir()
SETTINGS_FILE = CONFIG_DIR / "gui_settings.json"
LEGACY_SETTINGS_FILE = APP_DIR / "gui_settings.json"
TRANSITIONS_CONFIG_FILE = CONFIG_DIR / "transitions_config.json"
LEGACY_TRANSITIONS_CONFIG_FILE = APP_DIR / "transitions_config.json"
SCRIPT_CLEAR = APP_DIR / "clear_vids.py"
SCRIPT_TRANS = APP_DIR / "make_transitions.py"
SCRIPT_AUDIO = APP_DIR / "setup_audio.py"
SCRIPT_STOCK = APP_DIR / "stock_fetcher.py"
IS_WIN = os.name == "nt"


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    for path in (SETTINGS_FILE, LEGACY_SETTINGS_FILE):
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}
    return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def draft_title(folder: Path) -> str:
    """Человеческое имя проекта из draft_meta_info.json (если есть)."""
    try:
        meta = json.loads((folder / "draft_meta_info.json").read_text(encoding="utf-8"))
        return meta.get("draft_name") or ""
    except Exception:
        return ""


def scan_drafts(root: Path) -> dict:
    """{подпись: путь}, свежие проекты сверху."""
    items = []
    if root.is_dir():
        for d in root.iterdir():
            if not d.is_dir():
                continue
            try:
                f = find_draft_file(d)
            except DraftError:
                continue
            items.append((f.stat().st_mtime, d))
    items.sort(reverse=True)
    out = {}
    for mtime, d in items:
        name = draft_title(d)
        stamp = time.strftime("%d.%m %H:%M", time.localtime(mtime))
        label = f"{name}  |  {d.name}  |  {stamp}" if (name and name != d.name) else f"{d.name}  |  {stamp}"
        out[label] = d
    return out


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CapCut Automation + Stock Fetcher")
        self.geometry("1000x760")
        self.minsize(840, 620)
        icon_base = Path(getattr(sys, "_MEIPASS", APP_DIR))
        icon_file = icon_base / "app_icon.ico"
        if icon_file.is_file():
            try:
                self.iconbitmap(default=str(icon_file))
            except Exception:
                pass

        self.cfg = load_settings()
        saved_dir = self.cfg.get("drafts_dir", "")
        self.drafts_dir = Path(saved_dir) if saved_dir and Path(saved_dir).exists() else default_drafts_dir()
        self.drafts = {}
        self.proc = None
        self.running = False
        self.stop_flag = False
        self.q = queue.Queue()

        style = ttk.Style(self)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Run.TButton", font=("Segoe UI", 10, "bold"), padding=8)
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=10)

        self._build_vars()
        self._build_ui()
        self.refresh_drafts(select=self.cfg.get("last_project"))
        self._update_stock_mode_ui()
        self._refresh_api_status()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(60, self.poll_queue)
        self.after(500, self.update_capcut_status)

    # ---- переменные ------------------------------------------------------
    def _build_vars(self):
        g = self.cfg.get
        self.v_project = tk.StringVar()
        # переходы
        self.t_dry = tk.BooleanVar(value=g("t_dry", False))
        self.t_skip = tk.BooleanVar(value=g("t_skip", False))
        self.t_skip_edges = tk.BooleanVar(value=g("t_skip_edges", False))
        self.t_every = tk.StringVar(value=g("t_every", "1"))
        self.t_chance = tk.StringVar(value=g("t_chance", "100"))
        self.t_min_clip = tk.StringVar(value=g("t_min_clip", "0"))
        self.t_seed = tk.StringVar(value=g("t_seed", ""))
        # вотермарки
        self.c_dry = tk.BooleanVar(value=g("c_dry", False))
        self.c_scale = tk.StringVar(value=g("c_scale", "1.15"))
        self.c_reset = tk.BooleanVar(value=g("c_reset", False))
        self.c_debug = tk.BooleanVar(value=g("c_debug", False))
        self.c_thr = tk.StringVar(value=g("c_thr", ""))
        self.c_thr_single = tk.StringVar(value=g("c_thr_single", ""))
        self.c_track = tk.StringVar(value=g("c_track", ""))
        self.c_size = tk.StringVar(value=g("c_size", ""))
        self.c_probe = tk.StringVar(value=g("c_probe", ""))
        self.c_extra = tk.StringVar(value=g("c_extra", ""))
        # аудио + fade out
        self.a_dry = tk.BooleanVar(value=g("a_dry", False))
        self.a_intro = tk.StringVar(value=g("a_intro", ""))
        self.a_ambient = tk.StringVar(value=g("a_ambient", ""))
        self.a_voice = tk.StringVar(value=g("a_voice", "voice"))
        self.a_db = tk.StringVar(value=g("a_db", "-7"))
        self.a_fade = tk.StringVar(value=g("a_fade", "4"))
        self.a_norm = tk.BooleanVar(value=g("a_norm", True))
        self.a_vfade = tk.BooleanVar(value=g("a_vfade", True))
        # stock fetcher
        self.s_mode = tk.StringVar(value=g("s_mode", "yt"))
        self.s_target = tk.StringVar(value=g("s_target", ""))
        self.s_count = tk.StringVar(value=g("s_count", "3"))
        self.s_duration = tk.StringVar(value=g("s_duration", "30"))
        self.s_full = tk.BooleanVar(value=g("s_full", False))
        self.s_batch_srv = tk.StringVar(value=g("s_batch_srv", "pexels"))
        self.s_output = tk.StringVar(value=g("s_output", str(APP_DIR / "downloads")))

    # ---- интерфейс -------------------------------------------------------
    def _build_ui(self):
        pad = dict(padx=10, pady=6)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="x", **pad)

        tab_capcut = ttk.Frame(self.notebook)
        tab_stock = ttk.Frame(self.notebook)
        self.notebook.add(tab_capcut, text="  🎬 Обработка проекта (CapCut)  ")
        self.notebook.add(tab_stock, text="  📥 Скачать видео (Stock Fetcher)  ")

        self._build_capcut_tab(tab_capcut, pad)
        self._build_stock_tab(tab_stock, pad)

        # --- общий лог
        lf = ttk.LabelFrame(self, text="Лог")
        lf.pack(fill="both", expand=True, **pad)
        self.log = ScrolledText(lf, wrap="word", font=("Consolas", 10), state="disabled",
                                bg="#1e1e1e", fg="#dcdcdc", insertbackground="#dcdcdc")
        self.log.pack(fill="both", expand=True, padx=6, pady=(6, 2))
        self.log.tag_config("cmd", foreground="#7aa2f7")
        self.log.tag_config("err", foreground="#f7768e")
        self.log.tag_config("wm", foreground="#e0af68")
        self.log.tag_config("ok", foreground="#9ece6a")
        self.log.tag_config("dim", foreground="#8a8a8a")
        lrow = ttk.Frame(lf)
        lrow.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(lrow, text="Очистить лог", command=self.clear_log).pack(side="left")
        ttk.Button(lrow, text="Копировать лог", command=self.copy_all_log).pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(lrow, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        self.lbl_status = ttk.Label(lrow, text="Готов")
        self.lbl_status.pack(side="right", padx=8)

        self._init_log_copy()

    def _build_capcut_tab(self, parent, pad):
        # --- проект
        pf = ttk.LabelFrame(parent, text="Проект CapCut")
        pf.pack(fill="x", **pad)
        row = ttk.Frame(pf)
        row.pack(fill="x", padx=8, pady=(8, 2))
        self.combo = ttk.Combobox(row, textvariable=self.v_project, state="normal")
        self.combo.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Обзор…", command=self.browse_project).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="⟳", width=3, command=lambda: self.refresh_drafts()).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="Открыть папку", command=self.open_project_folder).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="↩ Откатить бэкап", command=self.restore_backup).pack(side="left", padx=(4, 0))

        row2 = ttk.Frame(pf)
        row2.pack(fill="x", padx=8, pady=(0, 8))
        self.lbl_dir = ttk.Label(row2, foreground="#666")
        self.lbl_dir.pack(side="left")
        ttk.Button(row2, text="Сменить папку черновиков…", command=self.pick_drafts_dir).pack(side="right")
        self.lbl_capcut = ttk.Label(row2, text="")
        self.lbl_capcut.pack(side="right", padx=12)

        # --- два блока настроек
        mid = ttk.Frame(parent)
        mid.pack(fill="x", **pad)
        mid.columnconfigure(0, weight=1, uniform="a")
        mid.columnconfigure(1, weight=1, uniform="a")

        # Вотермарки / масштаб
        cf = ttk.LabelFrame(mid, text="1. Масштаб + вотермарка  (clear_vids.py)")
        cf.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        r = ttk.Frame(cf)
        r.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(r, text="Масштаб:").pack(side="left")
        ttk.Entry(r, textvariable=self.c_scale, width=7).pack(side="left", padx=6)
        ttk.Checkbutton(cf, text="Пробный прогон (ничего не записывать)", variable=self.c_dry).pack(anchor="w", padx=8, pady=2)
        ttk.Checkbutton(cf, text="Чистым клипам сбрасывать сдвиг X в 0", variable=self.c_reset).pack(anchor="w", padx=8, pady=2)
        ttk.Checkbutton(cf, text="Сохранять картинки проверки (папка debug)", variable=self.c_debug).pack(anchor="w", padx=8, pady=2)

        adv = ttk.LabelFrame(cf, text="Дополнительно (пусто = по умолчанию)")
        adv.pack(fill="x", padx=8, pady=6)
        fields = [
            ("Порог (среднее)", self.c_thr, "0.46 / 0.50"),
            ("Порог (кадр)", self.c_thr_single, "0.70"),
            ("Дорожка №", self.c_track, "0"),
            ("Только размер", self.c_size, "1280x720"),
            ("Кадры, сек", self.c_probe, "0.1 0.5 1.0 …"),
            ("Свои флаги", self.c_extra, "--marks …"),
        ]
        for i, (lab, var, hint) in enumerate(fields):
            ttk.Label(adv, text=lab).grid(row=i, column=0, sticky="w", padx=6, pady=2)
            ttk.Entry(adv, textvariable=var).grid(row=i, column=1, sticky="ew", padx=6, pady=2)
            ttk.Label(adv, text=hint, foreground="#999").grid(row=i, column=2, sticky="w", padx=(0, 6))
        adv.columnconfigure(1, weight=1)

        ttk.Button(cf, text="▶  Запустить", style="Run.TButton",
                   command=lambda: self.start(["clear"])).pack(fill="x", padx=8, pady=(4, 8))

        # Переходы
        tf = ttk.LabelFrame(mid, text="2. Переходы  (make_transitions.py)")
        tf.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ttk.Checkbutton(tf, text="Пробный прогон (ничего не записывать)", variable=self.t_dry).pack(anchor="w", padx=8, pady=(10, 2))
        ttk.Checkbutton(tf, text="Пропускать стыки, где начинается оверлей", variable=self.t_skip).pack(anchor="w", padx=8, pady=2)
        ttk.Checkbutton(tf, text="Не ставить после 1-го и перед последним клипом", variable=self.t_skip_edges).pack(anchor="w", padx=8, pady=2)

        tadv = ttk.LabelFrame(tf, text="Параметры частоты (по умолч. = на каждый)")
        tadv.pack(fill="x", padx=8, pady=6)
        tfields = [
            ("Каждый N-й стык", self.t_every, "1"),
            ("Вероятность, %", self.t_chance, "100"),
            ("Мин. клип, сек", self.t_min_clip, "0"),
            ("Seed (для %)", self.t_seed, "пусто = случ."),
        ]
        for i, (lab, var, hint) in enumerate(tfields):
            ttk.Label(tadv, text=lab).grid(row=i, column=0, sticky="w", padx=6, pady=2)
            ttk.Entry(tadv, textvariable=var).grid(row=i, column=1, sticky="ew", padx=6, pady=2)
            ttk.Label(tadv, text=hint, foreground="#999").grid(row=i, column=2, sticky="w", padx=(0, 6))
        tadv.columnconfigure(1, weight=1)

        ttk.Button(tf, text="⚙ Настроить паттерн переходов…", command=self.open_transitions_editor).pack(
            anchor="w", padx=8, pady=(4, 2))
        ttk.Label(tf, text="Настройки сохраняются в transitions_config.json.",
                  foreground="#888", justify="left").pack(anchor="w", padx=8, pady=2)
        ttk.Frame(tf).pack(expand=True, fill="both")
        ttk.Button(tf, text="▶  Запустить", style="Run.TButton",
                   command=lambda: self.start(["trans"])).pack(fill="x", padx=8, pady=(4, 8), side="bottom")

        # --- аудио + fade out
        af = ttk.LabelFrame(parent, text="3. Аудио (Intro / Ambient / Озвучка) + Fade Out  (setup_audio.py)")
        af.pack(fill="x", **pad)
        af.columnconfigure(1, weight=1)

        ttk.Label(af, text="Intro:").grid(row=0, column=0, sticky="w", padx=(8, 6), pady=(6, 2))
        ttk.Entry(af, textvariable=self.a_intro).grid(row=0, column=1, sticky="ew", pady=(6, 2))
        ttk.Button(af, text="Обзор…", command=lambda: self.browse_audio_file(self.a_intro, "Выберите Intro")).grid(
            row=0, column=2, padx=(6, 8), pady=(6, 2))

        ttk.Label(af, text="Ambient:").grid(row=1, column=0, sticky="w", padx=(8, 6), pady=2)
        ttk.Entry(af, textvariable=self.a_ambient).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Button(af, text="Обзор…", command=lambda: self.browse_audio_file(self.a_ambient, "Выберите Ambient")).grid(
            row=1, column=2, padx=(6, 8), pady=2)

        arow = ttk.Frame(af)
        arow.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=4)
        ttk.Label(arow, text="Озвучка:").pack(side="left")
        ttk.Entry(arow, textvariable=self.a_voice, width=14).pack(side="left", padx=(4, 12))
        ttk.Label(arow, text="Громкость, дБ:").pack(side="left")
        ttk.Entry(arow, textvariable=self.a_db, width=6).pack(side="left", padx=(4, 12))
        ttk.Label(arow, text="Fade, сек:").pack(side="left")
        ttk.Entry(arow, textvariable=self.a_fade, width=5).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(arow, text="Нормализация озвучки (-23 LUFS)", variable=self.a_norm).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(arow, text="Fade Out на последнем клипе", variable=self.a_vfade).pack(side="left")

        arow2 = ttk.Frame(af)
        arow2.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 6))
        ttk.Checkbutton(arow2, text="Пробный прогон (ничего не записывать)", variable=self.a_dry).pack(side="left")
        ttk.Button(arow2, text="▶  Запустить", style="Run.TButton",
                   command=lambda: self.start(["audio"])).pack(side="right")

        # --- общие кнопки
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=10, pady=(2, 8))
        self.btn_both = ttk.Button(bar, text="▶▶  Запустить всё подряд (масштаб → переходы → аудио)",
                                   style="Big.TButton", command=lambda: self.start(["clear", "trans", "audio"]))
        self.btn_both.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(bar, text="■ Стоп", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0), ipady=8)

    def _build_stock_tab(self, parent, pad):
        sf = ttk.LabelFrame(parent, text="Источник видео")
        sf.pack(fill="x", **pad)
        mrow = ttk.Frame(sf)
        mrow.pack(fill="x", padx=8, pady=8)
        modes = [
            ("YouTube (ссылка / .txt)", "yt"),
            ("Pexels (поиск)", "pexels"),
            ("Pixabay (поиск)", "pixabay"),
            ("Пакетно по тегам (.txt)", "batch-tags"),
        ]
        for text, val in modes:
            ttk.Radiobutton(mrow, text=text, value=val, variable=self.s_mode,
                            command=self._update_stock_mode_ui).pack(side="left", padx=(0, 14))

        self.lbl_api_status = ttk.Label(mrow, text="", foreground="#666")
        self.lbl_api_status.pack(side="right")
        ttk.Button(mrow, text="⚙ API-ключи…", command=self.open_stock_settings).pack(side="right", padx=(0, 8))

        pf = ttk.LabelFrame(parent, text="Параметры загрузки")
        pf.pack(fill="x", **pad)

        # Строка цели (URL / запрос / файл)
        r1 = ttk.Frame(pf)
        r1.pack(fill="x", padx=8, pady=(8, 4))
        self.lbl_s_target = ttk.Label(r1, text="Ссылка / файл:", width=18)
        self.lbl_s_target.pack(side="left")
        ttk.Entry(r1, textvariable=self.s_target).pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.btn_s_browse_file = ttk.Button(r1, text="Файл…", command=self.browse_stock_file)
        self.btn_s_browse_file.pack(side="left", padx=(6, 0))

        # Строка опций (количество / длительность / сервис)
        r2 = ttk.Frame(pf)
        r2.pack(fill="x", padx=8, pady=4)
        self.lbl_s_param = ttk.Label(r2, text="Длительность (сек):", width=18)
        self.lbl_s_param.pack(side="left")
        self.ent_s_duration = ttk.Entry(r2, textvariable=self.s_duration, width=8)
        self.ent_s_count = ttk.Entry(r2, textvariable=self.s_count, width=8)
        self.chk_s_full = ttk.Checkbutton(r2, text="Скачать видео целиком (игнорировать таймкод)", variable=self.s_full)
        self.cmb_s_srv = ttk.Combobox(r2, textvariable=self.s_batch_srv, values=["pexels", "pixabay"], state="readonly", width=10)

        # Строка папки назначения
        r3 = ttk.Frame(pf)
        r3.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Label(r3, text="Папка сохранения:", width=18).pack(side="left")
        ttk.Entry(r3, textvariable=self.s_output).pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Button(r3, text="Обзор…", command=self.browse_stock_output).pack(side="left", padx=(6, 0))
        ttk.Button(r3, text="В папку проекта", command=self.set_stock_output_to_project).pack(side="left", padx=(4, 0))
        ttk.Button(r3, text="Открыть", command=self.open_stock_output).pack(side="left", padx=(4, 0))

        # Кнопка запуска
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=10, pady=(2, 8))
        self.btn_stock_run = ttk.Button(bar, text="▶  Скачать видео", style="Big.TButton",
                                        command=lambda: self.start(["stock"]))
        self.btn_stock_run.pack(side="left", fill="x", expand=True)
        self.btn_stock_stop = ttk.Button(bar, text="■ Стоп", command=self.stop, state="disabled")
        self.btn_stock_stop.pack(side="left", padx=(8, 0), ipady=8)

    def _update_stock_mode_ui(self):
        mode = self.s_mode.get()
        for w in (self.ent_s_duration, self.ent_s_count, self.chk_s_full, self.cmb_s_srv):
            w.pack_forget()

        if mode == "yt":
            self.lbl_s_target.config(text="URL или .txt файл:")
            self.btn_s_browse_file.config(state="normal")
            self.lbl_s_param.config(text="Фрагмент (сек):")
            self.ent_s_duration.pack(side="left", padx=4)
            self.chk_s_full.pack(side="left", padx=10)
        elif mode in ("pexels", "pixabay"):
            self.lbl_s_target.config(text="Запрос (англ.):")
            self.btn_s_browse_file.config(state="disabled")
            self.lbl_s_param.config(text="Количество видео:")
            self.ent_s_count.pack(side="left", padx=4)
        else:  # batch-tags
            self.lbl_s_target.config(text="Файл тегов (.txt):")
            self.btn_s_browse_file.config(state="normal")
            self.lbl_s_param.config(text="Видео на тег / сервис:")
            self.ent_s_count.pack(side="left", padx=4)
            self.cmb_s_srv.pack(side="left", padx=8)

    def _refresh_api_status(self):
        cfg = stock_fetcher.load_config()
        pex = "Pexels ✓" if cfg.get("pexels_api_key") else "Pexels ✗"
        pix = "Pixabay ✓" if cfg.get("pixabay_api_key") else "Pixabay ✗"
        self.lbl_api_status.config(text=f"{pex}  |  {pix}")

    def open_stock_settings(self):
        cfg = stock_fetcher.load_config()
        win = tk.Toplevel(self)
        win.title("Настройки Stock Fetcher (API-ключи)")
        win.geometry("500x190")
        win.transient(self)
        win.grab_set()

        v_pex = tk.StringVar(value=cfg.get("pexels_api_key", ""))
        v_pix = tk.StringVar(value=cfg.get("pixabay_api_key", ""))

        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Pexels API Key:").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(frm, textvariable=v_pex, width=48).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(frm, text="Pixabay API Key:").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frm, textvariable=v_pix, width=48).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Label(frm, text="Ключи сохраняются локально в stock_config.json", foreground="#666").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=4)
        frm.columnconfigure(1, weight=1)

        def on_save():
            stock_fetcher.save_config({
                "pexels_api_key": v_pex.get().strip(),
                "pixabay_api_key": v_pix.get().strip(),
            })
            self._refresh_api_status()
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Сохранить", command=on_save).pack(side="right")
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="right", padx=6)

    def browse_stock_file(self):
        p = filedialog.askopenfilename(title="Выберите .txt файл", filetypes=[("Text files", "*.txt"), ("All", "*.*")])
        if p:
            self.s_target.set(p)

    def browse_stock_output(self):
        d = filedialog.askdirectory(title="Папка для скачанных видео", initialdir=self.s_output.get() or str(APP_DIR))
        if d:
            self.s_output.set(d)

    def set_stock_output_to_project(self):
        p = self.current_project()
        if p and p.is_dir():
            self.s_output.set(str(p / "stock_media"))
        else:
            messagebox.showinfo("Проект не выбран", "Сначала выбери проект CapCut на первой вкладке.")

    def open_stock_output(self):
        d = Path(self.s_output.get().strip() or (APP_DIR / "downloads"))
        d.mkdir(parents=True, exist_ok=True)
        if IS_WIN:
            os.startfile(d)  # noqa
        else:
            subprocess.Popen(["xdg-open", str(d)])

    # ---- проекты ---------------------------------------------------------
    def refresh_drafts(self, select=None):
        self.drafts = scan_drafts(self.drafts_dir)
        labels = list(self.drafts.keys())
        self.combo["values"] = labels
        self.lbl_dir.config(text=f"Папка: {self.drafts_dir}  ({len(labels)} проектов)")
        if select:
            for lab, p in self.drafts.items():
                if str(p) == select:
                    self.v_project.set(lab)
                    return
            self.v_project.set(select)
        elif labels and not self.v_project.get():
            self.v_project.set(labels[0])

    def current_project(self):
        txt = self.v_project.get().strip()
        if txt in self.drafts:
            return self.drafts[txt]
        return Path(txt.strip('"')) if txt else None

    def browse_project(self):
        d = filedialog.askdirectory(title="Папка проекта CapCut", initialdir=str(self.drafts_dir))
        if not d:
            return
        p = Path(d)
        if p.is_file():
            proj_dir = p.parent.parent.parent if p.parent.parent.name == "Timelines" else p.parent
        else:
            proj_dir = p
        if proj_dir.parent.is_dir():
            self.drafts_dir = proj_dir.parent
            self.refresh_drafts(select=str(proj_dir))
        else:
            self.v_project.set(str(proj_dir))

    def browse_audio_file(self, var, title):
        cur = var.get().strip()
        init_dir = str(Path(cur).parent) if cur and Path(cur).parent.is_dir() else str(APP_DIR)
        p = filedialog.askopenfilename(
            title=title,
            initialdir=init_dir,
            filetypes=[("Аудиофайлы", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"), ("Все файлы", "*.*")],
        )
        if p:
            var.set(p)

    def pick_drafts_dir(self):
        d = filedialog.askdirectory(title="Папка с черновиками CapCut", initialdir=str(self.drafts_dir))
        if d:
            self.drafts_dir = Path(d)
            self.v_project.set("")
            self.refresh_drafts()

    def open_project_folder(self):
        p = self.current_project()
        if p and p.exists():
            folder = p if p.is_dir() else p.parent
            if IS_WIN:
                os.startfile(folder)  # noqa
            else:
                subprocess.Popen(["xdg-open", str(folder)])

    def restore_backup(self):
        if self.running:
            return
        project = self.current_project()
        if not project:
            messagebox.showerror("Проект не выбран", "Выбери проект CapCut.")
            return
        try:
            draft_path = find_draft_file(project)
        except DraftError as exc:
            messagebox.showerror("Ошибка", str(exc))
            return
        if capcut_is_running():
            if not messagebox.askyesno(
                    "CapCut запущен",
                    "CapCut сейчас открыт и может перезаписать восстановленный файл.\nВсё равно откатить?"):
                return
        if not messagebox.askyesno(
                "Откат бэкапа",
                f"Восстановить самый свежий бэкап из .backups для:\n{draft_path}?"):
            return
        try:
            restored = restore_latest_backup(draft_path)
            self.write(f"\nГотово: восстановлен бэкап {restored.name}\n", "ok")
            self.lbl_status.config(text=f"Откачено: {restored.name}")
        except DraftError as exc:
            messagebox.showerror("Не удалось откатить", str(exc))

    def open_transitions_editor(self):
        durations, pattern = make_transitions.load_transition_pattern()
        all_ids = list(dict.fromkeys(
            list(pattern) + list(durations.keys()) + list(make_transitions.DEFAULT_DURATIONS_MAP.keys())
        ))
        names = {k: v["name"] for k, v in make_transitions.FALLBACK_TEMPLATES.items()}

        win = tk.Toplevel(self)
        win.title("Паттерн переходов (transitions_config.json)")
        win.geometry("540x320")
        win.transient(self)
        win.grab_set()

        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Отметьте переходы, задайте длительность (сек) и порядок:").pack(anchor="w", pady=(0, 8))

        rows_frame = ttk.Frame(frm)
        rows_frame.pack(fill="both", expand=True)

        rows = []
        for eid in all_ids:
            enabled = tk.BooleanVar(value=eid in pattern)
            dur_val = durations.get(eid, make_transitions.DEFAULT_DURATIONS_MAP.get(eid, 1_000_000)) / 1_000_000
            dur_var = tk.StringVar(value=f"{dur_val:.3f}".rstrip("0").rstrip("."))
            rows.append([eid, enabled, dur_var])

        def redraw():
            for ch in rows_frame.winfo_children():
                ch.destroy()
            for idx, (eid, en_var, dur_var) in enumerate(rows):
                r = ttk.Frame(rows_frame)
                r.pack(fill="x", pady=3)
                ttk.Checkbutton(r, variable=en_var).pack(side="left")
                ttk.Label(r, text=f"{names.get(eid, eid)} ({eid})", width=36).pack(side="left", padx=4)
                ttk.Entry(r, textvariable=dur_var, width=7).pack(side="left", padx=4)
                ttk.Label(r, text="сек").pack(side="left")
                ttk.Button(r, text="↑", width=3, command=lambda i=idx: move(i, -1)).pack(side="right", padx=2)
                ttk.Button(r, text="↓", width=3, command=lambda i=idx: move(i, 1)).pack(side="right", padx=2)

        def move(idx, delta):
            nxt = idx + delta
            if 0 <= nxt < len(rows):
                rows[idx], rows[nxt] = rows[nxt], rows[idx]
                redraw()

        def on_reset():
            for cfg_path in (TRANSITIONS_CONFIG_FILE, LEGACY_TRANSITIONS_CONFIG_FILE):
                if cfg_path.exists():
                    cfg_path.unlink()
            win.destroy()
            self.write("\nСброшен transitions_config.json к значениям по умолчанию.\n", "ok")

        def on_save():
            new_durs = {}
            new_pat = []
            try:
                for eid, en_var, dur_var in rows:
                    sec = float(dur_var.get().strip().replace(",", "."))
                    if not math.isfinite(sec) or round(sec, 3) <= 0:
                        raise ValueError("Длительность должна быть > 0")
                    new_durs[eid] = round(sec, 3)
                    if en_var.get():
                        new_pat.append(eid)
                if not new_pat:
                    raise ValueError("Нужно выбрать хотя бы один переход")
            except ValueError as exc:
                messagebox.showerror("Ошибка", str(exc), parent=win)
                return
            TRANSITIONS_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            TRANSITIONS_CONFIG_FILE.write_text(
                json.dumps({"durations_sec": new_durs, "pattern": new_pat}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.write(f"\nГотово: сохранён паттерн переходов ({len(new_pat)} шт.) в transitions_config.json\n", "ok")
            win.destroy()

        redraw()
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Сбросить по умолчанию", command=on_reset).pack(side="left")
        ttk.Button(btns, text="Сохранить", command=on_save).pack(side="right")
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="right", padx=6)

    def update_capcut_status(self):
        if capcut_is_running():
            self.lbl_capcut.config(text="⚠ CapCut запущен — закрой его", foreground="#c0392b")
        else:
            self.lbl_capcut.config(text="CapCut закрыт ✓", foreground="#2e8b57")
        self.after(4000, self.update_capcut_status)

    # ---- лог -------------------------------------------------------------
    def clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _init_log_copy(self):
        self._log_menu = tk.Menu(self.log, tearoff=0)
        self._log_menu.add_command(label="Копировать", command=self.copy_selected_log)
        self._log_menu.add_command(label="Выделить всё", command=self.select_all_log)
        self._log_menu.add_separator()
        self._log_menu.add_command(label="Копировать весь лог", command=self.copy_all_log)

        self.log.bind("<Button-3>", self._show_log_menu)
        self.log.bind("<Control-KeyPress>", self._on_log_ctrl_key)

    def _show_log_menu(self, event):
        self.log.focus_set()
        has_sel = bool(self.log.tag_ranges("sel"))
        self._log_menu.entryconfig("Копировать", state="normal" if has_sel else "disabled")
        self._log_menu.tk_popup(event.x_root, event.y_root)

    def _on_log_ctrl_key(self, event):
        if event.keycode == 67 or event.keysym.lower() == "c":
            self.copy_selected_log()
            return "break"
        if event.keycode == 65 or event.keysym.lower() == "a":
            self.select_all_log()
            return "break"

    def select_all_log(self):
        self.log.tag_add("sel", "1.0", "end-1c")

    def copy_selected_log(self):
        try:
            text = self.log.get("sel.first", "sel.last")
        except tk.TclError:
            return
        self.clipboard_clear()
        self.clipboard_append(text)

    def copy_all_log(self):
        text = self.log.get("1.0", "end-1c")
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    def write(self, text, tag=None):
        self.log.config(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.config(state="disabled")

    @staticmethod
    def classify(line: str):
        low = line.lower()
        if "ошибка" in low or "traceback" in low or "error" in low or "не найден" in low or "[!]" in low:
            return "err"
        if "вотермарка" in low:
            return "wm"
        if line.startswith("Готово") or "файл не изменён" in low or "[✓]" in low:
            return "ok"
        if line.startswith("[") and "чисто" in low:
            return "dim"
        return None

    # ---- команды ---------------------------------------------------------
    def _python_exe(self) -> str:
        py = sys.executable
        if IS_WIN and py.lower().endswith("pythonw.exe"):
            cand = py[:-len("pythonw.exe")] + "python.exe"
            if os.path.exists(cand):
                return cand
        return py

    def _base_worker_cmd(self, kind: str, script_path: Path) -> list[str]:
        if IS_FROZEN:
            return [sys.executable, "--run-worker", kind]
        return [self._python_exe(), "-u", str(script_path)]

    def build_cmd(self, kind, project: Path | None):
        if kind == "stock":
            mode = self.s_mode.get()
            target = self.s_target.get().strip()
            if not target:
                raise ValueError("Укажи ссылку, поисковый запрос или файл.")
            out_dir = self.s_output.get().strip() or str(APP_DIR / "downloads")
            cmd = self._base_worker_cmd("stock", SCRIPT_STOCK) + [mode, target, "-o", out_dir]
            if mode == "yt":
                dur = self.s_duration.get().strip() or "30"
                if int(dur) <= 0:
                    raise ValueError("Длительность фрагмента должна быть > 0")
                cmd += ["-d", dur]
                if self.s_full.get():
                    cmd.append("--full")
            else:
                cnt = self.s_count.get().strip() or "3"
                if int(cnt) <= 0:
                    raise ValueError("Количество видео должно быть > 0")
                cmd += ["-n", cnt]
                if mode == "batch-tags":
                    cmd += ["--service", self.s_batch_srv.get()]
            return cmd

        if kind == "audio":
            cmd = self._base_worker_cmd("audio", SCRIPT_AUDIO) + [str(project)]
            intro_s = self.a_intro.get().strip().strip('"')
            ambient_s = self.a_ambient.get().strip().strip('"')
            if intro_s:
                cmd += ["--intro", intro_s]
            if ambient_s:
                cmd += ["--ambient", ambient_s]
            voice_s = self.a_voice.get().strip()
            if voice_s:
                cmd += ["--voice", voice_s]
            db_s = self.a_db.get().strip().replace(",", ".")
            if db_s:
                db_val = float(db_s)
                if not math.isfinite(db_val):
                    raise ValueError("Громкость (дБ) должна быть числом")
                cmd += ["--db", str(db_val)]
            fade_s = self.a_fade.get().strip().replace(",", ".")
            if fade_s:
                fade_val = float(fade_s)
                if not math.isfinite(fade_val) or fade_val < 0:
                    raise ValueError("Длительность fade должна быть >= 0")
                cmd += ["--fade", str(fade_val)]
            if not self.a_norm.get():
                cmd.append("--no-norm")
            if not self.a_vfade.get():
                cmd.append("--no-video-fade")
            if self.a_dry.get():
                cmd.append("--dry-run")
            return cmd

        if kind == "trans":
            cmd = self._base_worker_cmd("trans", SCRIPT_TRANS) + [str(project)]
            if self.t_dry.get():
                cmd.append("--dry-run")
            if self.t_skip.get():
                cmd.append("--skip-overlays")
            if self.t_skip_edges.get():
                cmd.append("--skip-edges")
            every_s = self.t_every.get().strip()
            if every_s:
                every = int(every_s)
                if every < 1:
                    raise ValueError("«Каждый N-й стык» должен быть >= 1")
                if every != 1:
                    cmd += ["--every", str(every)]
            chance_s = self.t_chance.get().strip().replace(",", ".")
            if chance_s:
                chance = float(chance_s)
                if not math.isfinite(chance) or not (0.0 <= chance <= 100.0):
                    raise ValueError("Вероятность должна быть от 0 до 100%")
                if chance != 100.0:
                    cmd += ["--chance", str(chance)]
            min_s = self.t_min_clip.get().strip().replace(",", ".")
            if min_s:
                min_clip = float(min_s)
                if not math.isfinite(min_clip) or min_clip < 0.0:
                    raise ValueError("Мин. длина клипа не может быть < 0")
                if min_clip > 0.0:
                    cmd += ["--min-clip", str(min_clip)]
            seed_s = self.t_seed.get().strip()
            if seed_s:
                cmd += ["--seed", str(int(seed_s))]
            return cmd

        cmd = self._base_worker_cmd("clear", SCRIPT_CLEAR) + [str(project)]
        scale = self.c_scale.get().strip().replace(",", ".")
        if scale:
            if float(scale) <= 0:
                raise ValueError("Масштаб должен быть > 0")
            cmd += ["--scale", scale]
        if self.c_dry.get():
            cmd.append("--dry-run")
        if self.c_reset.get():
            cmd.append("--reset-clean")
        if self.c_debug.get():
            cmd += ["--debug-dir", str(APP_DIR / "debug")]
        if self.c_thr.get().strip():
            cmd += ["--threshold", self.c_thr.get().strip().replace(",", ".")]
        if self.c_thr_single.get().strip():
            cmd += ["--threshold-single", self.c_thr_single.get().strip().replace(",", ".")]
        if self.c_track.get().strip():
            cmd += ["--track", self.c_track.get().strip()]
        if self.c_size.get().strip():
            cmd += ["--size", self.c_size.get().strip()]
        if self.c_probe.get().strip():
            cmd += ["--probe-times"] + self.c_probe.get().replace(",", ".").split()
        if self.c_extra.get().strip():
            cmd += shlex.split(self.c_extra.get(), posix=not IS_WIN)
        return cmd

    def start(self, kinds):
        if self.running:
            return

        need_project = any(k in ("clear", "trans", "audio") for k in kinds)
        project = self.current_project() if need_project else None
        if need_project:
            has_draft = False
            if project:
                try:
                    find_draft_file(project)
                    has_draft = True
                except DraftError:
                    has_draft = False
            if not has_draft:
                messagebox.showerror("Проект не выбран", "Выбери папку проекта, в которой есть draft_content.json или draft_info.json.")
                return

        if not IS_FROZEN:
            script_map = {"clear": SCRIPT_CLEAR, "trans": SCRIPT_TRANS, "audio": SCRIPT_AUDIO, "stock": SCRIPT_STOCK}
            for k in kinds:
                if not script_map[k].exists():
                    messagebox.showerror("Нет скрипта", f"Не найден файл:\n{script_map[k]}")
                    return

        try:
            cmds = [(k, self.build_cmd(k, project)) for k in kinds]
        except ValueError as exc:
            messagebox.showerror("Неверное значение", f"Проверь заполненные поля:\n{exc}")
            return

        will_write = (
            ("clear" in kinds and not self.c_dry.get())
            or ("trans" in kinds and not self.t_dry.get())
            or ("audio" in kinds and not self.a_dry.get())
        )
        if will_write and capcut_is_running():
            if not messagebox.askyesno(
                    "CapCut запущен",
                    "CapCut сейчас открыт — он может перезаписать изменения.\n"
                    "Лучше закрыть его и повторить.\n\nВсё равно запустить?"):
                return

        self.save_all()
        self.running = True
        self.stop_flag = False
        self.set_busy(True)
        threading.Thread(target=self.worker, args=(cmds,), daemon=True).start()

    def worker(self, cmds):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
        ok_all = True
        titles = {"clear": "Масштаб + вотермарка", "trans": "Переходы", "audio": "Аудио + Fade Out", "stock": "Скачивание видео"}
        for n, (kind, cmd) in enumerate(cmds, 1):
            title = titles.get(kind, kind)
            self.q.put(("line", f"\n=== {n}/{len(cmds)}: {title} ===\n", "cmd"))
            self.q.put(("line", "> " + " ".join(f'"{c}"' if " " in c else c for c in cmd) + "\n", "dim"))
            self.q.put(("status", f"Выполняется: {title}…"))
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(APP_DIR),
                    env=env, creationflags=flags, text=True, encoding="utf-8", errors="replace", bufsize=1)
                for line in self.proc.stdout:
                    self.q.put(("line", line, self.classify(line)))
                code = self.proc.wait()
            except Exception as e:  # noqa
                self.q.put(("line", f"Не удалось запустить: {e}\n", "err"))
                ok_all = False
                break
            if self.stop_flag:
                self.q.put(("line", "\n■ Остановлено пользователем\n", "err"))
                ok_all = False
                break
            if code != 0:
                self.q.put(("line", f"\nСкрипт завершился с кодом {code} — дальше не продолжаю.\n", "err"))
                ok_all = False
                break
        self.q.put(("done", ok_all))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.stop_flag = True
            if IS_WIN:
                try:
                    subprocess.Popen(
                        ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    return
                except Exception:
                    pass
            self.proc.terminate()

    def poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "line":
                    self.write(item[1], item[2])
                elif item[0] == "status":
                    self.lbl_status.config(text=item[1])
                elif item[0] == "done":
                    self.running = False
                    self.proc = None
                    self.set_busy(False)
                    self.lbl_status.config(text="Готово ✓" if item[1] else "Завершено с ошибкой / остановлено")
        except queue.Empty:
            pass
        self.after(60, self.poll_queue)

    def set_busy(self, busy):
        self.btn_stop.config(state="normal" if busy else "disabled")
        self.btn_stock_stop.config(state="normal" if busy else "disabled")
        self.btn_both.config(state="disabled" if busy else "normal")
        self.btn_stock_run.config(state="disabled" if busy else "normal")
        for w in self.winfo_children():
            self._toggle_buttons(w, busy)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
            self._update_stock_mode_ui()

    def _toggle_buttons(self, widget, busy):
        skip = (self.btn_stop, self.btn_stock_stop, self.btn_both, self.btn_stock_run)
        for ch in widget.winfo_children():
            if isinstance(ch, ttk.Button) and ch not in skip:
                ch.config(state="disabled" if busy else "normal")
            self._toggle_buttons(ch, busy)

    # ---- настройки -------------------------------------------------------
    def save_all(self):
        p = self.current_project()
        self.cfg.update(
            drafts_dir=str(self.drafts_dir), last_project=str(p) if p else "",
            t_dry=self.t_dry.get(), t_skip=self.t_skip.get(),
            t_skip_edges=self.t_skip_edges.get(), t_every=self.t_every.get(),
            t_chance=self.t_chance.get(), t_min_clip=self.t_min_clip.get(),
            t_seed=self.t_seed.get(),
            c_dry=self.c_dry.get(), c_scale=self.c_scale.get(), c_reset=self.c_reset.get(),
            c_debug=self.c_debug.get(), c_thr=self.c_thr.get(), c_thr_single=self.c_thr_single.get(),
            c_track=self.c_track.get(), c_size=self.c_size.get(), c_probe=self.c_probe.get(),
            c_extra=self.c_extra.get(),
            a_dry=self.a_dry.get(), a_intro=self.a_intro.get(), a_ambient=self.a_ambient.get(),
            a_voice=self.a_voice.get(), a_db=self.a_db.get(), a_fade=self.a_fade.get(),
            a_norm=self.a_norm.get(), a_vfade=self.a_vfade.get(),
            s_mode=self.s_mode.get(), s_target=self.s_target.get(), s_count=self.s_count.get(),
            s_duration=self.s_duration.get(), s_full=self.s_full.get(),
            s_batch_srv=self.s_batch_srv.get(), s_output=self.s_output.get(),
        )
        save_settings(self.cfg)

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Идёт выполнение", "Скрипт ещё работает. Остановить и выйти?"):
                return
            self.stop()
        self.save_all()
        self.destroy()


def _dispatch_worker() -> bool:
    """Точка входа для дочерних процессов при запуске собранного .exe."""
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-worker":
        for stream in (sys.stdout, sys.stderr):
            if stream and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
                except Exception:
                    pass
        kind = sys.argv[2]
        sys.argv = [sys.argv[0]] + sys.argv[3:]
        if kind == "clear":
            if clear_vids is None:
                sys.exit(f"Не удалось загрузить clear_vids: {_CLEAR_VIDS_ERR}")
            clear_vids.main()
        elif kind == "trans":
            make_transitions.main()
        elif kind == "audio":
            setup_audio.main()
        elif kind == "stock":
            stock_fetcher.main()
        else:
            sys.exit(f"Неизвестный режим: {kind}")
        return True
    return False


if __name__ == "__main__":
    if not _dispatch_worker():
        App().mainloop()
