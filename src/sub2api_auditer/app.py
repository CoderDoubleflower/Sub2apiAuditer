from __future__ import annotations

import argparse
import os

import uvicorn

from .service import env_int
from .web import create_app

app = create_app()


def cli() -> None:
    parser = argparse.ArgumentParser(description="sub2api 提示词审计格式转换服务")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=env_int("PORT", 8080))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"))
    args = parser.parse_args()
    uvicorn.run(
        "sub2api_auditer.app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    cli()

__all__ = ["app", "cli", "create_app"]
