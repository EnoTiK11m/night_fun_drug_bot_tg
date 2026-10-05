@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "RESTART_DELAY=10"
set "LAUNCHER_LOG=logs\bat_launcher.log"
set "STARTUP_OUT=logs\startup_output.log"
set "STARTUP_ERR=logs\startup_errors.log"
set "STARTUP_OUT_PREVIOUS=logs\startup_output.previous.log"
set "STARTUP_ERR_PREVIOUS=logs\startup_errors.previous.log"
set "MODE=watchdog"
set "PYTHON=python"

cd /d "%~dp0"
if errorlevel 1 (
    echo ERROR: Cannot enter the launcher directory.
    exit /b 1
)
if not "%~2"=="" goto usage
if "%~1"=="" goto prepare
if /i "%~1"=="once" (
    set "MODE=once"
    goto prepare
)
goto usage

:prepare
if not exist "logs" mkdir "logs" >nul 2>&1
if not exist "logs\" (
    echo ERROR: Cannot create the logs directory.
    exit /b 1
)
if exist ".venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"
>> "%LAUNCHER_LOG%" echo [%date% %time%] Launcher started; mode=%MODE%; Python="%PYTHON%"
"%PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    >> "%LAUNCHER_LOG%" echo [%date% %time%] ERROR: Python is unavailable; launcher exiting.
    echo ERROR: Python is unavailable. Check .venv or install Python and add it to PATH.
    exit /b 1
)

:restart
if exist "%STARTUP_OUT%" (
    copy /y "%STARTUP_OUT%" "%STARTUP_OUT_PREVIOUS%" >nul 2>&1
    if errorlevel 1 goto log_error
)
if exist "%STARTUP_ERR%" (
    copy /y "%STARTUP_ERR%" "%STARTUP_ERR_PREVIOUS%" >nul 2>&1
    if errorlevel 1 goto log_error
)
> "%STARTUP_OUT%" echo [%date% %time%] Application started; mode=%MODE%
if errorlevel 1 goto log_error
> "%STARTUP_ERR%" echo [%date% %time%] Application started; mode=%MODE%
if errorlevel 1 goto log_error
>> "%LAUNCHER_LOG%" echo [%date% %time%] Application started.
"%PYTHON%" -m app.main >> "%STARTUP_OUT%" 2>> "%STARTUP_ERR%"
set "EXIT_CODE=%ERRORLEVEL%"
>> "%LAUNCHER_LOG%" echo [%date% %time%] Application exit code=%EXIT_CODE%
if "%EXIT_CODE%"=="130" goto manual_interrupt
if "%EXIT_CODE%"=="-1073741510" goto manual_interrupt
if "%EXIT_CODE%"=="3221225786" goto manual_interrupt
if "%EXIT_CODE%"=="0" goto clean_shutdown
if /i "%MODE%"=="once" (
    >> "%LAUNCHER_LOG%" echo [%date% %time%] Once mode complete; no restart.
    exit /b %EXIT_CODE%
)
if "%EXIT_CODE%"=="42" (
    >> "%LAUNCHER_LOG%" echo [%date% %time%] Restart requested by /restart or /update.
    goto restart
)
>> "%LAUNCHER_LOG%" echo [%date% %time%] Unexpected crash; restart delay=%RESTART_DELAY% seconds.
timeout /t %RESTART_DELAY% /nobreak >nul 2>&1
if errorlevel 1 (
    rem timeout cannot wait when stdin is redirected, including some hidden launches.
    powershell.exe -NoProfile -NonInteractive -Command "Start-Sleep -Seconds %RESTART_DELAY%"
    if errorlevel 1 (
        >> "%LAUNCHER_LOG%" echo [%date% %time%] ERROR: Restart delay failed; launcher exiting.
        exit /b 1
    )
)
goto restart

:clean_shutdown
>> "%LAUNCHER_LOG%" echo [%date% %time%] Clean shutdown; launcher exiting.
exit /b 0

:manual_interrupt
>> "%LAUNCHER_LOG%" echo [%date% %time%] Manual interrupt; launcher exiting without restart.
exit /b %EXIT_CODE%

:log_error
>> "%LAUNCHER_LOG%" echo [%date% %time%] ERROR: Cannot preserve or initialize startup logs; launcher exiting.
echo ERROR: Cannot preserve or initialize startup logs. Check directory permissions.
exit /b 1

:usage
echo Usage: "%~nx0" [once]
exit /b 2
