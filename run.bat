@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] Sozdanie virtualnogo okruzheniya .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo Ne udalos sozdat .venv. Proverьте, chto Python ustanovlen.
        pause
        exit /b 1
    )
    echo [2/2] Ustanovka zavisimostey iz requirements.txt...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

start "" ".venv\Scripts\pythonw.exe" "%~dp0gui.py"
