@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if exist "backend\.env" for /f "usebackq tokens=1,* delims==" %%A in ("backend\.env") do (
    if /i "%%A"=="MDM_HOST" if not defined MDM_HOST set "MDM_HOST=%%B"
    if /i "%%A"=="MDM_PORT" if not defined MDM_PORT set "MDM_PORT=%%B"
    if /i "%%A"=="MDM_THREADS" if not defined MDM_THREADS set "MDM_THREADS=%%B"
    if /i "%%A"=="MDM_PRODUCTION" if not defined MDM_PRODUCTION set "MDM_PRODUCTION=%%B"
    if /i "%%A"=="MDM_DB_PATH" if not defined MDM_DB_PATH set "MDM_DB_PATH=%%B"
    if /i "%%A"=="MDM_LOG_DIR" if not defined MDM_LOG_DIR set "MDM_LOG_DIR=%%B"
)
if not defined MDM_HOST set "MDM_HOST=0.0.0.0"
if not defined MDM_PORT set "MDM_PORT=5000"
if not defined MDM_THREADS set "MDM_THREADS=8"
if not defined MDM_PRODUCTION set "MDM_PRODUCTION=1"
if not defined MDM_LOG_DIR set "MDM_LOG_DIR=%~dp0runtime\logs"
if not defined MDM_DB_PATH (
    if exist "%~dp0backend\mdm_data.db" (
        set "MDM_DB_PATH=%~dp0backend\mdm_data.db"
    ) else (
        set "MDM_DB_PATH=%~dp0runtime\data\mdm_data.db"
    )
)
if not exist "%~dp0runtime\data" mkdir "%~dp0runtime\data"
if not exist "%~dp0runtime\logs" mkdir "%~dp0runtime\logs"

set "LOCAL_URL=http://127.0.0.1:%MDM_PORT%"
set "HEALTH_URL=%LOCAL_URL%/api/health"
if /i "%MDM_OCR_RUNTIME%"=="1" (set "VENV_DIR=%~dp0.venv-ocr") else (set "VENV_DIR=%~dp0.venv")
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

echo ============================================
echo   M-AI Master - Enterprise Launcher
echo ============================================

powershell -NoProfile -Command "try { $r=Invoke-RestMethod -Uri '%HEALTH_URL%' -TimeoutSec 2; if($r.ready -eq $true -and $r.version -eq '4.3'){exit 0}; exit 1 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    echo M-AI Master 4.3 is already running.
    echo Local: %LOCAL_URL%
    if not defined MDM_NO_BROWSER start "" "%LOCAL_URL%"
    exit /b 0
)

powershell -NoProfile -Command "if(Get-NetTCPConnection -LocalPort %MDM_PORT% -State Listen -ErrorAction SilentlyContinue){exit 0}; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo [ERROR] Port %MDM_PORT% is occupied by another or older service.
    echo Stop the old server with Ctrl+C, or change MDM_PORT in backend\.env.
    pause
    exit /b 1
)

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" --version >nul 2>&1
    if not errorlevel 1 goto runtime_ready
)

where py >nul 2>&1
if not errorlevel 1 (
    set "BOOTSTRAP_PYTHON=py -3"
    goto create_runtime
)
where python >nul 2>&1
if not errorlevel 1 (
    set "BOOTSTRAP_PYTHON=python"
    goto create_runtime
)
echo [ERROR] Python 3.11 or later was not found.
echo Install Python from https://www.python.org/downloads/ and enable Add Python to PATH.
pause
exit /b 1

:create_runtime
echo [1/4] Creating isolated Python environment...
%BOOTSTRAP_PYTHON% -m venv --clear "%VENV_DIR%"
if errorlevel 1 goto runtime_error

:runtime_ready
echo [1/4] Python runtime:
"%VENV_PYTHON%" --version
if errorlevel 1 goto runtime_error

echo [2/4] Checking dependencies...
if /i "%MDM_OCR_RUNTIME%"=="1" (
    "%VENV_PYTHON%" -c "import flask,chardet,networkx,numpy,paddleocr,pandas,requests,sklearn,waitress" >nul 2>&1
) else (
    "%VENV_PYTHON%" -c "import flask,chardet,networkx,numpy,pandas,requests,sklearn,waitress" >nul 2>&1
)
if not errorlevel 1 goto dependencies_ready
echo Installing dependencies into .venv. The first run may take several minutes...
"%VENV_PYTHON%" -m pip install --upgrade pip
if /i "%MDM_OCR_RUNTIME%"=="1" (
    "%VENV_PYTHON%" -m pip install -r "backend\requirements-ocr.txt"
) else (
    "%VENV_PYTHON%" -m pip install -r "backend\requirements.txt"
)
if errorlevel 1 goto dependency_error

:dependencies_ready
echo [3/4] Network addresses:
echo Local: %LOCAL_URL%
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254*'} | ForEach-Object { Write-Host ('LAN:   http://' + $_.IPAddress + ':%MDM_PORT%') }"
echo Health: %HEALTH_URL%
echo Data:   %MDM_DB_PATH%
echo.
echo Other computers must use a LAN address printed above.
echo If they cannot connect, allow TCP port %MDM_PORT% in Windows Firewall.
echo.

echo [4/4] Starting production WSGI server...
if not defined MDM_NO_BROWSER start "" powershell -NoProfile -WindowStyle Hidden -Command "$u='%LOCAL_URL%'; for($i=0;$i -lt 120;$i++){try{$h=Invoke-RestMethod -Uri ($u+'/api/health') -TimeoutSec 1;if($h.ready){Start-Process $u;exit 0}}catch{};Start-Sleep -Milliseconds 500};exit 1"
cd /d "%~dp0backend"
"%VENV_PYTHON%" app.py
set "SERVER_EXIT=%ERRORLEVEL%"
if not "%SERVER_EXIT%"=="0" (
    echo.
    echo [ERROR] Server stopped with exit code %SERVER_EXIT%.
    echo See %MDM_LOG_DIR%\mai-master.log for details.
    pause
)
exit /b %SERVER_EXIT%

:runtime_error
echo [ERROR] Failed to create or start the isolated Python environment.
pause
exit /b 1

:dependency_error
echo [ERROR] Dependency installation failed. Check network or the pip error above.
pause
exit /b 1
