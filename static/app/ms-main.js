// ============================== 模型部署站视图(Model Start,SPA 子页) ==============================
// SSH 主机(分组/密钥登录)/ 容器检测 / 文件浏览 / 命令执行 / 预设 / 任务编排 / 容器终端
import { reactive, ref, computed, watch, onMounted, onUnmounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  MS, loadHosts, saveHosts, probeHost, selectHost, loadPresets, loadPlans, stopRunPoll,
  hostGroups, ungroupedHosts, groupNames, toggleGroup, isGroupCollapsed,
} from "./ms-store.js";
import { AUTH, isAdmin, isViewer, canManageHosts, loadUsers, assignHost } from "./auth.js";
import { ContainersView, PresetsView, PlansView } from "./ms-views.js";

// ---------- 迷你终端模拟器:解析 ANSI/SGR,维护 行列表 + 光标 ----------
const ANSI_FG = {
  30: "#6b7280", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b", 34: "#61afef",
  35: "#c678dd", 36: "#56b6c2", 37: "#d7dae0",
  90: "#7f848e", 91: "#ff7b86", 92: "#b5e890", 93: "#ffd479", 94: "#84c1ff",
  95: "#e29bed", 96: "#79e0f2", 97: "#ffffff",
};
function _esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function _lineLen(line) { let n = 0; for (const r of line) n += r.t.length; return n; }
function makeTerm() { return { lines: [[]], cy: 0, cx: 0, color: "", bold: false, esc: "" }; }
function _ensure(T) { while (T.lines.length <= T.cy) T.lines.push([]); }
function _trunc(line, cx) {          // 保留行内前 cx 个字符
  let pos = 0;
  for (let ri = 0; ri < line.length; ri++) {
    const l = line[ri].t.length;
    if (pos + l > cx) {
      const keep = line[ri].t.slice(0, cx - pos);
      const style = { c: line[ri].c, b: line[ri].b };
      line.splice(ri, line.length - ri);
      if (keep) line.push({ t: keep, ...style });
      return;
    }
    pos += l;
  }
}
function _put(T, ch) {
  _ensure(T);
  const line = T.lines[T.cy];
  const last = line[line.length - 1];
  const total = _lineLen(line);
  if (T.cx === total) {                       // 行尾追加(快路径)
    if (last && last.c === T.color && last.b === T.bold) last.t += ch;
    else line.push({ t: ch, c: T.color, b: T.bold });
  } else if (T.cx > total) {                   // 光标越过行尾:补空格
    line.push({ t: " ".repeat(T.cx - total), c: "", b: false });
    line.push({ t: ch, c: T.color, b: T.bold });
  } else {                                    // 覆盖中间
    let pos = 0, ri = 0;
    for (; ri < line.length; ri++) {
      if (pos + line[ri].t.length > T.cx) break;
      pos += line[ri].t.length;
    }
    const run = line[ri];
    const off = T.cx - pos;
    const before = run.t.slice(0, off), after = run.t.slice(off + 1);
    const repl = [];
    if (before) repl.push({ t: before, c: run.c, b: run.b });
    repl.push({ t: ch, c: T.color, b: T.bold });
    if (after) repl.push({ t: after, c: run.c, b: run.b });
    line.splice(ri, 1, ...repl);
  }
  T.cx++;
}
function termWrite(T, text) {
  for (const ch of text) {
    if (T.esc) {                                // 转义序列缓冲
      T.esc += ch;
      if (T.esc.length === 2 && ch === "]") continue;    // OSC(窗口标题等)
      if (T.esc[1] === "]") {
        if (ch === "\x07" || (ch === "\\" && T.esc.endsWith("\x1b"))) T.esc = "";
        if (T.esc.length > 512) T.esc = "";
        continue;
      }
      if (ch === "m") {                        // SGR 颜色
        const params = T.esc.slice(2, -1).split(";").map(x => parseInt(x || "0", 10) || 0);
        for (const p of params) {
          if (p === 0) { T.color = ""; T.bold = false; }
          else if (p === 1) T.bold = true;
          else if (ANSI_FG[p]) T.color = ANSI_FG[p];
        }
        T.esc = "";
      } else if (/[A-Za-z@`~]/.test(ch)) {     // 其它 CSI:光标移动/擦除
        const body = T.esc.slice(2, -1);
        const n = Math.max(1, parseInt(body || "1", 10) || 1);
        if (ch === "A") T.cy = Math.max(0, T.cy - n);
        else if (ch === "B") { T.cy += n; _ensure(T); }
        else if (ch === "C") T.cx += n;
        else if (ch === "D") T.cx = Math.max(0, T.cx - n);
        else if (ch === "H") T.cx = 0;
        else if (ch === "F") { _ensure(T); T.cx = _lineLen(T.lines[T.cy]); }
        else if (ch === "K") {                  // 擦行(0/2 常用)
          _ensure(T);
          _trunc(T.lines[T.cy], body === "1" ? T.cx : (body === "2" ? 0 : T.cx));
        } else if (ch === "J" && (body === "2" || body === "")) {
          T.lines = [[]]; T.cy = 0; T.cx = 0; T.color = ""; T.bold = false;
        }
        T.esc = "";
      } else if (T.esc.length > 32) T.esc = "";
      continue;
    }
    if (ch === "\x1b") { T.esc = "\x1b"; continue; }
    if (ch === "\n") { T.cy++; _ensure(T); T.cx = 0; continue; }
    if (ch === "\r") { T.cx = 0; continue; }
    if (ch === "\x08") { T.cx = Math.max(0, T.cx - 1); continue; }
    if (ch < " ") continue;
    _put(T, ch);
  }
  if (T.lines.length > 3000) {                  // 防爆内存:只留最近 3000 行
    const cut = T.lines.length - 3000;
    T.lines.splice(0, cut);
    T.cy = Math.max(0, T.cy - cut);
  }
}
function termHtml(T, caret) {
  const out = [];
  for (let y = 0; y < T.lines.length; y++) {
    let h = "";
    for (const r of T.lines[y]) {
      if (!r.t) continue;
      if (r.c || r.b) {
        const st = [];
        if (r.c) st.push("color:" + r.c);
        if (r.b) st.push("font-weight:700");
        h += `<span style="${st.join(";")}">${_esc(r.t)}</span>`;
      } else h += _esc(r.t);
    }
    out.push(h || " ");
  }
  return out.join("\n") + (caret ? '<span class="ms-caret"> </span>' : "");
}

// ---------- 底部容器终端抽屉 ----------
const TerminalDock = {
  setup() {
    const preRef = ref(null), inputRef = ref(null), screenRef = ref(null);
    let ws = null, T = makeTerm(), raf = 0, autoscroll = true;
    function renderTerm() {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const pre = preRef.value;
        if (!pre) return;
        pre.innerHTML = termHtml(T, MS.term.connected);
        if (autoscroll) {
          const sc = screenRef.value;
          if (sc) sc.scrollTop = sc.scrollHeight;
        }
      });
    }
    function feed(text) { termWrite(T, text); renderTerm(); }
    function send(data) {
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "input", data }));
    }
    function sendResize() {
      const sc = screenRef.value;
      if (!sc || !ws || ws.readyState !== 1) return;
      const cols = Math.max(40, Math.min(400, Math.floor(sc.clientWidth / 8)));
      const rows = Math.max(8, Math.min(200, Math.floor(sc.clientHeight / 17)));
      try { ws.send(JSON.stringify({ type: "resize", cols, rows })); } catch {}
    }
    function doConnect() {
      if (!MS.term.host) { ElMessage.warning("请选择主机"); return; }
      if (!MS.term.container.trim()) { ElMessage.warning("请填写容器名 / Pod 名"); return; }
      disconnect();
      T = makeTerm();
      const q = new URLSearchParams({
        host: MS.term.host, runtime: MS.term.runtime,
        container: MS.term.container.trim(),
        namespace: MS.term.namespace || "default",
      });
      const proto = location.protocol === "https:" ? "wss" : "ws";
      try {
        ws = new WebSocket(`${proto}://${location.host}/api/modelstart/terminal?${q}`);
      } catch (e) {
        ElMessage.error("WebSocket 创建失败:" + e.message);
        return;
      }
      ws.onopen = () => { MS.term.connected = true; setTimeout(sendResize, 300); };
      ws.onmessage = (ev) => {
        let msg = null;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "output") feed(msg.data || "");
        else if (msg.type === "error") { ElMessage.error(msg.text || "终端错误"); MS.term.connected = false; }
      };
      ws.onclose = () => { MS.term.connected = false; feed("\r\n\x1b[33m── 连接已断开 ──\x1b[0m\r\n"); };
      ws.onerror = () => { MS.term.connected = false; };
      feed("\x1b[36m连接中…" + (MS.term.runtime === "k8s" ? `kubectl exec -it ${MS.term.container}` : `docker exec -it ${MS.term.container}`) + "…\x1b[0m\r\n");
    }
    function disconnect() {
      if (ws) { try { ws.close(); } catch {} ws = null; }
      MS.term.connected = false;
    }
    function toggle() { MS.term.open = !MS.term.open; }
    function clearTerm() { T = makeTerm(); renderTerm(); }
    function onScroll() {
      const sc = screenRef.value;
      if (!sc) return;
      autoscroll = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 48;
    }
    function focusInput() { if (inputRef.value) inputRef.value.focus(); }
    function onKey(e) {
      if (e.isComposing || e.keyCode === 229) return;      // 中文输入法组词中
      if (e.ctrlKey || e.metaKey) {
        const map = { c: "\x03", d: "\x04", l: "\x0c", u: "\x15", a: "\x01", e: "\x05", z: "\x1a", w: "\x17", k: "\x0b", r: "\x12", b: "\x02", f: "\x06", n: "\x0e", p: "\x10" };
        const seq = map[e.key.toLowerCase()];
        if (seq) { send(seq); e.preventDefault(); }
        return;
      }
      switch (e.key) {
        case "Enter": send("\r"); break;
        case "Backspace": send("\x7f"); break;
        case "Tab": send("\t"); break;
        case "ArrowUp": send("\x1b[A"); break;
        case "ArrowDown": send("\x1b[B"); break;
        case "ArrowRight": send("\x1b[C"); break;
        case "ArrowLeft": send("\x1b[D"); break;
        case "Home": send("\x1b[H"); break;
        case "End": send("\x1b[F"); break;
        case "Delete": send("\x1b[3~"); break;
        case "PageUp": send("\x1b[5~"); break;
        case "PageDown": send("\x1b[6~"); break;
        case "Escape": send("\x1b"); break;
        default:
          if (e.key.length === 1) send(e.key); else return;
      }
      e.preventDefault();
    }
    function onInput(e) {                     // 输入法组词 / 粘贴落到这里
      const ta = e.target;
      if (ta.value) { send(ta.value); ta.value = ""; }
    }
    watch(() => MS.term.connectReq, () => { if (MS.term.connectReq) doConnect(); });
    onUnmounted(() => { disconnect(); if (raf) cancelAnimationFrame(raf); });
    return { MS, preRef, inputRef, screenRef, doConnect, disconnect, toggle, clearTerm, onKey, onInput, onScroll, focusInput };
  },
  template: `
  <div class="ms-term" :class="{ open: MS.term.open }">
    <div class="ms-term-bar" @click="toggle">
      <span>⌨ 容器终端 <span class="ix-note-inline">登录容器内直接修改 · 作为页面功能补充</span></span>
      <span class="grow"></span>
      <span class="ms-term-status" :class="{ on: MS.term.connected }">● {{ MS.term.connected ? "已连接" : "未连接" }}</span>
      <i class="ms-term-arrow">{{ MS.term.open ? "▾" : "▴" }}</i>
    </div>
    <div v-if="MS.term.open" class="ms-term-body">
      <div class="ms-term-cfg" @click.stop>
        <span class="pl">主机</span>
        <el-select v-model="MS.term.host" size="small" style="width:160px;" placeholder="选择主机">
          <el-option v-for="h in MS.hosts" :key="h.id" :value="h.id" :label="h.name"/>
        </el-select>
        <span class="pl">运行时</span>
        <el-select v-model="MS.term.runtime" size="small" style="width:88px;">
          <el-option value="docker" label="Docker"/><el-option value="k8s" label="K8s"/>
        </el-select>
        <el-input v-model="MS.term.container" size="small" style="width:210px;" placeholder="容器名 / Pod 名"/>
        <el-input v-if="MS.term.runtime === 'k8s'" v-model="MS.term.namespace" size="small" style="width:120px;" placeholder="命名空间"/>
        <el-button size="small" type="primary" :disabled="MS.term.connected" @click="doConnect()">连接</el-button>
        <el-button size="small" :disabled="!MS.term.connected" @click="disconnect()">断开</el-button>
        <el-button size="small" @click="clearTerm()">清屏</el-button>
        <span class="ix-note-inline">点击终端区域获得键盘;支持方向键 / Tab / Ctrl+C / Ctrl+L / 中文输入</span>
      </div>
      <div class="ms-term-screen" ref="screenRef" @click="focusInput" @scroll="onScroll">
        <pre class="ms-term-pre" ref="preRef"></pre>
        <textarea class="ms-term-input" ref="inputRef" @keydown="onKey" @input="onInput"
                  autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"></textarea>
      </div>
    </div>
  </div>`,
};

// ---------- 模型部署站视图 ----------
const ModelStartView = {
  components: { ContainersView, PresetsView, PlansView, TerminalDock },
  setup() {
    // ---------- SSH 主机(分组侧栏 + 编辑对话框:单台/批量添加) ----------
    const hostDlg = reactive({
      visible: false,
      mode: "single",          // single=单台添加 | batch=批量粘贴添加(仅新增时)
      batch: "",               // 批量模式文本:每行 [名称(可选)] 用户@主机[:端口]
      form: { id: "", name: "", host: "", ssh_port: 22, username: "root", auth: "password", password: "", private_key: "", passphrase: "", group: "" },
      subs: [],                 // 子账号列表(管理员分配主机用)
      assignedSubs: [],         // 当前主机被分配给的子账号
    });
    async function _loadSubs() {
      if (!isAdmin()) { hostDlg.subs = []; return; }
      try { hostDlg.subs = ((await loadUsers()).users || []).filter(u => u.role === "sub"); }
      catch { hostDlg.subs = []; }
    }
    // 批量行解析:[名称(可选)] [用户@]主机[:端口];# 开头为注释;返回 null=跳过,{bad:true}=无法解析
    function _parseHostLine(line) {
      const s = line.trim();
      if (!s || s.startsWith("#")) return null;
      let name = "", target = s;
      const sp = s.split(/\s+/);
      if (sp.length === 2) { name = sp[0]; target = sp[1]; }
      else if (sp.length > 2) return { bad: true };
      const m = target.match(/^(?:([A-Za-z0-9._-]+)@)?([A-Za-z0-9._-]+|\[[0-9a-fA-F:]+\])(?::(\d{1,5}))?$/);
      if (!m) return { bad: true };
      if (m[3] != null) {
        const port = +m[3];
        if (port < 1 || port > 65535) return { bad: true };
        return { name, username: m[1] || "", host: m[2], port };
      }
      return { name, username: m[1] || "", host: m[2], port: 0 };   // port=0 表示未指定,沿用默认
    }
    const batchParsed = computed(() => {
      const list = [], bad = [];
      for (const line of (hostDlg.batch || "").split("\n")) {
        const r = _parseHostLine(line);
        if (r == null) continue;
        if (r.bad) { bad.push(line.trim()); continue; }
        list.push(r);
      }
      return { list, bad };
    });
    function addHost() {
      hostDlg.form = { id: "", name: "", host: "", ssh_port: 22, username: "root", auth: "password", password: "", private_key: "", passphrase: "", group: "" };
      hostDlg.mode = "single";
      hostDlg.batch = "";
      hostDlg.assignedSubs = [];
      _loadSubs();
      hostDlg.visible = true;
    }
    async function editHost(h) {
      hostDlg.form = { id: h.id, name: h.name, host: h.host, ssh_port: h.ssh_port, username: h.username,
                       auth: h.auth || "password", password: "", private_key: "", passphrase: "", group: h.group || "" };
      hostDlg.mode = "single";
      hostDlg.batch = "";
      await _loadSubs();
      hostDlg.assignedSubs = hostDlg.subs.filter(u => (u.hosts || []).includes(h.id)).map(u => u.username);
      hostDlg.visible = true;
    }
    // 私钥文件:本地读取内容填入文本框(不上传服务端)
    const keyFileRef = ref(null);
    function pickKeyFile() { if (keyFileRef.value) { keyFileRef.value.value = ""; keyFileRef.value.click(); } }
    function onKeyFile(e) {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      const r = new FileReader();
      r.onload = () => {
        hostDlg.form.private_key = String(r.result || "").trim();
        ElMessage.success(`已读取私钥「${f.name}」(${f.size} 字节)`);
      };
      r.readAsText(f);
    }
    async function saveHostDlg(thenProbe) {
      const f = hostDlg.form;
      // ---------- 批量模式:按行解析,登录方式 / 分组对全部主机生效 ----------
      if (hostDlg.mode === "batch") {
        const parsed = batchParsed.value;
        if (!parsed.list.length) { ElMessage.warning("没有可识别的主机行;每行格式:名称(可选) 用户@主机:端口"); return; }
        const start = MS.hosts.length;
        for (const it of parsed.list) {
          MS.hosts.push({ id: "", name: it.name || it.host, host: it.host,
                          ssh_port: it.port || (+f.ssh_port || 22),
                          username: it.username || f.username.trim() || "root",
                          auth: f.auth, password: f.password, private_key: f.private_key,
                          passphrase: f.passphrase, group: f.group.trim() });
        }
        hostDlg.visible = false;
        await saveHosts();
        ElMessage.success(`已添加 ${parsed.list.length} 台主机` + (parsed.bad.length ? `,${parsed.bad.length} 行无法解析已跳过` : ""));
        if (thenProbe) for (const h of MS.hosts.slice(start)) await probeHost(h);
        return;
      }
      // ---------- 单台模式 ----------
      if (!f.host.trim()) { ElMessage.warning("主机地址不能为空"); return; }
      const port = +f.ssh_port;
      if (!(port >= 1 && port <= 65535)) { ElMessage.warning("SSH 端口需为 1-65535 的数字"); return; }
      const name = f.name.trim() || f.host.trim();          // 名称留空时用主机地址
      if (f.id) {
        const h = MS.hosts.find(x => x.id === f.id);
        if (h) {
          h.name = name; h.host = f.host.trim(); h.ssh_port = port;
          h.username = f.username.trim() || "root";
          h.auth = f.auth; h.group = f.group.trim();
          if (f.password) h.password = f.password;
          if (f.private_key.trim()) h.private_key = f.private_key;
          if (f.passphrase) h.passphrase = f.passphrase;
        }
      } else {
        MS.hosts.push({ id: "", name, host: f.host.trim(), ssh_port: port,
                        username: f.username.trim() || "root", auth: f.auth, password: f.password,
                        private_key: f.private_key, passphrase: f.passphrase, group: f.group.trim() });
      }
      hostDlg.visible = false;
      await saveHosts();
      // 主机分配(管理员,编辑已有主机时)
      if (isAdmin() && f.id) {
        try { await assignHost(f.id, hostDlg.assignedSubs); }
        catch (e) { ElMessage.warning("主机分配未保存:" + e.message); }
      }
      if (thenProbe) {
        const saved = f.id ? MS.hosts.find(x => x.id === f.id) : MS.hosts[MS.hosts.length - 1];
        if (saved) {
          await probeHost(saved);
          const r = MS.hostProbe[saved.id];
          if (r && !r.ok) hostDlg.visible = true;   // 探测失败:重开对话框并保留所填内容,便于直接改
        }
      }
    }
    async function delHost(h) {
      try { await ElMessageBox.confirm(`删除主机「${h.name || h.host}」?`, "删除主机", { type: "warning" }); } catch { return; }
      MS.hosts = MS.hosts.filter(x => x.id !== h.id);
      if (MS.activeHost === h.id) selectHost("");
      await saveHosts();
    }
    const activeHostObj = computed(() => MS.hosts.find(h => h.id === MS.activeHost) || null);
    const probeInfo = computed(() => {
      const p = MS.hostProbe[MS.activeHost];
      if (!p) return "";
      if (!p.ok) return `<span style="color:var(--el-color-danger);">✗ 探测失败:${_esc(String(p.raw || "").slice(0, 160))}</span>`;
      const i = p.info || {};
      const row = (k, v) => v ? `<div><b style="color:var(--el-color-primary);">${k}</b> ${_esc(String(v))}</div>` : "";
      return `<div class="ms-probe-ok">${row("主机", i.HOST)}${row("内核", i.KERNEL)}${row("Docker", i.DOCKER)}${row("kubectl", i.KUBECTL)}${row("GPU", i.GPU)}</div>`;
    });
    const probeCls = (id) => {
      const p = MS.hostProbe[id];
      if (!p) return "";
      return p.ok ? "ok" : "err";
    };

    onMounted(async () => {
      await Promise.all([loadHosts(), loadPresets(), loadPlans()]);
    });
    onUnmounted(stopRunPoll);

    return {
      MS, AUTH, isAdmin, isViewer, canManageHosts, hostDlg, addHost, editHost, saveHostDlg, delHost, probeHost, selectHost,
      activeHostObj, probeInfo, probeCls, batchParsed, keyFileRef, pickKeyFile, onKeyFile,
      hostGroups, ungroupedHosts, groupNames, toggleGroup, isGroupCollapsed,
    };
  },
  template: `
  <div class="ms-shell">
    <div class="ms-body">
      <aside class="ms-aside">
        <div class="aside-head">
          <span class="aside-title">SSH 主机<em>({{ MS.hosts.length }})</em></span>
          <span class="grow"></span>
          <el-button v-if="canManageHosts()" size="small" type="primary" @click="addHost"><el-icon><Plus/></el-icon>添加</el-button>
        </div>
        <div class="ms-host-filter aside-search">
          <el-input v-model="MS.hostFilter" size="small" clearable placeholder="搜索主机 / IP…" prefix-icon="Search"/>
        </div>
        <div class="ms-host-list">
          <div v-for="h in ungroupedHosts()" :key="h.id" class="ms-host-item" :class="{ on: h.id === MS.activeHost }">
            <div class="main" @click="selectHost(h.id)">
              <div class="t"><i class="dot" :class="probeCls(h.id)"></i>{{ h.name }}<span class="ms-auth-badge" :title="h.auth === 'key' ? '密钥登录' : '密码登录'">{{ h.auth === 'key' ? '🔐' : '🔑' }}</span></div>
              <div class="sub mono">{{ h.username }}@{{ h.host }}:{{ h.ssh_port }}</div>
            </div>
            <div v-if="!isViewer()" class="ops">
              <el-button text size="small" title="连通性探测" @click.stop="probeHost(h)">⚡</el-button>
              <el-button v-if="canManageHosts()" text size="small" title="编辑" @click.stop="editHost(h)">✎</el-button>
              <el-button v-if="canManageHosts()" text size="small" title="删除" @click.stop="delHost(h)">🗑</el-button>
            </div>
          </div>
          <template v-for="g in hostGroups()" :key="g.name">
            <div class="ms-group-head" @click="toggleGroup(g.name)">
              <i class="arrow" :class="{ open: !isGroupCollapsed(g.name) }">▸</i>
              <span class="gname">{{ g.name }}</span>
              <span class="cnt">{{ g.hosts.length }} 台</span>
            </div>
            <template v-if="!isGroupCollapsed(g.name)">
              <div v-for="h in g.hosts" :key="h.id" class="ms-host-item" :class="{ on: h.id === MS.activeHost }">
                <div class="main" @click="selectHost(h.id)">
                  <div class="t"><i class="dot" :class="probeCls(h.id)"></i>{{ h.name }}<span class="ms-auth-badge">{{ h.auth === 'key' ? '🔐' : '🔑' }}</span></div>
                  <div class="sub mono">{{ h.username }}@{{ h.host }}:{{ h.ssh_port }}</div>
                </div>
                <div v-if="!isViewer()" class="ops">
                  <el-button text size="small" title="连通性探测" @click.stop="probeHost(h)">⚡</el-button>
                  <el-button v-if="canManageHosts()" text size="small" title="编辑" @click.stop="editHost(h)">✎</el-button>
                  <el-button v-if="canManageHosts()" text size="small" title="删除" @click.stop="delHost(h)">🗑</el-button>
                </div>
              </div>
            </template>
          </template>
          <div v-if="!MS.hosts.length" class="ms-empty">暂无主机。点「＋ 添加」录入 SSH 主机,可设分组(如 7 台 GPU 节点一组),支持密码 / 私钥登录</div>
          <div v-else-if="!ungroupedHosts().length && !hostGroups().length" class="ms-empty">没有匹配「{{ MS.hostFilter }}」的主机</div>
        </div>
        <div v-if="probeInfo" class="ms-aside-foot" v-html="probeInfo"></div>
      </aside>

      <main class="ms-main">
        <div class="subnav">
          <button class="subnav-btn" :class="{ on: MS.view === 'containers' }" @click="MS.view = 'containers'">
            <el-icon><Monitor/></el-icon>容器总览</button>
          <button class="subnav-btn" :class="{ on: MS.view === 'presets' }" @click="MS.view = 'presets'">
            <el-icon><List/></el-icon>预设命令</button>
          <button class="subnav-btn" :class="{ on: MS.view === 'plans' }" @click="MS.view = 'plans'">
            <el-icon><Grid/></el-icon>任务编排</button>
          <span class="grow"></span>
          <span v-if="activeHostObj" class="subnav-info">当前主机:<b>{{ activeHostObj.name }}</b><span class="mono" style="margin-left:6px;">{{ activeHostObj.host }}</span></span>
        </div>
        <div class="ms-view">
          <ContainersView v-if="MS.view === 'containers'"/>
          <PresetsView v-else-if="MS.view === 'presets'"/>
          <PlansView v-else/>
        </div>
      </main>
    </div>

    <TerminalDock v-if="!isViewer()"/>

    <el-dialog v-model="hostDlg.visible" :title="hostDlg.form.id ? '编辑 SSH 主机' : '添加 SSH 主机'" width="580px">
      <el-form label-width="86px">
        <!-- 添加方式切换(仅新增;编辑时只有单台) -->
        <div v-if="!hostDlg.form.id" class="ix-seg ms-dlg-mode">
          <button type="button" :class="{ on: hostDlg.mode === 'single' }" @click="hostDlg.mode = 'single'">➕ 单台添加</button>
          <button type="button" :class="{ on: hostDlg.mode === 'batch' }" @click="hostDlg.mode = 'batch'">📋 批量粘贴</button>
        </div>

        <template v-if="hostDlg.mode === 'single'">
          <div class="ms-dlg-grid">
            <el-form-item label="名称"><el-input v-model="hostDlg.form.name" placeholder="留空则用主机地址"/></el-form-item>
            <el-form-item label="分组">
              <el-select v-model="hostDlg.form.group" filterable allow-create default-first-option clearable
                         placeholder="选择或输入新分组" style="width:100%;">
                <el-option v-for="g in groupNames()" :key="g" :value="g" :label="g"/>
              </el-select>
            </el-form-item>
          </div>
          <div class="ms-dlg-grid">
            <el-form-item label="主机地址"><el-input v-model="hostDlg.form.host" placeholder="IP 或域名,如 10.0.1.10"/></el-form-item>
            <el-form-item label="SSH 端口"><el-input v-model="hostDlg.form.ssh_port" placeholder="1-65535,默认 22"/></el-form-item>
          </div>
          <el-form-item label="用户名"><el-input v-model="hostDlg.form.username" placeholder="root"/></el-form-item>
        </template>
        <template v-else>
          <el-form-item label="主机清单">
            <el-input v-model="hostDlg.batch" type="textarea" :rows="7" class="mono" resize="vertical"
                      placeholder="每行一台,支持: 名称(可选) 用户@主机:端口 · 用户@主机 · 主机:端口 · 主机;# 开头为注释。&#10;示例:&#10;worker-01 user@10.0.1.10&#10;worker-02 user@10.0.1.11"/>
            <div class="ms-batch-hint">
              ✓ 识别到 <b>{{ batchParsed.list.length }}</b> 台主机<template v-if="batchParsed.bad.length"> · <span class="ms-batch-bad">✗ {{ batchParsed.bad.length }} 行无法解析已跳过</span></template>
              <template v-if="batchParsed.list.length"> · {{ batchParsed.list.map(it => it.host).slice(0, 6).join("、") }}{{ batchParsed.list.length > 6 ? "…" : "" }}</template>
              <br/>下方登录方式与分组对全部主机生效;未写用户名/端口的行将沿用「用户名」「SSH 端口」字段
            </div>
          </el-form-item>
          <div class="ms-dlg-grid">
            <el-form-item label="用户名"><el-input v-model="hostDlg.form.username" placeholder="行内未写用户名时使用,默认 root"/></el-form-item>
            <el-form-item label="SSH 端口"><el-input v-model="hostDlg.form.ssh_port" placeholder="行内未写端口时使用,默认 22"/></el-form-item>
          </div>
          <el-form-item label="分组">
            <el-select v-model="hostDlg.form.group" filterable allow-create default-first-option clearable
                       placeholder="选择或输入新分组(应用到全部)" style="width:100%;">
              <el-option v-for="g in groupNames()" :key="g" :value="g" :label="g"/>
            </el-select>
          </el-form-item>
        </template>

        <el-divider content-position="left">登录方式{{ hostDlg.mode === 'batch' ? '(对全部主机生效)' : '' }}</el-divider>
        <div class="ms-auth-row">
          <button type="button" class="ms-auth-card" :class="{ on: hostDlg.form.auth === 'password' }" @click="hostDlg.form.auth = 'password'">
            <div class="t">🔑 密码登录</div>
            <div class="d">最常用,直接输入账号密码</div>
          </button>
          <button type="button" class="ms-auth-card" :class="{ on: hostDlg.form.auth === 'key' }" @click="hostDlg.form.auth = 'key'">
            <div class="t">🔐 私钥登录</div>
            <div class="d">适用于禁用密码的主机(Ed25519/ECDSA/RSA)</div>
          </button>
        </div>
        <template v-if="hostDlg.form.auth === 'password'">
          <el-form-item label="密码" style="margin-top:12px;">
            <el-input v-model="hostDlg.form.password" type="password" show-password placeholder="编辑时留空表示不修改"/>
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item label="私钥" style="margin-top:12px;">
            <div class="ms-keyfile-row">
              <div>
                <input type="file" ref="keyFileRef" style="display:none;" @change="onKeyFile"/>
                <el-button size="small" @click="pickKeyFile()">📄 选择私钥文件(本地读取,不上传)</el-button>
                <span class="ix-note-inline">或直接在下方粘贴 PEM 内容</span>
              </div>
              <el-input v-model="hostDlg.form.private_key" type="textarea" :rows="5" class="mono" resize="vertical"
                        placeholder="-----BEGIN OPENSSH PRIVATE KEY----- …;编辑时留空表示沿用原密钥"/>
            </div>
          </el-form-item>
          <el-form-item label="私钥口令">
            <el-input v-model="hostDlg.form.passphrase" type="password" show-password placeholder="私钥无口令可留空"/>
          </el-form-item>
        </template>
        <!-- 管理员:把该主机分配给子账号(子账号仅可操作被分配的主机) -->
        <el-form-item v-if="isAdmin() && hostDlg.form.id && hostDlg.subs.length" label="分配子账号">
          <el-select v-model="hostDlg.assignedSubs" multiple filterable style="width:100%;"
                     placeholder="选择可使用该主机的子账号">
            <el-option v-for="u in hostDlg.subs" :key="u.username" :value="u.username" :label="u.username"/>
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="hostDlg.visible = false">取消</el-button>
        <el-button plain @click="saveHostDlg(true)">⚡ {{ hostDlg.mode === 'batch' ? '测试并保存全部' : '测试并保存' }}</el-button>
        <el-button type="primary" @click="saveHostDlg(false)">
          {{ hostDlg.mode === 'batch' ? ('保存 ' + batchParsed.list.length + ' 台') : '保存' }}
        </el-button>
      </template>
    </el-dialog>
  </div>`,
};

export default ModelStartView;
