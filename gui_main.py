#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WMTS 瓦片下载与拼接工具 - wxPython 图形界面

功能：
  1. 可视化任务流程（配置参数 → 下载瓦片 → 拼接大图 → 任务完成）及整体进度
  2. 单个瓦片预览（本地文件 / 网络实时获取），并支持在范围总览网格中点击跳转
  3. 下载与拼接的实时进度、统计信息、日志输出

运行：
    uv run python gui_main.py
"""

import os
import sys
import time
import json
import subprocess
import threading
from pathlib import Path

import numpy as np
import wx

import config as cfg
import download_tiles as dt_mod
import merge_tiles as mt_mod
import tile_cache
import httpx
from tile_path import ensure_tile_dir, find_tile, iter_tiles, tile_path

APP_TITLE = "WMTS 瓦片下载与拼接工具 (wxPython)"
CONFIG_FILE = "gui_config.json"
PNG_HEADER = b"\x89PNG\r\n\x1a\n"

# ---------- 任务流程常量 ----------
STEP_NAMES = ["配置参数", "下载瓦片", "拼接大图", "任务完成"]
ST_PENDING, ST_RUNNING, ST_DONE, ST_FAILED = 0, 1, 2, 3
ST_COLORS = {
    ST_PENDING: "#9e9e9e",
    ST_RUNNING: "#1e88e5",
    ST_DONE: "#43a047",
    ST_FAILED: "#e53935",
}
ST_TEXTS = {ST_PENDING: "待命", ST_RUNNING: "进行中", ST_DONE: "完成", ST_FAILED: "失败"}


def wx_color(hex_str):
    return wx.Colour(int(hex_str[1:3], 16), int(hex_str[3:5], 16), int(hex_str[5:7], 16))


# ======================================================================
#  任务流程面板（流水线示意图 + 整体进度条）
# ======================================================================
class FlowPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent, size=(-1, 104))
        self.states = [ST_PENDING] * 4
        self.step_percent = [0.0, 0.0, 0.0, 0.0]
        self.overall = 0.0
        self.overall_text = "就绪"
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.SetBackgroundColour(wx.Colour("#fafafa"))

    def reset(self):
        self.states = [ST_PENDING] * 4
        self.step_percent = [0.0] * 4
        self.overall = 0.0
        self.overall_text = "就绪"
        self.Refresh()

    def set_step(self, idx, state, percent=None):
        self.states[idx] = state
        if percent is not None:
            self.step_percent[idx] = max(0.0, min(100.0, percent))
        self.Refresh()

    def set_overall(self, pct, text=None):
        self.overall = max(0.0, min(100.0, pct))
        if text is not None:
            self.overall_text = text
        self.Refresh()

    def _on_paint(self, evt):
        dc = wx.PaintDC(self)
        dc.SetBackground(wx.Brush("#fafafa"))
        dc.Clear()
        w, h = self.GetClientSize()
        if w <= 10:
            return

        n = len(STEP_NAMES)
        cy = 20
        radius = 10
        step_w = w / n
        centers = [(int(step_w * i + step_w / 2), cy) for i in range(n)]

        # 步骤之间的连接线
        for i in range(n - 1):
            x1, y1 = centers[i]
            x2, y2 = centers[i + 1]
            # 当前步骤已完成时连线为绿色，否则灰色
            line_color = wx_color("#43a047") if self.states[i] == ST_DONE else wx_color("#d0d0d0")
            dc.SetPen(wx.Pen(line_color, 3))
            dc.DrawLine(x1 + radius + 2, y1, x2 - radius - 2, y2)

        # 步骤圆点 + 文字
        for i, (cx, cy_) in enumerate(centers):
            color = wx_color(ST_COLORS[self.states[i]])
            dc.SetBrush(wx.Brush(color))
            dc.SetPen(wx.Pen("#ffffff", 2))
            dc.DrawCircle(cx, cy_, radius)

            # 步骤名
            name_font = wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
            dc.SetFont(name_font)
            dc.SetTextForeground(wx.Colour("#303030"))
            tw, th = dc.GetTextExtent(STEP_NAMES[i])
            dc.DrawText(STEP_NAMES[i], cx - tw // 2, cy_ + radius + 6)

            # 状态文字
            if self.states[i] == ST_RUNNING:
                st_text = f"{self.step_percent[i]:.0f}%"
            else:
                st_text = ST_TEXTS[self.states[i]]
            dc.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            dc.SetTextForeground(color)
            tw2, th2 = dc.GetTextExtent(st_text)
            dc.DrawText(st_text, cx - tw2 // 2, cy_ + radius + 22)

        # 整体进度条
        bar_y = 76
        bar_h = 16
        bar_x = 8
        bar_w = w - 16
        dc.SetBrush(wx.Brush("#e3e3e3"))
        dc.SetPen(wx.Pen("#c8c8c8", 1))
        dc.DrawRoundedRectangle(bar_x, bar_y, bar_w, bar_h, 8)
        fill_w = int(bar_w * self.overall / 100.0)
        if fill_w > 4:
            fill_color = wx_color("#43a047") if self.overall >= 100 else wx_color("#1e88e5")
            dc.SetBrush(wx.Brush(fill_color))
            dc.SetPen(wx.Pen(fill_color, 1))
            dc.DrawRoundedRectangle(bar_x, bar_y, max(fill_w, 14), bar_h, 8)
        dc.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        dc.SetTextForeground(wx.Colour("#ffffff") if fill_w > 40 else wx.Colour("#555555"))
        label = f"整体进度 {self.overall:.1f}%   ·   {self.overall_text}"
        tw3, th3 = dc.GetTextExtent(label)
        dc.DrawText(label, bar_x + (bar_w - tw3) // 2, bar_y + (bar_h - th3) // 2)


# ======================================================================
#  范围总览网格（可视化每个瓦片的状态：已完成/失败/待处理）
#
#  渲染方式：用 numpy 把状态数组映射成 RGB 位图后一次性贴图，
#  不再逐格画矩形，因此 5 万乃至百万级瓦片也能流畅刷新。
#  缩小到一像素多格时先做「最大值池化」，失败点不会被绿色淹没。
#  交互：滚轮缩放、拖拽平移、悬停查看坐标、点击预览。
# ======================================================================
GRID_PENDING, GRID_OK, GRID_FAILED = 0, 1, 2
GRID_PALETTE = np.array([
    [213, 213, 213],   # 待处理 #d5d5d5
    [67, 160, 71],     # 已下载 #43a047
    [229, 57, 53],     # 失败   #e53935
], dtype=np.uint8)
GRID_STATUS_TEXT = {GRID_PENDING: "待处理", GRID_OK: "已下载", GRID_FAILED: "失败"}
GRID_STATUS_CODE = {"pending": GRID_PENDING, "ok": GRID_OK, "failed": GRID_FAILED}


class TileGridPanel(wx.Panel):
    LEGEND_H = 22
    MAX_POOL = 64        # 缩小视图时单轴最大合并倍数
    MAX_ZOOM = 80.0      # 单瓦片最大屏幕像素数

    def __init__(self, parent, frame):
        super().__init__(parent, size=(-1, 190))
        self.frame = frame
        self._range = None                       # (matrix, cs, ce, rs, re)
        self._status = None                      # (nrows, ncols) uint8
        self._pooled = None                      # 池化缓存
        self._pooled_k = 1
        self._pool_dirty = True

        # 视图（scale = 每个瓦片的屏幕像素数）
        self._scale = 1.0
        self._ox = 0.0
        self._oy = 0.0
        self._view_ready = False

        self._raster = None
        self._dirty = True
        self._repaint_scheduled = False

        self._hover = None
        self._dragging = False
        self._drag_moved = False
        self._drag_last = None

        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(wx.Colour("#ffffff"))
        self.SetToolTip("滚轮缩放 · 拖拽平移 · 点击格子预览该瓦片")
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)

    # ---------- 数据 ----------
    def set_range(self, matrix, col_start, col_end, row_start, row_end):
        self._range = (matrix, col_start, col_end, row_start, row_end)
        ncols = max(0, col_end - col_start + 1)
        nrows = max(0, row_end - row_start + 1)
        self._status = np.zeros((nrows, ncols), dtype=np.uint8)
        self._pooled = None
        self._pool_dirty = True
        self._view_ready = False
        self._hover = None
        self._invalidate()

    def mark(self, col, row, status):
        if not self._range or self._status is None:
            return
        _m, cs, ce, rs, re = self._range
        if not (cs <= col <= ce and rs <= row <= re):
            return
        code = GRID_STATUS_CODE.get(status, GRID_PENDING)
        r, c = row - rs, col - cs
        if self._status[r, c] != code:
            self._status[r, c] = code
            self._pool_dirty = True
            self._invalidate()

    def scan_disk_status(self):
        """扫描输出目录，生成状态数组。只读文件系统、不触碰 wx，可放后台线程。"""
        if not self._range:
            return None
        matrix, cs, ce, rs, re = self._range
        ncols, nrows = ce - cs + 1, re - rs + 1
        if ncols <= 0 or nrows <= 0:
            return None
        arr = np.zeros((nrows, ncols), dtype=np.uint8)
        out_dir = cfg.OUTPUT_DIR
        if not os.path.isdir(out_dir):
            return arr
        # 遍历缓存（兼容新旧结构），标记范围内的瓦片
        try:
            for m, col, row, _path in iter_tiles(out_dir):
                if m == matrix and cs <= col <= ce and rs <= row <= re:
                    arr[row - rs, col - cs] = GRID_OK
        except OSError:
            pass
        return arr

    def apply_status(self, arr):
        if arr is None or not self._range or self._status is None:
            return
        if arr.shape != self._status.shape:
            return
        self._status = arr
        self._pooled = None
        self._pool_dirty = True
        self._invalidate()

    def refresh_from_disk(self, status=None):
        self.apply_status(status if status is not None else self.scan_disk_status())

    def counts(self):
        if self._status is None or self._status.size == 0:
            return 0, 0
        b = np.bincount(self._status.ravel(), minlength=3)
        return int(b[GRID_OK]), int(b[GRID_FAILED])

    # ---------- 视图 ----------
    def _area_size(self):
        w, h = self.GetClientSize()
        return max(1, w), max(1, h - self.LEGEND_H)

    def _dims(self):
        if self._status is None:
            return 0, 0
        return self._status.shape[1], self._status.shape[0]

    def _fit_view(self):
        ncols, nrows = self._dims()
        aw, ah = self._area_size()
        if ncols <= 0 or nrows <= 0:
            self._scale, self._ox, self._oy = 1.0, 0.0, 0.0
        else:
            self._scale = max(min(aw / ncols, ah / nrows), 1e-6)
            self._ox = (aw - ncols * self._scale) / 2
            self._oy = (ah - nrows * self._scale) / 2
        self._view_ready = True
        self._invalidate()

    def _clamp_view(self):
        ncols, nrows = self._dims()
        aw, ah = self._area_size()
        cw, ch = ncols * self._scale, nrows * self._scale
        self._ox = min(aw - 40, max(40 - cw, self._ox))
        self._oy = min(ah - 40, max(40 - ch, self._oy))

    def _get_pooled(self, k):
        if self._pooled is not None and self._pooled_k == k and not self._pool_dirty:
            return self._pooled
        a = self._status
        nr, nc = a.shape
        pr, pc = (-nr) % k, (-nc) % k
        if pr or pc:
            a = np.pad(a, ((0, pr), (0, pc)))
        h, w = a.shape[0] // k, a.shape[1] // k
        self._pooled = a.reshape(h, k, w, k).max(axis=(1, 3))
        self._pooled_k = k
        self._pool_dirty = False
        return self._pooled

    # ---------- 渲染 ----------
    def _invalidate(self, immediate=False):
        self._dirty = True
        if immediate:
            self.Refresh(False)
        elif not self._repaint_scheduled:
            self._repaint_scheduled = True
            wx.CallLater(80, self._flush_repaint)

    def _flush_repaint(self):
        self._repaint_scheduled = False
        if self._dirty:
            self.Refresh(False)

    def _build_raster(self):
        ncols, nrows = self._dims()
        aw, ah = self._area_size()
        if ncols <= 0 or nrows <= 0 or self._status is None:
            return None
        # 一像素覆盖多个瓦片时先池化，保留失败点可见性
        k = 1
        if self._scale < 1.0:
            k = min(self.MAX_POOL, max(1, int(np.ceil(1.0 / self._scale))))
        if k > 1:
            src = self._get_pooled(k)
            step = self._scale * k
        else:
            src = self._status
            step = self._scale
        sh, sw = src.shape
        ox, oy = self._ox, self._oy

        xs = (np.arange(aw) - ox) / step
        ys = (np.arange(ah) - oy) / step
        ci = np.floor(xs).astype(np.int64)
        ri = np.floor(ys).astype(np.int64)
        mx = (ci >= 0) & (ci < sw)
        my = (ri >= 0) & (ri < sh)
        ci = np.clip(ci, 0, sw - 1)
        ri = np.clip(ri, 0, sh - 1)

        sub = src[np.ix_(ri, ci)]
        rgb = GRID_PALETTE[sub]
        mask = my[:, None] & mx[None, :]
        if not mask.all():
            rgb[~mask] = (255, 255, 255)
        buf = np.ascontiguousarray(rgb).tobytes()
        return wx.Bitmap.FromBuffer(aw, ah, buf)

    def _draw_grid_lines(self, dc):
        step = self._scale
        if step < 3:
            return
        ncols, nrows = self._dims()
        aw, ah = self._area_size()
        dc.SetPen(wx.Pen("#ffffff" if step < 12 else "#9e9e9e", 1))
        first = max(0, int(np.floor(-self._ox / step)))
        last = min(ncols, int(np.ceil((aw - self._ox) / step)))
        for i in range(first, last + 1):
            x = int(round(self._ox + i * step))
            if 0 <= x < aw:
                dc.DrawLine(x, 0, x, ah)
        first = max(0, int(np.floor(-self._oy / step)))
        last = min(nrows, int(np.ceil((ah - self._oy) / step)))
        for i in range(first, last + 1):
            y = int(round(self._oy + i * step))
            if 0 <= y < ah:
                dc.DrawLine(0, y, aw, y)

    def _on_paint(self, evt):
        dc = wx.PaintDC(self)
        dc.SetBackground(wx.Brush("#ffffff"))
        dc.Clear()
        w, h = self.GetClientSize()
        if not self._range or self._status is None:
            dc.SetTextForeground(wx.Colour("#999999"))
            dc.DrawText("未设置下载范围，请先在「配置参数」中填写", 12, h // 2 - 8)
            return
        ncols, nrows = self._dims()
        if ncols <= 0 or nrows <= 0:
            return
        if not self._view_ready:
            self._fit_view()

        if self._dirty or self._raster is None:
            self._raster = self._build_raster()
            self._dirty = False
        if self._raster is not None:
            dc.DrawBitmap(self._raster, 0, 0)
            self._draw_grid_lines(dc)
        self._draw_overlay(dc, w, h)

    def _draw_overlay(self, dc, w, h):
        # 悬停高亮
        tip = None
        if self._hover and self._status is not None:
            ncols, nrows = self._dims()
            _m, cs, _ce, rs, _re = self._range
            r, c = self._hover[1] - rs, self._hover[0] - cs
            if 0 <= r < nrows and 0 <= c < ncols:
                x = int(round(self._ox + c * self._scale))
                y = int(round(self._oy + r * self._scale))
                size = int(np.ceil(self._scale))
                if size >= 2:
                    dc.SetBrush(wx.TRANSPARENT_BRUSH)
                    dc.SetPen(wx.Pen("#1e1e1e", 2))
                    dc.DrawRectangle(x - 1, y - 1, max(2, size + 2), max(2, size + 2))
                code = int(self._status[r, c])
                tip = f"列 {self._hover[0]} / 行 {self._hover[1]} · {GRID_STATUS_TEXT.get(code, '?')}"

        font = wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        dc.SetFont(font)
        if tip:
            tw, th = dc.GetTextExtent(tip)
            bx, by = 6, 6
            dc.SetBrush(wx.Brush(wx.Colour(255, 255, 255, 235)))
            dc.SetPen(wx.Pen("#bbbbbb", 1))
            dc.DrawRoundedRectangle(bx, by, tw + 12, th + 6, 4)
            dc.SetTextForeground(wx.Colour("#333333"))
            dc.DrawText(tip, bx + 6, by + 3)

        ok, failed = self.counts()
        total = self._status.size
        pending = total - ok - failed
        ly = h - self.LEGEND_H + 4
        dc.SetFont(font)
        dc.SetTextForeground(wx.Colour("#555555"))
        legend = f"● 已完成: {ok}   ● 失败: {failed}   ● 待处理: {pending}   (共 {total} 片)"
        dc.DrawText(legend, 6, ly)
        hint = "滚轮缩放 · 拖拽平移 · 点击预览"
        tw, _th = dc.GetTextExtent(hint)
        dc.SetTextForeground(wx.Colour("#999999"))
        dc.DrawText(hint, max(6, w - tw - 8), ly)

    # ---------- 交互 ----------
    def _tile_at(self, px, py):
        if not self._range or self._status is None or not self._view_ready:
            return None
        aw, ah = self._area_size()
        if not (0 <= px < aw and 0 <= py < ah):
            return None
        ncols, nrows = self._dims()
        col = int(np.floor((px - self._ox) / self._scale))
        row = int(np.floor((py - self._oy) / self._scale))
        if 0 <= col < ncols and 0 <= row < nrows:
            _m, cs, _ce, rs, _re = self._range
            return (cs + col, rs + row)
        return None

    def _update_hover(self, px, py):
        t = self._tile_at(px, py)
        if t != self._hover:
            self._hover = t
            self.Refresh(False)

    def _on_left_down(self, evt):
        if not self._range:
            return
        self.SetFocus()
        if not self.HasCapture():
            self.CaptureMouse()
        self._dragging = True
        self._drag_moved = False
        self._drag_last = evt.GetPosition()

    def _on_left_up(self, evt):
        was_drag = self._drag_moved
        self._dragging = False
        self._drag_last = None
        if self.HasCapture():
            self.ReleaseMouse()
        if not was_drag:
            t = self._tile_at(*evt.GetPosition())
            if t:
                self.frame.on_grid_click(t[0], t[1])

    def _on_motion(self, evt):
        pos = evt.GetPosition()
        if self._dragging and self._drag_last is not None:
            dx = pos[0] - self._drag_last[0]
            dy = pos[1] - self._drag_last[1]
            if dx or dy:
                if abs(dx) > 3 or abs(dy) > 3:
                    self._drag_moved = True
                self._ox += dx
                self._oy += dy
                self._clamp_view()
                self._invalidate(immediate=True)
            self._drag_last = pos
        self._update_hover(*pos)

    def _on_wheel(self, evt):
        if not self._range or self._status is None:
            return
        rot = evt.GetWheelRotation()
        if not rot:
            return
        if not self._view_ready:
            self._fit_view()
        aw, ah = self._area_size()
        ncols, nrows = self._dims()
        fit = min(aw / max(ncols, 1), ah / max(nrows, 1))
        factor = 1.25 if rot > 0 else 1 / 1.25
        new_scale = max(fit, min(self._scale * factor, self.MAX_ZOOM))
        if abs(new_scale - self._scale) < 1e-9:
            return
        mx, my = evt.GetPosition()
        my = min(my, ah)
        tx = (mx - self._ox) / self._scale
        ty = (my - self._oy) / self._scale
        self._scale = new_scale
        self._ox = mx - tx * new_scale
        self._oy = my - ty * new_scale
        self._clamp_view()
        self._invalidate(immediate=True)
        self._update_hover(mx, my)

    def _on_capture_lost(self, evt):
        self._dragging = False
        self._drag_last = None

    def _on_leave(self, evt):
        if self._hover is not None:
            self._hover = None
            self.Refresh(False)

    def _on_size(self, evt):
        evt.Skip()
        if not self._view_ready:
            self._invalidate()
            return
        aw, ah = self._area_size()
        ncols, nrows = self._dims()
        if ncols > 0 and nrows > 0:
            fit = min(aw / ncols, ah / nrows)
            if self._scale <= fit * 1.02:
                self._view_ready = False   # 处于适配状态，跟随窗口重新适配
            else:
                self._clamp_view()
        self._invalidate()


# ======================================================================
#  瓦片预览面板
# ======================================================================
class PreviewPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self._fetching = False
        self._current_path = None
        self._auto_timer = None
        self._fetched_bytes = None  # 网络获取但尚未保存的原始字节
        self._unsaved = False
        self.PREVIEW_W, self.PREVIEW_H = 340, 300

        outer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(self, label="瓦片预览")
        title.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        outer.Add(title, 0, wx.ALL, 8)

        # 坐标选择
        coords = wx.BoxSizer(wx.HORIZONTAL)
        coords.Add(wx.StaticText(self, label="级别"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        self.matrix_spin = wx.SpinCtrl(self, min=0, max=30, initial=cfg.TILE_MATRIX)
        coords.Add(self.matrix_spin, 0, wx.LEFT, 4)
        coords.Add(wx.StaticText(self, label="列"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        self.col_spin = wx.SpinCtrl(self, min=0, max=2000000, initial=cfg.TILE_COL_START)
        coords.Add(self.col_spin, 1, wx.LEFT, 4)
        coords.Add(wx.StaticText(self, label="行"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        self.row_spin = wx.SpinCtrl(self, min=0, max=2000000, initial=cfg.TILE_ROW_START)
        coords.Add(self.row_spin, 1, wx.LEFT | wx.RIGHT, 4)
        outer.Add(coords, 0, wx.EXPAND | wx.TOP, 4)

        # 坐标变化 -> 自动加载预览（防抖）
        for _s in (self.matrix_spin, self.col_spin, self.row_spin):
            _s.Bind(wx.EVT_SPINCTRL, lambda e: self._schedule_auto())
            _s.Bind(wx.EVT_TEXT, lambda e: self._schedule_auto())

        # 操作按钮（预览自动加载，保存需手动点击）
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.save_btn = wx.Button(self, label="保存瓦片")
        self.clear_btn = wx.Button(self, label="清空")
        self.save_btn.Bind(wx.EVT_BUTTON, lambda e: self.frame.on_save_tile())
        self.clear_btn.Bind(wx.EVT_BUTTON, lambda e: self._clear())
        self.save_btn.Disable()
        btns.Add(self.save_btn, 0, wx.RIGHT, 6)
        btns.Add(self.clear_btn, 0)
        outer.Add(btns, 0, wx.TOP, 8)

        hint = wx.StaticText(self, label="修改坐标自动加载预览：本地已有则直接显示，缺失时自动从网络获取（不保存）。")
        hint.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        hint.SetForegroundColour(wx.Colour("#888888"))
        outer.Add(hint, 0, wx.TOP, 6)

        # 预览图像
        self.image_ctrl = wx.StaticBitmap(self, size=(self.PREVIEW_W, self.PREVIEW_H))
        self._set_placeholder("输入行列号后点击预览")
        outer.Add(self.image_ctrl, 0, wx.TOP, 8)

        # 信息
        self.info_ctrl = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 96))
        self.info_ctrl.SetFont(wx.Font(8, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        outer.Add(self.info_ctrl, 1, wx.EXPAND | wx.TOP, 8)

        self.SetSizer(outer)

    # ---------- 内部工具 ----------
    def _coords(self):
        return self.matrix_spin.GetValue(), self.col_spin.GetValue(), self.row_spin.GetValue()

    def set_coords(self, matrix, col, row):
        self.matrix_spin.SetValue(matrix)
        self.col_spin.SetValue(col)
        self.row_spin.SetValue(row)
        self._schedule_auto()

    def _schedule_auto(self):
        """防抖：坐标停止变化 400ms 后自动加载预览"""
        if self._auto_timer and self._auto_timer.IsRunning():
            self._auto_timer.Stop()
        self._auto_timer = wx.CallLater(400, self.frame.on_preview_auto)

    def cancel_auto(self):
        if self._auto_timer and self._auto_timer.IsRunning():
            self._auto_timer.Stop()

    def reset_pending(self):
        """开始一次新的网络获取前，清除上次未保存的内容"""
        self._fetched_bytes = None
        self._unsaved = False
        self.save_btn.Disable()

    def show_fetched(self, content, matrix, col, row):
        """显示网络获取的原始字节（写临时文件供 wx 解码，不保存到输出目录）"""
        import tempfile
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="wmts_preview_", suffix=".png", delete=False) as f:
                f.write(content)
                tmp_path = f.name
            self._fetched_bytes = content
            self._unsaved = True
            self.show_image(tmp_path, "network")
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def mark_saved(self, path):
        """手动保存成功后的回调"""
        self._unsaved = False
        self._fetched_bytes = None
        self.save_btn.Disable()
        txt = self.info_ctrl.GetValue().replace("（未保存", "（已保存")
        self.info_ctrl.SetValue(txt)

    @property
    def fetched_bytes(self):
        return self._fetched_bytes

    def _set_placeholder(self, text=""):
        bmp = wx.Bitmap(self.PREVIEW_W, self.PREVIEW_H)
        mdc = wx.MemoryDC()
        mdc.SelectObject(bmp)
        mdc.SetBackground(wx.Brush(wx.Colour("#f0f0f0")))
        mdc.Clear()
        mdc.SetTextForeground(wx.Colour("#999999"))
        if text:
            tw, th = mdc.GetTextExtent(text)
            mdc.DrawText(text, (self.PREVIEW_W - tw) // 2, (self.PREVIEW_H - th) // 2)
        mdc.SelectObject(wx.NullBitmap)
        self.image_ctrl.SetBitmap(bmp)
        self.Layout()

    def show_image(self, path, source):
        """在 UI 线程中显示本地图片文件"""
        try:
            img = wx.Image(path)
            iw, ih = img.GetWidth(), img.GetHeight()
            scale = min(self.PREVIEW_W / iw, self.PREVIEW_H / ih, 1.0)
            nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
            img = img.Scale(nw, nh, wx.IMAGE_QUALITY_HIGH)
            bmp = wx.Bitmap(img)

            canvas = wx.Bitmap(self.PREVIEW_W, self.PREVIEW_H)
            mdc = wx.MemoryDC()
            mdc.SelectObject(canvas)
            mdc.SetBackground(wx.Brush(wx.Colour("#f0f0f0")))
            mdc.Clear()
            mdc.DrawBitmap(bmp, (self.PREVIEW_W - nw) // 2, (self.PREVIEW_H - nh) // 2)
            mdc.SelectObject(wx.NullBitmap)
            self.image_ctrl.SetBitmap(canvas)

            size_kb = os.path.getsize(path) / 1024
            m, c, r = self.matrix_spin.GetValue(), self.col_spin.GetValue(), self.row_spin.GetValue()
            if source == "local":
                src_txt = "本地文件（已存在）"
                loc_line = f"路径: {path}"
                self.save_btn.Disable()
            else:
                target = tile_path(m, c, r, cfg.OUTPUT_DIR)
                src_txt = "网络获取（未保存）" if self._unsaved else "网络获取"
                loc_line = f"保存位置: {target}（未保存，可点「保存瓦片」）"
                self.save_btn.Enable(self._unsaved)
            self.info_ctrl.SetValue(
                f"来源: {src_txt}\n"
                f"坐标: 级别 {m} / 列 {c} / 行 {r}\n"
                f"尺寸: {iw} × {ih} 像素\n"
                f"大小: {size_kb:.1f} KB\n"
                f"{loc_line}"
            )
            self._current_path = path
            self.Layout()
        except Exception as e:
            self._set_placeholder("无法加载图片")
            self.info_ctrl.SetValue(f"加载图片失败: {e}")

    def show_error(self, text):
        self._set_placeholder("加载失败")
        self.info_ctrl.SetValue(text)
        self.save_btn.Disable()

    def _clear(self):
        self._current_path = None
        self._fetched_bytes = None
        self._unsaved = False
        self.save_btn.Disable()
        self._set_placeholder("修改坐标即可自动加载预览")
        self.info_ctrl.SetValue("")

    def set_fetching(self, flag):
        self._fetching = flag

    @property
    def fetching(self):
        return self._fetching


# ======================================================================
#  日志面板
# ======================================================================
class LogPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        sizer = wx.BoxSizer(wx.VERTICAL)

        header = wx.BoxSizer(wx.HORIZONTAL)
        t = wx.StaticText(self, label="运行日志")
        t.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        header.Add(t, 0, wx.ALIGN_CENTER_VERTICAL)
        header.AddStretchSpacer(1)
        self.clear_btn = wx.Button(self, label="清空日志", size=(-1, 24))
        self.save_btn = wx.Button(self, label="保存日志", size=(-1, 24))
        self.clear_btn.Bind(wx.EVT_BUTTON, lambda e: self._clear())
        self.save_btn.Bind(wx.EVT_BUTTON, lambda e: self._save())
        header.Add(self.clear_btn, 0, wx.LEFT, 6)
        header.Add(self.save_btn, 0, wx.LEFT, 6)
        sizer.Add(header, 0, wx.EXPAND | wx.BOTTOM, 2)

        self.text = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.text.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        sizer.Add(self.text, 1, wx.EXPAND)
        self.SetSizer(sizer)

    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.text.AppendText(f"[{ts}] {msg}\n")
        self.text.ShowPosition(self.text.GetLastPosition())

    def _clear(self):
        self.text.Clear()

    def _save(self):
        with wx.FileDialog(self, "保存日志", wildcard="文本文件 (*.txt)|*.txt",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                try:
                    with open(dlg.GetPath(), "w", encoding="utf-8") as f:
                        f.write(self.text.GetValue())
                    self.frame._log(f"日志已保存到 {dlg.GetPath()}")
                except Exception as e:
                    self.frame._log(f"保存日志失败: {e}")


# ======================================================================
#  主窗口
# ======================================================================
class WMTSFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=APP_TITLE, size=(1180, 820))
        self.SetMinSize((960, 680))
        self._busy = False
        self._pipeline = False
        self._downloader = None
        self._dl_start = 0.0
        self._dl_stats = {}
        self._fetch_seq = 0  # 网络预览请求序号，用于丢弃过期结果
        self._merge_mode = "batch"
        self._save_mode = "fast"

        # ---------- 布局 ----------
        root = wx.BoxSizer(wx.VERTICAL)

        # 顶部：任务流程
        self.flow = FlowPanel(self)
        root.Add(self.flow, 0, wx.EXPAND)

        # 操作按钮条
        action_bar = wx.BoxSizer(wx.HORIZONTAL)
        self.pipeline_btn = wx.Button(self, label="▶ 一键执行全部")
        self.dl_btn = wx.Button(self, label="开始下载")
        self.merge_btn = wx.Button(self, label="开始拼接")
        self.stop_btn = wx.Button(self, label="停止")
        self.cache_btn = wx.Button(self, label="缓存管理")
        self.pipeline_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_pipeline())
        self.dl_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_download())
        self.merge_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_merge())
        self.stop_btn.Bind(wx.EVT_BUTTON, lambda e: self.stop_current())
        self.cache_btn.Bind(wx.EVT_BUTTON, lambda e: self.open_cache_manager())
        self.stop_btn.Disable()
        for b in (self.pipeline_btn, self.dl_btn, self.merge_btn, self.stop_btn, self.cache_btn):
            action_bar.Add(b, 0, wx.RIGHT, 8)
        action_bar.AddStretchSpacer(1)
        self.range_hint = wx.StaticText(self, label="")
        action_bar.Add(self.range_hint, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(action_bar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # 中间：左右分栏
        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        left_panel = wx.Panel(splitter)
        self.preview = PreviewPanel(splitter, self)
        self._build_config_tab(left_panel)
        splitter.SplitVertically(left_panel, self.preview, 640)
        splitter.SetMinimumPaneSize(300)
        root.Add(splitter, 1, wx.EXPAND | wx.ALL, 8)

        # 底部：日志
        self.log_panel = LogPanel(self, self)
        root.Add(self.log_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.log_panel.SetMinSize((-1, 170))

        self.SetSizer(root)

        # 状态栏
        self.status = self.CreateStatusBar(2)
        self.status.SetStatusText("就绪", 0)
        self.status.SetStatusText("", 1)

        # 事件
        self.Bind(wx.EVT_CLOSE, self._on_close)

        # 初始化
        self._load_config()
        self._update_range_info()
        self._sync_core_modules()
        self._refresh_all()
        self._log("欢迎使用 WMTS 瓦片下载与拼接工具")
        self._log(f"下载范围: 级别 {cfg.TILE_MATRIX}, 列 {cfg.TILE_COL_START}-{cfg.TILE_COL_END}, "
                  f"行 {cfg.TILE_ROW_START}-{cfg.TILE_ROW_END}, 共 {self._calc_total()} 片")

        # 后台扫描已下载瓦片
        threading.Thread(target=self._thread_scan, daemon=True).start()
        # 启动后自动预览一次当前坐标的瓦片（本地优先）
        wx.CallLater(400, self.on_preview_auto)

    # ================= 配置面板 =================
    def _build_config_tab(self, parent):
        nb = wx.Notebook(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(nb, 1, wx.EXPAND)
        parent.SetSizer(sizer)

        # ---- 配置参数页 ----
        cfg_page = wx.Panel(nb)
        nb.AddPage(cfg_page, " 配置参数 ")
        grid = wx.GridBagSizer(5, 8)

        def lab(row, col, text):
            grid.Add(wx.StaticText(cfg_page, label=text), (row, col), flag=wx.ALIGN_CENTER_VERTICAL)

        def ctl(row, col, c, span=1):
            grid.Add(c, (row, col), span=(1, span), flag=wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)

        self.url_ctrl = wx.TextCtrl(cfg_page, value=cfg.BASE_URL)
        lab(0, 0, "服务地址"); ctl(0, 1, self.url_ctrl, 5)
        self.layer_ctrl = wx.TextCtrl(cfg_page, value=cfg.LAYER)
        lab(1, 0, "图层 Layer"); ctl(1, 1, self.layer_ctrl)
        self.style_ctrl = wx.TextCtrl(cfg_page, value=cfg.STYLE)
        lab(1, 2, "样式"); ctl(1, 3, self.style_ctrl)

        self.matrix_ctrl = wx.SpinCtrl(cfg_page, min=0, max=30, initial=cfg.TILE_MATRIX)
        lab(2, 0, "缩放级别"); ctl(2, 1, self.matrix_ctrl)
        self.col_start_ctrl = wx.SpinCtrl(cfg_page, min=0, max=2000000, initial=cfg.TILE_COL_START)
        self.col_end_ctrl = wx.SpinCtrl(cfg_page, min=0, max=2000000, initial=cfg.TILE_COL_END)
        lab(2, 2, "列范围"); ctl(2, 3, self.col_start_ctrl)
        grid.Add(wx.StaticText(cfg_page, label="~"), (2, 4), flag=wx.ALIGN_CENTER_VERTICAL)
        ctl(2, 5, self.col_end_ctrl)
        self.row_start_ctrl = wx.SpinCtrl(cfg_page, min=0, max=2000000, initial=cfg.TILE_ROW_START)
        self.row_end_ctrl = wx.SpinCtrl(cfg_page, min=0, max=2000000, initial=cfg.TILE_ROW_END)
        lab(3, 0, "行范围"); ctl(3, 1, self.row_start_ctrl)
        grid.Add(wx.StaticText(cfg_page, label="~"), (3, 2), flag=wx.ALIGN_CENTER_VERTICAL)
        ctl(3, 3, self.row_end_ctrl)

        self.out_dir_ctrl = wx.TextCtrl(cfg_page, value=cfg.OUTPUT_DIR)
        lab(4, 0, "保存目录"); ctl(4, 1, self.out_dir_ctrl, 3)
        browse_btn = wx.Button(cfg_page, label="浏览…")
        browse_btn.Bind(wx.EVT_BUTTON, lambda e: self._browse_dir())
        grid.Add(browse_btn, (4, 4), span=(1, 2))
        self.out_file_ctrl = wx.TextCtrl(cfg_page, value=cfg.OUTPUT_FILE)
        lab(5, 0, "输出文件"); ctl(5, 1, self.out_file_ctrl, 5)

        self.workers_ctrl = wx.SpinCtrl(cfg_page, min=1, max=128, initial=cfg.MAX_WORKERS)
        lab(6, 0, "并发数"); ctl(6, 1, self.workers_ctrl)
        self.timeout_ctrl = wx.SpinCtrl(cfg_page, min=1, max=120, initial=cfg.TIMEOUT)
        lab(6, 2, "超时(秒)"); ctl(6, 3, self.timeout_ctrl)

        grid.AddGrowableCol(1, 1)
        grid.AddGrowableCol(3, 1)
        grid.AddGrowableCol(5, 1)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        save_cfg = wx.Button(cfg_page, label="保存配置")
        load_cfg = wx.Button(cfg_page, label="加载配置")
        reset_cfg = wx.Button(cfg_page, label="恢复默认")
        save_cfg.Bind(wx.EVT_BUTTON, lambda e: self._save_config())
        load_cfg.Bind(wx.EVT_BUTTON, lambda e: self._load_config())
        reset_cfg.Bind(wx.EVT_BUTTON, lambda e: self._reset_config())
        btn_row.Add(save_cfg, 0, wx.RIGHT, 6)
        btn_row.Add(load_cfg, 0, wx.RIGHT, 6)
        btn_row.Add(reset_cfg, 0)

        page_sizer = wx.BoxSizer(wx.VERTICAL)
        page_sizer.Add(grid, 0, wx.EXPAND)
        page_sizer.Add(btn_row, 0, wx.TOP, 10)
        cfg_page.SetSizer(page_sizer)

        # 绑定范围变化事件
        for c in (self.matrix_ctrl, self.col_start_ctrl, self.col_end_ctrl,
                  self.row_start_ctrl, self.row_end_ctrl):
            c.Bind(wx.EVT_SPINCTRL, lambda e: self._update_range_info())

        # ---- 下载页 ----
        dl_page = wx.Panel(nb)
        nb.AddPage(dl_page, " 下载瓦片 ")
        dl_sizer = wx.BoxSizer(wx.VERTICAL)

        self.dl_progress = wx.Gauge(dl_page, range=1000)
        dl_sizer.Add(wx.StaticText(dl_page, label="下载进度:"), 0, wx.BOTTOM, 2)
        dl_sizer.Add(self.dl_progress, 0, wx.EXPAND)
        self.dl_percent = wx.StaticText(dl_page, label="0%")
        dl_sizer.Add(self.dl_percent, 0, wx.TOP, 2)
        self.dl_stats = wx.StaticText(dl_page, label="总计 0 | 成功 0 | 失败 0 | 跳过 0 | 无效 0 | 重试 0")
        dl_sizer.Add(self.dl_stats, 0, wx.TOP, 4)

        dl_sizer.Add(wx.StaticText(dl_page, label="范围总览 (滚轮缩放 / 拖拽平移 / 点击格子可预览):"), 0, wx.TOP | wx.BOTTOM, 8)
        self.grid = TileGridPanel(dl_page, self)
        dl_sizer.Add(self.grid, 1, wx.EXPAND)

        dl_sizer.Add(wx.StaticText(dl_page, label="说明: 绿色=已下载, 红色=失败, 灰色=待处理"), 0, wx.TOP, 4)
        dl_page.SetSizer(dl_sizer)

        # ---- 拼接页 ----
        mg_page = wx.Panel(nb)
        nb.AddPage(mg_page, " 拼接大图 ")
        mg_sizer = wx.BoxSizer(wx.VERTICAL)

        mg_sizer.Add(wx.StaticText(mg_page, label="拼接模式:"), 0, wx.BOTTOM, 4)
        self.mode_radio = wx.RadioBox(mg_page, choices=["批量模式（平衡速度与内存）", "流式模式（低内存）", "超低内存模式（超大图）"],
                                      style=wx.RA_SPECIFY_ROWS)
        self.mode_radio.Bind(wx.EVT_RADIOBOX, lambda e: self._update_merge_ui())
        mg_sizer.Add(self.mode_radio, 0, wx.EXPAND)

        mg_sizer.Add(wx.StaticText(mg_page, label="保存模式:"), 0, wx.TOP | wx.BOTTOM, 4)
        self.save_radio = wx.RadioBox(mg_page, choices=["快速保存（速度优先）", "优化压缩（文件更小）"],
                                      style=wx.RA_SPECIFY_ROWS)
        mg_sizer.Add(self.save_radio, 0, wx.EXPAND)

        self.batch_rows_ctrl = wx.SpinCtrl(mg_page, min=1, max=200, initial=10)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(mg_page, label="每批处理行数:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        row.Add(self.batch_rows_ctrl, 0)
        mg_sizer.Add(row, 0, wx.TOP, 8)

        # Rust 引擎（超大图推荐：内存恒定、并行压缩）
        self.rust_chk = wx.CheckBox(mg_page, label="使用 Rust 引擎（内存恒定，适合超大图）")
        self.rust_chk.Bind(wx.EVT_CHECKBOX, lambda e: self._update_merge_ui())
        mg_sizer.Add(self.rust_chk, 0, wx.TOP, 10)
        self.rust_fmt_radio = wx.RadioBox(
            mg_page,
            choices=["BigTIFF 分块（推荐，无损）", "单张 PNG"],
            style=wx.RA_SPECIFY_ROWS,
        )
        mg_sizer.Add(self.rust_fmt_radio, 0, wx.EXPAND | wx.TOP, 4)

        self.mg_progress = wx.Gauge(mg_page, range=1000)
        mg_sizer.Add(wx.StaticText(mg_page, label="拼接进度:"), 0, wx.TOP | wx.BOTTOM, 8)
        mg_sizer.Add(self.mg_progress, 0, wx.EXPAND)
        self.mg_stats = wx.StaticText(mg_page, label="状态: 尚未开始")
        mg_sizer.Add(self.mg_stats, 0, wx.TOP, 4)

        mg_sizer.AddStretchSpacer(1)
        mg_page.SetSizer(mg_sizer)
        self._update_merge_ui()

    # ================= 配置读写 =================
    def _browse_dir(self):
        with wx.DirDialog(self, "选择保存目录", defaultPath=self.out_dir_ctrl.GetValue()) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.out_dir_ctrl.SetValue(dlg.GetPath())

    def _update_merge_ui(self):
        """根据拼接模式启用/禁用相应控件"""
        if not hasattr(self, "mode_radio"):
            return
        use_rust = self.rust_chk.GetValue()
        self.mode_radio.Enable(not use_rust)
        self.save_radio.Enable(not use_rust)
        self.batch_rows_ctrl.Enable(not use_rust and self.mode_radio.GetSelection() == 0)
        self.rust_fmt_radio.Enable(use_rust)

    def _read_ui(self):
        """从 UI 读取配置"""
        return {
            "base_url": self.url_ctrl.GetValue().strip(),
            "layer": self.layer_ctrl.GetValue().strip(),
            "style": self.style_ctrl.GetValue().strip(),
            "tile_matrix": self.matrix_ctrl.GetValue(),
            "col_start": self.col_start_ctrl.GetValue(),
            "col_end": self.col_end_ctrl.GetValue(),
            "row_start": self.row_start_ctrl.GetValue(),
            "row_end": self.row_end_ctrl.GetValue(),
            "output_dir": self.out_dir_ctrl.GetValue().strip(),
            "output_file": self.out_file_ctrl.GetValue().strip(),
            "max_workers": self.workers_ctrl.GetValue(),
            "timeout": self.timeout_ctrl.GetValue(),
        }

    def _apply_ui(self, d):
        self.url_ctrl.SetValue(d["base_url"])
        self.layer_ctrl.SetValue(d["layer"])
        self.style_ctrl.SetValue(d["style"])
        self.matrix_ctrl.SetValue(d["tile_matrix"])
        self.col_start_ctrl.SetValue(d["col_start"])
        self.col_end_ctrl.SetValue(d["col_end"])
        self.row_start_ctrl.SetValue(d["row_start"])
        self.row_end_ctrl.SetValue(d["row_end"])
        self.out_dir_ctrl.SetValue(d["output_dir"])
        self.out_file_ctrl.SetValue(d["output_file"])
        self.workers_ctrl.SetValue(d["max_workers"])
        self.timeout_ctrl.SetValue(d["timeout"])
        self._update_range_info()

    def _save_config(self):
        try:
            d = self._read_ui()
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            self._log(f"配置已保存到 {CONFIG_FILE}")
        except Exception as e:
            self._log(f"保存配置失败: {e}")

    def _load_config(self):
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k in ("base_url", "layer", "style", "tile_matrix", "col_start", "col_end",
                      "row_start", "row_end", "output_dir", "output_file", "max_workers", "timeout"):
                if k in d:
                    self._set_attr(k, d[k])
            self._log(f"已加载配置 {CONFIG_FILE}")
        except Exception as e:
            self._log(f"加载配置失败: {e}")

    def _set_attr(self, key, val):
        """将保存的配置写入对应控件"""
        map_ = {
            "base_url": self.url_ctrl, "layer": self.layer_ctrl, "style": self.style_ctrl,
            "tile_matrix": self.matrix_ctrl, "col_start": self.col_start_ctrl,
            "col_end": self.col_end_ctrl, "row_start": self.row_start_ctrl,
            "row_end": self.row_end_ctrl, "output_dir": self.out_dir_ctrl,
            "output_file": self.out_file_ctrl, "max_workers": self.workers_ctrl,
            "timeout": self.timeout_ctrl,
        }
        c = map_.get(key)
        if c:
            c.SetValue(int(val) if isinstance(c, wx.SpinCtrl) else str(val))

    def _reset_config(self):
        d = {
            "base_url": "https://geocloud.hubgs.com/api/igs/rest/ogc/WMTSServer",
            "layer": cfg.LAYER, "style": "default",
            "tile_matrix": 16, "col_start": 52792, "col_end": 52838,
            "row_start": 10467, "row_end": 10497,
            "output_dir": "tiles", "output_file": "merged_map.png",
            "max_workers": 16, "timeout": 10,
        }
        self._apply_ui(d)
        self._log("已恢复默认配置")

    def _calc_total(self):
        return (self.col_end_ctrl.GetValue() - self.col_start_ctrl.GetValue() + 1) * (
            self.row_end_ctrl.GetValue() - self.row_start_ctrl.GetValue() + 1)

    def _update_range_info(self):
        try:
            cols = self.col_end_ctrl.GetValue() - self.col_start_ctrl.GetValue() + 1
            rows = self.row_end_ctrl.GetValue() - self.row_start_ctrl.GetValue() + 1
            total = cols * rows
            if total > 0:
                self.range_hint.SetLabel(f"共 {cols} 列 × {rows} 行 = {total:,} 片 (级别 {self.matrix_ctrl.GetValue()})")
            else:
                self.range_hint.SetLabel("范围无效")
        except Exception:
            self.range_hint.SetLabel("范围无效")

    def _sync_core_modules(self):
        """将 UI 配置写入 config 模块，并同步到 download_tiles / merge_tiles 的全局变量"""
        d = self._read_ui()
        cfg.BASE_URL = d["base_url"]
        cfg.LAYER = d["layer"]
        cfg.STYLE = d["style"]
        cfg.TILE_MATRIX = d["tile_matrix"]
        cfg.TILE_COL_START = d["col_start"]
        cfg.TILE_COL_END = d["col_end"]
        cfg.TILE_ROW_START = d["row_start"]
        cfg.TILE_ROW_END = d["row_end"]
        cfg.OUTPUT_DIR = d["output_dir"]
        cfg.OUTPUT_FILE = d["output_file"]
        cfg.MAX_WORKERS = d["max_workers"]
        cfg.TIMEOUT = d["timeout"]

        # 同步到下游模块（它们使用模块级全局变量）
        dt_mod.OUTPUT_DIR = cfg.OUTPUT_DIR
        dt_mod.MAX_WORKERS = cfg.MAX_WORKERS
        dt_mod.TIMEOUT = cfg.TIMEOUT
        dt_mod.HEADERS = cfg.HEADERS
        dt_mod.get_tile_url = cfg.get_tile_url

        mt_mod.TILES_DIR = cfg.OUTPUT_DIR
        mt_mod.TILE_MATRIX = cfg.TILE_MATRIX
        mt_mod.TILE_COL_START = cfg.TILE_COL_START
        mt_mod.TILE_COL_END = cfg.TILE_COL_END
        mt_mod.TILE_ROW_START = cfg.TILE_ROW_START
        mt_mod.TILE_ROW_END = cfg.TILE_ROW_END
        mt_mod.OUTPUT_FILE = cfg.OUTPUT_FILE

    def _refresh_all(self):
        self.grid.set_range(cfg.TILE_MATRIX, cfg.TILE_COL_START, cfg.TILE_COL_END,
                            cfg.TILE_ROW_START, cfg.TILE_ROW_END)
        self.grid.refresh_from_disk()

    # ================= 流程控制 =================
    def _set_busy(self, busy):
        self._busy = busy
        for b in (self.pipeline_btn, self.dl_btn, self.merge_btn):
            b.Enable(not busy)
        self.stop_btn.Enable(busy)

    def _validate(self):
        d = self._read_ui()
        if d["col_end"] < d["col_start"] or d["row_end"] < d["row_start"]:
            self._log("错误: 列/行范围不合法（结束值不能小于起始值）")
            return False
        if d["col_end"] - d["col_start"] + 1 > 5000 or d["row_end"] - d["row_start"] + 1 > 5000:
            self._log("错误: 下载范围过大（单边超过 5000）")
            return False
        return True

    # ---- 下载 ----
    def start_pipeline(self):
        if self._busy:
            return
        if not self._validate():
            return
        self._sync_core_modules()
        self._refresh_all()
        self._pipeline = True
        self._reset_flow_for("download")
        self._set_busy(True)
        self.status.SetStatusText("正在下载瓦片...")
        self._log("开始一键执行: 下载 → 拼接")
        threading.Thread(target=self._thread_download, daemon=True).start()

    def start_download(self):
        if self._busy:
            return
        if not self._validate():
            return
        self._sync_core_modules()
        self._refresh_all()
        self._pipeline = False
        self._reset_flow_for("download")
        self._set_busy(True)
        self.status.SetStatusText("正在下载瓦片...")
        self._log("开始下载瓦片")
        threading.Thread(target=self._thread_download, daemon=True).start()

    def _reset_flow_for(self, op):
        self.flow.reset()
        self.flow.set_step(0, ST_DONE, 100)
        if op == "download":
            self.flow.set_step(1, ST_RUNNING, 0)
        elif op == "merge":
            self.flow.set_step(1, ST_DONE, 100)
            self.flow.set_step(2, ST_RUNNING, 0)
        self.flow.set_overall(0, "准备中...")

    def _thread_download(self):
        out_dir = cfg.OUTPUT_DIR
        nworkers = cfg.MAX_WORKERS
        matrix = cfg.TILE_MATRIX
        cs, ce, rs, re = cfg.TILE_COL_START, cfg.TILE_COL_END, cfg.TILE_ROW_START, cfg.TILE_ROW_END
        self._dl_start = time.time()

        downloader = dt_mod.AsyncTileDownloader(
            output_dir=out_dir,
            max_workers=nworkers,
            progress_callback=lambda info: wx.CallAfter(self._on_dl_progress, info),
        )
        self._downloader = downloader
        try:
            asyncio_run(downloader.download_batch(matrix, cs, ce, rs, re))
            ok = not downloader.cancel_event.is_set()
        except Exception as e:
            ok = False
            wx.CallAfter(self._log, f"下载发生异常: {e}")
        finally:
            self._downloader = None
            wx.CallAfter(self._on_dl_done, ok)

    def _on_dl_progress(self, info):
        total = info["total"]
        done = info["done"]
        pct = (done / total * 100) if total else 0
        self.dl_progress.SetValue(int(pct * 10))
        self.dl_percent.SetLabel(f"{pct:.1f}%  ({done}/{total})")
        self.dl_stats.SetLabel(
            f"总计 {total} | 成功 {info['success']} | 失败 {info['fail']} | "
            f"跳过 {info['skip']} | 无效 {info['invalid']} | 重试 {info['retried']}"
        )
        elapsed = time.time() - self._dl_start
        speed = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / speed if speed > 0 else 0
        extra = f" | 速度 {speed:.1f} 片/秒 | 预计剩余 {eta:.0f}s" if speed > 0 else ""
        self.status.SetStatusText(f"下载中 {pct:.1f}%{extra}")
        self.flow.set_step(1, ST_RUNNING, pct)
        if self._pipeline:
            self.flow.set_overall(pct * 0.5, f"下载瓦片 {pct:.1f}%")
        else:
            self.flow.set_overall(pct, f"下载瓦片 {pct:.1f}%")
        # 实时更新网格
        if info.get("last_result") == "success" or info.get("last_result") == "skipped":
            self.grid.mark(info["last_col"], info["last_row"], "ok")
        elif info.get("last_result") == "failed":
            self.grid.mark(info["last_col"], info["last_row"], "failed")
        # 失败时记录日志（降低频率）
        if info["fail"] and info["done"] % 20 == 0:
            self._log(f"进度 {pct:.1f}%: 成功 {info['success']}, 失败 {info['fail']}, 跳过 {info['skip']}")
        self._dl_stats = info

    def _on_dl_done(self, ok):
        info = self._dl_stats or {}
        self.grid.refresh_from_disk()
        if not ok:
            self.flow.set_step(1, ST_FAILED)
            self.flow.set_step(2, ST_PENDING)
            self.flow.set_step(3, ST_PENDING)
            self.flow.set_overall(0, "下载已取消")
            self._set_busy(False)
            self.status.SetStatusText("下载已取消")
            self._log(f"下载已取消 (已下载 {info.get('success', 0)} 片)")
            self._pipeline = False
            return

        self.flow.set_step(1, ST_DONE, 100)
        self.status.SetStatusText("下载完成")
        self._log(f"下载完成: 成功 {info.get('success', 0)}, 失败 {info.get('fail', 0)}, "
                  f"跳过 {info.get('skip', 0)}, 无效 {info.get('invalid', 0)}, 重试 {info.get('retried', 0)}")

        if self._pipeline:
            self._log("开始拼接大图...")
            self.flow.set_step(2, ST_RUNNING, 0)
            self.flow.set_overall(50, "开始拼接...")
            # 主线程缓存控件取值，供拼接工作线程使用（wx 控件不能在子线程访问）
            self._merge_params = self._capture_merge_params()
            threading.Thread(target=self._thread_merge, daemon=True).start()
        else:
            self._set_busy(False)
            self._pipeline = False

    # ---- 拼接 ----
    def start_merge(self):
        if self._busy:
            return
        self._sync_core_modules()
        self._refresh_all()
        self._pipeline = False
        self._reset_flow_for("merge")
        # 在主线程缓存控件取值，供工作线程使用（wx 控件不能在子线程访问）
        self._merge_params = self._capture_merge_params()
        self._set_busy(True)
        self.status.SetStatusText("正在拼接大图...")
        self._log("开始拼接大图")
        threading.Thread(target=self._thread_merge, daemon=True).start()

    def _capture_merge_params(self):
        return {
            "use_rust": self.rust_chk.GetValue(),
            "mode": self.mode_radio.GetSelection(),  # 0=batch 1=streaming 2=lowmem
            "use_fast": self.save_radio.GetSelection() == 0,  # fast=True / optimize=False
            "batch_rows": self.batch_rows_ctrl.GetValue(),
            "rust_fmt": self.rust_fmt_radio.GetSelection(),  # 0=BigTIFF 1=PNG
        }

    def _thread_merge(self):
        params = getattr(self, "_merge_params", None)
        if params is None:
            wx.CallAfter(self._log, "警告: 拼接参数未缓存，使用默认参数(批量模式/快速保存)")
            params = {"use_rust": False, "mode": 0, "use_fast": True, "batch_rows": 10, "rust_fmt": 0}

        if params.get("use_rust"):
            self._thread_merge_rust(params)
        else:
            self._thread_merge_python(params)

    def _thread_merge_python(self, params):
        mode, use_fast, batch_rows = params["mode"], params["use_fast"], params["batch_rows"]

        def cb(info):
            wx.CallAfter(self._on_merge_progress, info)

        self._merge_start = time.time()
        try:
            if mode == 0:
                result = mt_mod.merge_tiles_ultra(use_fast_save=use_fast, batch_rows=batch_rows, progress_callback=cb)
            elif mode == 1:
                result = mt_mod.merge_tiles_streaming_v2(use_fast_save=use_fast, progress_callback=cb)
            else:
                result = mt_mod.merge_tiles_low_memory(use_fast_save=use_fast, progress_callback=cb)
            wx.CallAfter(self._on_merge_done, bool(result))
        except Exception as e:
            wx.CallAfter(self._log, f"拼接发生异常: {e}")
            wx.CallAfter(self._on_merge_done, False)

    def _thread_merge_rust(self, params):
        """调用 Rust 引擎（merge_rs）执行合并，解析 JSON 进度"""
        fmt = "tif" if params.get("rust_fmt", 0) == 0 else "png"
        if fmt == "tif":
            out = str(Path(cfg.OUTPUT_FILE).with_suffix(".tif"))
        else:
            out = cfg.OUTPUT_FILE
        bin_path = Path(__file__).resolve().parent / "merge_rs" / "target" / "release" / "merge_rs"
        if not bin_path.exists():
            wx.CallAfter(self._log, f"未找到 Rust 引擎: {bin_path}\n请先执行: cd merge_rs && cargo build --release")
            wx.CallAfter(self._on_merge_done, False)
            return
        cmd = [
            str(bin_path),
            "--tiles-dir", cfg.OUTPUT_DIR,
            "--matrix", str(cfg.TILE_MATRIX),
            "--col-start", str(cfg.TILE_COL_START), "--col-end", str(cfg.TILE_COL_END),
            "--row-start", str(cfg.TILE_ROW_START), "--row-end", str(cfg.TILE_ROW_END),
            "--out", out, "--format", fmt, "--progress-json",
        ]
        self._merge_start = time.time()
        wx.CallAfter(self._log, "运行 Rust 引擎: " + " ".join(cmd))
        self._merge_proc = None
        try:
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
            self._merge_proc = proc
            for line in proc.stderr:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    info = json.loads(line)
                except Exception:
                    continue
                if info.get("done"):
                    wx.CallAfter(self._on_merge_progress, {
                        "stage": "save", "percent": 100,
                        "message": f"写出完成 | 峰值内存 {info.get('peak_mb', 0):.0f} MB"
                    })
                    continue
                wx.CallAfter(self._on_merge_progress, {
                    "stage": "merge",
                    "percent": info.get("percent", 0),
                    "loaded": info.get("tiles", 0),
                    "missing": 0,
                })
            rc = proc.wait()
            self._merge_proc = None
            ok = (rc == 0) and os.path.exists(out)
            if not ok:
                wx.CallAfter(self._log, f"Rust 引擎退出码 {rc}")
            else:
                cfg.OUTPUT_FILE = out  # 让完成弹窗显示实际输出文件
            wx.CallAfter(self._on_merge_done, ok)
        except Exception as e:
            self._merge_proc = None
            wx.CallAfter(self._log, f"Rust 引擎运行异常: {e}")
            wx.CallAfter(self._on_merge_done, False)

    def _on_merge_progress(self, info):
        stage = info.get("stage", "")
        pct = info.get("percent", 0)
        self.mg_progress.SetValue(int(pct * 10))
        if stage == "scan":
            self.mg_stats.SetLabel(info.get("message", "扫描瓦片目录..."))
        elif stage == "merge":
            self.mg_stats.SetLabel(
                f"拼接中 {pct:.1f}% | 已拼接 {info.get('loaded', 0)} 片 | 缺失 {info.get('missing', 0)} 片"
            )
        elif stage == "save":
            self.mg_stats.SetLabel(info.get("message", "保存大图中..."))
        self.flow.set_step(2, ST_RUNNING, pct)
        if self._pipeline:
            self.flow.set_overall(50 + pct * 0.5, f"拼接大图 {pct:.1f}%")
        else:
            self.flow.set_overall(pct, f"拼接大图 {pct:.1f}%")
        self.status.SetStatusText(f"拼接中 {pct:.1f}%")

    def _on_merge_done(self, ok):
        self._set_busy(False)
        if ok:
            self.flow.set_step(2, ST_DONE, 100)
            self.flow.set_step(3, ST_DONE, 100)
            self.flow.set_overall(100, "任务完成")
            self.status.SetStatusText("任务完成")
            self._log("拼接完成，全部任务结束")
            if not self._pipeline:
                pass
            out = os.path.abspath(cfg.OUTPUT_FILE)
            if os.path.exists(out):
                size_mb = os.path.getsize(out) / (1024 * 1024)
                self.mg_stats.SetLabel(f"完成 | 输出 {out} | {size_mb:.2f} MB")
                self._log(f"拼接成功: {out} ({size_mb:.2f} MB)")
                wx.MessageBox(f"拼接完成！\n输出文件: {out}\n大小: {size_mb:.2f} MB", "完成",
                              wx.OK | wx.ICON_INFORMATION)
        else:
            self.flow.set_step(2, ST_FAILED)
            self.flow.set_step(3, ST_PENDING)
            self.flow.set_overall(0, "拼接失败")
            self.status.SetStatusText("拼接失败")
            self.mg_stats.SetLabel("状态: 失败")
            self._log("拼接失败")
        self._pipeline = False

    # ---- 停止 ----
    def stop_current(self):
        if self._downloader is not None:
            self._downloader.cancel_event.set()
            self._log("已请求停止下载，正在取消剩余任务...")
            self.status.SetStatusText("正在停止下载...")
        elif getattr(self, "_merge_proc", None) is not None:
            try:
                self._merge_proc.terminate()
            except Exception:
                pass
            self._log("已终止 Rust 合并进程")
        else:
            self._log("拼接过程不支持中断，请等待完成")

    # ================= 瓦片预览（自动加载 / 手动保存） =================
    def on_grid_click(self, col, row):
        self.preview.set_coords(cfg.TILE_MATRIX, col, row)
        self.on_preview_auto()

    def on_preview_auto(self):
        """坐标变化后的自动加载：本地已有则直接显示，缺失时自动联网（不保存）"""
        if self._busy:
            return
        self._sync_core_modules()
        m, c, r = self.preview._coords()
        if find_tile(m, c, r, cfg.OUTPUT_DIR):
            self.on_preview_local()
        else:
            self.on_preview_network()

    def on_preview_local(self):
        self._sync_core_modules()
        m, c, r = self.preview._coords()
        path = find_tile(m, c, r, cfg.OUTPUT_DIR)
        if not path:
            self.preview.show_error(f"本地未找到该瓦片:\n{tile_path(m, c, r, cfg.OUTPUT_DIR)}")
            self._log(f"本地预览: [{c},{r}] 不存在")
            return
        try:
            with open(path, "rb") as f:
                if f.read(8) != PNG_HEADER:
                    raise ValueError("文件不是有效 PNG")
            self.preview.show_image(path, "local")
            self._log(f"本地预览: [{c},{r}]")
        except Exception as e:
            self.preview.show_error(f"无法加载瓦片 [{c},{r}]: {e}")
            self._log(f"本地预览失败 [{c},{r}]: {e}")

    def on_preview_network(self):
        """从网络获取当前坐标的瓦片（仅预览，不自动保存）"""
        if self._busy:
            self.preview.show_error("任务运行中，暂不联网预览")
            return
        self._sync_core_modules()
        m, c, r = self.preview._coords()
        self._fetch_seq += 1
        self.preview.reset_pending()
        self.preview.set_fetching(True)
        self.preview.info_ctrl.SetValue(f"正在从网络获取 [{c},{r}] ...")
        threading.Thread(target=self._thread_fetch_tile, args=(m, c, r, self._fetch_seq), daemon=True).start()

    def _thread_fetch_tile(self, m, c, r, seq):
        url = cfg.get_tile_url(m, c, r)
        try:
            resp = httpx.get(url, headers=cfg.HEADERS, timeout=15)
            resp.raise_for_status()
            if not resp.content.startswith(PNG_HEADER):
                raise ValueError("响应内容不是有效 PNG 图片")

            def apply():
                # 丢弃过期结果：已发起新请求或坐标已变化
                if seq != self._fetch_seq:
                    return
                if self.preview._coords() != (m, c, r):
                    return
                self.preview.show_fetched(resp.content, m, c, r)
                self._log(f"网络预览 [{c},{r}] 完成 ({len(resp.content) / 1024:.1f} KB, 未保存)")
            wx.CallAfter(apply)
        except Exception as e:
            wx.CallAfter(self._log, f"网络预览失败 [{c},{r}]: {e}")
            wx.CallAfter(self.preview.show_error, f"网络获取失败 [{c},{r}]:\n{e}")
        finally:
            wx.CallAfter(self.preview.set_fetching, False)

    def on_save_tile(self):
        """手动保存当前预览的瓦片到输出目录"""
        content = self.preview.fetched_bytes
        if content is None:
            self._log("没有可保存的预览内容")
            return
        m, c, r = self.preview._coords()
        out_dir = cfg.OUTPUT_DIR
        path = tile_path(m, c, r, out_dir)
        try:
            ensure_tile_dir(m, r, out_dir)
            with open(path, "wb") as f:
                f.write(content)
            self.preview.mark_saved(path)
            self.grid.mark(c, r, "ok")
            self._log(f"瓦片已手动保存 [{c},{r}] -> {path}")
        except Exception as e:
            self.preview.show_error(f"保存失败 [{c},{r}]: {e}")
            self._log(f"保存瓦片失败 [{c},{r}]: {e}")

    # ================= 缓存管理 =================
    def open_cache_manager(self):
        """打开缓存管理对话框"""
        if self._busy:
            self._log("任务运行中，请稍后再管理缓存")
            return
        CacheManageDialog(self, cfg.OUTPUT_DIR).Show()

    def _thread_cache_op(self, dlg, op, *args):
        """后台执行缓存操作，避免阻塞界面"""
        def work():
            try:
                result = op(*args)
                wx.CallAfter(dlg.on_op_done, None, result)
            except Exception as e:
                wx.CallAfter(dlg.on_op_done, str(e), None)
        threading.Thread(target=work, daemon=True).start()

    # ================= 杂项 =================
    def _thread_scan(self):
        """启动时后台扫描已下载瓦片，避免阻塞界面"""
        time.sleep(0.2)
        arr = self.grid.scan_disk_status()
        wx.CallAfter(self.grid.apply_status, arr)

    def _log(self, msg):
        self.log_panel.log(msg)

    def _on_close(self, evt):
        self.preview.cancel_auto()
        if self._busy and self._downloader is not None:
            if wx.MessageBox("任务正在进行，确定要退出吗？", "确认退出",
                             wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
                evt.Veto()
                return
            self._downloader.cancel_event.set()
        self.Destroy()


def asyncio_run(coro):
    """在任意线程中运行 asyncio 协程"""
    import asyncio
    asyncio.run(coro)


class CacheManageDialog(wx.Dialog):
    """缓存管理对话框：统计 + 清理 + 迁移"""

    def __init__(self, parent, base_dir):
        super().__init__(parent, title="瓦片缓存管理", size=(600, 480))
        self.base = base_dir
        self._busy = False

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.stats_txt = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP
        )
        sizer.Add(self.stats_txt, 1, wx.EXPAND | wx.ALL, 8)

        # 清理条件
        cond = wx.BoxSizer(wx.HORIZONTAL)
        cond.Add(wx.StaticText(panel, label="级别:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.matrix_ctrl = wx.SpinCtrl(panel, min=0, max=30, initial=cfg.TILE_MATRIX)
        cond.Add(self.matrix_ctrl, 0, wx.RIGHT, 14)
        cond.Add(wx.StaticText(panel, label="N 天前:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.days_ctrl = wx.SpinCtrl(panel, min=1, max=3650, initial=30)
        cond.Add(self.days_ctrl, 0, wx.RIGHT, 14)
        cond.Add(wx.StaticText(panel, label="容量上限(MB):"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.maxsize_ctrl = wx.SpinCtrl(panel, min=1, max=100000, initial=500)
        cond.Add(self.maxsize_ctrl, 0)
        sizer.Add(cond, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # 按钮
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self._mk_btn(btns, panel, "刷新统计", self._on_refresh)
        self._mk_btn(btns, panel, "清理该级别", self._on_prune_matrix)
        self._mk_btn(btns, panel, "清理N天前", self._on_prune_old)
        self._mk_btn(btns, panel, "按容量清理", self._on_prune_size)
        self._mk_btn(btns, panel, "迁移旧结构", self._on_migrate)
        self._mk_btn(btns, panel, "清空缓存", self._on_clear)
        sizer.Add(btns, 0, wx.ALL, 8)

        self.result_txt = wx.StaticText(panel, label="")
        sizer.Add(self.result_txt, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(sizer)
        self.refresh_stats()

    def _mk_btn(self, sizer, parent, label, handler):
        b = wx.Button(parent, label=label)
        b.Bind(wx.EVT_BUTTON, handler)
        sizer.Add(b, 0, wx.RIGHT, 6)
        return b

    def _set_busy(self, busy, msg=""):
        self._busy = busy
        self.result_txt.SetLabel(msg)

    # ---------- 统计 ----------
    def refresh_stats(self):
        import io
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            tile_cache.print_stats(self.base)
        finally:
            sys.stdout = old
        self.stats_txt.SetValue(buf.getvalue())

    def on_op_done(self, err, result):
        """后台操作完成回调（主线程）"""
        self._set_busy(False)
        if err:
            self.result_txt.SetLabel(f"操作失败: {err}")
            return
        kind = getattr(self, "_op_kind", "prune")
        if not result:
            self.result_txt.SetLabel("完成（无变化）")
        elif kind == "migrate":
            moved, left = result
            extra = f"，目标已存在跳过 {left} 个" if left else ""
            self.result_txt.SetLabel(f"完成：迁移 {moved} 个瓦片{extra}")
        elif kind == "clear":
            removed, freed = result
            self.result_txt.SetLabel(
                f"完成：清空 {removed} 项，释放 {freed / (1024 * 1024):.1f} MB"
            )
        else:
            removed, freed = result
            self.result_txt.SetLabel(
                f"完成：删除 {removed} 个瓦片，释放 {freed / (1024 * 1024):.1f} MB"
            )
        self.refresh_stats()

    def _run(self, op, kind, *args):
        if self._busy:
            return
        self._op_kind = kind
        self._set_busy(True, "处理中...")
        frame = self.GetParent()
        if hasattr(frame, "_thread_cache_op"):
            frame._thread_cache_op(self, op, self.base, *args)
        else:
            try:
                result = op(self.base, *args)
                self.on_op_done(None, result)
            except Exception as e:
                self.on_op_done(str(e), None)

    # ---------- 操作 ----------
    def _on_refresh(self, _evt):
        self.refresh_stats()

    def _on_prune_matrix(self, _evt):
        m = self.matrix_ctrl.GetValue()
        if wx.MessageBox(f"删除级别 {m} 的全部瓦片？", "确认清理",
                         wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            self._run(tile_cache.prune_matrix, "prune", m)

    def _on_prune_old(self, _evt):
        days = self.days_ctrl.GetValue()
        if wx.MessageBox(f"删除 {days} 天前下载的所有瓦片？", "确认清理",
                         wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            self._run(tile_cache.prune_older_than, "prune", days)

    def _on_prune_size(self, _evt):
        mb = self.maxsize_ctrl.GetValue()
        if wx.MessageBox(f"按最旧优先删除，直到缓存 ≤ {mb} MB？", "确认清理",
                         wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            self._run(tile_cache.prune_to_max_size, "prune", mb)

    def _on_migrate(self, _evt):
        if wx.MessageBox("把旧扁平结构瓦片迁移到分级目录？", "确认迁移",
                         wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            self._run(tile_cache.migrate, "migrate")

    def _on_clear(self, _evt):
        if wx.MessageBox("清空全部缓存瓦片？此操作不可恢复！", "确认清空",
                         wx.YES_NO | wx.ICON_WARNING) == wx.YES:
            self._run(tile_cache.clear_cache, "clear")


def main():
    app = wx.App(False)
    frame = WMTSFrame()
    frame.Show()
    app.MainLoop()


if __name__ == "__main__":
    main()
