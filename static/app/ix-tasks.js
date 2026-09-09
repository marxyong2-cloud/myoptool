// ============================== 右栏:任务列表 + 任务详情(ECharts 指标看板) ==============================
import { computed, reactive, ref, watch, nextTick, onMounted, onUnmounted } from "vue";
import { ElMessageBox } from "element-plus";
import * as echarts from "echarts";
import { renderMarkdown } from "./markdown.js";
import {
  S, fmt, selectTask, taskAction, renameTask, deleteTask, saveRemaining,
  STATUS_LABEL, STATUS_TYPE, downloadReport, openPreview,
} from "./ix-store.js";

// ---------- ECharts 图表实例封装(响应依赖变化自动重绘,高频事件下 150ms 合并) ----------
function useChart(elRef, optionFn, depsWatch) {
  let inst = null;
  let timer = null;
  const draw = () => {
    if (timer) return;                     // 已有待执行的绘制,合并本次变更
    timer = setTimeout(() => {
      timer = null;
      nextTick(() => {
        if (!elRef.value) { if (inst) { inst.dispose(); inst = null; } return; }
        if (!inst) inst = echarts.init(elRef.value);
        inst.setOption(optionFn(), true);
        inst.resize();
      });
    }, 150);
  };
  watch(depsWatch, draw, { deep: true });
  onMounted(draw);
  onUnmounted(() => {
    clearTimeout(timer);
    if (inst) { inst.dispose(); inst = null; }
  });
  window.addEventListener("resize", draw);
  onUnmounted(() => window.removeEventListener("resize", draw));
  return { redraw: draw };
}

// ---------- 任务列表卡 ----------
export const TaskListCard = {
  setup() {
    const items = computed(() =>
      S.order.filter(id => S.tasks[id]).map(id => S.tasks[id]).reverse());
    const pct = t => t.total_requests ? Math.round(t.done_requests / t.total_requests * 100) : 0;

    async function onRename(ui) {
      try {
        const { value } = await ElMessageBox.prompt("任务名称:", "重命名任务", { inputValue: ui.info.name || ui.info.model });
        if (!value || !value.trim()) return;
        await renameTask(ui.info.id, value.trim());
      } catch {}
    }
    async function onDelete(ui) {
      try {
        await ElMessageBox.confirm(`确认删除任务「${ui.info.name || ui.info.model}」?进行中的会被停止。`, "删除任务", { type: "warning" });
      } catch { return; }
      await deleteTask(ui.info.id);
    }
    return { S, items, pct, selectTask, onRename, onDelete, STATUS_LABEL, STATUS_TYPE };
  },
  template: `
  <el-card shadow="never" class="ix-card">
    <template #header><span class="ix-card-title">任务列表 (可多 API 并行)</span></template>
    <div v-if="!items.length" class="ix-note">暂无任务,左侧配置后点击「创建压测任务」。</div>
    <div v-for="ui in items" :key="ui.info.id" class="ix-task-item"
      :class="{ active: S.selected === ui.info.id }" @click="selectTask(ui.info.id)">
      <div class="t-head">
        <span class="t-name">{{ ui.info.name || ui.info.model }}</span>
        <span class="t-ops">
          <el-tag size="small" :type="STATUS_TYPE[ui.info.status] || 'info'">{{ STATUS_LABEL[ui.info.status] || ui.info.status }}</el-tag>
          <el-button text size="small" title="重命名" @click.stop="onRename(ui)">✏️</el-button>
          <el-button text size="small" type="danger" title="删除任务" @click.stop="onDelete(ui)">×</el-button>
        </span>
      </div>
      <div class="t-sub">
        <span>{{ ui.info.created_at }} · {{ ui.info.strategy_count }} 策略 · {{ ui.info.model }}</span>
        <span>{{ ui.info.done_requests }}/{{ ui.info.total_requests }} ({{ pct(ui.info) }}%)</span>
      </div>
    </div>
  </el-card>`,
};

// ---------- 任务详情卡 ----------
export const TaskDetailCard = {
  setup() {
    const ui = computed(() => (S.selected && S.tasks[S.selected]) || null);
    const t = computed(() => (ui.value ? ui.value.info : null));

    const doneTotal = computed(() => {
      const u = ui.value;
      if (!u) return 0;
      return u.summaries.reduce((a, s) => a + (s ? s.summary.success_count + s.summary.failed_count : 0), 0)
        + (u.stratStates[u.info.current_index] === "running" ? u.curDone : 0);
    });
    const liveRps = computed(() => {
      const u = ui.value;
      if (!u) return "-";
      const now = Date.now();
      const recent = u.live.filter(x => now - x.t < 10000);
      return recent.length ? (recent.length / 10).toFixed(1) : "-";
    });
    const liveTps = computed(() => {
      const u = ui.value;
      if (!u) return "-";
      const arr = u.live.map(x => x.tps).filter(x => x > 0);
      return arr.length ? (arr.reduce((a, b) => a + b, 0) / arr.length).toFixed(1) : "-";
    });
    const totalReq = computed(() => {
      const u = ui.value;
      if (!u) return 0;
      return u.ok + u.fail;
    });
    const liveRate = computed(() => totalReq.value ? (ui.value.ok / totalReq.value * 100).toFixed(1) + "%" : "-");
    const liveErr = computed(() => totalReq.value ? (ui.value.fail / totalReq.value * 100).toFixed(1) + "%" : "-");

    // 策略时间线状态推导
    function stlState(i) {
      const u = ui.value;
      if (!u) return "";
      let st = u.stratStates[i] || (i < u.info.current_index ? "done" : "");
      if (!st && u.summaries[i]) st = u.summaries[i].summary.failed_count > 0 ? "has-fail" : "done";
      return st;
    }
    function stlRight(i) {
      const u = ui.value;
      const s = (u.info.strategies || [])[i];
      const sm = u.summaries[i];
      if (sm) {
        const sum = sm.summary;
        const tot = (sum.success_count || 0) + (sum.failed_count || 0);
        const rate = tot ? Math.round((sum.success_count || 0) / tot * 100) : 0;
        return stlState(i) === "has-fail"
          ? `${rate}% · 失败 ${sum.failed_count}`
          : `${rate}% · 延迟P50 ${fmt((sum.latency_s || {}).p50, "s")}`;
      }
      return `并发${s.concurrency} · ${s.total_requests}请求`;
    }
    // 时间线随策略数完全展开(整页滚动),无需卡内自动滚动

    // 日志自动滚动
    const logEl = ref(null);
    watch(() => ui.value && ui.value.logs.length, () => {
      nextTick(() => { if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight; });
    });

    // 最近完成的策略 → 指标卡
    const lastSummary = computed(() => {
      const u = ui.value;
      if (!u) return null;
      return [...u.summaries].reverse().find(x => x) || null;
    });
    const metricCards = computed(() => {
      const it = lastSummary.value;
      if (!it) return [];
      const s = it.summary;
      const ttft = s.ttft_ms || {}, lat = s.latency_s || {}, itl = s.itl_ms || {};
      const tps = s.throughput_per_request_tps || {}, pre = s.prefill_tps || {}, dec = s.decode_tps || {};
      const cards = [
        { label: "成功率", value: `${((1 - s.error_rate) * 100).toFixed(1)}%`,
          sub: `${s.success_count} 成功 / ${s.failed_count} 失败` + (s.warmup_count ? ` · 预热丢弃 ${s.warmup_count}` : "") },
        { label: "TTFT P50", value: fmt(ttft.p50, " ms"), sub: `avg ${fmt(ttft.avg, " ms")} · P95 ${fmt(ttft.p95, " ms")}` },
        { label: "TTFT P99", value: fmt(ttft.p99, " ms"), sub: `max ${fmt(ttft.max, " ms")}` },
        { label: "总延迟 P50", value: fmt(lat.p50, " s"), sub: `avg ${fmt(lat.avg, " s")}` },
        { label: "总延迟 P99", value: fmt(lat.p99, " s"), sub: `max ${fmt(lat.max, " s")}` },
        { label: "TPOT(ITL) P50", value: fmt(itl.p50, " ms"), sub: `avg ${fmt(itl.avg, " ms")} · P90 ${fmt(itl.p90, " ms")} · P99 ${fmt(itl.p99, " ms")}` },
        { label: "输入 TPS (prefill)", value: fmt(pre.avg), sub: `P50 ${fmt(pre.p50)}` },
        { label: "解码 TPS (decode)", value: fmt(dec.avg), sub: `P50 ${fmt(dec.p50)}` },
        { label: "单请求 TPS", value: fmt(tps.avg), sub: `P50 ${fmt(tps.p50)} · P95 ${fmt(tps.p95)}` },
        { label: "聚合吞吐", value: `${s.aggregate_throughput_tps}`, sub: `tok/s · RPS ${s.rps} · 总输出 ${s.total_output_tokens} tok` },
      ];
      const slo = s.slo;
      if (slo && slo.ok_count != null) {
        cards.splice(1, 0, {
          label: "Goodput (满足SLO)",
          value: `${slo.goodput_rps} req/s`,
          sub: `达标率 ${(slo.rate * 100).toFixed(1)}% · ` +
            [slo.ttft_ms ? `TTFT≤${slo.ttft_ms}ms` : "", slo.tpot_ms ? `TPOT≤${slo.tpot_ms}ms` : ""].filter(Boolean).join(" 且 "),
        });
      }
      return cards;
    });

    // ---------- ECharts 看板 ----------
    const chartQ = ref(null), chartTps = ref(null), chartScale = ref(null), chartLive = ref(null);

    // 图 1:最近策略分位条形(TTFT/延迟 P50-P99,统一 ms)
    useChart(chartQ, () => {
      const it = lastSummary.value;
      const s = it ? it.summary : {};
      const ttft = s.ttft_ms || {}, lat = s.latency_s || {};
      const cats = ["P50", "P95", "P99"];
      return {
        backgroundColor: "transparent",
        grid: { left: 48, right: 12, top: 28, bottom: 24 },
        tooltip: { trigger: "axis", valueFormatter: v => v + " ms" },
        legend: { data: ["TTFT", "总延迟"], top: 0, textStyle: { fontSize: 11, color: "#909399" } },
        xAxis: { type: "category", data: cats, axisLabel: { fontSize: 10, color: "#909399" } },
        yAxis: { type: "value", name: "ms", axisLabel: { fontSize: 10, color: "#909399" }, splitLine: { lineStyle: { color: "rgba(128,128,128,.15)" } } },
        series: [
          { name: "TTFT", type: "bar", data: cats.map(k => ttft[k.toLowerCase()] ?? null), itemStyle: { color: "#38bdf8", borderRadius: [3, 3, 0, 0] }, barMaxWidth: 28 },
          { name: "总延迟", type: "bar", data: cats.map(k => lat[k.toLowerCase()] != null ? +(lat[k.toLowerCase()] * 1000).toFixed(1) : null), itemStyle: { color: "#f59e0b", borderRadius: [3, 3, 0, 0] }, barMaxWidth: 28 },
        ],
      };
    }, () => lastSummary.value);

    // 图 2:TPS 对比(prefill/decode/单请求/聚合)
    useChart(chartTps, () => {
      const it = lastSummary.value;
      const s = it ? it.summary : {};
      const items = [
        ["输入prefill", (s.prefill_tps || {}).avg],
        ["解码decode", (s.decode_tps || {}).avg],
        ["单请求", (s.throughput_per_request_tps || {}).avg],
        ["聚合", s.aggregate_throughput_tps],
      ];
      return {
        backgroundColor: "transparent",
        grid: { left: 48, right: 12, top: 28, bottom: 24 },
        tooltip: { trigger: "axis", valueFormatter: v => v + " tok/s" },
        legend: { show: false },
        xAxis: { type: "category", data: items.map(x => x[0]), axisLabel: { fontSize: 10, color: "#909399" } },
        yAxis: { type: "value", name: "tok/s", axisLabel: { fontSize: 10, color: "#909399" }, splitLine: { lineStyle: { color: "rgba(128,128,128,.15)" } } },
        series: [{
          type: "bar", data: items.map(x => x[1]), barMaxWidth: 34,
          itemStyle: { color: p => ["#60a5fa", "#10b981", "#38bdf8", "#f59e0b"][p.dataIndex], borderRadius: [3, 3, 0, 0] },
        }],
      };
    }, () => lastSummary.value);

    // 图 3:策略扩展性(并发 → 聚合TPS 柱 + TTFT P50 折线)
    useChart(chartScale, () => {
      const u = ui.value;
      const rows = (u ? u.summaries : []).map((x, i) => x ? { i, ...x } : null).filter(Boolean);
      const names = rows.map(r => `${(r.config || {}).concurrency ?? "-"}`);
      return {
        backgroundColor: "transparent",
        grid: { left: 48, right: 44, top: 28, bottom: 24 },
        tooltip: { trigger: "axis" },
        legend: { data: ["聚合TPS", "TTFT P50", ...(rows.some(r => r.summary.slo) ? ["Goodput"] : [])],
                   top: 0, textStyle: { fontSize: 11, color: "#909399" } },
        xAxis: { type: "category", data: names, name: "并发", nameLocation: "end", axisLabel: { fontSize: 10, color: "#909399" } },
        yAxis: [
          { type: "value", name: "tok/s", axisLabel: { fontSize: 10, color: "#909399" }, splitLine: { lineStyle: { color: "rgba(128,128,128,.15)" } } },
          { type: "value", name: "ms", axisLabel: { fontSize: 10, color: "#909399" }, splitLine: { show: false } },
        ],
        series: [
          { name: "聚合TPS", type: "bar", data: rows.map(r => r.summary.aggregate_throughput_tps), itemStyle: { color: "#38bdf8", borderRadius: [3, 3, 0, 0] }, barMaxWidth: 24 },
          { name: "TTFT P50", type: "line", yAxisIndex: 1, data: rows.map(r => ((r.summary.ttft_ms || {}).p50 ?? null)), itemStyle: { color: "#f59e0b" }, symbolSize: 5 },
          ...(rows.some(r => r.summary.slo) ? [{
            name: "Goodput", type: "line",
            data: rows.map(r => r.summary.slo ? r.summary.slo.goodput_rps : null),
            itemStyle: { color: "#a78bfa" }, symbolSize: 5,
          }] : []),
        ],
      };
    }, () => ui.value && ui.value.summaries);

    // 图 4:实时输出吞吐曲线(滚动窗口)
    useChart(chartLive, () => {
      const u = ui.value;
      const live = u ? u.live : [];
      return {
        backgroundColor: "transparent",
        grid: { left: 44, right: 12, top: 24, bottom: 24 },
        tooltip: { trigger: "axis", valueFormatter: v => v + " tok/s" },
        legend: { show: false },
        xAxis: { type: "category", data: live.map((_, i) => i + 1), axisLabel: { fontSize: 10, color: "#909399" } },
        yAxis: { type: "value", name: "tok/s", axisLabel: { fontSize: 10, color: "#909399" }, splitLine: { lineStyle: { color: "rgba(128,128,128,.15)" } } },
        series: [{
          type: "line", data: live.map(x => x.tps), showSymbol: false,
          areaStyle: { opacity: 0.15 }, lineStyle: { width: 1.6, color: "#10b981" }, itemStyle: { color: "#10b981" },
        }],
      };
    }, () => ui.value && ui.value.live);

    // ---------- 暂停编辑剩余策略 ----------
    const pauseRows = reactive([]);
    watch(() => t.value && t.value.status, (st) => {
      pauseRows.splice(0);
      if (st === "paused" && t.value) {
        (t.value.strategies || []).slice(t.value.current_index + 1).forEach(s => {
          pauseRows.push({ ...s });
        });
      }
    }, { immediate: true });

    async function onSaveRemaining() {
      if (!S.selected) return;
      const list = pauseRows.map(s => ({
        name: (s.name || "").trim() || "未命名策略",
        concurrency: Math.min(256, Math.max(1, parseInt(s.concurrency, 10) || 4)),
        total_requests: Math.min(2000, Math.max(1, parseInt(s.total_requests, 10) || 16)),
        input_tokens: parseInt(s.input_tokens, 10) || 256,
        max_output_tokens: parseInt(s.max_output_tokens, 10) || 256,
        lang: s.lang,
        cache_mode: ["auto", "cold", "hot"].includes(s.cache_mode) ? s.cache_mode : "auto",
        warmup_requests: Math.max(0, Math.min(10, parseInt(s.warmup_requests, 10) || 0)),
      }));
      await saveRemaining(S.selected, list);
    }

    // ---------- 控制 ----------
    async function onStart() { if (S.selected) await taskAction(S.selected, "start"); }
    async function onPause() { if (S.selected) await taskAction(S.selected, "pause"); }
    async function onResume() { if (S.selected) await taskAction(S.selected, "resume"); }
    async function onCancel() {
      if (!S.selected) return;
      try { await ElMessageBox.confirm("确认停止该任务?已完成部分会正常出报告。", "停止任务", { type: "warning" }); }
      catch { return; }
      await taskAction(S.selected, "cancel");
    }

    function failShort(r) { return String(r).split(" — ")[0]; }
    function titleOf(u) {
      const info = u.info;
      return info.name || `${info.model} @ ${String(info.api_url).replace(/^https?:\/\//, "")}`;
    }

    return {
      S, ui, t, doneTotal, liveRps, liveTps, liveRate, liveErr,
      stlState, stlRight, logEl,
      lastSummary, metricCards, chartQ, chartTps, chartScale, chartLive,
      pauseRows, onSaveRemaining, onStart, onPause, onResume, onCancel,
      fmt, failShort, titleOf, STATUS_LABEL, STATUS_TYPE, downloadReport, openPreview, renderMarkdown,
    };
  },
  template: `
  <el-card shadow="never" class="ix-card" id="detailCard">
    <div v-if="!ui" class="ix-statusbar">选择或创建一个任务查看详情。</div>
    <div v-else>
      <div class="ix-detail-head">
        <div>
          <strong style="font-size:14px;">{{ titleOf(ui) }}</strong>
          <el-tag size="small" :type="STATUS_TYPE[t.status] || 'info'" style="margin-left:8px;">{{ STATUS_LABEL[t.status] || t.status }}</el-tag>
        </div>
        <div>
          <el-button v-if="t.status === 'pending'" type="primary" size="small" @click="onStart">▶ 启动压测</el-button>
          <el-button v-if="t.status === 'running'" type="warning" size="small" @click="onPause">暂停</el-button>
          <el-button v-if="t.status === 'paused'" type="primary" size="small" @click="onResume">继续</el-button>
          <el-button v-if="['running','paused','pending'].includes(t.status)" type="danger" size="small" @click="onCancel">停止</el-button>
        </div>
      </div>

      <div v-if="t.error" class="ix-statusbar err">{{ t.error }}</div>

      <div class="ix-progress-block">
        <div class="ix-progress-info">
          <span>总进度: {{ doneTotal }} / {{ t.total_requests }} 请求 · 策略 {{ ui.summaries.filter(x => x).length }}/{{ t.strategy_count }}</span>
          <span>
            <el-tag size="small" type="success">成功 {{ ui.ok }}</el-tag>
            <el-tag size="small" type="danger">失败 {{ ui.fail }}</el-tag>
          </span>
        </div>
        <el-progress :percentage="t.total_requests ? Math.min(100, Math.round(doneTotal / t.total_requests * 100)) : 0"
          :stroke-width="10" :show-text="false"/>
      </div>

      <div class="ix-live-chips">
        <div class="chip">实时速度: <b>{{ liveRps }}</b> req/s</div>
        <div class="chip">实时输出: <b>{{ liveTps }}</b> tok/s</div>
        <div class="chip ok">成功率: <b>{{ liveRate }}</b></div>
        <div class="chip err">错误率: <b>{{ liveErr }}</b></div>
      </div>

      <!-- 策略执行状态时间线 -->
      <div class="ix-stl">
        <div v-for="(s, i) in (t.strategies || [])" :key="i" class="stl-row" :class="stlState(i)"
          :title="s.name + ' · 并发' + s.concurrency + ' · ' + s.total_requests + '请求 · 输出' + s.max_output_tokens + 'tok · ' + (s.lang === 'zh' ? '中文' : 'EN') + ' · ' + ({ auto: '默认', cold: '冷缓存', hot: '热缓存' }[s.cache_mode || 'auto'] || '默认') + (s.warmup_requests ? ' · 预热' + s.warmup_requests : '')">
          <template v-if="stlState(i) === 'running'">
            <div class="stl-head"><span class="st">▶</span><span class="nm">{{ i + 1 }}. {{ s.name }}</span>
              <span class="rt">{{ ui.curDone }}/{{ ui.curTotal }} 请求 · {{ ui.curTotal ? Math.min(100, Math.round(ui.curDone / ui.curTotal * 100)) : 0 }}%</span></div>
            <el-progress :percentage="ui.curTotal ? Math.min(100, Math.round(ui.curDone / ui.curTotal * 100)) : 0"
              :stroke-width="6" :show-text="false" style="margin:4px 0;"/>
            <div class="stl-sub">
              <span>成功 <b>{{ ui.ok }}</b></span><span>失败 <b>{{ ui.fail }}</b></span>
              <span>并发 <b>{{ s.concurrency }}</b></span>
              <span>输出上限 <b>{{ s.max_output_tokens }}</b> tok</span>
              <span>{{ s.lang === "zh" ? "中文" : "英文" }}</span>
            </div>
          </template>
          <template v-else>
            <span class="st">{{ stlState(i) === 'done' ? '✓' : stlState(i) === 'has-fail' ? '⚠' : '○' }}</span>
            <span class="nm">{{ i + 1 }}. {{ s.name }}</span>
            <span class="rt">{{ stlRight(i) }}</span>
          </template>
        </div>
      </div>

      <!-- 暂停编辑剩余策略 -->
      <div v-if="t.status === 'paused'" class="ix-pause-edit">
        <div class="ix-section-title" style="color:var(--el-color-warning);">已暂停 — 可编辑剩余策略,保存后点「继续」</div>
        <div class="ix-st-scroll">
          <table class="ix-st-table compact" v-if="pauseRows.length">
            <thead><tr><th>策略</th><th class="num">并发</th><th class="num">请求数</th>
              <th class="num">输入tok</th><th class="num">输出tok</th><th>语言</th><th>缓存</th><th class="num">预热</th></tr></thead>
            <tbody>
              <tr v-for="s in pauseRows" :key="s.name + s.concurrency">
                <td><input type="text" class="st-name" v-model="s.name"/></td>
                <td><input type="number" class="st-num" v-model.number="s.concurrency" min="1" max="256" step="4"/></td>
                <td><input type="number" class="st-num" v-model.number="s.total_requests" min="1" max="2000" step="4"/></td>
                <td><input type="number" class="st-num" v-model.number="s.input_tokens" min="10"/></td>
                <td><input type="number" class="st-num" v-model.number="s.max_output_tokens" min="1"/></td>
                <td><select class="st-lang" v-model="s.lang"><option value="zh">中</option><option value="en">EN</option></select></td>
                <td><select class="st-lang" v-model="s.cache_mode" title="缓存模式:默认/冷(唯一前缀)/热(固定同文)"><option value="auto">默认</option><option value="cold">冷</option><option value="hot">热</option></select></td>
                <td><input type="number" class="st-num" v-model.number="s.warmup_requests" min="0" max="10"/></td>
              </tr>
            </tbody>
          </table>
          <div v-else class="ix-note">没有剩余策略</div>
        </div>
        <el-button type="primary" size="small" style="margin-top:8px;" @click="onSaveRemaining">保存剩余策略</el-button>
      </div>

      <!-- ECharts 指标看板 -->
      <template v-if="lastSummary">
        <div class="ix-section-title">当前/最近策略关键指标 — {{ lastSummary.name }}</div>
        <div class="ix-metrics-grid">
          <div v-for="m in metricCards" :key="m.label" class="ix-metric">
            <div class="label">{{ m.label }}</div>
            <div class="value">{{ m.value }}</div>
            <div class="sub">{{ m.sub }}</div>
          </div>
        </div>
        <div class="ix-chart-grid">
          <div class="ix-chart-box"><div class="ix-chart-title">延迟分位 (最近策略)</div><div ref="chartQ" class="ix-chart"></div></div>
          <div class="ix-chart-box"><div class="ix-chart-title">TPS 对比 (最近策略)</div><div ref="chartTps" class="ix-chart"></div></div>
          <div class="ix-chart-box"><div class="ix-chart-title">并发扩展性 (已完成策略)</div><div ref="chartScale" class="ix-chart"></div></div>
          <div class="ix-chart-box"><div class="ix-chart-title">实时输出吞吐 (滚动窗口)</div><div ref="chartLive" class="ix-chart"></div></div>
        </div>
      </template>

      <!-- 分析报告 -->
      <template v-if="ui.analysis && ui.analysis.length">
        <div class="ix-section-title">压测结果分析报告 (已写入 Excel「分析报告」页)</div>
        <div class="ix-analysis-box">
          <div v-for="([k, v], i) in ui.analysis" :key="i" class="ix-analysis-row">
            <span class="k">{{ k }}</span><span class="v">{{ v }}</span>
          </div>
        </div>
      </template>

      <!-- AI 深度分析 -->
      <template v-if="ui.aiAnalysis">
        <div class="ix-section-title" style="color:var(--el-color-primary);">🤖 AI 深度分析报告 (由被测模型生成,已写入 Excel)</div>
        <div class="ix-ai-box"><div class="md-body" v-html="renderMarkdown(ui.aiAnalysis)"></div></div>
      </template>

      <!-- 下载 -->
      <div v-if="ui.excel" class="ix-download-banner">
        <div>
          <strong>{{ t.status === 'cancelled' ? '任务已停止(部分结果)' : '压测完成!' }}</strong>
          <div class="fname">{{ ui.excel }}</div>
        </div>
        <div>
          <el-button @click="openPreview(ui.excel)">预览报告</el-button>
          <el-button type="primary" @click="downloadReport(ui.excel)">下载 Excel 报告</el-button>
        </div>
      </div>

      <!-- 汇总表 -->
      <div class="ix-section-title">压测结果汇总 (与 Excel 汇总表一致)</div>
      <div class="ix-table-wrap">
        <table class="ix-result-table">
          <thead>
            <tr><th>策略</th><th>并发</th><th>成功率</th><th>TTFT P50</th><th>TTFT P99</th><th>TPOT P50</th>
              <th>延迟 P50</th><th>输入TPS</th><th>解码TPS</th><th>TPS avg</th><th>聚合TPS</th><th>Goodput</th><th>SLO达标</th><th>失败原因</th></tr>
          </thead>
          <tbody>
            <tr v-if="!ui.summaries.some(x => x)"><td colspan="14" class="muted" style="text-align:center;padding:20px;">暂无数据</td></tr>
            <tr v-for="(item, i) in ui.summaries" :key="i">
              <template v-if="item">
                <td>{{ item.name }}</td>
                <td>{{ item.config ? item.config.concurrency : '-' }}</td>
                <td>{{ ((1 - item.summary.error_rate) * 100).toFixed(1) }}%</td>
                <td>{{ fmt((item.summary.ttft_ms || {}).p50, ' ms') }}</td>
                <td>{{ fmt((item.summary.ttft_ms || {}).p99, ' ms') }}</td>
                <td>{{ fmt((item.summary.itl_ms || {}).p50, ' ms') }}</td>
                <td>{{ fmt((item.summary.latency_s || {}).p50, ' s') }}</td>
                <td>{{ fmt((item.summary.prefill_tps || {}).avg) }}</td>
                <td>{{ fmt((item.summary.decode_tps || {}).avg) }}</td>
                <td>{{ fmt((item.summary.throughput_per_request_tps || {}).avg) }}</td>
                <td>{{ item.summary.aggregate_throughput_tps }}</td>
                <td>{{ item.summary.slo ? item.summary.slo.goodput_rps : '-' }}</td>
                <td>{{ item.summary.slo ? (item.summary.slo.rate * 100).toFixed(1) + '%' : '-' }}</td>
                <td v-if="item.summary.failed_count > 0">
                  <span v-for="(c, r, j) in (item.summary.error_groups || {})" :key="j" :title="r"
                    style="color:var(--el-color-warning);">{{ failShort(r) }} × {{ c }}<br/></span>
                </td>
                <td v-else style="color:var(--el-color-success);">—</td>
              </template>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 实时日志 -->
      <div class="ix-section-title">实时日志</div>
      <div ref="logEl" class="ix-logbox">
        <div v-if="!ui.logs.length" class="muted">该任务暂无日志(仅运行中实时展示;完成后点击任务可回看结果摘要)</div>
        <div v-for="(l, i) in ui.logs" :key="i" :class="l.cls">[{{ l.t }}] {{ l.msg }}</div>
      </div>
    </div>
  </el-card>`,
};
