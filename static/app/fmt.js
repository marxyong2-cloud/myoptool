// ============================== 格式化与转义工具 ==============================
export const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export function fmtSize(n) {
  if (!n) return "";
  return n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(2) + " MB";
}

export function fmtNum(n) { return (n ?? 0).toLocaleString("zh-CN"); }

// 附件大小(紧凑格式:B/KB/MB)
export function fmtAttBytes(b) {
  if (b >= 1048576) return (b / 1048576).toFixed(1) + "MB";
  if (b >= 1024) return Math.round(b / 1024) + "KB";
  return (b || 0) + "B";
}

// 时间码 00:03.2
export function stTc(t) {
  if (!isFinite(t) || t < 0) t = 0;
  const m = Math.floor(t / 60);
  return String(m).padStart(2, "0") + ":" + (t - m * 60).toFixed(1).padStart(4, "0");
}

export function fmtPct(x, digits = 1) {
  return (x * 100).toFixed(digits) + "%";
}
