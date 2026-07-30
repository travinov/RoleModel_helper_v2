# Архитектура RoleModel Helper V2

## Разделение контуров

```mermaid
flowchart LR
    U["Пользователь"] --> API["FastAPI V2<br/>127.0.0.1:8001"]
    API --> RT["Runtime service"]
    RT --> SR["rolemodel_v2_runtime<br/>state + turns + telemetry"]
    RT --> A["Bounded agent"]
    A --> C["Immutable catalog cache"]
    C --> CR["rolemodel_v2_catalog<br/>active release"]
    A --> T["Typed read-only tools"]
    T --> C
    A -. "0 или 1 planning call" .-> G["GigaChat"]

    V1["V1 active snapshot"] -->|"read-only import job"| P["Normalizer + validator"]
    P -->|"atomic publish"| CR
    P -. "не используется в request path" .-> API
```

V1 и V2 могут находиться в одном PostgreSQL cluster и одной database.
Изоляция достигается схемами и ролями, а не отдельным сервером.

## Один пользовательский ход

```mermaid
flowchart TD
    R["Запрос + request_id + state_revision"] --> V["API validation"]
    V --> S["Чтение состояния V2"]
    S --> PIN["Pin одной версии каталога"]
    PIN --> D{"Детерминированный маршрут?"}
    D -->|"да"| TOOLS["Typed tools"]
    TOOLS --> RES{"Однозначно?"}
    RES -->|"да"| FACT["Фактический ответ"]
    RES -->|"нет"| ASK["До 5 вариантов / один уточняющий slot"]
    D -->|"нет"| RET["Локальный retrieval<br/>до 5 кандидатов"]
    RET --> LLM["1 structured GigaChat call"]
    LLM --> CHECK["Schema + action + ID + version validation"]
    CHECK -->|"валидно"| EXEC["Одно детерминированное выполнение tool plan"]
    CHECK -->|"ошибка / timeout"| SAFE["Явное уточнение или retryable failure"]
    EXEC --> FACT
    FACT --> COMMIT["Atomic state + turn + telemetry commit"]
    ASK --> COMMIT
    SAFE --> COMMIT
```

Рекурсивной Hermes-подобной автономной петли здесь намеренно нет. Для
read-only поиска прав она не добавляет новых возможностей, зато увеличивает
число удалённых вызовов и время ожидания. Применена ограниченная схема:

- максимум один LLM planning step;
- максимум один детерминированный execution step;
- максимум пять кандидатов одного типа;
- состояние диалога продолжается в следующем пользовательском ходе;
- факты всегда повторно проверяются по pinned catalog version.

## Инструменты

Инструменты — обычные типизированные Python-функции над прогретым immutable
каталогом, не `grep`-скрипты и не свободный SQL модели:

| Инструмент | Назначение | Жёсткие ограничения |
|---|---|---|
| `search_departments` | подразделение по имени/номеру/городу | номер — exact filter; limit ≤ 10 |
| `search_positions` | должность с контекстом отдела/города | только найденные profile relations |
| `resolve_profiles` | профиль по отделу + должности + городу | точные foreign relations |
| `search_systems` | АС, доступные профилю | только profile access |
| `search_roles` | роли в выбранной АС | только profile + system relation |
| `get_access_instruction` | инструкция по АС | citation из каталога |

Нормализация (`№2`, `номер 2`, `второй отдел`, `2-й отдел`) выполняется при
публикации каталога. На запросе выполняется только дешёвая нормализация текста
и поиск по уже подготовленным полям.

## Что получает GigaChat

GigaChat не получает полный список АС, ролей, отделов или должностей. Payload
содержит:

- текст текущего запроса (до 2000 символов);
- компактное состояние диалога;
- catalog version;
- максимум 5 кандидатов АС/подразделений/должностей;
- максимум 3 совпавшие роли внутри кандидата АС.

Ответ модели — только план. Названия ролей, уровни доступа и инструкции
формируются из каталога после проверки ID.

## Контур каталога

```mermaid
sequenceDiagram
    participant Job as "Import job role"
    participant V1 as "V1 schema"
    participant N as "Normalizer/validator"
    participant V2 as "V2 catalog schema"
    participant W as "Runtime worker"

    Job->>V1: "BEGIN REPEATABLE READ READ ONLY"
    V1-->>Job: "active snapshot + source SHA-256"
    Job->>N: "raw source rows"
    N->>N: "stable IDs, names, aliases, numbers, relations"
    N->>V2: "insert STAGING release"
    N->>V2: "validate and atomically switch ACTIVE"
    W->>V2: "poll active pointer every 5s"
    V2-->>W: "new version"
    W->>W: "load immutable bundle and swap between turns"
```

Ошибка импорта или публикации не меняет предыдущий ACTIVE-релиз. Ошибка
фонового refresh оставляет last-good bundle и переводит health freshness в
`DEGRADED`.

## PostgreSQL-роли

| Роль | V1 | V2 runtime | V2 catalog |
|---|---|---|---|
| runtime app | нет доступа | SELECT/DML | SELECT |
| catalog import job | SELECT ограниченных ETL-таблиц | нет доступа | SELECT/DML |
| migration owner | по политике DBA | DDL | DDL |

Все таблицы в коде schema-qualified. Модель не получает DSN и не выполняет
SQL.

## Наблюдаемость

Каждый успешно зафиксированный ход хранит:

- trace ID, request ID, route и outcome;
- catalog version;
- число GigaChat calls (0 или 1);
- total и GigaChat latency;
- полный diagnostics JSON для локального разбора.

Команда `python -m app.runtime.telemetry --hours 24` считает p50/p95/p99 без
чтения пользовательских сообщений.
