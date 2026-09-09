// ============================== 左栏卡片 3:KV Cache 效率测试(ECharts 可视化) ==============================
import { reactive, ref, watch, onMounted, onUnmounted, nextTick } from "vue";
import { ElMessage } from "element-plus";
import * as echarts from "echarts";
import { api } from "./api.js";
import { S, fmt } from "./ix-store.js";

const KV_MODE_LABEL = { basic: "冷热对比", multiturn: "多轮增量", shared: "共享前缀并发", eviction: "缓存逐出" };
const SIZES = [256, 512, 1024, 2048, 4096, 8192];

export default {
  setup() {
    const kv = reactive({
      mode: "basic",
      sizes: { 256: false, 512: false, 1024: true, 2048: false, 4096: true, 8192: false },
      conc: 8,
      testing: false, diag: false,
      note: "", cfgNote: "", cfgOk: null,
      headers: [], rows: [],
      basicData: [],   // {toks, cold, warm, speedup} 冷热对比图表数据
    });
    const chartEl = ref(null);
    let chart = null;
    const chartVisible = ref(false);

    // 切换模式清空旧结果(不同模式的表头/行结构不同,残留会误导)
    watch(() => kv.mode, () => {
      kv.headers = []; kv.rows = []; kv.basicData = []; kv.note = "";
      renderChart();
    });

    function renderChart() {
      if (!kv.basicData.length) {
        chartVisible.value = false;
        if (chart) { chart.dispose(); chart = null; }   // div 被 v-if 移除,下次重建后重新 init
        return;
      }
      chartVisible.value = true;
      nextTick(() => {
        if (!chartEl.value) return;
        if (!chart) chart = echarts.init(chartEl.value);
        const d = kv.basicData;
        chart.setOption({
          backgroundColor: "transparent",
          grid: { left: 44, right: 40, top: 30, bottom: 26 },
          tooltip: { trigger: "axis" },
          legend: { data: ["冷启动 TTFT", "缓存命中 TTFT", "加速比"], top: 0, textStyle: { fontSize: 11, color: "#909399" } },
          xAxis: { type: "category", data: d.map(x => x.toks + " tok"), axisLabel: { fontSize: 10, color: "#909399" } },
          yAxis: [
            { type: "value", name: "ms", axisLabel: { fontSize: 10, color: "#909399" }, splitLine: { lineStyle: { color: "rgba(128,128,128,.15)" } } },
            { type: "value", name: "×", min: 0, axisLabel: { fontSize: 10, color: "#909399" }, splitLine: { show: false } },
          ],
          series: [
            { name: "冷启动 TTFT", type: "bar", data: d.map(x => x.cold), itemStyle: { color: "#f59e0b", borderRadius: [3, 3, 0, 0] }, barMaxWidth: 26 },
            { name: "缓存命中 TTFT", type: "bar", data: d.map(x => x.warm), itemStyle: { color: "#10b981", borderRadius: [3, 3, 0, 0] }, barMaxWidth: 26 },
            { name: "加速比", type: "line", yAxisIndex: 1, data: d.map(x => x.speedup), itemStyle: { color: "#38bdf8" }, lineStyle: { width: 2 }, symbolSize: 6 },
          ],
        });
        chart.resize();
      });
    }

    onMounted(() => {
      window.addEventListener("resize", onResize);
    });
    onUnmounted(() => {
      window.removeEventListener("resize", onResize);
      if (chart) { chart.dispose(); chart = null; }
    });
    function onResize() { if (chart) chart.resize(); }

    async function runOne(mode, apiUrl, model, toks) {
      const isBasic = mode === "basic";
      const d = await api(isBasic ? "/api/kv-cache-test" : "/api/kv-cache-suite", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_url: apiUrl, api_key: S.apiKey, protocol: S.protocol, model,
          input_tokens: toks,
          ...(isBasic ? {} : { mode, concurrency: parseInt(kv.conc, 10) || 8 }),
        }),
      });
      return d;
    }

    function pushResult(mode, toks, d) {
      if (mode === "basic") {
        if (!kv.headers.length) {
          kv.headers = ["输入tok", "冷启动TTFT", "缓存命中TTFT", "加速比", "冷prefill", "热prefill", "结论"];
        }
        kv.rows.push({ type: "data", cells: [
          String(toks), fmt(d.cold.ttft_ms, " ms"), fmt(d.warm.ttft_ms, " ms"),
          d.speedup != null ? d.speedup + "×" : "-", fmt(d.cold.prefill_tps), fmt(d.warm.prefill_tps),
          d.cached ? "✅ 缓存生效" : "⚠ 未加速",
        ], ok: d.cached });
        kv.basicData.push({ toks, cold: d.cold.ttft_ms, warm: d.warm.ttft_ms, speedup: d.speedup });
        renderChart();
        return d.cached ? "✅" : "⚠";
      }
      if (!kv.headers.length) kv.headers = d.headers || [];
      const cells = (d.rows && d.rows[0] || []).map(c => String(c));
      kv.rows.push({ type: "data", cells });
      const metricsNote = d.metrics_delta && d.metrics_delta.prefix_cache_hits != null
        ? ` · /metrics 命中计数 +${d.metrics_delta.prefix_cache_hits}` : "";
      kv.rows.push({ type: "verdict", text: `${d.verdict || ""}${metricsNote}`, span: (d.headers || []).length || 9 });
      return (d.verdict || "").startsWith("✅") ? "✅" : "⚠";
    }

    async function refreshCfgHint(apiUrl) {
      kv.cfgNote = "读取服务配置中..."; kv.cfgOk = null;
      try {
        const d = await api("/api/server-info", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ api_url: apiUrl, api_key: S.apiKey }),
        });
        if (d.server_info && "enable_prefix_caching" in d.server_info) {
          const on = d.server_info.enable_prefix_caching === true || d.server_info.enable_prefix_caching === "true";
          kv.cfgOk = on;
          kv.cfgNote = on
            ? "✅ 服务配置:prefix_caching 已开启(/server_info)"
            : "❌ 服务配置:prefix_caching 关闭(/server_info)— 建议启动参数加 --enable-prefix-caching 后重测";
        } else if (d.sglang_info) {
          const off = d.sglang_info.disable_radix_cache === true;
          kv.cfgOk = !off;
          kv.cfgNote = off
            ? "❌ 服务配置:RadixAttention 已禁用(/get_server_info)"
            : "✅ SGLang RadixAttention 前缀缓存默认启用(/get_server_info)";
        } else {
          kv.cfgNote = "服务配置端点(/server_info · /get_server_info)未暴露 prefix_caching 信息";
        }
      } catch { kv.cfgNote = ""; }
    }

    function validate() {
      const apiUrl = S.apiUrl.trim();
      const model = S.model.trim();
      if (!apiUrl || !model) { ElMessage.warning("请先在「API 配置」卡片填写 API 地址并「自动检测」选择模型"); return null; }
      const sizes = SIZES.filter(v => kv.sizes[v]).sort((a, b) => a - b);
      if (!sizes.length) { ElMessage.warning("请至少勾选一个输入长度"); return null; }
      return { apiUrl, model, sizes };
    }

    async function runTest() {
      const v = validate();
      if (!v) return;
      const { apiUrl, model, sizes } = v;
      const mode = kv.mode;
      refreshCfgHint(apiUrl);
      kv.testing = true;
      kv.headers = []; kv.rows = []; kv.basicData = [];
      renderChart();
      try {
        for (let i = 0; i < sizes.length; i++) {
          const toks = sizes[i];
          kv.note = `${KV_MODE_LABEL[mode]}测试中: ${toks} tok(${i + 1}/${sizes.length})...`;
          const row = reactive({ type: "pending" });
          kv.rows.push(row);
          try {
            const d = await runOne(mode, apiUrl, model, toks);
            kv.rows.splice(kv.rows.indexOf(row), 1);
            pushResult(mode, toks, d);
          } catch (e) {
            row.type = "error";
            row.text = `${toks} tok 失败: ${e.message}`;
          }
        }
        kv.note = `完成:${KV_MODE_LABEL[mode]} · ${sizes.join(" / ")} tok`;
      } finally { kv.testing = false; }
    }

    async function runDiag() {
      const v = validate();
      if (!v) return;
      const { apiUrl, model } = v;
      const toks = v.sizes[0];
      refreshCfgHint(apiUrl);
      kv.diag = true;
      kv.headers = []; kv.rows = []; kv.basicData = [];
      renderChart();
      const marks = [];
      try {
        for (const mode of ["basic", "multiturn", "shared", "eviction"]) {
          kv.note = `全套诊断(${toks} tok): ${KV_MODE_LABEL[mode]}...`;
          kv.rows.push({ type: "label", text: `${KV_MODE_LABEL[mode]} · ${toks} tok` });
          const row = reactive({ type: "pending" });
          kv.rows.push(row);
          try {
            const d = await runOne(mode, apiUrl, model, toks);
            kv.rows.splice(kv.rows.indexOf(row), 1);
            marks.push([KV_MODE_LABEL[mode], pushResult(mode, toks, d)]);
          } catch (e) {
            row.type = "error";
            row.text = `${KV_MODE_LABEL[mode]} 失败: ${e.message}`;
            marks.push([KV_MODE_LABEL[mode], "❌"]);
          }
        }
        const ok = marks.filter(x => x[1] === "✅").length;
        kv.rows.push({ type: "sum", text: `全套诊断汇总(${toks} tok): ${marks.map(x => x.join(" ")).join(" · ")} — `
          + (ok >= 3 ? "前缀缓存工作良好" : ok >= 1 ? "前缀缓存部分生效" : "前缀缓存未生效(建议检查服务启动参数)") });
        kv.note = `全套诊断完成 · ${toks} tok · ${ok}/4 项通过`;
      } finally { kv.diag = false; }
    }

    return { kv, KV_MODE_LABEL, SIZES, chartEl, chartVisible, runTest, runDiag };
  },
  template: `
  <el-card shadow="never" class="ix-card">
    <template #header><span class="ix-card-title">⚡ KV Cache 效率测试</span></template>
    <div class="ix-note">四种通用场景实测前缀缓存(Prefix Caching / KV Cache 复用):<b>冷热对比</b>同 prompt 连发两次; <b>多轮增量</b>验证长对话续写是否只 prefill 新增部分; <b>共享前缀并发</b>验证同前缀多请求并发命中; <b>缓存逐出</b>验证容量与 LRU 行为。使用「API 配置」卡片中的地址 / 协议 / 模型。</div>

    <div class="ix-seg">
      <button v-for="(label, m) in KV_MODE_LABEL" :key="m" type="button"
        :class="{ on: kv.mode === m }" @click="kv.mode = m">{{ label }}</button>
    </div>

    <div class="ix-kv-row">
      <label v-for="v in SIZES" :key="v" class="ix-kv-size">
        <input type="checkbox" v-model="kv.sizes[v]"/>{{ v }}
      </label>
      <select v-if="kv.mode === 'shared'" v-model="kv.conc" class="st-lang" title="并发请求数(共享前缀模式)">
        <option value="4">并发 4</option>
        <option value="8">并发 8</option>
        <option value="16">并发 16</option>
      </select>
      <span class="grow"></span>
      <el-button type="primary" size="small" :loading="kv.testing" style="margin-left:auto;" @click="runTest">▶ 开始测试</el-button>
      <el-button size="small" :loading="kv.diag" title="依次执行冷热对比/多轮增量/共享前缀并发/缓存逐出四种测试并汇总" @click="runDiag">🩺 一键全套诊断</el-button>
    </div>

    <div class="ix-note" :style="kv.cfgOk === true ? 'color:var(--el-color-success)' : kv.cfgOk === false ? 'color:var(--el-color-danger)' : ''">{{ kv.cfgNote }}</div>
    <div class="ix-note">{{ kv.note }}</div>

    <div v-if="chartVisible" ref="chartEl" class="ix-kv-chart"></div>

    <div v-if="kv.rows.length" class="ix-table-wrap">
      <table class="ix-result-table">
        <thead v-if="kv.headers.length">
          <tr><th v-for="(h, i) in kv.headers" :key="i">{{ h }}</th></tr>
        </thead>
        <tbody>
          <template v-for="(r, i) in kv.rows" :key="i">
            <tr v-if="r.type === 'data'">
              <td v-for="(c, j) in r.cells" :key="j"
                :style="j === r.cells.length - 1 && r.ok != null ? 'color:' + (r.ok ? 'var(--el-color-success)' : 'var(--el-color-warning)') : ''">{{ c }}</td>
            </tr>
            <tr v-else-if="r.type === 'pending'"><td :colspan="kv.headers.length || 9" class="muted">请求进行中...</td></tr>
            <tr v-else-if="r.type === 'error'"><td :colspan="kv.headers.length || 9" style="color:var(--el-color-danger);">{{ r.text }}</td></tr>
            <tr v-else-if="r.type === 'label'"><td :colspan="kv.headers.length || 9" class="ix-kv-label">{{ r.text }}</td></tr>
            <tr v-else-if="r.type === 'verdict'"><td :colspan="r.span" class="ix-kv-verdict">{{ r.text }}</td></tr>
            <tr v-else-if="r.type === 'sum'"><td :colspan="kv.headers.length || 9" class="ix-kv-sum">{{ r.text }}</td></tr>
          </template>
        </tbody>
      </table>
    </div>
  </el-card>`,
};
