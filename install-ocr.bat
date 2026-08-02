@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "OCR_VENV=%~dp0.venv-ocr"

py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PaddleOCR runtime requires Python 3.11 for this delivery.
    echo Install Python 3.11 x64, then run this file again.
    pause
    exit /b 1
)

if not exist "%OCR_VENV%\Scripts\python.exe" (
    echo [1/3] Creating isolated Python 3.11 OCR environment...
    py -3.11 -m venv "%OCR_VENV%"
    if errorlevel 1 exit /b 1
)

echo [2/3] Installing PaddleOCR CPU dependencies...
"%OCR_VENV%\Scripts\python.exe" -m pip install --upgrade pip
"%OCR_VENV%\Scripts\python.exe" -m pip install -r "backend\requirements-ocr.txt"
if errorlevel 1 (
    echo [ERROR] OCR dependency installation failed. Check the network and pip output above.
    pause
    exit /b 1
)

echo [3/3] Verifying OCR runtime...
"%OCR_VENV%\Scripts\python.exe" -c "import paddle,paddleocr; print('PaddleOCR runtime is ready')"
if errorlevel 1 exit /b 1
echo Run start-ocr.bat to start the platform with real local OCR.
pause
