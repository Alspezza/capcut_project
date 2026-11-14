@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] Sozdanie virtualnogo okruzheniya .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo Ne udalos sozdat .venv. Proverte, chto Python ustanovlen.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -c "import cv2, numpy, yt_dlp" >nul 2>&1
if errorlevel 1 (
    echo [2/2] Ustanovka zavisimostey iz requirements.txt...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Oshibka ustanovki zavisimostey!
        pause
        exit /b 1
    )
)

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "%~dp0gui.py"
) else (
    start "" ".venv\Scripts\python.exe" "%~dp0gui.py"
)
