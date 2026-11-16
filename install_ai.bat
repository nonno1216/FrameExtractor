@echo off
cd /d "%~dp0"
echo Installing the AI cutout part (rembg, CPU). This may take a few minutes...
py -m pip uninstall -y onnxruntime-directml
py -m pip install --upgrade "rembg[cpu]"
if errorlevel 1 (
  echo Install failed. Please check the messages above.
) else (
  echo Done. Restart the app with start.bat.
)
pause
