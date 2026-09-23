import { useEffect, useRef, useState } from 'react'
import { Icon } from '@components/common/Icon'
import {
  RPANEL_VIEW_LABELS,
  type RPanelPersist,
  type RPanelTab,
  type RPanelView
} from '@protocols/agentProtocol'
import { activeOrDefault, tabKey } from '@lib/rpanelTabs'
import { useRightPanelStore } from '@store/rightPanelStore'
import RPanelAddMenu from './RPanelAddMenu'
import { RPANEL_VIEW_ICONS } from './viewMeta'

interface RPanelTabBarProps {
  sid: string
  layout: RPanelPersist
}

/**
 * 单栏混放的标签栏（19 篇 §5.3）—— 本期最核心的一块 UI。
 *
 * 一条栏里同时住着**视图标签**（文件/变更/终端/浏览器，用栏尾「+」添加，每类至多一枚）
 * 与**文件标签**（一个文件一枚，带"预览位/常驻位"两种身份）。
 * 刻意**不是**固定五项常驻 —— 用户要的是"按需打开的标签"，不是"永远占着一排"。
 *
 * 两种身份的可视差别（只靠这一处区分，别再加第三种信号）：
 * | | 预览位（`pinned:false`） | 常驻位（`pinned:true`） |
 * | --- | --- | --- |
 * | 文字 | **斜体** | 正常字重 |
 * | 来源 | 一枚 `--color-accent` 圆点 | 无 |
 * | 关闭键 | 不提供（它是"被顶替即消失"的临时物） | hover 显现 |
 *
 * 标签文案**必须显示文件名**（`name`，与输入区 `@` 胶囊同口径），
 * `title` 挂完整绝对路径 —— 只显示路径会让窄栏里的标签全变成 `/Users/…`。
 */
export default function RPanelTabBar({ sid, layout }: RPanelTabBarProps): JSX.Element {
  const addMenuOpen = useRightPanelStore((s) => s.addMenuOpen)
  const setAddMenuOpen = useRightPanelStore((s) => s.setAddMenuOpen)
  const openViewTab = useRightPanelStore((s) => s.openViewTab)
  const activateTab = useRightPanelStore((s) => s.activateTab)
  const closeTab = useRightPanelStore((s) => s.closeTab)
  const pinTab = useRightPanelStore((s) => s.pinTab)

  const scrollRef = useRef<HTMLDivElement>(null)
  const addBtnRef = useRef<HTMLButtonElement>(null)
  const [anchor, setAnchor] = useState<{ right: number; bottom: number } | null>(null)

  const activeKey = activeOrDefault(layout)
  const openViews = layout.tabs
    .filter((t): t is Extract<RPanelTab, { kind: 'view' }> => t.kind === 'view')
    .map((t) => t.view)

  // 激活项自动滚入可视区。`block: 'nearest'` 是必须的 —— 默认值会让浏览器
  // 连祖先容器（整个应用）一起滚，表现为"切个标签整页抖一下"。
  useEffect(() => {
    const el = scrollRef.current?.querySelector('.rpanel-tab.active')
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ inline: 'nearest', block: 'nearest' })
    }
  }, [activeKey])

  const toggleAddMenu = (): void => {
    if (addMenuOpen) {
      setAddMenuOpen(false)
      return
    }
    const rect = addBtnRef.current?.getBoundingClientRect()
    setAnchor(rect ? { right: rect.right, bottom: rect.bottom } : null)
    setAddMenuOpen(true)
  }

  return (
    <div className="rpanel-tabs" role="tablist">
      <div className="rpanel-tabs-scroll" ref={scrollRef}>
        {layout.tabs.map((tab) => {
          const key = tabKey(tab)
          const isView = tab.kind === 'view'
          const active = key === activeKey
          const cls = [
            'rpanel-tab',
            isView ? '--view' : tab.pinned ? '--pinned' : '--preview',
            active ? 'active' : ''
          ]
            .filter(Boolean)
            .join(' ')
          const title = isView
            ? RPANEL_VIEW_LABELS[tab.view]
            : tab.pinned
              ? tab.path
              : `来自会话链接 · ${tab.path}`
          return (
            <div
              key={key}
              className={cls}
              // 中键关闭：浏览器/编辑器的通用约定（比右键菜单快一步）
              onMouseDown={(e) => {
                if (e.button === 1) e.preventDefault()
              }}
              onAuxClick={(e) => {
                if (e.button === 1) closeTab(sid, key)
              }}
            >
              <button
                className="rpanel-tab-main"
                role="tab"
                aria-selected={active}
                title={title}
                onClick={() => activateTab(sid, key)}
                // 双击预览位 = 升级为常驻（防被顶替）。点两下本来就是"激活 + 激活"，
                // 幂等，不会有多余副作用。
                onDoubleClick={() => {
                  if (!isView && !tab.pinned) pinTab(sid, key)
                }}
              >
                <Icon
                  name={isView ? RPANEL_VIEW_ICONS[tab.view] : 'fileText'}
                  size={13}
                />
                <span className="rpanel-tab-label">
                  {isView ? RPANEL_VIEW_LABELS[tab.view] : tab.name}
                </span>
                {!isView && !tab.pinned ? (
                  <span className="rpanel-tab-origin" title="来自会话链接" />
                ) : null}
              </button>
              <button
                className="rpanel-tab-close"
                title="关闭标签"
                onClick={() => closeTab(sid, key)}
              >
                <Icon name="close" size={12} />
              </button>
            </div>
          )
        })}
      </div>
      <button
        className={`rpanel-tab-add${addMenuOpen ? ' open' : ''}`}
        ref={addBtnRef}
        title="添加标签页"
        aria-haspopup="menu"
        aria-expanded={addMenuOpen}
        onClick={toggleAddMenu}
      >
        <Icon name="plus" size={14} />
      </button>
      {addMenuOpen ? (
        <RPanelAddMenu
          openViews={openViews}
          anchor={anchor}
          onPick={(view: RPanelView) => openViewTab(sid, view)}
          onClose={() => setAddMenuOpen(false)}
        />
      ) : null}
    </div>
  )
}
