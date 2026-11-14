#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stock_fetcher.py — модуль загрузки стоковых видео (YouTube, Pexels, Pixabay)
для проекта CapCut Automation. Может использоваться как из GUI, так и из CLI.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

CONFIG_PATH = APP_DIR / "stock_config.json"


def load_config() -> dict:
    """Загружает настройки Stock Fetcher из stock_config.json (или config.json) и env."""
    default_config = {
        "pexels_api_key": os.getenv("PEXELS_API_KEY", ""),
        "pixabay_api_key": os.getenv("PIXABAY_API_KEY", ""),
        "download_dir": "downloads",
        "default_clip_duration": 30,
    }
    for cand in (CONFIG_PATH, APP_DIR / "config.json"):
        if cand.exists():
            try:
                user_conf = json.loads(cand.read_text(encoding="utf-8"))
                for k, v in user_conf.items():
                    if v or not default_config.get(k):
                        default_config[k] = v
                break
            except Exception as exc:
                print(f"[!] Ошибка чтения {cand.name}: {exc}")
    return default_config


def save_config(new_conf: dict) -> dict:
    """Обновляет и сохраняет настройки в stock_config.json."""
    cfg = load_config()
    cfg.update(new_conf)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


CONFIG = load_config()


def sanitize_filename(name: str, max_len: int = 100) -> str:
    """Удаляет запрещённые в Windows символы и обрезает длину."""
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" ._")
    return sanitized[:max_len] if sanitized else "clip"


def parse_youtube_timestamp(url: str) -> int | None:
    """Извлекает таймкод в секундах из URL (t=87, t=87s, t=1m27s, t=1h2m3s)."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    t_val = None
    if "t" in qs:
        t_val = qs["t"][0]
    elif parsed.fragment and "t=" in parsed.fragment:
        t_match = re.search(r"t=([0-9hms]+)", parsed.fragment)
        if t_match:
            t_val = t_match.group(1)

    if not t_val:
        return None
    if re.fullmatch(r"\d+s?", t_val):
        return int(t_val.rstrip("s"))

    hours = int(m.group(1)) if (m := re.search(r"(\d+)h", t_val)) else 0
    minutes = int(m.group(1)) if (m := re.search(r"(\d+)m", t_val)) else 0
    seconds = int(m.group(1)) if (m := re.search(r"(\d+)s", t_val)) else 0
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def download_file_stream(url: str, dest_path: Path, headers: dict | None = None) -> bool:
    """Скачивает файл через urllib с отображением прогресса (совместимо с GUI и терминалом)."""
    part_path = dest_path.with_name(dest_path.name + ".part")
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, headers=req_headers)
        is_tty = bool(sys.stdout and sys.stdout.isatty())
        last_step = -1

        with urllib.request.urlopen(req, timeout=60) as response:
            try:
                total_size = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total_size = 0
            downloaded = 0
            print(f"[*] Скачивание: {dest_path.name}")
            with open(part_path, "wb") as f:
                while True:
                    chunk = response.read(1024 * 512)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = (downloaded / total_size) * 100
                        mb_done = downloaded / (1024 * 1024)
                        mb_total = total_size / (1024 * 1024)
                        if is_tty:
                            sys.stdout.write(f"\r    Прогресс: {percent:.1f}% ({mb_done:.1f}/{mb_total:.1f} MB)")
                            sys.stdout.flush()
                        else:
                            step = int(percent // 25)
                            if step > last_step and step > 0:
                                last_step = step
                                print(f"    Прогресс: {percent:.0f}% ({mb_done:.1f}/{mb_total:.1f} MB)")
        if is_tty:
            print()
        part_path.replace(dest_path)
        print(f"Готово: {dest_path}")
        return True
    except Exception as exc:
        print(f"[!] Ошибка при скачивании {url}: {exc}")
        if part_path.exists():
            part_path.unlink(missing_ok=True)
        return False


def http_get_json(url: str, headers: dict | None = None, params: dict | None = None) -> dict | None:
    """Выполняет GET-запрос и возвращает JSON."""
    try:
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"
        req_headers = {"User-Agent": "Mozilla/5.0"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"[!] HTTP ошибка {exc.code}: {exc.reason}")
        return None
    except Exception as exc:
        print(f"[!] Ошибка запроса: {exc}")
        return None


# ==========================================
# Модуль YouTube (через yt-dlp)
# ==========================================
def download_youtube_url(url: str, output_dir: Path, duration: int = 30, full: bool = False) -> bool:
    """Скачивает YouTube-видео (или фрагмент с таймкода)."""
    try:
        import yt_dlp
    except ImportError:
        print("[!] Ошибка: yt-dlp не установлен. Установите: pip install yt-dlp")
        return False

    if duration <= 0 and not full:
        print("[!] Ошибка: длительность фрагмента должна быть больше 0.")
        return False

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = parse_youtube_timestamp(url)
    if timestamp is not None and not full:
        start_sec = timestamp
        end_sec = timestamp + duration
        outtmpl = str(output_dir / f"%(title).60s [%(id)s] [{start_sec}-{end_sec}s].%(ext)s")
    else:
        outtmpl = str(output_dir / "%(title).60s [%(id)s].%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": True,
        "windowsfilenames": True,
    }

    if timestamp is not None and not full:
        print(f"[*] Найден таймкод: t={start_sec}s. Скачиваем фрагмент [{start_sec}s - {end_sec}s] ({duration} сек)")

        def download_ranges_callback(info_dict, ydl):
            return [{"start_time": start_sec, "end_time": end_sec}]

        ydl_opts["download_ranges"] = download_ranges_callback
        ydl_opts["force_keyframes_at_cuts"] = True
    else:
        print(f"[*] Скачиваем видео целиком: {url}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print(f"Готово: видео сохранено в {output_dir}")
        return True
    except Exception as exc:
        print(f"[!] Ошибка yt-dlp: {exc}")
        return False


# ==========================================
# Модуль Pexels Video API
# ==========================================
def search_pexels_videos(query: str, count: int = 3) -> list[dict]:
    if count <= 0:
        return []
    cfg = load_config()
    api_key = cfg.get("pexels_api_key")
    if not api_key:
        print("[!] Ошибка: не указан pexels_api_key (stock_config.json или PEXELS_API_KEY).")
        print("    Получить бесплатный ключ: https://www.pexels.com/api/")
        return []

    data = http_get_json(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": api_key},
        params={"query": query, "per_page": min(80, count), "orientation": "landscape"},
    )
    if not data:
        return []

    videos = []
    for item in data.get("videos", [])[:count]:
        files = [f for f in item.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("link")]
        if not files:
            continue
        best_file = max(files, key=lambda x: x.get("width") or 0)
        videos.append({
            "id": item["id"],
            "width": best_file.get("width") or 0,
            "height": best_file.get("height") or 0,
            "url": best_file["link"],
        })
    return videos


def download_pexels(query: str, count: int, output_dir: Path) -> int:
    print(f"[*] Поиск на Pexels: '{query}' (лимит: {count})...")
    videos = search_pexels_videos(query, count)
    if not videos:
        print("[-] Ничего не найдено.")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    ok_count = 0
    safe_query = sanitize_filename(query, max_len=60)
    for idx, v in enumerate(videos, 1):
        filename = f"pexels_{safe_query}_{v['id']}_{v['width']}x{v['height']}.mp4"
        filepath = output_dir / filename
        if filepath.exists():
            print(f"[i] Уже скачано: {filename}")
            ok_count += 1
            continue
        print(f"[{idx}/{len(videos)}] {filename} ({v['width']}x{v['height']})")
        if download_file_stream(v["url"], filepath):
            ok_count += 1
    return ok_count


# ==========================================
# Модуль Pixabay Video API
# ==========================================
def search_pixabay_videos(query: str, count: int = 3) -> list[dict]:
    if count <= 0:
        return []
    cfg = load_config()
    api_key = cfg.get("pixabay_api_key")
    if not api_key:
        print("[!] Ошибка: не указан pixabay_api_key (stock_config.json или PIXABAY_API_KEY).")
        print("    Получить бесплатный ключ: https://pixabay.com/api/docs/")
        return []

    # Pixabay API требует per_page в диапазоне 3..200; обрезаем до count после ответа.
    per_page = max(3, min(200, int(count)))
    data = http_get_json(
        "https://pixabay.com/api/videos/",
        params={"key": api_key, "q": query, "per_page": per_page, "video_type": "film"},
    )
    if not data:
        return []

    videos = []
    for item in data.get("hits", [])[:count]:
        v_types = item.get("videos", {})
        best_opt = None
        for quality in ("large", "medium", "small", "tiny"):
            cand = v_types.get(quality)
            if isinstance(cand, dict) and cand.get("url"):
                best_opt = cand
                break
        if best_opt:
            videos.append({
                "id": item["id"],
                "width": best_opt.get("width") or 0,
                "height": best_opt.get("height") or 0,
                "url": best_opt["url"],
            })
    return videos


def download_pixabay(query: str, count: int, output_dir: Path) -> int:
    print(f"[*] Поиск на Pixabay: '{query}' (лимит: {count})...")
    videos = search_pixabay_videos(query, count)
    if not videos:
        print("[-] Ничего не найдено.")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    ok_count = 0
    safe_query = sanitize_filename(query, max_len=60)
    for idx, v in enumerate(videos, 1):
        filename = f"pixabay_{safe_query}_{v['id']}_{v['width']}x{v['height']}.mp4"
        filepath = output_dir / filename
        if filepath.exists():
            print(f"[i] Уже скачано: {filename}")
            ok_count += 1
            continue
        print(f"[{idx}/{len(videos)}] {filename} ({v['width']}x{v['height']})")
        if download_file_stream(v["url"], filepath):
            ok_count += 1
    return ok_count


# ==========================================
# Пакетная обработка файла тегов
# ==========================================
def parse_tags_file(file_path: Path) -> list[dict]:
    categories = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            raw_category, raw_tags = line.split(":", 1)
            cat_name = sanitize_filename(raw_category.strip())
            tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
            if tags:
                categories.append({"category": cat_name, "tags": tags})
    return categories


def process_batch_tags(file_path: Path, service: str = "pexels", per_tag_count: int = 1, out_base: Path | None = None) -> int:
    if not file_path.exists():
        print(f"[!] Ошибка: файл не найден: {file_path}")
        return -1

    categories = parse_tags_file(file_path)
    print(f"[*] Найдено категорий: {len(categories)}")
    out_base = out_base or Path(load_config().get("download_dir", "downloads"))

    total_ok = 0
    for cat_idx, cat in enumerate(categories, 1):
        cat_dir = out_base / f"{cat_idx:02d}_{cat['category']}"
        print(f"\n=== Категория [{cat_idx}/{len(categories)}]: {cat['category']} ===")
        print(f"Папка: {cat_dir}")
        for tag in cat["tags"]:
            if service == "pexels":
                total_ok += download_pexels(tag, count=per_tag_count, output_dir=cat_dir)
            elif service == "pixabay":
                total_ok += download_pixabay(tag, count=per_tag_count, output_dir=cat_dir)
    return total_ok


def main():
    parser = argparse.ArgumentParser(
        description="Stock Fetcher — скачивание стоковых видео (YouTube, Pexels, Pixabay)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Команды")

    p_yt = subparsers.add_parser("yt", help="Скачать YouTube видео (по ссылке или файлу)")
    p_yt.add_argument("target", help="Ссылка на видео или путь к .txt файлу со списком ссылок")
    p_yt.add_argument("-d", "--duration", type=int, default=CONFIG.get("default_clip_duration", 30),
                      help="Длительность фрагмента в сек от таймкода (по умолч. 30)")
    p_yt.add_argument("--full", action="store_true", help="Игнорировать таймкод и качать видео целиком")
    p_yt.add_argument("-o", "--output", default="downloads/youtube", help="Папка для сохранения")

    p_pex = subparsers.add_parser("pexels", help="Поиск и скачивание с Pexels")
    p_pex.add_argument("query", help="Поисковый запрос на английском")
    p_pex.add_argument("-n", "--count", type=int, default=3, help="Количество видео")
    p_pex.add_argument("-o", "--output", default="downloads/pexels", help="Папка для сохранения")

    p_pix = subparsers.add_parser("pixabay", help="Поиск и скачивание с Pixabay")
    p_pix.add_argument("query", help="Поисковый запрос на английском")
    p_pix.add_argument("-n", "--count", type=int, default=3, help="Количество видео")
    p_pix.add_argument("-o", "--output", default="downloads/pixabay", help="Папка для сохранения")

    p_batch = subparsers.add_parser("batch-tags", help="Пакетная выкачка по файлу тегов")
    p_batch.add_argument("file", help="Путь к .txt файлу с категориями и тегами")
    p_batch.add_argument("--service", choices=["pexels", "pixabay"], default="pexels", help="Сервис стоков")
    p_batch.add_argument("-n", "--count", type=int, default=1, help="Сколько видео качать на каждый тег")
    p_batch.add_argument("-o", "--output", default="downloads/stock_scenes", help="Базовая папка")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == "yt":
        out_path = Path(args.output)
        target_path = Path(args.target)
        if target_path.is_file():
            urls = [
                line.strip()
                for line in target_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            ]
            if not urls:
                print(f"[!] В файле нет ссылок: {target_path}")
                sys.exit(1)
            print(f"[*] Найдено ссылок в файле: {len(urls)}")
            ok = 0
            for idx, u in enumerate(urls, 1):
                print(f"\n--- Ролик {idx}/{len(urls)} ---")
                if download_youtube_url(u, out_path, duration=args.duration, full=args.full):
                    ok += 1
            if ok == 0:
                sys.exit(1)
        elif args.target.lower().endswith(".txt") and not target_path.exists():
            print(f"[!] Ошибка: файл со ссылками не найден: {args.target}")
            sys.exit(1)
        else:
            if not download_youtube_url(args.target, out_path, duration=args.duration, full=args.full):
                sys.exit(1)
    elif args.command == "pexels":
        if not load_config().get("pexels_api_key"):
            print("[!] Ошибка: не указан pexels_api_key (stock_config.json или PEXELS_API_KEY).")
            sys.exit(1)
        download_pexels(args.query, args.count, Path(args.output))
    elif args.command == "pixabay":
        if not load_config().get("pixabay_api_key"):
            print("[!] Ошибка: не указан pixabay_api_key (stock_config.json или PIXABAY_API_KEY).")
            sys.exit(1)
        download_pixabay(args.query, args.count, Path(args.output))
    elif args.command == "batch-tags":
        key_name = "pexels_api_key" if args.service == "pexels" else "pixabay_api_key"
        if not load_config().get(key_name):
            print(f"[!] Ошибка: не указан {key_name} (stock_config.json или env).")
            sys.exit(1)
        res = process_batch_tags(
            Path(args.file),
            service=args.service,
            per_tag_count=args.count,
            out_base=Path(args.output),
        )
        if res < 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
