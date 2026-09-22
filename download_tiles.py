#!/usr/bin/env python3
"""
WMTS瓦片下载脚本（CLI 快速入口）— 实现已迁移到 wmts/core/downloader.py。

本文件保持命令行与旧 import 路径兼容：
    from download_tiles import AsyncTileDownloader
"""

from __future__ import annotations

import asyncio
import time

from wmts.core.config import Config
from wmts.core.downloader import (  # noqa: F401
    AuthError,
    AsyncTileDownloader,
    RETRYABLE_STATUS,
)
from wmts.core.events import make_event

# 旧代码里的模块级常量名保留（供外部引用），实际值以 Config 为准
ASYNC_MAX_WORKERS = 64
PROGRESS_FILE = "download_progress.json"
FAILED_FILE = "download_failed.txt"


def _print_stats_header(cfg: Config, workers: int) -> None:
    print("开始下载瓦片（异步版，内存优化）...")
    print(f"级别: {cfg.tile_matrix}")
    print(f"列范围: {cfg.col_start} - {cfg.col_end}")
    print(f"行范围: {cfg.row_start} - {cfg.row_end}")
    print(f"总计瓦片数: {cfg.total}")
    print(f"并发数: {workers}")
    print(f"HTTP/2: 已启用")
    print("-" * 60)


def _print_stats_footer(downloader: AsyncTileDownloader, elapsed: float) -> None:
    stats = downloader.success_count + downloader.fail_count + downloader.skip_count
    speed = stats / elapsed if elapsed > 0 else 0
    print("-" * 60)
    print("下载完成!")
    print(f"总计: {stats} | 成功: {downloader.success_count} | 失败: {downloader.fail_count} "
          f"| 跳过: {downloader.skip_count}")
    print(f"无效文件: {downloader.invalid_count} | 重试次数: {downloader.retried_count}")
    print(f"耗时: {elapsed:.2f}秒 | 速度: {speed:.2f}片/秒")
    if downloader.fail_count > 0:
        print(f"\n有 {downloader.fail_count} 个任务失败，已记录到 {downloader.config.failed_file}")
        print("可以重新运行脚本来自动重试失败的任务")


async def _run(workers: int) -> None:
    cfg = Config.load()
    cfg.max_workers = workers
    downloader = AsyncTileDownloader(cfg)
    _print_stats_header(cfg, workers)
    start = time.time()
    try:
        await downloader.download_batch(
            cfg.tile_matrix, cfg.col_start, cfg.col_end, cfg.row_start, cfg.row_end)
    except AuthError as e:
        print(f"\n错误: {e}")
        raise SystemExit(1)
    if downloader.failed_tasks:
        print("\n开始重试失败的任务...")
        await downloader.retry_failed(cfg.tile_matrix)
    _print_stats_footer(downloader, time.time() - start)


def main() -> None:
    asyncio.run(_run(ASYNC_MAX_WORKERS))


if __name__ == "__main__":
    main()
