#!/usr/bin/env python3
"""
WMTS瓦片下载脚本（异步IO版，内存优化）
使用 asyncio + httpx 实现高并发异步下载
配置请查看 config.py

相对旧版的内存优化：
1. 进度记录改为 JSONL 追加写（O(1) 内存），不再把全部瓦片记录常驻内存；
   （断点续传依据是「文件是否已存在」，进度文件仅作日志，旧 dict 格式自动归档）
2. 任务改为「有界队列 + 固定 worker 池」，不再一次性创建全部协程；
3. 下载改为流式写盘（client.stream），避免整片缓冲进内存。
4. 重试改为指数退避，且只重试可恢复错误（429/5xx/网络错误），404 等直接判失败。
"""

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import aiofiles
import httpx
from tqdm import tqdm
from config import *
from tile_path import ensure_tile_dir, find_tile, legacy_tile_path, tile_path

# ============ 下载配置 ============
ASYNC_MAX_WORKERS = MAX_WORKERS  # 默认并发数（GUI 会按界面配置覆盖）
PROGRESS_FILE = "download_progress.json"  # 进度日志（JSONL，追加写）
FAILED_FILE = "download_failed.txt"  # 失败记录文件
PROGRESS_FLUSH_EVERY = 500  # 每完成多少片 flush 一次进度日志
QUEUE_FACTOR = 2  # 任务队列长度 = 并发数 * QUEUE_FACTOR（限制内存占用）

# 可重试的 HTTP 状态码（其余 4xx 视为不可恢复，直接失败）
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _is_retryable(exc, status=None):
    """判断错误是否值得重试"""
    if status is not None:
        return status in RETRYABLE_STATUS
    if isinstance(exc, ValueError):  # 内容无效（如坏 PNG）
        return True
    return isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError))


class AsyncTileDownloader:
    """异步瓦片下载器（内存优化版）"""

    # PNG 文件头标识
    PNG_HEADER = b'\x89PNG\r\n\x1a\n'

    def __init__(self, output_dir=OUTPUT_DIR, max_workers=ASYNC_MAX_WORKERS, progress_callback=None):
        self.output_dir = output_dir
        self.max_workers = max_workers
        self.semaphore = asyncio.Semaphore(max_workers)
        self.success_count = 0
        self.fail_count = 0
        self.skip_count = 0
        self.invalid_count = 0
        self.retried_count = 0  # 重试次数统计
        self.failed_tasks = []
        # 进度回调（供 GUI 使用，例如 gui_main.py）
        # 回调签名: callback(info: dict)，info 包含 stage/done/total/success/fail/skip 等字段
        self.progress_callback = progress_callback
        # 取消事件（GUI 可通过 set() 请求停止下载）
        self.cancel_event = threading.Event()
        # 进度日志句柄（懒加载）
        self._progress_fh = None
        self._since_flush = 0
        # 已创建的行目录缓存（避免重复 os.makedirs 系统调用）
        self._created_dirs = set()

    # ---------- 进度日志（JSONL 追加写，O(1) 内存） ----------
    def _open_progress_log(self):
        """打开进度日志；检测到旧版 dict 格式则先归档为 .bak"""
        if self._progress_fh is not None:
            return
        if os.path.exists(PROGRESS_FILE):
            try:
                with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
                    first = f.read(1).strip()
                # 旧版本是 {"filename": {...}, ...} 的 dict JSON，先归档（不影响断点续传）
                if first == '{':
                    os.replace(PROGRESS_FILE, PROGRESS_FILE + '.bak')
            except OSError:
                pass
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        self._progress_fh = open(PROGRESS_FILE, 'a', encoding='utf-8', buffering=65536)
        self._since_flush = 0

    def _log_done(self, filename):
        """记录一片完成（追加一行，不占内存）"""
        if self._progress_fh is None:
            self._open_progress_log()
        self._progress_fh.write(json.dumps({"f": filename}, ensure_ascii=False) + "\n")
        self._since_flush += 1
        if self._since_flush >= PROGRESS_FLUSH_EVERY:
            self._progress_fh.flush()
            self._since_flush = 0

    def _close_progress_log(self, clean=False):
        """关闭进度日志；clean=True 且无失败时清理日志文件（瓦片文件本身即断点依据）"""
        if self._progress_fh is not None:
            self._progress_fh.flush()
            self._progress_fh.close()
            self._progress_fh = None
            if clean and os.path.exists(PROGRESS_FILE):
                try:
                    os.remove(PROGRESS_FILE)
                except OSError:
                    pass

    def _save_failed_tasks(self):
        """保存失败任务"""
        with open(FAILED_FILE, 'w', encoding='utf-8') as f:
            for task in self.failed_tasks:
                f.write(f"{task}\n")

    def _is_valid_png(self, filepath):
        """验证文件是否是有效的PNG图片（通过文件头）"""
        try:
            with open(filepath, 'rb') as f:
                return f.read(8) == self.PNG_HEADER
        except OSError:
            return False

    # ---------- 单个瓦片下载 ----------
    async def download_tile(self, client, tile_matrix, tile_col, tile_row, max_retries=3):
        """异步下载单个瓦片（流式写盘 + 智能重试 + 文件验证）"""
        # 检查是否已请求取消
        if self.cancel_event.is_set():
            return "cancelled"

        async with self.semaphore:  # 控制并发数
            if self.cancel_event.is_set():
                return "cancelled"

            url = get_tile_url(tile_matrix, tile_col, tile_row)

            # 断点续传：新/旧结构任一已存在且有效则跳过
            existing = find_tile(tile_matrix, tile_col, tile_row, self.output_dir)
            if existing is not None:
                if self._is_valid_png(existing):
                    self.skip_count += 1
                    return "skipped"
                # 文件无效，删除并重新下载（新旧路径都清理）
                try:
                    os.remove(existing)
                except OSError:
                    pass
                lp = legacy_tile_path(tile_matrix, tile_col, tile_row, self.output_dir)
                if lp != existing:
                    try:
                        os.remove(lp)
                    except OSError:
                        pass
                self.invalid_count += 1

            # 分级缓存：tiles/{matrix}/{row}/{col}.png，先确保行目录存在（带缓存）
            dir_key = (tile_matrix, tile_row)
            if dir_key not in self._created_dirs:
                ensure_tile_dir(tile_matrix, tile_row, self.output_dir)
                self._created_dirs.add(dir_key)
            filepath = tile_path(tile_matrix, tile_col, tile_row, self.output_dir)
            # 进度日志只记录逻辑坐标（断点续传依据是文件存在，非日志内容）
            filename = f"{tile_matrix}_{tile_col}_{tile_row}"

            last_exc = None
            for attempt in range(max_retries):
                try:
                    # 流式下载：边下边写，避免整片缓冲在内存
                    await self._download_to_file(client, url, filepath)

                    # 验证下载的文件是否有效PNG
                    if not self._is_valid_png(filepath):
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        self.invalid_count += 1
                        raise ValueError("下载内容不是有效的PNG图片")

                    # 记录成功
                    self.success_count += 1
                    self._log_done(filename)
                    return "success"

                except Exception as e:
                    last_exc = e
                    resp = getattr(e, "response", None)
                    status = getattr(resp, "status_code", None)
                    if not _is_retryable(e, status):
                        break
                    if attempt < max_retries - 1:
                        # 指数退避：429/503 表示服务端要求降速，退避更久
                        if status in (429, 503):
                            delay = min(1.0 * (2 ** attempt), 8)
                        else:
                            delay = min(0.5 * (2 ** attempt), 4)
                        self.retried_count += 1
                        await asyncio.sleep(delay)
                    else:
                        break

            # 最终失败
            self.fail_count += 1
            self.failed_tasks.append(f"[{tile_col},{tile_row}] {last_exc}")
            return "failed"

    async def _download_to_file(self, client, url, filepath):
        """流式下载：分块写入，不把整片数据缓存在内存"""
        async with client.stream("GET", url, timeout=TIMEOUT) as resp:
            if resp.status_code >= 400:
                # 抛带状态码的异常，供重试策略判断
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            async with aiofiles.open(filepath, 'wb') as f:
                async for chunk in resp.aiter_bytes():
                    await f.write(chunk)

    # ---------- 批量下载（有界队列 + 固定 worker 池） ----------
    async def run_tasks(self, client, tasks, total, desc="下载进度", emit_callback=True):
        """执行任务列表（惰性生成，内存占用恒定为 O(并发数)，与任务总数无关）

        tasks: 可迭代对象，产出 (tile_matrix, col, row)
        emit_callback: 是否推送进度回调（重试场景无需 GUI 回调）
        """
        q = asyncio.Queue(maxsize=max(1, self.max_workers * QUEUE_FACTOR))
        done = 0
        last = [None, None, ""]
        # CLI 模式（无回调）显示 tqdm 进度条；GUI 模式隐藏
        use_tqdm = self.progress_callback is None
        pbar = tqdm(total=total, desc=desc, unit="片", disable=not use_tqdm)

        def on_done(result, col, row):
            nonlocal done
            done += 1
            last[:] = [col, row, result]
            if use_tqdm:
                pbar.update(1)
                pbar.set_postfix_str(
                    f"成功:{self.success_count} 失败:{self.fail_count} "
                    f"跳过:{self.skip_count} 无效:{self.invalid_count} 重试:{self.retried_count}"
                )
            if emit_callback and self.progress_callback:
                self.progress_callback({
                    "stage": "download",
                    "done": done,
                    "total": total,
                    "success": self.success_count,
                    "fail": self.fail_count,
                    "skip": self.skip_count,
                    "invalid": self.invalid_count,
                    "retried": self.retried_count,
                    "cancelled": self.cancel_event.is_set(),
                    "finished": False,
                    # 刚完成的瓦片坐标与结果（供 GUI 网格着色）
                    "last_col": col,
                    "last_row": row,
                    "last_result": result,
                })

        async def worker():
            while True:
                if self.cancel_event.is_set():
                    break
                item = await q.get()
                if item is None:  # 毒丸：正常结束
                    break
                matrix, col, row = item
                result = await self.download_tile(client, matrix, col, row)
                on_done(result, col, row)

        workers = [asyncio.create_task(worker()) for _ in range(self.max_workers)]

        try:
            # 生产者：惰性生成任务；队列满时最多等 0.5s，避免取消信号被
            # 满队列阻塞（worker 可能全部卡在超时/重试中而无人消费）
            for matrix, col, row in tasks:
                if self.cancel_event.is_set():
                    break
                try:
                    await asyncio.wait_for(q.put((matrix, col, row)), timeout=0.5)
                except asyncio.TimeoutError:
                    if self.cancel_event.is_set():
                        break
                    continue  # 队列仍满，重试放入
            if not self.cancel_event.is_set():
                # 所有真实任务入队后再放毒丸（FIFO 保证毒丸最后被消费）
                for _ in workers:
                    await q.put(None)
        finally:
            if self.cancel_event.is_set():
                # 取消：直接取消 worker，丢弃队列中未处理的任务
                for w in workers:
                    w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            pbar.close()

        # 结束回调
        if emit_callback and self.progress_callback:
            self.progress_callback({
                "stage": "download",
                "done": done,
                "total": total,
                "success": self.success_count,
                "fail": self.fail_count,
                "skip": self.skip_count,
                "invalid": self.invalid_count,
                "retried": self.retried_count,
                "cancelled": self.cancel_event.is_set(),
                "finished": True,
                "last_col": last[0],
                "last_row": last[1],
                "last_result": "cancelled" if self.cancel_event.is_set() else last[2],
            })

    async def download_batch(self, tile_matrix, col_start, col_end, row_start, row_end):
        """批量下载瓦片"""
        # 创建输出目录
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        cols = col_end - col_start + 1
        rows = row_end - row_start + 1
        total = cols * rows

        # 惰性生成任务迭代器（不一次性创建全部任务，内存 O(1)）
        def gen_tasks():
            for col in range(col_start, col_end + 1):
                for row in range(row_start, row_end + 1):
                    yield tile_matrix, col, row

        # 创建异步 HTTP 客户端（HTTP/2 支持，连接池）
        limits = httpx.Limits(
            max_connections=self.max_workers * 2,  # 连接池大小
            max_keepalive_connections=self.max_workers  # 保持活跃的连接数
        )

        async with httpx.AsyncClient(
            headers=HEADERS,
            limits=limits,
            http2=True,  # 启用 HTTP/2
            timeout=httpx.Timeout(TIMEOUT)
        ) as client:
            await self.run_tasks(client, gen_tasks(), total, desc="下载进度", emit_callback=True)

        # 保存进度和失败记录
        self._close_progress_log(clean=self.fail_count == 0)
        if self.failed_tasks:
            self._save_failed_tasks()


async def retry_failed_tasks(downloader, tile_matrix):
    """重试失败的任务"""
    if not os.path.exists(FAILED_FILE):
        return

    print("\n开始重试失败的任务...")

    with open(FAILED_FILE, 'r', encoding='utf-8') as f:
        failed_lines = [line.strip() for line in f if line.strip()]

    if not failed_lines:
        return

    # 解析失败任务
    retry_tasks = []
    for line in failed_lines:
        try:
            # 格式: [col,row] error_msg
            if line.startswith('['):
                parts = line.split(']')
                coord_part = parts[0][1:]  # 移除 '['
                coords = coord_part.split(',')
                retry_tasks.append((tile_matrix, int(coords[0]), int(coords[1])))
        except (ValueError, IndexError):
            continue

    if not retry_tasks:
        return

    print(f"找到 {len(retry_tasks)} 个失败任务，开始重试...")

    # 创建异步 HTTP 客户端
    limits = httpx.Limits(
        max_connections=downloader.max_workers * 2,
        max_keepalive_connections=downloader.max_workers
    )

    async with httpx.AsyncClient(
        headers=HEADERS,
        limits=limits,
        http2=True,
        timeout=httpx.Timeout(TIMEOUT)
    ) as client:
        await downloader.run_tasks(client, iter(retry_tasks), len(retry_tasks), desc="重试进度", emit_callback=False)

    # 更新失败记录（成功的瓦片不再保留）
    if downloader.failed_tasks:
        downloader._save_failed_tasks()
    elif os.path.exists(FAILED_FILE):
        try:
            os.remove(FAILED_FILE)
        except OSError:
            pass


async def main():
    """主函数"""
    # 创建下载器
    downloader = AsyncTileDownloader(
        output_dir=OUTPUT_DIR,
        max_workers=ASYNC_MAX_WORKERS
    )

    # 创建输出目录
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    print(f"开始下载瓦片（异步版，内存优化）...")
    print(f"级别: {TILE_MATRIX}")
    print(f"列范围: {TILE_COL_START} - {TILE_COL_END}")
    print(f"行范围: {TILE_ROW_START} - {TILE_ROW_END}")
    print(f"总计瓦片数: {(TILE_COL_END - TILE_COL_START + 1) * (TILE_ROW_END - TILE_ROW_START + 1)}")
    print(f"并发数: {ASYNC_MAX_WORKERS}")
    print(f"HTTP/2: 已启用")
    print(f"连接池: {ASYNC_MAX_WORKERS * 2}")
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
