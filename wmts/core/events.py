"""
wmts.core.events — 统一进度事件与发布/订阅总线。

所有阶段（下载 / 拼接 / 地理标签 / 缓存）都产出同一种 ProgressEvent 结构，
前端（Web 与 wx）只消费一种事件格式，与具体实现解耦。

事件结构::

    {
      "type": "progress" | "log" | "state" | "done" | "error",
      "task_id": "t_xxx",
      "stage": "download" | "merge" | "geo" | "cache" | "pipeline",
      "state": "pending" | "running" | "done" | "failed" | "cancelled",
      "done": 123, "total": 456, "percent": 27.0,
      "counters": {"success":..,"fail":..,"skip":..,"invalid":..,"retried":..},
      "speed": 12.3, "eta": 45.0,
      "message": "...",
      "tile": {"col":..,"row":..,"result":"success|failed|skipped"} | None,
      "ts": 1690000000.0,
    }

线程模型：EventBus.publish 可在任意线程调用；订阅者拿到 queue.Queue，
跨线程/跨事件循环消费（SSE 用 asyncio.to_thread 阻塞取）。
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

# ---- 阶段 / 状态常量 ----
STAGE_DOWNLOAD = "download"
STAGE_MERGE = "merge"
STAGE_GEO = "geo"
STAGE_CACHE = "cache"
STAGE_PIPELINE = "pipeline"

ST_PENDING = "pending"
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"
ST_CANCELLED = "cancelled"

FINAL_STATES = (ST_DONE, ST_FAILED, ST_CANCELLED)

# ---- 事件类型 ----
EV_PROGRESS = "progress"
EV_LOG = "log"
EV_STATE = "state"
EV_DONE = "done"
EV_ERROR = "error"


def make_event(
    type_: str,
    stage: str | None = None,
    state: str | None = None,
    task_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """构造标准事件（补齐公共字段，调用方无需关心结构细节）。"""
    ev: dict[str, Any] = {
        "type": type_,
        "task_id": task_id,
        "stage": stage,
        "state": state,
        "done": None,
        "total": None,
        "percent": None,
        "counters": None,
        "speed": None,
        "eta": None,
        "message": None,
        "tile": None,
        "result": None,
        "ts": time.time(),
    }
    for k, v in fields.items():
        if k in ev:
            ev[k] = v
    return ev


class EventBus:
    """线程安全的多订阅事件总线。

    - publish() 非阻塞：订阅者队列满时丢弃该订阅者的这条事件（进度流可容忍丢帧）；
    - subscribe() 返回 (subscriber_id, queue.Queue)；
    - 另维护一个 log 环形缓冲，供 GET /api/logs 读取历史。
    """

    def __init__(self, log_buffer_size: int = 1000, queue_size: int = 2000):
        self._subs: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._log_buffer: deque[dict[str, Any]] = deque(maxlen=log_buffer_size)
        self._queue_size = queue_size

    # ---- 订阅 ----
    def subscribe(self) -> tuple[int, "queue.Queue[dict[str, Any]]"]:
        q: queue.Queue = queue.Queue(maxsize=self._queue_size)
        sid = next(self._ids)
        with self._lock:
            self._subs[sid] = q
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

    # ---- 发布 ----
    def publish(self, event: dict[str, Any]) -> None:
        if event.get("type") == EV_LOG:
            self._log_buffer.append(event)
        with self._lock:
            subs = list(self._subs.values())
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # 订阅者消费太慢：丢掉这条进度（可容忍），并尽量腾出空间
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    # ---- 历史 ----
    def log_history(self, limit: int | None = None) -> list[dict[str, Any]]:
        items = list(self._log_buffer)
        return items[-limit:] if limit else items


# ---- 取消令牌 ----

class CancelToken:
    """协作式取消令牌（threading.Event 封装）。

    下载器在异步协程里轮询 is_set()（非阻塞），merger 用于终止子进程。
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: str | None = None

    def cancel(self, reason: str | None = None) -> None:
        if not self._event.is_set():
            self.reason = reason or self.reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def event(self) -> threading.Event:
        """底层 threading.Event（供需要原生 Event 的接口使用）。"""
        return self._event

    def is_set(self) -> bool:
        return self._event.is_set()


ProgressCallback = Callable[[dict[str, Any]], None]
