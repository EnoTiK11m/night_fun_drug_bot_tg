# Архитектура проекта

`app/main.py` — точка входа. Корневой `bot.py` оставлен как совместимый launcher для
существующих Windows-скриптов. Он не содержит обработчиков или бизнес-логики.

```text
app/
  main.py, config.py
  telegram/
    application.py       сборка приложения, lifecycle, команды и UI-сценарии
    handlers/callbacks.py callback-handler
    delivery.py          Telegram quota/cooldown
    media.py             доставка, скачивание и fallback
    keyboards.py, formatting.py, state.py
  services/
    search.py            progressive search, cursor, fresh/archive selection
    subscriptions.py     claim/pass/defer/backoff и scheduler
    media_preferences.py фильтры и качество медиа, runtime metrics
    zip_export.py        очередь и сборка ZIP
  integrations/
    rule34/client.py, rule34/rate_limiter.py
    tag_translation.py
  storage/
    database.py          публичный DB facade, connection, модели и настройки
    migrations.py        schema/init и versioned migrations
    subscriptions.py     подписки, claims, history и digest queue
    favorites.py         избранное, коллекции, read-later и storage cleanup
    cache.py, users.py, translations.py, delivery_failures.py
  observability/logic_trace.py
  infrastructure/instance_lock.py, user_gate.py, project_update.py
```

Telegram handlers вызывают services и существующий delivery layer. Search service
обращается к Rule34 integration и storage; обе integrations и delivery используют
самостоятельные лимитеры. Observability не зависит от Telegram handlers. ENV parsing
централизован в `app/config.py`; PROJECT_ROOT означает корень checkout, поэтому пути
к `.env`, SQLite, locks, logs и backup не зависят от перемещения Python-модулей.

Большой callback-handler перенесён целиком, без изменения порядка ветвей. Остальные
команды и UI helpers пока остаются в application.py: дальнейшее разделение допустимо
по самостоятельным сценариям, с сохранением поведения и тестов.

## Совместимые фасады

Публичные DB-функции сохраняют signatures/defaults. Их короткие wrappers делегируют
доменным модулям, передавая текущий runtime namespace как зависимость. Так connection,
конфигурация retention, вспомогательные DB-функции и их тестовые подмены остаются
едиными. Доменный модуль не импортирует facade обратно; циклические imports не нужны.

Аналогично application.py сохраняет точки входа `button_handler`,
`process_one_subscription`, `get_subscription_cached_image`, `process_subscriptions`.
Trace и callback lifecycle decorators остаются у публичных точек входа. Реализации
получают существующие сервисы/отправку/состояние через injected runtime. Это переходная
граница для дальнейшего выделения более узких dependency objects, без изменения
пользовательских сценариев и monkeypatch-контрактов существующих тестов.

Facade imports имеют отдельные repository aliases, чтобы имена аргументов не
перекрывали модули. Используются абсолютные package imports, без `import *` и без
дублирующих root implementation modules.

## Runtime и диагностика

`.env`, SQLite/WAL/SHM, `logs/`, `backups/`, `data/`, generated `audit/` и bytecode
локальны и игнорируются Git и Docker build context. Приложение создаёт runtime
каталоги по необходимости; `.gitkeep` не требуется. Runtime-пути сохранены для
совместимости существующих локальных установок.

Rule34 default — 55 физических attempts за rolling 60 секунд. Interactive имеет
приоритет с background grant после пяти contended interactive grants. Все retries
считаются; HTTP 429 включает общий cooldown. Telegram quota остаётся отдельной.
Подписка может иметь interval_seconds=30, polling default=5; занятые очереди могут
увеличить задержку. Deadline сохраняет progress и вызывает defer, а не empty backoff.

Tracing выключен по умолчанию. `LOGIC_TRACE_ENABLED=true`, уровень normal — отдельный
`logs/logic_trace.jsonl`, bounded queue, rotation, redaction и hash user ID. Verbose
предназначен для временной диагностики. Reader:

```bash
python scripts/read_trace.py --last 100 --summary
python scripts/read_trace.py --trace TRACE_ID --summary
```

## Проверка

```bash
python -m compileall -q app bot.py scripts tests
python scripts/check_imports.py
python tests/run_isolated_suite.py
python -m pip check
git diff --check
```

Suite устанавливает DB_PATH на временную SQLite; рабочая БД в тестах не используется.
Import smoke загружает все package modules без polling и без DB initialization.
