"""
wmts — WMTS 瓦片下载与拼接工具包（核心逻辑，UI 无关）。

分层：
    wmts.core       纯业务逻辑（配置 / 下载 / 合并 / 地理标签 / 缓存 / 预览 / 事件）
    wmts.tasks      任务编排（TaskManager：流水线、取消、状态机）
    wmts.api        HTTP API（FastAPI：REST + SSE）
    wmts.web        Web 前端静态资源（由 wmts.api 托管）
"""

from .core.config import Config, ROOT
from .core.events import CancelToken, EventBus, make_event
from .core.layer_meta import LayerMeta, MetaError, fetch_layer_meta

__all__ = [
    "Config",
    "ROOT",
    "CancelToken",
    "EventBus",
    "make_event",
    "LayerMeta",
    "MetaError",
    "fetch_layer_meta",
]
