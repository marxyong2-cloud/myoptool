// ============================== 账户会话:登录态 / 当前用户 / 用户管理 API ==============================
import { reactive } from "vue";
import { api } from "./api.js";

const JSON_HDR = { "Content-Type": "application/json" };

export const AUTH = reactive({
  user: null,               // {username, role, role_label, hosts, created, perms, status}
});

// 账号角色层级(惯例五级):总管理员 > 管理员 > 子账号 > 预览用户;(待审批为自助注册过渡状态)
export const ROLE_LABEL = { super: "总管理员", admin: "管理员", sub: "子账号", viewer: "预览用户" };

export const isAdmin = () => !!AUTH.user && (AUTH.user.role === "super" || AUTH.user.role === "admin");
export const isSuper = () => !!AUTH.user && AUTH.user.role === "super";
export const isViewer = () => !!AUTH.user && AUTH.user.role === "viewer";

// ---------- 功能模块权限(管理员天然全有;子账号按被分配的 perms;预览用户仅可看三个页面) ----------
export const PERM_KEYS = ["bench", "modeluse", "modelstart", "ssh_hosts", "gateway"];
export const PERM_LABEL = {
  bench: "压测工作台", modeluse: "模型工作台", modelstart: "模型部署站",
  ssh_hosts: "部署站 SSH 主机分组管理", gateway: "工作台网关渠道 / 密钥",
};
const VIEWER_PAGES = ["bench", "modeluse", "modelstart"];   // 预览用户可见(只读)的页面
export function hasPerm(mod) {
  if (!AUTH.user) return false;
  const r = AUTH.user.role;
  if (r === "super" || r === "admin") return true;
  if (r === "viewer") return VIEWER_PAGES.includes(mod);
  return (AUTH.user.perms || []).includes(mod);
}
// 部署站主机池管理:管理员或被授予 ssh_hosts 权限的子账号(预览用户只读)
export const canManageHosts = () => isAdmin() || hasPerm("ssh_hosts");
// 网关渠道/密钥为管理员共享配置:子账号即便有 gateway 权限也仅可查看
export const canManageGateway = () => isAdmin();

export async function fetchMe() {
  try {
    const d = await api("/api/auth/me");
    AUTH.user = d;
    window.__USER = d.username;
    return d;
  } catch {
    AUTH.user = null;
    window.__USER = "";
    return null;
  }
}

export async function login(username, password) {
  return api("/api/auth/login", { method: "POST", headers: JSON_HDR, body: JSON.stringify({ username, password }) });
}

export async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch {}
  location.reload();
}

export async function changePassword(oldPassword, newPassword) {
  return api("/api/auth/password", { method: "POST", headers: JSON_HDR, body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }) });
}

// ---------- 用户管理(管理员) ----------
export async function loadUsers() { return api("/api/users"); }
export async function createUser(body) { return api("/api/users", { method: "POST", headers: JSON_HDR, body: JSON.stringify(body) }); }
export async function updateUser(name, body) { return api(`/api/users/${encodeURIComponent(name)}`, { method: "POST", headers: JSON_HDR, body: JSON.stringify(body) }); }
export async function deleteUser(name) { return api(`/api/users/${encodeURIComponent(name)}`, { method: "DELETE" }); }
export async function approveUser(name, approve, perms) {
  return api(`/api/users/${encodeURIComponent(name)}/approve`, { method: "POST", headers: JSON_HDR, body: JSON.stringify({ approve: !!approve, perms: perms || [] }) });
}
export async function register(username, password) {
  return api("/api/auth/register", { method: "POST", headers: JSON_HDR, body: JSON.stringify({ username, password }) });
}
export async function assignHost(hostId, users) {
  return api("/api/modelstart/hosts-assign", { method: "POST", headers: JSON_HDR, body: JSON.stringify({ host_id: hostId, users }) });
}

// ---------- 本地数据按账户隔离:API 保存记录等键名加用户前缀 ----------
export const lsKey = (k) => (window.__USER ? window.__USER + ":" + k : k);

// 旧版(无账户体系)浏览器本地数据归入 admin:首次以 admin 登录时一次性迁移
export function migrateLocalData() {
  if (window.__USER !== "admin") return;
  for (const k of ["bench-cfg", "chat-saved-apis", "mu-cfg"]) {
    const oldV = localStorage.getItem(k);
    if (oldV !== null && localStorage.getItem("admin:" + k) === null) {
      localStorage.setItem("admin:" + k, oldV);
    }
  }
}
