// ============================== Element Plus 图标注册(IIFE 图标包 + ESM Vue 桥接) ==============================
// icons.iife.min.js 是面向 <script> 全局构建的 IIFE 包,内部引用全局 `Vue`;
// 本项目以 ESM 方式加载 Vue(不存在 window.Vue),若在 <head> 直接引入该包会在执行中途抛
// ReferenceError,导致 window.ElementPlusIconsVue 保持 undefined、所有 <el-icon> 图标渲染为空。
// 这里取回脚本文本后,在注入 Vue 命名空间的函数作用域内执行,再全局注册图标组件。
import * as Vue from "vue";

const ICONS_SRC = "/static/vendor/icons.iife.min.js";

async function loadIcons() {
  if (window.ElementPlusIconsVue && Object.keys(window.ElementPlusIconsVue).length) {
    return window.ElementPlusIconsVue;          // 页面 <head> 直接引入成功时(全局构建 Vue)直接复用
  }
  const text = await (await fetch(ICONS_SRC)).text();
  // 在函数作用域内以参数 Vue 执行 IIFE 包,不污染 window.Vue
  return new Function("Vue", `${text}\n;return ElementPlusIconsVue;`)(Vue);
}

export async function registerIcons(app) {
  let icons = {};
  try {
    icons = await loadIcons() || {};
  } catch (e) {
    console.error("Element Plus 图标包加载失败:", e);
  }
  for (const name of Object.keys(icons)) app.component(name, icons[name]);
}
