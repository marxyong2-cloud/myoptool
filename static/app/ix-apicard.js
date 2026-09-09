// ============================== 左栏卡片 1:API 配置 + 服务信息 ==============================
import { computed, ref, watch, onUnmounted } from "vue";
import { ElMessage } from "element-plus";
import {
  S, detect, probeLimits, refreshServerMetrics, loadSavedApis, removeSavedApi, log,
} from "./ix-store.js";

export default {
  setup() {
    const cfgEntries = computed(() => {
      const cfg = S.serverInfo.cfg || {};
      return Object.keys(cfg).sort((a, b) => a.localeCompare(b)).map(k => ({ k, raw: cfg[k] }));
    });

    // 服务信息面板:展开期间每 15s 自动刷新(压测进行时可实时盯 vLLM/SGLang 指标)
    const svcOpen = ref([]);
    let svcTimer = null;
    watch(svcOpen, open => {
      clearInterval(svcTimer);
      if (open.includes("svc")) {
        if (S.apiUrl.trim() && !S.serverInfo.loading) refreshServerMetrics();   // 展开立即刷一次
        svcTimer = setInterval(() => {
          if (!S.serverInfo.loading && S.apiUrl.trim()) refreshServerMetrics();
        }, 15000);
      }
    });
    onUnmounted(() => clearInterval(svcTimer));

    function cfgVal(entry) {
      const { k, raw } = entry;
      if (/prefix_caching/i.test(k)) {
        const on = raw === true || raw === "true" || raw === 1;
        return { on, text: on ? "✅ 开启" : "❌ 关闭" };
      }
      if (/disable_radix/i.test(k)) {
        const off = raw === true || raw === "true" || raw === 1;
        return { on: !off, text: off ? "❌ 已禁用" : "✅ 启用" };
      }
      const v = typeof raw === "object" ? JSON.stringify(raw) : String(raw);
      return { on: /prefix_caching|radix/i.test(k), text: v.length > 60 ? v.slice(0, 60) + "…" : v };
    }

    function onSavedApiChange() {
      const i = S.savedSel;
      if (i === "") return;
      const c = loadSavedApis()[parseInt(i, 10)];
      if (!c) return;
      S.apiUrl = c.url;
      S.apiKey = c.key || "";
      detect();
    }

    async function onDeleteSavedApi() {
      const i = S.savedSel;
      if (i === "") { ElMessage.warning("请先在下拉中选择要删除的 API"); return; }
      const removed = removeSavedApi(parseInt(i, 10));
      S.savedSel = "";
      if (removed) log(`已删除已保存 API: ${removed.url}`, "l-ok");
    }

    const capLabel = { chat: "对话", image: "文生图", video: "文生视频" };

    return { S, svcOpen, cfgEntries, cfgVal, capLabel, onSavedApiChange, onDeleteSavedApi,
             detect, probeLimits, refreshServerMetrics };
  },
  template: `
  <el-card shadow="never" class="ix-card">
    <template #header><span class="ix-card-title">🔌 API 配置</span></template>

    <el-form label-position="top" size="small" class="ix-form">
      <div class="ix-label">API 地址 (Base URL)</div>
      <div class="ix-row">
        <el-input v-model="S.apiUrl" placeholder="https://api.openai.com 或 http://localhost:8000"
          clearable @keyup.enter="detect"/>
        <el-button :loading="S.detecting" @click="detect">自动检测</el-button>
      </div>
      <div class="ix-note">支持 OpenAI / Anthropic / Ollama / vLLM / SGLang / TGI / LM Studio</div>

      <div v-if="S.protocol || S.framework !== 'Unknown'" class="ix-badges" style="margin-top:6px;">
        <el-tag size="small" type="info">协议: {{ S.protocol }}</el-tag>
        <el-tag size="small" type="info">架构: {{ S.framework }}</el-tag>
        <el-tag size="small" type="success">模型数: {{ S.models.length }}</el-tag>
        <el-button v-if="S.modelDetails.length || Object.keys(S.serverMeta).length"
          size="small" @click="S.infoVisible = true">模型详情</el-button>
      </div>

      <div class="ix-label" style="margin-top:12px;">模型上下文上限探测 <span class="ix-note-inline">(自动检测后自动执行;探测中点下方按钮可停止)</span></div>
      <div class="ix-row">
        <el-button style="flex:none;" :type="S.probing ? 'warning' : 'default'"
          :title="S.probing ? '探测进行中,点击立即停止' : '对被测 API 发送梯度请求,二分搜索最大输入/输出 tokens'"
          @click="probeLimits(false)">{{ S.probing ? '⏹ 停止探测' : '探测最大输入/输出 tokens' }}</el-button>
        <el-select v-model="S.savedSel" placeholder="已保存 API..." @change="onSavedApiChange">
          <el-option v-for="(c, i) in S.savedApis" :key="i" :value="String(i)" :label="c.url"/>
        </el-select>
        <el-button style="flex:none;" title="删除下拉中选中的已保存 API" @click="onDeleteSavedApi">删</el-button>
      </div>
      <div v-if="S.maxIn != null || S.maxOut != null" class="ix-badges" style="margin-top:6px;">
        <el-tag size="small" type="success">最大输入: {{ S.maxIn != null ? '≈' + S.maxIn : '探测失败' }}</el-tag>
        <el-tag size="small" type="success">最大输出: {{ S.maxOut != null ? '≈' + S.maxOut : '探测失败' }}</el-tag>
      </div>

      <div class="ix-label" style="margin-top:12px;">API Key <span class="ix-note-inline">(可选,可留空)</span></div>
      <el-input v-model="S.apiKey" type="password" show-password placeholder="sk-... (本地服务通常无需填写)"/>

      <div class="ix-row2">
        <div class="grow">
          <div class="ix-label">任务类型</div>
          <el-select v-model="S.taskType">
            <el-option value="chat" label="对话 (chat)"/>
            <el-option value="image" label="文生图 (image)"/>
            <el-option value="video" label="文生视频 (video)"/>
          </el-select>
        </div>
        <div class="grow">
          <div class="ix-label">协议</div>
          <el-select v-model="S.protocol">
            <el-option value="openai" label="OpenAI Compatible"/>
            <el-option value="anthropic" label="Anthropic"/>
            <el-option value="ollama" label="Ollama"/>
          </el-select>
        </div>
      </div>

      <div class="ix-label" style="margin-top:12px;">模型</div>
      <el-select v-model="S.model" filterable allow-create default-first-option
        placeholder="输入或选择模型" style="width:100%;">
        <el-option v-for="m in S.models" :key="m" :value="m" :label="m"/>
      </el-select>
      <div v-if="S.models.length" class="ix-chipbox">
        <span v-for="m in S.models" :key="m" class="ix-chip" :class="{ sel: S.model === m }"
          title="点击选用该模型" @click="S.model = m">{{ m }}</span>
      </div>
      <div v-if="S.caps.length" class="ix-chipbox">
        <span class="ix-chip static">支持能力:</span>
        <span v-for="c in S.caps" :key="c" class="ix-chip" :class="{ sel: S.taskType === c }"
          title="点击切换为该任务类型" @click="S.taskType = c">{{ capLabel[c] || c }}</span>
      </div>

      <el-collapse v-model="svcOpen" class="ix-svc-collapse" style="margin-top:12px;">
        <el-collapse-item name="svc">
          <template #title>
            <span class="ix-card-title" style="font-size:13.5px;">📡 服务信息(全量)</span>
            <span class="ix-note-inline" style="margin-left:8px;">{{ S.serverInfo.sub }}</span>
          </template>
          <div class="ix-note">自动探测 API 能访问到的全部端点:/health、/version、/server_info(vLLM)、/get_server_info(SGLang)、/v1/models、/metrics(vLLM/SGLang 指标)。直连被网关拦截时,若已在「GPU监控」配置该主机的 SSH 直连,自动经 SSH 通道在主机内部获取(含 worker 端口发现)。</div>
          <div class="ix-badges">
            <span v-for="(b, i) in S.serverInfo.badges" :key="i" class="ix-badge" :class="b.cls"
              :title="b.title || ''">{{ b.text }}</span>
          </div>
          <template v-if="cfgEntries.length">
            <div class="ix-section-title">{{ S.serverInfo.cfgFrom }}</div>
            <div class="ix-kvgrid">
              <div v-for="e in cfgEntries" :key="e.k" class="ix-kvrow">
                <span class="k">{{ e.k }}</span>
                <span class="v" :class="{ on: cfgVal(e).on, off: cfgVal(e).on === false && /prefix_caching|disable_radix/i.test(e.k) }">{{ cfgVal(e).text }}</span>
              </div>
            </div>
          </template>
          <template v-if="S.serverInfo.models.length">
            <div class="ix-section-title">模型列表(/v1/models)</div>
            <div class="ix-chipbox">
              <span v-for="m in S.serverInfo.models" :key="m" class="ix-chip static">{{ m }}</span>
            </div>
          </template>
          <el-button size="small" style="margin-top:8px;" :loading="S.serverInfo.loading" @click="refreshServerMetrics()">🔄 刷新</el-button>
        </el-collapse-item>
      </el-collapse>
    </el-form>
  </el-card>`,
};
