# Observability / Logging v2

[README](../README.md) · [Configuration](CONFIGURATION.md#observability) · [Production](PRODUCTION.md#логи-и-архивирование)

## Включение и чтение

Runtime health и `/diag` доступны независимо от JSONL writer. Дополнительный
trace выключен по умолчанию (`LOGIC_TRACE_ENABLED=false` в config и example).
Для включения задайте в своей конфигурации и запустите новый процесс:

```env
LOGIC_TRACE_ENABLED=true
LOGIC_TRACE_LEVEL=normal
```

Canonical путь — `logs/logic_trace.jsonl`; rotated files — `logic_trace.jsonl.1`, `.2`, ….
`minimal`, `normal`, `verbose` задают детализацию, не severity. Verbose используйте временно.

```bash
python scripts/read_trace.py --last 100 --summary
python scripts/read_trace.py --trace TRACE_ID --summary
python scripts/read_trace.py --file path/to/copied_trace.jsonl --last 100 --summary
```

`path/to/copied_trace.jsonl` — условный путь к вашей копии, не файл репозитория.
Без `--file` reader использует canonical trace; при его отсутствии поддерживает
legacy-файл `logs/app.observability.logic_trace.jsonl`. Reader также принимает
фильтры `--flow`, `--user`, `--event`; `--last` допускает 1–10000 записей.
Текст запросов может попадать в
trace на любом уровне, если выбранное событие его содержит; hash не заменяет этот текст.

## Интерпретация

HTTP errors, failed attempts, logical requests, fallback и terminal flows
описывают разные наблюдения; их нельзя складывать как число потерянных результатов.
V2 связывает wrapper error с причиной через request ID, агрегирует повторные
traceback и отличает успешный fallback от конечной потери результата.
DB phase instrumentation сохраняет отдельные измерения стадий операции.

## Architecture and correlation

Используется существующий `logic_trace` с одним bounded writer. `RuntimeHealth`
хранит только process-local наблюдения и никогда не участвует в admission,
scheduling, claims, retries или выборе media. Потоки обновляют registry под коротким
RLock, без await или SQLite внутри блокировки.

| ID | Meaning |
|---|---|
| trace_id | Корень операции, общий для вложенных flows |
| flow_id | Конкретный flow; parent_flow_id связывает вложенность |
| request_id | Логический внешний запрос, сохраняется между retries |
| attempt | Физический dispatch, 1, 2, 3; suppressed request имеет 0 attempts |
| connection_id | Отдельное SQLite connection, не равно корневому trace_id |

Telegram notification внутри Rule34 запроса получает отдельный request ID.
PTB recursion внутри уже ограниченного Telegram запроса сохраняет его ID.
Background DB operation получает один технический context; фиктивного user нет.
Root context сохраняется через asyncio tasks/claim renewal и существующий callback
diagnostic context. Старый parent_trace_id оставлен для reader compatibility.

## Event schema

JSONL: `ts` UTC ISO timestamp, `event`, `level`, `component`, `operation`,
`trace_id`, `flow_id`; применимые `request_id`, `attempt`, `connection_id`,
`duration_ms`, `user_hash`/legacy `user_id_hash`, `query`/`query_hash`, `post_id`.
Errors: `error_type`, `error_message`, `root_error_type`, `root_error_message`,
`error_category`, `http_status`, `root_http_status`, `retryable`, `incident_id`.
Policies: `retry_in_seconds`, raw/applied RetryAfter, remaining/backoff fields.
Finals: `outcome`, `user_impact`, `retry_count`, `fallback_used`.
Отсутствующие поля не заполняются null; nested snapshots могут иметь unknown/null.
`level` — severity; аргумент trace level minimal/normal/verbose — детализация.

Legacy `*.start`/`*.finish` сохранены; новые `*.started`/`*.finished` дают
canonical outcomes. Потребители должны выбирать один набор, чтобы не удваивать
flow counts. Legacy finish сохраняет прежние значения API/error/budget outcomes.

## Error chain and taxonomy

`error_details()` обходит cause/context максимум на 8 exception, защищается от
циклов и уважает suppress_context. HTTP status извлекается из status/status_code,
исходного HTTP сообщения или типизированного Rule34Unavailable. Диагностика
не назначает retry: `retryable` — классификация, фактическая политика в events.

Основные категории: http_401/403/429/5xx, invalid_response, dns/connect,
read_timeout/read_error/tls/proxy, telegram_retry_after/timeout_ambiguous/network/
bad_request/forbidden, db_locked/integrity/failure/slow, deadline/cancellation/
delivery_precondition/invalid_state. Media source events отдельно используют
telegram_url_fetch_failed, invalid_media, fallback_failed. Неизвестная причина
медленной SQL-фазы обозначается unknown, а не lock_wait.

## Outcomes and user impact

`*.finished` использует success, recovered, fallback_success, deferred,
suppressed, cancelled, failed, ignored, no_result. Intermediate attempt.failed
не означает failed flow. Успешный cache/media fallback сохраняет предыдущую
ошибку как previous_error_category и имеет fallback_success/delivered.

Delivery impact: delivered, delayed, no_delivery, unknown_delivery,
no_user_impact; DB failure после доставки может иметь no_delivery_impact.
Успешная отправка сообщения об ошибке поиска не считается доставкой результата.
Timeout на outgoing send сохраняет unknown_delivery: сервер мог принять send.

## Rule34

request.started → http.attempt → HTTP/validation evidence → retry или breaker
opened/extended/suppressed/probe/closed → request.finished. Search retry виден
на normal level с задержкой 1/2s. HTTP 403 не повторяется внутри этого search retry.
403 backoff: 60 → 120 → 300 → 600 → 900s, затем максимум 900s; после cooldown
одна shared half-open probe. Запросы во время open подавляются до расходования
limiter quota. Generation validation не даёт устаревшему in-flight success
закрыть новый breaker; cancelled/failed probe освобождается с новым cooldown.
Safe 403 diagnostics сохраняют bounded classification/hash и allowlisted headers,
без body snippet и credentials. Authenticated valid post response восстанавливает
API health; autocomplete, cache delivery и stale HTTP result этого не делают.
Валидный post response — JSON list, включая пустой список. Только 403 открывает
этот circuit; 429 использует limiter cooldown, другие ошибки не становятся 403 outage.
Body evidence ограничен sample до 8192 bytes и коротким ожиданием, записываются
только classification (empty/json_error/auth_like/cloudflare_challenge/html_forbidden/
unknown_text) и hash. Header allowlist допускает проверенные content type/length,
Retry-After, server и cf-ray, а также ограниченные cf-mitigated/cf-cache-status,
без произвольного текста, cookies или credentials.

## Telegram

Logical request объединяет RetryAfter и разрешённый safe timeout retry. Physical
attempt не сбрасывается после timeout. Queue wait измеряет совокупное ожидание
FIFO/limiter, не разделяет каждый внутренний источник задержки. Полный валидный
RetryAfter применяется без верхнего усечения. RetryAfter events сохраняют
raw/applied wait и remaining. Cooldown opened/extended фиксируются без
нового API вызова; recovered — один раз после успешного outgoing request, когда
deadline действительно прошёл. Network health восстанавливается по реальной
успешной отправке. BadRequest/Forbidden для конкретного payload не объявляются
исправленными успешной отправкой другого payload.

`last_error` — историческое наблюдение, не признак текущего degraded. Telegram
BadRequest сохраняется в bounded aggregation как `scope/state=operation_local`:
он виден в `/diag errors`, но не повышает `active_incidents` и не делает всю
подсистему degraded. Успех fallback не объявляет исходный payload исправленным.
Известные причины отображаются коротким allowlisted `reason`, без raw message/URL.
Network/timeout incidents остаются systemic до успешного запроса; RetryAfter и
положительный cooldown делают Telegram degraded даже при локальных успехах.
Cooldown recovery фиксируется после окончания ожидания и реального успешного send.
Это event-based policy, без новых TTL, probe или определения outage по возрасту success.

Domain-specific Telegram exception в cause/context chain имеет приоритет над
generic/transport classification: `TimedOut → ReadTimeout` остаётся
`telegram_timeout_ambiguous`, а `root_error_type=ReadTimeout` сохраняется отдельно.
Повторное наблюдение той же причины верхним flow не заменяет category на
`invalid_state`; финальный impact сохраняет `unknown_delivery`.

## Subscriptions and media

subscription.started → claim request/created/acquired → options/filter/dedup/
post selection → revalidation → media delivery → history/DB acknowledgement →
schedule update → claim release → subscription.finished. Claim renewal каждые
60 секунд имеет тот же context и normal event. API/budget/deadline deferral имеет delayed.
Cache fallback имеет normal cache.decision и не меняет API health.
Перед send проверяются доступ и claim; cross-subscription dedup использует
сериализацию пользователя и общую историю доставок. Утрата lease останавливает worker.

Media: source.attempt → source.failed → source.fallback/telegram.send.fallback →
delivery.success → media.delivery.finished. Download/upload, sample URL и text
link fallback явно обозначаются. Text link delivery означает доставку ссылки,
не подтверждение доставки изображения. Media events не содержат media URL.
Pause opened/closed отражают явные pause/resume actions; expiry не порождает
отдельного timer event.

## SQLite

Сохранены connect/init/begin/execute/fetch/commit/rollback/context_body/close,
query labels, timings и active/peak counters. No SQL или bind values в diagnostics.
Фазы >250ms видны на normal; legacy slow flag >1000ms сохранён. Whole operation
slow count дополняет фазовые измерения. Успешная быстрая операция с тем же label
закрывает соответствующий lock/slow incident. Это наблюдение успешного выполнения,
не доказательство исчезновения всех возможных SQLite contention.

## Runtime health, aggregation and alerts

Registry: last success/error/recovery, degraded, counters; process stage/version.
100 incident ring + максимум 100 aggregation fingerprints (systemic и local); 128 recent request/attempt
markers на incident. Identity включает component/operation/category/root/status/
safe root message. Повторное наблюдение той же request attempt увеличивает
observations, но не failures. Уникальная причина создаёт отдельный incident.
Старые incident markers могут быть вытеснены при очень длинном incident.
`active_incidents` считает открытые systemic incidents, не operation-local
aggregation. `/diag errors` показывает `state=operation_local` или `recovered`
вместо misleading active для исторических локальных ошибок. Разные root causes
сохраняют разные fingerprints, даже если короткий safe reason совпадает.

Первый occurrence имеет traceback, повторы — lightweight count; summary каждые
300s при последующей ошибке, recovery — финальные count/duration/last failure.
Обычные WARNING/ERROR также агрегируются bounded фильтром до 5min summaries.
INFO не копирует каждый trace event. Rule34 alert policy сохранена: первый 403,
затем не чаще 900s; recovery notification — после реального API success.
Пример: `Rule34 outage: HTTP 403 ... logical failures=... physical failures=...
suppressed=...`. Recovery: `Rule34 API восстановился после ... мин`.

## Heartbeat v2 and /diag

Heartbeat каждые пять минут: PID, cached HEAD/version, uptime, lock/stage; Telegram ages/category/
retry/cooldown/queue; Rule34 health/status/outage/backoff/attempts/suppressed/probes;
subscriptions active/due/active+expired claims/delivery/failure/deferral;
SQLite counters/last/slow; trace writer drops/queue/error; gate/stale/duplicates.
Один compact human line, подробный machine snapshot в trace. Git subprocess на
heartbeat или diag отсутствует. Version — HEAD плюс явно возможные local changes.

`/diag` только private admin. Авторизация до snapshot/SQLite. Runtime + один
read-only SELECT subscriptions через `mode=ro`; connection timeout .1s, SQL
progress deadline .25s, outer await budget 1s. Нет init/migration/write/checkpoint/
cleanup/claim mutation/Rule34 probe/Telegram self-test. Единственный outgoing
Telegram вызов — ответ на команду. `/health` сохраняет старые проверки, включая API
probe; поэтому для пассивного чтения состояния используйте `/diag`.
`/diag errors`: последние 10 incident из памяти; без чтения rotated trace.
Human timezone — системная local timezone, поддерживается injection для tests.

Нейтральный шаблон полей `/diag` (значения заменяются runtime):

```text
Diag <local timestamp>
Process pid=<pid> version=<version> uptime=<seconds> stage=<stage> lock=<bool>
Telegram success_age=<age> error_age=<age> category=<category> degraded=<bool> cooldown=<seconds> retry_after=<count> queue=<count> ambiguous=<count> retries=<count>
Rule34 success_age=<age> error_age=<age> breaker=<bool> status=<status> category=<category> cooldown=<seconds> outage=<seconds> backoff=<seconds> attempts=<count> failures=<count> suppressed=<count> probes=<count> recoveries=<count>
Subscriptions active=<count> due=<count> claims=<active>/<expired> last_delivery_age=<age> failed=<count> delivered=<count> deferred=<count> snapshot_error=<error>
SQLite connections=<active>/<peak> operations=<active>/<peak> last=<label> duration=<ms> slow=<count> last_slow=<label>:<ms> category=<category>
Trace ok=<bool> queue=<count> drops=<count> errors=<count> malformed=<count> last_error=<error>
App active_incidents=<count> gate=<count> waiters=<count> stale=<count> duplicate=<count>
```

## Trace writer, security and lifecycle

Очередь writer ограничена 2048 событиями. Default active file limit — 52428800 bytes
(50 MiB), backup count — 7, retention — 7 дней. Возрастная очистка numeric rotated
files выполняется при старте writer, а не непрерывным ежедневным timer.
Операционные и launcher logs имеют отдельные правила:
[Production](PRODUCTION.md#логи-и-архивирование).

Writer snapshot tracks queue,
drops, malformed events, I/O errors (including handler.handleError), last successful
write, flush result. I/O failure is represented by exception type, not sensitive
path/error payload. Shutdown is bounded; remaining queue can be lost at timeout.

Sanitization precedes persistence and incident storage: known bot/API/user secrets,
credential keys, processing token, callback_data/payload, Authorization/cookie,
URL query credentials, bearer/basic, HTTP/SOCKS proxy credentials/private keys.
Raw user/chat IDs excluded from trace; bounded hashes retained. Operational
formatter also sanitizes and retains legacy known-secret placeholders.
No raw HTTP payload/headers/params/env/locals added. Arbitrary unlabelled secrets
cannot be inferred: callers must not place unstructured private content in events.

Stages: process_started → configuration_loaded → instance_lock_waiting/acquired → startup checks /
cleanup → local_startup_complete → application_constructed → telegram_bootstrap →
database_initializing/initialized → background_tasks_started → polling_ready.
Ready appears only after successful Application.start with running Updater.
Shutdown draining → flush → complete → lock released; existing order and budgets
unchanged. Updater checked-command stages have request/attempt/error evidence
without logging command args/stdout/stderr. Windows launcher использует `python -m app.main`; lifecycle-коды описаны в [Production](PRODUCTION.md#windows).

## Проверки и ограничения

Regression coverage: fake HTTP/clock, retries, suppressed/real recovery/stale success,
cross-component request IDs, media fallback, ambiguous outcome, 100-repeat
aggregation/new roots/recovery/new incident, redaction, writer malformed/I/O,
read-only real temp SQLite and exclusive lock, auth/no probe/startup stages.
Также покрыты distinct connection IDs, recovery events, фильтрация быстрых DB
phases, rotation, overflow и concurrency.

Команды проверки и CI: [Development](DEVELOPMENT.md#локальные-проверки).
Счётчик tests не является эксплуатационной гарантией и не фиксируется здесь.

Registry reset при restart; состояние unknown до наблюдений. Сообщения доставлены
на уровне успешного API response, не прочитаны пользователем. Нет гарантии
наблюдения каждого incident при queue overflow, abrupt kill или trace disabled.
Read-only SELECT кратко берёт read lock, но не DB write lock. Heartbeat/diag не
проверяют HTTP доступность. Версия HEAD не идентифицирует dirty patch как commit.
IDs/flow events/incident bookkeeping имеют overhead; production latency benchmark
не выполнялся. Дополнительные flow events и correlation fields увеличивают healthy traffic;
относительный объём зависит от количества операций, выбранного уровня и ошибок.
Оценка размера строится по числу добавленных events и размеру записи, а не
по числу пользователей. Это не replay, benchmark или гарантия объёма;
агрегация повторных traceback уменьшает шум при outage.
