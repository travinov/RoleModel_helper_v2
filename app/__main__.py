from __future__ import annotations

import uvicorn

from app.bootstrap import build_default_app
from app.config import Settings


def main() -> None:
    settings = Settings.from_env()
    settings.validate_isolation()
    uvicorn.run(
        build_default_app(),
        host=settings.host,
        port=settings.port,
        access_log=True,
    )


if __name__ == "__main__":
    main()
