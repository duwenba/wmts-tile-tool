"""
wmts.server — 服务启动入口。

用法::

    uv run python -m wmts.server                 # 默认 127.0.0.1:8760，自动打开浏览器
    uv run python -m wmts.server --port 9000
    uv run python -m wmts.server --host 0.0.0.0  # 允许局域网访问（注意 Cookie 安全）
    uv run python -m wmts.server --no-browser
"""

from __future__ import annotations

import argparse
import threading
import webbrowser


def main() -> None:
    ap = argparse.ArgumentParser(description="WMTS 瓦片工具 Web 服务")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认 127.0.0.1，仅本机访问）")
    ap.add_argument("--port", type=int, default=8760, help="监听端口（默认 8760）")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}/"
    print(f"WMTS 瓦片工具 Web 服务: {url}")
    print("API 文档 (Swagger): http://%s:%d/docs" % (args.host, args.port))

    if not args.no_browser and args.host in ("127.0.0.1", "localhost"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    import uvicorn

    uvicorn.run("wmts.api:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
