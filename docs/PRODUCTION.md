# Руководство по эксплуатации

[README](../README.md) · [English](PRODUCTION.en.md) · [Configuration](CONFIGURATION.md) · [Observability](observability.md)

## Подготовка

Нужны Python 3.11+ и [requirements.txt](../requirements.txt) либо Docker Compose.
Создайте `.env` из [.env.example](../.env.example), заполните `BOT_TOKEN`,
`API_USER_ID`, `API_KEY`. `ADMIN_USER_IDS` необязателен, но нужен для администрирования;
замените демонстрационное значение своими ID или оставьте пустым.
Правила доступа: [Configuration](CONFIGURATION.md#accessadmin).

Бот использует long polling. Запускайте один процесс на одну SQLite DB
и не используйте один Telegram token в нескольких polling deployments.
Контент 18+: встроенной проверки возраста нет, доступ контролирует оператор.
Credentials, базы, backups и logs должны оставаться в закрытом доступе.

## Windows

Из корня checkout:

```powershell
.\rule34.bat
```

[rule34.bat](../rule34.bat) переходит в каталог проекта, выбирает
`.venv\Scripts\python.exe`, если он есть, иначе `python` из PATH, и запускает
`python -m app.main`. Активация окружения для batch launcher не требуется.

| Завершение | Watchdog (default) | `once` |
| --- | --- | --- |
| `0` — штатная остановка | Launcher завершается | Launcher завершается |
| `42` — restart/update | Немедленный новый запуск | Выход без нового запуска |
| Неожиданный ненулевой код | Повтор через 10 секунд | Выход без повтора |
| Ручное прерывание: `130`, `-1073741510`, `3221225786` | Выход без перезапуска | Выход без перезапуска |

Один запуск для диагностики:

```powershell
.\rule34.bat once
```

Отсутствие Python или ошибка подготовки логов останавливают launcher.
[start_hidden.vbs](../start_hidden.vbs) выполняет `cmd /c rule34.bat` без окна;
краткая инструкция: [START_HIDDEN_README.txt](../START_HIDDEN_README.txt).
Для интерактивной остановки используйте Ctrl+C и дождитесь shutdown.
`/restart` запрашивает graceful shutdown, а не окончательную остановку watchdog.
У скрытого launcher нет отдельной команды stop: для планового обслуживания
используйте видимый процесс или process manager с управляемой остановкой.
Принудительное завершение может потерять queued trace и оставить неизвестный результат send.

## Docker

После создания `.env`:

```bash
docker compose up -d --build
docker compose logs -f bot
docker compose stop bot
docker compose start bot
docker compose restart bot
docker compose down
```

[Compose](../docker-compose.yml) монтирует `./data` в `/app/data`, `./logs` в
`/app/logs` и задаёт `DB_PATH=/app/data/bot_data.db` поверх `.env`.
[Dockerfile](../Dockerfile) использует Python 3.11-slim, пользователя `appuser`
и entrypoint `python -m app.main`. Обеспечьте appuser права записи на bind mounts.
Policy `unless-stopped` перезапускает контейнер после выхода, пока оператор его не остановил.
Это отличается от batch launcher, который прекращает работу при exit `0`.
`down` сохраняет host bind directories с базой и логами.
Для нового кода обновите checkout и повторите `up -d --build`.
Нужна сеть к Telegram, Rule34 API/CDN; переводы дополнительно требуют Google Translate.

## Single instance и SQLite

Default DB — `bot_data.db`; относительный `DB_PATH` разрешается от корня проекта.
Connections включают WAL, foreign keys и `busy_timeout=30000` ms.
Межпроцессный lock привязан к базе; ожидает её освобождения по
`INSTANCE_LOCK_WAIT_SECONDS` с шагом `INSTANCE_LOCK_RETRY_INTERVAL_SECONDS`.
Lock не делает deployment многопроцессным: не обходите его и не удаляйте lock-файл работающего процесса.

## Резервное копирование и восстановление

Согласованный backup работающей базы через SQLite backup API:

```bash
python scripts/backup_sqlite.py --db bot_data.db --output-dir backups
```

Для Docker-базы с host-машины с установленными зависимостями:

```bash
python scripts/backup_sqlite.py --db data/bot_data.db --output-dir backups
```

[backup_sqlite.py](../scripts/backup_sqlite.py) создаёт timestamped `.db`.
Резервируйте также приватную конфигурацию; logs сохраняйте отдельно по своей политике.
Проверьте читаемость backup и восстановление в отдельной копии.
При ручном копировании сначала остановите все процессы с базой.
Сохраните `.db` и существующие `.db-wal` / `.db-shm` как единый комплект;
не копируйте только main DB во время записи.
Для восстановления остановите все процессы, сохраните текущий комплект,
замените его выбранным backup и запустите один экземпляр.
Не оставляйте WAL/SHM от другой базы рядом с восстановленным main-файлом.

## Обновление

В личном чате администратор может использовать:

- `/version`: commit, branch, date и локальные изменения.
- `/update_check`: fetch настроенного remote и сравнение commits; Git refs меняются, рабочие файлы нет.
- `/update`: clean-tree проверка, fetch, backup SQLite, `git pull --ff-only`,
  установка requirements при изменении dependency files, compile/import checks и exit `42`.

Remote/branch берутся только из ENV; shell-аргументы из Telegram не принимаются.
Параллельное обновление блокируется. При ошибке после pull код на диске уже может
быть обновлён; автоматического rollback нет. Проверьте этап ошибки и backup до следующего запуска.
Без watchdog/restart policy exit `42` не создаёт новый процесс; `once` также не возобновляется.
Ожидание административного shutdown-уведомления ограничено, чтобы Telegram cooldown
не блокировал запрос остановки навсегда.
В стандартном Docker image Git не установлен, а `.git` исключён из build context:
Telegram updater там не заменяет rebuild/redeploy.
Для ручного обновления сделайте backup, остановите bot, обновите checkout и dependencies,
выполните [проверки](DEVELOPMENT.md#локальные-проверки) и снова запустите launcher.

## Логи и архивирование

| Файл в `logs/` | Содержание / хранение |
| --- | --- |
| `info.log` | Рабочие события и heartbeat; 5 MiB, 3 backups |
| `warnings.log` | Предупреждения; 5 MiB, 3 backups |
| `errors.log` | Ошибки и traceback; 5 MiB, 5 backups |
| `logic_trace.jsonl` | Опциональный Observability v2 JSONL |
| `logic_trace.jsonl.1`, `.2`, … | Trace rotation по ENV size/count; очистка старых rotated files при старте writer |
| `bat_launcher.log` | История launcher; встроенной ротации нет |
| `startup_output.log`, `startup_errors.log` | stdout/stderr текущего batch запуска |
| `startup_output.previous.log`, `startup_errors.previous.log` | Предыдущий запуск; перезаписываются при следующем |

Для архивации штатно остановите bot и launcher, дождитесь flush, скопируйте полный
комплект в отдельный timestamped каталог и проверьте состав, размеры и контрольные суммы.
При переносе создайте новый writable `logs/` перед запуском; не перемещайте открытые файлы writer.
Назначьте retention архивов и launcher logs отдельно от автоматической ротации.
Не добавляйте архивы в Git. Trace может содержать запросы:
[privacy и redaction](observability.md#trace-writer-security-and-lifecycle).

## Диагностика и recovery

Начните с `/diag` в личном admin-чате: локальный runtime, ограниченный read-only DB
snapshot, cooldown, queue, writer health. `/diag errors` добавляет последние 10
process-local incidents. `/health` включает DB quick check и запрос к Rule34.
Heartbeat появляется каждые пять минут; подробный `process.heartbeat` пишется
в trace, если он включён. Его отсутствие — повод проверить процесс/startup stage,
но не доказательство конкретной причины отказа.

| Симптом | Что проверить |
| --- | --- |
| Нет запуска / polling | Required ENV, Python/dependencies, текущие и previous startup logs, instance lock |
| Rule34 HTTP 403 | Breaker cooldown, safe classification; не форсировать циклические запросы |
| Telegram задерживается | Полный RetryAfter, global/per-chat queue; длительный cooldown возможен |
| Locked / slow DB | DB phases и другие процессы с базой; долгий await не доказывает lock wait |
| Нет trace | Disabled default, LOGIC_TRACE_ENABLED, права, writer errors/drops, уровень |
| Подписка не доставлена | Active/paused, interval, due, claim, filters, cache, dedup и cooldown |
| GIF / ZIP недоступен | Telegram limitations, preview/sample fallback, формат и лимиты экспорта |
| Нет перевода | TAG_TRANSLATION_ENABLED, Google Translate и cache; английский тег остаётся |

Rule34 403 открывает shared breaker: 60 → 120 → 300 → 600 → 900 секунд,
затем максимум 900; после cooldown допускается одна half-open probe.
Только валидный authenticated post response текущего поколения восстанавливает API.
Cache/autocomplete/stale success не закрывают breaker.
Первый 403 вызывает alert, повторный — не чаще 900 секунд, recovery alert — после API success.
События и safe HTTP evidence: [Observability](observability.md#rule34).

Telegram limiter соблюдает полный RetryAfter без верхнего усечения.
Claim renewal каждые 60 секунд сохраняет ownership активной подписки; утрата
ownership останавливает её обработку. Перед send проверяются claim и доступ.
Cross-subscription dedup пользователя связывает selection/send/ack с сериализацией
и DB history. Telegram send и SQLite acknowledgement не атомарны.

## Ограничения

- Один process на SQLite DB, long polling; горизонтальное масштабирование этим deployment не поддерживается.
- Поиск/файлы зависят от Rule34 API/CDN; cache fallback не подтверждает API recovery.
- GIF animation не входит в media group: обычная галерея использует static preview,
  animation mode отправляет GIF отдельно.
- Digest готов при пяти постах или шести часах ожидания и доступен вручную из меню.
  Confirmed элементы удаляются из очереди, failed остаются, ambiguous откладываются.
  Это не гарантированный шестичасовой срок доставки; timeout не доказывает недоставку.
  Exactly-once не гарантируется.
- ZIP включает статичные `.jpg`, `.jpeg`, `.png`, `.webp`; ограничен queue/workers,
  timeout, числом файлов/частей, фактическими download bytes и temporary disk budget.
  Part target не превышает 45 MiB. Отправленные до ошибки/отмены части остаются в Telegram.
- Встроенной проверки возраста нет; оператор отвечает за 18+ доступ.
- Registry/incidents сбрасываются при restart; trace disabled, overflow и abrupt kill
  ограничивают полноту наблюдений. Успешный send не подтверждает чтение пользователем.
