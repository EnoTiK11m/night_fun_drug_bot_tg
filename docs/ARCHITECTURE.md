# Архитектура проекта

`app/main.py` — точка входа (`python -m app.main`) для Windows launcher и Docker.
Корневой `bot.py` оставлен как совместимый entrypoint; он не содержит обработчиков или бизнес-логики.

[README](../README.md) · [Configuration](CONFIGURATION.md) · [Production](PRODUCTION.md) · [Development](DEVELOPMENT.md)

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
    rule34/client.py, rule34/rate_limiter.py, rule34/outage.py
    tag_translation.py
  storage/
    database.py          публичный DB facade, connection, модели и настройки
    migrations.py        schema/init и versioned migrations
    subscriptions.py     подписки, claims, history и digest queue
    favorites.py         избранное, коллекции, read-later и storage cleanup
    cache.py, users.py, translations.py, delivery_failures.py
  observability/
    logic_trace.py       correlation, bounded JSONL writer
    health.py            process-local registry и incidents
    errors.py, diagnostics.py, db_diagnostics.py, logging_filters.py
  infrastructure/instance_lock.py, user_gate.py, project_update.py
```

Telegram handlers вызывают services и существующий delivery layer. Search service
обращается к Rule34 integration и storage; обе integrations и delivery используют
самостоятельные лимитеры. Observability не зависит от Telegram handlers. ENV parsing
централизован в `app/config.py`; PROJECT_ROOT означает корень checkout.
Относительный DB_PATH разрешается от этого корня; locks связаны с базой,
logs и backups используют пути проекта. `.env` загружается через `load_dotenv`;
launcher задаёт рабочий каталог проекта.

Infrastructure обеспечивает instance lock, startup/shutdown, административное
Git-обновление и per-user operation gate. Gate сериализует пользовательские
операции; lifecycle останавливает фоновые задачи и освобождает lock после shutdown.

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
HTTP 403 открывает shared circuit breaker; после backoff допускается только одна
half-open probe. Подавленные запросы не тратят Rule34 quota. Только валидный
authenticated post response текущего поколения закрывает breaker: cache,
autocomplete и stale success не восстанавливают API health.
Backoff и HTTP diagnostics: [Observability](observability.md#rule34).

Подписка может иметь interval_seconds=30, polling default=5; занятые очереди могут
увеличить задержку. Deadline сохраняет progress и вызывает defer, а не empty backoff.

Активная подписка продлевает claim каждые 60 секунд. Утрата lease останавливает
worker; перед send повторно проверяются ownership, активность и право получателя.
Per-user сериализация selection/send/ack вместе с общей DB history поддерживает
cross-subscription dedup. Telegram send и SQLite commit не атомарны:
неопределённая доставка не превращается в гарантию exactly-once.
Digest хранит элементы в SQLite, claim выделяет batch; подтверждённые элементы
удаляются, failed остаются, ambiguous откладываются. ZIP queue и временные
user states process-local, с ограниченными ресурсами и cleanup.

Telegram limiter имеет отдельные общую/per-chat очереди и cooldown, учитывает
полный RetryAfter без верхнего усечения. Media layer использует URL, sample,
local download/upload и text-link fallback; успешная ссылка не означает
успешную доставку изображения.

Tracing выключен по умолчанию. `LOGIC_TRACE_ENABLED=true`, уровень normal — отдельный
`logs/logic_trace.jsonl`, bounded queue, rotation, redaction и hash user ID. Verbose
предназначен для временной диагностики. Reader:

```bash
python scripts/read_trace.py --last 100 --summary
python scripts/read_trace.py --trace TRACE_ID --summary
```

Observability v2 связывает trace/flow/request/attempt/connection, различает
intermediate failures и terminal outcome, хранит process-local runtime health.
Registry не управляет scheduling, retries или claims. SQLite instrumentation
измеряет connect, execute/fetch, commit/rollback, close и другие фазы без SQL/binds;
await time включает queue, engine/busy wait и async resume, а не только lock wait.
Heartbeat каждые пять минут показывает stages, external health, subscriptions,
DB и writer. `/diag` читает локальное состояние и ограниченный read-only snapshot;
`/health` дополнительно выполняет API request. Подробности: [Observability](observability.md).

## Проверка и ограничения

Syntax/import checks, isolated SQLite suite и CI: [Development](DEVELOPMENT.md).
Эксплуатационные ограничения, backup и recovery: [Production](PRODUCTION.md#ограничения).
