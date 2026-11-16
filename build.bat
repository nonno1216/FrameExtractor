@echo off
cd /d "%~dp0"
echo ===== Frame Extractor : build exe =====
where py >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python from https://www.python.org/downloads/
  echo Check "Add python.exe to PATH" during setup, then run this file again.
  pause
  exit /b 1
)
py -m pip install --upgrade opencv-python-headless pillow customtkinter yt-dlp imageio-ffmpeg tkinterdnd2 pyinstaller
if errorlevel 1 goto fail
py convert_icon.py
set ICONOPT=
if exist app.ico set ICONOPT=--icon app.ico --add-data "app.ico;."
py -m PyInstaller --onefile --windowed --noconfirm --collect-all customtkinter --collect-all yt_dlp --collect-all imageio_ffmpeg --collect-all tkinterdnd2 --add-data "browser_extension;browser_extension" --name FrameExtractor %ICONOPT% frame_extractor.py
if errorlevel 1 goto fail
echo.
echo Done. Your exe is here: dist\FrameExtractor.exe
explorer dist
pause
exit /b 0
:fail
echo Build failed. Please check the messages above.
pause
exit /b 1
