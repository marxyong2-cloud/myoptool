// ============================== 模型部署站(Model Start)状态与 API ==============================
import { reactive } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "./api.js";

const JSON_HDR = { "Content-Type": "application/json" };

export const MS = reactive({
  view: "containers",          // containers | presets | plans
  // SSH 主机(支持分组 / 密码·密钥登录)
  hosts: [], activeHost: "",
  hostFilter: "",               // 侧栏主机过滤(按名称 / 地址)
  hostProbe: {},               // id -> {ok, info, raw}
  collapsedGroups: {},         // 分组折叠状态
  // 容器 / Pod 检测
  detecting: false,
  detected: { containers: [], pods: [], docker_error: "", k8s_error: "" },
  // 容器运维操作(启动/停止/重启)与日志查看
  cop: { running: false, op: "" },
  clogs: { visible: false, loading: false, container: "", runtime: "docker", namespace: "", tail: 200, text: "", cmd: "" },
  // 当前选中容器
  cur: { runtime: "docker", container: "", namespace: "" },
  // 文件浏览
  files: { path: "", entries: [], loading: false, raw: "" },
  // 容器内命令执行(格式校验 + 分离的 stdout/stderr/exit code)
  exec: { command: "", running: false, lastCmd: "", out: "", err: "", rc: null, duration: null, hint: "" },
  // 预设命令 v2(标签 / 语言 / 命令·脚本·上传三种形式)
  presets: [],
  presetFilter: { kw: "", tag: "" },
  // 预设试运行
  once: { visible: false, hostId: "", container: "", namespace: "", runtime: "docker", running: false,
          cmd: "", out: "", err: "", rc: null, duration: null, hint: "" },
  // 任务编排
  plans: [],
  // 方案运行
  run: { id: "", status: "", paused: false, started: "", steps: [], logs: [], last: -1 },
  // 执行历史(方案运行记录,服务端持久化,重启后仍可回看)
  history: { visible: false, loading: false, runs: [], detailId: "", detail: null },
  // 终端(底部抽屉)
  term: { open: false, connected: false, host: "", runtime: "docker", container: "", namespace: "", connectReq: 0 },
});

export const hostById = (id) => MS.hosts.find(h => h.id === id) || null;

// ---------- 主机分组(支持侧栏过滤:按名称 / 地址匹配,过滤后隐藏空分组) ----------
const _hostHit = (h) => {
  const q = (MS.hostFilter || "").trim().toLowerCase();
  return !q || (h.name || "").toLowerCase().includes(q) || (h.host || "").toLowerCase().includes(q);
};
export function hostGroups() {
  const groups = [];
  const byName = new Map();
  for (const h of MS.hosts) {
    if (!_hostHit(h)) continue;
    const g = (h.group || "").trim();
    if (!g) continue;
    if (!byName.has(g)) { const item = { name: g, hosts: [] }; byName.set(g, item); groups.push(item); }
    byName.get(g).hosts.push(h);
  }
  return groups;
}
export const ungroupedHosts = () => MS.hosts.filter(h => !(h.group || "").trim() && _hostHit(h));
export const groupNames = () => [...new Set(MS.hosts.map(h => (h.group || "").trim()).filter(Boolean))];
export function toggleGroup(name) { MS.collapsedGroups[name] = !MS.collapsedGroups[name]; }
export const isGroupCollapsed = (name) => !!MS.collapsedGroups[name];

// ---------- 主机 ----------
export async function loadHosts() {
  const d = await api("/api/modelstart/hosts");
  MS.hosts = d.hosts || [];
  if (MS.activeHost && !hostById(MS.activeHost)) MS.activeHost = "";
}
export async function saveHosts() {
  const d = await api("/api/modelstart/hosts", { method: "POST", headers: JSON_HDR, body: JSON.stringify({ hosts: MS.hosts }) });
  MS.hosts = d.hosts || [];
  ElMessage.success("主机列表已保存");
}
export async function probeHost(h) {
  try {
    const d = await api(`/api/modelstart/hosts/${h.id}/probe`, { method: "POST" });
    MS.hostProbe[h.id] = d;
    ElMessage[d.ok ? "success" : "warning"](d.ok ? `探测成功:${d.info.HOST || h.host}` : "探测失败,详情见左下角");
  } catch (e) {
    MS.hostProbe[h.id] = { ok: false, info: {}, raw: e.message };
    ElMessage.error("探测失败:" + e.message);
  }
}
export async function selectHost(hid) {
  MS.activeHost = hid;
  MS.detected = { containers: [], pods: [], docker_error: "", k8s_error: "" };
  MS.cur = { runtime: "docker", container: "", namespace: "" };
  MS.files = { path: "", entries: [], loading: false, raw: "" };
  MS.exec = { command: "", running: false, lastCmd: "", out: "", err: "", rc: null, duration: null, hint: "" };
  if (hid) await detect();
}

// ---------- 容器 / Pod 检测 ----------
export async function detect() {
  if (!MS.activeHost) { ElMessage.warning("请先在左侧选择主机"); return; }
  MS.detecting = true;
  try {
    MS.detected = await api("/api/modelstart/detect", { method: "POST", headers: JSON_HDR, body: JSON.stringify({ host_id: MS.activeHost }) });
  } catch (e) {
    ElMessage.error("检测失败:" + e.message);
  }
  MS.detecting = false;
}

export function pickContainer(runtime, container, namespace) {
  MS.cur = { runtime, container, namespace: namespace || "" };
  MS.term.host = MS.activeHost;
  MS.term.runtime = runtime;
  MS.term.container = container;
  MS.term.namespace = namespace || "";
  loadFiles();
}

// ---------- 命令格式校验(执行前本地校验,错误红色提示) ----------
export function validateCmd(cmd) {
  const s = (cmd || "").trim();
  if (!s) return "命令为空";
  const quotePair = { "'": "'", '"': '"', "`": "`" };
  const openB = { "(": ")", "[": "]", "{": "}" };
  const closeB = { ")": "(", "]": "[", "}": "{" };
  let q = null;
  const stack = [];
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === "\\" && q !== "'") { i++; continue; }
    if (q) { if (c === q) q = null; continue; }
    if (quotePair[c]) { q = c; continue; }
    if (openB[c]) stack.push(c);
    else if (closeB[c]) {
      const last = stack.pop();
      if (last !== closeB[c]) return `括号不匹配:出现多余的 "${c}"`;
    }
  }
  if (q) return `存在未闭合的 ${q === "'" ? "单引号" : q === '"' ? "双引号" : "反引号"} ${q}`;
  if (stack.length) return `存在未闭合的括号:${stack.join(" ")}`;
  return "";
}

// ---------- 高风险命令识别(kill / 停止 / 删除等,运行前确认) ----------
export const RISK_RE = /\b(kill|pkill|killall|shutdown|reboot|halt|poweroff|taskkill)\b|\brm\s+(-[a-z]*[rf][a-z]*\b|--recursive\b|-r\b)|\bdocker\s+(rm|stop|kill|restart|pause|prune)\b|\bkubectl\s+(delete|scale)\b|\bfuser\s+-k\b|\bmkfs\b|\bdd\s+if=/i;
export const isRiskyCmd = (text) => RISK_RE.test(text || "");

// ---------- 文件浏览 ----------
export async function loadFiles(path) {
  if (!MS.cur.container) return;
  MS.files.loading = true;
  try {
    const d = await api("/api/modelstart/files", {
      method: "POST", headers: JSON_HDR,
      body: JSON.stringify({ host_id: MS.activeHost, runtime: MS.cur.runtime, container: MS.cur.container, namespace: MS.cur.namespace, path: path || MS.files.path || "" }),
    });
    MS.files.path = d.path || "";
    MS.files.entries = d.entries || [];
    MS.files.raw = (d.raw || "").includes("[stderr]") ? d.raw : "";
  } catch (e) {
    ElMessage.error("读取目录失败:" + e.message);
  }
  MS.files.loading = false;
}
export function enterDir(name) { loadFiles(MS.files.path.replace(/\/+$/, "") + "/" + name); }
export function goUp() {
  const p = (MS.files.path || "/").replace(/\/+$/, "");
  const up = p.split("/").slice(0, -1).join("/") || "/";
  loadFiles(up);
}

// ---------- 容器内命令执行 ----------
export async function runExec() {
  const err = validateCmd(MS.exec.command);
  if (err) { ElMessage.warning("命令格式有误:" + err); return; }
  if (!MS.cur.container) { ElMessage.warning("请先选择容器"); return; }
  if (!MS.exec.command.trim()) return;
  MS.exec.running = true;
  MS.exec.rc = null;
  MS.exec.lastCmd = MS.exec.command;
  try {
    const d = await api("/api/modelstart/exec", {
      method: "POST", headers: JSON_HDR,
      body: JSON.stringify({ host_id: MS.activeHost, runtime: MS.cur.runtime, container: MS.cur.container, namespace: MS.cur.namespace, command: MS.exec.command }),
    });
    MS.exec.out = d.out || "";
    MS.exec.err = d.err || "";
    MS.exec.rc = d.rc;
    MS.exec.duration = d.duration;
    MS.exec.hint = d.hint || "";
  } catch (e) {
    MS.exec.out = "";
    MS.exec.err = e.message;
    MS.exec.rc = -1;
    MS.exec.duration = null;
    MS.exec.hint = "";
  }
  MS.exec.running = false;
}

// ---------- 容器运维操作:启动 / 停止 / 重启(危险操作先确认,完成后自动刷新列表) ----------
const OP_LABEL = { start: "启动", stop: "停止", restart: "重启" };
export async function containerOp(op) {
  if (!MS.cur.container) { ElMessage.warning("请先选择容器"); return; }
  if (MS.cur.runtime === "k8s") { ElMessage.warning("K8s Pod 由控制器管理,暂不支持直接启停"); return; }
  const cname = MS.cur.container;
  if (op !== "start") {
    try {
      await ElMessageBox.confirm(`${OP_LABEL[op]}容器「${cname}」?${op === "stop" ? "容器内服务将中断。" : "容器内服务将短暂中断后恢复。"}`,
        `${OP_LABEL[op]}容器`, { type: "warning", confirmButtonText: `确定${OP_LABEL[op]}`, cancelButtonText: "取消" });
    } catch { return; }
  }
  MS.cop = { running: true, op };
  try {
    const d = await api("/api/modelstart/container-op", {
      method: "POST", headers: JSON_HDR,
      body: JSON.stringify({ host_id: MS.activeHost, runtime: MS.cur.runtime, container: MS.cur.container, namespace: MS.cur.namespace, op }),
    });
    if (d.ok) ElMessage.success(`已${OP_LABEL[op]}容器「${cname}」(${d.duration}s)`);
    else ElMessage.error(`${OP_LABEL[op]}失败:exit ${d.rc}${d.hint ? " · " + d.hint : ""}`);
    detect();                                   // 刷新容器状态
  } catch (e) {
    ElMessage.error(e.message);
  }
  MS.cop = { running: false, op: "" };
}

// ---------- 容器日志查看(docker logs / kubectl logs) ----------
export function openContainerLogs() {
  if (!MS.cur.container) { ElMessage.warning("请先选择容器"); return; }
  Object.assign(MS.clogs, {
    visible: true, loading: false, container: MS.cur.container, runtime: MS.cur.runtime,
    namespace: MS.cur.namespace, tail: 200, text: "", cmd: "",
  });
  loadContainerLogs();
}
export async function loadContainerLogs() {
  MS.clogs.loading = true;
  try {
    const d = await api("/api/modelstart/container-logs", {
      method: "POST", headers: JSON_HDR,
      body: JSON.stringify({ host_id: MS.activeHost, runtime: MS.clogs.runtime, container: MS.clogs.container, namespace: MS.clogs.namespace, tail: MS.clogs.tail }),
    });
    MS.clogs.text = d.logs || "(无输出)";
    MS.clogs.cmd = d.cmd || "";
  } catch (e) {
    MS.clogs.text = "✗ " + e.message;
  }
  MS.clogs.loading = false;
}

// ---------- 预设命令 v2 ----------
export async function loadPresets() {
  const d = await api("/api/modelstart/presets");
  MS.presets = d.presets || [];
}
export async function savePresets() {
  const d = await api("/api/modelstart/presets", { method: "POST", headers: JSON_HDR, body: JSON.stringify({ presets: MS.presets }) });
  MS.presets = d.presets || [];
}
export async function uploadPresetFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/modelstart/presets/upload", { method: "POST", body: fd });
  const d = await r.json();
  if (!r.ok || !d.ok) throw new Error(d.detail || "上传失败");
  return d;   // { filename, name, size }
}
export async function runPresetOnce() {
  if (!MS.once.hostId) { ElMessage.warning("请选择试运行主机"); return; }
  const p = MS.presets.find(x => x.id === MS.once.presetId);
  if (!p) return;
  if (p.kind === "container" && !MS.once.container.trim()) { ElMessage.warning("容器内预设需要填写容器名"); return; }
  MS.once.running = true;
  MS.once.cmd = ""; MS.once.out = ""; MS.once.err = ""; MS.once.rc = null;
  try {
    const d = await api("/api/modelstart/presets/run-once", {
      method: "POST", headers: JSON_HDR,
      body: JSON.stringify({ host_id: MS.once.hostId, preset_id: p.id, runtime: MS.once.runtime, container: MS.once.container.trim(), namespace: MS.once.namespace || "" }),
    });
    MS.once.cmd = d.cmd; MS.once.out = d.out || ""; MS.once.err = d.err || "";
    MS.once.rc = d.rc; MS.once.duration = d.duration; MS.once.hint = d.hint || "";
  } catch (e) {
    MS.once.err = e.message;
    MS.once.rc = -1;
  }
  MS.once.running = false;
}

// ---------- 任务编排 ----------
export async function loadPlans() {
  const d = await api("/api/modelstart/plans");
  MS.plans = d.plans || [];
}
export async function savePlans() {
  const d = await api("/api/modelstart/plans", { method: "POST", headers: JSON_HDR, body: JSON.stringify({ plans: MS.plans }) });
  MS.plans = d.plans || [];
}

let pollTimer = null;
export function stopRunPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
export async function runPlan(pid, onError) {
  stopRunPoll();
  try {
    const d = await api(`/api/modelstart/plans/${pid}/run`, { method: "POST" });
    MS.run = { id: d.run_id, status: "running", paused: false, started: "", steps: [], logs: [], last: -1 };
    pollTimer = setInterval(async () => {
      try {
        const r = await api(`/api/modelstart/runs/${MS.run.id}?after=${MS.run.last}`);
        MS.run.status = r.status;
        MS.run.paused = !!r.paused;
        MS.run.started = r.started;
        MS.run.steps = r.steps || [];
        if ((r.logs || []).length) {
          MS.run.logs.push(...r.logs);
          if (MS.run.logs.length > 4000) MS.run.logs.splice(0, MS.run.logs.length - 4000);   // 防长跑日志无限膨胀
        }
        MS.run.last = r.last != null ? r.last : MS.run.last;
        if (r.status !== "running") { stopRunPoll(); loadHistory(true); }
      } catch (e) {
        stopRunPoll();
        ElMessage.error(e.message);
      }
    }, 1000);
  } catch (e) {
    if (onError) onError(e); else ElMessage.error(e.message);
  }
}
export async function cancelRun() {
  try { await api(`/api/modelstart/runs/${MS.run.id}/cancel`, { method: "POST" }); } catch (e) { ElMessage.error(e.message); }
}
export async function pauseRun() {
  try { await api(`/api/modelstart/runs/${MS.run.id}/pause`, { method: "POST" }); } catch (e) { ElMessage.error(e.message); }
}
export async function resumeRun() {
  try { await api(`/api/modelstart/runs/${MS.run.id}/resume`, { method: "POST" }); } catch (e) { ElMessage.error(e.message); }
}
export function closeRun() { stopRunPoll(); MS.run = { id: "", status: "", paused: false, started: "", steps: [], logs: [], last: -1 }; }

// ---------- 执行历史(方案运行记录) ----------
export async function loadHistory(silent) {
  MS.history.loading = true;
  try {
    const d = await api("/api/modelstart/runs");
    MS.history.runs = d.runs || [];
    // 当前展开的详情对应的运行已被清空 / 移除时收起
    if (MS.history.detailId && !MS.history.runs.some(r => r.id === MS.history.detailId)) {
      MS.history.detailId = ""; MS.history.detail = null;
    }
  } catch (e) {
    if (!silent) ElMessage.error("加载执行历史失败:" + e.message);
  }
  MS.history.loading = false;
}
export function openHistory() {
  MS.history.visible = !MS.history.visible;   // 底部折叠面板:点击切换展开 / 收起
  if (MS.history.visible) loadHistory();
}
export async function openHistoryRun(r) {
  if (MS.history.detailId === r.id) { MS.history.detailId = ""; MS.history.detail = null; return; }
  const prev = MS.history.detailId;
  MS.history.detailId = r.id;
  MS.history.detail = null;
  try {
    const d = await api(`/api/modelstart/runs/${r.id}?after=-1`);
    if (MS.history.detailId !== r.id) return;   // 期间已点击其它条目
    MS.history.detail = { id: r.id, status: d.status, started: d.started, steps: d.steps || [], logs: d.logs || [] };
  } catch (e) {
    if (MS.history.detailId === r.id) MS.history.detailId = prev;
    ElMessage.error(e.message);
  }
}
export async function clearHistory() {
  try { await ElMessageBox.confirm("清空全部执行历史?进行中的运行不受影响", "清空执行历史", { type: "warning" }); } catch { return; }
  try {
    await api("/api/modelstart/runs", { method: "DELETE" });
    MS.history.runs = []; MS.history.detail = null; MS.history.detailId = "";
    ElMessage.success("执行历史已清空");
  } catch (e) { ElMessage.error(e.message); }
}

// ---------- 工具 ----------
export function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

// 复制文本到剪贴板(优先 Clipboard API,降级 execCommand)
export async function copyText(text) {
  const s = text || "";
  try {
    await navigator.clipboard.writeText(s);
    ElMessage.success("已复制到剪贴板");
    return;
  } catch { /* 降级处理 */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = s;
    ta.style.cssText = "position:fixed;opacity:0;";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    ElMessage.success("已复制到剪贴板");
  } catch {
    ElMessage.error("复制失败,请手动选择复制");
  }
}

// ISO 时间 → 运行时长(如 "3天2时" / "5时12分" / "8分")
export function fmtAge(iso) {
  if (!iso) return "";
  const t = new Date(iso);
  if (isNaN(t.getTime())) return "";
  let s = Math.max(0, Math.floor((Date.now() - t.getTime()) / 1000));
  const d = Math.floor(s / 86400); s %= 86400;
  const h = Math.floor(s / 3600); s %= 3600;
  const m = Math.floor(s / 60);
  if (d) return d + "天" + h + "时";
  if (h) return h + "时" + m + "分";
  return m + "分";
}

// ISO 时间 → 本地可读时间(2026-09-09 08:30)
export function fmtTime(iso) {
  if (!iso) return "";
  const t = new Date(iso);
  if (isNaN(t.getTime())) return String(iso);
  const p = (x) => String(x).padStart(2, "0");
  return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())} ${p(t.getHours())}:${p(t.getMinutes())}`;
}

// 字节数 → 内存大小(docker mem_limit / shm 用)
export function fmtMem(n) {
  if (!n) return "";
  return fmtSize(n);
}
