// ============================== 对话抽屉(Cherry 风格模型对话面板) ==============================
import { reactive, computed, ref, nextTick, watch, onMounted, onUnmounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "./api.js";
import { renderMarkdown } from "./markdown.js";
import { fmtSize } from "./fmt.js";
import { loadSavedApis, saveSavedApiCfg, removeSavedApi, S } from "./ix-store.js";
import { isViewer } from "./auth.js";

const ACCEPT = ".pdf,.docx,.doc,.txt,.md,.csv,.json,.py,.js,.ts,.java,.c,.cpp,.go,.rs,.sh,.bat,.sql,.html,.css,.log,.xml,.yaml,.yml,.toml,.ini,.png,.jpg,.jpeg,.gif,.webp,.bmp,.mp4,.mov,.webm,.mkv,.avi,.m4v";

// ---------- 全局抽屉状态(壳与抽屉共享) ----------
export const C = reactive({
  open: false, width: Math.min(parseInt(localStorage.getItem("chat-width") || "460", 10) || 460, Math.round(window.innerWidth * 0.8)),
  // 面板自己的 API 配置(与主页面共享已保存列表)
  apiUrl: "", apiKey: "", protocol: "openai", model: "",
  models: [], detecting: false,
  savedSel: "",
  // 任务
  taskType: "chat", videoDur: 5,
  thinking: true, webSearch: false, searchCount: 10,
  // 会话
  sessions: [], sessionId: null, history: [],
  sending: false, abort: null, attachments: [],
  input: "", expanded: false,
  // 多选
  selectMode: false, selSet: new Set(),
  // 流式中的消息(发送时展示)
  stream: null,   // {content, think, searchResults, searchMs, searching, generating}
  // 发送失败的错误气泡(不入会话历史,下次发送时清除 — 与旧版一致)
  errorMsg: "",
  // 归档管理弹窗
  projVisible: false,
  // 预览账号:管理员网关渠道信息(只读)
  viewer: { configured: false, channel: "" },
});
window.C = C;

export function toggleChat(open) {
  C.open = open === undefined ? !C.open : !!open;
  if (C.open) {
    if (isViewer()) loadViewerCfg();
    else inheritMainCfg();
    loadSessions();
  }
}

// 预览账号:对话统一走管理员配置的网关渠道(无可编辑 API 配置,仅可选模型)
async function loadViewerCfg() {
  try {
    const d = await api("/api/viewer/chat-cfg");
    C.viewer.configured = !!d.configured;
    C.viewer.channel = d.channel || "";
    if (!d.configured) return;
    C.apiUrl = "(viewer-gateway)";            // 占位:服务端按预览账号自动注入网关渠道
    C.apiKey = "";
    C.models = d.models || [];
    if (!C.model || !C.models.includes(C.model)) C.model = d.model || C.models[0] || "";
  } catch (e) { console.error(e); }
}

// 抽屉自身未配置时,自动带入主页面已检测的配置(开箱即用,不用重复填写)
function inheritMainCfg() {
  if (C.apiUrl || !S.apiUrl) return;
  C.apiUrl = S.apiUrl;
  C.apiKey = S.apiKey;
  C.protocol = S.protocol;
  if (!C.models.length && S.models.length) C.models = S.models.slice();
  if (!C.model && S.model) C.model = S.model;
}

function openModeluse() { window.open("/modeluse", "_blank", "noopener"); }

async function loadSessions() {
  try { C.sessions = await api("/api/chat/sessions"); } catch { C.sessions = []; }
  const active = C.sessions.filter(s => !s.archived);
  if (!active.length) { await createSession(); return; }
  if (!C.sessionId || !C.sessions.find(s => s.id === C.sessionId)) {
    C.sessionId = active[active.length - 1].id;
  }
  loadCurrentSession();
}

async function createSession() {
  const s = await api("/api/chat/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "", archived: false, messages: [] }),
  });
  C.sessions.push(s);
  C.sessionId = s.id;
  C.history = [];
}

function loadCurrentSession() {
  const s = C.sessions.find(x => x.id === C.sessionId);
  C.history = s ? (s.messages || []) : [];
}

async function saveCurrentSession() {
  if (!C.sessionId) return;
  const s = C.sessions.find(x => x.id === C.sessionId);
  const title = s && s.title ? s.title
    : ((C.history.find(m => m.role === "user") || {}).content || "会话").slice(0, 20);
  const body = { title, archived: s ? s.archived : false, messages: C.history, project: s ? (s.project || "") : "" };
  await api(`/api/chat/sessions/${C.sessionId}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (s) s.title = title;
}

// ---------- 检测 / 已保存配置 ----------
async function chatDetect() {
  const url = C.apiUrl.trim();
  if (!url) return;
  C.detecting = true;
  try {
    const data = await api("/api/detect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_url: url, api_key: C.apiKey }),
    });
    C.protocol = data.protocol || "openai";
    C.models = data.models || [];
    if (C.models.length && !C.model) C.model = C.models[0];
    saveSavedApiCfg(url, C.apiKey, C.models);
    ElMessage.success(`识别成功:${data.protocol} · ${data.framework} · ${C.models.length} 个模型`);
  } catch (e) {
    ElMessage.error(`检测失败: ${e.message}`);
  } finally { C.detecting = false; }
}

function removeModelChip(m) {
  C.models = C.models.filter(x => x !== m);
  const cfgs = loadSavedApis();
  const cfg = cfgs.find(c => c.url === C.apiUrl.trim());
  if (cfg) {
    cfg.models = (cfg.models || []).filter(x => x !== m);
    localStorage.setItem("chat-saved-apis", JSON.stringify(cfgs));
  }
  if (C.model === m) C.model = "";
}

function onSavedChange() {
  const i = C.savedSel;
  if (i === "") return;
  const c = loadSavedApis()[parseInt(i, 10)];
  if (!c) return;
  C.apiUrl = c.url;
  C.apiKey = c.key || "";
  C.models = c.models || [];
  if (C.models.length && !C.model) C.model = C.models[0];
}

function onSaveCfg() {
  const url = C.apiUrl.trim();
  if (!url) { ElMessage.warning("请先填写 API 地址"); return; }
  saveSavedApiCfg(url, C.apiKey, C.models);
  ElMessage.success("已保存该 API 配置");
}

function onDelCfg() {
  const i = C.savedSel;
  if (i === "") { ElMessage.warning("请先在下拉中选择要删除的配置"); return; }
  removeSavedApi(parseInt(i, 10));
  C.savedSel = "";
}

// ---------- 归档 / 项目管理 ----------
async function archiveSession() {
  const s = C.sessions.find(x => x.id === C.sessionId);
  if (!s) return;
  let project;
  try {
    const r = await ElMessageBox.prompt("归档到项目名称:", "归档会话", { inputValue: s.project || "" });
    project = r.value;
  } catch { return; }
  s.archived = true;
  s.project = (project || "").trim() || "默认项目";
  await api(`/api/chat/sessions/${s.id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: s.title, archived: true, messages: s.messages, project: s.project }),
  });
  await loadSessions();
}

async function unarchiveSession() {
  const s = C.sessions.find(x => x.id === C.sessionId);
  if (!s) return;
  s.archived = false;
  s.project = "";
  await api(`/api/chat/sessions/${s.id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: s.title, archived: false, messages: s.messages, project: "" }),
  });
  await loadSessions();
}

const projGroups = computed(() => {
  const groups = {};
  C.sessions.filter(s => s.archived)
    .forEach(s => (groups[s.project || "默认项目"] = groups[s.project || "默认项目"] || []).push(s));
  return groups;
});

async function renameProject(proj) {
  let newName;
  try {
    const r = await ElMessageBox.prompt(`重命名项目「${proj}」为:`, "重命名项目", { inputValue: proj });
    newName = (r.value || "").trim();
  } catch { return; }
  if (!newName || newName === proj) return;
  try {
    await api("/api/chat/projects/rename", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: proj, new_name: newName }),
    });
    await loadSessions();
  } catch (e) { ElMessage.error(`重命名失败: ${e.message}`); }
}

async function deleteProject(proj) {
  try {
    await ElMessageBox.confirm(`确定删除项目「${proj}」及其下全部会话?\n删除后不可恢复!`, "删除项目", { type: "warning" });
  } catch { return; }
  try {
    await api("/api/chat/projects/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: proj }),
    });
    await loadSessions();
  } catch (e) { ElMessage.error(`删除失败: ${e.message}`); }
}

async function projPut(sid, body) {
  await api(`/api/chat/sessions/${sid}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function restoreArchived(s) {
  try {
    await projPut(s.id, { title: s.title, archived: false, messages: s.messages, project: "" });
    C.sessionId = s.id;
    await loadSessions();
  } catch (e) { ElMessage.error(`恢复失败: ${e.message}`); }
}

async function renameArchived(s) {
  let nt;
  try {
    const r = await ElMessageBox.prompt("会话重命名:", "重命名", { inputValue: s.title });
    nt = (r.value || "").trim();
  } catch { return; }
  if (!nt || nt === s.title) return;
  try {
    await projPut(s.id, { title: nt, archived: true, messages: s.messages, project: s.project });
    await loadSessions();
  } catch (e) { ElMessage.error(`改名失败: ${e.message}`); }
}

async function deleteArchived(s) {
  try {
    await ElMessageBox.confirm(`确定删除会话「${s.title}」(${(s.messages || []).length} 条消息)?\n删除后不可恢复!`, "删除会话", { type: "warning" });
  } catch { return; }
  try {
    await api(`/api/chat/sessions/${s.id}`, { method: "DELETE" });
    await loadSessions();
  } catch (e) { ElMessage.error(`删除失败: ${e.message}`); }
}

// ---------- 消息操作 ----------
function metricsText(mt) {
  if (!mt) return "";
  const p = [];
  if (mt.duration_s != null) p.push(`⏱ ${mt.duration_s}s`);
  if (mt.ttft_ms != null) p.push(`TTFT ${mt.ttft_ms}ms`);
  if (mt.decode_tps != null) p.push(`${mt.decode_tps} tok/s`);
  if (mt.output_tokens != null) p.push(`${mt.output_tokens} tok`);
  if (mt.think_tokens) p.push(`思维 ${mt.think_tokens} tok`);
  if (mt.search_ms != null) p.push(`🌐 ${mt.search_ms}ms`);
  return p.join(" · ");
}

async function copyMd(m) {
  try { await navigator.clipboard.writeText(m.content || ""); ElMessage.success("已复制"); }
  catch { ElMessage.error("复制失败"); }
}

async function deleteMsgs(msgs) {
  if (!msgs.length || C.sending) return;
  C.history = C.history.filter(x => !msgs.includes(x));
  await saveCurrentSession();
  exitSelect();
}

async function regenerate(m) {
  const idx = C.history.indexOf(m);
  if (idx < 0 || C.sending) return;
  let userIdx = idx - 1;
  while (userIdx >= 0 && C.history[userIdx].role !== "user") userIdx--;
  if (userIdx < 0) return;
  const userMsg = C.history[userIdx].content;
  C.history.splice(userIdx);
  C.input = userMsg;
  await send(true);
}

// ---------- 多选 ----------
function enterSelect(preselect) {
  if (C.sending) return;
  C.selectMode = true;
  C.selSet = new Set(preselect ? [preselect] : []);
}
function exitSelect() {
  C.selectMode = false;
  C.selSet = new Set();
}
function toggleSel(m) {
  if (C.selSet.has(m)) C.selSet.delete(m); else C.selSet.add(m);
}
function selAll() {
  const all = C.history.length > 0 && C.selSet.size === C.history.length;
  C.selSet = all ? new Set() : new Set(C.history);
}
async function selDelete() {
  if (!C.selSet.size) return;
  try {
    await ElMessageBox.confirm(`确定删除所选的 ${C.selSet.size} 条消息?删除后不可恢复。`, "删除消息", { type: "warning" });
  } catch { return; }
  await deleteMsgs([...C.selSet]);
}

// ---------- 附件 ----------
async function onFiles(files) {
  for (const f of files) {
    const isVideo = /\.(mp4|mov|webm|mkv|avi|m4v)$/i.test(f.name);
    const limit = isVideo ? 50 * 1048576 : 20 * 1048576;
    if (f.size > limit) { ElMessage.warning(`${f.name} 超过 ${limit / 1048576}MB,已跳过`); continue; }
    const fd = new FormData();
    fd.append("file", f);
    try {
      const r = await fetch("/api/chat/upload", { method: "POST", body: fd });
      const d = await r.json();
      if (d.name === undefined) throw new Error(d.detail || r.status);
      C.attachments.push(d);
    } catch (e) {
      ElMessage.error(`${f.name} 上传失败: ${e.message}`);
    }
  }
}

// ---------- 发送 ----------
const bodyEl = ref(null);

// Enter 发送;输入法组合中(中文选词确认)不触发,避免误发送
function onChatKeydown(e) {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send(false);
  }
}
function scrollBottom() { nextTick(() => { if (bodyEl.value) bodyEl.value.scrollTop = bodyEl.value.scrollHeight; }); }
watch(() => [C.history.length, C.stream && C.stream.content, C.stream && C.stream.think], scrollBottom, { deep: true });

async function send(isRegen = false) {
  if (C.sending) {
    if (C.abort) C.abort.abort();
    return;
  }
  if (isViewer()) C.taskType = "chat";          // 预览账号:仅文本对话
  const text = C.input.trim();
  if (!text && !C.attachments.length) return;
  if (!C.apiUrl.trim() || !C.model.trim()) {
    ElMessage.error("请先在上方填写 API 地址并检测/选择模型");
    return;
  }
  const taskType = C.taskType;
  const attachments = C.attachments.slice();
  C.sending = true;
  C.errorMsg = "";
  exitSelect();
  C.abort = new AbortController();
  C.input = "";
  C.attachments = [];
  const userMsgObj = { role: "user", content: text };
  C.history.push(userMsgObj);
  C.stream = { content: "", think: "", searchResults: null, searchMs: null, searching: "", generating: taskType !== "chat" ? (taskType === "image" ? "正在生成图片..." : "正在生成视频(完成后自动取回到本地,可能需要几分钟)...") : "" };
  scrollBottom();

  const payload = {
    api_url: C.apiUrl.trim(), api_key: C.apiKey, protocol: C.protocol,
    task_type: taskType, video_duration: parseFloat(C.videoDur) || 5,
    model: C.model.trim(), message: text,
    history: C.history.slice(0, -1).map(m => ({ role: m.role, content: m.content })),
    enable_thinking: C.thinking, web_search: C.webSearch,
    search_count: parseInt(C.searchCount, 10) || 10,
    attachments,
  };
  const sig = { signal: C.abort.signal };
  const mRecord = { role: "assistant", content: "", think: "", metrics: null };

  try {
    if (taskType === "chat") {
      const resp = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload), ...sig,
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let full = "", think = "", lastRender = 0;
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
          try {
            const evt = JSON.parse(ds);
            if (evt.type === "content") {
              full += evt.content;
              const now = Date.now();
              if (now - lastRender > 150) { C.stream.content = full; lastRender = now; }
            } else if (evt.type === "think") {
              think += evt.content;
              C.stream.think = think;
            } else if (evt.type === "searching") {
              C.stream.searching = `🌐 正在联网搜索: ${evt.query || ""}`;
            } else if (evt.type === "search") {
              if (evt.results && evt.results.length) {
                mRecord.search = evt.results;
                mRecord.search_ms = evt.ms;
                C.stream.searchResults = evt.results;
                C.stream.searchMs = evt.ms;
              }
              C.stream.searching = "";
            } else if (evt.type === "metrics") {
              mRecord.metrics = evt.data;
            } else if (evt.type === "error") {
              throw new Error(evt.message);
            }
          } catch (e) { if (e.message && !ds.startsWith("{")) continue; }
        }
      }
      mRecord.content = full;
      mRecord.think = think;
      C.stream.content = full;   // 结束前冲刷最后一段(节流可能落后)
      C.history.push(mRecord);
    } else {
      const resp = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload), ...sig,
      });
      const d = await resp.json();
      if (!resp.ok) throw new Error(d.detail || `HTTP ${resp.status}`);
      mRecord.content = d.url;
      mRecord.type = d.type;
      mRecord.metrics = { duration_s: d.latency_s };
      if (d.media_url) {
        mRecord.media_url = d.media_url;
        mRecord.media_name = d.media_name || "";
        mRecord.media_size = d.media_size || 0;
      } else if (d.media_error) {
        mRecord.media_error = d.media_error;
      }
      C.history.push(mRecord);
    }
    await saveCurrentSession();
  } catch (e) {
    if (e.name === "AbortError") {
      if (mRecord.content) {
        mRecord.content += "\n\n[已停止]";
        C.history.push(mRecord);
        await saveCurrentSession();
      } else {
        C.errorMsg = "[已停止]";
      }
    } else {
      // 错误气泡显示在聊天流内(不写入会话历史),下次发送自动消失
      C.errorMsg = `出错: ${e.message}`;
    }
  } finally {
    C.sending = false;
    C.abort = null;
    C.stream = null;
    scrollBottom();
  }
}

// ---------- 宽度拖拽 ----------
function initResize() {
  let dragging = false;
  const onMove = (e) => {
    if (!dragging) return;
    C.width = Math.min(Math.round(window.innerWidth * 0.8), Math.max(320, Math.round(window.innerWidth - e.clientX)));
  };
  const onUp = () => {
    if (dragging) {
      dragging = false;
      localStorage.setItem("chat-width", String(C.width));
    }
  };
  const el = ref(null);
  onMounted(() => {
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
  onUnmounted(() => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  });
  const start = (e) => { dragging = true; e.preventDefault(); };
  return { start, el };
}

export default {
  setup() {
    const savedApis = computed(() => loadSavedApis());
    const taskLabel = { chat: "对话", image: "文生图", video: "文生视频" };
    const ctxText = computed(() => {
      if (isViewer()) return C.model ? `对话 · ${C.model} · 网关「${C.viewer.channel || "未配置"}」` : "请选择模型";
      return C.model
        ? `${taskLabel[C.taskType]} · ${C.model} · ${C.apiUrl.trim() || "未填地址"}`
        : "请在上方填写 API 地址并检测/选择模型";
    });
    const sessionOptions = computed(() => {
      const active = C.sessions.filter(s => !s.archived);
      const groups = {};
      C.sessions.filter(s => s.archived)
        .forEach(s => (groups[s.project || "默认项目"] = groups[s.project || "默认项目"] || []).push(s));
      const opts = active.map(s => ({ value: s.id, label: `${s.title || "会话"} (${(s.messages || []).length})` }));
      for (const [proj, list] of Object.entries(groups)) {
        for (const s of list) opts.push({ value: s.id, label: `📁 ${proj} / ${s.title}`, archived: true });
      }
      return opts;
    });
    const curSession = computed(() => C.sessions.find(x => x.id === C.sessionId));
    const fileInput = ref(null);
    const { start: startResize } = initResize();

    function onSessionChange(id) {
      C.sessionId = id;
      loadCurrentSession();
    }

    return {
      C, savedApis, ctxText, sessionOptions, curSession, taskLabel, isViewer,
      bodyEl, fileInput, ACCEPT, startResize, openModeluse,
      chatDetect, removeModelChip, onSavedChange, onSaveCfg, onDelCfg,
      createSession, onSessionChange, archiveSession, unarchiveSession,
      projGroups, renameProject, deleteProject, restoreArchived, renameArchived, deleteArchived,
      metricsText, copyMd, deleteMsgs, regenerate,
      enterSelect, exitSelect, toggleSel, selAll, selDelete,
      onFiles, send, onChatKeydown,
      renderMarkdown, fmtSize,
      toggleChat,
    };
  },
  template: `
  <div class="ix-chat-drawer" :class="{ open: C.open }" :style="{ width: C.width + 'px' }">
    <div class="ix-chat-resize" title="拖动调整宽度" @mousedown="startResize"></div>
    <div class="ix-chat-head">
      <span>💬 模型对话</span>
      <span>
        <button class="ix-chat-mini-btn" title="弹出全屏:打开独立的模型工作台页面(/modeluse),URL 可直接分享;会话与本窗口同步"
          @click="openModeluse">⛶ 弹出全屏</button>
        <button class="ix-chat-mini-btn" @click="toggleChat(false)">关闭</button>
      </span>
    </div>

    <div class="ix-chat-cfg">
      <!-- 预览账号:只读网关渠道配置(管理员统一配置,仅可切换模型) -->
      <template v-if="isViewer()">
        <div class="ix-row">
          <el-select v-model="C.model" size="small" filterable placeholder="选择模型">
            <el-option v-for="m in C.models" :key="m" :value="m" :label="m"/>
          </el-select>
        </div>
        <div v-if="C.viewer.configured" class="ix-note" style="margin-top:6px;">
          👁 预览模式:对话由管理员配置的网关渠道「{{ C.viewer.channel }}」提供,不可修改
        </div>
        <div v-else class="ix-note" style="margin-top:6px;color:var(--el-color-danger);">
          ⚠️ 管理员尚未在网关配置可用渠道,暂无法对话
        </div>
      </template>
      <template v-else>
      <div class="ix-row">
        <el-select v-model="C.savedSel" placeholder="— 选择已保存的 API —" size="small" @change="onSavedChange">
          <el-option v-for="(c, i) in savedApis" :key="i" :value="String(i)" :label="c.url"/>
        </el-select>
        <el-button size="small" style="flex:none;" title="保存当前 API+Key 供以后选择" @click="onSaveCfg">存</el-button>
        <el-button size="small" type="danger" style="flex:none;" title="删除选中的已保存配置" @click="onDelCfg">删</el-button>
      </div>
      <div class="ix-row" style="margin-top:6px;">
        <el-input v-model="C.apiUrl" size="small" placeholder="API 地址,如 http://10.0.1.8:32000"/>
        <el-button size="small" style="flex:none;" :loading="C.detecting" @click="chatDetect">检测</el-button>
      </div>
      <div class="ix-row" style="margin-top:6px;">
        <el-input v-model="C.apiKey" size="small" type="password" show-password placeholder="API Key (可留空)"/>
      </div>
      <div class="ix-row" style="margin-top:6px;">
        <el-select v-model="C.protocol" size="small" style="width:38%;flex:none;">
          <el-option value="openai" label="OpenAI"/>
          <el-option value="anthropic" label="Anthropic"/>
          <el-option value="ollama" label="Ollama"/>
        </el-select>
        <el-select v-model="C.model" size="small" filterable allow-create default-first-option placeholder="模型(可输入或选择)">
          <el-option v-for="m in C.models" :key="m" :value="m" :label="m"/>
        </el-select>
      </div>
      <div v-if="C.models.length" class="ix-chipbox" style="margin-top:6px;max-height:72px;overflow:auto;">
        <span v-for="m in C.models" :key="m" class="ix-chip" :class="{ sel: C.model === m }">
          <span title="点击选用该模型" @click="C.model = m">{{ m }}</span>
          <span class="ix-chip-x" title="从模型历史删除" @click.stop="removeModelChip(m)">×</span>
        </span>
      </div>
      </template>
    </div>

    <div class="ix-chat-toolbar">
      <el-select v-model="C.sessionId" size="small" style="flex:1;min-width:120px;"
        :disabled="C.sending" title="生成中暂不可切换会话" @change="onSessionChange">
        <el-option v-for="o in sessionOptions" :key="o.value" :value="o.value" :label="o.label"/>
      </el-select>
      <el-button size="small" title="新建会话" :disabled="C.sending" @click="createSession">＋ 新建</el-button>
      <el-button v-if="curSession && !curSession.archived" size="small" title="归档到项目" @click="archiveSession">归档</el-button>
      <el-button v-if="curSession && curSession.archived" size="small" title="取消归档" @click="unarchiveSession">取消归档</el-button>
      <el-button size="small" title="归档项目管理:重命名/删除项目,恢复/删除归档会话" @click="C.projVisible = true">📦 归档管理</el-button>
    </div>
    <div class="ix-chat-seg-row">
      <div class="ix-seg" style="flex:none;">
        <button type="button" :class="{ on: C.taskType === 'chat' }" @click="C.taskType = 'chat'">💬 对话</button>
        <button v-if="!isViewer()" type="button" :class="{ on: C.taskType === 'image' }" @click="C.taskType = 'image'">🎨 文生图</button>
        <button v-if="!isViewer()" type="button" :class="{ on: C.taskType === 'video' }" @click="C.taskType = 'video'">🎬 文生视频</button>
      </div>
      <select v-if="C.taskType === 'video'" v-model.number="C.videoDur" class="st-lang" title="视频时长(秒)">
        <option v-for="d in [5, 10, 15, 20, 25, 30]" :key="d" :value="d">⏱ {{ d }}s</option>
      </select>
    </div>
    <div class="ix-chat-ctx">{{ ctxText }}</div>

    <div v-if="C.selectMode" class="ix-chat-selbar">
      <span>已选 {{ C.selSet.size }} 条</span>
      <el-button size="small" text @click="selAll">{{ C.history.length > 0 && C.selSet.size === C.history.length ? '取消全选' : '全选' }}</el-button>
      <el-button size="small" text type="danger" @click="selDelete">🗑 删除所选</el-button>
      <el-button size="small" text @click="exitSelect">完成</el-button>
    </div>

    <div ref="bodyEl" class="ix-chat-body" :class="{ 'select-mode': C.selectMode }">
      <div v-for="(m, i) in C.history" :key="i" class="ix-msg"
        :class="[m.role === 'user' ? 'user' : 'ai', { selected: C.selSet.has(m) }]"
        @click="C.selectMode && toggleSel(m)">
        <template v-if="m.role === 'user'">
          <div class="bubble user">{{ m.content }}</div>
        </template>
        <template v-else>
          <details v-if="m.search && m.search.length" class="ix-search-block">
            <summary>已联网搜索({{ m.search.length }} 条结果<template v-if="m.search_ms">,耗时 {{ m.search_ms }} ms</template>)</summary>
            <div class="ix-search-body">
              <div v-for="(r, j) in m.search" :key="j" class="ix-search-item">
                <a :href="r.url" target="_blank" rel="noopener noreferrer">{{ j + 1 }}. {{ r.title || r.url }}</a>
                <div v-if="r.snippet" class="ix-search-snippet">{{ r.snippet }}</div>
              </div>
            </div>
          </details>
          <details v-if="m.think" class="ix-think-block">
            <summary>🧠 思维链 ({{ (m.think || '').length }} 字)</summary>
            <div class="ix-think-body">{{ m.think }}</div>
          </details>
          <div v-if="m.type === 'image' || m.type === 'video'" class="ix-msg-media">
            <template v-if="m.type === 'image'">
              <img v-if="m.media_url || m.content" :src="m.media_url || m.content" alt="生成图片"/>
            </template>
            <template v-else-if="m.type === 'video'">
              <video v-if="m.media_url || m.content" :src="m.media_url || m.content" controls preload="metadata" playsinline/>
            </template>
            <div class="ix-media-meta">
              原始输出:
              <a v-if="/^https?:\\/\\//.test(m.content || '')" :href="m.content" target="_blank" rel="noopener noreferrer">{{ m.content }}</a>
              <template v-else>{{ m.content || '(无)' }}</template>
              <template v-if="m.media_size"> ({{ fmtSize(m.media_size) }})</template>
            </div>
            <div v-if="m.media_error" class="ix-media-meta" style="color:var(--el-color-warning);">
              ⚠ 未能取回{{ m.type === 'video' ? '视频' : '图片' }}文件(仅能展示原始输出): {{ m.media_error }}
            </div>
          </div>
          <div v-else class="md-body" v-html="renderMarkdown(m.content)"></div>
          <div v-if="m.metrics" class="ix-msg-metrics">{{ metricsText(m.metrics) }}</div>
          <div class="ix-msg-actions">
            <el-button text size="small" @click.stop="copyMd(m)">复制MD</el-button>
            <el-button text size="small" @click.stop="regenerate(m)">重新生成</el-button>
            <el-button text size="small" type="danger" @click.stop="deleteMsgs([m])">删除</el-button>
            <el-button text size="small" title="进入多选模式,可勾选多条消息批量删除" @click.stop="enterSelect(m)">多选</el-button>
            <a v-if="(m.type === 'image' || m.type === 'video') && m.media_url" class="ix-msg-dl"
              :href="m.media_url + '?download=1'" :download="m.media_name || (m.type === 'video' ? 'video.mp4' : 'image.png')"
              @click.stop>{{ m.type === 'video' ? '⬇ 下载原视频' : '⬇ 下载原图' }}</a>
          </div>
        </template>
      </div>

      <!-- 流式中的回复 -->
      <div v-if="C.stream" class="ix-msg ai">
        <details v-if="C.stream.searchResults" class="ix-search-block" open>
          <summary>已联网搜索({{ C.stream.searchResults.length }} 条结果<template v-if="C.stream.searchMs">,耗时 {{ C.stream.searchMs }} ms</template>)</summary>
          <div class="ix-search-body">
            <div v-for="(r, j) in C.stream.searchResults" :key="j" class="ix-search-item">
              <a :href="r.url" target="_blank" rel="noopener noreferrer">{{ j + 1 }}. {{ r.title || r.url }}</a>
              <div v-if="r.snippet" class="ix-search-snippet">{{ r.snippet }}</div>
            </div>
          </div>
        </details>
        <details v-if="C.stream.think" class="ix-think-block" open>
          <summary>🧠 思维链 (生成中...)</summary>
          <div class="ix-think-body">{{ C.stream.think }}</div>
        </details>
        <div v-if="C.stream.searching" class="ix-typing">{{ C.stream.searching }}</div>
        <div v-else-if="C.stream.generating" class="ix-typing">{{ C.stream.generating }}</div>
        <div v-if="C.stream.content" class="md-body" v-html="renderMarkdown(C.stream.content)"></div>
      </div>

      <!-- 发送失败的错误气泡(不入历史,下次发送消失) -->
      <div v-if="C.errorMsg" class="ix-msg ai"><div class="bubble error">{{ C.errorMsg }}</div></div>
    </div>

    <div v-if="C.attachments.length" class="ix-attach-row">
      <span v-for="(att, i) in C.attachments" :key="i" class="ix-attach-chip">
        <img v-if="att.type === 'image'" :src="att.content" alt=""/>
        <video v-else-if="att.type === 'video'" :src="att.content" muted></video>
        <template v-else>📄</template>
        <span class="nm" :title="att.name">{{ att.name }}</span>
        <span class="x" @click="C.attachments.splice(i, 1)">×</span>
      </span>
    </div>

    <div class="ix-chat-preinput">
      <el-checkbox v-model="C.thinking" size="small">🧠 思维链</el-checkbox>
      <el-checkbox v-model="C.webSearch" size="small"
        title="发送前由本服务真实联网搜索(Bing/DuckDuckGo),先用模型提炼搜索词多路搜索,结果注入对话上下文;模型不可用时自动取消">🌐 联网搜索</el-checkbox>
      <select v-if="C.webSearch" v-model.number="C.searchCount" class="st-lang" title="联网搜索结果条数(多路搜索合并去重后)">
        <option value="5">5 条</option><option value="8">8 条</option><option value="10">10 条</option>
        <option value="15">15 条</option><option value="20">20 条</option>
      </select>
      <span style="margin-left:auto;font-size:11px;color:var(--el-text-color-secondary);">{{ C.history.length }} 条</span>
    </div>
    <div class="ix-chat-input-row">
      <el-button size="small" style="flex:none;" title="上传文件(PDF/Word/TXT/MD/图片/视频)"
        @click="fileInput && fileInput.click()">📎</el-button>
      <input ref="fileInput" type="file" hidden multiple :accept="ACCEPT"
        @change="onFiles([...$event.target.files]); $event.target.value = ''"/>
      <el-button size="small" style="flex:none;" title="扩大/收起输入框" @click="C.expanded = !C.expanded">⤢</el-button>
      <textarea v-model="C.input" class="ix-chat-input" :class="{ expanded: C.expanded }"
        placeholder="输入消息,Enter 发送,Shift+Enter 换行"
        @keydown="onChatKeydown"></textarea>
      <el-button :type="C.sending ? 'danger' : 'primary'" style="flex:none;" @click="send(false)">{{ C.sending ? '停止' : '发送' }}</el-button>
    </div>

    <!-- 归档项目管理弹窗 -->
    <el-dialog v-model="C.projVisible" title="📦 归档项目管理" width="min(600px, 94vw)">
      <div class="ix-note" style="margin-bottom:8px;">重命名 / 删除项目 · 恢复 / 改名 / 删除会话</div>
      <div v-if="!Object.keys(projGroups).length" class="ix-note">
        暂无归档项目。在对话面板点「归档」把会话归档到项目后,可在这里统一管理。
      </div>
      <div v-for="(list, proj) in projGroups" :key="proj" class="ix-proj-group">
        <div class="ix-proj-head">
          <b>📁 {{ proj }}</b>
          <span class="cnt">{{ list.length }} 个会话</span>
          <span class="ops">
            <el-button size="small" @click="renameProject(proj)">✏️ 重命名项目</el-button>
            <el-button size="small" type="danger" @click="deleteProject(proj)">🗑 删除项目</el-button>
          </span>
        </div>
        <div v-for="s in list" :key="s.id" class="ix-proj-sess">
          <span class="t" :title="s.title">{{ s.title }}</span>
          <span class="m">{{ (s.messages || []).length }} 条消息</span>
          <span class="ops">
            <el-button size="small" @click="restoreArchived(s)">↩ 恢复</el-button>
            <el-button size="small" @click="renameArchived(s)">✏️</el-button>
            <el-button size="small" type="danger" @click="deleteArchived(s)">🗑</el-button>
          </span>
        </div>
      </div>
    </el-dialog>
  </div>`,
};
