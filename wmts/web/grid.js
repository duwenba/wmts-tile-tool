/* grid.js — 下载状态网格（Canvas，支持缩放/平移/点击），数据为 2bit/片的位图 */
"use strict";

const TILE_COLORS = ["#3a3d45", "#43a047", "#e53935"]; // 缺失 / 已下载 / 损坏

class StatusGrid {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onTileClick = opts.onTileClick || (() => {});
    this.cols = 0; this.rows = 0;
    this.colStart = 0; this.rowStart = 0;
    this.data = null;          // Uint8Array，2bit/片
    this.scale = 1; this.ox = 0; this.oy = 0; // 视图变换（像素坐标 → 画布）
    this._drag = null;
    this._hover = null;
    this._raf = 0;
    this._bind();
    this._resizeObserver();
  }

  _resizeObserver() {
    const ro = new ResizeObserver(() => { this._syncSize(); this.render(); });
    ro.observe(this.canvas);
    this._syncSize();
  }

  _syncSize() {
    const r = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.max(1, Math.round(r.width * dpr));
    this.canvas.height = Math.max(1, Math.round(r.height * dpr));
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.vw = r.width; this.vh = r.height;
  }

  /** 设置范围并加载状态（调用后端） */
  async setRange(matrix, cs, ce, rs, re) {
    this.matrix = matrix;
    this.colStart = cs; this.rowStart = rs;
    this.cols = ce - cs + 1; this.rows = re - rs + 1;
    const r = await api(`/api/tiles/status?matrix=${matrix}&col_start=${cs}&col_end=${ce}&row_start=${rs}&row_end=${re}`);
    this.setStatus(r.status_b64, r.counts);
    this.fitView();
  }

  setStatus(b64, counts) {
    const bin = atob(b64);
    this.data = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) this.data[i] = bin.charCodeAt(i);
    this.counts = counts;
  }

  /** 运行中增量更新某个瓦片状态 */
  mark(col, row, result) {
    if (!this.data) return;
    const ci = col - this.colStart, ri = row - this.rowStart;
    if (ci < 0 || ri < 0 || ci >= this.cols || ri >= this.rows) return;
    const idx = ri * this.cols + ci;
    const status = result === "failed" ? 2 : (result === "skipped" || result === "success" ? 1 : 0);
    this.data[idx >> 2] |= status << ((idx & 3) * 2);
    if (this.counts) {
      this.counts.missing++; this.counts[status === 1 ? "ok" : "invalid"]++;
      if (this.counts.missing > 0) this.counts.missing--;
    }
  }

  statusAt(col, row) {
    if (!this.data) return 0;
    const ci = col - this.colStart, ri = row - this.rowStart;
    if (ci < 0 || ri < 0 || ci >= this.cols || ri >= this.rows) return 0;
    const idx = ri * this.cols + ci;
    return (this.data[idx >> 2] >> ((idx & 3) * 2)) & 3;
  }

  fitView() {
    if (!this.cols || !this.rows) return;
    const pad = 12;
    this.scale = Math.min((this.vw - pad * 2) / this.cols, (this.vh - pad * 2) / this.rows);
    this.scale = Math.max(1, this.scale);
    this.ox = (this.vw - this.cols * this.scale) / 2;
    this.oy = (this.vh - this.rows * this.scale) / 2;
    this.render();
  }

  render() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this._draw(); });
  }

  _draw() {
    const { ctx, vw, vh } = this;
    ctx.save();
    ctx.clearRect(0, 0, vw, vh);
    if (!this.cols || !this.rows) { ctx.restore(); return; }
    const s = this.scale;
    // 可见范围（视口裁剪，超大网格只画可见部分）
    const c0 = Math.max(0, Math.floor(-this.ox / s));
    const c1 = Math.min(this.cols, Math.ceil((vw - this.ox) / s));
    const r0 = Math.max(0, Math.floor(-this.oy / s));
    const r1 = Math.min(this.rows, Math.ceil((vh - this.oy) / s));
    for (let ri = r0; ri < r1; ri++) {
      for (let ci = c0; ci < c1; ci++) {
        const idx = ri * this.cols + ci;
        const st = (this.data[idx >> 2] >> ((idx & 3) * 2)) & 3;
        ctx.fillStyle = TILE_COLORS[st];
        ctx.fillRect(this.ox + ci * s, this.oy + ri * s, Math.ceil(s), Math.ceil(s));
      }
    }
    // 网格线（放大到足够大时）
    if (s >= 6) {
      ctx.strokeStyle = "rgba(255,255,255,.10)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let ci = c0; ci <= c1; ci++) {
        const x = this.ox + ci * s;
        ctx.moveTo(x, this.oy + r0 * s); ctx.lineTo(x, this.oy + r1 * s);
      }
      for (let ri = r0; ri <= r1; ri++) {
        const y = this.oy + ri * s;
        ctx.moveTo(this.ox + c0 * s, y); ctx.lineTo(this.ox + c1 * s, y);
      }
      ctx.stroke();
    }
    // 悬停高亮
    if (this._hover) {
      const { col, row } = this._hover;
      ctx.strokeStyle = "#8ab4f8"; ctx.lineWidth = 2;
      ctx.strokeRect(this.ox + (col - this.colStart) * s, this.oy + (row - this.rowStart) * s, s, s);
    }
    ctx.restore();
  }

  _pos(e) {
    const r = this.canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  _tileAt(x, y) {
    const s = this.scale;
    const ci = Math.floor((x - this.ox) / s), ri = Math.floor((y - this.oy) / s);
    if (ci < 0 || ri < 0 || ci >= this.cols || ri >= this.rows) return null;
    return { col: this.colStart + ci, row: this.rowStart + ri };
  }

  _zoom(factor, px, py) {
    const s2 = Math.min(64, Math.max(0.5, this.scale * factor));
    const k = s2 / this.scale;
    this.ox = px - (px - this.ox) * k;
    this.oy = py - (py - this.oy) * k;
    this.scale = s2;
    this.render();
  }

  _bind() {
    const c = this.canvas;
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const p = this._pos(e);
      this._zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, p.x, p.y);
    }, { passive: false });

    c.addEventListener("mousedown", (e) => {
      const p = this._pos(e);
      this._drag = { x0: p.x, y0: p.y, ox: this.ox, oy: this.oy, moved: false, btn: e.button };
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!this._drag) {
        const p = this._pos(e);
        this._hover = this._tileAt(p.x, p.y);
        this.render();
        return;
      }
      const p = this._pos(e);
      const dx = p.x - this._drag.x0, dy = p.y - this._drag.y0;
      if (Math.abs(dx) + Math.abs(dy) > 4) this._drag.moved = true;
      if (this._drag.moved) {
        if (this._drag.btn === 2 || e.buttons & 4) { // 右/中键拖拽平移
          this.ox = this._drag.ox + dx; this.oy = this._drag.oy + dy;
        } else { // 左键拖拽 = 平移（网格）
          this.ox = this._drag.ox + dx; this.oy = this._drag.oy + dy;
        }
        this.render();
      }
    });
    window.addEventListener("mouseup", (e) => {
      if (!this._drag) return;
      const d = this._drag; this._drag = null;
      if (!d.moved && d.btn === 0) {
        const p = this._pos(e);
        const t = this._tileAt(p.x, p.y);
        if (t) this.onTileClick(t.col, t.row);
      }
    });
    c.addEventListener("mouseleave", () => { this._hover = null; this.render(); });
    c.addEventListener("contextmenu", (e) => e.preventDefault());
    c.addEventListener("dblclick", () => this.fitView());
  }
}
