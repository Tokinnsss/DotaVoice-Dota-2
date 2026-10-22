@echo off
cd /d "%~dp0"
echo === Building DotaVoice.exe ===

if not exist "dota_voice_app.py" (
    echo ERROR: dota_voice_app.py not found next to this file.
    pause
    exit /b 1
)

if not exist "vosk-model-small-ru-0.22\am" (
    echo ERROR: model folder not found or unpacked incorrectly.
    echo Download vosk-model-small-ru-0.22 from https://alphacephei.com/vosk/models
    echo and unpack it so that this path exists:
    echo   vosk-model-small-ru-0.22\am
    echo ^(often the archive unpacks into a nested folder - move the inner one up^)
    pause
    exit /b 1
)

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ and tick "Add to PATH".
    pause
    exit /b 1
)

python -m pip install vosk sounddevice psutil pystray pillow pyinstaller
if errorlevel 1 (
    echo ERROR: failed to install dependencies.
    pause
    exit /b 1
)

python -m PyInstaller --noconfirm --clean --onefile --windowed --name DotaVoice --collect-all vosk --collect-all sounddevice --collect-submodules pystray --hidden-import pystray._win32 --add-data "vosk-model-small-ru-0.22;vosk-model-small-ru-0.22" dota_voice_app.py
if errorlevel 1 (
    echo ERROR: build failed. See messages above.
    pause
    exit /b 1
)

echo.
echo DONE! File: %~dp0dist\DotaVoice.exe
pause
