"""
wmts.tasks — 任务编排层：下载 / 拼接 / 地理标签 / 流水线的统一管理。

职责：
    - 串行执行任务（同一时刻最多一个，占用时拒绝新任务）；
    - 取消（CancelToken 贯穿下载器与合并子进程）；
    - 把各阶段事件补上 task_id 后经 EventBus 广播；
    - 维护任务历史与最新进度快照。

调用方式（进程内，GUI 与 HTTP API 共用）::

    tm = TaskManager(Config.load())
    task = tm.start("pipeline")            # download|merge|geo|pipeline|retry_failed
    sid, q = tm.events.subscribe()         # 消费统一 ProgressEvent
    tm.cancel()
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .core.config import Config
from .core.downloader import AsyncTileDownloader, AuthError
from .core.events import (
    ST_CANCELLED as CANCELLED,
    ST_DONE as DONE,
    ST_FAILED as FAILED,
    ST_RUNNING as RUNNING,
    EV_DONE,
    EV_ERROR,
    EV_LOG,
    EV_PROGRESS,
    EV_STATE,
    STAGE_DOWNLOAD,
    STAGE_GEO,
    STAGE_MERGE,
    STAGE_PIPELINE,
    CancelToken,
    EventBus,
    make_event,
)
from .core.georef import GeoRefError, attach_geo_for_config
from .core.layer_meta import MetaError, fetch_layer_meta
from .core.merger import MergeError, merge_tiles

# 流水线各阶段在总进度中的权重
PIPELINE_WEIGHTS = {STAGE_DOWNLOAD: 50.0, STAGE_MERGE: 40.0, STAGE_GEO: 10.0}

TASK_TYPES = ("download", "merge", "geo", "pipeline", "retry_failed")


class TaskError(RuntimeError):
    """任务启动/执行错误。"""


class TaskBusyError(TaskError):
    """已有任务在运行。"""


@dataclass
class Task:
    id: str
    type: str
    state: str = "pending"
    params: dict[str, Any] = field(default_factory=dict)
    stage: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)  # stage -> 最近一次进度
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    # 事件回放缓冲（SSE 订阅前发出的事件不丢失），带全局自增序号用于去重
    events: "deque[dict[str, Any]]" = field(
        default_factory=lambda: deque(maxlen=5000))
    last_seq: int = 0

    @property
    def finished(self) -> bool:
        return self.state in (DONE, FAILED, CANCELLED)

    def to_dict(self, include_progress: bool = False) -> dict[str, Any]:
        d = {
            "id": self.id,
            "type": self.type,
            "state": self.state,
            "stage": self.stage,
            "params": self.params,
            "error": self.error,
            "result": self.result,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if include_progress:
            d["progress"] = self.progress
        return d


class TaskManager:
    """串行任务管理器（线程安全）。"""

    def __init__(self, config: Config, max_history: int = 50):
        self.config = config
        self.events = EventBus()
        self._tasks: list[Task] = []
        self._max_history = max_history
        self._lock = threading.RLock()
        self._counter = itertools.count(1)
        self._seq = itertools.count(1)
        self._thread: threading.Thread | None = None
        self._token: CancelToken | None = None

    # ---------- 查询 ----------
    def current(self) -> Task | None:
        with self._lock:
            for t in reversed(self._tasks):
                if not t.finished:
                    return t
            return None

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return next((t for t in self._tasks if t.id == task_id), None)

    def history(self) -> list[Task]:
        with self._lock:
            return list(self._tasks)

    def snapshot(self) -> dict[str, Any]:
        cur = self.current()
        return {
            "current": cur.to_dict(include_progress=True) if cur else None,
            "history": [t.to_dict() for t in self.history()[-10:]],
            "config_errors": self.config.validate_download_range(),
        }

    # ---------- 启动 / 取消 ----------
    def start(self, type_: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if type_ not in TASK_TYPES:
            raise TaskError(f"未知任务类型: {type_}（可选: {', '.join(TASK_TYPES)}）")
        with self._lock:
            cur = self.current()
            if cur is not None:
                raise TaskBusyError(
                    f"任务 {cur.id}（{cur.type}）正在运行，请先等待完成或取消")
            # 下载/流水线启动前校验范围
            if type_ in ("download", "pipeline", "retry_failed"):
                errs = self.config.validate_download_range()
                if errs:
                    raise TaskError("配置有误: " + "；".join(errs))
            task = Task(
                id=f"t_{next(self._counter):04d}",
                type=type_,
                params=dict(params or {}),
            )
            self._tasks.append(task)
            if len(self._tasks) > self._max_history:
                self._tasks = self._tasks[-self._max_history:]
            self._token = CancelToken()
            self._thread = threading.Thread(
                target=self._run, args=(task,), daemon=True, name=f"wmts-task-{task.id}")
            self._thread.start()
            return task.to_dict()

    def cancel(self, reason: str = "用户取消") -> bool:
        with self._lock:
            token = self._token
        if token is not None and not token.cancelled:
            token.cancel(reason)
            self._log(f"已请求取消: {reason}")
            return True
        return False

    # ---------- 事件 ----------
    def _publish(self, task: Task, ev: dict[str, Any]) -> None:
        ev["task_id"] = task.id
        ev["seq"] = next(self._seq)
        task.last_seq = ev["seq"]
        task.events.append(ev)
        if ev.get("type") == EV_PROGRESS and ev.get("stage"):
            task.progress[ev["stage"]] = {
                k: ev.get(k) for k in ("done", "total", "percent",
                                       "counters", "speed", "eta", "message")
            }
            task.stage = ev["stage"]
        try:
            self.events.publish(ev)
        except Exception:
            pass

    def _log(self, message: str, task: Task | None = None) -> None:
        ev = make_event(EV_LOG, stage=task.stage if task else None, message=message)
        if task is not None:
            ev["task_id"] = task.id
        try:
            self.events.publish(ev)
        except Exception:
            pass

    def _state(self, task: Task, state: str, message: str | None = None) -> None:
        task.state = state
        self._publish(task, make_event(
            EV_STATE, stage=task.stage or STAGE_PIPELINE, state=state, message=message))

    # ---------- 执行 ----------
    def _run(self, task: Task) -> None:
        task.started_at = time.time()
        self._log(f"任务 {task.id} 开始: {task.type}", task)
        self._state(task, RUNNING, f"任务 {task.type} 开始")
        try:
            if task.type == "download":
                result = self._do_download(task)
            elif task.type == "retry_failed":
                result = self._do_retry_failed(task)
            elif task.type == "merge":
                result = self._do_merge(task)
            elif task.type == "geo":
                result = self._do_geo(task)
            elif task.type == "pipeline":
                result = self._do_pipeline(task)
            else:  # pragma: no cover
                raise TaskError(f"未实现的任务类型: {task.type}")
            task.result = result
            task.state = DONE
            self._publish(task, make_event(
                EV_DONE, stage=task.stage or STAGE_PIPELINE, state=DONE,
                result=result, message=f"任务 {task.type} 完成"))
            self._state(task, DONE, f"任务 {task.type} 完成")
        except _TaskCancelled:
            task.state = CANCELLED
            task.error = "已取消"
            self._publish(task, make_event(
                EV_DONE, stage=task.stage or STAGE_PIPELINE, state=CANCELLED,
                message="任务已取消"))
            self._state(task, CANCELLED, "任务已取消")
        except (AuthError, MergeError, GeoRefError, MetaError, TaskError) as e:
            task.state = FAILED
            task.error = str(e)
            self._publish(task, make_event(
                EV_ERROR, stage=task.stage or STAGE_PIPELINE, state=FAILED,
                message=str(e)))
            self._publish(task, make_event(
                EV_DONE, stage=task.stage or STAGE_PIPELINE, state=FAILED,
                message=str(e)))
            self._state(task, FAILED, str(e))
        except Exception as e:  # noqa: BLE001 - 兜底，避免线程静默死亡
            task.state = FAILED
            task.error = f"{type(e).__name__}: {e}"
            self._publish(task, make_event(
                EV_ERROR, stage=task.stage or STAGE_PIPELINE, state=FAILED,
                message=task.error))
            self._publish(task, make_event(
                EV_DONE, stage=task.stage or STAGE_PIPELINE, state=FAILED,
                message=task.error))
            self._state(task, FAILED, task.error)
        finally:
            task.finished_at = time.time()
            with self._lock:
                self._token = None

    def _check_cancel(self) -> None:
        token = self._token
        if token is not None and token.cancelled:
            raise _TaskCancelled()

    # ---- 各阶段实现 ----
    def _do_download(self, task: Task) -> dict[str, Any]:
        cfg = self.config
        token = self._token
        assert token is not None

        def on_event(ev: dict[str, Any]) -> None:
            self._publish(task, ev)
            # 流水线总进度 = 下载 50%
            if ev.get("stage") == STAGE_DOWNLOAD and ev.get("percent") is not None:
                self._pipeline_overall(task, STAGE_DOWNLOAD, float(ev["percent"]))

        downloader = AsyncTileDownloader(cfg, on_event=on_event,
                                         cancel_event=token.event)
        task.stage = STAGE_DOWNLOAD
        self._state(task, RUNNING, "开始下载瓦片")
        try:
            stats = asyncio.run(downloader.download_batch(
                cfg.tile_matrix, cfg.col_start, cfg.col_end,
                cfg.row_start, cfg.row_end))
        except AuthError:
            raise
        self._check_cancel()
        self._log(
            f"下载完成: 成功 {stats.get('success', 0)}, 失败 {stats.get('fail', 0)}, "
            f"跳过 {stats.get('skip', 0)}, 耗时 {stats.get('elapsed', 0):.1f}s", task)
        return stats

    def _do_retry_failed(self, task: Task) -> dict[str, Any]:
        cfg = self.config
        token = self._token
        assert token is not None

        def on_event(ev: dict[str, Any]) -> None:
            self._publish(task, ev)

        downloader = AsyncTileDownloader(cfg, on_event=on_event,
                                         cancel_event=token.event)
        task.stage = STAGE_DOWNLOAD
        self._state(task, RUNNING, "重试失败瓦片")
        coords = downloader.load_failed_coords()
        if not coords:
            self._log("没有需要重试的失败瓦片", task)
            return {"retried": 0, "message": "没有需要重试的失败瓦片"}
        self._log(f"找到 {len(coords)} 个失败瓦片，开始重试", task)
        stats = asyncio.run(downloader.retry_failed(cfg.tile_matrix))
        self._check_cancel()
        return stats

    def _do_merge(self, task: Task) -> dict[str, Any]:
        token = self._token
        assert token is not None
        task.stage = STAGE_MERGE
        self._state(task, RUNNING, "开始拼接大图")
        self._check_cancel()

        def on_event(ev: dict[str, Any]) -> None:
            self._publish(task, ev)
            if ev.get("stage") == STAGE_MERGE and ev.get("percent") is not None:
                self._pipeline_overall(task, STAGE_MERGE, float(ev["percent"]))

        params = task.params or {}
        result = merge_tiles(
            self.config, on_event=on_event, cancel_event=token.event,
            fmt=params.get("format", "tif"),
            threads=params.get("threads"),
            level=params.get("level"),
        )
        self._check_cancel()
        if not result.ok:
            raise MergeError(result.error or "拼接失败")
        size_mb = 0.0
        from pathlib import Path
        p = Path(result.output)
        if p.exists():
            size_mb = p.stat().st_size / (1024 * 1024)
        self._log(f"拼接完成: {result.output} ({size_mb:.2f} MB, "
                  f"耗时 {result.elapsed:.1f}s, 峰值内存 {result.peak_mb:.0f} MB)", task)
        return {"output": result.output, "size_mb": size_mb,
                "elapsed": result.elapsed, "peak_mb": result.peak_mb}

    def _do_geo(self, task: Task) -> dict[str, Any]:
        token = self._token
        assert token is not None
        task.stage = STAGE_GEO
        self._state(task, RUNNING, "附加地理标签")
        self._check_cancel()

        def on_event(ev: dict[str, Any]) -> None:
            self._publish(task, ev)
            if ev.get("stage") == STAGE_GEO and ev.get("percent") is not None:
                self._pipeline_overall(task, STAGE_GEO, float(ev["percent"]))

        result = attach_geo_for_config(self.config, on_event=on_event)
        self._check_cancel()
        return result

    def _do_pipeline(self, task: Task) -> dict[str, Any]:
        """流水线：下载 → 拼接 → 地理标签（auto_geo 开启时）。"""
        task.stage = STAGE_PIPELINE
        result: dict[str, Any] = {}
        result["download"] = self._do_download(task)
        self._check_cancel()
        result["merge"] = self._do_merge(task)
        self._check_cancel()
        if self.config.auto_geo:
            result["geo"] = self._do_geo(task)
        return result

    def _pipeline_overall(self, task: Task, stage: str, stage_percent: float) -> None:
        """流水线总进度：按阶段权重加权。"""
        if task.type != "pipeline":
            return
        weights = PIPELINE_WEIGHTS
        acc = 0.0
        for s, w in weights.items():
            if s == stage:
                acc += w * stage_percent / 100.0
                break
            acc += w
        ev = make_event(
            EV_PROGRESS, stage=STAGE_PIPELINE, percent=acc,
            message=f"总进度 {acc:.1f}%（{stage} {stage_percent:.1f}%）")
        self._publish(task, ev)


class _TaskCancelled(Exception):
    """内部：任务被取消。"""
