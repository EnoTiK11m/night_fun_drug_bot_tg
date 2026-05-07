@echo off
mkdir logs 2>nul

:restart
echo [%date% %time%] Launcher started >> logs\bat_launcher.log
python bot.py >> logs\script_output.log 2>> logs\script_errors.log
set EXIT_CODE=%ERRORLEVEL%
echo [%date% %time%] Launcher stopped with code %EXIT_CODE% >> logs\bat_launcher.log

if "%EXIT_CODE%"=="0" (
    echo [%date% %time%] Clean shutdown, launcher exiting >> logs\bat_launcher.log
    exit /b 0
)

if "%EXIT_CODE%"=="42" (
    echo [%date% %time%] Restart requested by /restart >> logs\bat_launcher.log
    goto restart
)

echo [%date% %time%] Unexpected crash, restarting in 10 seconds >> logs\bat_launcher.log
timeout /t 10 /nobreak >nul
goto restart
