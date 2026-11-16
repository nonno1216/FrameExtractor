@echo off
cd /d "%~dp0"
set NAME=FrameExtractorAI
set GPU=0
if /i "%~1"=="gpu" set GPU=1
if "%GPU%"=="1" set NAME=FrameExtractorAI_GPU
echo ===== Frame Extractor : build exe WITH AI (rembg) : %NAME% =====
echo This takes a long time and the result is large (several hundred MB).
where py >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python from https://www.python.org/downloads/
  echo Check "Add python.exe to PATH" during setup, then run this file again.
  pause
  exit /b 1
)
if "%GPU%"=="1" (
  py -m pip uninstall -y onnxruntime onnxruntime-gpu
  py -m pip install --upgrade opencv-python-headless pillow customtkinter yt-dlp imageio-ffmpeg tkinterdnd2 pyinstaller rembg onnxruntime-directml
) else (
  py -m pip uninstall -y onnxruntime-directml
  py -m pip install --upgrade opencv-python-headless pillow customtkinter yt-dlp imageio-ffmpeg tkinterdnd2 pyinstaller "rembg[cpu]"
)
if errorlevel 1 goto fail
py convert_icon.py
set ICONOPT=
if exist app.ico set ICONOPT=--icon app.ico --add-data "app.ico;."
py -m PyInstaller --onedir --windowed --noconfirm --name %NAME% %ICONOPT% ^
  --collect-all customtkinter --collect-all yt_dlp --collect-all imageio_ffmpeg --collect-all tkinterdnd2 ^
  --collect-all rembg --collect-all onnxruntime --collect-all pymatting --collect-all numba ^
  --collect-all llvmlite --collect-all skimage --collect-all pooch ^
  --add-data "browser_extension;browser_extension" ^
  frame_extractor.py
if errorlevel 1 goto fail
echo.
echo Checking that the AI is inside the exe...
if exist "dist\%NAME%\ai_check.txt" del "dist\%NAME%\ai_check.txt"
start /wait "" "dist\%NAME%\%NAME%.exe" --check-ai
if exist "dist\%NAME%\ai_check.txt" (
  type "dist\%NAME%\ai_check.txt"
) else (
  echo The check did not run. The AI may not work in this exe.
)
echo.
echo Done. Use this file: dist\%NAME%\%NAME%.exe
echo Keep the whole "%NAME%" folder together when you move or share it.
explorer dist\%NAME%
pause
exit /b 0
:fail
echo Build failed. Please check the messages above.
pause
exit /b 1
