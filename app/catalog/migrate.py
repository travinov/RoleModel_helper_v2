from __future__ import annotations

import json

from app.catalog.postgres import PostgresCatalogMigrator
from app.config import Settings


def main() -> None:
    settings = Settings.from_env()
    settings.validate_isolation()
    result = PostgresCatalogMigrator(
        dsn=settings.effective_migration_dsn,
        schema=settings.catalog_schema,
        v1_schema=settings.v1_state_schema,
        reader_role=settings.catalog_reader_role or settings.database_app_role,
        writer_role=settings.catalog_writer_role,
    ).migrate()
    print(
        json.dumps(
            {
                "status": "ok",
                "schema": settings.catalog_schema,
                "current_version": result.current_version,
                "applied_versions": list(result.applied_versions),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
