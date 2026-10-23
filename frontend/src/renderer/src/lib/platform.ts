/**
 * 宿主平台判定（**只服务于窗口外框**，2026-09-23）。
 *
 * 为什么用 UA 而不是让主进程下发 `process.platform`：渲染层只有**一处**需要它 ——
 * 自绘标题栏在 Windows/Linux 上必须给 `titleBarOverlay` 的系统按钮（最小化/最大化/
 * 关闭）留白，而 macOS 的交通灯在**左边**、无此需求。为一处纯样式分支新增一个跨进程
 * API（preload + preload/index.d.ts + 浏览器回退桥三处同步，且回退桥是
 * `implements AgentApi` 的，漏一处就 typecheck 红）不划算；UA 在 Electron 与普通浏览器
 * 两个宿主里都拿得到，且这层判定不会随业务演进。
 *
 * ⚠️ 若将来平台信息要参与**行为**（不只是留白），改成主进程显式下发，别继续扩 UA 判据。
 */

/** macOS（交通灯在左上角，标题栏左右留白对称） */
export const IS_MAC = /Macintosh|Mac OS X/i.test(navigator.userAgent)

/** Windows / Linux（`titleBarOverlay` 的系统按钮压在本层右上角，需留位） */
export const HAS_TITLEBAR_OVERLAY = !IS_MAC && /Windows|Linux/i.test(navigator.userAgent)
