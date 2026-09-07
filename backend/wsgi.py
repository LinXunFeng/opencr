#!/usr/bin/env python3
"""
WSGI 入口文件 - 用于 Gunicorn 生产环境
"""

from review_server import app

if __name__ == "__main__":
    app.run()
