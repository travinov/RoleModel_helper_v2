from __future__ import annotations

import argparse
import json
from typing import Any

from app.catalog.postgres import PostgresCatalogPublisher
from app.catalog.v1_snapshot import V1SnapshotCatalogAdapter
from app.config import Settings


def publish_active_v1(
    settings: Settings,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if not settings.catalog_import_dsn:
        raise ValueError(
            "RMV2_CATALOG_IMPORT_DSN is required for V1 snapshot import"
        )
    extracted = V1SnapshotCatalogAdapter(
        dsn=settings.catalog_import_dsn,
        v1_schema=settings.v1_catalog_schema,
    ).extract()
    report: dict[str, Any] = {
        "status": "validated" if dry_run else "published",
        "dry_run": dry_run,
        "source_schema": settings.v1_catalog_schema,
        "target_schema": settings.catalog_schema,
        "snapshot_id": extracted.snapshot_id,
        "version": extracted.document["version"],
        "source_sha256": extracted.source_sha256,
        "counts": extracted.counts,
    }
    if dry_run:
        return report
    publication = PostgresCatalogPublisher(
        dsn=settings.catalog_import_dsn,
        schema=settings.catalog_schema,
    ).publish(
        extracted.document,
        source_sha256=extracted.source_sha256,
    )
    report["previous_version"] = publication.previous_version
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish the active read-only V1 snapshot into V2"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and validate counts without writing the V2 catalog",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.validate_isolation()
    print(
        json.dumps(
            publish_active_v1(settings, dry_run=args.dry_run),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
