@echo off
mkdir logs 2>nul
echo [%date% %time%] Скрипт запущен >> logs\bat_launcher.log
pythonw bot.py >> logs\script_output.log 2>> logs\script_errors.log
echo [%date% %time%] Скрипт завершен >> logs\bat_launcher.log