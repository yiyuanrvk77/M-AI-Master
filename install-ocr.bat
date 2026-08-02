@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PIP_DEFAULT_TIMEOUT=120"
if exist "backend\.env" for /f "usebackq tokens=1,* delims==" %%A in ("backend\.env") do (
    if /i "%%A"=="MDM_PIP_INDEX_URL" if not defined MDM_PIP_INDEX_URL set "MDM_PIP_INDEX_URL=%%B"
)
set "PIP_PRIMARY_INDEX=%MDM_PIP_INDEX_URL%"
if not defined PIP_PRIMARY_INDEX set "PIP_PRIMARY_INDEX=https://mirrors.aliyun.com/pypi/simple"
set "PADDLE_PDX_MODEL_SOURCE=BOS"
set "OCR_VENV=%~dp0.venv-ocr"
set "READY_MARKER=%~dp0runtime\ocr-ready"
set "LOCK_DIR=%~dp0runtime\ocr-installing.lock"
set "NON_INTERACTIVE=0"
if /i "%~1"=="--non-interactive" set "NON_INTERACTIVE=1"

if not exist "%~dp0runtime" mkdir "%~dp0runtime"
mkdir "%LOCK_DIR%" >nul 2>&1
if errorlevel 1 (
    echo Another OCR installation is already running.
    exit /b 0
)
if exist "%READY_MARKER%" del /q "%READY_MARKER%" >nul 2>&1

echo [1/4] Checking Python 3.11 runtime...
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    py help install >nul 2>&1
    if not errorlevel 1 (
        echo Python 3.11 is missing. Downloading with Python Install Manager...
        py install -y 3.11
    )
)
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo Python Install Manager is unavailable. Trying winget...
    where winget >nul 2>&1
    if errorlevel 1 goto python_error
    winget install --id Python.Python.3.11 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
)
py -3.11 --version >nul 2>&1
if errorlevel 1 goto python_error

echo [2/4] Creating isolated Python 3.11 OCR environment...
if not exist "%OCR_VENV%\Scripts\python.exe" (
    py -3.11 -m venv "%OCR_VENV%"
    if errorlevel 1 goto install_error
)
"%OCR_VENV%\Scripts\python.exe" -m ensurepip --upgrade >nul 2>&1

echo [3/4] Installing PaddleOCR CPU dependencies...
echo Trying configured/Alibaba Cloud mirror: %PIP_PRIMARY_INDEX%
"%OCR_VENV%\Scripts\python.exe" -m pip install --prefer-binary --retries 5 --timeout 120 --index-url "%PIP_PRIMARY_INDEX%" -r "backend\requirements-ocr.txt"
if errorlevel 1 goto try_tsinghua
"%OCR_VENV%\Scripts\python.exe" -c "import paddle, paddleocr" >nul 2>&1
if not errorlevel 1 goto packages_ready

:try_tsinghua
echo Configured source failed validation. Trying Tsinghua mirror...
"%OCR_VENV%\Scripts\python.exe" -m pip install --prefer-binary --retries 5 --timeout 120 --index-url "https://pypi.tuna.tsinghua.edu.cn/simple" -r "backend\requirements-ocr.txt"
if errorlevel 1 goto try_official
"%OCR_VENV%\Scripts\python.exe" -c "import paddle, paddleocr" >nul 2>&1
if not errorlevel 1 goto packages_ready

:try_official
echo Tsinghua mirror failed validation. Trying official PyPI...
"%OCR_VENV%\Scripts\python.exe" -m pip install --prefer-binary --retries 5 --timeout 120 --index-url "https://pypi.org/simple" -r "backend\requirements-ocr.txt"
if errorlevel 1 goto install_error
"%OCR_VENV%\Scripts\python.exe" -c "import paddle, paddleocr" >nul 2>&1
if errorlevel 1 goto install_error

:packages_ready
echo Verifying installed PaddleOCR packages...
"%OCR_VENV%\Scripts\python.exe" -c "import paddle, paddleocr; print('PaddlePaddle', paddle.__version__, 'and PaddleOCR import OK')"
if errorlevel 1 goto install_error

echo [4/4] Downloading and verifying OCR models...
"%OCR_VENV%\Scripts\python.exe" "backend\ocr_worker.py" --check
if errorlevel 1 goto model_error

>"%READY_MARKER%" echo ready
echo PaddleOCR runtime is ready. Return to the browser and retry OCR.
goto success

:python_error
echo [ERROR] Python 3.11 could not be downloaded.
echo Check access to python.org or the winget source, then retry.
goto failed

:install_error
echo [ERROR] OCR packages could not be installed or imported.
echo Tried the configured mirror, Tsinghua mirror, and official PyPI.
echo Check proxy, antivirus, disk space, and network access, then retry.
goto failed

:model_error
echo [ERROR] PaddleOCR packages installed, but model download or initialization failed.
echo Check access to Paddle model storage, then retry.
goto failed

:success
if exist "%LOCK_DIR%" rmdir "%LOCK_DIR%" >nul 2>&1
if "%NON_INTERACTIVE%"=="0" pause
exit /b 0

:failed
if exist "%LOCK_DIR%" rmdir "%LOCK_DIR%" >nul 2>&1
if "%NON_INTERACTIVE%"=="0" pause
exit /b 1
