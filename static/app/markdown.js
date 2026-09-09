// ============================== 轻量 Markdown 渲染器(与旧版一致) ==============================
import { esc } from "./fmt.js";

// 结果缓存:历史消息 / AI 分析在列表重渲时不再重复解析(Vue 重渲染会重跑模板表达式)
const _mdCache = new Map();
const MD_CACHE_MAX = 300;

export function renderMarkdown(md) {
  const key = String(md || "");
  const hit = _mdCache.get(key);
  if (hit !== undefined) return hit;
  const html = _renderMarkdown(key);
  if (_mdCache.size >= MD_CACHE_MAX) _mdCache.delete(_mdCache.keys().next().value);
  _mdCache.set(key, html);
  return html;
}

function _renderMarkdown(md) {
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener" class="md-link">$1</a>');
  const lines = String(md || "").split("\n");
  let html = "", inCode = false, inList = false, inTable = false;
  let inDetails = false, detailsBuf = [];
  for (const line of lines) {
    const trimmed = line.trim();
    // <details> 折叠块:收集内部 markdown,结束时递归渲染(README 部署手册等使用)
    if (trimmed === "<details>") {
      if (inList) { html += "</ul>"; inList = false; }
      if (inTable) { html += "</table>"; inTable = false; }
      inDetails = true; detailsBuf = [];
      continue;
    }
    if (trimmed.startsWith("<summary>") && inDetails) {
      detailsBuf.unshift(trimmed.replace(/<\/?summary>/g, ""));
      continue;
    }
    if (trimmed === "</details>") {
      if (inDetails) {
        // 标题剥掉内联 HTML 标签(如 README 的 <strong>)只留文字,再走行内格式化(先转义,防注入)
        const title = String(detailsBuf.shift() || "展开").replace(/<\/?[a-zA-Z][^>]*>/g, "");
        html += `<details class="md-fold"><summary>${inline(title)}</summary><div class="md-fold-body">${_renderMarkdown(detailsBuf.join("\n"))}</div></details>`;
        inDetails = false; detailsBuf = [];
      }
      continue;
    }
    if (inDetails) { detailsBuf.push(line); continue; }
    if (trimmed.startsWith("```")) {
      if (inCode) { html += "</code></pre>"; inCode = false; }
      else { if (inList) { html += "</ul>"; inList = false; } if (inTable) { html += "</table>"; inTable = false; } html += "<pre><code>"; inCode = true; }
      continue;
    }
    if (inCode) { html += esc(line) + "\n"; continue; }
    if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
      if (inList) { html += "</ul>"; inList = false; }
      const cells = trimmed.slice(1, -1).split("|").map(c => c.trim());
      if (cells.every(c => /^:?-+:?$/.test(c))) continue;
      if (!inTable) { html += "<table>"; inTable = true; }
      const tag = html.endsWith("<table>") ? "th" : "td";
      html += "<tr>" + cells.map(c => `<${tag}>${inline(c)}</${tag}>`).join("") + "</tr>";
      continue;
    } else if (inTable) { html += "</table>"; inTable = false; }
    const h = trimmed.match(/^(#{1,4})\s+(.*)/);
    if (h) { if (inList) { html += "</ul>"; inList = false; } const lv = h[1].length; html += `<h${lv}>${inline(h[2])}</h${lv}>`; continue; }
    if (/^>\s?/.test(trimmed)) { if (inList) { html += "</ul>"; inList = false; } html += `<blockquote>${inline(trimmed.replace(/^>\s?/, ""))}</blockquote>`; continue; }
    if (/^\s*[-*]\s+/.test(trimmed) || /^\s*\d+\.\s+/.test(trimmed)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(trimmed.replace(/^\s*(?:[-*]|\d+\.)\s+/, ""))}</li>`;
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    if (trimmed) html += `<p>${inline(trimmed)}</p>`;
  }
  if (inCode) html += "</code></pre>";
  if (inList) html += "</ul>";
  if (inTable) html += "</table>";
  if (inDetails && detailsBuf.length) {   // 未闭合的 details 兜底渲染
    html += _renderMarkdown(detailsBuf.join("\n"));
  }
  return html;
}

// 生成 96px 缩略图(图片直接绘制;视频取首帧) — 存入消息历史,避免数据膨胀
export function makeThumb(att) {
  return new Promise(res => {
    try {
      if (att.type === "image") {
        const img = new Image();
        img.onload = () => {
          const c = document.createElement("canvas");
          const s = 96 / Math.max(img.width, img.height, 1);
          c.width = Math.max(1, Math.round(img.width * s));
          c.height = Math.max(1, Math.round(img.height * s));
          c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
          res(c.toDataURL("image/jpeg", 0.75));
        };
        img.onerror = () => res("");
        img.src = att.content;
      } else if (att.type === "video") {
        const v = document.createElement("video");
        v.muted = true; v.playsInline = true; v.preload = "metadata";
        const done = () => {
          try {
            const c = document.createElement("canvas");
            const s = 96 / Math.max(v.videoWidth, v.videoHeight, 1);
            c.width = Math.max(1, Math.round(v.videoWidth * s));
            c.height = Math.max(1, Math.round(v.videoHeight * s));
            c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
            res(c.toDataURL("image/jpeg", 0.75));
          } catch { res(""); }
        };
        v.onloadeddata = () => { try { v.currentTime = Math.min(0.3, (v.duration || 1) / 10); } catch { done(); } };
        v.onseeked = done;
        v.onerror = () => res("");
        v.src = att.content;
      } else res("");
    } catch { res(""); }
  });
}
