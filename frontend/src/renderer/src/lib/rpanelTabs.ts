import type { RPanelPersist, RPanelTab, RPanelView } from '@protocols/agentProtocol'

/**
 * 右栏标签栏的**纯函数语义层**（单栏混放：视图标签与文件标签同排）。
 *
 * 为什么单独成文件、且全是纯函数：标签栏这套语义是本期最容易写错的地方
 * （预览位顶替 / 升级为常驻 / 关闭后的激活转移 / 超限淘汰），而它**没有 DOM、
 * 没有网络**，抽出来就能逐条验。组件只负责画，store 只负责存。
 *
 * ⚠️ **规则必须与后端 `session_manage.normalize_right_panel()` 逐条对齐** ——
 * 它落盘前会把这份数据再归一化一次，两边一旦漂移，症状是"内存里好好的，
 * 重启后标签莫名少了一枚"。
 *
 * 设计见 docs/frontend/19-右侧面板（文件与变更）.md。
 */

/** 常驻文件标签上限（与后端 `RIGHTPANEL_MAX_FILE_TABS` 同值） */
export const RPANEL_MAX_FILE_TABS = 12
/** 标签总数上限 = 4 个视图 + 12 个常驻文件 + 1 个预览位（与后端同值） */
export const RPANEL_MAX_TABS = 17

/** 该会话从未用过右栏时的默认值（`right_panel` 为 null / 字段缺失时回落） */
export const DEFAULT_RPANEL: RPanelPersist = { open: false, tabs: [], active: null }

/** 视图枚举顺序（与后端 `RIGHTPANEL_VIEWS` 同序；`normalizeTabs` 的校验源） */
export const RPANEL_VIEW_ORDER: RPanelView[] = ['files', 'changes', 'terminal', 'browser']

/** 文件标签的来源：`chat` = 会话内点文件链接（走预览位）；`tree` = 树里点开（走常驻位） */
export type FileTabOrigin = 'chat' | 'tree'

/** 标签集合 + 当前激活（store 里所有标签操作都按这个形状传进传出） */
export interface TabSet {
  tabs: RPanelTab[]
  active: string | null
}

/** 视图标签的稳定键（与后端 `right_panel_tab_key` 逐字一致） */
export function viewTabKey(view: RPanelView): string {
  return `view:${view}`
}

/** 文件标签的稳定键 */
export function fileTabKey(path: string): string {
  return `file:${path}`
}

export function tabKey(tab: RPanelTab): string {
  return tab.kind === 'view' ? viewTabKey(tab.view) : fileTabKey(tab.path)
}

/** 绝对路径取文件名（显示用）。与输入区 `@` 胶囊同口径：只给 basename。 */
export function basenameOf(path: string): string {
  const clean = (path || '').replace(/\/+$/, '')
  const i = clean.lastIndexOf('/')
  return i >= 0 ? clean.slice(i + 1) : clean
}

/** `active` 可能指向一个已不存在的标签（关标签/淘汰/脏数据）→ 回落到末项。
 *
 *  回落末项而不是首项：标签栏"末位 = 最近打开"，用户点过的那个永远在最后。 */
export function activeOrDefault(layout: RPanelPersist): string | null {
  const keys = layout.tabs.map(tabKey)
  if (layout.active && keys.includes(layout.active)) return layout.active
  return keys.length ? keys[keys.length - 1] : null
}

/** 当前是否正停在某个视图标签上（工具栏按钮的 `aria-pressed` 判据） */
export function isViewActive(layout: RPanelPersist, view: RPanelView): boolean {
  return layout.open && activeOrDefault(layout) === viewTabKey(view)
}

/** 该视图标签是否已存在（「+」菜单显示 ✓） */
export function hasViewTab(tabs: RPanelTab[], view: RPanelView): boolean {
  return tabs.some((t) => t.kind === 'view' && t.view === view)
}

/** 超限淘汰：先常驻文件标签（丢最旧的非激活），再文件标签总数。
 *  视图标签与预览位（最近一次交互的结果）永远保留 —— 淘汰它们是"点了没反应"。 */
export function evictOverflow(set: TabSet): TabSet {
  const tabs = set.tabs.slice()
  const pinnedCount = (): number =>
    tabs.filter((t) => t.kind === 'file' && t.pinned).length
  const droppable = (): number[] =>
    tabs
      .map((t, i) => ({ t, i }))
      .filter(({ t }) => t.kind === 'file' && t.pinned && tabKey(t) !== set.active)
      .map(({ i }) => i)

  while (pinnedCount() > RPANEL_MAX_FILE_TABS) {
    const idx = droppable()
    if (!idx.length) break
    tabs.splice(idx[0], 1)
  }
  while (tabs.length > RPANEL_MAX_TABS) {
    const idx = droppable()
    if (!idx.length) break
    tabs.splice(idx[0], 1)
  }
  const keys = tabs.map(tabKey)
  return { tabs, active: set.active && keys.includes(set.active) ? set.active : null }
}

/** 打开（或激活）一个视图标签；每类至多一枚 —— 已存在就只激活，不新增。 */
export function addViewTab(tabs: RPanelTab[], view: RPanelView): TabSet {
  const key = viewTabKey(view)
  if (hasViewTab(tabs, view)) return { tabs, active: key }
  return { tabs: [...tabs, { kind: 'view', view }], active: key }
}

/**
 * 打开一个文件标签 —— **预览位 / 常驻位的全部语义都在这里**。
 *
 * 四条硬规则（目的是"同一文件永远只有一枚标签"，且"用户手动开的不会被顶掉"）：
 * 1. 该 path 已在**常驻位** → 只激活（**不新建、不消耗预览位**）；
 * 2. 该 path 已在**预览位** → 会话来源只激活；树来源**就地升级为常驻**
 *    （去掉斜体与来源点，但仍是一枚标签，不产生两枚同名）；
 * 3. 否则新增：会话来源落**预览位**（全场唯一，已有预览位就**就地替换** ——
 *    保持原位置，标签栏不跳动）；树来源落常驻位；
 * 4. 新增时的**位置**一律由 `fileTabAnchor` 决定（紧跟最后一个文件相关标签），
 *    不是一律追加到栏尾 —— 那正是"文件标签跳到终端右边"的根因，见该函数注释。
 */
export function openFileTab(
  set: TabSet,
  file: { path: string; name?: string },
  origin: FileTabOrigin
): TabSet {
  const path = String(file?.path ?? '')
  if (!path) return set
  const name = (file?.name || '').trim() || basenameOf(path)
  const key = fileTabKey(path)
  const idx = set.tabs.findIndex((t) => t.kind === 'file' && t.path === path)

  if (idx >= 0) {
    const found = set.tabs[idx]
    if (found.kind !== 'file' || found.pinned || origin === 'chat') {
      // 常驻命中 / 预览位命中且来源也是会话 → 只激活
      return { tabs: set.tabs, active: key }
    }
    const tabs = set.tabs.slice()
    tabs[idx] = { ...found, pinned: true }
    return evictOverflow({ tabs, active: key })
  }

  const at = fileTabAnchor(set.tabs)
  const tabs = set.tabs.slice()
  if (origin === 'tree') {
    tabs.splice(at, 0, { kind: 'file', path, name, pinned: true })
    return evictOverflow({ tabs, active: key })
  }

  // 会话来源：已有预览位就**就地顶替**（同位置换内容），否则插到文件锚点处
  const pIdx = set.tabs.findIndex((t) => t.kind === 'file' && !t.pinned)
  const tab: RPanelTab = { kind: 'file', path, name, pinned: false }
  if (pIdx >= 0) tabs[pIdx] = tab
  else tabs.splice(at, 0, tab)
  return evictOverflow({ tabs, active: key })
}

/**
 * 新文件标签的插入位置 —— **位置稳定**的关键（2026-09-23 用户实测回归，19 篇 §3.1）。
 *
 * 锚点 = 最后一个「文件相关」标签（`view:files` 视图标签，或任一文件标签）；
 * 新标签插在它**之后**。一枚文件相关标签都没有时（例如只开着「变更」视图）才落末尾。
 *
 * 为什么不是「一律追加到末尾」：本栏是**单栏混放**，视图标签与文件标签同排。用户在
 * 「终端 / 变更」之后打开一个文件时，追加到末尾会让新标签落在那些视图标签**右边** ——
 * 从用户视角就是「文件标签跳到终端右边去了」（2026-09-23 实测报的就是这个）。
 * 锚定在同类之后，文件标签永远挨着文件视图与已有文件标签；且**只决定新标签插在哪，
 * 已有标签的相对顺序一枚都不动**（这比「文件标签永远排最后」更保守：后者会把已经
 * 开着的文件标签从「文件」视图后面推走，同样是「位置变了」）。
 */
function fileTabAnchor(tabs: RPanelTab[]): number {
  for (let i = tabs.length - 1; i >= 0; i -= 1) {
    const t = tabs[i]
    if (t.kind === 'file' || (t.kind === 'view' && t.view === 'files')) return i + 1
  }
  return tabs.length
}

/** 关闭一枚标签 → 激活**右邻，无则左邻**；关光则 `active` 为 null（回欢迎态）。 */
export function closeTabKey(set: TabSet, key: string): TabSet {
  const idx = set.tabs.findIndex((t) => tabKey(t) === key)
  if (idx < 0) return set
  const tabs = set.tabs.filter((_, i) => i !== idx)
  if (set.active !== key) {
    const keys = tabs.map(tabKey)
    return { tabs, active: set.active && keys.includes(set.active) ? set.active : null }
  }
  const next = tabs[idx] ?? tabs[idx - 1] ?? null
  return { tabs, active: next ? tabKey(next) : null }
}

/** 预览位 → 常驻位（双击标签 / 树里再点同一文件时用）。 */
export function pinTab(set: TabSet, key: string): TabSet {
  const idx = set.tabs.findIndex((t) => tabKey(t) === key)
  if (idx < 0) return set
  const found = set.tabs[idx]
  if (found.kind !== 'file' || found.pinned) return set
  const tabs = set.tabs.slice()
  tabs[idx] = { ...found, pinned: true }
  return { tabs, active: set.active }
}

/**
 * 前端兜底归一化（与后端 `normalize_right_panel` 同规则）。
 *
 * 后端已经归一化过一遍，这里再来的理由：**前端也会自己造数据**（点击累积），
 * 而 store 里的 `tabs` 一旦混进畸形项，渲染层会直接抛（`tab.kind` 分支没有
 * default）。规则刻意与后端逐条一致，谁改都要同时改两处。
 */
export function normalizeTabs(raw: unknown, active: unknown): TabSet {
  const list = Array.isArray(raw) ? raw : []
  const tabs: RPanelTab[] = []
  const seenViews = new Set<string>()
  const seenPaths = new Set<string>()

  for (const item of list) {
    if (!item || typeof item !== 'object') continue
    const rec = item as Record<string, unknown>
    if (rec.kind === 'view') {
      const view = rec.view as RPanelView
      if (!RPANEL_VIEW_ORDER.includes(view) || seenViews.has(view)) continue
      seenViews.add(view)
      tabs.push({ kind: 'view', view })
    } else if (rec.kind === 'file') {
      const path = typeof rec.path === 'string' ? rec.path.trim() : ''
      if (!path || seenPaths.has(path)) continue
      seenPaths.add(path)
      const name =
        typeof rec.name === 'string' && rec.name.trim() ? rec.name.trim() : basenameOf(path)
      tabs.push({
        kind: 'file',
        path,
        name,
        // 非 bool 一律视作常驻：手改数据不该凭空造出"幽灵预览位"
        pinned: typeof rec.pinned === 'boolean' ? rec.pinned : true
      })
    }
  }

  // 预览位全场唯一：只留最后一枚
  const previews = tabs
    .map((t, i) => ({ t, i }))
    .filter(({ t }) => t.kind === 'file' && !t.pinned)
    .map(({ i }) => i)
  let kept = tabs
  if (previews.length > 1) {
    const drop = new Set(previews.slice(0, -1))
    kept = tabs.filter((_, i) => !drop.has(i))
  }

  const activeKey = typeof active === 'string' && active ? active : null
  return evictOverflow({ tabs: kept, active: activeKey })
}

/** 归一化整个右栏状态（`right_panel` 字段的入口；null / 脏形状 → 默认值）。 */
export function normalizePersist(raw: unknown): RPanelPersist {
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_RPANEL }
  const rec = raw as Record<string, unknown>
  const set = normalizeTabs(rec.tabs, rec.active)
  return { open: !!rec.open, tabs: set.tabs, active: set.active }
}
