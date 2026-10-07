Скрытый запуск бота

Используйте `start_hidden.vbs`, чтобы запустить бота без видимого окна
командной строки.

Цепочка запуска:

`start_hidden.vbs` → `rule34.bat` → `python -m app.main`

Не исключайте `rule34.bat` из этой цепочки: он обрабатывает команду `/restart`,
отслеживает завершение приложения с кодом 42 и после этого запускает бот заново.
Если есть `.venv\Scripts\python.exe`, launcher использует его; иначе — Python из PATH.
При exit 0 или ручном прерывании launcher завершается; после неожиданной ошибки
повторяет запуск через 10 секунд. Режим `rule34.bat once` не перезапускает приложение.

Текущий и предыдущий stdout/stderr находятся в logs/startup_output.log,
logs/startup_errors.log и соответствующих *.previous.log. История launcher —
logs/bat_launcher.log. Подробная инструкция: docs/PRODUCTION.md.
