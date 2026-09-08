import { create } from 'zustand'
import type { AgentEvent, HistoryMessage, SessionMeta, UiEvent, LlmConfig } from '@protocols/agentProtocol'

export type ConnState = 'connecting' | 'connected' | 'disconnected'
export type PythonState = 'starting' | 'running' | 'crashed' | 'stopped'
export type SettingsTab = 'general' | 'model' | 'trash' | 'about'

/** 会话显示名：无标题（未生成/老会话）回退 session_N */
export function sessionDisplayName(s: SessionMeta): string {
  return s.title?.trim() || `session_${s.num}`
}

export interface ToolCallMsg {
  id: string
  name: string
  args: string
  status: 'running' | 'done'
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  thinking: string
  toolCalls: ToolCallMsg[]
  activeToolId: string | null
  streaming: boolean
  usage: Record<string, number>
}

interface AgentState {
  connection: ConnState
  python: PythonState
  messages: Message[]
  sessions: SessionMeta[]
  trashSessions: SessionMeta[]
  activeSession: number | null
  isSending: boolean
  settingsOpen: boolean
  settingsTab: SettingsTab
  llmConfig: LlmConfig | null
  llmSaving: boolean
  toast: string | null
  toastType: 'info' | 'error'

  setConnection: (c: ConnState) => void
  setPython: (p: PythonState) => void
  send: (text: string) => void
  stop: () => void
  handleEvent: (ev: UiEvent) => void
  refreshSessions: () => Promise<void>
  refreshTrash: () => Promise<void>
  newSession: () => Promise<void>
  switchSession: (num: number) => Promise<void>
  clearSession: () => Promise<void>
  renameSession: (num: number, title: string) => Promise<void>
  trashSession: (num: number) => Promise<void>
  restoreSession: (num: number) => Promise<void>
  deleteSessions: (nums: number[]) => Promise<void>
  openSettings: (tab?: SettingsTab) => void
  closeSettings: () => void
  loadLlConfig: () => Promise<void>
  saveLlConfig: (config: LlmConfig) => Promise<boolean>
  setActiveModel: (id: string) => Promise<void>
  clearToast: () => void
}

let msgSeq = 0
const mid = (): string => `m${++msgSeq}`

/** 当前正在流式累积的 assistant 消息 id（不存在返回 null） */
const pendingAssistantId = (): string | null => {
  const msgs = useAgentStore.getState().messages
  const last = msgs[msgs.length - 1]
  return last && last.role === 'assistant' && last.streaming ? last.id : null
}

/** Toast 自动消失计时器：重复触发时重置，避免旧计时器提前清掉新提示 */
let toastTimer: ReturnType<typeof setTimeout> | null = null

/** 显示 Toast 并自动消失（info 默认 3s，error 默认 4s）。函数声明提升，运行时 useAgentStore 已初始化 */
export function showToast(msg: string, type: 'info' | 'error' = 'info', ms = 3000): void {
  if (toastTimer) clearTimeout(toastTimer)
  useAgentStore.setState({ toast: msg, toastType: type })
  toastTimer = setTimeout(() => {
    toastTimer = null
    useAgentStore.getState().clearToast()
  }, ms)
}

export const useAgentStore = create<AgentState>((set, get) => ({
  connection: 'disconnected',
  python: 'stopped',
  messages: [],
  sessions: [],
  trashSessions: [],
  activeSession: null,
  isSending: false,
  settingsOpen: false,
  settingsTab: 'model',
  llmConfig: null,
  llmSaving: false,
  toast: null,
  toastType: 'info',

  setConnection: (c) => set({ connection: c }),
  setPython: (p) => set({ python: p }),

  send: (text) => {
    const t = text.trim()
    if (!t || get().isSending) return
    // 惰性会话：无激活会话（新建任务后的首条消息）带 fresh 标记，由后端创建 jsonl
    const fresh = get().activeSession === null
    set((s) => ({
      messages: [
        ...s.messages,
        { id: mid(), role: 'user', content: t, thinking: '', toolCalls: [], activeToolId: null, streaming: false, usage: {} },
        { id: mid(), role: 'assistant', content: '', thinking: '', toolCalls: [], activeToolId: null, streaming: true, usage: {} }
      ],
      isSending: true
    }))
    window.agent.send(t, fresh).catch(() => set({ isSending: false }))
  },

  stop: () =>
    set((s) => ({
      isSending: false,
      messages: s.messages.map((m) =>
        m.role === 'assistant' && m.streaming ? { ...m, streaming: false, activeToolId: null } : m
      )
    })),

  handleEvent: (ev) => {
    if (ev.kind === 'event') {
      set((s) => applyAgentEvent(s, ev.payload as AgentEvent))
      return
    }
    switch (ev.kind) {
      case 'sessions': {
        const payload = ev.payload as { sessions?: SessionMeta[] } | SessionMeta[] | null
        const raw = Array.isArray(payload) ? payload : payload?.sessions
        // 后端异常/未就绪时可能下发非数组，忽略而不是让渲染树崩溃
        if (!Array.isArray(raw)) break
        // 只更新列表，不自动激活：activeSession 仅由用户切换/新建消息触发，
        // 否则启动时会被 list[0] 占位，与后端"惰性会话"状态不一致
        set({ sessions: raw as SessionMeta[] })
        break
      }
      case 'session': {
        const num = (ev.payload as { num?: number })?.num
        if (typeof num === 'number') set({ activeSession: num })
        break
      }
      case 'sessions_trashed': {
        // 回收站列表（trash_list 的回发），与任务树的 sessions 列表分开存放
        const payload = ev.payload as { sessions?: SessionMeta[] } | null
        if (Array.isArray(payload?.sessions)) set({ trashSessions: payload.sessions })
        break
      }
      case 'session_delete_result': {
        const payload = ev.payload as { deleted?: number[]; failed?: number[] } | null
        const deleted = payload?.deleted?.length ?? 0
        const failed = payload?.failed?.length ?? 0
        if (deleted > 0) showToast(`已彻底删除 ${deleted} 个会话`, 'info')
        if (failed > 0) showToast(`${failed} 个会话删除失败`, 'error', 4000)
        break
      }
      case 'session_history': {
        // 切换会话：后端回放该会话历史消息，整体替换当前消息流
        const payload = ev.payload as { num?: number; messages?: HistoryMessage[] } | null
        if (typeof payload?.num !== 'number' || !Array.isArray(payload.messages)) break
        set({
          activeSession: payload.num,
          messages: payload.messages.map((m, i) => ({
            id: `h${payload.num}_${i}`,
            role: m.role,
            content: m.content ?? '',
            thinking: m.thinking ?? '',
            toolCalls: (m.toolCalls ?? []).map((t, j) => ({
              id: `h${payload.num}_${i}_${j}`,
              name: t.name,
              args: t.args,
              status: 'done' as const
            })),
            activeToolId: null,
            streaming: false,
            usage: {}
          }))
        })
        break
      }
      case 'goal_status':
      case 'tasks':
      case 'skills':
        // 目标/待办/技能面板已并入设置弹窗，此三类事件不再单独展示
        break
      case 'llm_config': {
        const payload = ev.payload as { config?: LlmConfig; applied?: boolean; msg?: string }
        if (payload?.config) set({ llmConfig: payload.config })
        if (payload?.msg) showToast(payload.msg, 'info')
        break
      }
      case 'error': {
        const msg = (ev.payload as { msg?: string })?.msg ?? '未知错误'
        showToast(msg, 'error', 4000)
        break
      }
    }
  },

  refreshSessions: async () => {
    try {
      const list = (await window.agent.listSessions()) as SessionMeta[]
      // 后端未就绪时主进程会返回 { error: 'backend timeout' } 等非数组值，
      // 不校验会把对象当数组存入，导致 TaskTree 里 sessions.map 崩溃白屏
      if (!Array.isArray(list)) return
      set({ sessions: list })
    } catch {
      /* 忽略 */
    }
  },

  refreshTrash: async () => {
    try {
      const list = (await window.agent.listTrash()) as SessionMeta[]
      // 同 refreshSessions 的防御：非数组返回直接忽略
      if (!Array.isArray(list)) return
      set({ trashSessions: list })
    } catch {
      /* 后端未就绪时静默忽略 */
    }
  },

  /** 新建任务：纯前端行为——清空消息流、回到欢迎空态；jsonl 由首条消息发送时惰性创建 */
  newSession: () => {
    set({ messages: [], activeSession: null })
    return Promise.resolve()
  },
  switchSession: async (num) => {
    // 立即高亮并清空消息流，历史消息等后端 session_history 事件回放
    set({ activeSession: num, messages: [] })
    await window.agent.switchSession(num)
  },
  clearSession: async () => {
    await window.agent.clearSession()
    get().refreshSessions()
  },

  renameSession: async (num, title) => {
    const t = title.trim()
    if (!t) return
    try {
      await window.agent.renameSession(num, t)
    } catch {
      showToast('重命名失败', 'error', 4000)
    }
    // 成功路径由后端 sessions 事件刷新；这里兜底刷一次列表
    await get().refreshSessions()
  },
  trashSession: async (num) => {
    try {
      await window.agent.trashSession(num)
    } catch {
      showToast('删除失败', 'error', 4000)
      return
    }
    // 删除的是当前激活会话：回到欢迎空态（后端已置空会话态）
    if (get().activeSession === num) await get().newSession()
    await get().refreshSessions()
    await get().refreshTrash()
    showToast('已移入回收站', 'info')
  },
  restoreSession: async (num) => {
    try {
      await window.agent.restoreSession(num)
    } catch {
      showToast('还原失败', 'error', 4000)
      return
    }
    await get().refreshSessions()
    await get().refreshTrash()
    showToast('已还原会话', 'info')
  },
  deleteSessions: async (nums) => {
    if (nums.length === 0) return
    try {
      await window.agent.deleteSessions(nums)
    } catch {
      showToast('删除失败', 'error', 4000)
      return
    }
    // 结果 toast 由 session_delete_result 事件触发；这里刷新两个列表
    await get().refreshSessions()
    await get().refreshTrash()
  },

  openSettings: (tab = 'model') => {
    set({ settingsOpen: true, settingsTab: tab })
    // 打开模型页时拉取最新配置（服务商预置数据一并下发）
    if (tab === 'model' && !get().llmConfig) void get().loadLlConfig()
  },
  closeSettings: () => set({ settingsOpen: false }),
  loadLlConfig: async () => {
    try {
      const res = (await window.agent.llmConfigGet()) as { config?: LlmConfig } | null
      if (res?.config) set({ llmConfig: res.config })
    } catch {
      /* 后端未就绪时静默忽略 */
    }
  },
  saveLlConfig: async (config) => {
    set({ llmSaving: true })
    try {
      const res = (await window.agent.llmConfigSave(config)) as {
        config?: LlmConfig
        applied?: boolean
        msg?: string
      } | null
      if (res?.config) set({ llmConfig: res.config })
      if (res?.msg) showToast(res.msg, 'info')
      return res?.applied ?? false
    } catch {
      showToast('保存模型配置失败', 'error', 4000)
      return false
    } finally {
      set({ llmSaving: false })
    }
  },
  setActiveModel: async (id) => {
    const cfg = get().llmConfig
    if (!cfg) return
    await get().saveLlConfig({ active_model_id: id, models: cfg.models })
  },
  clearToast: () => set({ toast: null })
}))

/** 把一条流式增量事件合并进消息列表（增量只 append，形成打字机效果） */
function applyAgentEvent(state: AgentState, ev: AgentEvent): AgentState {
  let msgs = state.messages

  const ensureAssistant = (): string => {
    const existing = pendingAssistantId()
    if (existing) return existing
    const id = mid()
    msgs = [
      ...msgs,
      { id, role: 'assistant', content: '', thinking: '', toolCalls: [], activeToolId: null, streaming: true, usage: {} }
    ]
    return id
  }

  const current = (id: string): Message => msgs.find((m) => m.id === id) as Message

  switch (ev.type) {
    case 'thinking_delta': {
      const id = ensureAssistant()
      const m = current(id)
      msgs = mapMsg(msgs, id, { thinking: m.thinking + (ev.text ?? '') })
      break
    }
    case 'content_delta': {
      const id = ensureAssistant()
      const m = current(id)
      msgs = mapMsg(msgs, id, { content: m.content + (ev.text ?? '') })
      break
    }
    case 'tool_call_start': {
      const id = ensureAssistant()
      msgs = msgs.map((m) => {
        if (m.id !== id) return m
        const hasRunning = m.toolCalls.some((t) => t.status === 'running')
        if (hasRunning) return m
        const toolCalls = [
          ...m.toolCalls,
          { id: ev.tool_id || `t${Date.now()}`, name: ev.tool_name ?? '', args: ev.args ?? '', status: 'running' as const }
        ]
        return { ...m, toolCalls, activeToolId: toolCalls[toolCalls.length - 1].id }
      })
      break
    }
    case 'tool_call_delta':
      msgs = msgs.map((m) => {
        if (!m.activeToolId) return m
        return {
          ...m,
          toolCalls: m.toolCalls.map((t) =>
            t.id === m.activeToolId && t.status === 'running' ? { ...t, args: t.args + (ev.args ?? '') } : t
          )
        }
      })
      break
    case 'tool_call':
      msgs = msgs.map((m) => ({
        ...m,
        toolCalls: m.toolCalls.map((t) =>
          t.id === (ev.tool_id || '')
            ? { ...t, args: ev.args || t.args, name: ev.tool_name || t.name, status: 'done' as const }
            : t
        ),
        activeToolId: null
      }))
      break
    case 'turn_end':
      msgs = msgs.map((m) =>
        m.role === 'assistant' && m.streaming
          ? { ...m, streaming: false, usage: ev.usage ?? {}, activeToolId: null }
          : m
      )
      queueUnlock()
      break
  }
  return { ...state, messages: msgs }
}

function mapMsg(msgs: Message[], id: string, patch: Partial<Message>): Message[] {
  return msgs.map((m) => (m.id === id ? { ...m, ...patch } : m))
}

let unlockTick: ReturnType<typeof setTimeout> | undefined
function queueUnlock(): void {
  // 等事件批次结束再解锁输入，避免中间态误判
  clearTimeout(unlockTick)
  unlockTick = setTimeout(() => useAgentStore.setState({ isSending: false }), 60)
}