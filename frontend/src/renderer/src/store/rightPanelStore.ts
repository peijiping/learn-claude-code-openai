import { create } from 'zustand'
import type {
  FileContentPayload,
  GitDiffPayload,
  GitStatusPayload,
  RPanelPersist,
  RPanelView,
  RefsPayload
} from '@protocols/agentProtocol'
import {
  DEFAULT_RPANEL,
  activeOrDefault,
  addViewTab,
  basenameOf,
  closeTabKey,
  isViewActive,
  normalizePersist,
  openFileTab as openFileTabPure,
  pinTab as pinTabPure,
  tabKey,
  type FileTabOrigin
} from '@lib/rpanelTabs'
import { ancestorDirs } from '@lib/fileTree'

/**
 * 右侧面板状态（2026-09-23，docs/frontend/19）。
 *
 * ════════════════════════════════════════════════════════════════════
 * ⚠️ 本 store **禁止 import agentStore**
 * ════════════════════════════════════════════════════════════════════
 * `agentStore` 要在 `session_history` 分支里调本 store 的 `applySessionUi`，
 * 若本 store 再反向 import `agentStore` 去读"当前会话"，就形成循环依赖
 * （ESM 下初始化顺序不定，症状是偶发 `undefined`，且只在某些打包顺序下复现）。
 *
 * 所以：**所有 action 一律把 `sessionId` 当参数收**，"当前是哪个会话"由调用方
 * （组件层读 `activeSession`，或 agentStore 内部）传入。依赖保持单向：
 * `agentStore → rightPanelStore`、`组件 → 两者`。
 *
 * ════════════════════════════════════════════════════════════════════
 * 两级内存结构：布局按会话长期留，数据缓存只留当前一份
 * ════════════════════════════════════════════════════════════════════
 * - `layoutBySession`：每会话一份"开着的标签 + 当前激活 + 开合"。**长期保留**，
 *   这样切回来时标签栏同步恢复、无闪动；体积极小（≤17 条）。
 *   它是内存镜像，落盘走会话元数据（§持久化）。
 * - `liveBySession`：文件树 / 预览内容 / git status / diff 这些**大块数据**，
 *   只保留当前会话那一份，切会话即丢弃其它（与 `currentContextStats` 同范式：
 *   树可能 3000 条、diff 可能几百 KB，N 个会话累积明显吃内存，且天然易过期）。
 *
 * 见 19 篇 §2.2/§2.3。
 */

/** 一个会话的"活缓存"（大块数据，只留当前会话） */
export interface RPanelLive {
  tree: RefsPayload | null
  treeLoading: boolean
  treeError: string
  expanded: Record<string, boolean>
  /** 树尚未加载时暂存"要展开到哪个文件"，回执到达后消费掉 */
  pendingReveal: string
  /** 最近一次成功读取的预览内容（渲染前必须校验 `path` 与激活标签一致） */
  preview: FileContentPayload | null
  /** 在途预览请求的路径（'' = 没有在途）；回执按它自校验，防慢回执覆盖新选中 */
  previewPath: string
  previewLoading: boolean
  previewError: string
  git: GitStatusPayload | null
  gitLoading: boolean
  gitError: string
  /** 按仓库相对路径缓存已取到的 diff（同一文件反复展开不再打 git） */
  diffs: Record<string, GitDiffPayload>
  diffLoading: Record<string, boolean>
  /** 当前展开着 diff 的那一行（同时只开一条） */
  gitExpanded: string
}

function emptyLive(): RPanelLive {
  return {
    tree: null,
    treeLoading: false,
    treeError: '',
    expanded: {},
    pendingReveal: '',
    preview: null,
    previewPath: '',
    previewLoading: false,
    previewError: '',
    git: null,
    gitLoading: false,
    gitError: '',
    diffs: {},
    diffLoading: {},
    gitExpanded: ''
  }
}

/** 组件选择器里的兜底值。
 *
 *  ⚠️ 必须是**模块级同一对象**：`useRightPanelStore(s => s.liveBySession[sid]) ?? 新对象`
 *  每次渲染都产生新引用 → zustand 的比较永远为"变了" → 组件无限重渲染。 */
export const EMPTY_LIVE: RPanelLive = emptyLive()

// ══════════════════════════════════════════════════════════════════
//  落盘：防抖合并上报（`session_ui` 是 fire-and-forget 命令）
// ══════════════════════════════════════════════════════════════════
//
// 三条纪律（19 篇 §2.5，每条都对应一次真实踩坑）：
// 1. **payload 自带 session_id**：绝不在发送那一刻读"当前会话"。否则"防抖窗口内
//    切了会话"会把 A 的状态写进 B 的元数据。
// 2. **切会话先 flush**：`flushRightPanelPending()` 由 `switchSession` 调用。
// 3. **内存优先于落盘**：落盘是异步的，回读时若内存已有该会话的桶就整条忽略
//    （`applySessionUi`），否则会把用户刚点开/刚关掉的标签吃掉。

const PERSIST_DEBOUNCE_MS = 400
const persistTimers = new Map<string, ReturnType<typeof setTimeout>>()
const persistQueue = new Map<string, RPanelPersist>()

function flushOne(sid: string): void {
  const ui = persistQueue.get(sid)
  persistQueue.delete(sid)
  const timer = persistTimers.get(sid)
  if (timer) {
    clearTimeout(timer)
    persistTimers.delete(sid)
  }
  if (!ui) return
  try {
    // 有意不 await：命令无回包，落盘失败也只能记日志（状态以内存桶为准）
    void window.agent?.sessionUi?.({ session_id: sid, ui })
  } catch {
    /* preload 未就绪 / 浏览器回退通道缺失：忽略，下一次操作会再排一次 */
  }
}

/** 立即把**所有**会话的待写状态发出去（切会话、关窗前调用）。 */
export function flushRightPanelPending(): void {
  for (const sid of Array.from(persistQueue.keys())) flushOne(sid)
}

function schedulePersist(sid: string, ui: RPanelPersist): void {
  if (!sid) return
  persistQueue.set(sid, ui)
  const old = persistTimers.get(sid)
  if (old) clearTimeout(old)
  persistTimers.set(
    sid,
    setTimeout(() => flushOne(sid), PERSIST_DEBOUNCE_MS)
  )
}

// 关窗兜底：防抖窗口内关掉应用时，最近 400ms 的操作不该丢。
// 注册在模块级（幂等，模块只求值一次）—— 右栏组件在"面板收起"时并不渲染，
// 挂在组件里会漏掉"收起面板后操作 → 立刻关窗"这条路。
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', () => flushRightPanelPending())
}

// ══════════════════════════════════════════════════════════════════
//  宽度（全局单值，窗口级布局属性，与会话无关）
// ══════════════════════════════════════════════════════════════════

export const RPANEL_MIN_WIDTH = 280
export const RPANEL_DEFAULT_WIDTH = 360
export const RPANEL_MAX_WIDTH = 720
const WIDTH_KEY = 'aigent.rpanel.width'
/** 右栏之外至少要留给聊天区+侧边栏的宽度（clamp 上限用） */
const RESERVED_WIDTH = 520

/** 当前窗口下允许的最大宽度（窗口很窄时不能让右栏把聊天区挤成 0） */
export function rpanelMaxWidth(): number {
  const avail = (typeof window === 'undefined' ? 1440 : window.innerWidth) - RESERVED_WIDTH
  return Math.max(RPANEL_MIN_WIDTH, Math.min(RPANEL_MAX_WIDTH, avail))
}

export function clampWidth(w: number): number {
  const n = Number.isFinite(w) ? w : RPANEL_DEFAULT_WIDTH
  return Math.round(Math.max(RPANEL_MIN_WIDTH, Math.min(rpanelMaxWidth(), n)))
}

function loadWidth(): number {
  try {
    const raw = localStorage.getItem(WIDTH_KEY)
    if (!raw) return RPANEL_DEFAULT_WIDTH
    return clampWidth(Number(raw))
  } catch {
    return RPANEL_DEFAULT_WIDTH
  }
}

function saveWidth(w: number): void {
  try {
    localStorage.setItem(WIDTH_KEY, String(w))
  } catch {
    /* 隐私模式等场景忽略：持久化失败不影响本次会话内的宽度 */
  }
}

// ══════════════════════════════════════════════════════════════════
//  Store
// ══════════════════════════════════════════════════════════════════

export interface RightPanelState {
  /** 每会话一份：开着的标签 + 当前激活 + 开合（长期保留） */
  layoutBySession: Record<string, RPanelPersist>
  /** 大块数据缓存，只保留当前会话一份 */
  liveBySession: Record<string, RPanelLive>
  width: number
  resizing: boolean
  /** 「+」下拉是否展开（纯 UI，切会话/切标签即关） */
  addMenuOpen: boolean

    // ── 读取 ───────────────────────────────────────────────────────
  /** 该会话的布局（无记录 → 默认值，引用稳定，可安全用于选择器） */
  layoutOf: (sid: string) => RPanelPersist

  // ── 面板开合 / 视图标签 ────────────────────────────────────────
  /** **视图直达**（不是"开栏"）：右栏已开着且正停在 `view` 上 → 收栏；否则开栏 +
   *  建/激活该视图标签。入口 = `⌘⇧E`(files) / `⌘⇧G`(changes) 快捷键。
   *  顶栏那枚图标**不走这里** —— 它只开关栏体，见下。 */
  togglePanel: (sid: string, view: RPanelView) => void
  /** 开栏并激活某视图（必开）：栏内「+」菜单 / 欢迎态四入口。 */
  openViewTab: (sid: string, view: RPanelView) => void
  /** **纯开合取反**（`open = !open`，不动 `tabs` / `active`）：窗口标题栏那枚开关图标。
   *
   *  ⚠️ 刻意做成"store 内部读-改-写"，而不是在组件里写 `setPanelOpen(sid, !open)`：
   *  后者把"现在是不是开着"钉死在**渲染那一刻的闭包**里，一旦组件没跟着 store 重渲染
   *  （订阅断了 / 被 memo / handler 存进 ref / 双实例 store……），就会每次都写同一个值，
   *  症状正是**"点得开、点不关"**（2026-09-23 用户实测报的就是这个）。
   *  开合状态只该有一个真相来源：`get()` 里的当前值。 */
  togglePanelOpen: (sid: string) => void
  /** **纯开合（定向）**：只改 `open` 这一个字段。`Esc` 收栏走这里；
   *  标题栏图标走上面的 `togglePanelOpen`。开合与内容正交 —— 收起再打开必须
   *  回到原来那些标签（19 篇 §2.1）。 */
  setPanelOpen: (sid: string, open: boolean) => void
  activateTab: (sid: string, key: string) => void
  closeTab: (sid: string, key: string) => void
  pinTab: (sid: string, key: string) => void
  setAddMenuOpen: (open: boolean) => void

  // ── 文件标签 ───────────────────────────────────────────────────
  /** 打开/激活一个文件标签（origin 决定落预览位还是常驻位） */
  openFileTab: (sid: string, file: { path: string; name?: string }, origin: FileTabOrigin) => void
  /** 会话内点文件链接的完整链路：开右栏 + 落预览位 + 展开祖先链 + 读内容 */
  revealFile: (sid: string, path: string, name?: string) => void

  // ── 数据取数 ───────────────────────────────────────────────────
  ensureTree: (sid: string, force?: boolean) => void
  toggleDir: (sid: string, path: string) => void
  expandTo: (sid: string, path: string) => void
  ensurePreview: (sid: string, path: string) => void
  ensureGit: (sid: string, force?: boolean) => void
  toggleGitDiff: (sid: string, path: string, staged?: boolean) => void

  // ── 回执入口（由 agentStore / 组件调用）────────────────────────
  applySessionUi: (sid: string, seed: unknown) => void
  applyRefs: (sid: string, p: RefsPayload | null, error?: string) => void
  applyFileContent: (sid: string, p: FileContentPayload | null, error?: string) => void
  applyGitStatus: (sid: string, p: GitStatusPayload | null, error?: string) => void
  applyGitDiff: (sid: string, p: GitDiffPayload | null, error?: string) => void

  // ── 生命周期 ───────────────────────────────────────────────────
  keepOnly: (sid: string) => void
  dropSessions: (ids: string[]) => void
  /** 断线重连：只清"在途"标志，**不动 `layoutBySession`**（19 篇 §6 第 9 条） */
  resetTransient: () => void

  // ── 宽度 ───────────────────────────────────────────────────────
  setWidth: (w: number) => void
  resetWidth: () => void
  setResizing: (v: boolean) => void
}

export const useRightPanelStore = create<RightPanelState>((set, get) => {
  /** 改某个会话的布局 → 顺带排一次落盘。**唯一的布局写入口**，防漏落盘。 */
  const mutateLayout = (sid: string, fn: (cur: RPanelPersist) => RPanelPersist): void => {
    if (!sid) return
    const cur = get().layoutBySession[sid] ?? { ...DEFAULT_RPANEL }
    const next = fn(cur)
    set((s) => ({ layoutBySession: { ...s.layoutBySession, [sid]: next } }))
    schedulePersist(sid, next)
  }

  /** 改某个会话的活缓存；桶不存在时**不重建**（说明它已被切走/删除，回执该丢） */
  const mutateLive = (sid: string, fn: (cur: RPanelLive) => RPanelLive): void => {
    const cur = get().liveBySession[sid]
    if (!cur) return
    set((s) => ({ liveBySession: { ...s.liveBySession, [sid]: fn(cur) } }))
  }

  const requestTree = (sid: string): void => {
    mutateLive(sid, (l) => ({ ...l, treeLoading: true, treeError: '' }))
    window.agent
      .listRefs({ sessionId: sid })
      .then((res) => {
        get().applyRefs(sid, (res as RefsPayload | null) ?? null, res ? '' : '读取文件列表超时，请重试')
      })
      .catch(() => get().applyRefs(sid, null, '读取文件列表失败'))
  }

  const requestPreview = (sid: string, path: string): void => {
    mutateLive(sid, (l) => ({ ...l, previewPath: path, previewLoading: true, previewError: '' }))
    window.agent
      .readFile({ path, sessionId: sid })
      .then((res) => {
        get().applyFileContent(
          sid,
          (res as FileContentPayload | null) ?? null,
          res ? '' : '读取文件超时，请重试'
        )
      })
      .catch(() => get().applyFileContent(sid, null, '读取文件失败'))
  }

  const requestGit = (sid: string): void => {
    mutateLive(sid, (l) => ({ ...l, gitLoading: true, gitError: '' }))
    window.agent
      .gitStatus({ sessionId: sid })
      .then((res) => {
        get().applyGitStatus(sid, (res as GitStatusPayload | null) ?? null, res ? '' : '读取 git 状态超时')
      })
      .catch(() => get().applyGitStatus(sid, null, '读取 git 状态失败'))
  }

  const requestDiff = (sid: string, path: string, staged: boolean): void => {
    mutateLive(sid, (l) => ({ ...l, diffLoading: { ...l.diffLoading, [path]: true } }))
    window.agent
      .gitDiff({ path, staged, sessionId: sid })
      .then((res) => get().applyGitDiff(sid, (res as GitDiffPayload | null) ?? null))
      .catch(() => {
        const l = get().liveBySession[sid]
        if (!l) return
        mutateLive(sid, (cur) => ({
          ...cur,
          diffLoading: { ...cur.diffLoading, [path]: false },
          diffs: {
            ...cur.diffs,
            [path]: {
              path,
              staged,
              available: false,
              reason: '读取 diff 失败',
              diff: '',
              chars: 0,
              too_large: false,
              binary: false,
              untracked: false
            }
          }
        }))
      })
  }

  return {
    layoutBySession: {},
    liveBySession: {},
    width: loadWidth(),
    resizing: false,
    addMenuOpen: false,

    layoutOf: (sid) => get().layoutBySession[sid] ?? DEFAULT_RPANEL,

    // ── 面板开合 / 视图标签 ──────────────────────────────────────
    togglePanel: (sid, view) => {
      if (!sid) return
      const layout = get().layoutOf(sid)
      set({ addMenuOpen: false })
      if (!layout.open) {
        mutateLayout(sid, (cur) => {
          const next = addViewTab(cur.tabs, view)
          return { open: true, tabs: next.tabs, active: next.active }
        })
        return
      }
      if (isViewActive(layout, view)) {
        // 再按一次同一个快捷键 = 收起右栏（**不关标签**：下次打开还是刚才那些）
        mutateLayout(sid, (cur) => ({ ...cur, open: false }))
        return
      }
      mutateLayout(sid, (cur) => {
        const next = addViewTab(cur.tabs, view)
        return { ...cur, tabs: next.tabs, active: next.active }
      })
    },

    openViewTab: (sid, view) => {
      if (!sid) return
      set({ addMenuOpen: false })
      mutateLayout(sid, (cur) => {
        const next = addViewTab(cur.tabs, view)
        return { open: true, tabs: next.tabs, active: next.active }
      })
    },

    setPanelOpen: (sid, open) => mutateLayout(sid, (cur) => ({ ...cur, open })),

    togglePanelOpen: (sid) => {
      if (!sid) return
      // 取反用的是 `mutateLayout` 回调里的 `cur`（= 此刻 store 里的值），不是调用方
      // 渲染时读到的旧值 —— 这是"点得开、点不关"的根治办法。
      mutateLayout(sid, (cur) => ({ ...cur, open: !cur.open }))
    },

    activateTab: (sid, key) => {
      const layout = get().layoutOf(sid)
      if (!layout.tabs.some((t) => tabKey(t) === key)) return
      set({ addMenuOpen: false })
      mutateLayout(sid, (cur) => ({ ...cur, active: key }))
    },

    closeTab: (sid, key) => {
      mutateLayout(sid, (cur) => {
        const next = closeTabKey({ tabs: cur.tabs, active: activeOrDefault(cur) }, key)
        return { open: cur.open, tabs: next.tabs, active: next.active }
      })
    },

    pinTab: (sid, key) => {
      mutateLayout(sid, (cur) => {
        const next = pinTabPure({ tabs: cur.tabs, active: activeOrDefault(cur) }, key)
        return { ...cur, tabs: next.tabs, active: next.active }
      })
    },

    setAddMenuOpen: (open) => set({ addMenuOpen: open }),

    // ── 文件标签 ─────────────────────────────────────────────────
    openFileTab: (sid, file, origin) => {
      if (!sid || !file?.path) return
      set({ addMenuOpen: false })
      mutateLayout(sid, (cur) => {
        const next = openFileTabPure(
          { tabs: cur.tabs, active: activeOrDefault(cur) },
          { path: file.path, name: file.name || basenameOf(file.path) },
          origin
        )
        return { open: true, tabs: next.tabs, active: next.active }
      })
    },

    revealFile: (sid, path, name) => {
      if (!sid || !path) return
      get().openFileTab(sid, { path, name }, 'chat')
      get().expandTo(sid, path)
    },

    // ── 数据取数 ─────────────────────────────────────────────────
    ensureTree: (sid, force) => {
      if (!sid) return
      const live = get().liveBySession[sid]
      if (!live) {
        set((s) => ({ liveBySession: { ...s.liveBySession, [sid]: emptyLive() } }))
      }
      const cur = get().liveBySession[sid]
      if (!cur) return
      if (cur.treeLoading) return
      if (!force && cur.tree) return
      requestTree(sid)
    },

    toggleDir: (sid, path) => {
      mutateLive(sid, (l) => ({ ...l, expanded: { ...l.expanded, [path]: !l.expanded[path] } }))
    },

    expandTo: (sid, path) => {
      if (!sid || !path) return
      const live = get().liveBySession[sid]
      const tree = live?.tree
      if (!tree || !tree.workdir) {
        // 树还没加载：先记下目标，回执到达后在 applyRefs 里消费
        mutateLive(sid, (l) => ({ ...l, pendingReveal: path }))
        get().ensureTree(sid)
        return
      }
      const dirs = ancestorDirs(tree.workdir, path)
      if (!dirs.length) return
      mutateLive(sid, (l) => {
        const expanded = { ...l.expanded }
        for (const d of dirs) expanded[d] = true
        return { ...l, expanded }
      })
    },

    ensurePreview: (sid, path) => {
      if (!sid || !path) return
      const live = get().liveBySession[sid]
      if (!live) {
        set((s) => ({ liveBySession: { ...s.liveBySession, [sid]: emptyLive() } }))
      }
      const cur = get().liveBySession[sid]
      if (!cur) return
      // 已经在读同一个文件、或已有它的内容 → 不重发（本地切换零延迟）
      if (cur.previewPath === path && (cur.previewLoading || cur.preview?.path === path)) return
      if (cur.preview?.path === path && !cur.previewError) {
        mutateLive(sid, (l) => ({ ...l, previewPath: path }))
        return
      }
      requestPreview(sid, path)
    },

    ensureGit: (sid, force) => {
      if (!sid) return
      if (!get().liveBySession[sid]) {
        set((s) => ({ liveBySession: { ...s.liveBySession, [sid]: emptyLive() } }))
      }
      const cur = get().liveBySession[sid]
      if (!cur || cur.gitLoading) return
      if (!force && cur.git) return
      requestGit(sid)
    },

    toggleGitDiff: (sid, path, staged) => {
      if (!sid || !path) return
      const live = get().liveBySession[sid]
      if (!live) return
      if (live.gitExpanded === path) {
        mutateLive(sid, (l) => ({ ...l, gitExpanded: '' }))
        return
      }
      mutateLive(sid, (l) => ({ ...l, gitExpanded: path }))
      if (live.diffs[path]) return
      requestDiff(sid, path, !!staged)
    },

    // ── 回执入口 ─────────────────────────────────────────────────
    applySessionUi: (sid, seed) => {
      if (!sid) return
      // **只在桶不存在时写入**：落盘是防抖异步的，回读可能滞后于用户刚做的操作；
      // 整份替换会把刚点开/刚关掉的标签吃掉（同 permission_config 那次事故）。
      if (get().layoutBySession[sid]) return
      const layout = normalizePersist(seed)
      set((s) => ({ layoutBySession: { ...s.layoutBySession, [sid]: layout } }))
    },

    applyRefs: (sid, p, error) => {
      const live = get().liveBySession[sid]
      if (!live) return
      const data = p ?? null
      mutateLive(sid, (l) => {
        // 树回执到达后消费"要展开到哪个文件"（会话内点文件时树还没加载的那条路）
        let expanded = l.expanded
        const pending = l.pendingReveal
        if (data && pending && data.workdir) {
          const dirs = ancestorDirs(data.workdir, pending)
          if (dirs.length) {
            expanded = { ...expanded }
            for (const d of dirs) expanded[d] = true
          }
        }
        return {
          ...l,
          tree: data ?? l.tree,
          treeLoading: false,
          treeError: error ?? '',
          expanded,
          pendingReveal: data && pending ? '' : l.pendingReveal
        }
      })
    },

    applyFileContent: (sid, p, error) => {
      const live = get().liveBySession[sid]
      if (!live) return
      mutateLive(sid, (l) => {
        // 自校验：晚到的回执不得覆盖新选中（主进程 pending 表按 kind 配对、无 id）
        if (p && l.previewPath && p.path !== l.previewPath) return l
        return {
          ...l,
          preview: p ?? l.preview,
          previewLoading: false,
          // 后端把"读不到"也当正常结果回（内容里带 reason），只有传输层失败才走这里
          previewError: p ? '' : (error ?? ''),
          previewPath: p ? p.path : l.previewPath
        }
      })
    },

    applyGitStatus: (sid, p, error) => {
      const live = get().liveBySession[sid]
      if (!live) return
      mutateLive(sid, (l) => ({
        ...l,
        git: p ?? l.git,
        gitLoading: false,
        gitError: p ? '' : (error ?? '')
      }))
    },

    applyGitDiff: (sid, p) => {
      const live = get().liveBySession[sid]
      if (!live || !p?.path) return
      // 按 path 单条 merge：**绝不整份替换** diffs（会丢其它已展开文件的 diff）。
      // 同时清掉在途标记 —— 无论它是否还是当前展开的那一行（单飞行，见 §4.4）。
      mutateLive(sid, (l) => ({
        ...l,
        diffLoading: { ...l.diffLoading, [p.path]: false },
        diffs: { ...l.diffs, [p.path]: p }
      }))
    },

    // ── 生命周期 ─────────────────────────────────────────────────
    keepOnly: (sid) => {
      // 只丢**数据缓存**：`layoutBySession` 要长期留着，切回来才能立刻恢复标签栏
      const live = get().liveBySession
      const kept: Record<string, RPanelLive> = {}
      if (sid && live[sid]) kept[sid] = live[sid]
      set({ liveBySession: kept, addMenuOpen: false })
    },

    dropSessions: (ids) => {
      if (!ids?.length) return
      const drop = new Set(ids)
      const layout = { ...get().layoutBySession }
      const live = { ...get().liveBySession }
      for (const id of drop) {
        delete layout[id]
        delete live[id]
        persistQueue.delete(id)
        const timer = persistTimers.get(id)
        if (timer) {
          clearTimeout(timer)
          persistTimers.delete(id)
        }
      }
      set({ layoutBySession: layout, liveBySession: live })
    },

    resetTransient: () => {
      // 断线期间发出去的请求**永远不会有回执**（连接已经没了），
      // 不清这些标志的话切回来会看到几个永远转圈的面板。
      // 刻意**不碰** layoutBySession：标签栏是用户的意图，不是可恢复的瞬时态。
      const live = get().liveBySession
      const next: Record<string, RPanelLive> = {}
      for (const [key, item] of Object.entries(live)) {
        next[key] = {
          ...item,
          treeLoading: false,
          previewLoading: false,
          gitLoading: false,
          diffLoading: {},
          pendingReveal: ''
        }
      }
      set({ liveBySession: next })
    },

    // ── 宽度 ─────────────────────────────────────────────────────
    setWidth: (w) => set({ width: clampWidth(w) }),
    resetWidth: () => {
      set({ width: RPANEL_DEFAULT_WIDTH })
      saveWidth(RPANEL_DEFAULT_WIDTH)
    },
    setResizing: (v) => set({ resizing: v })
  }
})

/** 拖拽结束时才落 localStorage（拖拽过程中每帧写会让拖动发涩）。 */
export function persistWidth(w: number): void {
  saveWidth(clampWidth(w))
}
