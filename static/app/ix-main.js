// ============================== 压测工作台视图(Model Bench,SPA 子页) ==============================
import { ref, reactive, computed, watch, onMounted, onUnmounted } from "vue";
import { api } from "./api.js";
import { renderMarkdown } from "./markdown.js";
import { S, pollTasks, pollReports, loadConfig, openPreview, loadMainCfg, saveMainCfg } from "./ix-store.js";
import { isViewer } from "./auth.js";
import ApiCard from "./ix-apicard.js";
import StrategiesCard from "./ix-strategies.js";
import KvCard from "./ix-kvcache.js";
import { TaskListCard, TaskDetailCard } from "./ix-tasks.js";
import { ReportsCard, PreviewDialog } from "./ix-reports.js";

// ---------- 深空补给动画层(SVG 与旧版一致) ----------
const ShipFx = {
  setup() {
    const visible = computed(() => S.ship.shown || S.ship.departing);
    return { S, visible };
  },
  template: `
  <div v-if="visible" class="ship-fx" :class="{ arrive: S.ship.shown, depart: S.ship.departing }">
    <div class="sf-ship-wrap">
      <svg class="sf-ship-svg" viewBox="0 0 640 180" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="sfHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#aab4c8"/><stop offset="0.5" stop-color="#7c8699"/>
            <stop offset="1" stop-color="#4b5468"/>
          </linearGradient>
          <linearGradient id="sfHullTop" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#c6cfdf"/><stop offset="1" stop-color="#8b95a8"/>
          </linearGradient>
          <radialGradient id="sfEng" cx="0.5" cy="0.5" r="0.5">
            <stop offset="0" stop-color="#eaffff"/><stop offset="0.4" stop-color="#57c8ff"/>
            <stop offset="1" stop-color="#1e6fd9" stop-opacity="0"/>
          </radialGradient>
        </defs>
        <polygon points="6,104 140,74 430,60 548,66 548,128 420,138 140,128" fill="url(#sfHull)" stroke="#39415a" stroke-width="1.5"/>
        <polygon points="80,100 220,66 440,58 470,58 470,78 240,86 100,108" fill="url(#sfHullTop)" opacity="0.9"/>
        <polygon points="398,58 470,58 462,26 410,26" fill="url(#sfHullTop)" stroke="#39415a" stroke-width="1"/>
        <rect x="404" y="12" width="66" height="14" rx="2" fill="#5c667c"/>
        <circle class="sf-globe" cx="418" cy="9" r="5.5" fill="#9fb3d9"/>
        <circle class="sf-globe" cx="452" cy="9" r="5.5" fill="#9fb3d9"/>
        <line x1="150" y1="78" x2="150" y2="120" stroke="#39415a" stroke-width="1" opacity="0.7"/>
        <line x1="250" y1="68" x2="250" y2="128" stroke="#39415a" stroke-width="1" opacity="0.6"/>
        <line x1="340" y1="63" x2="340" y2="134" stroke="#39415a" stroke-width="1" opacity="0.6"/>
        <line x1="60" y1="98" x2="440" y2="72" stroke="#39415a" stroke-width="1" opacity="0.5"/>
        <rect x="520" y="66" width="26" height="62" rx="3" fill="#454e63"/>
        <circle class="sf-eng" cx="546" cy="76" r="10" fill="url(#sfEng)"/>
        <circle class="sf-eng" cx="546" cy="97" r="12" fill="url(#sfEng)"/>
        <circle class="sf-eng" cx="546" cy="118" r="10" fill="url(#sfEng)"/>
        <ellipse class="sf-trail" cx="600" cy="97" rx="55" ry="13" fill="url(#sfEng)" opacity="0.55"/>
      </svg>
    </div>
    <div class="sf-station-wrap">
      <svg class="sf-station-svg" viewBox="0 0 240 320" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="sfStn" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stop-color="#9aa4b8"/><stop offset="1" stop-color="#525b70"/>
          </linearGradient>
          <radialGradient id="sfTankG" cx="0.35" cy="0.35" r="0.75">
            <stop offset="0" stop-color="#b9ffe9"/><stop offset="0.45" stop-color="#37e2a5"/>
            <stop offset="1" stop-color="#0b6b4e"/>
          </radialGradient>
        </defs>
        <rect x="112" y="70" width="16" height="210" fill="url(#sfStn)" stroke="#39415a" stroke-width="1"/>
        <ellipse cx="120" cy="120" rx="88" ry="26" fill="none" stroke="url(#sfStn)" stroke-width="9"/>
        <ellipse cx="120" cy="120" rx="70" ry="18" fill="none" stroke="#39415a" stroke-width="2" opacity="0.8"/>
        <line x1="120" y1="70" x2="120" y2="24" stroke="#8b95a8" stroke-width="3"/>
        <circle class="sf-beacon" cx="120" cy="20" r="5" fill="#ff4d6d"/>
        <circle class="sf-tank" cx="70" cy="230" r="26" fill="url(#sfTankG)"/>
        <circle class="sf-tank" cx="170" cy="230" r="26" fill="url(#sfTankG)"/>
        <rect x="18" y="150" width="94" height="12" rx="3" fill="url(#sfStn)" stroke="#39415a" stroke-width="1"/>
        <circle class="sf-beacon sf-beacon2" cx="20" cy="156" r="6" fill="#57f7c8"/>
        <polygon points="104,280 136,280 120,312" fill="#454e63"/>
      </svg>
    </div>
    <div class="sf-tether"></div>
    <div class="sf-streaks"><i></i><i></i><i></i><i></i></div>
    <div class="sf-hud">
      <div class="sf-hud-title">{{ S.ship.title }}</div>
      <div class="sf-hud-sub">{{ S.ship.sub }}<span class="sf-pct">{{ S.ship.pct }}%</span></div>
    </div>
    <div class="sf-progress"><div class="sf-progress-fill" :style="{ width: S.ship.pct + '%' }"></div></div>
  </div>`,
};

// ---------- 帮助抽屉(全局标题栏「帮助」按钮控制) ----------
export const HelpDrawer = {
  setup() {
    const open = ref(false);
    const html = ref("");
    const loaded = ref(false);
    async function toggle() {
      open.value = !open.value;
      if (open.value && !loaded.value) {
        try {
          const d = await api("/api/readme");
          html.value = renderMarkdown(d.content || "无内容");
          loaded.value = true;
        } catch (e) { html.value = "加载失败: " + e.message; }
      }
    }
    return { open, html, toggle };
  },
  template: `
  <div class="ix-help-drawer" :class="{ open }">
    <div class="ix-chat-head">
      <span>📖 使用说明</span>
      <button class="ix-chat-mini-btn" @click="open = false">关闭</button>
    </div>
    <div class="ix-help-body"><div class="md-body" v-html="html"></div></div>
  </div>`,
};

// ---------- 游戏弹窗(全局标题栏「小游戏」按钮控制) ----------
export const GameModal = {
  setup() {
    const visible = ref(false);
    const loaded = ref(false);
    function onMsg(e) {
      if (e.data && e.data.type === "closeGame") visible.value = false;
    }
    onMounted(() => window.addEventListener("message", onMsg));
    onUnmounted(() => window.removeEventListener("message", onMsg));
    function open() {
      visible.value = true;
      loaded.value = true;
    }
    return { visible, loaded, open };
  },
  template: `
  <el-dialog v-model="visible" width="min(720px, 96vw)" top="2vh" title="🐍 贪吃蛇">
    <template #header>
      <span style="font-weight:700;">🐍 贪吃蛇</span>
      <span class="ix-note-inline" style="margin-left:8px;">压测等待时放松一下 · 4 档速度可调</span>
    </template>
    <div style="display:flex;justify-content:center;overflow:auto;">
      <iframe v-if="loaded" src="/game" style="width:100%;height:760px;border:none;border-radius:8px;background:#1a1a2e;"></iframe>
    </div>
  </el-dialog>`,
};

// ---------- 模型详情弹窗 ----------
const ModelInfoDialog = {
  setup() {
    const visible = computed({
      get: () => S.infoVisible,
      set: v => { S.infoVisible = v; },
    });
    const metaEntries = computed(() => Object.entries(S.serverMeta || {}));
    const modelKeys = computed(() => [...new Set((S.modelDetails || []).flatMap(m => Object.keys(m)))]);
    return { visible, metaEntries, modelKeys, S };
  },
  template: `
  <el-dialog v-model="visible" width="min(760px, 94vw)" title="模型详情">
    <template v-if="metaEntries.length">
      <div class="ix-section-title" style="margin-top:0;">服务元数据</div>
      <table class="ix-preview-table">
        <tbody>
          <tr v-for="([k, v]) in metaEntries" :key="k">
            <td style="color:var(--el-color-primary);font-weight:600;">{{ k }}</td><td>{{ v }}</td>
          </tr>
        </tbody>
      </table>
    </template>
    <template v-if="S.modelDetails.length">
      <div class="ix-section-title">模型列表 ({{ S.modelDetails.length }})</div>
      <table class="ix-preview-table">
        <thead><tr><th v-for="k in modelKeys" :key="k">{{ k }}</th></tr></thead>
        <tbody>
          <tr v-for="(m, i) in S.modelDetails" :key="i">
            <td v-for="k in modelKeys" :key="k" :title="m[k]">{{ m[k] }}</td>
          </tr>
        </tbody>
      </table>
    </template>
    <div v-if="!metaEntries.length && !S.modelDetails.length" class="ix-note">无详情数据</div>
  </el-dialog>`,
};

// ---------- 压测工作台视图 ----------
const BenchView = {
  components: { ApiCard, StrategiesCard, KvCard, TaskListCard, ReportsCard, TaskDetailCard,
                ShipFx, PreviewDialog, ModelInfoDialog },
  setup() {
    // 字号(全局外壳应用缩放,这里只读取用于拖拽增量换算)
    const font = ref(localStorage.getItem("bench-font") || "1");

    // ---------- 面板系统:页面所有板块可拖拽互换位置(可跨栏) ----------
    // 板块按内容自然展开、页面整体滚动,不再限制高度;
    // 仅保留拖拽换位(⠿ 手柄)与左栏宽度拖拽
    const PANEL_COMPS = {
      api: ApiCard, strategies: StrategiesCard, kv: KvCard,
      tasks: TaskListCard, reports: ReportsCard, detail: TaskDetailCard,
    };
    const LAYOUT_KEY = "ix-layout-v4";
    function loadLayout() {
      const def = { left: ["api", "strategies", "kv"], right: ["tasks", "reports", "detail"] };
      try {
        const c = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "null");
        if (!c || !Array.isArray(c.left) || !Array.isArray(c.right)) return def;
        const known = Object.keys(PANEL_COMPS);
        const left = c.left.filter(id => known.includes(id));
        const right = c.right.filter(id => known.includes(id) && !left.includes(id));
        for (const id of known) if (!left.includes(id) && !right.includes(id)) right.push(id);
        return { left, right };
      } catch { return def; }
    }
    const layout = reactive(loadLayout());
    // 预览账号:只保留查看类板块(任务列表 / 报告 / 任务详情),配置与压测创建板块不渲染
    const RO = isViewer();
    const VIEWER_PANELS = ["tasks", "reports", "detail"];
    const leftPanels = computed(() => RO ? layout.left.filter(id => VIEWER_PANELS.includes(id)) : layout.left);
    const rightPanels = computed(() => RO ? layout.right.filter(id => VIEWER_PANELS.includes(id)) : layout.right);
    function saveLayout() {
      localStorage.setItem(LAYOUT_KEY, JSON.stringify({ left: layout.left, right: layout.right }));
    }
    const dragId = ref(null);
    const dropHint = ref("");   // "left:2" 表示插入左栏下标 2 处
    function onDragStart(id, e) {
      dragId.value = id;
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        try { e.dataTransfer.setData("text/plain", id); } catch {}
      }
    }
    function onDragEnd() { dragId.value = null; dropHint.value = ""; }
    function panelOver(col, idx, e) {
      if (!dragId.value) return;
      e.preventDefault(); e.stopPropagation();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      const r = e.currentTarget.getBoundingClientRect();
      const at = (e.clientY - r.top) < r.height / 2 ? idx : idx + 1;
      if (dropHint.value !== col + ":" + at) dropHint.value = col + ":" + at;
    }
    function applyDrop(col, at) {
      const id = dragId.value;
      if (!id) return;
      const target = col === "left" ? layout.left : layout.right;
      const src = layout.left.includes(id) ? layout.left : layout.right;
      const si = src.indexOf(id);
      if (src === target && si > -1 && si < at) at -= 1;
      src.splice(si, 1);
      target.splice(Math.max(0, Math.min(at, target.length)), 0, id);
      dragId.value = null; dropHint.value = "";
      saveLayout();
    }
    function panelDrop(col, idx, e) {
      e.preventDefault(); e.stopPropagation();
      const r = e.currentTarget.getBoundingClientRect();
      applyDrop(col, (e.clientY - r.top) < r.height / 2 ? idx : idx + 1);
    }
    function colOver(col, e) {
      if (!dragId.value || e.target !== e.currentTarget) return;
      e.preventDefault();
      const at = col === "left" ? layout.left.length : layout.right.length;
      if (dropHint.value !== col + ":" + at) dropHint.value = col + ":" + at;
    }
    function colDrop(col, e) {
      e.preventDefault();
      applyDrop(col, col === "left" ? layout.left.length : layout.right.length);
    }

    // ---------- 左栏宽度可拖拽调整(localStorage 记忆) ----------
    const leftW = ref(+(localStorage.getItem("ix-left-w") || 500));
    function _drag(onMove, onDone) {
      const move = (ev) => onMove(ev);
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        onDone && onDone();
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
      document.body.style.userSelect = "none";
    }
    function startColDrag(e) {
      e.preventDefault();
      const x0 = e.clientX, w0 = leftW.value, zoom = +font.value || 1;
      document.body.style.cursor = "col-resize";
      _drag((ev) => {
        leftW.value = Math.max(380, Math.min(w0 + (ev.clientX - x0) / zoom, 900));
      }, () => localStorage.setItem("ix-left-w", leftW.value));
    }

    let taskTimer = null, reportTimer = null, cfgTimer = null;
    loadMainCfg();
    // 配置变化防抖持久化(刷新后自动回填)
    watch(() => [S.apiUrl, S.apiKey, S.protocol, S.taskType, S.model, S.models.length, S.framework, S.maxIn, S.maxOut], () => {
      clearTimeout(cfgTimer);
      cfgTimer = setTimeout(saveMainCfg, 600);
    });
    onMounted(() => {
      pollTasks(); pollReports(); loadConfig();
      taskTimer = setInterval(pollTasks, 2000);
      reportTimer = setInterval(pollReports, 5000);
    });
    onUnmounted(() => { clearInterval(taskTimer); clearInterval(reportTimer); });

    function onPreviewReport(filename) {
      openPreview(filename);
    }

    return {
      S, font, RO,
      leftW, startColDrag,
      PANEL_COMPS, layout, leftPanels, rightPanels, dragId, dropHint,
      onDragStart, onDragEnd, panelOver, panelDrop, colOver, colDrop,
      onPreviewReport,
    };
  },
  template: `
  <div class="ix-shell">
    <ShipFx/>
    <div v-if="RO" class="ix-ro-banner">👁 预览模式:压测工作台仅可查看任务与报告,不可配置 API / 创建任务</div>

    <main class="ix-main">
      <div class="ix-col ix-left-col" :style="{ width: leftW + 'px' }"
           @dragover="colOver('left', $event)" @drop="colDrop('left', $event)">
        <template v-for="(id, i) in leftPanels" :key="id">
          <div v-if="dropHint === 'left:' + i" class="ix-drop-mark"></div>
          <div class="ix-panel" :class="{ ghost: dragId === id }"
               @dragover="panelOver('left', i, $event)" @drop="panelDrop('left', i, $event)">
            <component :is="PANEL_COMPS[id]" @preview="onPreviewReport"/>
            <span class="ix-panel-grip" draggable="true" title="按住拖拽:移动 / 互换板块位置(可跨左右栏)"
                  @dragstart="onDragStart(id, $event)" @dragend="onDragEnd">⠿</span>
          </div>
        </template>
        <div v-if="dropHint === 'left:' + leftPanels.length" class="ix-drop-mark"></div>
      </div>
      <div class="ix-col-drag" title="左右拖动调整左栏宽度" @mousedown="startColDrag"></div>
      <section class="ix-col ix-right-col"
               @dragover="colOver('right', $event)" @drop="colDrop('right', $event)">
        <template v-for="(id, i) in rightPanels" :key="id">
          <div v-if="dropHint === 'right:' + i" class="ix-drop-mark"></div>
          <div class="ix-panel" :class="{ ghost: dragId === id }"
               @dragover="panelOver('right', i, $event)" @drop="panelDrop('right', i, $event)">
            <component :is="PANEL_COMPS[id]" @preview="onPreviewReport"/>
            <span class="ix-panel-grip" draggable="true" title="按住拖拽:移动 / 互换板块位置(可跨左右栏)"
                  @dragstart="onDragStart(id, $event)" @dragend="onDragEnd">⠿</span>
          </div>
        </template>
        <div v-if="dropHint === 'right:' + rightPanels.length" class="ix-drop-mark"></div>
      </section>
    </main>

    <PreviewDialog/>
    <ModelInfoDialog/>
  </div>`,
};

export default BenchView;
