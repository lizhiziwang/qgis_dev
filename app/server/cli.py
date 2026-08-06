"""命令行入口。"""

from __future__ import annotations

import argparse

import uvicorn

from app.server.app import create_app
from app.server.config import ServerConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="qgis_dev FastAPI service")
    parser.add_argument("--host", default=None, help="bind host")
    parser.add_argument("--port", type=int, default=None, help="bind port")
    parser.add_argument("--workers", type=int, default=None, help="uvicorn workers")
    parser.add_argument("--reload", action="store_true", help="reload on file change")
    args = parser.parse_args()

    cfg = ServerConfig.from_env()
    host = args.host or cfg.host
    port = args.port or cfg.port
    workers = args.workers or cfg.workers
    reload = args.reload or cfg.reload

    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        workers=workers,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
