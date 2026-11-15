# CapCut Automation + Stock Fetcher

Набор утилит с графическим интерфейсом (`gui.py`) для автоматизации подготовки проектов CapCut и загрузки стоковых видео.

## Модули

- **`gui.py`** — оконный интерфейс (вкладки *Обработка проекта* и *Скачать видео*).
- **`clear_vids.py`** — масштабирование клипов и скрытие вотермарок Gemini/Veo с помощью OpenCV.
- **`make_transitions.py`** — циклическая расстановка переходов по паттерну между клипами основной дорожки.
- **`stock_fetcher.py`** — скачивание видео с YouTube (через `yt-dlp`, с вырезкой фрагмента по таймкоду), Pexels и Pixabay, а также пакетная выкачка по файлу тегов.
- **`capcut_common.py`** — общие функции работы с черновиками CapCut (поиск `draft_content.json` / `draft_info.json`, проверка запущенного CapCut, атомарная запись и ротация `.backups`).

## Установка зависимостей

```bash
pip install -r requirements.txt
```

## Запуск

```bash
python gui.py
```

Для CLI-использования любого модуля можно передать `--help`:

```bash
python clear_vids.py --help
python make_transitions.py --help
python stock_fetcher.py --help
```

## Сборка `.exe` (PyInstaller)

Конфигурация сборки с иконкой зафиксирована в `CapCutAutomation.spec`:

```bash
pyinstaller CapCutAutomation.spec
```

Готовый файл появится в `dist/CapCutAutomation.exe`. Настройки GUI (`gui_settings.json`) и паттерн переходов (`transitions_config.json`) сохраняются в `%APPDATA%\CapCutAutomation`.

## Настройка переходов

По умолчанию паттерн переходов задан в `make_transitions.py`. Для переопределения без правки кода можно создать файл `transitions_config.json` в `%APPDATA%\CapCutAutomation` (или рядом со скриптом):

```json
{
  "durations_sec": {
    "7159450506648097281": 1.0,
    "7140916992118100482": 1.0,
    "7184693655641264641": 0.9,
    "7140916996262072833": 0.8
  },
  "pattern": [
    "7159450506648097281",
    "7140916992118100482",
    "7184693655641264641",
    "7140916996262072833"
  ]
}
```
