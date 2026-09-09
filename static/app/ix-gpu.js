// ============================== GPU 监控对话框(SSH 直连 + URL 服务双模式) ==============================
import { reactive } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "./api.js";
import { isViewer } from "./auth.js";

export const G = reactive({
  visible: false,
  urlMonitors: [],          // [{name, url}]
  sshMonitors: [],          // [{name, host, ssh_port, username, password}]
  active: null,             // "ssh:0" / "url:1" / null(管理页)
  // 添加表单
  sshForm: { name: "", host: "", port: 22, user: "root", pwd: "" },
  sshProbe: { msg: "", ok: null },
  urlForm: { name: "", url: "" },
  urlProbe: { msg: "", ok: null },
});
window.G = G;

const gpuHost = u => { try { return new URL(u).host; } catch { return u; } };

export function openGpu() {
  G.visible = true;
  loadGpu();
}

async function loadGpu() {
  try {
    const [r1, r2] = await Promise.all([
      api("/api/gpu-monitors").catch(() => ({ monitors: [] })),
      api("/api/gpu-ssh").catch(() => ({ monitors: [] })),
    ]);
    G.urlMonitors = r1.monitors || [];
    G.sshMonitors = r2.monitors || [];
  } catch { G.urlMonitors = []; G.sshMonitors = []; }
  if (G.active !== null) {
    const [t, i] = G.active.split(":");
    const list = t === "ssh" ? G.sshMonitors : G.urlMonitors;
    if (!list[i]) G.active = null;
  }
  if (G.active === null) {
    if (G.sshMonitors.length) G.active = "ssh:0";
    else if (G.urlMonitors.length) G.active = "url:0";
  }
}

async function saveUrlMonitors() {
  try {
    await api("/api/gpu-monitors", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ monitors: G.urlMonitors }),
    });
  } catch { /* 保存失败不阻塞界面 */ }
}
async function saveSshMonitors() {
  try {
    await api("/api/gpu-ssh", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ monitors: G.sshMonitors }),
    });
  } catch { /* 保存失败不阻塞界面 */ }
}

function frameSrc() {
  if (!G.active) return "";
  const [t, i] = G.active.split(":");
  if (t === "ssh") {
    const m = G.sshMonitors[i];
    return m ? `/gpu-ssh-dashboard?name=${encodeURIComponent(m.name || m.host)}` : "";
  }
  const m = G.urlMonitors[i];
  return m ? m.url : "";
}

async function probeSsh() {
  const f = G.sshForm;
  if (!f.host.trim()) { G.sshProbe = { msg: "请填写主机 IP", ok: false }; return; }
  G.sshProbe = { msg: "SSH 连接探测中...", ok: null };
  try {
    const d = await api("/api/gpu-ssh/probe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: f.name.trim(), host: f.host.trim(),
        ssh_port: parseInt(f.port, 10) || 22,
        username: f.user.trim() || "root", password: f.pwd,
      }),
    });
    G.sshProbe = { msg: (d.ok ? "✅ " : "❌ ") + (d.message || ""), ok: !!d.ok };
  } catch (e) {
    G.sshProbe = { msg: "❌ " + e.message, ok: false };
  }
}

async function addSsh() {
  const f = G.sshForm;
  if (!f.host.trim()) { ElMessage.warning("请填写主机 IP"); return; }
  if (!f.pwd) {
    try {
      await ElMessageBox.confirm("未填写密码,确定添加?(无密码通常无法 SSH 登录)", "提示", { type: "warning" });
    } catch { return; }
  }
  G.sshMonitors.push({
    name: f.name.trim() || f.host.trim(), host: f.host.trim(),
    ssh_port: parseInt(f.port, 10) || 22,
    username: f.user.trim() || "root", password: f.pwd,
  });
  await saveSshMonitors();
  G.active = "ssh:" + (G.sshMonitors.length - 1);
  G.sshForm = { name: "", host: "", port: 22, user: "root", pwd: "" };
  G.sshProbe = { msg: "", ok: null };
}

async function probeUrl() {
  const url = G.urlForm.url.trim();
  if (!url) { G.urlProbe = { msg: "请先填写服务地址", ok: false }; return; }
  G.urlProbe = { msg: "探测中...", ok: null };
  try {
    const d = await api("/api/gpu-monitors/probe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    G.urlProbe = { msg: (d.ok ? "✅ " : "❌ ") + (d.message || ""), ok: !!d.ok };
  } catch (e) {
    G.urlProbe = { msg: "❌ " + e.message, ok: false };
  }
}

async function addUrl() {
  const url = G.urlForm.url.trim();
  if (!url) { ElMessage.warning("请填写服务地址"); return; }
  G.urlMonitors.push({ name: G.urlForm.name.trim(), url });
  await saveUrlMonitors();
  G.active = "url:" + (G.urlMonitors.length - 1);
  G.urlForm = { name: "", url: "" };
  G.urlProbe = { msg: "", ok: null };
}

async function delSsh(i) {
  const m = G.sshMonitors[i];
  try {
    await ElMessageBox.confirm(`删除 SSH 主机「${m.name || m.host}」?`, "删除", { type: "warning" });
  } catch { return; }
  G.sshMonitors.splice(i, 1);
  await saveSshMonitors();
  if (G.active === "ssh:" + i) G.active = null;
}

async function delUrl(i) {
  const m = G.urlMonitors[i];
  try {
    await ElMessageBox.confirm(`删除监控「${m.name || gpuHost(m.url)}」?`, "删除", { type: "warning" });
  } catch { return; }
  G.urlMonitors.splice(i, 1);
  await saveUrlMonitors();
  if (G.active === "url:" + i) G.active = null;
}

export default {
  setup() {
    return { G, gpuHost, frameSrc, probeSsh, addSsh, probeUrl, addUrl, delSsh, delUrl, RO: isViewer() };
  },
  template: `
  <el-dialog v-model="G.visible" width="min(1280px, 97vw)" top="2vh"
    :show-close="true" class="ix-gpu-dialog">
    <template #header>
      <span style="font-weight:700;">🖥 GPU 监控</span>
      <span class="ix-note-inline" style="margin-left:8px;">看板来自 GPU 服务器上的 gpu-monitor 服务 · 自动刷新</span>
    </template>
    <div class="ix-gpu-tabs">
      <button v-for="(m, i) in G.sshMonitors" :key="'ssh' + i" type="button" class="ix-gpu-tab"
        :class="{ on: G.active === 'ssh:' + i }" :title="'SSH 直连 ' + m.host + ':' + m.ssh_port + ' 执行 mthreads-gmi'"
        @click="G.active = 'ssh:' + i">🔐 {{ m.name || m.host }}</button>
      <button v-for="(m, i) in G.urlMonitors" :key="'url' + i" type="button" class="ix-gpu-tab"
        :class="{ on: G.active === 'url:' + i }" :title="m.url"
        @click="G.active = 'url:' + i">🔗 {{ m.name || gpuHost(m.url) }}</button>
      <button v-if="!RO" type="button" class="ix-gpu-tab" :class="{ on: G.active === null }"
        title="管理监控主机与服务地址" @click="G.active = null">＋ 添加 / 管理</button>
    </div>

    <!-- 监控看板 -->
    <div v-if="G.active !== null" class="ix-gpu-frame-wrap">
      <iframe v-if="frameSrc()" :src="frameSrc()" class="ix-gpu-frame"></iframe>
    </div>

    <!-- 管理页(预览账号只读) -->
    <div v-else-if="RO" class="ix-gpu-manage">
      <p class="ix-note">👁 预览模式:GPU 监控仅可查看,不可添加 / 管理监控主机</p>
    </div>
    <div v-else class="ix-gpu-manage">
      <h3>添加 SSH 直连主机(推荐 — 无需在服务器部署任何服务)</h3>
      <div class="ix-row">
        <input v-model="G.sshForm.name" type="text" class="ix-gpu-in" placeholder="名称(如 S5000-31)" style="width:130px;"/>
        <input v-model="G.sshForm.host" type="text" class="ix-gpu-in" placeholder="主机 IP,如 10.0.1.5" style="width:150px;"/>
        <input v-model.number="G.sshForm.port" type="number" class="ix-gpu-in" title="SSH 端口" style="width:64px;"/>
        <input v-model="G.sshForm.user" type="text" class="ix-gpu-in" placeholder="用户名" style="width:90px;"/>
        <input v-model="G.sshForm.pwd" type="password" class="ix-gpu-in" placeholder="密码" style="width:110px;"/>
        <el-button size="small" @click="probeSsh">探测</el-button>
        <el-button size="small" type="primary" @click="addSsh">添加</el-button>
      </div>
      <div v-if="G.sshProbe.msg" class="ix-probe-msg" :class="{ ok: G.sshProbe.ok, err: G.sshProbe.ok === false }">{{ G.sshProbe.msg }}</div>
      <p class="ix-note">填写 GPU 服务器的 SSH 用户名/密码,本服务将直连主机执行 mthreads-gmi,实时查看每卡利用率/显存/温度/功耗、GPU 进程、驱动与内核模块状态、拓扑矩阵。密码明文保存在本机 config.json,请确认环境安全。</p>

      <h3>已配置的 SSH 主机</h3>
      <div v-if="!G.sshMonitors.length" class="ix-note">尚未配置任何 SSH 主机</div>
      <div v-for="(m, i) in G.sshMonitors" :key="i" class="ix-mon-item">
        <b>{{ m.name || m.host }}</b>
        <span class="u">{{ m.username }}@{{ m.host }}:{{ m.ssh_port }}</span>
        <el-button size="small" @click="G.active = 'ssh:' + i">查看</el-button>
        <el-button size="small" type="danger" @click="delSsh(i)">删除</el-button>
      </div>

      <h3>添加 URL 型监控服务(gpu-monitor 已部署时)</h3>
      <div class="ix-row">
        <input v-model="G.urlForm.name" type="text" class="ix-gpu-in" placeholder="名称(如 S5000-31)" style="width:150px;"/>
        <input v-model="G.urlForm.url" type="text" class="ix-gpu-in" placeholder="服务地址,如 http://10.0.1.5:8080" style="flex:1;min-width:200px;"/>
        <el-button size="small" @click="probeUrl">探测</el-button>
        <el-button size="small" type="primary" @click="addUrl">添加</el-button>
      </div>
      <div v-if="G.urlProbe.msg" class="ix-probe-msg" :class="{ ok: G.urlProbe.ok, err: G.urlProbe.ok === false }">{{ G.urlProbe.msg }}</div>
      <div v-if="!G.urlMonitors.length" class="ix-note">尚未配置任何 URL 型监控服务</div>
      <div v-for="(m, i) in G.urlMonitors" :key="i" class="ix-mon-item">
        <b>{{ m.name || gpuHost(m.url) }}</b>
        <span class="u">{{ m.url }}</span>
        <el-button size="small" @click="G.active = 'url:' + i">查看</el-button>
        <el-button size="small" type="danger" @click="delUrl(i)">删除</el-button>
      </div>

      <h3>URL 型服务部署说明</h3>
      <pre class="ix-deploy-pre">cd gpu-monitor
sudo bash install.sh        # 默认 8080 端口,或指定:sudo bash install.sh 8899
# 防火墙放行:sudo ufw allow 8080/tcp</pre>
    </div>
  </el-dialog>`,
};
