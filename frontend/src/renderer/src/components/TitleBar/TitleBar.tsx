import { useAgentStore } from '@store/agentStore'
import { useRightPanelStore } from '@store/rightPanelStore'
import { HAS_TITLEBAR_OVERLAY } from '@lib/platform'
import { Icon } from '@components/common/Icon'

/**
 * 应用名 = 窗口标题。
 * ⚠️ 与 `src/renderer/index.html` 的 `<title>`、主进程 `app.setName()` 是**三处**
 * 同一个字符串（原生标题栏被隐去后，可见的那份由这里渲染，另两处是系统展示位：
 * 菜单栏 / Dock / Cmd+Tab / 任务栏）。改名要三处一起改。
 */
const APP_NAME = '个人AI助手'

/**
 * 窗口标题栏（自绘，2026-09-23）。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么存在
 * ════════════════════════════════════════════════════════════════════
 * 右栏开关的落点从「聊天区顶栏」挪到了**窗口最外层**——与窗口标题同一层级。
 * 原生标题栏画不了自绘控件，所以主进程改用 `titleBarStyle: 'hidden'`
 * （`src/main/index.ts`），标题文字与拖拽区改由本组件承担。
 *
 * 三道硬约束（改之前先读 layout.css 的同名注释）：
 * 1. **拖拽区只落在 `.titlebar-drag` 填充块上**，不是整条栏；开关按钮的 26×26 内
 *    **不得有任何 drag 矩形**（几何不变式，见 layout.css 与 19 篇 §7.3.1）；
 * 2. **标题居中**、底色左灰右白两段，与下面的侧边栏 / 聊天区对齐（原生观感）；
 * 3. 开关按钮的语义是**纯开合取反**（`togglePanelOpen` —— 在 store 里读当前值取反，
 *    不是组件里 `!渲染时的布尔`）—— 视图切换仍走 `⌘⇧E`/`⌘⇧G`（快捷键**在
 *    RightPanel 里**，不在这里：它读的是 store 的视图语义，与 Esc 同属"右栏的键盘面"）。
 *
 * 新建任务态（`activeSession === null`）下**不渲染按钮**：那时还没有会话，
 * 也就没有"这个会话的右栏"可言。标题本身恒在（它是窗口的属性，不是会话的属性）。
 */
export default function TitleBar(): JSX.Element {
  const activeSession = useAgentStore((s) => s.activeSession)
  const sid = activeSession ?? ''
  // 选择器只返回布尔原始值（`!!`）—— 直接返回对象会让 zustand 每次渲染都判定"变了"。
  const panelOpen = useRightPanelStore((s) => !!s.layoutBySession[sid]?.open)
  const togglePanelOpen = useRightPanelStore((s) => s.togglePanelOpen)

  return (
    <header className={`titlebar${HAS_TITLEBAR_OVERLAY ? ' titlebar--overlay' : ''}`}>
      {/* 拖拽区不是"整条栏"，而是三块**填充**：左翼 / 标题 / 开关左侧的空白。
          这样"可拖矩形"与"开关按钮"在几何上永不重叠（见 layout.css 的说明）。 */}
      <div className="titlebar-drag titlebar-drag--left" aria-hidden="true" />
      <div className="titlebar-title" title={APP_NAME}>
        {APP_NAME}
      </div>
      <div className="titlebar-right">
        <div className="titlebar-drag titlebar-drag--flex" aria-hidden="true" />
        {activeSession ? (
          <button
            className="titlebar-toggle"
            aria-pressed={panelOpen}
            aria-label={panelOpen ? '收起右侧面板' : '打开右侧面板'}
            title={panelOpen ? '收起右侧面板' : '打开右侧面板'}
            onClick={() => togglePanelOpen(sid)}
          >
            <Icon name="panelRight" size={15} />
          </button>
        ) : null}
      </div>
    </header>
  )
}
