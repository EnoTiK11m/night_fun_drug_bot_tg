# Observability / Logging v2

## Audit findings

Аудит выполнен поверх локального working tree с новым Rule34 breaker. Baseline:
592 tests passed. Production не перезапускался и продолжает выполнять старый код.
Проверены observability, DB phases, Rule34, Telegram limiter, media, subscriptions,
search, callbacks, admin, instance lifecycle и updater, а также текущие и rotated logs.

В `logic_trace.jsonl.1` за 06–07 октября обнаружены 6394 события HTTP error и
6394 request.failed, 4209 общих API errors, 93 Telegram failures и 94 fallback events.
В текущем rotated окне: 187 успешных Telegram send, 11 failures и 10 fallback.
Это разные наблюдения, не количество потерянных пользовательских результатов.
Старый trace не связывал wrapper error с причиной отдельным request ID, повторял
traceback и не отличал успешный fallback от конечной потери результата. DB phase
instrumentation уже была полезной и сохранена.

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
opened/extended/suppressed/probe/closed → request.finished. Search retry теперь
виден на normal level с прежней задержкой 1/2s. Без изменений request policy,
403 backoff 60/120/300/600/900s, quota, одиночный probe и generation validation.
Safe 403 diagnostics сохраняют bounded classification/hash и allowlisted headers,
без body snippet и credentials. Authenticated valid post response восстанавливает
API health; autocomplete, cache delivery и stale HTTP result этого не делают.

## Telegram

Logical request объединяет RetryAfter и разрешённый safe timeout retry. Physical
attempt не сбрасывается после timeout. Queue wait измеряет совокупное ожидание
FIFO/limiter, не разделяет каждый внутренний источник задержки. RetryAfter events
сохраняют raw/applied wait и remaining. Cooldown opened/extended фиксируются без
нового API вызова; recovered — один раз после успешного outgoing request, когда
deadline действительно прошёл. Network health восстанавливается по реальной
успешной отправке. BadRequest/Forbidden для конкретного payload не объявляются
исправленными успешной отправкой другого payload.

## Subscriptions and media

subscription.started → claim request/created/acquired → options/filter/dedup/
post selection → revalidation → media delivery → history/DB acknowledgement →
schedule update → claim release → subscription.finished. Claim renewal имеет
тот же context и normal event. API/budget/deadline deferral имеет delayed.
Cache fallback имеет normal cache.decision и не меняет API health.
Existing schedule/claim/dedup/dispatch/acknowledgement semantics сохранены.

Media: source.attempt → source.failed → source.fallback/telegram.send.fallback →
delivery.success → media.delivery.finished. Download/upload, sample URL и text
link fallback явно обозначаются. Text link delivery означает доставку ссылки,
не подтверждение доставки изображения. Новые events не содержат media URL.
Pause opened/closed отражают явные pause/resume actions; expiry не порождает
отдельного timer event. Расписание и таймеры не менялись.

## SQLite

Сохранены connect/init/begin/execute/fetch/commit/rollback/context_body/close,
query labels, timings и active/peak counters. No SQL или bind values в diagnostics.
Фазы >250ms видны на normal; legacy slow flag >1000ms сохранён. Whole operation
slow count дополняет фазовые измерения. Успешная быстрая операция с тем же label
закрывает соответствующий lock/slow incident. Это наблюдение успешного выполнения,
не доказательство исчезновения всех возможных SQLite contention.

## Runtime health, aggregation and alerts

Registry: last success/error/recovery, degraded, counters; process stage/version.
100 incident ring + максимум 100 active fingerprints; 128 recent request/attempt
markers на incident. Identity включает component/operation/category/root/status/
safe root message. Повторное наблюдение той же request attempt увеличивает
observations, но не failures. Уникальная причина создаёт отдельный incident.
Старые incident markers могут быть вытеснены при очень длинном incident.

Первый occurrence имеет traceback, повторы — lightweight count; summary каждые
300s при последующей ошибке, recovery — финальные count/duration/last failure.
Обычные WARNING/ERROR также агрегируются bounded фильтром до 5min summaries.
INFO не копирует каждый trace event. Rule34 alert policy сохранена: первый 403,
затем не чаще 900s; recovery notification — после реального API success.
Пример: `Rule34 outage: HTTP 403 ... logical failures=... physical failures=...
suppressed=...`. Recovery: `Rule34 API восстановился после ... мин`.

## Heartbeat v2 and /diag

Heartbeat: PID, cached HEAD/version, uptime, lock/stage; Telegram ages/category/
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

Пример synthetic state (не live production `/diag`):

```text
Diag 2026-10-07 05:00:00 UTC+03:00
Process pid=4040 version=101a2dee uptime=39600s stage=polling_ready lock=True
Telegram success_age=3s error_age=1080s category=telegram_timeout_ambiguous degraded=False cooldown=0s retry_after=2 queue=0 ambiguous=1 retries=2
Rule34 success_age=2880s error_age=30s breaker=True status=403 category=http_403_unknown cooldown=540s outage=2820s backoff=600s attempts=19 failures=19 suppressed=292 probes=18 recoveries=0
Subscriptions active=11 due=0 claims=0/0 last_delivery_age=16s failed=0 delivered=120 deferred=10 snapshot_error=none
SQLite connections=0/5 operations=0/3 last=subscription.schedule.update duration=5ms slow=2 last_slow=cache.replace:8830ms category=none
Trace ok=True queue=0 drops=0 errors=0 malformed=0 last_error=none
App active_incidents=1 gate=1 waiters=0 stale=0 duplicate=0
```

## Trace writer, security and lifecycle

Existing queue/rotation/retention/flush retained. Writer snapshot tracks queue,
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

Stages: configuration_loaded → instance_lock_waiting/acquired → startup checks /
cleanup → local_startup_complete → application_constructed → telegram_bootstrap →
database_initializing/initialized → background_tasks_started → polling_ready.
Ready appears only after successful Application.start with running Updater.
Shutdown draining → flush → complete → lock released; existing order and budgets
unchanged. Updater checked-command stages have request/attempt/error evidence
without logging command args/stdout/stderr. Launcher files не изменены.

## Tests and limits

Новые tests: fake HTTP/clock, retries, suppressed/real recovery/stale success,
cross-component request IDs, media fallback, ambiguous outcome, 100-repeat
aggregation/new roots/recovery/new incident, redaction, writer malformed/I/O,
read-only real temp SQLite and exclusive lock, auth/no probe/startup stages.
Legacy assertions обновлены только для новой distinct connection ID и поиска
event по имени вместо позиции, а также допуска нового recovery event при проверке
фильтрации быстрых DB phases. Rotation/overflow/concurrency tests сохранены.

Проверено 2026-10-07: baseline 592; добавлено 38; полный isolated suite —
630 tests, 0 failures/errors (105.726s). Wrapped Telegram timeout и admin update
failure включены в regression coverage.
`compileall -q app bot.py scripts tests`, import smoke (42 modules, без polling),
`pip check`, `git diff --check` — OK. Последний даёт только обычные LF/CRLF warnings.

Registry reset при restart; состояние unknown до наблюдений. Сообщения доставлены
на уровне успешного API response, не прочитаны пользователем. Нет гарантии
наблюдения каждого incident при queue overflow, abrupt kill или trace disabled.
Read-only SELECT кратко берёт read lock, но не DB write lock. Heartbeat/diag не
проверяют HTTP доступность. Версия HEAD не идентифицирует dirty patch как commit.
Дополнительные IDs/flow events/incident bookkeeping имеют overhead; production
latency benchmark не выполнялся. По read-only выборке текущего trace (20425 events,
6.58MB) грубая оценка дополнительного healthy traffic: 1.21x records / 1.72x bytes
(новые event pairs, correlation fields, 500 bytes на добавленный event). Это оценка,
не replay или гарантия объёма; повторные traceback при outage сокращаются.
Production process остаётся на старом коде до
отдельного разрешённого пользователем запуска.
