"""
wmts.api — HTTP API（FastAPI：REST + SSE）。

把 wmts.core / wmts.tasks 封装为一套可编程接口：
    - REST：配置 / 图层元数据 / 网格换算 / 瓦片状态与预览 / 任务 / 缓存 / 输出 / 日志
    - SSE：任务进度流（/api/tasks/{id}/events）与日志流（/api/logs/stream）
    - 静态托管 wmts/web（Web 前端）

交互约定：
    - 同一时刻只允许一个任务运行，占用时 POST /api/tasks 返回 409；
    - 进度推送为单向 SSE；事件带 seq 序号，SSE 先回放缓冲再接实时流（不丢不重）；
    - 上游数据源的 Cookie 只保存在服务端，浏览器永远拿不到真实值。

启动：``uv run python -m wmts.server``（见 wmts/server.py）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterator

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core.cache import (
    cache_stats,
    clear_cache,
    migrate,
    prune,
    range_status,
    verify,
)
from .core.config import ROOT, Config
from .core.downloader import AuthError
from .core.events import EV_DONE, EV_LOG
from .core.grid import (
    bbox_to_range,
    estimate_size,
    layer_range,
    range_size,
    range_to_bbox,
)
from .core.layer_meta import MetaError, fetch_layer_meta
from .core.georef import GEO_TAGS, read_head_ifd
from .core.paths import tile_path
from .core import preview as tile_preview
from .tasks import TASK_TYPES, TaskBusyError, TaskError, TaskManager

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(
    title="WMTS 瓦片工具 API",
    description="WMTS 瓦片下载 / 拼接 / 地理标签全流程的可编程接口",
    version="1.0.0",
)


class _State:
    """进程级共享状态（配置 + 任务管理器 + 元数据缓存）。"""

    def __init__(self) -> None:
        self.config: Config = Config.load()
        self.tm: TaskManager = TaskManager(self.config)
        self._meta: Any = None

    def reset_meta(self) -> None:
        self._meta = None


_state = _State()


@app.on_event("startup")
def _startup() -> None:
    # 重新加载配置（便于测试后重置）
    _state.config = Config.load()
    _state.tm = TaskManager(_state.config)


# ================= 基础 =================

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "time": time.time()}


# ================= 配置 =================

@app.get("/api/config")
def get_config() -> dict:
    return _state.config.to_public_dict()


@app.put("/api/config")
def put_config(update: dict = Body(...)) -> dict:
    """部分更新配置；保存到 config.json。Cookie 被打码回传时不会覆盖真实值。"""
    cfg = _state.config
    from dataclasses import fields as dc_fields

    known = {f.name for f in dc_fields(Config)}
    headers = update.pop("headers", None)
    for k, v in update.items():
        if k in known:
            setattr(cfg, k, v)
    if isinstance(headers, dict):
        headers = {k: v for k, v in headers.items() if not k.startswith("_")}
        cookie = headers.get("Cookie")
        if cookie and "…" in cookie:
            headers.pop("Cookie", None)  # 打码值不覆盖真实 Cookie
        cfg.headers.update(headers)
    cfg.sanitize()
    errs = cfg.validate_download_range()
    path = cfg.save()
    return {"ok": True, "saved_to": str(path), "errors": errs,
            "config": cfg.to_public_dict()}


@app.post("/api/config/reset")
def reset_config() -> dict:
    """恢复为 config.py 的默认值并保存。"""
    cfg = Config.defaults()
    cfg.save()
    _state.config = cfg
    # 已有任务管理器引用旧配置对象 → 同步替换字段值
    _state.tm.config = cfg
    return {"ok": True, "config": cfg.to_public_dict()}


# ================= 图层元数据 / 网格换算 =================

@app.get("/api/layer/meta")
def get_layer_meta(refresh: bool = Query(False, description="强制重新拉取")) -> dict:
    try:
        meta = fetch_layer_meta(_state.config, refresh=refresh)
    except MetaError as e:
        raise HTTPException(502, str(e))
    d = meta.to_dict()
    lr = layer_range(meta, _state.config.tile_matrix)
    cols, rows, total = range_size(*lr)
    d["layer_range_at_current_level"] = {
        "col_start": lr[0], "col_end": lr[1], "row_start": lr[2], "row_end": lr[3],
        "cols": cols, "rows": rows, "total": total,
    }
    return d


@app.get("/api/grid")
def get_grid() -> dict:
    """当前下载范围信息 + 图层元数据概览。"""
    cfg = _state.config
    out: dict[str, Any] = {
        "matrix": cfg.tile_matrix,
        "col_start": cfg.col_start, "col_end": cfg.col_end,
        "row_start": cfg.row_start, "row_end": cfg.row_end,
        "cols": cfg.cols, "rows": cfg.rows, "total": cfg.total,
        "estimated_bytes": estimate_size(cfg.total),
        "errors": cfg.validate_download_range(),
    }
    try:
        meta = fetch_layer_meta(cfg)
        lr = layer_range(meta, cfg.tile_matrix)
        cols, rows, total = range_size(*lr)
        out["layer"] = {
            "name": meta.layer,
            "bbox": list(meta.bbox),
            "start_level": meta.start_level, "end_level": meta.end_level,
            "resolution": meta.resolution(cfg.tile_matrix),
            "layer_range": {"col_start": lr[0], "col_end": lr[1],
                            "row_start": lr[2], "row_end": lr[3],
                            "cols": cols, "rows": rows, "total": total},
        }
    except MetaError as e:
        out["layer"] = {"error": str(e)}
    return out


class BBoxRequest(BaseModel):
    bbox: tuple[float, float, float, float] = Field(
        ..., description="地理范围 (xmin, ymin, xmax, ymax)")
    matrix: int | None = Field(None, description="目标级别（默认当前配置）")


@app.post("/api/grid/from-bbox")
def grid_from_bbox(req: BBoxRequest) -> dict:
    """地理范围 → 瓦片范围（区域选择下载的核心换算）。"""
    cfg = _state.config
    matrix = req.matrix if req.matrix is not None else cfg.tile_matrix
    try:
        meta = fetch_layer_meta(cfg)
    except MetaError as e:
        raise HTTPException(502, str(e))
    if not (meta.start_level <= matrix <= meta.end_level):
        raise HTTPException(400, f"级别需在 {meta.start_level}-{meta.end_level} 之间")
    try:
        cs, ce, rs, re_ = bbox_to_range(meta, matrix, req.bbox, clamp_to_layer=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    cols, rows, total = range_size(cs, ce, rs, re_)
    return {
        "matrix": matrix,
        "col_start": cs, "col_end": ce, "row_start": rs, "row_end": re_,
        "cols": cols, "rows": rows, "total": total,
        "bbox": list(range_to_bbox(meta, matrix, cs, ce, rs, re_)),
        "estimated_bytes": estimate_size(total),
    }


# ================= 瓦片状态 / 预览 =================

@app.get("/api/tiles/status")
def tiles_status(
    matrix: int | None = None,
    col_start: int | None = None, col_end: int | None = None,
    row_start: int | None = None, row_end: int | None = None,
    layer: str | None = None,
) -> dict:
    """范围内瓦片状态位图（2 bit/片，base64），供前端状态网格渲染。"""
    cfg = _state.config
    matrix = matrix if matrix is not None else cfg.tile_matrix
    cs = col_start if col_start is not None else cfg.col_start
    ce = col_end if col_end is not None else cfg.col_end
    rs = row_start if row_start is not None else cfg.row_start
    re_ = row_end if row_end is not None else cfg.row_end
    if ce < cs or re_ < rs:
        raise HTTPException(400, "范围不合法（结束值小于起始值）")
    if (ce - cs + 1) * (re_ - rs + 1) > 4_000_000:
        raise HTTPException(400, "范围过大")
    counts, packed = range_status(str(cfg.resolve_path(cfg.output_dir)),
                                  matrix, cs, ce, rs, re_,
                                  layer=layer or cfg.layer)
    return {
        "matrix": matrix, "col_start": cs, "col_end": ce,
        "row_start": rs, "row_end": re_, "layer": layer or cfg.layer,
        "cols": ce - cs + 1, "rows": re_ - rs + 1,
        "counts": counts, "status_b64": packed,
    }


@app.get("/api/tiles/{matrix}/{col}/{row}")
def get_tile(matrix: int, col: int, row: int,
             source: str = Query("auto", description="auto|local|remote")) -> Response:
    """单瓦片预览（auto=本地优先缺失联网；Cookie 只在服务端使用）。"""
    cfg = _state.config
    try:
        content, used = tile_preview.get_tile(cfg, matrix, col, row, source=source)
    except FileNotFoundError:
        raise HTTPException(404, "本地不存在该瓦片")
    except AuthError as e:
        raise HTTPException(502, str(e))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(502, str(e))
    return Response(content=content, media_type="image/png",
                    headers={"X-Tile-Source": used, "Cache-Control": "no-store"})


@app.post("/api/tiles/{matrix}/{col}/{row}/save")
def save_tile(matrix: int, col: int, row: int,
              source: str = Query("remote")) -> dict:
    """把预览瓦片保存到缓存（默认从上游拉取后落盘）。"""
    cfg = _state.config
    try:
        if source == "remote":
            content = tile_preview.fetch_tile_remote(cfg, matrix, col, row)
            path = tile_preview.save_tile(cfg, matrix, col, row, content)
        else:
            path = cfg.resolve_path(tile_path(matrix, col, row,
                                              str(cfg.resolve_path(cfg.output_dir)),
                                              layer=cfg.layer))
            if not Path(path).exists():
                raise HTTPException(404, "本地不存在该瓦片")
    except AuthError as e:
        raise HTTPException(502, str(e))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "path": str(path), "size": os.path.getsize(path)}


# ================= 断点续传状态 =================

@app.get("/api/download/progress")
def download_progress() -> dict:
    """断点续传状态：进度日志行数 + 失败列表。"""
    cfg = _state.config
    pf = cfg.resolve_path(cfg.progress_file)
    ff = cfg.resolve_path(cfg.failed_file)
    out: dict[str, Any] = {
        "progress_file": str(pf) if pf.exists() else None,
        "failed_file": str(ff) if ff.exists() else None,
        "done_lines": 0,
        "failed": [],
    }
    if pf.exists():
        try:
            with open(pf, "r", encoding="utf-8") as f:
                out["done_lines"] = sum(1 for line in f if line.strip())
        except OSError:
            pass
    if ff.exists():
        try:
            with open(ff, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            out["failed_count"] = len(lines)
            out["failed"] = lines[:500]
        except OSError:
            pass
    return out


# ================= 任务 =================

class TaskStart(BaseModel):
    type: str = Field(..., description="download | merge | geo | pipeline | retry_failed")
    params: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/tasks", status_code=202)
def start_task(req: TaskStart) -> dict:
    if req.type not in TASK_TYPES:
        raise HTTPException(400, f"未知任务类型: {req.type}（可选: {', '.join(TASK_TYPES)}）")
    try:
        task = _state.tm.start(req.type, req.params)
    except TaskBusyError as e:
        raise HTTPException(409, str(e))
    except TaskError as e:
        raise HTTPException(400, str(e))
    return task


@app.get("/api/tasks")
def list_tasks() -> dict:
    return _state.tm.snapshot()


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    task = _state.tm.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task.to_dict(include_progress=True)


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict:
    task = _state.tm.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.finished:
        return {"ok": False, "message": "任务已结束"}
    ok = _state.tm.cancel()
    return {"ok": ok, "message": "已请求取消" if ok else "取消失败"}


def _sse_response(gen, status_code: int = 200) -> StreamingResponse:
    return StreamingResponse(
        gen,
        status_code=status_code,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _format_sse(ev: dict[str, Any]) -> str:
    seq = ev.get("seq")
    sid = f"id: {seq}\n" if seq is not None else ""
    return f"{sid}data: {json.dumps(ev, ensure_ascii=False)}\n\n"


async def _task_event_stream(task_id: str, request: Request):
    """任务事件流：先回放缓冲，再接实时流，任务结束（done/failed/cancelled）后关闭。"""
    task = _state.tm.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    sid, q = _state.tm.events.subscribe()
    last_seq = 0
    try:
        # 1) 回放已缓冲事件
        for ev in list(task.events):
            last_seq = max(last_seq, ev.get("seq", 0))
            yield _format_sse(ev)
        # 2) 实时流
        while True:
            if await request.is_disconnected():
                break
            try:
                ev = await asyncio.wait_for(
                    asyncio.to_thread(q.get, True, 1.0), timeout=15.0)
            except (asyncio.TimeoutError, TimeoutError):
                yield ": ping\n\n"
                continue
            if ev.get("task_id") != task_id:
                continue
            if ev.get("seq", 0) <= last_seq:
                continue
            last_seq = ev.get("seq", last_seq)
            yield _format_sse(ev)
            if ev.get("type") == EV_DONE:
                break
    finally:
        _state.tm.events.unsubscribe(sid)


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request):
    return _sse_response(_task_event_stream(task_id, request))


async def _log_stream(request: Request):
    sid, q = _state.tm.events.subscribe()
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                ev = await asyncio.wait_for(
                    asyncio.to_thread(q.get, True, 1.0), timeout=15.0)
            except (asyncio.TimeoutError, TimeoutError):
                yield ": ping\n\n"
                continue
            if ev.get("type") != EV_LOG:
                continue
            yield _format_sse(ev)
    finally:
        _state.tm.events.unsubscribe(sid)


@app.get("/api/logs")
def get_logs(limit: int = 200) -> dict:
    return {"logs": _state.tm.events.log_history(limit)}


@app.get("/api/logs/stream")
async def logs_stream(request: Request):
    return _sse_response(_log_stream(request))


# ================= 缓存管理 =================

@app.get("/api/cache/stats")
def cache_statistics() -> dict:
    base = str(_state.config.resolve_path(_state.config.output_dir))
    total, size, per_layer = cache_stats(base)
    per_out = {label: {str(m): v for m, v in mats.items()}
               for label, mats in per_layer.items()}
    return {"base": base, "total_tiles": total, "total_bytes": size,
            "current_layer": _state.config.layer, "per_layer": per_out}


class CachePruneRequest(BaseModel):
    matrix: int | None = None
    col_start: int | None = None
    col_end: int | None = None
    row_start: int | None = None
    row_end: int | None = None
    older_than: int | None = Field(None, description="删除 N 天前的瓦片")
    max_size: int | None = Field(None, description="删最旧直到 ≤ N MB")
    layer: str | None = Field(None, description="只清理该图层（默认当前图层；all=全部）")


@app.post("/api/cache/prune")
def cache_prune(req: CachePruneRequest) -> dict:
    base = str(_state.config.resolve_path(_state.config.output_dir))
    if req.layer == "all":
        layer = None
    elif req.layer:
        layer = req.layer
    else:
        layer = _state.config.layer
    try:
        removed, freed = prune(base, layer=layer, matrix=req.matrix,
                               col_start=req.col_start, col_end=req.col_end,
                               row_start=req.row_start, row_end=req.row_end,
                               older_than=req.older_than, max_size=req.max_size)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "removed": removed, "freed_bytes": freed,
            "layer": layer or "all"}


@app.post("/api/cache/migrate")
def cache_migrate(dry_run: bool = False, layer: str | None = None) -> dict:
    """把旧结构（v1 扁平 / v2 无图层）迁移到按图层分层结构。"""
    base = str(_state.config.resolve_path(_state.config.output_dir))
    target_layer = layer or _state.config.layer
    try:
        moved, skipped, left = migrate(base, target_layer, dry_run=dry_run)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "layer": target_layer, "moved": moved,
            "skipped": skipped, "legacy_left": left, "dry_run": dry_run}


@app.post("/api/cache/verify")
def cache_verify(all_layers: bool = False, layer: str | None = None) -> dict:
    base = str(_state.config.resolve_path(_state.config.output_dir))
    scope = None if all_layers else (layer or _state.config.layer)
    total, bad = verify(base, layer=scope)
    return {"ok": True, "layer": scope or "all", "total": total,
            "bad_count": len(bad), "bad": bad[:100]}


class CacheClearRequest(BaseModel):
    confirm: bool = Field(..., description="必须显式传 true")


@app.post("/api/cache/clear")
def cache_clear(req: CacheClearRequest) -> dict:
    if not req.confirm:
        raise HTTPException(400, "危险操作：需要 confirm=true 显式确认")
    base = str(_state.config.resolve_path(_state.config.output_dir))
    removed, freed = clear_cache(base)
    return {"ok": True, "removed": removed, "freed_bytes": freed}


# ================= 输出文件 =================

def _list_output_files() -> list[Path]:
    exts = (".tif", ".png")
    files = []
    for p in ROOT.iterdir():
        if p.is_file() and p.suffix.lower() in exts:
            files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _has_geo_tags(path: Path) -> bool | None:
    try:
        with open(path, "rb") as f:
            _, entries = read_head_ifd(f)
        return any(t in GEO_TAGS for t, *_ in entries)
    except Exception:
        return None


@app.get("/api/outputs")
def list_outputs() -> dict:
    items = []
    for p in _list_output_files():
        st = p.stat()
        items.append({
            "name": p.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "geo_attached": _has_geo_tags(p),
        })
    return {"outputs": items}


_NAME_RE = re.compile(r"^[\w.\-]+$")


@app.get("/api/outputs/{name}")
def download_output(name: str, request: Request) -> Response:
    """下载输出文件（支持 Range，分块流式，可断点续传）。"""
    if not _NAME_RE.match(name) or ".." in name:
        raise HTTPException(400, "非法文件名")
    path = ROOT / name
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    size = path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
        if m and (m.group(1) or m.group(2)):
            start_s, end_s = m.group(1), m.group(2)
            if start_s == "":
                # 后缀范围：最后 N 字节
                length = int(end_s)
                start = max(0, size - length)
                end = size - 1
            else:
                start = int(start_s)
                end = min(int(end_s), size - 1) if end_s else size - 1
            if start > end or start >= size:
                return Response(status_code=416,
                                headers={"Content-Range": f"bytes */{size}"})
            length = end - start + 1

            def file_iter(p: Path, s: int, n: int) -> Iterator[bytes]:
                with open(p, "rb") as f:
                    f.seek(s)
                    remaining = n
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            return StreamingResponse(
                file_iter(path, start, length), status_code=206,
                media_type="application/octet-stream",
                headers={
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Content-Length": str(length),
                    "Accept-Ranges": "bytes",
                })

    def full_iter(p: Path) -> Iterator[bytes]:
        with open(p, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        full_iter(path), media_type="application/octet-stream",
        headers={"Content-Length": str(size), "Accept-Ranges": "bytes"})


# ================= Web 前端（静态托管，须在 API 路由之后挂载） =================

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
