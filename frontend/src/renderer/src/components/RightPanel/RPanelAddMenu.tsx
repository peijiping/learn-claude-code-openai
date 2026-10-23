import { useEffect, useRef } from 'react'
import { Icon } from '@components/common/Icon'
import { RPANEL_AVAILABLE_VIEWS, RPANEL_VIEW_LABELS, type RPanelView } from '@protocols/agentProtocol'
import { RPANEL_VIEW_ORDER } from '@lib/rpanelTabs'
import { RPANEL_VIEW_ICONS } from './viewMeta'

interface RPanelAddMenuProps {
  /** 已打开的视图标签（右侧打 ✓；点击 = 激活已有那枚，不新增） */
  openViews: RPanelView[]
  /** 触发按钮的视口矩形（null = 拿不到，回落到右上角） */
  anchor: { right: number; bottom: number } | null
  onPick: (view: RPanelView) => void
  onClose: () => void
}

const MENU_WIDTH = 168
/** 4 项 × 约 30px + 上下内边距 —— 用于"向上还是向下弹"的判断 */
const MENU_HEIGHT = 136

/**
 * 标签栏栏尾「+」的下拉菜单（19 篇 §5.4）。
 *
 * 三个复用与一个刻意不做的选择：
 * - 复用 `.session-menu` 的**视觉**（`.rpanel-menu` 只覆盖 min-width）与
 *   `MessageMenu.tsx` 的 **click-outside / Esc 写法** —— 项目里已有三种下拉
 *   （model-menu / ws-picker / session-menu），形状必须看起来是同一个东西；
 * - 刻意**不做**键盘上下选：本菜单是"点一下就走"的轻交互，
 *   加一套 roving focus 会让 `Esc` 的归属变得含糊（它同时是"关菜单"和
 *   "收右栏"的候选，19 篇 §6 第 4 条已经定了层级，多一层就说不清了）。
 */
export default function RPanelAddMenu({
  openViews,
  anchor,
  onPick,
  onClose
}: RPanelAddMenuProps): JSX.Element {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onDocMouseDown = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onDocContextMenu = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        // 只关菜单：**stopPropagation** 是必须的 —— 否则事件继续冒泡到
        // RightPanel 的 Esc 处理，同一次按键会连右栏一起收掉。
        e.stopPropagation()
        onClose()
      }
    }
    document.addEventListener('mousedown', onDocMouseDown)
    document.addEventListener('contextmenu', onDocContextMenu)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      document.removeEventListener('contextmenu', onDocContextMenu)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  // 默认向右对齐到触发按钮右缘（右栏在窗口右边，向左展开才不会被裁掉）
  const right = anchor?.right ?? window.innerWidth - 8
  const left = Math.max(8, Math.min(right - MENU_WIDTH, window.innerWidth - MENU_WIDTH - 8))
  const top = anchor
    ? Math.min(anchor.bottom + 4, Math.max(8, window.innerHeight - MENU_HEIGHT - 8))
    : 8

  return (
    <div className="session-menu rpanel-menu" style={{ left, top }} ref={ref}>
      {RPANEL_VIEW_ORDER.map((view) => {
        const available = RPANEL_AVAILABLE_VIEWS.includes(view)
        const opened = openViews.includes(view)
        return (
          <button
            key={view}
            className="session-menu-item rpanel-menu-item"
            disabled={!available}
            onClick={() => {
              onPick(view)
              onClose()
            }}
          >
            <Icon name={RPANEL_VIEW_ICONS[view]} size={14} />
            <span className="rpanel-menu-label">{RPANEL_VIEW_LABELS[view]}</span>
            {!available ? (
              <span className="rpanel-menu-badge">第二期</span>
            ) : opened ? (
              <span className="rpanel-menu-check">
                <Icon name="check" size={13} />
              </span>
            ) : null}
          </button>
        )
      })}
    </div>
  )
}
