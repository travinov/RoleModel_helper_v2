from __future__ import annotations

import json
from pathlib import Path

from app.catalog.cache import CatalogBundle


class CatalogVersionMismatch(ValueError):
    pass


class JsonCatalogSource:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self, version: str) -> CatalogBundle:
        with self.path.open(encoding="utf-8") as stream:
            payload = json.load(stream)
        actual = str(payload.get("version") or "")
        if actual != version:
            raise CatalogVersionMismatch(
                f"Catalog file contains version {actual!r}, requested {version!r}"
            )
        return CatalogBundle.from_mapping(payload)
