# V2 на текущем PostgreSQL: production runbook

V2 переиспользует существующий PostgreSQL-кластер и существующую database.
Новый PostgreSQL-сервер не нужен. V1 остаётся владельцем своей схемы; V2
получает две новые схемы:

- `rolemodel_v2_runtime` — сессии, ходы и latency telemetry;
- `rolemodel_v2_catalog` — нормализованные immutable-релизы каталога.

## Роли

Рекомендуются три учётные записи:

- migration owner — создаёт/изменяет только схемы V2;
- `rolemodel_v2_app` — DML в runtime и SELECT в catalog;
- `rolemodel_v2_catalog_writer` — SELECT ограниченных таблиц V1 и DML в
  catalog V2; используется только командой публикации.

Пароли создаются DBA и передаются через серверный secret store / `.env` с
правами `0600`; их нельзя добавлять в Git.

Пример DBA-заготовки без паролей:

```sql
CREATE ROLE rolemodel_v2_app LOGIN;
CREATE ROLE rolemodel_v2_catalog_writer LOGIN;
```

Migration CLI сама выдаёт гранты внутри схем V2. Для import job DBA отдельно
выдаёт минимальный `USAGE` и `SELECT` на V1:

```sql
GRANT USAGE ON SCHEMA <v1_schema> TO rolemodel_v2_catalog_writer;
GRANT SELECT ON
  <v1_schema>.etl_run,
  <v1_schema>.snapshot,
  <v1_schema>.profile,
  <v1_schema>.profile_structure_segment,
  <v1_schema>.profile_department,
  <v1_schema>.profile_position,
  <v1_schema>.system,
  <v1_schema>.system_alias,
  <v1_schema>.system_alias_candidate,
  <v1_schema>.department_alias_candidate,
  <v1_schema>.entitlement,
  <v1_schema>.profile_entitlement_access
TO rolemodel_v2_catalog_writer;
```

Runtime-роль не получает эти гранты.

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
