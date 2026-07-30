from __future__ import annotations

import argparse
import json
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.config import Settings


class PostgresTurnMetrics:
    """Aggregates only low-cardinality V2 turn telemetry."""

    def __init__(self, *, dsn: str, schema: str) -> None:
        self._dsn = dsn
        self._schema = schema

    def summary(self, *, hours: int = 24) -> dict[str, Any]:
        bounded_hours = max(1, min(int(hours), 24 * 366))
        schema = sql.Identifier(self._schema)
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            overall = connection.execute(
                sql.SQL(
                    """
                    SELECT count(*) AS turns,
                           coalesce(sum(gigachat_calls), 0) AS gigachat_calls,
                           coalesce(
                               percentile_cont(0.50)
                               WITHIN GROUP (ORDER BY total_ms),
                               0
                           ) AS p50,
                           coalesce(
                               percentile_cont(0.95)
                               WITHIN GROUP (ORDER BY total_ms),
                               0
                           ) AS p95,
                           coalesce(
                               percentile_cont(0.99)
                               WITHIN GROUP (ORDER BY total_ms),
                               0
                           ) AS p99
                    FROM {}.turn_message
                    WHERE created_at >=
                          now() - (%s * interval '1 hour')
                    """
                ).format(schema),
                (bounded_hours,),
            ).fetchone()
            route_rows = connection.execute(
                sql.SQL(
                    """
                    SELECT route, count(*) AS turns,
                           coalesce(sum(gigachat_calls), 0) AS gigachat_calls,
                           percentile_cont(0.95)
                               WITHIN GROUP (ORDER BY total_ms) AS p95
                    FROM {}.turn_message
                    WHERE created_at >=
                          now() - (%s * interval '1 hour')
                    GROUP BY route
                    ORDER BY route
                    """
                ).format(schema),
                (bounded_hours,),
            ).fetchall()
        assert overall is not None
        return {
            "window_hours": bounded_hours,
            "turns": int(overall["turns"]),
            "gigachat_calls": int(overall["gigachat_calls"]),
            "latency_ms": {
                "p50": float(overall["p50"]),
                "p95": float(overall["p95"]),
                "p99": float(overall["p99"]),
            },
            "routes": {
                str(row["route"]): {
                    "turns": int(row["turns"]),
                    "gigachat_calls": int(row["gigachat_calls"]),
                    "p95_ms": float(row["p95"]),
                }
                for row in route_rows
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize V2 route and latency telemetry"
    )
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.validate_isolation()
    report = PostgresTurnMetrics(
        dsn=settings.database_dsn,
        schema=settings.state_schema,
    ).summary(hours=args.hours)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
