from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.models import TurnRequest, TurnState
from app.agent.service import AgentEngine
from app.catalog.cache import CatalogCache
from app.catalog.json_source import JsonCatalogSource


class ForbiddenPlanner:
    def plan(self, request, context, *, deadline_ms: int):
        raise AssertionError("Fast-path benchmark called GigaChat")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=200)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "demo_catalog.json",
    )
    args = parser.parse_args()
    if args.turns < 10:
        parser.error("--turns must be at least 10")

    source = JsonCatalogSource(args.catalog)
    cache = CatalogCache(source)
    cache.refresh("demo-v1")
    engine = AgentEngine(catalog_cache=cache, planner=ForbiddenPlanner())
    timings: list[float] = []
    gigachat_calls = 0

    for turn_no in range(args.turns):
        started = time.perf_counter()
        result = engine.handle(
            TurnRequest(
                request_id=f"benchmark-{turn_no}",
                text="Покажи роли в Демо АС Доступ",
                state=TurnState.empty(session_id=f"benchmark-session-{turn_no}"),
            )
        )
        timings.append((time.perf_counter() - started) * 1000.0)
        gigachat_calls += result.diagnostics.gigachat_calls

    report = {
        "kind": "local_in_process_fast_path",
        "turns": args.turns,
        "catalog_version": cache.get().version,
        "gigachat_calls": gigachat_calls,
        "p50_ms": round(statistics.median(timings), 3),
        "p95_ms": round(percentile(timings, 0.95), 3),
        "p99_ms": round(percentile(timings, 0.99), 3),
        "max_ms": round(max(timings), 3),
        "provisional_guard_p95_ms": 50.0,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if gigachat_calls != 0 or report["p95_ms"] >= 50.0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
