// ============================== 左栏卡片 2:测试策略编辑 + 创建任务 ==============================
import { ElMessage, ElMessageBox } from "element-plus";
import { S, buildDefaultStrategies, createTask, saveReportsDir } from "./ix-store.js";

export default {
  setup() {
    function selectAll(v) { S.strategies.forEach(s => { s.checked = v; }); }
    function resetDefaults() { S.strategies = buildDefaultStrategies(); }
    function addRow() {
      S.strategies.push({
        name: `自定义策略${S.strategies.length + 1}`,
        concurrency: 4, total: 16, input: 256, output: 256, lang: "zh", checked: true,
      });
    }
    function delRow(i) { S.strategies.splice(i, 1); }

    // ---------- 一键并发扫描:固定 ISL/OSL 扫多档并发,得到延迟-吞吐/Goodput 曲线 ----------
    function sweepAdd() {
      const base = S.strategies.find(s => s.checked) || S.strategies[0];
      const input = parseInt(base && base.input, 10) || 256;
      const output = parseInt(base && base.output, 10) || 256;
      const lang = (base && base.lang) || "zh";
      const levels = [1, 2, 4, 8, 16, 32, 64];
      S.strategies = levels.map(c => ({
        name: `扫描·${c}并发·${input}入${output}出`,
        concurrency: c, total: Math.min(200, Math.max(32, c * 4)),
        input, output, lang, checked: true, cacheMode: "auto", warmup: 2,
      }));
      ElMessageBox.alert(
        `已生成 <b>${levels.length}</b> 档并发扫描策略(并发 ${levels.join("/")})<br>` +
        `ISL=${input} / OSL=${output} 固定不变,每组预热 2 个请求(不计入统计)<br>` +
        `配合下方 SLO 阈值可在详情页得到并发扩展性/Goodput 曲线;单点对比容易误判,曲线才是结论`,
        "并发扫描", { dangerouslyUseHTMLString: true },
      );
    }

    // ---------- 智能添加策略:按模型规模 + 探测上下文实际重建整套策略 ----------
    // 小模型(≤8B)减少策略条数;大模型(≥70B)或长上下文(≥32k)增加并发档位并追加超长入策略
    function guessParamsB(model) {
      const s = String(model || "");
      let m = s.match(/(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z0-9])/);   // 7B / 32B / 0.6B
      if (m) return parseFloat(m[1]);
      m = s.match(/(\d+(?:\.\d+)?)\s*[mM](?![a-zA-Z0-9])/);       // 500M
      if (m) return parseFloat(m[1]) / 1000;
      if (/air|mini|small|lite|nano|tiny|edge/i.test(s)) return 4;
      if (/max|large|xl\W|pro(?!\w)|flagship/i.test(s)) return 100;
      return null;
    }
    function smartAdd() {
      const maxIn = S.maxIn, maxOut = S.maxOut;
      const paramsB = guessParamsB(S.model);
      const ctx = maxIn || (maxOut ? maxOut + 1024 : 0);
      if (!S.model && !ctx) {
        ElMessage.warning("请先「自动检测」选择模型,或「探测最大输入/输出 tokens」后再智能生成");
        return;
      }
      // 规模分档:小(<8B)→ 2 档并发;中(8~70B)→ 3 档;大(≥70B 或 ctx≥64k)→ 4 档
      let tier = paramsB == null ? (ctx >= 65536 ? "large" : "mid")
                               : (paramsB < 8 ? "small" : paramsB < 70 ? "mid" : "large");
      if (ctx >= 131072) tier = "large";
      const tierLabel = { small: "小模型", mid: "中模型", large: "大模型" }[tier];
      const levels = { small: [4, 16], mid: [4, 16, 32], large: [4, 16, 32, 64] }[tier];
      const totalOf = c => (c <= 4 ? 8 : c <= 16 ? 16 : c <= 32 ? 24 : 32);
      // 组合:基础四象限;长上下文(≥32k)追加超长入两档
      let combos = [
        ["短入短出", 256, 256], ["长入短出", 2048, 256],
        ["短入长出", 256, 2048], ["长入长出", 2048, 2048],
      ];
      if (ctx >= 32768) {
        combos.push(["超长入短出", Math.floor(ctx * 0.5), 256]);
        combos.push(["超长入长出", Math.floor(ctx * 0.5), Math.min(4096, Math.floor(ctx * 0.25))]);
      }
      // 探测到上限时按预算收敛(输入 ≤ maxIn,输出 ≤ maxOut,in+out ≤ 80% 上下文)
      const capIn = maxIn || 0, capOut = maxOut || 0;
      const budget = ctx ? Math.floor(ctx * 0.8) : 0;
      const list = [];
      for (const c of levels) {
        for (const [label, i, o] of combos) {
          let fi = i, fo = o;
          if (ctx) {
            if (capIn) fi = Math.min(fi, capIn);
            if (capOut) fo = Math.min(fo, capOut);
            if (budget) {
              if (fi + fo > budget) fo = Math.max(64, Math.min(fo, budget - fi));
              fi = Math.min(fi, Math.max(128, budget - fo));
            }
            fi = Math.max(128, Math.round(fi));
            fo = Math.max(1, Math.round(fo));
          }
          list.push({
            name: `${c}并发·${label}${(fi !== i || fo !== o) ? `(${fi}/${fo})` : ""}`,
            concurrency: c, total: totalOf(c), input: fi, output: fo, lang: "zh", checked: true,
          });
        }
      }
      S.strategies = list;
      ElMessageBox.alert(
        `已按 <b>${tierLabel}</b> 实际重建 <b>${list.length}</b> 条策略<br>` +
        (paramsB != null ? `参数规模 ≈ ${paramsB < 1 ? (paramsB * 1000).toFixed(0) + "M" : paramsB + "B"}` : "参数规模:模型名未识别,按上下文判断") +
        `<br>` + (ctx
          ? `上下文 ≈ ${ctx.toLocaleString()},安全预算(80%)=${budget.toLocaleString()},输入上限 ${capIn || "未探测"},输出上限 ${capOut || "未探测"}`
          : `上下文未探测:建议先「探测最大输入/输出 tokens」让生成长度更贴合实际上限`) +
        `<br>并发档位 [${levels.join(", ")}]${ctx >= 32768 ? ",含超长入 2 条" : ""}`,
        "智能添加策略", { dangerouslyUseHTMLString: true },
      );
    }

    async function onSaveDir() {
      await saveReportsDir(S.reportsDir.trim());
    }

    return { S, selectAll, resetDefaults, addRow, delRow, smartAdd, sweepAdd, createTask, onSaveDir };
  },
  template: `
  <el-card shadow="never" class="ix-card">
    <template #header><span class="ix-card-title">🧪 测试策略</span></template>
    <div class="ix-note">默认 16 条,可增删/勾选/修改;请求数上限 2000(P99 有统计意义建议每组 ≥200 个请求)。缓存列:默认=跟随全局随机/固定;冷=每请求注入唯一前缀(强制 prefill,测纯引擎能力);热=固定同文(前缀缓存全命中,测缓存收益)。预热列:前 N 个请求仅执行不计入统计,剔除冷启动。</div>

    <div class="ix-tools">
      <el-button size="small" @click="selectAll(true)">全选</el-button>
      <el-button size="small" @click="selectAll(false)">全不选</el-button>
      <el-button size="small" @click="resetDefaults">恢复默认</el-button>
      <el-button size="small" @click="addRow">+ 添加策略</el-button>
      <el-button size="small" title="按模型规模与探测出的上下文上限,实际重建整套策略:小模型精简条数,大模型/长上下文加密档位" @click="smartAdd">智能添加策略</el-button>
      <el-button size="small" title="固定 ISL/OSL,自动生成 1~64 并发多档策略(画延迟-吞吐/Goodput 曲线,单点对比易误判)" @click="sweepAdd">📈 并发扫描</el-button>
      <span class="grow"></span>
      <el-checkbox v-model="S.randomPrompt" title="开启后:创建任务时先调用被测 API 生成一批随机主题,测试期间每个请求的输入文本互不相同;关闭则全部请求使用同一段固定文本">🎲 随机Prompt</el-checkbox>
    </div>

    <div class="ix-st-scroll">
      <table class="ix-st-table">
        <thead>
          <tr><th></th><th>策略</th><th class="num">并发</th><th class="num">请求数</th>
            <th class="num">输入tok</th><th class="num">输出tok</th><th>语言</th><th>缓存</th><th class="num" title="预热请求数:前 N 个请求仅执行不计入统计">预热</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="(s, i) in S.strategies" :key="i">
            <td style="text-align:center;"><input type="checkbox" v-model="s.checked" title="勾选参与压测"/></td>
            <td><input type="text" class="st-name" v-model="s.name"/></td>
            <td><input type="number" class="st-num" v-model.number="s.concurrency" min="1" max="256" step="4"/></td>
            <td><input type="number" class="st-num" v-model.number="s.total" min="1" max="2000" step="4"/></td>
            <td><input type="number" class="st-num" v-model.number="s.input" min="10" max="100000"/></td>
            <td><input type="number" class="st-num" v-model.number="s.output" min="1" max="16000"/></td>
            <td>
              <select class="st-lang" v-model="s.lang">
                <option value="zh">中</option>
                <option value="en">EN</option>
              </select>
            </td>
            <td>
              <select class="st-lang" v-model="s.cacheMode" title="缓存模式:默认跟随全局随机/固定;冷=每请求唯一前缀(前缀缓存必未命中);热=固定同文(首请求后全命中)">
                <option value="auto">默认</option>
                <option value="cold">冷</option>
                <option value="hot">热</option>
              </select>
            </td>
            <td><input type="number" class="st-num" v-model.number="s.warmup" min="0" max="10" title="预热请求数:前 N 个请求仅执行不计入统计(剔除冷启动)"/></td>
            <td><el-button text size="small" type="danger" title="删除此行" @click="delRow(i)">×</el-button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="ix-section-title" style="margin-top:14px;">对比受控项 (SLO / 采样 / 硬件)</div>
    <div class="ix-row" style="flex-wrap:wrap;gap:4px;">
      <el-input-number v-model="S.sloTtft" :min="0" :max="60000" :step="100" controls-position="right" style="width:128px;" title="SLO:TTFT 上限(ms)。设置后统计 Goodput(满足 SLO 的有效请求速率)与达标率;0=不启用"/>
      <span class="ix-mini">TTFT≤ms</span>
      <el-input-number v-model="S.sloTpot" :min="0" :max="2000" :step="5" controls-position="right" style="width:104px;" title="SLO:TPOT(逐 token 间隔)上限(ms)。0=不启用"/>
      <span class="ix-mini">TPOT≤ms</span>
      <el-input-number v-model="S.temperature" :min="0" :max="2" :step="0.1" controls-position="right" style="width:96px;" title="采样 temperature:固定后不同轮次的解码路径才可比;清空=引擎默认"/>
      <span class="ix-mini">temperature</span>
      <el-input-number v-model="S.seed" :min="0" :max="999999" :step="1" controls-position="right" style="width:108px;" title="采样 seed(OpenAI 协议);0=不指定"/>
      <span class="ix-mini">seed</span>
    </div>
    <el-input v-model="S.gpuInfo" style="margin-top:6px;" placeholder="GPU/硬件环境(写入报告头,如 A100×8 80G NVLink / H20×4,便于跨环境对比溯源)"/>
    <div class="ix-note">SLO 设为 0 不启用;设置后汇总表/分析报告以 Goodput(满足 SLO 的 req/s)为最终对比口径。temperature/seed 固定后对比轮次才可复现;冷/热缓存列用于受控对比前缀缓存收益。</div>

    <div style="margin-top:12px;">
      <el-button type="primary" style="width:100%;" @click="createTask">创建压测任务</el-button>
    </div>

    <div class="ix-section-title" style="margin-top:16px;">报告存储路径 (服务端主机目录)</div>
    <div class="ix-row">
      <el-input v-model="S.reportsDir" placeholder="默认为项目 reports/ 目录"/>
      <el-button style="flex:none;" @click="onSaveDir">保存</el-button>
    </div>
    <div class="ix-note">{{ S.dirNote || '留空则使用默认目录' }}</div>
  </el-card>`,
};
