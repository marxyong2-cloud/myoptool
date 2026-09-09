// ============================== API 请求与剪贴板 ==============================
import { ElMessage } from "element-plus";

export async function api(path, opts = {}) {
  const resp = await fetch(path, { cache: "no-store", ...opts });
  let data = null;
  try { data = await resp.json(); } catch {}
  if (resp.status === 401 && !path.startsWith("/api/auth/")) {
    window.dispatchEvent(new CustomEvent("auth-required"));   // 会话失效 → 外壳切回登录页
  }
  if (!resp.ok) throw new Error((data && (data.detail || data.message)) || `HTTP ${resp.status}`);
  return data;
}

export async function copyText(text, tip) {
  try {
    await navigator.clipboard.writeText(text);
    ElMessage.success(tip || "已复制");
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); ElMessage.success(tip || "已复制"); }
    catch { ElMessage.error("复制失败"); }
    ta.remove();
  }
}

// 文件上传(单文件,返回 {name,url,size,type} 或抛错)
export async function uploadFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/chat/upload", { method: "POST", body: fd });
  const d = await r.json();
  if (d.name === undefined) throw new Error(d.detail || "上传失败");
  return d;
}
