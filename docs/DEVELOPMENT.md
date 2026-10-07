# Разработка

[README](../README.md) · [Architecture](ARCHITECTURE.md)

## Окружение

Python 3.11+; стек — python-telegram-bot, aiohttp, aiosqlite, python-dotenv и SQLite.
Версии закреплены в [requirements.txt](../requirements.txt). Отдельного файла
dev dependencies нет: тесты используют стандартный `unittest` и зависимости приложения.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Для запуска приложения нужна собственная `.env` из [.env.example](../.env.example).
Настройки тестовых doubles и credentials задаются отдельными тестами;
для проверки синтаксиса запуск бота не требуется.

## Локальные проверки

Все команды выполняются из корня checkout:

```bash
python -m pip check
python -m compileall -q app bot.py scripts tests
python scripts/check_imports.py
python tests/run_isolated_suite.py
git diff --check
```

[check_imports.py](../scripts/check_imports.py) импортирует package modules без
polling и DB initialization. [run_isolated_suite.py](../tests/run_isolated_suite.py)
создаёт временный каталог, подставляет абсолютный `DB_PATH`, инициализирует
временную SQLite и запускает `unittest discover` в subprocess с этим окружением.
Рабочая база не используется. Для полного прогона предпочитайте этот wrapper
прямому `unittest discover` с production-настройками.
Число тестов меняется вместе с проектом; результат актуального запуска важнее
статичного счётчика в README.

## CI

[.github/workflows/ci.yml](../.github/workflows/ci.yml) запускается на push и
pull request: Ubuntu, Python 3.11, установка requirements, `pip check`,
`compileall`, import smoke и isolated suite. Изменение документации не меняет CI.

## Contribution flow

1. Создайте отдельную ветку.
2. Внесите ограниченное изменение; для изменения поведения добавьте подходящие тесты.
3. Выполните локальные проверки и просмотрите diff.
4. Откройте pull request с описанием проблемы, нового поведения и проверки.

Ошибки и предложения: [GitHub Issues](https://github.com/EnoTiK11m/night_fun_drug_bot_tg/issues).
Не коммитьте `.env`, рабочие базы, logs, backups и сгенерированные runtime-файлы:
[.gitignore](../.gitignore), [.dockerignore](../.dockerignore).

## Стиль и документация

Отдельный обязательный formatter / Python linter / Markdown linter в конфигурации
проекта и CI не задан. Следуйте стилю соседнего кода, используйте абсолютные
package imports и сохраняйте публичные facade-контракты.
`tests/test_markdown.py` проверяет Telegram escaping, а не Markdown-документы.

При изменении команды, ENV или lifecycle обновляйте тематический справочник.
Для документационных правок проверьте relative links и anchors, существование
исходных файлов, соответствие ENV и регистрации команд. Новые пакеты ради
разовой проверки ссылок не требуются.
