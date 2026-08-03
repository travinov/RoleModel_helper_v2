# V2 на текущем PostgreSQL: production runbook

V2 переиспользует существующий PostgreSQL-кластер и существующую database.
Новый PostgreSQL-сервер не нужен. V1 остаётся владельцем своей схемы; V2
получает две новые схемы:

- `rolemodel_v2_runtime` — сессии, ходы и latency telemetry;
- `rolemodel_v2_catalog` — нормализованные immutable-релизы каталога.

## Учётная запись

Установка повторяет проверенную схему V1 и использует существующую учётную
запись `CI09479675-pg-travinov`. Один DSN применяется для:

- создания и миграции отдельных схем V2;
- runtime-запросов V2;
- чтения активного snapshot из `rolemodel_helper` и записи каталога V2.

Новые роли и DBA-заявка не требуются. Пароль вводится скрыто в Mac deploy-script,
передаётся только через SSH stdin и сохраняется в серверном `.env` с правами
`0600`. Отдельные схемы и guards приложения не позволяют migration-командам V2
выбрать схему V1, однако PostgreSQL-изоляции на уровне разных ролей в этой
упрощённой модели нет.

## Последовательность установки

1. Проверить, что V1 работает, порт `8001` свободен, а каталоги и service names
   не совпадают.
2. Заполнить `.env`; DSN могут указывать на тот же host, port и database.
3. Создать только схемы V2:

```bash
.venv/bin/python -m app.runtime.migrate
.venv/bin/python -m app.catalog.migrate
```

4. Прочитать активный V1 snapshot в read-only repeatable-read транзакции и
   проверить source SHA-256/counts без записи:

```bash
.venv/bin/python -m app.catalog.publish --dry-run
```

5. После сверки опубликовать релиз атомарно:

```bash
.venv/bin/python -m app.catalog.publish
```

6. Запустить только V2 service и проверить:

```bash
curl -fsS http://127.0.0.1:8001/api/v2/health
.venv/bin/python -m app.runtime.telemetry --hours 1
```

7. Повторно проверить health V1. При ошибке остановить только
   `rolemodel-helper-v2.service`: данные и service V1 не меняются.

## Критерии серверной приёмки

- одновременно зелёные V1 и V2 health;
- active V2 catalog version соответствует одобренному source SHA-256;
- runtime-роль не читает и не пишет V1;
- import role не пишет V1;
- replay «отдел 2» не возвращает №20;
- same-host p50/p95/p99 измерены при согласованной конкуренции;
- GigaChat certificate/OAuth smoke выполнен с включённой TLS verification.
