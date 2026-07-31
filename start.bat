@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if exist "backend\.env" for /f "usebackq tokens=1,* delims==" %%A in ("backend\.env") do (
    if /i "%%A"=="MDM_HOST" if not defined MDM_HOST set "MDM_HOST=%%B"
    if /i "%%A"=="MDM_PORT" if not defined MDM_PORT set "MDM_PORT=%%B"
)
if not defined MDM_HOST set "MDM_HOST=127.0.0.1"
if not defined MDM_PORT set "MDM_PORT=5000"
set "BROWSER_HOST=%MDM_HOST%"
if "%BROWSER_HOST%"=="0.0.0.0" set "BROWSER_HOST=127.0.0.1"
set "APP_URL=http://%BROWSER_HOST%:%MDM_PORT%"
set "HEALTH_URL=%APP_URL%/api/health"

echo ============================================
echo   M-AI Master - Flask Launcher
echo ============================================

powershell -NoProfile -Command "try { $response = Invoke-RestMethod -Uri '%HEALTH_URL%' -TimeoutSec 2; if ($response.status -eq 'ok' -and $response.database -eq 'mdm_data.db') { exit 0 }; exit 1 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 goto find_python

echo M-AI Master is already running.
echo Opening %APP_URL%
if not defined MDM_NO_BROWSER start "" "%APP_URL%"
exit /b 0

:find_python
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %MDM_PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 }; exit 1" >nul 2>&1
if errorlevel 1 goto locate_runtime

echo [ERROR] Port %MDM_PORT% is already used by another program.
echo Stop that program or set MDM_PORT to another port before running this file.
pause
exit /b 1

:locate_runtime

where py >nul 2>&1
if not errorlevel 1 goto use_py

where python >nul 2>&1
if not errorlevel 1 goto use_python

echo [ERROR] Python 3 was not found.
echo Install Python 3.11 or later, then run this file again.
pause
exit /b 1

:use_py
set "PYTHON_CMD=py -3"
goto python_ready

:use_python
set "PYTHON_CMD=python"

:python_ready
echo [1/3] Python runtime:
%PYTHON_CMD% --version
if errorlevel 1 goto python_error

echo [2/3] Checking dependencies...
%PYTHON_CMD% -c "import flask, chardet, numpy, pandas, requests, sklearn" >nul 2>&1
if not errorlevel 1 goto dependencies_ready

echo Installing missing dependencies...
%PYTHON_CMD% -m pip install -r "backend\requirements.txt"
if errorlevel 1 goto dependency_error

:dependencies_ready
echo [3/3] Starting Flask server...
echo.
echo Open this address in your browser:
echo %APP_URL%
echo Health check: %HEALTH_URL%
echo.
echo Keep this window open. Press Ctrl+C to stop the server.
echo.

if not defined MDM_NO_BROWSER start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%APP_URL%'"
cd /d "%~dp0backend"
%PYTHON_CMD% app.py
set "SERVER_EXIT=%ERRORLEVEL%"
if not "%SERVER_EXIT%"=="0" (
    echo.
    echo [ERROR] Flask stopped unexpectedly with exit code %SERVER_EXIT%.
    echo Check that port %MDM_PORT% is available, then run start.bat again.
    pause
)
exit /b %SERVER_EXIT%
goto end

:python_error
echo [ERROR] The detected Python runtime could not start.
pause
exit /b 1

:dependency_error
echo [ERROR] Dependency installation failed.
pause
exit /b 1

:end
endlocal
