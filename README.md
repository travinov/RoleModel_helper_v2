# RoleModel Helper V2

Отдельный прототип помощника по ролям и доступам с упором на скорость ответа и прозрачную агентскую логику.

V2 не является копией V1. Частые сценарии проходят через детерминированный быстрый путь и прогретый immutable-каталог. GigaChat вызывается один раз только для действительно неоднозначной реплики. Состояние хранится в отдельной схеме существующего PostgreSQL-кластера, а HTTP-сервис по умолчанию слушает только `127.0.0.1:8001`.

## Что уже реализовано

- отдельный Git-репозиторий, порт, service name, install directory и state schema;
- typed `AgentEngine` с явными маршрутами `DETERMINISTIC` и `GIGACHAT_FALLBACK`;
- exact `SAFE` alias без GigaChat и bounded clarification для слабого alias;
- максимум один GigaChat planner call на неоднозначный ход;
- проверка версии каталога и всех catalog ID после ответа модели;
- immutable versioned catalog cache, single-flight refresh и last-good fallback;
- изолированная PostgreSQL-схема `rolemodel_v2_runtime`;
- изолированная PostgreSQL-схема `rolemodel_v2_catalog` с атомарными
  `STAGING -> ACTIVE -> RETIRED` релизами;
- read-only адаптер активного V1 snapshot: нормализация выполняется при
  публикации, а не в пользовательском запросе;
- typed tools для подразделений, должностей, профилей, АС, ролей и инструкций;
- строгий числовой фильтр: «отдел 2» не совпадает с №12 или №20;
- GigaChat получает не каталог, а максимум пять найденных кандидатов;
- отдельная миграция, JSONB state/turns, row-level locking, `request_id`
  idempotency и `state_revision` conflict;
- runtime-роль может быть ограничена DML только внутри схемы V2;
- API, возвращающий ответ, состояние и latency diagnostics одним POST;
- типизированные PostgreSQL-метрики route/outcome/p50/p95/p99;
- UI с быстрым прогрессом без обязательного повторного GET;
- persistent HTTP/OAuth session для GigaChat, mTLS/basic/access-token варианты, TLS verification включён по умолчанию.

## Локальный запуск

```bash
cd "/Volumes/SSD APFS/Python Project/RoleModel_helper_v2"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m app.runtime.migrate
.venv/bin/python -m app.catalog.migrate
.venv/bin/python -m app.catalog.publish --dry-run
.venv/bin/python -m app.catalog.publish
bash scripts/run_local.sh
```

Открыть: `http://127.0.0.1:8001/`

До миграции и публикации активного каталога приложение fail-closed и не
создаёт таблицы при старте. Значения DSN могут указывать на тот же
PostgreSQL-кластер и ту же database, что использует V1. Отдельный сервер
PostgreSQL не требуется. Обязательная граница — новые схемы
`rolemodel_v2_runtime` и `rolemodel_v2_catalog`, которые не совпадают со
схемой V1.

`RMV2_CATALOG_IMPORT_DSN` принадлежит отдельной job-роли: она читает
ограниченный набор таблиц активного V1 snapshot и пишет только каталог V2.
Runtime-роль не получает доступ к V1. Точный порядок и SQL-гранты:
[`docs/deployment-postgresql.md`](docs/deployment-postgresql.md).
Подробная схема агента, инструментов и ограниченных циклов:
[`docs/architecture.md`](docs/architecture.md).

Без GigaChat credentials приложение работает в deterministic-only mode. Для
проверки организационного быстрого пути:

```text
Какие роли у руководителя отдела 2 в Самаре?
```

## GigaChat

Поддерживаются:

- `RMV2_GIGACHAT_ACCESS_TOKEN`;
- `RMV2_GIGACHAT_AUTH_KEY`;
- `RMV2_GIGACHAT_CLIENT_ID` + `RMV2_GIGACHAT_CLIENT_SECRET`;
- mTLS: `RMV2_GIGACHAT_CERT_FILE` + `RMV2_GIGACHAT_KEY_FILE`.

Собственный CA задаётся через `RMV2_GIGACHAT_CA_BUNDLE`. `RMV2_TLS_VERIFY=false` предназначен только для явно согласованной тестовой среды.

## Изолированный production installer

Production installer запускается только из итогового каталога
`~/RoleModelHelperV2`, оставляет V1 нетронутой и жёстко отклоняет порт `8000`,
V1 service name, каталог и схемы. Он не загружает shell-код из env-файлов.
Из V1 копируются только клиентский сертификат, private key и, при наличии,
CA bundle:

1. сначала читаются явные пути `RM_GIGACHAT_CERT_FILE`,
   `RM_GIGACHAT_KEY_FILE`, `RM_GIGACHAT_CA_BUNDLE` из
   `~/RoleModelHelper2/.env.server` или `.env`;
2. если явных путей нет, используются
   `~/RoleModelHelper2/certs/gigachat/egress_sberca.crt`,
   `egress_sberca.key` и необязательный `ca.pem`;
3. файлы записываются в `~/RoleModelHelperV2/certs/gigachat` с mode `0600`,
   а их абсолютные V2-пути — в новый V2 `.env`.

Другой V1 env-файл можно указать перед запуском:

```bash
export RMV2_V1_ENV_FILE=/absolute/path/to/v1.env
bash scripts/install_rolemodel_v2_server.sh
```

Файл V1 `.env`, credentials и исходные сертификаты в release не попадают.

## Offline release для SberLinux 9

На машине с доступом к package index:

```bash
bash scripts/build_offline_release.sh
```

Builder скачивает только binary wheels для
`manylinux2014_x86_64`/CPython 3.9 по exact pins из
`requirements-release.txt`, держит wheelhouse во временном каталоге и
вкладывает его только в sanitized ZIP. Git metadata, реальные `.env`,
сертификаты, ключи, caches, logs, базы и предыдущий `output/` исключаются.
Повторная сборка неизменного дерева даёт идентичный ZIP и `.sha256`.

### Установка с Mac по SSH

Это основной production-сценарий, повторяющий схему установки V1. На Mac
достаточно распаковать именно offline ZIP (в нём должен быть `wheelhouse`),
переместить каталог V2 рядом с локальным каталогом V1 и запустить один скрипт:

```bash
test ! -e "/Users/travinov-sv/SBRF/Агентные решение/PythonProject/RoleModelHelperV2"
mv "/Users/travinov-sv/Downloads/RoleModelHelperV2" \
  "/Users/travinov-sv/SBRF/Агентные решение/PythonProject/RoleModelHelperV2"
cd "/Users/travinov-sv/SBRF/Агентные решение/PythonProject/RoleModelHelperV2"
bash scripts/deploy_rolemodel_v2_remote.sh
```

Локальный V1 checkout скрипту не нужен. Он подключается по SSH к
`CI09479675-lnx-travinov@tsles-assai0001.esrt.sber.ru`, проверяет удалённую V1,
передаёт offline-пакет в staging, устанавливает V2 в
`~/RoleModelHelperV2`, копирует сертификаты из удалённой
`~/RoleModelHelper2`, настраивает только `rolemodel-helper-v2.service` и запускает
интерактивную activation. На шаге dry-run нужно проверить counts и ввести
`PUBLISH`.

Как и installer V1, скрипт использует существующую учётную запись PostgreSQL
`CI09479675-pg-travinov` и скрыто запрашивает один её пароль. По умолчанию он
подключается к существующей БД V1 `10.135.162.149:5433/bdtest`; новый сервер и
новые роли PostgreSQL не нужны. Источником для импорта служит существующая схема
V1 `rolemodel_helper`; V2 создаёт только отдельные схемы
`rolemodel_v2_runtime` и `rolemodel_v2_catalog`.

При повторном запуске существующие V2 `.env`, `.env.runtime`, `certs` и `logs`
сохраняются. Чтобы осознанно заменить `.env`, запустите:

```bash
RMV2_REPLACE_ENV=1 bash scripts/deploy_rolemodel_v2_remote.sh
```

SSH target и параметры существующей БД при необходимости задаются локальными
переменными `RMV2_SSH_TARGET`, `RMV2_DB_HOST`, `RMV2_DB_PORT`, `RMV2_DB_NAME`
и `RMV2_DB_USER`. Пароль в ZIP и командную строку не попадает.

На целевом сервере проверка и установка выполняются без package index:

```bash
sha256sum -c RoleModel_helper_v2-sberlinux9-x86_64.zip.sha256
unzip RoleModel_helper_v2-sberlinux9-x86_64.zip -d "$HOME"
cd "$HOME/RoleModelHelperV2"
bash scripts/install_rolemodel_v2_server.sh
```

При наличии `wheelhouse/*.whl` installer всегда использует
`pip --no-index --find-links`. В source checkout без wheelhouse сохраняется
обычная online-установка для разработки.

## Что делать после installer

Installer не запускает сервис и не публикует данные автоматически. Сначала
откройте созданный файл:

```bash
nano "$HOME/RoleModelHelperV2/.env"
```

Обязательно проверьте:

- `RMV2_APP_HOST=0.0.0.0`, если V2 должна открываться с других компьютеров;
- `RMV2_APP_PORT=8001`;
- `RMV2_DATABASE_DSN` и `RMV2_CATALOG_DSN` используют runtime-роль V2;
- `RMV2_MIGRATION_DSN` использует владельца миграций V2;
- `RMV2_CATALOG_IMPORT_DSN` использует import-роль, которая читает только
  таблицы активного snapshot V1 и пишет каталог V2;
- схемы равны `rolemodel_v2_runtime` и `rolemodel_v2_catalog`;
- `RMV2_TLS_VERIFY=true`;
- пути `RMV2_GIGACHAT_CERT_FILE`, `RMV2_GIGACHAT_KEY_FILE` и optional CA
  уже заполнены installer и указывают на `~/RoleModelHelperV2/certs/gigachat`.

DSN могут вести в тот же PostgreSQL host/port/database, что у V1. Новый сервер
PostgreSQL не требуется, но роли и схемы V2 должны быть отдельными. Пароли не
добавляйте в Git; храните их только в `.env` с mode `0600` или в серверном
credential store.

После сохранения `.env` запустите от обычного пользователя:

```bash
cd "$HOME/RoleModelHelperV2"
bash scripts/activate_rolemodel_v2_server.sh
```

Activation выполняет последовательно:

1. проверку health V1 на `8000`;
2. миграцию только `rolemodel_v2_runtime`;
3. миграцию только `rolemodel_v2_catalog`;
4. `app.catalog.publish --dry-run` с выводом version, source SHA-256 и counts;
5. ожидание точного подтверждения `PUBLISH`;
6. атомарную публикацию активного снимка V1 в каталог V2;
7. restart только `rolemodel-helper-v2.service` и dual-health V1/V2.

Если подтверждение не введено, публикации и запуска не будет. Если V2 health
не поднимется или после запуска пропадёт V1 health, скрипт остановит только V2.
Excel повторно загружать не нужно: используется активный PostgreSQL snapshot V1.

Текущий V1 snapshot adapter не содержит статические корпоративные инструкции.
Если dry-run показывает `instructions: 0`, роли и доступы будут опубликованы,
но инструкции «как получить доступ» нужно импортировать отдельным адаптером.

## Проверки

```bash
/usr/bin/python3 -m unittest discover -s tests -p "test_*.py"
python3 -m unittest tests.test_postgres_runtime
python3 -m unittest tests.test_postgres_catalog
python3 -m unittest tests.test_v1_snapshot_adapter
python3 -m unittest tests.test_end_to_end_replay
/usr/bin/python3 scripts/benchmark_fast_path.py --turns 200
python3 -m app.runtime.telemetry --hours 24
```

PostgreSQL-тесты поднимают временный локальный кластер и проверяют миграции,
роли, V1 sentinel, активные релизы каталога, импорт V1 snapshot,
идемпотентность, конкурентные revision conflicts и полный HTTP replay.
Benchmark измеряет только локальный warm fast path и не заменяет сравнение на
корпоративном сервере.

## Архитектурная граница

```text
HTTP API
  -> read V2 state
  -> pin active immutable V2 catalog
  -> deterministic state machine
     -> typed catalog tools -> factual response / bounded clarification
     -> bounded candidate retrieval
        -> at most one structured GigaChat planning call
        -> validate action + IDs + catalog version
        -> typed catalog tools -> factual response
  -> atomic V2 state + turn telemetry commit
```

GigaChat не выполняет SQL, не получает полный каталог и не является источником
фактов. Рекурсивной автономной петли нет: для этого read-only сценария она
увеличила бы задержку и риск. Петля ограничена одним шагом планирования и одним
детерминированным выполнением инструментов.

## Пока не подтверждено

- production p50/p95/p99 и реальные query plans на корпоративном каталоге;
- владельцы/права PostgreSQL и same-host schema smoke на корпоративном сервере;
- реальная GigaChat certificate-auth цепочка;
- dual-service smoke на корпоративном сервере.
- точные corporate counts/SHA-256 после `app.catalog.publish --dry-run`;
- наличие корпоративных статических инструкций в отдельном V2-адаптере:
  текущий V1 snapshot adapter переносит ролевую модель, но не выдумывает
  отсутствующие в ETL-таблицах инструкции.

Спецификация и критерии приёмки: `docs/specs/2026-07-30-fast-hybrid-agent.md`.
