import { create } from 'zustand'
import type { AgentEvent, SessionMeta, UiEvent, LlmConfig } from '@protocols/agentProtocol'

export type ConnState = 'connecting' | 'connected' | 'disconnected'
export type PythonState = 'starting' | 'running' | 'crashed' | 'stopped'
export type SettingsTab = 'general' | 'model' | 'about'

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
  newSession: () => Promise<void>
  switchSession: (num: number) => Promise<void>
  clearSession: () => Promise<void>
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

export const useAgentStore = create<AgentState>((set, get) => ({
  connection: 'disconnected',
  python: 'stopped',
  messages: [],
  sessions: [],
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
    set((s) => ({
      messages: [
        ...s.messages,
        { id: mid(), role: 'user', content: t, thinking: '', toolCalls: [], activeToolId: null, streaming: false, usage: {} },
        { id: mid(), role: 'assistant', content: '', thinking: '', toolCalls: [], activeToolId: null, streaming: true, usage: {} }
      ],
      isSending: true
    }))
    window.agent.send(t).catch(() => set({ isSending: false }))
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
        const list = raw as SessionMeta[]
        set((s) => ({ sessions: list, activeSession: s.activeSession ?? list[0]?.num ?? null }))
        break
      }
      case 'session': {
        const num = (ev.payload as { num?: number })?.num
        if (typeof num === 'number') set({ activeSession: num })
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
        if (payload?.msg) set({ toast: payload.msg, toastType: 'info' })
        break
      }
      case 'error': {
        const msg = (ev.payload as { msg?: string })?.msg ?? '未知错误'
        set({ toast: msg, toastType: 'error' })
        setTimeout(() => get().clearToast(), 4000)
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
      set((s) => ({ sessions: list, activeSession: s.activeSession ?? list[0]?.num ?? null }))
    } catch {
      /* 忽略 */
    }
  },

  newSession: async () => {
    await window.agent.newSession()
    get().refreshSessions()
  },
  switchSession: async (num) => {
    set({ activeSession: num })
    await window.agent.switchSession(num)
  },
  clearSession: async () => {
    await window.agent.clearSession()
    get().refreshSessions()
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
      if (res?.msg) set({ toast: res.msg, toastType: 'info' })
      return res?.applied ?? false
    } catch {
      set({ toast: '保存模型配置失败', toastType: 'error' })
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