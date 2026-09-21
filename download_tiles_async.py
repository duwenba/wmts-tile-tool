#!/usr/bin/env python3
"""
WMTS瓦片下载脚本（异步IO版，CLI 快速入口）

实际实现已合并到 download_tiles.py（内存优化版：JSONL 追加写进度、
有界队列 + 固定 worker 池、流式写盘、指数退避重试）。
本文件仅保持命令行入口兼容，避免两套代码分叉。
"""

import asyncio
import os
import time
from pathlib import Path

from config import *
from download_tiles import AsyncTileDownloader as _BaseDownloader
from download_tiles import retry_failed_tasks

# ============ 异步下载配置 ============
ASYNC_MAX_WORKERS = 64  # CLI 默认并发数（比 GUI 默认高，适合纯下载场景）
PROGRESS_FILE = "download_progress.json"  # 进度日志文件
FAILED_FILE = "download_failed.txt"  # 失败记录文件


class AsyncTileDownloader(_BaseDownloader):
    """CLI 版下载器（默认 64 并发）"""

    def __init__(self, output_dir=OUTPUT_DIR, max_workers=ASYNC_MAX_WORKERS, progress_callback=None):
        super().__init__(
            output_dir=output_dir,
            max_workers=max_workers,
            progress_callback=progress_callback,
        )


async def main():
    """主函数"""
    # 创建下载器
    downloader = AsyncTileDownloader()

    # 创建输出目录
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    print(f"开始下载瓦片（异步版，内存优化）...")
    print(f"级别: {TILE_MATRIX}")
    print(f"列范围: {TILE_COL_START} - {TILE_COL_END}")
    print(f"行范围: {TILE_ROW_START} - {TILE_ROW_END}")
    print(f"总计瓦片数: {(TILE_COL_END - TILE_COL_START + 1) * (TILE_ROW_END - TILE_ROW_START + 1)}")
    print(f"并发数: {ASYNC_MAX_WORKERS}")
    print(f"HTTP/2: 已启用")
    print("-" * 60)

    start_time = time.time()

    # 执行批量下载
    await downloader.download_batch(
        TILE_MATRIX,
        TILE_COL_START,
        TILE_COL_END,
        TILE_ROW_START,
        TILE_ROW_END
    )

    # 如果有失败的任务，询问是否重试
    if downloader.failed_tasks and os.path.exists(FAILED_FILE):
        await retry_failed_tasks(downloader, TILE_MATRIX)

    elapsed_time = time.time() - start_time

    # 计算统计信息
    total_count = downloader.success_count + downloader.fail_count + downloader.skip_count
    speed = total_count / elapsed_time if elapsed_time > 0 else 0

    print("-" * 60)
    print(f"下载完成!")
    print(f"总计: {total_count} | 成功: {downloader.success_count} | 失败: {downloader.fail_count} | 跳过: {downloader.skip_count}")
    print(f"无效文件: {downloader.invalid_count} | 重试次数: {downloader.retried_count}")
    print(f"耗时: {elapsed_time:.2f}秒 | 速度: {speed:.2f}片/秒")
    print(f"保存目录: {os.path.abspath(OUTPUT_DIR)}")

    if downloader.fail_count > 0:
        print(f"\n有 {downloader.fail_count} 个任务失败，已记录到 {FAILED_FILE}")
        print("可以重新运行脚本来自动重试失败的任务")


if __name__ == "__main__":
    asyncio.run(main())
