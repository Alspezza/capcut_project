#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py — оконный запуск make_transitions.py и clear_vids.py.

Положи рядом со скриптами (D:\\transitions_automatisation) и запускай:
    python gui.py
Чтобы окно открывалось без консоли — переименуй в gui.pyw и запускай двойным кликом.
Зависимости самого GUI: только стандартная библиотека (tkinter).
Для clear_vids.py по-прежнему нужны: pip install opencv-python numpy
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DRAFTS = Path(r"D:\Для работы\CapCut Drafts")
SETTINGS_FILE = APP_DIR / "gui_settings.json"
SCRIPT_CLEAR = APP_DIR / "clear_vids.py"
SCRIPT_TRANS = APP_DIR / "make_transitions.py"
IS_WIN = os.name == "nt"


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
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
            f = d / "draft_content.json"
            if d.is_dir() and f.exists():
                items.append((f.stat().st_mtime, d))
    items.sort(reverse=True)
    out = {}
    for mtime, d in items:
        name = draft_title(d)
        stamp = time.strftime("%d.%m %H:%M", time.localtime(mtime))
        label = f"{name}  |  {d.name}  |  {stamp}" if name else f"{d.name}  |  {stamp}"
        out[label] = d
    return out


def capcut_running() -> bool:
    if not IS_WIN:
        return False
    try:
        r = subprocess.run(["tasklist"], capture_output=True, text=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        low = r.stdout.lower()
        return "capcut.exe" in low or "capcutlauncher" in low
    except Exception:
        return False


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CapCut Automation")
        self.geometry("980x720")
        self.minsize(820, 600)

        self.cfg = load_settings()
        self.drafts_dir = Path(self.cfg.get("drafts_dir", str(DEFAULT_DRAFTS)))
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

    # ---- интерфейс -------------------------------------------------------
    def _build_ui(self):
        pad = dict(padx=10, pady=6)

        # --- проект
        pf = ttk.LabelFrame(self, text="Проект CapCut")
        pf.pack(fill="x", **pad)
        row = ttk.Frame(pf)
        row.pack(fill="x", padx=8, pady=(8, 2))
        self.combo = ttk.Combobox(row, textvariable=self.v_project, state="normal")
        self.combo.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Обзор…", command=self.browse_project).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="⟳", width=3, command=lambda: self.refresh_drafts()).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="Открыть папку", command=self.open_project_folder).pack(side="left", padx=(4, 0))

        row2 = ttk.Frame(pf)
        row2.pack(fill="x", padx=8, pady=(0, 8))
        self.lbl_dir = ttk.Label(row2, foreground="#666")
        self.lbl_dir.pack(side="left")
        ttk.Button(row2, text="Сменить папку черновиков…", command=self.pick_drafts_dir).pack(side="right")
        self.lbl_capcut = ttk.Label(row2, text="")
        self.lbl_capcut.pack(side="right", padx=12)

        # --- два блока настроек
        mid = ttk.Frame(self)
        mid.pack(fill="x", **pad)
        mid.columnconfigure(0, weight=1, uniform="a")
        mid.columnconfigure(1, weight=1, uniform="a")

        # Вотермарки / масштаб (запускается первым)
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
            e = ttk.Entry(adv, textvariable=var)
            e.grid(row=i, column=1, sticky="ew", padx=6, pady=2)
            ttk.Label(adv, text=hint, foreground="#999").grid(row=i, column=2, sticky="w", padx=(0, 6))
        adv.columnconfigure(1, weight=1)

        ttk.Button(cf, text="▶  Запустить", style="Run.TButton",
                   command=lambda: self.start(["clear"])).pack(fill="x", padx=8, pady=(4, 8))

        # Переходы
        tf = ttk.LabelFrame(mid, text="2. Переходы  (make_transitions.py)")
        tf.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ttk.Checkbutton(tf, text="Пробный прогон (ничего не записывать)", variable=self.t_dry).pack(anchor="w", padx=8, pady=(10, 2))
        ttk.Checkbutton(tf, text="Пропускать стыки, где начинается оверлей", variable=self.t_skip).pack(anchor="w", padx=8, pady=2)
        ttk.Label(tf, text="Паттерн и длительности задаются в самом\nmake_transitions.py (DURATIONS_MAP).",
                  foreground="#888", justify="left").pack(anchor="w", padx=8, pady=10)
        ttk.Frame(tf).pack(expand=True, fill="both")
        ttk.Button(tf, text="▶  Запустить", style="Run.TButton",
                   command=lambda: self.start(["trans"])).pack(fill="x", padx=8, pady=(4, 8), side="bottom")

        # --- общие кнопки
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(2, 4))
        self.btn_both = ttk.Button(bar, text="▶▶  Запустить оба подряд (масштаб → переходы)",
                                   style="Big.TButton", command=lambda: self.start(["clear", "trans"]))
        self.btn_both.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(bar, text="■ Стоп", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0), ipady=8)

        # --- лог
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
        self.progress = ttk.Progressbar(lrow, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        self.lbl_status = ttk.Label(lrow, text="Готов")
        self.lbl_status.pack(side="right", padx=8)

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
        if d:
            self.v_project.set(str(Path(d)))

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

    def update_capcut_status(self):
        if capcut_running():
            self.lbl_capcut.config(text="⚠ CapCut запущен — закрой его", foreground="#c0392b")
        else:
            self.lbl_capcut.config(text="CapCut закрыт ✓", foreground="#2e8b57")
        self.after(4000, self.update_capcut_status)

    # ---- лог -------------------------------------------------------------
    def clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def write(self, text, tag=None):
        self.log.config(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.config(state="disabled")

    @staticmethod
    def classify(line: str):
        low = line.lower()
        if "ошибка" in low or "traceback" in low or "error" in low or "не найден" in low:
            return "err"
        if "вотермарка" in low:
            return "wm"
        if line.startswith("Готово") or "файл не изменён" in low:
            return "ok"
        if line.startswith("[") and "чисто" in low:
            return "dim"
        return None

    # ---- команды ---------------------------------------------------------
    def build_cmd(self, kind, project: Path):
        py = sys.executable
        if IS_WIN and py.lower().endswith("pythonw.exe"):
            cand = py[:-len("pythonw.exe")] + "python.exe"
            if os.path.exists(cand):
                py = cand

        if kind == "trans":
            cmd = [py, "-u", str(SCRIPT_TRANS), str(project)]
            if self.t_dry.get():
                cmd.append("--dry-run")
            if self.t_skip.get():
                cmd.append("--skip-overlays")
            return cmd

        cmd = [py, "-u", str(SCRIPT_CLEAR), str(project)]
        scale = self.c_scale.get().strip().replace(",", ".")
        if scale:
            float(scale)  # проверка
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
            import shlex
            cmd += shlex.split(self.c_extra.get(), posix=not IS_WIN)
        return cmd

    def start(self, kinds):
        if self.running:
            return
        project = self.current_project()
        if not project or not (project.is_file() or (project / "draft_content.json").exists()):
            messagebox.showerror("Проект не выбран", "Выбери папку проекта, в которой есть draft_content.json.")
            return
        for k in kinds:
            script = SCRIPT_CLEAR if k == "clear" else SCRIPT_TRANS
            if not script.exists():
                messagebox.showerror("Нет скрипта", f"Не найден файл:\n{script}")
                return
        try:
            cmds = [(k, self.build_cmd(k, project)) for k in kinds]
        except ValueError:
            messagebox.showerror("Неверное значение", "Проверь числовые поля (масштаб, пороги).")
            return

        will_write = (("clear" in kinds and not self.c_dry.get()) or ("trans" in kinds and not self.t_dry.get()))
        if will_write and capcut_running():
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
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
        ok_all = True
        for n, (kind, cmd) in enumerate(cmds, 1):
            title = "Масштаб + вотермарка" if kind == "clear" else "Переходы"
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
        self.btn_both.config(state="disabled" if busy else "normal")
        for w in self.winfo_children():
            self._toggle_buttons(w, busy)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _toggle_buttons(self, widget, busy):
        for ch in widget.winfo_children():
            if isinstance(ch, ttk.Button) and ch not in (self.btn_stop, self.btn_both):
                ch.config(state="disabled" if busy else "normal")
            self._toggle_buttons(ch, busy)

    # ---- настройки -------------------------------------------------------
    def save_all(self):
        p = self.current_project()
        self.cfg.update(
            drafts_dir=str(self.drafts_dir), last_project=str(p) if p else "",
            t_dry=self.t_dry.get(), t_skip=self.t_skip.get(),
            c_dry=self.c_dry.get(), c_scale=self.c_scale.get(), c_reset=self.c_reset.get(),
            c_debug=self.c_debug.get(), c_thr=self.c_thr.get(), c_thr_single=self.c_thr_single.get(),
            c_track=self.c_track.get(), c_size=self.c_size.get(), c_probe=self.c_probe.get(),
            c_extra=self.c_extra.get())
        save_settings(self.cfg)

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Идёт выполнение", "Скрипт ещё работает. Остановить и выйти?"):
                return
            self.stop()
        self.save_all()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()