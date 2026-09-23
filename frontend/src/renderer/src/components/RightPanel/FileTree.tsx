import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { Icon } from '@components/common/Icon'
import { showToast } from '@store/agentStore'
import { EMPTY_LIVE, useRightPanelStore } from '@store/rightPanelStore'
import { buildFileTree, type RPanelTreeNode } from '@lib/fileTree'
import RPanelState from './RPanelState'

interface FileTreeProps {
  sid: string
  /** 当前激活的文件标签路径 → 树里高亮它（预览打开的那个文件） */
  activePath: string | null
}

interface FlatRow {
  node: RPanelTreeNode
  depth: number
  /** 父目录的绝对路径（'' = 顶层）；`←` 回父节点时用 */
  parent: string
}

/** 按"当前展开态"把树压平成一维：键盘导航（↑↓ 顺序）与渲染共用同一份顺序，
 *  两者分头遍历会出现"按下键跳到了看不见的行"。 */
function flattenTree(roots: RPanelTreeNode[], expanded: Record<string, boolean>): FlatRow[] {
  const out: FlatRow[] = []
  const walk = (nodes: RPanelTreeNode[], depth: number, parent: string): void => {
    for (const node of nodes) {
      out.push({ node, depth, parent })
      if (node.isDir && node.children.length && expanded[node.path]) {
        walk(node.children, depth + 1, node.path)
      }
    }
  }
  walk(roots, 0, '')
  return out
}

/**
 * 「文件」视图：工作空间文件树（19 篇 §5.6）。
 *
 * 树数据来自 `refs_list` 的**扁平列表**，层级由 `lib/fileTree.ts` 还原
 * （那是全项目唯一一处扁平 → 树的切分逻辑）。本组件只做三件事：
 * 画行、管展开态（展开态在 store 的活缓存里，**按会话**记，不持久化）、键盘导航。
 *
 * 键盘（`role="tree"` + roving tabindex）刻意做全，因为右栏是"贴着键盘用"的区域：
 * 从会话里点开一个文件、想顺手看看同级目录，用鼠标在 360px 里找目标比按键慢。
 */
export default function FileTree({ sid, activePath }: FileTreeProps): JSX.Element {
  const live = useRightPanelStore((s) => s.liveBySession[sid]) ?? EMPTY_LIVE
  const toggleDir = useRightPanelStore((s) => s.toggleDir)
  const openFileTab = useRightPanelStore((s) => s.openFileTab)
  const { tree, treeLoading, treeError, expanded } = live

  const [focusKey, setFocusKey] = useState('')
  const [menu, setMenu] = useState<{ x: number; y: number; node: RPanelTreeNode } | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  const roots = useMemo(
    () => buildFileTree(tree?.workdir ?? '', tree?.items),
    [tree?.workdir, tree?.items]
  )
  const rows = useMemo(() => flattenTree(roots, expanded), [roots, expanded])

  // 键盘漫游：focusKey 变化时把 DOM 焦点搬过去（roving tabindex 的"漫游"就靠这一步）
  useEffect(() => {
    if (!focusKey || !scrollRef.current) return
    const el = scrollRef.current.querySelector<HTMLElement>(
      `[data-path="${CSS.escape(focusKey)}"]`
    )
    if (el) el.focus()
  }, [focusKey])

  const onKeyDown = (e: ReactKeyboardEvent): void => {
    if (!rows.length) return
    const idx = rows.findIndex((r) => r.node.path === focusKey)
    const cur = idx >= 0 ? rows[idx] : null
    const focusAt = (i: number): void => {
      const row = rows[Math.max(0, Math.min(i, rows.length - 1))]
      if (row) setFocusKey(row.node.path)
    }

    switch (e.key) {
      case 'ArrowDown':
        focusAt(idx < 0 ? 0 : idx + 1)
        break
      case 'ArrowUp':
        focusAt(idx < 0 ? 0 : idx - 1)
        break
      case 'ArrowRight': {
        if (!cur) return focusAt(0)
        if (!cur.node.isDir) return
        if (!expanded[cur.node.path]) {
          toggleDir(sid, cur.node.path)
        } else if (cur.node.children.length) {
          // 已展开 → 进第一个子节点（压平后它必然紧邻下一行）
          focusAt(idx + 1)
        }
        break
      }
      case 'ArrowLeft': {
        if (!cur) return focusAt(0)
        if (cur.node.isDir && expanded[cur.node.path]) {
          toggleDir(sid, cur.node.path)
        } else if (cur.parent) {
          const pIdx = rows.findIndex((r) => r.node.path === cur.parent)
          if (pIdx >= 0) focusAt(pIdx)
        }
        break
      }
      case 'Enter':
      case ' ':
        if (!cur) return focusAt(0)
        if (cur.node.isDir) toggleDir(sid, cur.node.path)
        else openFileTab(sid, { path: cur.node.path, name: cur.node.name }, 'tree')
        break
      case 'Home':
        focusAt(0)
        break
      case 'End':
        focusAt(rows.length - 1)
        break
      default:
        return
    }
    e.preventDefault()
  }

  const copyPath = (path: string): void => {
    // 走 navigator.clipboard（与 MessageItem 的复制同一条路），失败给 toast 而不是静默
    navigator.clipboard
      .writeText(path)
      .then(() => showToast('已复制路径'))
      .catch(() => showToast('复制失败', 'error', 3000))
  }

  const head = (
    <div className="rpanel-head">
      <div className="rpanel-head-title">
        <span className="rpanel-head-name">文件</span>
        {tree?.workdir ? <span className="rpanel-head-path" title={tree.workdir}>{tree.workdir}</span> : null}
      </div>
      <div className="rpanel-head-actions">
        <button
          className="mini-btn"
          title="刷新文件列表"
          onClick={() => useRightPanelStore.getState().ensureTree(sid, true)}
        >
          {treeLoading ? <span className="rpanel-spinner" /> : <Icon name="refresh" size={14} />}
        </button>
      </div>
    </div>
  )

  // ── 状态判定（优先级固定：不可用 > error > loading > empty > 内容）──
  if (tree?.disabled) {
    return (
      <div className="rpanel-tree">
        {head}
        <RPanelState kind="empty" icon="folder" title="当前工作空间没有可浏览的文件" sub={tree.reason} />
      </div>
    )
  }
  if (treeError) {
    return (
      <div className="rpanel-tree">
        {head}
        <RPanelState
          kind="error"
          icon="folder"
          title="无法读取文件列表"
          sub={treeError}
          action={
            <button className="rpanel-btn" onClick={() => useRightPanelStore.getState().ensureTree(sid, true)}>
              重试
            </button>
          }
        />
      </div>
    )
  }
  if (!tree && treeLoading) {
    return (
      <div className="rpanel-tree">
        {head}
        <RPanelState kind="loading" title="正在读取文件列表…" />
      </div>
    )
  }
  if (!rows.length) {
    return (
      <div className="rpanel-tree">
        {head}
        <RPanelState kind="empty" icon="folder" title="这个工作空间里没有文件" />
      </div>
    )
  }

  return (
    <div className="rpanel-tree">
      {head}
      <div
        className="rpanel-tree-scroll"
        role="tree"
        aria-label="工作空间文件"
        ref={scrollRef}
        onKeyDown={onKeyDown}
      >
        {rows.map(({ node, depth }) => {
          const isOpen = !!expanded[node.path]
          const active = !node.isDir && node.path === activePath
          return (
            <div
              key={node.path}
              className={`rpanel-tree-node${active ? ' active' : ''}`}
              data-path={node.path}
              role="treeitem"
              aria-expanded={node.isDir ? isOpen : undefined}
              aria-selected={active}
              tabIndex={focusKey === node.path ? 0 : -1}
              title={node.path}
              style={{ paddingLeft: 4 + depth * 14 }}
              onClick={() => {
                setFocusKey(node.path)
                if (node.isDir) toggleDir(sid, node.path)
                else openFileTab(sid, { path: node.path, name: node.name }, 'tree')
              }}
              onContextMenu={(e) => {
                e.preventDefault()
                setFocusKey(node.path)
                setMenu({ x: e.clientX, y: e.clientY, node })
              }}
            >
              {node.isDir ? (
                <span className={`rpanel-tree-chevron${isOpen ? ' rot' : ''}`}>
                  <Icon name="chevronRight" size={12} />
                </span>
              ) : (
                <span className="rpanel-tree-chevron" />
              )}
              <span className="rpanel-tree-icon">
                <Icon name={node.isDir ? 'folder' : 'fileText'} size={14} />
              </span>
              <span className="rpanel-tree-name">{node.name}</span>
            </div>
          )
        })}
        {tree?.truncated ? (
          <div className="rpanel-tree-foot">
            列表已达上限（{rows.length} 项），仅显示前一部分
          </div>
        ) : null}
      </div>

      {menu ? (
        <TreeContextMenu
          x={menu.x}
          y={menu.y}
          onReveal={() => {
            void window.agent.openInFinder(menu.node.path)
            setMenu(null)
          }}
          onCopy={() => {
            copyPath(menu.node.path)
            setMenu(null)
          }}
          onClose={() => setMenu(null)}
        />
      ) : null}
    </div>
  )
}

interface TreeContextMenuProps {
  x: number
  y: number
  onReveal: () => void
  onCopy: () => void
  onClose: () => void
}

/** 树行右键菜单（复用 `.session-menu` 外壳与 MessageMenu 的关闭逻辑）。 */
function TreeContextMenu({ x, y, onReveal, onCopy, onClose }: TreeContextMenuProps): JSX.Element {
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

  const left = Math.min(x, window.innerWidth - 160)
  const top = Math.min(y, window.innerHeight - 80)

  return (
    <div className="session-menu rpanel-menu" style={{ left, top }} ref={ref}>
      <button className="session-menu-item rpanel-menu-item" onClick={onReveal}>
        <Icon name="externalLink" size={14} />
        <span className="rpanel-menu-label">在 Finder 中显示</span>
      </button>
      <button className="session-menu-item rpanel-menu-item" onClick={onCopy}>
        <Icon name="copy" size={14} />
        <span className="rpanel-menu-label">复制路径</span>
      </button>
    </div>
  )
}
