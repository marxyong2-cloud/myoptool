// ============================== Element Plus 弹层关闭动画卡死清理 ==============================
// 标签页后台化时 transitionend 可能不触发,已关闭的 ElDialog / ElMessageBox
// 会残留为 opacity:0 的全屏遮罩(仍拦截鼠标,页面看起来"卡死")。
// 定期清扫这类僵尸遮罩,恢复正常交互。
export function sweepStuckOverlays() {
  if (typeof document === "undefined") return;
  setInterval(() => {
    const now = Date.now();
    let visible = 0;
    document.querySelectorAll(".el-overlay").forEach(o => {
      if (getComputedStyle(o).opacity !== "0") {
        delete o.dataset.sweep0;
        visible++;
        return;
      }
      if (!o.dataset.sweep0) { o.dataset.sweep0 = String(now); return; }
      if (now - Number(o.dataset.sweep0) > 1500) o.remove();   // 透明超过 1.5s = 关闭动画已死
    });
    // 无可见弹层时解除 body 滚动锁(同样可能因动画卡死而残留)
    if (!visible && document.body.classList.contains("el-popup-parent--hidden")) {
      document.body.classList.remove("el-popup-parent--hidden");
    }
  }, 2000);
}
