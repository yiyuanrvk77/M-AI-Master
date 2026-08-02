@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv-ocr\Scripts\python.exe" (
    call install-ocr.bat
    if errorlevel 1 exit /b 1
)
set "MDM_OCR_RUNTIME=1"
set "MDM_PADDLEOCR_ENABLED=1"
call start.bat
