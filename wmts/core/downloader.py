"""
wmts.core.downloader — 异步瓦片下载器（UI 无关）。

由原 download_tiles.py 重构而来，保持全部内存优化：
1. 进度记录 JSONL 追加写（O(1) 内存）；
2. 有界队列 + 固定 worker 池；
3. 流式写盘（client.stream）；
4. 指数退避重试（只重试可恢复错误）。

解耦要点：
    - 配置全部来自 ``Config`` 对象，不再读模块级全局变量；
    - 进度通过 ``on_event(event_dict)`` 回调输出统一 ProgressEvent（无 task_id，
      由 TaskManager 补齐），调用方是 GUI / HTTP API 还是脚本都无所谓；
    - 下载鉴权失效（405 / JSON 错误体）会立即中止整批并抛 ``AuthError``，
      而不是把全部瓦片逐个打成"无效 PNG"。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import aiofiles
import httpx

from .config import Config
from .events import (
    EV_DONE,
    EV_PROGRESS,
    STAGE_DOWNLOAD,
    CancelToken,
    make_event,
)
from .paths import ensure_tile_dir, find_tile, legacy_tile_path, tile_path

# 可重试的 HTTP 状态码（其余 4xx 视为不可恢复，直接失败）
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

PROGRESS_FLUSH_EVERY = 500   # 每完成多少片 flush 一次进度日志
QUEUE_FACTOR = 2             # 任务队列长度 = 并发数 * QUEUE_FACTOR


class AuthError(RuntimeError):
    """数据源鉴权失效（Cookie 过期 / 权限不足）。"""


def _is_retryable(exc, status=None):
    """判断错误是否值得重试"""
    if status is not None:
        return status in RETRYABLE_STATUS
    if isinstance(exc, ValueError):  # 内容无效（如坏 PNG）
        return True
    return isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError))


class AsyncTileDownloader:
    """异步瓦片下载器（内存优化版，UI 无关）。"""

    PNG_HEADER = b"\x89PNG\r\n\x1a\n"

    def __init__(
        self,
        config: Config,
        on_event=None,
        cancel_event: asyncio.Event | None = None,
    ):
        self.config = config
        self.on_event = on_event
        self.cancel_event = cancel_event  # threading.Event，外部可 set() 请求停止
        self.max_workers = config.max_workers
        self.output_dir = config.output_dir

        self.semaphore = asyncio.Semaphore(max(1, self.max_workers))
        self.success_count = 0
        self.fail_count = 0
        self.skip_count = 0
        self.invalid_count = 0
        self.retried_count = 0
        self.failed_tasks: list[str] = []       # 文本格式（兼容 download_failed.txt）
        self.failed_coords: list[tuple[int, int]] = []
        self.auth_error: AuthError | None = None

        self._progress_fh = None
        self._since_flush = 0
        self._created_dirs: set[tuple[int, int]] = set()
        self._start_time = 0.0

    # ---------- 事件 ----------
    def _emit(self, **fields) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(make_event(EV_PROGRESS, stage=STAGE_DOWNLOAD, **fields))
        except Exception:
            pass  # 进度回调异常不阻断下载

    def _counters(self) -> dict:
        return {
            "success": self.success_count,
            "fail": self.fail_count,
            "skip": self.skip_count,
            "invalid": self.invalid_count,
            "retried": self.retried_count,
        }

    def _speed_eta(self, done: int, total: int) -> tuple[float, float]:
        elapsed = time.time() - self._start_time if self._start_time else 0.0
        speed = done / elapsed if elapsed > 0 else 0.0
        eta = (total - done) / speed if speed > 0 and total >= done else 0.0
        return speed, eta

    # ---------- 进度日志（JSONL 追加写，O(1) 内存） ----------
    @property
    def _progress_file(self) -> Path:
        return self.config.resolve_path(self.config.progress_file)

    @property
    def _failed_file(self) -> Path:
        return self.config.resolve_path(self.config.failed_file)

    def _open_progress_log(self):
        """打开进度日志；检测到旧版 dict 格式则先归档为 .bak"""
        if self._progress_fh is not None:
            return
        pf = self._progress_file
        if pf.exists():
            try:
                with open(pf, "r", encoding="utf-8") as f:
                    first = f.read(1).strip()
                if first == "{":  # 旧版 dict 格式，先归档
                    os.replace(pf, str(pf) + ".bak")
            except OSError:
                pass
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        self._progress_fh = open(pf, "a", encoding="utf-8", buffering=65536)
        self._since_flush = 0

    def _log_done(self, filename):
        """记录一片完成（追加一行，不占内存）"""
        if self._progress_fh is None:
            self._open_progress_log()
        self._progress_fh.write(
            json.dumps({"f": filename}, ensure_ascii=False) + "\n")
        self._since_flush += 1
        if self._since_flush >= PROGRESS_FLUSH_EVERY:
            self._progress_fh.flush()
            self._since_flush = 0

    def _close_progress_log(self, clean=False):
        """关闭进度日志；clean=True 且无失败时清理日志文件"""
        if self._progress_fh is not None:
            self._progress_fh.flush()
            self._progress_fh.close()
            self._progress_fh = None
            if clean and self._progress_file.exists():
                try:
                    os.remove(self._progress_file)
                except OSError:
                    pass

    def _save_failed_tasks(self):
        ff = self._failed_file
        ff.parent.mkdir(parents=True, exist_ok=True)
        with open(ff, "w", encoding="utf-8") as f:
            for task in self.failed_tasks:
                f.write(f"{task}\n")

    def _is_valid_png(self, filepath):
        """验证文件是否是有效的PNG图片（通过文件头）"""
        try:
            with open(filepath, "rb") as f:
                return f.read(8) == self.PNG_HEADER
        except OSError:
            return False

    @staticmethod
    def _looks_like_auth_error(filepath) -> bool:
        """文件头是 JSON 错误体（如 {"error":"操作权限不足"}）→ 鉴权失效"""
        try:
            with open(filepath, "rb") as f:
                head = f.read(64).lstrip()
            return head.startswith(b"{")
        except OSError:
            return False

    # ---------- 单个瓦片下载 ----------
    async def download_tile(self, client, tile_matrix, tile_col, tile_row, max_retries=3):
        """异步下载单个瓦片（流式写盘 + 智能重试 + 文件验证）"""
        if self.cancel_event is not None and self.cancel_event.is_set():
            return "cancelled"

        async with self.semaphore:
            if self.cancel_event is not None and self.cancel_event.is_set():
                return "cancelled"

            url = self.config.tile_url(tile_matrix, tile_col, tile_row)

            # 断点续传：新/旧结构任一已存在且有效则跳过
            existing = find_tile(tile_matrix, tile_col, tile_row, self.output_dir)
            if existing is not None:
                if self._is_valid_png(existing):
                    self.skip_count += 1
                    return "skipped"
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

            dir_key = (tile_matrix, tile_row)
            if dir_key not in self._created_dirs:
                ensure_tile_dir(tile_matrix, tile_row, self.output_dir)
                self._created_dirs.add(dir_key)
            filepath = tile_path(tile_matrix, tile_col, tile_row, self.output_dir)
            filename = f"{tile_matrix}_{tile_col}_{tile_row}"

            last_exc = None
            for attempt in range(max_retries):
                try:
                    await self._download_to_file(client, url, filepath)

                    if not self._is_valid_png(filepath):
                        if self._looks_like_auth_error(filepath):
                            # 鉴权失效：立即中止整批，不要把几十万片全打成无效
                            try:
                                os.remove(filepath)
                            except OSError:
                                pass
                            self.auth_error = AuthError(
                                "数据源鉴权失效（Cookie 过期或权限不足），已中止下载")
                            self.cancel_event.set()
                            return "failed"
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        self.invalid_count += 1
                        raise ValueError("下载内容不是有效的PNG图片")

                    self.success_count += 1
                    self._log_done(filename)
                    return "success"

                except Exception as e:
                    last_exc = e
                    resp = getattr(e, "response", None)
                    status = getattr(resp, "status_code", None)
                    if status in (401, 403, 405):
                        self.auth_error = AuthError(
                            f"数据源鉴权失效（HTTP {status}，Cookie 过期或权限不足），已中止下载")
                        self.cancel_event.set()
                        return "failed"
                    if not _is_retryable(e, status):
                        break
                    if attempt < max_retries - 1:
                        if status in (429, 503):
                            delay = min(1.0 * (2 ** attempt), 8)
                        else:
                            delay = min(0.5 * (2 ** attempt), 4)
                        self.retried_count += 1
                        await asyncio.sleep(delay)
                    else:
                        break

            self.fail_count += 1
            self.failed_tasks.append(f"[{tile_col},{tile_row}] {last_exc}")
            self.failed_coords.append((tile_col, tile_row))
            return "failed"

    async def _download_to_file(self, client, url, filepath):
        """流式下载：分块写入，不把整片数据缓存在内存"""
        timeout = httpx.Timeout(self.config.timeout)
        async with client.stream("GET", url, timeout=timeout) as resp:
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            async with aiofiles.open(filepath, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    await f.write(chunk)

    # ---------- 批量下载（有界队列 + 固定 worker 池） ----------
    async def run_tasks(self, client, tasks, total, desc="下载进度", emit=True):
        """执行任务列表（惰性生成，内存 O(并发数)）。"""
        q = asyncio.Queue(maxsize=max(1, self.max_workers * QUEUE_FACTOR))
        done = 0
        last = [None, None, ""]
        self._start_time = self._start_time or time.time()

        def on_done(result, col, row):
            nonlocal done
            done += 1
            last[:] = [col, row, result]
            if emit and self.on_event:
                speed, eta = self._speed_eta(done, total)
                self._emit(
                    done=done, total=total,
                    percent=(done / total * 100) if total else 0.0,
                    counters=self._counters(),
                    speed=speed, eta=eta,
                    tile={"col": col, "row": row, "result": result},
                    cancelled=self.cancel_event.is_set(),
                    finished=False,
                    message=f"{desc} {done}/{total}",
                )

        async def worker():
            while True:
                if self.cancel_event.is_set() or self.auth_error:
                    break
                item = await q.get()
                if item is None:
                    break
                matrix, col, row = item
                result = await self.download_tile(client, matrix, col, row)
                on_done(result, col, row)

        workers = [asyncio.create_task(worker()) for _ in range(self.max_workers)]

        try:
            for matrix, col, row in tasks:
                if self.cancel_event.is_set() or self.auth_error:
                    break
                try:
                    await asyncio.wait_for(q.put((matrix, col, row)), timeout=0.5)
                except asyncio.TimeoutError:
                    if self.cancel_event.is_set() or self.auth_error:
                        break
                    continue
            if not self.cancel_event.is_set() and not self.auth_error:
                for _ in workers:
                    await q.put(None)
        finally:
            if self.cancel_event.is_set() or self.auth_error:
                for w in workers:
                    w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        speed, eta = self._speed_eta(done, total)
        if emit and self.on_event:
            self._emit(
                done=done, total=total,
                percent=(done / total * 100) if total else 0.0,
                counters=self._counters(),
                speed=speed, eta=eta,
                tile={"col": last[0], "row": last[1],
                      "result": "cancelled" if self.cancel_event.is_set() else last[2]},
                cancelled=self.cancel_event.is_set(),
                finished=True,
                message=f"{desc} 结束 {done}/{total}",
            )
        return done

    async def download_batch(self, tile_matrix, col_start, col_end, row_start, row_end):
        """批量下载瓦片。返回统计 dict；鉴权失效抛 AuthError。"""
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        cols = col_end - col_start + 1
        rows = row_end - row_start + 1
        total = cols * rows

        def gen_tasks():
            for col in range(col_start, col_end + 1):
                for row in range(row_start, row_end + 1):
                    yield tile_matrix, col, row

        limits = httpx.Limits(
            max_connections=self.max_workers * 2,
            max_keepalive_connections=self.max_workers,
        )
        async with httpx.AsyncClient(
            headers=self.config.headers,
            limits=limits,
            http2=True,
            timeout=httpx.Timeout(self.config.timeout),
        ) as client:
            self._start_time = time.time()
            done = await self.run_tasks(client, gen_tasks(), total, desc="下载进度")

        self._close_progress_log(clean=self.fail_count == 0 and not self.auth_error)
        if self.failed_tasks and not self.auth_error:
            self._save_failed_tasks()

        elapsed = time.time() - self._start_time
        stats = {
            "done": done,
            "total": total,
            "elapsed": elapsed,
            "speed": done / elapsed if elapsed > 0 else 0.0,
            **self._counters(),
        }
        if self.auth_error:
            raise self.auth_error
        return stats

    # ---------- 失败重试 ----------
    def load_failed_coords(self) -> list[tuple[int, int]]:
        """从 download_failed.txt 解析失败瓦片坐标（格式: [col,row] 错误信息）。"""
        ff = self._failed_file
        if not ff.exists():
            return []
        coords: list[tuple[int, int]] = []
        with open(ff, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("["):
                    continue
                try:
                    coord_part = line.split("]")[0][1:]
                    col_s, row_s = coord_part.split(",")
                    coords.append((int(col_s), int(row_s)))
                except (ValueError, IndexError):
                    continue
        return coords

    async def retry_failed(self, tile_matrix=None) -> dict:
        """重试失败列表里的瓦片；成功项从失败文件中移除。"""
        retry_tasks = self.load_failed_coords()
        if not retry_tasks:
            return {"retried": 0, "success": 0, "fail": 0, "total": 0}

        matrix = tile_matrix if tile_matrix is not None else self.config.tile_matrix
        total = len(retry_tasks)
        limits = httpx.Limits(
            max_connections=self.max_workers * 2,
            max_keepalive_connections=self.max_workers,
        )
        async with httpx.AsyncClient(
            headers=self.config.headers,
            limits=limits,
            http2=True,
            timeout=httpx.Timeout(self.config.timeout),
        ) as client:
            self._start_time = time.time()
            await self.run_tasks(client, iter(retry_tasks), total,
                                 desc="重试进度", emit=True)

        # 成功的不再保留
        still_failed = [
            (c, r) for (c, r) in self.failed_coords
        ]
        if still_failed:
            self._failed_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._failed_file, "w", encoding="utf-8") as f:
                for task in self.failed_tasks:
                    f.write(f"{task}\n")
        elif self._failed_file.exists():
            try:
                os.remove(self._failed_file)
            except OSError:
                pass

        elapsed = time.time() - self._start_time
        return {
            "retried": total,
            "total": total,
            "success": self.success_count,
            "fail": self.fail_count,
            "skip": self.skip_count,
            "elapsed": elapsed,
            **self._counters(),
        }
