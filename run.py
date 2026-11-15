"""启动 kfm_tdx_industry（默认 0.0.0.0:13520）。"""
from __future__ import annotations

import argparse

import uvicorn

from app import config


def main():
    cfg = config.load()
    parser = argparse.ArgumentParser(description="kfm_tdx_industry")
    parser.add_argument("--host", default=cfg["host"])
    parser.add_argument("--port", type=int, default=cfg["port"])
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
