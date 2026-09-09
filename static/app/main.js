// ============================== 全局 SPA 外壳:雍宁工具链(压测工作台 · 模型工作台 · 模型部署站) ==============================
// 标题栏常驻置顶(hash 路由切换下方页面,标题栏不刷新);左上角品牌「雍宁工具链」固定,模块名随导航切换;字号/主题三页统一
// 账户体系:未登录显示登录页;登录后按账号隔离数据(压测/会话/API/渠道/预设/方案/主机分配)
import { createApp, ref, reactive, computed, watch, onMounted } from "vue";
import ElementPlus from "element-plus";
import zhCn from "element-plus/locale-zh-cn";
import { useTheme } from "./theme.js";
import { registerIcons } from "./icons.js";
import { sweepStuckOverlays } from "./domfix.js";
import BenchView, { HelpDrawer, GameModal } from "./ix-main.js";
import ModelUseView from "./mu-main.js";
import ModelStartView from "./ms-main.js";
import ChatDrawer, { C, toggleChat } from "./ix-chat.js";
import GpuDialog, { openGpu } from "./ix-gpu.js";
import { AUTH, isAdmin, isViewer, hasPerm, login, logout, register, fetchMe, migrateLocalData } from "./auth.js";
import { UsersDrawer, PwdDialog } from "./users.js";
import { api } from "./api.js";

const PAGES = [
  { v: "bench", label: "压测工作台", en: "Model Bench" },
  { v: "modeluse", label: "模型工作台", en: "Model Use" },
  { v: "modelstart", label: "模型部署站", en: "Model Start" },
];
const VIEW_COMPS = { bench: BenchView, modeluse: ModelUseView, modelstart: ModelStartView };

const App = {
  components: { BenchView, ModelUseView, ModelStartView, ChatDrawer, GpuDialog, HelpDrawer, GameModal, UsersDrawer, PwdDialog },
  setup() {
    const { theme, themes, setTheme } = useTheme();
    const font = ref(localStorage.getItem("bench-font") || "1");
    // 字号缩放挂在 #app:三页共用;--app-zoom 反向折算保证不撑破视口
    function applyFont() {
      const el = document.getElementById("app");
      if (el) el.style.zoom = font.value;
      document.documentElement.style.setProperty("--app-zoom", font.value);
      localStorage.setItem("bench-font", font.value);
    }
    applyFont();

    // ---------- 账户:启动时校验会话,失效则回到登录页 ----------
    const authed = ref(null);                 // null=校验中 / true / false
    const loginForm = reactive({ username: "", password: "", loading: false, err: "" });
    const loginMode = ref("login");           // login | register
    const regForm = reactive({ username: "", password: "", confirm: "", loading: false, err: "", done: "" });
    onMounted(async () => {
      const u = await fetchMe();
      authed.value = !!u;
      if (u) {
        migrateLocalData();                   // 旧版本地数据一次性归入 admin
        // 从登录页「账户管理入口」进入:自动打开用户管理
        if (localStorage.getItem("open-users-drawer") === "1") {
          localStorage.removeItem("open-users-drawer");
          if (isAdmin() && users.value) setTimeout(() => users.value.open(), 300);
        }
      }
    });
    window.addEventListener("auth-required", () => { authed.value = false; });
    async function doLogin() {
      if (!loginForm.username.trim() || !loginForm.password) { loginForm.err = "请输入用户名和密码"; return; }
      loginForm.loading = true;
      loginForm.err = "";
      try {
        await login(loginForm.username.trim(), loginForm.password);
        location.reload();                    // 重载以让各 store 按新账号初始化
      } catch (e) {
        loginForm.err = e.message;
      }
      loginForm.loading = false;
    }
    // 账户管理入口:仅总管理员,登录成功后直接进入用户管理
    async function doLoginManage() {
      if (!loginForm.username.trim() || !loginForm.password) { loginForm.err = "请输入总管理员账号和密码"; return; }
      loginForm.loading = true;
      loginForm.err = "";
      try {
        const d = await login(loginForm.username.trim(), loginForm.password);
        if (!d.user || d.user.role !== "super") {
          await api("/api/auth/logout", { method: "POST" });   // 非总管理员:立即撤销会话
          loginForm.err = "账户管理入口仅限总管理员账号";
        } else {
          localStorage.setItem("open-users-drawer", "1");
          location.reload();
        }
      } catch (e) {
        loginForm.err = e.message;
      }
      loginForm.loading = false;
    }
    // ---------- 自助注册:提交后进入待审批,管理员通过后方可登录 ----------
    function toRegister() {
      loginMode.value = "register";
      regForm.username = loginForm.username; regForm.password = ""; regForm.confirm = "";
      regForm.err = ""; regForm.done = "";
    }
    function toLogin() { loginMode.value = "login"; loginForm.err = ""; }
    async function doRegister() {
      if (!regForm.username.trim() || !regForm.password) { regForm.err = "请填写用户名和密码"; return; }
      if (regForm.password.length < 6) { regForm.err = "密码至少 6 位"; return; }
      if (regForm.password !== regForm.confirm) { regForm.err = "两次输入的密码不一致"; return; }
      regForm.loading = true;
      regForm.err = "";
      try {
        const d = await register(regForm.username.trim(), regForm.password);
        regForm.done = d.msg || "注册申请已提交,待管理员审批";
      } catch (e) {
        regForm.err = e.message;
      }
      regForm.loading = false;
    }
    const users = ref(null);
    const pwd = ref(null);
    function onUserCmd(cmd) {
      if (cmd === "logout") logout();
      else if (cmd === "users" && users.value) users.value.open();
      else if (cmd === "pwd" && pwd.value) pwd.value.open();
    }

    // ---------- hash 路由:点击导航只切换下方页面,标题栏保持不变;子账号仅见有权限的模块 ----------
    const pages = computed(() => PAGES.filter(p => hasPerm(p.v)));
    const page = ref("bench");
    // 启动时尚未登录(pages 为空),登录态就绪后优先回到启动 hash 指定的页面,避免刷新后被重置回第一页
    let pendingHash = (location.hash || "").replace(/^#/, "");
    const fromHash = () => {
      const v = (location.hash || "").replace(/^#/, "");
      page.value = pages.value.some(p => p.v === v) ? v : (pages.value[0] || {}).v || "";
    };
    fromHash();
    window.addEventListener("hashchange", fromHash);
    watch(pages, () => {
      if (pendingHash) {                     // 登录态就绪:优先应用启动时的 hash
        const v = pendingHash; pendingHash = "";
        if (pages.value.some(p => p.v === v)) { page.value = v; location.hash = v; return; }
      }
      if (!pages.value.some(p => p.v === page.value)) page.value = (pages.value[0] || {}).v || "";
    });
    function go(v) {
      pendingHash = "";
      if (page.value !== v) page.value = v;
      if (location.hash !== "#" + v) location.hash = v;
    }
    const viewComp = computed(() => {
      const first = pages.value[0];
      return VIEW_COMPS[page.value] || (first && VIEW_COMPS[first.v]) || null;
    });

    // ---------- 左上角标题:品牌「雍宁工具链」固定,当前模块名随导航切换 ----------
    const curPage = computed(() => pages.value.find(p => p.v === page.value) || PAGES[0]);
    watch(curPage, p => { document.title = `雍宁工具链 · ${p.label}`; }, { immediate: true });

    const help = ref(null);
    const game = ref(null);
    const chatOpen = computed(() => C.open);
    return {
      PAGES, pages, page, go, viewComp, curPage,
      theme, themes, setTheme, font, applyFont,
      help, game, C, chatOpen, openGpu, toggleChat,
      AUTH, authed, loginForm, loginMode, regForm, doLogin, doLoginManage, toRegister, toLogin, doRegister,
      isAdmin, isViewer, onUserCmd, users, pwd,
    };
  },
  template: `
  <div v-if="authed === null" class="boot-loading">雍宁工具链启动中…</div>

  <!-- 登录页 -->
  <div v-else-if="!authed" class="login-page">
    <div class="login-card">
      <div class="login-head">
        <span class="logo-dot login-dot">雍</span>
        <div>
          <div class="login-title">雍宁工具链</div>
          <div class="login-sub">Yongning Toolchain · Benchmark / Gateway / Deploy</div>
        </div>
      </div>

      <!-- 登录 -->
      <template v-if="loginMode === 'login'">
        <el-input v-model="loginForm.username" size="large" placeholder="用户名" @keydown.enter="doLogin()"/>
        <el-input v-model="loginForm.password" size="large" type="password" show-password placeholder="密码"
                  style="margin-top:12px;" @keydown.enter="doLogin()"/>
        <div v-if="loginForm.err" class="login-err">✗ {{ loginForm.err }}</div>
        <div class="login-btns">
          <el-button type="primary" size="large" class="login-btn" :loading="loginForm.loading" @click="doLogin()">登 录</el-button>
          <el-button size="large" class="login-btn login-mgmt-btn" :loading="loginForm.loading"
                     title="输入总管理员账号后,点击直接进入用户管理" @click="doLoginManage()">👥 账户管理</el-button>
        </div>
        <div class="login-links">
          <span class="login-hint">默认总管理员 admin / admin123,登录后请立即修改密码</span>
          <a class="login-reg-link" @click="toRegister()">📝 注册账号</a>
        </div>
      </template>

      <!-- 自助注册:提交后待管理员审批 -->
      <template v-else>
        <el-input v-model="regForm.username" size="large" placeholder="用户名(2-24 位,可含中文/字母/数字/_/-)"/>
        <el-input v-model="regForm.password" size="large" type="password" show-password
                  placeholder="密码(至少 6 位)" style="margin-top:12px;"/>
        <el-input v-model="regForm.confirm" size="large" type="password" show-password
                  placeholder="确认密码" style="margin-top:12px;" @keydown.enter="doRegister()"/>
        <div v-if="regForm.err" class="login-err">✗ {{ regForm.err }}</div>
        <div v-if="regForm.done" class="login-ok">✓ {{ regForm.done }}</div>
        <el-button v-else type="primary" size="large" class="login-btn" :loading="regForm.loading" @click="doRegister()">提交注册</el-button>
        <div class="login-links">
          <span class="login-hint">注册申请需管理员审批通过后方可登录</span>
          <a class="login-reg-link" @click="toLogin()">← 返回登录</a>
        </div>
      </template>
    </div>
  </div>

  <!-- 主外壳 -->
  <div v-else class="spa-shell">
    <header class="app-header spa-header">
      <div class="app-logo">
        <span class="logo-dot">雍</span>
        <div>
          <div class="logo-line">雍宁工具链<span class="logo-sep">·</span><span class="logo-cur">{{ curPage.label }}</span><span class="logo-en">{{ curPage.en }}</span></div>
          <div class="logo-sub">Yongning Toolchain · LLM Benchmark / Gateway / Deploy</div>
        </div>
      </div>
      <nav class="spa-nav">
        <button v-for="p in pages" :key="p.v" class="spa-nav-btn" :class="{ on: page === p.v }"
                @click="go(p.v)">
          <span class="t">{{ p.label }}</span>
          <span class="en">{{ p.en }}</span>
        </button>
      </nav>
      <span v-if="isViewer()" class="spa-viewer-badge" title="预览账号:全站只读,仅可查看与使用对话功能">👁 预览模式(只读)</span>
      <span class="grow" :style="{ marginRight: chatOpen ? C.width + 'px' : '0', transition: 'margin-right .2s' }"></span>
      <div class="spa-actions">
        <el-button @click="openGpu()"><el-icon><Monitor/></el-icon>GPU监控</el-button>
        <el-button @click="game && game.open()"><el-icon><Trophy/></el-icon>小游戏</el-button>
        <el-button @click="help && help.toggle()"><el-icon><HelpFilled/></el-icon>帮助</el-button>
        <el-button :type="chatOpen ? 'primary' : ''" @click="toggleChat()"><el-icon><ChatDotRound/></el-icon>对话</el-button>
        <el-select v-model="font" class="font-select" title="字号" @change="applyFont">
          <el-option value="0.85" label="小"/>
          <el-option value="0.92" label="较小"/>
          <el-option value="1" label="标准"/>
          <el-option value="1.1" label="较大"/>
          <el-option value="1.25" label="大"/>
          <el-option value="1.5" label="特大"/>
        </el-select>
        <el-select :model-value="theme" class="theme-select" title="界面主题" @change="setTheme">
          <el-option v-for="t in themes" :key="t.id" :value="t.id" :label="t.label"/>
        </el-select>
        <span class="spa-user-sep"></span>
        <el-dropdown trigger="click" @command="onUserCmd">
          <button class="user-chip">
            <span class="usr-avatar">{{ (AUTH.user.username[0] || "?").toUpperCase() }}</span>
            <span class="uname">{{ AUTH.user.username }}</span>
            <span class="urole">{{ AUTH.user.role_label }}</span>
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="pwd"><el-icon><Key/></el-icon>修改密码</el-dropdown-item>
              <el-dropdown-item v-if="isAdmin()" command="users"><el-icon><User/></el-icon>用户管理</el-dropdown-item>
              <el-dropdown-item command="logout" divided><el-icon><SwitchButton/></el-icon>退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>
    <div v-if="!pages.length" class="spa-body">
      <div class="no-perm-panel">
        <div class="np-ico">🔒</div>
        <div class="np-t">当前账号未开通任何功能模块</div>
        <div class="np-d">请联系总管理员在「用户管理」中为该账号分配功能权限(压测工作台 / 模型工作台 / 模型部署站 / 主机分组管理 / 网关渠道密钥)</div>
      </div>
    </div>
    <div v-else class="spa-body">
      <KeepAlive><component :is="viewComp"/></KeepAlive>
    </div>
    <HelpDrawer ref="help"/>
    <GameModal ref="game"/>
    <ChatDrawer/>
    <GpuDialog/>
    <UsersDrawer ref="users"/>
    <PwdDialog ref="pwd"/>
  </div>`,
};

const app = createApp(App);
sweepStuckOverlays();
app.config.errorHandler = (err, inst, info) => {
  console.error("Vue error:", err, info);
  window.__vueErrors = (window.__vueErrors || []).concat([`${err} | ${info}`]);
};
app.use(ElementPlus, { locale: zhCn });
// 图标包需异步取回并桥接 ESM Vue(见 icons.js),注册完成后再挂载,避免首帧图标空缺
registerIcons(app).finally(() => app.mount("#app"));
