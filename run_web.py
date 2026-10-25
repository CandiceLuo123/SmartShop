# -*- coding: utf-8 -*-
"""Web 服务启动入口：python run_web.py [--port 8000]"""
import argparse

import uvicorn

from web.db import ensure_schema

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    ensure_schema()
    print(f"小铺智答 Web 服务启动: http://{args.host}:{args.port}"
          f"（顾客端 / ，登录 /login ，商家端 /admin）")
    uvicorn.run("web.server:app", host=args.host, port=args.port, log_level="warning")
