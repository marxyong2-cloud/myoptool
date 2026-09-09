// ============================== 右栏:历史报告 + Excel 预览弹窗 ==============================
import { computed } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "./api.js";
import { S, pollReports, downloadReport } from "./ix-store.js";

// ---------- 历史报告卡 ----------
export const ReportsCard = {
  setup() {
    async function onDelete(it) {
      try {
        await ElMessageBox.confirm(`删除报告 ${it.filename}?`, "删除报告", { type: "warning" });
      } catch { return; }
      try {
        await api(`/api/reports/${encodeURIComponent(it.filename)}`, { method: "DELETE" });
        pollReports();
      } catch (e) { ElMessage.error(`删除失败: ${e.message}`); }
    }
    const title = it => it.model ? `${it.model} · benchmark ${it.mtime}` : it.filename;
    const sub = it => [it.framework, it.size_kb + "KB"].filter(Boolean).join(" · ");
    return { S, title, sub, downloadReport, onDelete };
  },
  template: `
  <el-card shadow="never" class="ix-card">
    <template #header><span class="ix-card-title">历史报告</span></template>
    <div v-if="!S.reports.length" class="ix-note">暂无历史报告。</div>
    <div v-for="it in S.reports.slice(0, 30)" :key="it.filename" class="ix-report-item">
      <span class="r-name" :title="it.filename">
        <b>{{ title(it) }}</b>
        <div class="r-sub">{{ sub(it) }}</div>
      </span>
      <el-button size="small" @click="$emit('preview', it.filename)">预览</el-button>
      <el-button size="small" @click="downloadReport(it.filename)">下载</el-button>
      <el-button size="small" type="danger" title="删除报告" @click="onDelete(it)">×</el-button>
    </div>
  </el-card>`,
  emits: ["preview"],
};

// ---------- Excel 预览弹窗(全局单例,由 store 的 S.preview 驱动) ----------
export const PreviewDialog = {
  setup() {
    const p = S.preview;
    const sheet = computed(() => (p.data && p.data.sheets && p.data.sheets[p.sheetIdx]) || null);
    function isHead(ri) {
      const s = sheet.value;
      if (!s) return false;
      if (ri === 0) return true;
      // 汇总表表头行随环境信息块行数变化,动态识别(首列为「策略」的行)
      if (s.name === "汇总") {
        const r = s.rows[ri];
        return !!r && r[0] === "策略";
      }
      if (s.name === "分析报告") return ri === 5;
      return ri === 1;
    }
    return { p, sheet, isHead };
  },
  template: `
  <el-dialog v-model="p.visible" width="min(1000px, 94vw)" top="4vh" :title="'报告预览 — ' + p.filename">
    <div v-if="p.data" class="ix-sheet-tabs">
      <button v-for="(s, i) in p.data.sheets" :key="i" class="ix-sheet-tab"
        :class="{ active: i === p.sheetIdx }" @click="p.sheetIdx = i">{{ s.name }}</button>
    </div>
    <div v-if="sheet" style="max-height:68vh;overflow:auto;">
      <table class="ix-preview-table">
        <tbody>
          <tr v-for="(row, ri) in sheet.rows" :key="ri" :class="{ 'head-row': isHead(ri) }">
            <td v-for="(cell, ci) in row" :key="ci" :title="cell">{{ cell }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="sheet.truncated" class="ix-note" style="padding:8px 2px;">
        仅预览前 {{ sheet.rows.length }} / {{ sheet.total_rows }} 行,完整内容请下载 Excel 查看。
      </div>
    </div>
  </el-dialog>`,
};
