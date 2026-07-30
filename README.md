# RoleModel Helper V2

Отдельный прототип помощника по ролям и доступам с упором на скорость ответа и прозрачную агентскую логику.

V2 не является копией V1. Частые сценарии проходят через детерминированный быстрый путь и прогретый immutable-каталог. GigaChat вызывается один раз только для действительно неоднозначной реплики. Состояние хранится в отдельном SQLite-файле, а HTTP-сервис по умолчанию слушает только `127.0.0.1:8001`.

## Что уже реализовано

- отдельный Git-репозиторий, порт, service name, install directory и state database;
- typed `AgentEngine` с явными маршрутами `DETERMINISTIC` и `GIGACHAT_FALLBACK`;
- exact `SAFE` alias без GigaChat и bounded clarification для слабого alias;
- максимум один GigaChat planner call на неоднозначный ход;
- проверка версии каталога и всех catalog ID после ответа модели;
- immutable versioned catalog cache, single-flight refresh и last-good fallback;
- атомарные SQLite turns, `request_id` idempotency и `state_revision` conflict;
- API, возвращающий ответ, состояние и latency diagnostics одним POST;
- UI с быстрым прогрессом без обязательного повторного GET;
- persistent HTTP/OAuth session для GigaChat, mTLS/basic/access-token варианты, TLS verification включён по умолчанию.

## Локальный запуск

```bash
cd "/Volumes/SSD APFS/Python Project/RoleModel_helper_v2"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
bash scripts/run_local.sh
```

Открыть: `http://127.0.0.1:8001/`

Без GigaChat credentials приложение работает в deterministic-only demo mode. Для проверки быстрого пути:

```text
Покажи роли в Демо АС Доступ
```

## GigaChat

Поддерживаются:

- `RMV2_GIGACHAT_ACCESS_TOKEN`;
- `RMV2_GIGACHAT_AUTH_KEY`;
- `RMV2_GIGACHAT_CLIENT_ID` + `RMV2_GIGACHAT_CLIENT_SECRET`;
- mTLS: `RMV2_GIGACHAT_CERT_FILE` + `RMV2_GIGACHAT_KEY_FILE`.

Собственный CA задаётся через `RMV2_GIGACHAT_CA_BUNDLE`. `RMV2_TLS_VERIFY=false` предназначен только для явно согласованной тестовой среды.

## Проверки

```bash
/usr/bin/python3 -m unittest discover -s tests -p "test_*.py"
/usr/bin/python3 scripts/benchmark_fast_path.py --turns 200
```

Benchmark измеряет только локальный warm fast path и не заменяет сравнение на корпоративном сервере.

## Архитектурная граница

```text
HTTP API
  -> isolated SQLite state transaction
  -> deterministic router
     -> pinned in-memory catalog -> factual response
     -> one structured GigaChat planner call -> catalog revalidation -> factual response
```

Каталог `data/demo_catalog.json` синтетический. Для реальной эксплуатации нужен отдельный V2 snapshot/export из корпоративной ролевой модели; V2 не должна писать в схему или state V1.

## Пока не подтверждено

- production p50/p95/p99: локальная PostgreSQL и корпоративный хост в этой итерации не были доступны;
- реальная GigaChat certificate-auth цепочка;
- импорт полной ролевой модели и profile-aware поиск по должности, городу и подразделению;
- dual-service smoke на корпоративном сервере.

Спецификация и критерии приёмки: `docs/specs/2026-07-30-fast-hybrid-agent.md`.
