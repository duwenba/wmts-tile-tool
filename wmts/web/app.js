/* app.js — 前端主控：配置 / 地图工作台 / 任务与 SSE / 抽屉 / 状态条 */
"use strict";

/* ================= 基础工具 ================= */

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { msg = (await resp.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return resp.json();
}

let toastTimer = 0;
function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

function fmtBytes(n) {
  if (n == null) return "-";
  if (n > 1 << 30) return (n / (1 << 30)).toFixed(2) + " GB";
  if (n > 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n > 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

const $ = (id) => document.getElementById(id);

/* ================= 全局状态 ================= */

const state = {
  config: null,
  meta: null,
  busy: false,
  taskId: null,
  es: null,        // 任务 SSE
  logEs: null,     // 日志 SSE
  map: null,       // MapWorkbench
  pendingRange: null,
  pendingBBox: null,
  popTile: null,
  overallKey: null, // 进度单调化：当前任务标识
  overallMax: 0,    // 进度单调化：本任务历史最高百分比
};

const CFG_FIELDS = [
  "base_url", "layer", "style", "tilematrixset", "tile_matrix", "epsg",
  "col_start", "col_end", "row_start", "row_end",
  "output_dir", "output_file", "max_workers", "timeout",
];

/* ================= 地图回调 ================= */

function onCursor(lon, lat, info) {
  $("ro-lon").textContent = lon.toFixed(6);
  $("ro-lat").textContent = lat.toFixed(6);
  $("ro-tile").textContent = info ? `${info.level}/${info.col}/${info.row}` : "—";
  $("ro-res").textContent = info ? `${info.resolution.toExponential(3)} °/px` : "—";
}

function onLevelChange(level) {
  if (level != null) $("tb-level").value = String(level);
}

async function onMapSelect(bbox) {
  state.pendingBBox = bbox;
  const sel = $("sel-level");
  if (state.meta) {
    sel.innerHTML = "";
    for (let lv = state.meta.start_level; lv <= state.meta.end_level; lv++) {
      sel.add(new Option(`级别 ${lv}`, lv));
    }
    sel.value = String(state.map.level);
  }
  await recomputeSelection();
}

/** 按「经纬度范围 + 下载级别」重算需要下载的瓦片范围。 */
async function recomputeSelection() {
  const bbox = state.pendingBBox;
  if (!bbox) return;
  const matrix = +$("sel-level").value;
  try {
    const r = await api("/api/grid/from-bbox",
      { method: "POST", body: { bbox, matrix } });
    state.pendingRange = r;
    $("sel-bbox").textContent =
      `${bbox[0].toFixed(5)}, ${bbox[1].toFixed(5)} ~ ` +
      `${bbox[2].toFixed(5)}, ${bbox[3].toFixed(5)}`;
    $("sel-text").innerHTML =
      `级别 ${r.matrix}：<b>${r.cols}</b> 列 × <b>${r.rows}</b> 行 = ` +
      `<b>${r.total.toLocaleString()}</b> 片 · 约 ${fmtBytes(r.estimated_bytes)}`;
    $("selection-bar").hidden = false;
  } catch (e) {
    toast("换算失败: " + e.message, true);
  }
}

function hideSelectionBar() {
  $("selection-bar").hidden = true;
  state.pendingRange = null;
  state.pendingBBox = null;
}

/** 把地图缩放到当前下载范围并开启状态叠加。 */
function focusDownloadRange() {
  if (!state.config || !state.map || !state.map.meta) {
    toast("图层元数据未就绪", true);
    return;
  }
  const m = +state.config.tile_matrix;
  const bbox = state.map.rangeBBox(m, +state.config.col_start, +state.config.col_end,
    +state.config.row_start, +state.config.row_end);
  state.map.fitBBox(bbox);
  if (!$("tb-status").checked) {
    $("tb-status").checked = true;
    state.map.setStatus(true);
  }
  toast(`已定位到下载范围（级别 ${m}）`);
}

async function applySelection() {
  const r = state.pendingRange;
  if (!r) return;
  try {
    const resp = await api("/api/config", { method: "PUT", body: {
      tile_matrix: r.matrix,
      col_start: r.col_start, col_end: r.col_end,
      row_start: r.row_start, row_end: r.row_end,
    }});
    state.config = resp.config;
    fillConfigForm(state.config);
    updateDownloadRangeRect();
    hideSelectionBar();
    state.map.clearSelection();
    toast("已保存为下载范围");
  } catch (e) { toast("应用失败: " + e.message, true); }
}

/* ================= 瓦片气泡 ================= */

let popPreviewUrl = null;

async function onTileClick(level, col, row) {
  const pop = $("tile-pop");
  $("pop-title").textContent = `${level}/${col}/${row}`;
  $("pop-meta").textContent = "加载中…";
  $("pop-img").style.display = "none";
  $("pop-save").disabled = true;
  state.popTile = null;
  pop.hidden = false;
  try {
    const resp = await fetch(`/api/tiles/${level}/${col}/${row}?source=auto`);
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try { detail = (await resp.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    const src = resp.headers.get("X-Tile-Source") || "auto";
    const blob = await resp.blob();
    if (popPreviewUrl) URL.revokeObjectURL(popPreviewUrl);
    popPreviewUrl = URL.createObjectURL(blob);
    $("pop-img").src = popPreviewUrl;
    $("pop-img").style.display = "block";
    const b = state.map.tileBounds(level, col, row);
    $("pop-meta").innerHTML =
      `来源：${src === "local" ? "本地缓存" : "网络"} · ${fmtBytes(blob.size)}<br>` +
      `范围：${b.west.toFixed(5)}, ${b.south.toFixed(5)} ~ ` +
      `${b.east.toFixed(5)}, ${b.north.toFixed(5)}`;
    state.popTile = { level, col, row };
    $("pop-save").disabled = src === "local";
  } catch (e) {
    $("pop-img").style.display = "none";
    $("pop-meta").textContent = "加载失败：" + e.message;
  }
}

async function popSave() {
  const t = state.popTile;
  if (!t) return;
  try {
    const resp = await api(`/api/tiles/${t.level}/${t.col}/${t.row}/save`, { method: "POST" });
    toast(`已保存 ${fmtBytes(resp.size)} → ${resp.path}`);
    $("pop-save").disabled = true;
    state.map.markTile(t.level, t.col, t.row, "success");
    refreshCache();
  } catch (e) { toast("保存失败: " + e.message, true); }
}

function popZoom() {
  const t = state.popTile;
  if (!t) return;
  const b = state.map.tileBounds(t.level, t.col, t.row);
  state.map.centerOn(t.level, (b.west + b.east) / 2, (b.north + b.south) / 2);
  $("tile-pop").hidden = true;
}

/* ================= 配置 ================= */

function readConfigForm() {
  const body = {};
  for (const f of CFG_FIELDS) body[f] = $(`c-${f}`).value;
  const cookie = $("c-cookie").value.trim();
  if (cookie) body.headers = { Cookie: cookie };
  body.auto_geo = $("c-auto_geo").checked;
  return body;
}

function fillConfigForm(cfg) {
  for (const f of CFG_FIELDS) $(`c-${f}`).value = cfg[f] ?? "";
  $("c-cookie").value = "";
  const shown = cfg.headers?.Cookie;
  $("c-cookie").placeholder = shown
    ? `当前: ${shown}（不修改请留空）` : "未设置 Cookie";
  $("c-auto_geo").checked = !!cfg.auto_geo;
  updateRangeSummary();
}

function updateRangeSummary() {
  const cs = +$("c-col_start").value, ce = +$("c-col_end").value;
  const rs = +$("c-row_start").value, re = +$("c-row_end").value;
  const m = +$("c-tile_matrix").value;
  const box = $("range-summary"), hint = $("cfg-hint");
  hint.textContent = "";
  if ([cs, ce, rs, re, m].some(Number.isNaN) || ce < cs || re < rs) {
    box.className = "summary invalid";
    box.textContent = "范围无效：结束值不能小于起始值";
    return;
  }
  const total = (ce - cs + 1) * (re - rs + 1);
  box.className = "summary";
  box.innerHTML = `级别 <b>${m}</b>：<b>${ce - cs + 1}</b> 列 × ` +
    `<b>${re - rs + 1}</b> 行 = <b>${total.toLocaleString()}</b> 片 · ` +
    `约 ${fmtBytes(total * 18 * 1024)}`;
}

async function saveConfig() {
  const prev = state.config;
  try {
    const r = await api("/api/config", { method: "PUT", body: readConfigForm() });
    state.config = r.config;
    fillConfigForm(r.config);
    updateDownloadRangeRect();
    toast(r.errors.length ? `已保存，但有警告：${r.errors.join("；")}` : "配置已保存",
      r.errors.length > 0);
    // 数据源或图层变化时重新拉取元数据并缩放地图
    if (!prev || prev.layer !== r.config.layer || prev.base_url !== r.config.base_url) {
      await reloadLayer(true);
    }
  } catch (e) { toast("保存失败: " + e.message, true); }
}

/** 重新拉取图层元数据并刷新地图 / 级别选择器 / 下载范围框。 */
async function reloadLayer(force = false) {
  try {
    state.meta = await state.map.loadMeta(force);
  } catch (e) {
    toast("图层元数据获取失败: " + e.message, true);
    return false;
  }
  const sel = $("tb-level");
  sel.innerHTML = "";
  for (let lv = state.meta.start_level; lv <= state.meta.end_level; lv++) {
    sel.add(new Option(`级别 ${lv}`, lv));
  }
  $("sb-layer").textContent = `图层 ${state.meta.layer}`;
  state.map.fitLayer();
  updateDownloadRangeRect();
  return true;
}

async function resetConfig() {
  try {
    const r = await api("/api/config/reset", { method: "POST" });
    state.config = r.config;
    fillConfigForm(r.config);
    updateDownloadRangeRect();
    toast("已恢复默认配置");
  } catch (e) { toast("失败: " + e.message, true); }
}

function applyLayerRange() {
  if (!state.map || !state.meta) { toast("图层元数据未就绪", true); return; }
  const m = +$("c-tile_matrix").value;
  if (Number.isNaN(m)) { toast("请先填写级别", true); return; }
  const s = state.map.tileSpan(m);
  const [xmin, ymin, xmax, ymax] = state.meta.bbox;
  const c0 = Math.floor((xmin - state.meta.origin_x) / s);
  const c1 = Math.floor((xmax - state.meta.origin_x) / s);
  const r0 = Math.floor((state.meta.origin_y - ymax) / s);
  const r1 = Math.floor((state.meta.origin_y - ymin) / s);
  $("c-col_start").value = c0; $("c-col_end").value = c1;
  $("c-row_start").value = r0; $("c-row_end").value = r1;
  updateRangeSummary();
  toast(`已填入整个图层范围（${c1 - c0 + 1} × ${r1 - r0 + 1} 片）`);
}

function applyViewRange() {
  if (!state.map || state.map.level == null) return;
  const vr = state.map.viewTileRange();
  const lr = state.map.layerTileRange(state.map.level);
  const c0 = Math.max(vr.c0, lr.c0), c1 = Math.min(vr.c1, lr.c1);
  const r0 = Math.max(vr.r0, lr.r0), r1 = Math.min(vr.r1, lr.r1);
  if (c1 < c0 || r1 < r0) { toast("当前视图不在图层范围内", true); return; }
  $("c-tile_matrix").value = state.map.level;
  $("c-col_start").value = c0; $("c-col_end").value = c1;
  $("c-row_start").value = r0; $("c-row_end").value = r1;
  updateRangeSummary();
  toast("已填入当前视图范围");
}

function updateDownloadRangeRect() {
  const cfg = state.config;
  if (!cfg || !state.map || state.map.meta == null) return;
  const m = +cfg.tile_matrix;
  if (Number.isNaN(m)) return;
  state.map.setDownloadRange(m, +cfg.col_start, +cfg.col_end,
    +cfg.row_start, +cfg.row_end);
}

/* ================= 任务流水线 ================= */

const STEP_OF_STAGE = { download: 1, merge: 2, geo: 3 };

function setPipelineSteps(fn) {
  document.querySelectorAll("#pipeline li").forEach((li, i) => {
    li.className = fn(i) || "";
  });
}

function resetPipeline(type) {
  const upto = type === "merge" ? 1 : type === "geo" ? 2 : 0;
  setPipelineSteps((i) => (i === 0 || (i <= upto && i > 0)) ? "done" : "");
  setOverall(0, "准备中…");
}

/**
 * 写入总进度。同一任务内只增不减：SSE 重连会回放缓冲、流水线各阶段也各自从
 * 0 起跳，直接回写会让进度条前后抖动。note 为阶段名或状态说明。
 */
function setOverall(pct, note) {
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  const key = state.taskId ?? "idle";
  if (key !== state.overallKey) { state.overallKey = key; state.overallMax = 0; }
  if (v > state.overallMax) state.overallMax = v;
  const shown = state.overallMax;
  $("overall-bar").style.width = `${shown}%`;
  $("overall-text").textContent = note
    ? `${note}（${shown.toFixed(1)}%）`
    : `${shown.toFixed(1)}%`;
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ["btn-pipeline", "btn-download", "btn-merge", "btn-geo"])
    $(id).disabled = busy;
  $("btn-stop").disabled = !busy;
  const chip = $("sb-task");
  chip.className = "chip" + (busy ? " busy" : "");
  if (!busy) chip.textContent = "待命";
}

function maybeAuthBanner(msg) {
  if (msg && /鉴权|Cookie|权限|401|403|405/i.test(msg)) $("auth-banner").hidden = false;
}

function handleTaskEvent(ev) {
  if (ev.task_id && ev.task_id !== state.taskId) return;
  const type = ev.type;

  if ((type === "progress" || type === "state" || type === "error" || type === "done")
      && ev.message) {
    appendLog(ev.message, type === "error" ? "err" : null);
  }

  if (type === "progress") {
    const stage = ev.stage, pct = ev.percent ?? 0;
    if (stage === "pipeline") {
      setOverall(pct, "总进度");
    } else {
      const name = { download: "下载", merge: "拼接", geo: "地理标签" }[stage] || stage;
      setOverall(pct, name);
    }
    if (stage === "download") {
      $("dl-bar").style.width = pct + "%";
      $("dl-percent").textContent = `${pct.toFixed(1)}% (${ev.done ?? 0}/${ev.total ?? 0})`;
      const c = ev.counters || {};
      const speed = ev.speed ? ` · ${ev.speed.toFixed(1)} 片/秒` : "";
      const eta = ev.eta ? ` · 剩余 ${Math.ceil(ev.eta)}s` : "";
      $("dl-stats").textContent =
        `总计 ${ev.total} · 成功 ${c.success ?? 0} · 失败 ${c.fail ?? 0} · ` +
        `跳过 ${c.skip ?? 0} · 无效 ${c.invalid ?? 0}${speed}${eta}`;
      if (ev.tile && state.config) {
        state.map.markTile(+state.config.tile_matrix, ev.tile.col, ev.tile.row, ev.tile.result);
      }
      setPipelineSteps((i) => i === 0 ? "done" : i === 1 ? "running" : "");
    } else if (stage === "merge") {
      $("mg-bar").style.width = pct + "%";
      $("mg-percent").textContent = pct.toFixed(1) + "%";
      $("mg-stats").textContent = ev.message || "";
      setPipelineSteps((i) => i <= 1 ? "done" : i === 2 ? "running" : "");
    } else if (stage === "geo") {
      setPipelineSteps((i) => i <= 2 ? "done" : i === 3 ? "running" : "");
    }
  } else if (type === "state") {
    if (ev.state === "failed" || ev.state === "cancelled") {
      const step = STEP_OF_STAGE[ev.stage];
      setPipelineSteps((i) => (i === 0 || (step && i < step)) ? "done"
        : i === step ? "failed" : "");
      setOverall(state.overallMax, ev.message || ev.state);
    }
    maybeAuthBanner(ev.message);
  } else if (type === "error") {
    maybeAuthBanner(ev.message);
  } else if (type === "done") {
    setBusy(false);
    if (ev.state === "done") {
      setPipelineSteps(() => "done");
      state.overallMax = 100;
      setOverall(100, "任务完成");
      toast("任务完成");
    } else {
      setOverall(state.overallMax, ev.message || ev.state);
    }
    if (state.es) { state.es.close(); state.es = null; }
    state.taskId = null;
    $("sb-task").textContent = "待命";
    refreshAfterTask();
  }
}

function startTask(type, params = {}) {
  return api("/api/tasks", { method: "POST", body: { type, params } })
    .then((task) => {
      state.taskId = task.id;
      $("sb-task").textContent = `任务 ${task.id}（${type}）`;
      setBusy(true);
      resetPipeline(type);
      if (type === "download" || type === "pipeline" || type === "retry_failed") {
        $("dl-bar").style.width = "0%";
        $("dl-stats").textContent = "准备中…";
      }
      if (type === "merge" || type === "pipeline") {
        $("mg-bar").style.width = "0%";
        $("mg-stats").textContent = "准备中…";
      }
      subscribeTask(task.id);
      return task;
    })
    .catch((e) => { toast(e.message, true); throw e; });
}

function subscribeTask(taskId) {
  if (state.es) state.es.close();
  const es = new EventSource(`/api/tasks/${taskId}/events`);
  state.es = es;
  es.onmessage = (m) => {
    try { handleTaskEvent(JSON.parse(m.data)); } catch (e) { console.error(e); }
  };
  es.onerror = () => { /* EventSource 自动重连；任务结束后由 done 事件关闭 */ };
}

async function refreshAfterTask() {
  state.map.reloadStatus();
  await Promise.allSettled([refreshCache(), refreshOutputs(), refreshResume()]);
}

function mergeParams() {
  const fmt = document.querySelector('input[name="mg-fmt"]:checked').value;
  const threads = +$("mg-threads").value || 0;
  const level = +$("mg-level").value;
  const p = { format: fmt };
  if (threads > 0) p.threads = threads;
  if (Number.isFinite(level)) p.level = level;
  return p;
}

/* ================= 断点续传 ================= */

async function refreshResume() {
  try {
    const r = await api("/api/download/progress");
    const parts = [];
    if (r.progress_file) parts.push(`断点进度 <b>${r.done_lines}</b> 片`);
    if (r.failed_file) {
      parts.push(`失败列表 <b>${r.failed_count ?? (r.failed || []).length}</b> 片`);
    }
    const el = $("dl-resume");
    if (!parts.length) { el.innerHTML = ""; return; }
    el.innerHTML = parts.join(" · ") +
      ` <button id="dl-retry" class="btn ghost">重试失败瓦片</button>`;
    const btn = $("dl-retry");
    if (btn) btn.addEventListener("click", () => startTask("retry_failed").catch(() => {}));
  } catch { $("dl-resume").textContent = ""; }
}

/* ================= 缓存 ================= */

async function refreshCache() {
  try {
    const r = await api("/api/cache/stats");
    const tb = document.querySelector("#ca-table tbody");
    tb.innerHTML = "";
    const layers = Object.keys(r.per_layer).sort();
    if (!layers.length) {
      tb.innerHTML = `<tr><td colspan="7" class="empty">缓存为空（${r.base}）</td></tr>`;
    }
    for (const label of layers) {
      const isCurrent = label === r.current_layer;
      for (const m of Object.keys(r.per_layer[label]).sort((a, b) => a - b)) {
        const v = r.per_layer[label][m];
        tb.insertAdjacentHTML("beforeend", `<tr>
          <td>${label}${isCurrent ? " ★" : ""}</td>
          <td class="num">${m}</td><td class="num">${v.tiles.toLocaleString()}</td>
          <td class="num">${fmtBytes(v.size)}</td>
          <td class="num">${v.min_col ?? "-"} ~ ${v.max_col ?? "-"}</td>
          <td class="num">${v.min_row ?? "-"} ~ ${v.max_row ?? "-"}</td>
          <td class="num">${v.rows}</td></tr>`);
      }
    }
    $("ca-hint").textContent =
      `总计 ${r.total_tiles.toLocaleString()} 片 / ${fmtBytes(r.total_bytes)} · ` +
      `当前图层 ${r.current_layer} ★（清理默认只作用于它）`;
    $("sb-cache").textContent =
      `缓存 ${r.total_tiles.toLocaleString()} 片 / ${fmtBytes(r.total_bytes)}`;
  } catch (e) { $("ca-hint").textContent = "统计失败: " + e.message; }
}

async function cachePrune() {
  const body = {};
  if ($("ca-matrix").value) body.matrix = +$("ca-matrix").value;
  if ($("ca-older").value) body.older_than = +$("ca-older").value;
  if ($("ca-maxsize").value) body.max_size = +$("ca-maxsize").value;
  for (const [k, id] of [["col_start", "ca-cs"], ["col_end", "ca-ce"],
                         ["row_start", "ca-rs"], ["row_end", "ca-re"]]) {
    if ($(id).value) body[k] = +$(id).value;
  }
  if (!Object.keys(body).length) { toast("请至少填写一个清理条件", true); return; }
  if (!confirm("确认按所选条件删除瓦片？此操作不可恢复。")) return;
  try {
    const r = await api("/api/cache/prune", { method: "POST", body });
    toast(`已删除 ${r.removed} 片，释放 ${fmtBytes(r.freed_bytes)}`);
    refreshCache();
    state.map.reloadStatus();
  } catch (e) { toast("清理失败: " + e.message, true); }
}

async function cacheClear() {
  if (!confirm("将删除缓存目录下的全部瓦片，确认？")) return;
  if (!confirm("再次确认：真的要清空缓存吗？")) return;
  try {
    const r = await api("/api/cache/clear", { method: "POST", body: { confirm: true } });
    toast(`已清空：删除 ${r.removed} 项，释放 ${fmtBytes(r.freed_bytes)}`);
    refreshCache();
    state.map.refresh();
  } catch (e) { toast("失败: " + e.message, true); }
}

async function migrateCache() {
  try {
    const r = await api("/api/cache/migrate", { method: "POST", body: {} });
    const extra = r.skipped ? `，跳过 ${r.skipped} 个` : "";
    const left = r.legacy_left ? `，旧位置仍剩 ${r.legacy_left} 个` : "";
    toast(`迁移到图层 ${r.layer}：移动 ${r.moved} 个${extra}${left}`);
    refreshCache();
    state.map.reloadStatus();
  } catch (e) { toast("失败: " + e.message, true); }
}

async function verifyCache() {
  try {
    const r = await api("/api/cache/verify", { method: "POST", body: {} });
    toast(r.bad_count ? `校验完成：${r.total} 片中有 ${r.bad_count} 片损坏`
      : `校验完成：${r.total} 片全部有效`, r.bad_count > 0);
  } catch (e) { toast("失败: " + e.message, true); }
}

/* ================= 输出 ================= */

async function refreshOutputs() {
  try {
    const r = await api("/api/outputs");
    const box = $("out-list");
    box.innerHTML = "";
    if (!r.outputs.length) {
      box.innerHTML = `<div class="empty">暂无输出文件，完成一次拼接后在这里下载</div>`;
      return;
    }
    for (const o of r.outputs) {
      const badge = o.geo_attached == null
        ? `<span class="badge unk">地理标签?</span>`
        : o.geo_attached ? `<span class="badge ok">已配准</span>`
          : `<span class="badge bad">未配准</span>`;
      box.insertAdjacentHTML("beforeend", `<div class="out-item">
        <span class="name" title="${o.name}">${o.name}</span>
        ${badge}
        <span class="meta">${fmtBytes(o.size)} · ${fmtTime(o.mtime)}</span>
        <a class="btn" href="/api/outputs/${encodeURIComponent(o.name)}" download>下载</a>
      </div>`);
    }
  } catch (e) { console.warn(e); }
}

/* ================= 日志 ================= */

function appendLog(msg, cls = null) {
  const el = $("log-view");
  const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const line = document.createElement("div");
  if (cls) line.className = "log-" + cls;
  else if (/失败|错误|error/i.test(msg)) line.className = "log-err";
  else if (/完成|成功/.test(msg)) line.className = "log-ok";
  line.textContent = `[${time}] ${msg}`;
  el.appendChild(line);
  while (el.childNodes.length > 2000) el.removeChild(el.firstChild);
  if ($("log-follow").checked) el.scrollTop = el.scrollHeight;
}

async function loadLogHistory() {
  try {
    const r = await api("/api/logs?limit=200");
    for (const ev of r.logs) appendLog(ev.message || "");
  } catch { /* 忽略 */ }
}

function subscribeLogs() {
  if (state.logEs) state.logEs.close();
  const es = new EventSource("/api/logs/stream");
  state.logEs = es;
  es.onmessage = (m) => {
    try {
      const ev = JSON.parse(m.data);
      if (ev.message) appendLog(ev.message);
    } catch { /* 忽略 */ }
  };
}

function saveLogs() {
  const text = $("log-view").innerText;
  const blob = new Blob([text], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `wmts-log-${Date.now()}.txt`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ================= 抽屉 ================= */

function openDrawer(which) {
  $("drawer").hidden = false;
  const isCache = which === "cache";
  $("drawer-cache-view").hidden = !isCache;
  $("drawer-logs-view").hidden = isCache;
  $("drawer-title").textContent = isCache ? "缓存管理" : "运行日志";
  if (isCache) refreshCache();
  else setTimeout(() => { $("log-view").scrollTop = $("log-view").scrollHeight; }, 0);
}

function closeDrawer() { $("drawer").hidden = true; }

/* ================= 工具切换 / 定位 ================= */

function setTool(t) {
  state.map.setTool(t);
  $("tb-pan").classList.toggle("active", t === "pan");
  $("tb-select").classList.toggle("active", t === "select");
}

function gotoFromInputs() {
  const lon = Number($("goto-lon").value);
  const lat = Number($("goto-lat").value);
  if (!Number.isFinite(lon) || !Number.isFinite(lat)
      || lon < -180 || lon > 180 || lat < -90 || lat > 90) {
    toast("请输入有效的经纬度", true);
    return;
  }
  state.map.centerOn(state.map.level, lon, lat);
}

/* ================= 事件绑定与初始化 ================= */

function bindEvents() {
  // 面板折叠
  $("panel-toggle").addEventListener("click", () => {
    const p = $("panel");
    p.classList.toggle("collapsed");
    $("panel-toggle").textContent = p.classList.contains("collapsed") ? "‹" : "›";
    setTimeout(() => state.map.draw(), 220);
  });

  // 地图工具
  $("tb-zoom-in").addEventListener("click", () => state.map.zoomBy(1));
  $("tb-zoom-out").addEventListener("click", () => state.map.zoomBy(-1));
  $("tb-level").addEventListener("change", () => state.map.setLevel(+$("tb-level").value));
  $("tb-pan").addEventListener("click", () => setTool("pan"));
  $("tb-select").addEventListener("click", () => setTool("select"));
  $("tb-fit").addEventListener("click", () => state.map.fitLayer());
  $("tb-refresh").addEventListener("click", async () => {
    state.map.refresh();
    await reloadLayer(true);
    toast("已刷新瓦片与状态");
  });
  $("tb-grid").addEventListener("change", () => state.map.setGrid($("tb-grid").checked));
  $("tb-status").addEventListener("change", () => {
    state.map.setStatus($("tb-status").checked);
  });
  $("goto-btn").addEventListener("click", gotoFromInputs);

  // 选区
  $("sel-level").addEventListener("change", recomputeSelection);
  $("sel-apply").addEventListener("click", applySelection);
  $("sel-cancel").addEventListener("click", () => {
    hideSelectionBar();
    state.map.clearSelection();
  });

  // 瓦片气泡
  $("pop-close").addEventListener("click", () => { $("tile-pop").hidden = true; });
  $("pop-save").addEventListener("click", popSave);
  $("pop-zoom").addEventListener("click", popZoom);

  // 配置
  for (const f of ["col_start", "col_end", "row_start", "row_end", "tile_matrix"])
    $(`c-${f}`).addEventListener("input", updateRangeSummary);
  $("cfg-save").addEventListener("click", saveConfig);
  $("cfg-reset").addEventListener("click", resetConfig);
  $("cfg-apply-layer").addEventListener("click", applyLayerRange);
  $("cfg-from-view").addEventListener("click", applyViewRange);

  // 任务
  $("btn-pipeline").addEventListener("click", () => startTask("pipeline").catch(() => {}));
  $("btn-download").addEventListener("click", () => startTask("download").catch(() => {}));
  $("btn-merge").addEventListener("click", () => startTask("merge", mergeParams()).catch(() => {}));
  $("btn-geo").addEventListener("click", () => startTask("geo").catch(() => {}));
  $("dl-focus").addEventListener("click", focusDownloadRange);
  $("btn-stop").addEventListener("click", async () => {
    if (!state.taskId) return;
    try {
      const r = await api(`/api/tasks/${state.taskId}/cancel`, { method: "POST" });
      toast(r.message);
    } catch (e) { toast("取消失败: " + e.message, true); }
  });

  // 输出
  $("out-refresh").addEventListener("click", refreshOutputs);

  // 抽屉
  $("drawer-cache").addEventListener("click", () => openDrawer("cache"));
  $("drawer-logs").addEventListener("click", () => openDrawer("logs"));
  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer-mask").addEventListener("click", closeDrawer);
  $("ca-refresh").addEventListener("click", refreshCache);
  $("ca-prune").addEventListener("click", cachePrune);
  $("ca-clear").addEventListener("click", cacheClear);
  $("ca-migrate").addEventListener("click", migrateCache);
  $("ca-verify").addEventListener("click", verifyCache);
  $("log-clear").addEventListener("click", () => { $("log-view").innerHTML = ""; });
  $("log-save").addEventListener("click", saveLogs);

  // 鉴权横幅
  $("auth-fix").addEventListener("click", () => {
    $("auth-banner").hidden = true;
    $("panel").classList.remove("collapsed");
    $("c-cookie").focus();
  });
  $("auth-close").addEventListener("click", () => { $("auth-banner").hidden = true; });

  // 键盘快捷键
  window.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") {
      if (e.key === "Enter" && e.target.id === "goto-lon") gotoFromInputs();
      return;
    }
    if (e.key === "+" || e.key === "=") state.map.zoomBy(1);
    else if (e.key === "-" || e.key === "_") state.map.zoomBy(-1);
    else if (e.key === "s" || e.key === "S") setTool("select");
    else if (e.key === "v" || e.key === "V") setTool("pan");
    else if (e.key === "Escape") {
      hideSelectionBar();
      state.map.clearSelection();
      $("tile-pop").hidden = true;
    } else if (e.key === "Enter" && state.pendingRange) applySelection();
  });
}

async function restoreRunningTask() {
  try {
    const snap = await api("/api/tasks");
    if (snap.current) {
      state.taskId = snap.current.id;
      $("sb-task").textContent = `任务 ${snap.current.id}（${snap.current.type}）`;
      setBusy(true);
      resetPipeline(snap.current.type);
      subscribeTask(snap.current.id);
      appendLog(`检测到进行中的任务 ${snap.current.id}，已重新接入进度流`);
    }
  } catch { /* 忽略 */ }
}

async function init() {
  state.map = new MapWorkbench($("map"), {
    onCursor,
    onSelect: onMapSelect,
    onTileClick,
    onLevelChange,
  });

  bindEvents();

  try {
    state.config = await api("/api/config");
    fillConfigForm(state.config);
  } catch (e) {
    toast("加载配置失败: " + e.message, true);
    return;
  }

  await reloadLayer(false);
  await Promise.allSettled([
    refreshCache(), refreshOutputs(), refreshResume(), loadLogHistory(),
  ]);
  subscribeLogs();
  await restoreRunningTask();
  appendLog("欢迎使用 WMTS 瓦片工作台。当前数据源: " + state.config.base_url);
}

document.addEventListener("DOMContentLoaded", init);
