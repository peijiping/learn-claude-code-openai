import { useEffect, type PointerEvent as ReactPointerEvent } from 'react'
import { useAgentStore } from '@store/agentStore'
import {
  RPANEL_MIN_WIDTH,
  clampWidth,
  persistWidth,
  useRightPanelStore
} from '@store/rightPanelStore'
import { DEFAULT_RPANEL, activeOrDefault } from '@lib/rpanelTabs'
import ChangesPanel from './ChangesPanel'
import FilePreview from './FilePreview'
import FileTree from './FileTree'
import RPanelState from './RPanelState'
import RPanelTabBar from './RPanelTabBar'
import RPanelWelcome from './RPanelWelcome'

/**
 * 右侧面板容器（19 篇 §5.1/§5.2）。
 *
 * ════════════════════════════════════════════════════════════════════
 * 两条核心约束（这个组件存在的理由）
 * ════════════════════════════════════════════════════════════════════
 * 1. **按会话隔离**：整个右栏跟着会话走 —— 一个会话一份实例（开合 + 全部标签 +
 *    当前激活）。状态存在会话元数据的 `right_panel` 字段里（不是 localStorage），
 *    随 `session_history` 回传恢复。所以本组件的每个 store 调用都**显式带 `sid`**，
 *    从不读"当前是哪个会话"（那会在防抖窗口里写错会话，19 篇 §2.5）。
 * 2. **单栏混放**：标签栏里既有视图标签（文件/变更/终端/浏览器）也有文件标签，
 *    后者分"预览位 / 常驻位"两种身份（细节在 `RPanelTabBar`）。
 *
 * 新建任务态（`activeSession === null`）**整个右栏不渲染** —— 那时还没有会话，
 * 也就没有"这个会话的右栏"可言。
 */
export default function RightPanel(): JSX.Element | null {
  const activeSession = useAgentStore((s) => s.activeSession)
  const askOpen = useAgentStore((s) => {
    if (!s.activeSession) return false
    const it = s.interactionBySession[s.activeSession]
    return !!it && it.questions.length > 0
  })
  const approvalPending = useAgentStore((s) =>
    s.activeSession ? Object.keys(s.approvalBySession[s.activeSession] ?? {}).length > 0 : false
  )

  const sid = activeSession ?? ''
  // 选择器返回 `undefined`（该会话从没用过右栏）—— 引用稳定，不会触发无限重渲染
  const stored = useRightPanelStore((s) => s.layoutBySession[sid])
  const layout = stored ?? DEFAULT_RPANEL
  const width = useRightPanelStore((s) => s.width)
  const resizing = useRightPanelStore((s) => s.resizing)

  const setWidth = useRightPanelStore((s) => s.setWidth)
  const setResizing = useRightPanelStore((s) => s.setResizing)
  const setPanelOpen = useRightPanelStore((s) => s.setPanelOpen)
  const togglePanel = useRightPanelStore((s) => s.togglePanel)
  const openViewTab = useRightPanelStore((s) => s.openViewTab)
  const ensureTree = useRightPanelStore((s) => s.ensureTree)
  const ensureGit = useRightPanelStore((s) => s.ensureGit)

  const activeKey = activeOrDefault(layout)
  const activeTab = layout.tabs.find((t) => (t.kind === 'view' ? `view:${t.view}` : `file:${t.path}`) === activeKey)

  const open = !!activeSession && layout.open

  // ── 打开着的视图标签 → 按需取数 ──────────────────────────────
  // **只做视图标签**：文件标签的预览由 `FilePreview` 自己懒读（它按 path 变化触发，
  // 与"哪个标签激活"是同一件事，分两处发请求只会加倍）。切会话时 `liveBySession`
  // 里该会话的桶已被 `keepOnly` 丢弃，所以这里必须能重新拉（`ensureXxx` 都是幂等的）。
  useEffect(() => {
    if (!sid || !layout.open || !activeTab) return
    if (activeTab.kind !== 'view') return
    if (activeTab.view === 'files') ensureTree(sid)
    else if (activeTab.view === 'changes') ensureGit(sid)
  }, [sid, layout.open, activeKey, activeTab, ensureTree, ensureGit])

  // ── 拖拽期间的全局态（禁选中 + 光标）─────────────────────────
  useEffect(() => {
    if (!resizing) return
    document.body.classList.add('is-resizing')
    return () => document.body.classList.remove('is-resizing')
  }, [resizing])

  // ── 窗口变窄时重新 clamp（右栏不能把聊天区挤成 0）────────────
  useEffect(() => {
    if (!open) return
    const onResize = (): void => {
      const cur = useRightPanelStore.getState().width
      const next = clampWidth(cur)
      if (next !== cur) useRightPanelStore.getState().setWidth(next)
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [open])

  // ── 视图直达快捷键（⌘⇧E = 文件 / ⌘⇧G = 变更）───────────────────
  // 与标题栏那枚开关图标**刻意不同义**（2026-09-23）：图标只管"这一栏要不要占屏幕"
  // （`setPanelOpen`，不动 `tabs`/`active`），这两个键从键盘**直达某个视图**
  // （`togglePanel`，该视图已激活时再按 = 收栏 —— 顺带提供键盘关闭路径）。
  //
  // 放在本组件而不是标题栏：它们读的是 store 的**视图语义**，与下面的 Esc 同属
  // "右栏的键盘面"，就近维护。**本组件在右栏关着时也挂载**（App 里无条件渲染），
  // 所以这个监听器任何时候都在 —— 这也正是"⌘⇧E 能把关着的右栏叫起来"的前提。
  // 新建任务态下空转（不报错、不弹提示）：那时根本没有"这个会话的右栏"。
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (!(e.metaKey || e.ctrlKey) || !e.shiftKey) return
      const key = e.key.toLowerCase()
      if (key !== 'e' && key !== 'g') return
      if (!sid) return
      e.preventDefault()
      togglePanel(sid, key === 'e' ? 'files' : 'changes')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sid, togglePanel])

  // ── Esc 层级（19 篇 §6 第 4 条）──────────────────────────────
  // ① 「+」/右键菜单（它们自己处理并 stopPropagation）→ ② 还原最大化预览（后置）
  // → ③ 收起右栏 → ④ 不处理。
  //
  // ⚠️ 第 ① 步必须在这里**再兜一次**（`addMenuOpen` 直接 return）：
  // document 上的监听器按注册顺序触发，本组件的监听器先于菜单注册，
  // 只靠菜单的 stopPropagation 挡不住 —— 同一次 Esc 会把右栏一起收掉。
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== 'Escape') return
      // 在途提问 / 审批期间右栏不响应（判据与 ChatPanel 同源）：
      // 那时 Esc 的语义已经被"作答面板"占用，再收右栏会让用户丢上下文。
      if (askOpen || approvalPending) return
      if (useRightPanelStore.getState().addMenuOpen) return
      const el = document.activeElement as HTMLElement | null
      // 焦点在输入类元素里时不收右栏（用户在打字，Esc 可能有别的含义）
      if (el && (el.isContentEditable || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) return
      setPanelOpen(sid, false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, askOpen, approvalPending, sid, setPanelOpen])

  if (!open) return null

  // ── 拖拽调宽（Pointer Events + setPointerCapture）─────────────
  // 不用 `window.mousemove`：鼠标移出窗口或落到 iframe 上会丢 `pointerup`，
  // 表现为"松手后还在跟着鼠标变宽"。捕获后事件全部回到本元素。
  const onResizerDown = (e: ReactPointerEvent<HTMLDivElement>): void => {
    e.preventDefault()
    const el = e.currentTarget
    const startX = e.clientX
    const startW = useRightPanelStore.getState().width
    el.setPointerCapture(e.pointerId)
    setResizing(true)

    const onMove = (ev: PointerEvent): void => {
      // 向左拖 = 变宽
      setWidth(startW + (startX - ev.clientX))
    }
    const onUp = (ev: PointerEvent): void => {
      el.releasePointerCapture?.(ev.pointerId)
      el.removeEventListener('pointermove', onMove)
      el.removeEventListener('pointerup', onUp)
      el.removeEventListener('pointercancel', onUp)
      setResizing(false)
      // **松手才落 localStorage**：拖拽过程中每帧写会让拖动发涩
      persistWidth(useRightPanelStore.getState().width)
    }
    el.addEventListener('pointermove', onMove)
    el.addEventListener('pointerup', onUp)
    el.addEventListener('pointercancel', onUp)
  }

  // 树里要高亮的那个文件 = **最后一个文件标签**（标签栏末位 = 最近打开）。
  // 哪怕此刻激活的是「文件」视图标签，也该高亮它 —— 它就是用户刚才从树里点开/从会话
  // 里跳过来的目标，"你刚才点的是这个"比"当前哪个标签是激活的"更有用。
  let activePath: string | null = null
  for (let i = layout.tabs.length - 1; i >= 0; i -= 1) {
    const t = layout.tabs[i]
    if (t.kind === 'file') {
      activePath = t.path
      break
    }
  }

  const body = (): JSX.Element => {
    if (!layout.tabs.length || !activeTab) {
      return <RPanelWelcome onPick={(v) => openViewTab(sid, v)} />
    }
    if (activeTab.kind === 'file') {
      return <FilePreview sid={sid} path={activeTab.path} name={activeTab.name} />
    }
    if (activeTab.view === 'files') return <FileTree sid={sid} activePath={activePath} />
    if (activeTab.view === 'changes') return <ChangesPanel sid={sid} />
    // 终端 / 浏览器：第二期。本期这两个标签**加不出来**（「+」菜单与欢迎态都是
    // disabled），这里只是脏数据兜底 —— 旧版本的 meta 里可能有它们。
    return <RPanelState kind="empty" icon="clock" title="该功能将在第二期提供" />
  }

  return (
    <aside
      className="rpanel"
      style={{ width }}
      aria-label="右侧面板"
    >
      <div
        className={`rpanel-resizer${resizing ? ' dragging' : ''}${width <= RPANEL_MIN_WIDTH ? ' at-min' : ''}`}
        role="separator"
        aria-orientation="vertical"
        title="拖动调整宽度，双击复位"
        onPointerDown={onResizerDown}
        onDoubleClick={() => useRightPanelStore.getState().resetWidth()}
      />
      <div className="rpanel-inner">
        <RPanelTabBar sid={sid} layout={layout} />
        <div className="rpanel-body">{body()}</div>
      </div>
    </aside>
  )
}
