from __future__ import annotations

import json

from app.config import Settings
from app.runtime.postgres import PostgresMigrator


def main() -> None:
    settings = Settings.from_env()
    settings.validate_isolation()
    result = PostgresMigrator(
        dsn=settings.effective_migration_dsn,
        schema=settings.state_schema,
        v1_schema=settings.v1_state_schema,
        app_role=settings.database_app_role,
    ).migrate()
    print(
        json.dumps(
            {
                "status": "ok",
                "schema": settings.state_schema,
                "current_version": result.current_version,
                "applied_versions": list(result.applied_versions),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
