// ============================== 主压测页:全局状态与后端动作 ==============================
import { reactive, nextTick } from "vue";
import { ElMessage } from "element-plus";
import { api } from "./api.js";
import { lsKey } from "./auth.js";

const ElMessageT = (text, type) => ElMessage({
  message: text,
  type: type === "success" ? "success" : type === "error" ? "error" : "info",
  duration: 3200,
});

// ---------- 默认 16 条策略 ----------
export function buildDefaultStrategies() {
  const levels = [
    { conc: 4, total: 8 }, { conc: 16, total: 16 },
    { conc: 32, total: 24 }, { conc: 64, total: 32 },
  ];
  const combos = [
    { label: "短入短出", input: 256, output: 256 },
    { label: "长入短出", input: 2048, output: 256 },
    { label: "短入长出", input: 256, output: 2048 },
    { label: "长入长出", input: 2048, output: 2048 },
  ];
  const list = [];
  for (const lv of levels) for (const cb of combos) {
    list.push({
      name: `${lv.conc}并发·${cb.label}`, concurrency: lv.conc, total: lv.total,
      input: cb.input, output: cb.output, lang: "zh", checked: true,
      cacheMode: "auto", warmup: 0,
    });
  }
  return list;
}

// ---------- 已保存 API 配置(localStorage,主页面与对话抽屉共享) ----------
const SAVED_KEY = "chat-saved-apis";

// ---------- 主页面配置持久化(刷新后自动回填 API/模型/任务类型) ----------
const MAIN_CFG_KEY = "bench-cfg";
export function loadMainCfg() {
  try {
    const c = JSON.parse(localStorage.getItem(lsKey(MAIN_CFG_KEY)) || "{}");
    if (c.apiUrl) S.apiUrl = c.apiUrl;
    if (c.apiKey !== undefined) S.apiKey = c.apiKey;
    if (c.protocol) S.protocol = c.protocol;
    if (c.taskType) S.taskType = c.taskType;
    if (c.model) S.model = c.model;
    if (Array.isArray(c.models) && c.models.length) S.models = c.models;
    if (c.framework) S.framework = c.framework;
    if (c.maxIn != null) S.maxIn = c.maxIn;
    if (c.maxOut != null) S.maxOut = c.maxOut;
    if (c.sloTtft != null) S.sloTtft = c.sloTtft;
    if (c.sloTpot != null) S.sloTpot = c.sloTpot;
    if (c.temperature !== undefined) S.temperature = c.temperature;
    if (c.seed !== undefined) S.seed = c.seed;
    if (c.gpuInfo !== undefined) S.gpuInfo = c.gpuInfo;
  } catch {}
}
export function saveMainCfg() {
  localStorage.setItem(lsKey(MAIN_CFG_KEY), JSON.stringify({
    apiUrl: S.apiUrl, apiKey: S.apiKey, protocol: S.protocol,
    taskType: S.taskType, model: S.model, models: S.models,
    framework: S.framework, maxIn: S.maxIn, maxOut: S.maxOut,
    sloTtft: S.sloTtft, sloTpot: S.sloTpot, temperature: S.temperature,
    seed: S.seed, gpuInfo: S.gpuInfo,
  }));
}
export function loadSavedApis() {
  try { return JSON.parse(localStorage.getItem(lsKey(SAVED_KEY)) || "[]"); } catch { return []; }
}
export function saveSavedApiCfg(url, key, models) {
  const cfgs = loadSavedApis();
  const exist = cfgs.find(c => c.url === url);
  if (exist) { exist.key = key; exist.models = models; }
  else cfgs.push({ url, key, models });
  localStorage.setItem(lsKey(SAVED_KEY), JSON.stringify(cfgs));
  S.savedApis = cfgs;
}
export function removeSavedApi(i) {
  const cfgs = loadSavedApis();
  const removed = cfgs.splice(i, 1);
  localStorage.setItem(lsKey(SAVED_KEY), JSON.stringify(cfgs));
  S.savedApis = cfgs;
  return removed[0] || null;
}

export const STATUS_LABEL = { pending: "待启动", running: "运行中", paused: "已暂停", done: "已完成", cancelled: "已停止", error: "出错" };
export const STATUS_TYPE = { pending: "info", running: "primary", paused: "warning", done: "success", cancelled: "danger", error: "danger" };

export const S = reactive({
  // API 配置卡
  apiUrl: "", apiKey: "", protocol: "openai", taskType: "chat",
  model: "", models: [], caps: [],
  framework: "Unknown", maxIn: null, maxOut: null,
  modelDetails: [], serverMeta: {},
  detecting: false, probing: false,
  // 服务信息(全量端点)
  serverInfo: { sub: "检测后自动获取", badges: [], cfg: null, cfgFrom: "", models: [], loading: false },
  // 策略
  strategies: buildDefaultStrategies(),
  randomPrompt: true,
  // 对比受控项:SLO 阈值(0=不启用 Goodput)、采样参数(空=引擎默认)、硬件环境说明
  sloTtft: 0, sloTpot: 0, temperature: null, seed: null, gpuInfo: "",
  savedApis: loadSavedApis(), savedSel: "",
  // 报告目录
  reportsDir: "", dirNote: "", reports: [],
  // 任务
  tasks: {},            // id -> ui 状态(reactive)
  order: [],            // 创建顺序
  selected: null,
  // Excel 预览
  preview: { visible: false, filename: "", data: null, sheetIdx: 0 },
  // 模型详情弹窗
  infoVisible: false,
  // 深空补给动画
  ship: { shown: false, pct: 0, title: "", sub: "" },
});
window.S = S;   // 调试/自动化测试可访

export function newTaskUi(info) {
  return reactive({
    info,
    logs: [],
    stratStates: {},      // index -> running|done|has-fail
    curName: "-", curDone: 0, curTotal: 0,
    ok: 0, fail: 0,
    errGroups: {},
    live: [],             // {t, tps} 滚动窗口
    summaries: [],
    analysis: null,
    aiAnalysis: null,
    excel: null,
  });
}

export const fmt = (v, suffix = "") => (v == null ? "-" : `${v}${suffix}`);

// 失败原因分类:把原始报错翻译成可操作的具体说明
export function classifyErr(e) {
  const s = String(e || "");
  if (/Cannot connect|Connection refused|拒绝网络连接|ClientConnectorError/i.test(s))
    return "连接被拒 — 服务未监听该端口,或地址/端口写错";
  if (/ConnectTimeout|ConnectionTimeout/i.test(s))
    return "连接超时 — 网络不通或被防火墙丢弃";
  if (/Timeout|timed?\s*out|超时/i.test(s))
    return "请求超时 — 服务无响应(负载过高/卡死)或响应过慢";
  if (/ServerDisconnected|ConnectionReset|RemoteDisconnected|连接被重置/i.test(s))
    return "连接被服务端断开 — 服务重启/崩溃或主动断连";
  if (/\b40[13]\b/.test(s)) return "认证失败(401/403) — API Key 缺失、无效或无权限";
  if (/\b404\b/.test(s)) return "接口不存在(404) — 路径或模型名写错";
  if (/\b400\b/.test(s)) return "请求被拒绝(400) — 参数或模型与服务端不匹配";
  if (/\b429\b/.test(s)) return "触发限流(429) — 服务端限速,并发过高";
  if (/\b5\d\d\b/.test(s)) return "服务端内部错误(5xx) — 被测服务异常,看服务端日志";
  if (s) return "其他错误";
  return "未知错误(无错误信息)";
}

// ---------- 日志(仅所选任务) ----------
export function log(msg, cls = "", tid = null) {
  const id = tid || S.selected;
  const ui = id && S.tasks[id];
  if (!ui) return;
  const t = new Date().toLocaleTimeString();
  ui.logs.push({ t, msg, cls });
  if (ui.logs.length > 500) ui.logs.shift();
}

// ---------- 自动检测 ----------
export async function detect() {
  const url = S.apiUrl.trim();
  if (!url) { ElMessageT("请输入 API 地址"); return; }
  S.detecting = true;
  try {
    const data = await api("/api/detect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_url: url, api_key: S.apiKey }),
    });
    S.protocol = data.protocol || "openai";
    S.framework = data.framework || "Unknown";
    S.models = data.models || [];
    if (S.models.length && !S.model) S.model = S.models[0];
    S.modelDetails = data.model_details || [];
    S.serverMeta = data.server_meta || {};
    S.caps = data.capabilities || [];
    saveSavedApiCfg(url, S.apiKey, S.models);
    ElMessageT(`识别成功: 协议=${data.protocol}, 架构=${data.framework}, 发现 ${S.models.length} 个模型`, "success");
    if (S.model.trim()) probeLimits(true);
    refreshServerMetrics();
  } catch (e) {
    ElMessageT(`检测失败: ${e.message}`, "error");
  } finally { S.detecting = false; }
}

// ---------- 上下文上限探测(探测中再点击「探测」按钮 = 停止探测) ----------
let _probeAbort = null;        // 当前探测的 AbortController
let _probeId = "";             // 本次探测 id(后端按 id 取消,立即停止后续请求)
let _probeStopByUser = false;  // 用户主动停止(区分于探测失败)

export function stopProbe() {
  if (!S.probing || !_probeAbort) return;
  _probeStopByUser = true;
  // 先通知后端立即终止探测循环,再中断本地等待(两条路互为兜底)
  fetch("/api/probe-limits/cancel", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ probe_id: _probeId }),
  }).catch(() => {});
  _probeAbort.abort();
}

export async function probeLimits(isAuto = false) {
  if (S.probing) {                     // 已在探测中:手动点击 → 停止;自动跟随触发 → 忽略
    if (!isAuto) stopProbe();
    return;
  }
  const url = S.apiUrl.trim();
  const model = S.model.trim();
  if (!url || !model) {
    if (!isAuto) ElMessageT(!url ? "请输入 API 地址" : "请先输入或选择模型", "error");
    return;
  }
  _probeId = `probe-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  _probeAbort = new AbortController();
  _probeStopByUser = false;
  S.probing = true;
  try {
    const d = await api("/api/probe-limits", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_url: url, api_key: S.apiKey, protocol: S.protocol, model, probe_id: _probeId }),
      signal: _probeAbort.signal,
    });
    S.maxIn = d.max_input_tokens.value;
    S.maxOut = d.max_output_tokens.value;
    if (!isAuto) ElMessageT(`探测完成: 最大输入 ≈${S.maxIn ?? "?"} tokens,最大输出 ≈${S.maxOut ?? "?"} tokens`, "success");
  } catch (e) {
    if (_probeStopByUser || e.name === "AbortError") ElMessageT("已停止上下文探测", "info");
    else ElMessageT(`探测失败: ${e.message}`, "error");
  } finally {
    S.probing = false;
    _probeAbort = null; _probeId = ""; _probeStopByUser = false;
  }
}

// ---------- 服务信息(全量端点探测) ----------
export async function refreshServerMetrics() {
  const si = S.serverInfo;
  const url = S.apiUrl.trim();
  if (!url) { si.sub = "未填写 API 地址"; return; }
  si.sub = "探测服务信息中..."; si.loading = true;
  si.badges = []; si.cfg = null; si.models = [];
  try {
    const d = await api("/api/server-info", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_url: url, api_key: S.apiKey }),
    });
    const add = (cls, text, title) => si.badges.push({ cls, text, title });
    if (!d.ok) {
      add("err", `❌ ${d.message || "所有端点均不可访问"}`);
      si.sub = "不可用";
      return;
    }
    if (d.health) add("ok", `健康: ${d.health}`);
    const ver = d.version ? (typeof d.version === "object" ? (d.version.version || JSON.stringify(d.version)) : d.version) : null;
    if (ver) add("info", `版本: ${String(ver).slice(0, 30)}`);
    if (d.framework) add("info", `框架: ${d.framework}`);
    if (d.models && d.models.length) add("ok", `模型 ${d.models.length} 个`);
    const srcKeys = Object.keys(d.sources || {});
    if (srcKeys.length) add("warn", `🛡 SSH 通道: ${srcKeys.join(" / ")}`, "这些端点直连被拦截,已通过 SSH 在主机内部获取");
    const m = (d.metrics && d.metrics.metrics) || {};
    if (m.kv_cache_usage_perc != null) add("info", `KV Cache ${m.kv_cache_usage_perc}%`);
    if (m.prefix_cache_hit_rate != null)
      add("ok", `前缀缓存命中率 ${m.prefix_cache_hit_rate}%(${m.prefix_cache_hits ?? 0}/${(m.prefix_cache_hits ?? 0) + (m.prefix_cache_misses ?? 0)})`);
    if (m.running_requests != null || m.waiting_requests != null)
      add("warn", `运行 ${m.running_requests ?? 0} · 排队 ${m.waiting_requests ?? 0}`);
    if (m.gen_throughput != null) add("run", `生成吞吐 ${Math.round(m.gen_throughput)} tok/s`);
    if (!si.badges.length) add("", "端点可达,但未识别到结构化信息");

    const cfg = d.server_info || d.sglang_info || null;
    if (cfg && Object.keys(cfg).length) {
      si.cfg = cfg;
      si.cfgFrom = d.server_info ? "服务配置(/server_info)" : "服务配置(/get_server_info)";
    }
    if (d.models && d.models.length) si.models = d.models;
    const parts = [];
    if (d.health) parts.push("health");
    if (ver) parts.push("version");
    if (cfg) parts.push(d.server_info ? "server_info" : "get_server_info");
    if (d.metrics) parts.push("metrics");
    si.sub = `已刷新 · ${parts.join(" + ")}`;
  } catch {
    si.sub = "获取失败";
  } finally { si.loading = false; }
}

// ---------- 报告 ----------
export async function pollReports() {
  try { S.reports = await api("/api/reports"); } catch { /* ignore */ }
}

export async function loadConfig() {
  try {
    const d = await api("/api/config");
    S.reportsDir = d.custom ? d.reports_dir : "";
    S.dirNote = d.custom ? `当前: ${d.reports_dir}` : `默认: ${d.reports_dir}`;
  } catch { /* ignore */ }
}

export async function saveReportsDir(dir) {
  try {
    const d = await api("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reports_dir: dir }),
    });
    S.dirNote = `已保存,当前: ${d.reports_dir}`;
    pollReports();
    return true;
  } catch (e) {
    S.dirNote = `保存失败: ${e.message}`;
    return false;
  }
}

export function downloadReport(filename) {
  const a = document.createElement("a");
  a.href = `/api/download/${encodeURIComponent(filename)}`;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export async function openPreview(filename) {
  try {
    const d = await api(`/api/preview/${encodeURIComponent(filename)}`);
    S.preview.filename = d.filename;
    S.preview.data = d;
    S.preview.sheetIdx = 0;
    S.preview.visible = true;
  } catch (e) {
    ElMessageT(`预览失败: ${e.message}`, "error");
  }
}

// ---------- 任务轮询 / SSE ----------
let streamAbort = null;

export async function pollTasks() {
  try {
    const list = await api("/api/tasks");
    for (const t of list) {
      if (!S.tasks[t.id]) { S.tasks[t.id] = newTaskUi(t); S.order.push(t.id); }
      S.tasks[t.id].info = t;
    }
    // 深空补给:有运行中任务时进场(燃料=真实进度),结束启航
    const running = list.filter(t => t.status === "running");
    if (running.length) {
      const done = running.reduce((s, t) => s + (t.done_requests || 0), 0);
      const total = running.reduce((s, t) => s + (t.total_requests || 0), 0);
      shipShow();
      shipFuel(total ? Math.min(100, Math.round(done / total * 100)) : 0);
    } else shipDepart();
  } catch { /* server restarting */ }
}

let shipTimer = null;
function shipShow() {
  if (S.ship.shown) return;
  S.ship.shown = true;
  clearTimeout(shipTimer);
  S.ship.title = "🛰 深空补给进行中";
  S.ship.sub = "正在为压测舰队补充燃料 · ";
  S.ship.pct = 0;
}
function shipFuel(pct) { S.ship.pct = pct; }
function shipDepart() {
  if (!S.ship.shown) return;
  S.ship.shown = false;
  S.ship.departing = true;
  S.ship.pct = 100;
  S.ship.title = "舰队启航 🚀";
  S.ship.sub = "补给完成,压测舰队准备跃迁 · ";
  clearTimeout(shipTimer);
  shipTimer = setTimeout(() => { S.ship.departing = false; }, 3000);
}

export function selectTask(id) {
  S.selected = id;
  if (streamAbort) streamAbort.abort();
  subscribe(id);
  // 拉取详情:回填全部策略结果 + AI 分析(历史回看/切走期间错过的结果)
  api("/api/tasks/" + id).then(d => {
    if (S.selected !== id || !S.tasks[id]) return;
    const ui = S.tasks[id];
    let changed = false;
    (d.summaries || []).forEach((s, i) => {
      if (s && s.summary && !ui.summaries[i]) { ui.summaries[i] = s; changed = true; }
    });
    if (d.ai_analysis && !ui.aiAnalysis) {
      ui.aiAnalysis = d.ai_analysis;
      if (d.analysis) ui.analysis = d.analysis;
      changed = true;
    }
    // 无实时日志的任务按回填结果合成摘要日志
    if (!ui.logs.length && ui.summaries.some(x => x)) {
      ui.summaries.forEach((s, i) => {
        if (!s) return;
        const sum = s.summary || {};
        ui.logs.push({ t: "--:--", cls: "l-strategy",
          msg: `[策略 ${i + 1}] ${s.name} 完成: 成功=${sum.success_count} 失败=${sum.failed_count}` +
               ` TTFT_P50=${fmt((sum.ttft_ms || {}).p50, "ms")} 聚合TPS=${sum.aggregate_throughput_tps}` });
      });
      ui.logs.push({ t: "--:--", cls: "l-ok",
        msg: `压测${d.status === "done" ? "完成" : "已停止"}! Excel: ${d.excel_file || "无"}` });
      changed = true;
    }
    if (changed && ui.aiAnalysis && !ui.analysis && d.analysis) ui.analysis = d.analysis;
  }).catch(() => {});
}

async function subscribe(id) {
  const ctrl = new AbortController();
  streamAbort = ctrl;
  try {
    const resp = await fetch(`/api/tasks/${id}/events`, { signal: ctrl.signal });
    if (!resp.ok) return;
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const ds = line.slice(5).trim();
        if (!ds) continue;
        try { handleTaskEvent(id, JSON.parse(ds)); } catch { /* ignore */ }
      }
    }
  } catch { /* aborted */ }
}

function handleTaskEvent(id, evt) {
  const ui = S.tasks[id];
  if (!ui) return;
  if (evt.type === "started") {
    log("任务启动,开始压测", "l-info", id);
  } else if (evt.type === "strategy_start") {
    ui.curName = evt.name;
    ui.curDone = 0;
    ui.curTotal = evt.config.total_requests;
    ui.ok = 0; ui.fail = 0;
    ui.errGroups = {};
    ui.stratStates[evt.index] = "running";
    log(`[策略 ${evt.index + 1}/${evt.total}] ${evt.name} 开始 (并发=${evt.config.concurrency} 请求=${evt.config.total_requests} 输入=${evt.config.input_tokens}tok 输出=${evt.config.max_output_tokens}tok ${evt.config.lang === "zh" ? "中文" : "英文"})`, "l-strategy", id);
  } else if (evt.type === "progress") {
    ui.curDone = evt.completed;
    ui.ok = evt.success;
    ui.fail = evt.failed;
    ui.live.push({ t: Date.now(), tps: evt.last.output_tokens_per_s || 0 });
    if (ui.live.length > 30) ui.live.shift();
    const r = evt.last;
    if (r.success) {
      log(`  #${r.request_id} OK ttft=${fmt(r.ttft_ms, "ms")} lat=${r.total_latency_s}s tps=${r.output_tokens_per_s} out=${r.output_tokens}tok`, "l-ok", id);
    } else {
      const reason = classifyErr(r.error);
      ui.errGroups[reason] = (ui.errGroups[reason] || 0) + 1;
      log(`  #${r.request_id} FAIL [HTTP ${r.status_code || "-"}] ${reason}`, "l-err", id);
      log(`      原始报错: ${r.error || "(无)"}`, "l-err", id);
    }
  } else if (evt.type === "strategy_complete") {
    ui.stratStates[evt.index] = evt.summary.failed_count > 0 ? "has-fail" : "done";
    ui.summaries[evt.index] = { name: evt.name, config: evt.config, summary: evt.summary };
    log(`[策略 ${evt.index + 1}] ${evt.name} 完成: 成功=${evt.summary.success_count} 失败=${evt.summary.failed_count} TTFT_P50=${fmt((evt.summary.ttft_ms || {}).p50, "ms")} 延迟P50=${fmt((evt.summary.latency_s || {}).p50, "s")} 聚合TPS=${evt.summary.aggregate_throughput_tps}`, "l-strategy", id);
    if (evt.summary.failed_count > 0) {
      log(`  ↳ 失败原因汇总 (${evt.summary.failed_count} 个失败请求):`, "l-err", id);
      const groups = evt.summary.error_groups && Object.keys(evt.summary.error_groups).length
        ? evt.summary.error_groups : (ui.errGroups || {});
      for (const [reason, cnt] of Object.entries(groups)) {
        log(`      · ${reason} × ${cnt}`, "l-err", id);
      }
    }
  } else if (evt.type === "paused") {
    log("任务已暂停,可编辑剩余策略后继续", "l-info", id);
  } else if (evt.type === "resumed") {
    log("任务继续执行", "l-info", id);
  } else if (evt.type === "strategies_updated") {
    ui.info.strategies = evt.strategies;
    log("剩余策略已更新", "l-info", id);
  } else if (evt.type === "complete" || evt.type === "cancelled") {
    ui.excel = evt.excel_file;
    ui.analysis = evt.analysis;
    ui.aiAnalysis = evt.ai_analysis || null;
    log(`${evt.type === "complete" ? "压测完成" : "任务已停止"}! Excel: ${evt.excel_file || "无"}`, "l-ok", id);
    if (evt.ai_analysis) log("AI 深度分析报告已生成并写入 Excel Sheet2", "l-ok", id);
  } else if (evt.type === "status") {
    log(evt.message || "", "l-info", id);
  } else if (evt.type === "error") {
    log(`任务出错: ${evt.message}`, "l-err", id);
  }
}

// ---------- 创建任务 ----------
export async function createTask() {
  const strategies = S.strategies.filter(s => s.checked).map(s => ({
    name: (s.name || "").trim() || "未命名策略",
    concurrency: Math.min(256, Math.max(1, parseInt(s.concurrency, 10) || 4)),
    total_requests: Math.min(2000, Math.max(1, parseInt(s.total, 10) || 16)),
    input_tokens: parseInt(s.input, 10) || 128,
    max_output_tokens: parseInt(s.output, 10) || 128,
    lang: s.lang,
    cache_mode: ["auto", "cold", "hot"].includes(s.cacheMode) ? s.cacheMode : "auto",
    warmup_requests: Math.max(0, Math.min(10, parseInt(s.warmup, 10) || 0)),
  }));
  if (!S.apiUrl.trim()) return ElMessageT("请输入 API 地址", "error");
  if (!S.model.trim()) return ElMessageT("请输入或选择模型", "error");
  if (!strategies.length) return ElMessageT("请至少勾选一个测试策略", "error");
  try {
    const info = await api("/api/tasks", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_url: S.apiUrl.trim(), api_key: S.apiKey, protocol: S.protocol,
        model: S.model.trim(), framework: S.framework, task_type: S.taskType,
        random_prompt: S.randomPrompt, strategies,
        slo_ttft_ms: S.sloTtft > 0 ? Math.round(S.sloTtft) : null,
        slo_tpot_ms: S.sloTpot > 0 ? Math.round(S.sloTpot) : null,
        sampling: Object.assign({},
          S.temperature !== null && S.temperature !== "" ? { temperature: Number(S.temperature) } : null,
          S.seed > 0 ? { seed: Math.round(S.seed) } : null),
        server_meta: S.serverMeta || {},
        gpu_info: (S.gpuInfo || "").trim(),
      }),
    });
    if (!S.tasks[info.id]) { S.tasks[info.id] = newTaskUi(info); S.order.push(info.id); }
    log(`任务 ${info.id} 已创建: ${info.model} @ ${info.api_url}, ${info.strategy_count} 个策略`, "l-info", info.id);
    selectTask(info.id);
    pollTasks();
    ElMessageT(`任务已创建(${info.strategy_count} 个策略),点击「▶ 启动压测」开始`, "success");
    // 窄屏单列布局下,任务列表在页面下方 — 自动滚过去让用户看到新任务
    if (window.innerWidth <= 1180) {
      nextTick(() => {
        const el = document.querySelector(".ix-task-item");
        if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    }
  } catch (e) {
    ElMessageT(`创建任务失败: ${e.message}`, "error");
  }
}

// ---------- 任务控制 ----------
export async function taskAction(id, action) {
  if (!id) return;
  try { await api(`/api/tasks/${id}/${action}`, { method: "POST" }); } catch { /* ignore */ }
  pollTasks();
}

export async function renameTask(id, name) {
  try {
    await api(`/api/tasks/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    S.tasks[id].info.name = name;
  } catch (e) { ElMessageT(`改名失败: ${e.message}`, "error"); }
}

export async function deleteTask(id) {
  try { await api(`/api/tasks/${id}`, { method: "DELETE" }); } catch { /* already gone */ }
  delete S.tasks[id];
  S.order = S.order.filter(x => x !== id);
  if (S.selected === id) {
    S.selected = null;
    if (streamAbort) streamAbort.abort();
  }
}

export async function saveRemaining(id, list) {
  try {
    await api(`/api/tasks/${id}/strategies`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategies: list }),
    });
    log(`剩余策略已保存(${list.length} 条)`, "l-ok", id);
    pollTasks();
  } catch (e) {
    log(`保存失败: ${e.message}`, "l-err", id);
  }
}
