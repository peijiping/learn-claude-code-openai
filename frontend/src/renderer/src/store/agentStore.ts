import { create } from 'zustand'
import type { AgentEvent, ContextStats, HistoryMessage, SessionMeta, SessionModelOverrides, SessionModelOverridesMap, SessionRunStatus, UiEvent, LlmConfig, LlmConfigPayload, LlmConnectionModel, LlmModel, LlmModelsResult } from '@protocols/agentProtocol'

// 会话级请求覆盖（模型下拉悬浮配置面板改动，仅本会话生效）
export interface SessionOverrides {
  /** 思考强度档位：low / high / very_high */
  thinkingStrength?: string
  /** 上下文选项：standard 标准 / extended 扩展（映射到模型元数据的窗口大小） */
  maxContextOption?: 'standard' | 'extended'
}

/** 按模型 id 分别保存的会话级参数覆盖（每个模型各自独立，互不串改） */
export type SessionOverridesMap = Record<string, SessionOverrides>

export type ConnState = 'connecting' | 'connected' | 'disconnected'
export type PythonState = 'starting' | 'running' | 'crashed' | 'stopped'
export type SettingsTab = 'general' | 'model' | 'trash' | 'about'

/** 会话显示名：无标题（未生成/老会话）回退 session_N */
export function sessionDisplayName(s: SessionMeta): string {
  return s.title?.trim() || `session_${s.num}`
}

/** 把会话级覆盖（思考档位 + 标准/扩展上下文）解析成后端 chat payload 的 overrides。
 * maxContextOption 的 standard/extended 需结合模型元数据换算成具体窗口字符串。
 * 覆盖参数按模型 id 保存在 overridesByModel map 里，取绑定模型（modelId 缺省回落
 * 全局 active_model_id）对应条目；新建任务（无会话号）同样携带：用户可在空态/新会话
 * 预设对话参数，随首条消息下发生效。 */
export function resolveOverridesPayload(
  llmConfig: LlmConfig | null,
  overridesByModel: SessionOverridesMap | null,
  modelId?: string | null
): { thinking_strength?: string; max_context?: string } | undefined {
  if (!overridesByModel) return undefined
  const modelOf = modelId || llmConfig?.active_model_id
  const overrides = (modelOf && overridesByModel[modelOf]) || undefined
  if (!overrides) return undefined
  const payload: { thinking_strength?: string; max_context?: string } = {}
  let hasOverride = false
  if (overrides.thinkingStrength) {
    hasOverride = true
    payload.thinking_strength = overrides.thinkingStrength
  }
  if (overrides.maxContextOption) {
    // 依据该模型元数据（max_context / max_context_extended）换算窗口字符串
    const activeModel = (llmConfig?.models ?? []).find((m) => m.id === modelOf)
    const meta = resolveModelMeta(llmConfig, activeModel)
    if (meta) {
      const window = overrides.maxContextOption === 'extended'
        ? meta.max_context_extended
        : meta.max_context
      if (window) {
        hasOverride = true
        payload.max_context = window
      }
    }
  }
  return hasOverride ? payload : undefined
}

/** 解析某模型的元数据（窗口 / 思考档位）：
 * 模型自身字段优先（后端归一化时已从预置目录继承过来，自定义模型则来自手动填写），
 * 回落预置目录（~/.aigent/providers.json）。都拿不到时返回 null（不渲染悬浮面板）。 */
export function resolveModelMeta(
  llmConfig: LlmConfig | null,
  model: (LlmModel | LlmConnectionModel) | null | undefined
): { max_context?: string; max_context_extended?: string; thinking_strengths?: string[]; default_thinking?: string } | null {
  if (!model) return null
  const providerKey = (model as LlmModel).provider
  const preset = providerKey
    ? llmConfig?.providers?.[providerKey]?.models?.find((p) => p.id === (model.model || model.id))
    : undefined
  const meta = {
    max_context: model.max_context ?? preset?.max_context,
    max_context_extended: model.max_context_extended ?? preset?.max_context_extended,
    thinking_strengths: model.thinking_strengths ?? preset?.thinking_strengths,
    default_thinking: model.default_thinking ?? preset?.default_thinking,
  }
  if (!meta.max_context && !meta.max_context_extended && !meta.thinking_strengths) return null
  return meta
}

/** 厂商小圆点样式类：预置厂商用专属配色，自定义厂商留空（走中性样式）。 */
export function providerDot(provider: string | undefined | null): string {
  if (provider === 'deepseek') return 'dp'
  if (provider === 'siliconflow') return 'sf'
  return ''
}

/** SessionOverridesMap（UI 形状，按模型 id）→ 后端元数据存储形状（按模型 id 的 thinking_strength / max_context_option） */
export function toBackendOverrides(
  ovMap: SessionOverridesMap | null | undefined
): SessionModelOverridesMap | undefined {
  if (!ovMap) return undefined
  const out: SessionModelOverridesMap = {}
  for (const [modelId, ov] of Object.entries(ovMap)) {
    const item: SessionModelOverrides = {}
    if (ov.thinkingStrength) item.thinking_strength = ov.thinkingStrength
    if (ov.maxContextOption) item.max_context_option = ov.maxContextOption
    if (Object.keys(item).length) out[modelId] = item
  }
  return Object.keys(out).length ? out : undefined
}

/** 后端元数据存储形状（按模型 id）→ SessionOverridesMap（UI 形状），空则返回 null */
export function fromBackendOverrides(
  ovMap: SessionModelOverridesMap | null | undefined
): SessionOverridesMap | null {
  if (!ovMap) return null
  const out: SessionOverridesMap = {}
  for (const [modelId, item] of Object.entries(ovMap)) {
    const ov: SessionOverrides = {}
    if (item.thinking_strength) ov.thinkingStrength = item.thinking_strength
    if (item.max_context_option) ov.maxContextOption = item.max_context_option
    if (Object.keys(ov).length) out[modelId] = ov
  }
  return Object.keys(out).length ? out : null
}

export interface ToolCallMsg {
  id: string
  name: string
  args: string
  status: 'running' | 'done'
}

/** 子智能体执行块：挂在 assistant 消息下，展示其思考过程与工具执行（可折叠） */
export interface SubAgentMsg {
  /** 后端下发的子任务 id（事件按此路由） */
  id: string
  name: string
  thinking: string
  toolCalls: ToolCallMsg[]
  activeToolId: string | null
  streaming: boolean
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  thinking: string
  toolCalls: ToolCallMsg[]
  /** 本消息内调用过的子智能体执行块（按后端 subagent_id 累积） */
  subagents: SubAgentMsg[]
  activeToolId: string | null
  streaming: boolean
  usage: Record<string, number>
}

interface AgentState {
  connection: ConnState
  python: PythonState
  /** 当前激活会话的消息投影（= messagesBySession[activeSession] ?? []），组件直接读取 */
  messages: Message[]
  /** 每个会话各自的独立消息缓冲（单一事实源），多会话并发各自累积、互不覆盖 */
  messagesBySession: Record<number, Message[]>
  /** 正在执行 turn 的会话号集合（脉冲运行指示 + 停止按钮状态） */
  runningSessions: number[]
  /** turn 已结束但后台任务（如后台子智能体）仍在执行的会话号集合（脉冲运行指示，无停止按钮） */
  bgSessions: number[]
  /** 后台完成且尚未查看的会话号集合（侧边栏绿点未读） */
  completedBg: number[]
  /** 新建任务（activeSession==null）首条消息的临时草稿缓冲，后端回发 session 号后迁移 */
  pendingFresh: Message[] | null
  sessions: SessionMeta[]
  trashSessions: SessionMeta[]
  activeSession: number | null
  isSending: boolean
  settingsOpen: boolean
  settingsTab: SettingsTab
  llmConfig: LlmConfig | null
  llmSaving: boolean
  /** 当前激活会话的上下文统计（每轮 turn_end / 切会话时后端下发） */
  currentContextStats: ContextStats | null
  /** 当前激活会话的按模型参数覆盖（仅本会话生效，不写配置；按模型 id 分别保存） */
  overridesByModel: SessionOverridesMap
  /** 当前激活会话（或新建任务）绑定/选择的模型 id（区别于全局 active_model_id） */
  sessionModelId: string | null
  /** 上一次会话/选择留下的模型 id 与按模型参数，供新建会话继承 */
  lastSessionModelId: string | null
  lastOverridesByModel: SessionOverridesMap
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
  saveLlConfig: (config: LlmConfigPayload) => Promise<boolean>
  /** 刷新某连接可用模型列表（GET {base_url}{models_path}），返回模型 id 列表（失败返回空） */
  fetchModels: (payload: {
    base_url?: string
    api_key?: string
    connection_id?: string
    api_format?: string
    models_path?: string
  }) => Promise<string[]>
  setActiveModel: (id: string) => Promise<void>
  setSessionModel: (id: string) => void
  setSessionOverrides: (overrides: SessionOverrides | null, modelId: string) => void
  clearToast: () => void
}

let msgSeq = 0
const mid = (): string => `m${++msgSeq}`

/** 去重追加（不可变数组） */
function addUnique(arr: number[], n: number): number[] {
  return arr.includes(n) ? arr : [...arr, n]
}

function historyToMessage(num: number, hist: HistoryMessage[]): Message[] {
  return hist.map((m, i) => {
    // 子智能体卡片：优先用后端 role=subagent 挂载的完整记录；若缺失（老会话/
    // 数据未落盘），则从主 toolCalls 里的 sub_agent 调用派生一张基础卡片，
    // 保证回放时 sub_agent 永远以卡片形式展示（与流式执行一致），绝不以普通工具条出现。
    const backendSubs = (m.subagents ?? []).map((s, k) => ({
      id: s.id,
      name: s.name,
      thinking: s.thinking ?? '',
      toolCalls: (s.toolCalls ?? []).map((t, l) => ({
        id: `h${num}_${i}_s${k}_${l}`,
        name: t.name,
        args: t.args,
        status: t.status === 'running' ? ('running' as const) : ('done' as const)
      })),
      activeToolId: null,
      streaming: false
    }))
    // 主 toolCalls 里的 sub_agent 调用 → 从中派生兜底卡片（含 prompt 作为名称），并从 toolCalls 剥离
    const subCalls = (m.toolCalls ?? []).filter((t) => t.name === 'sub_agent')
    const derivedSubs = subCalls.map((call, k) => ({
      id: `h${num}_${i}_submain_${k}`,
      name: subAgentNameFromArgs(call.args),
      thinking: '',
      toolCalls: [],
      activeToolId: null,
      streaming: false
    }))
    const subagents = backendSubs.length ? backendSubs : derivedSubs
    const normalCalls = (m.toolCalls ?? []).filter((t) => t.name !== 'sub_agent')
    return {
      id: `h${num}_${i}`,
      role: m.role,
      content: m.content ?? '',
      thinking: m.thinking ?? '',
      toolCalls: normalCalls.map((t, j) => ({
        id: `h${num}_${i}_${j}`,
        name: t.name,
        args: t.args,
        status: t.status === 'running' ? ('running' as const) : ('done' as const)
      })),
      activeToolId: null,
      streaming: false,
      subagents,
      usage: {}
    }
  })
}

/** 从 sub_agent 工具调用参数中提取可读名称作为卡片标题（无参数解析失败时回退到「子智能体」） */
function subAgentNameFromArgs(args: string): string {
  try {
    const parsed = JSON.parse(args || '{}')
    const prompt = (parsed.prompt || parsed.task || '').trim()
    if (prompt) return prompt.length > 48 ? prompt.slice(0, 48) + '…' : prompt
  } catch {
    /* 参数非 JSON，走回退名 */
  }
  return '子智能体'
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

/** 把一条会话内流式增量事件合并进「指定会话」的缓冲（纯函数，增量 append 形成打字机效果）。
 * 带 subagent_id 的事件（子智能体发出）路由到 applySubagentEvent，折叠进子智能体块；
 * 其余按主消息原逻辑处理。 */
function applyAgentEventBuffer(buffer: Message[], ev: AgentEvent): Message[] {
  let msgs = buffer

  // 子智能体事件单独分流（思考/工具/生命周期都进对应子智能体块）
  if (ev.subagent_id) {
    return applySubagentEvent(msgs, ev)
  }

  const ensureAssistant = (): string => {
    const last = msgs[msgs.length - 1]
    if (last && last.role === 'assistant' && last.streaming) return last.id
    const id = mid()
    msgs = [
      ...msgs,
      { id, role: 'assistant', content: '', thinking: '', toolCalls: [], subagents: [], activeToolId: null, streaming: true, usage: {} }
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
      // sub_agent 调用不进主工具条：子智能体展示统一由 SubAgentBlock 卡片承载
      //（sub_agent_start 事件创建），避免普通工具条与卡片并存/重复。
      if (ev.tool_name === 'sub_agent') return msgs
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
      break
  }
  return msgs
}

/** 子智能体事件：路由进「最后一条 assistant 消息」的子智能体块（与主消息互不干扰）。
 * 块不存在时先创建（sub_agent_start / 首个 thinking / 首个工具事件都能触发）。 */
function applySubagentEvent(msgs: Message[], ev: AgentEvent): Message[] {
  const subId = ev.subagent_id ?? ''
  if (!subId) return msgs

  const ensureAssistant = (): string => {
    const last = msgs[msgs.length - 1]
    if (last && last.role === 'assistant' && last.streaming) return last.id
    const id = mid()
    msgs = [
      ...msgs,
      { id, role: 'assistant', content: '', thinking: '', toolCalls: [], subagents: [], activeToolId: null, streaming: true, usage: {} }
    ]
    return id
  }
  const ensureSubagent = (msgId: string, name: string): [Message[], SubAgentMsg] => {
    const m = msgs.find((x) => x.id === msgId) as Message
    const existing = m.subagents.find((s) => s.id === subId)
    if (existing) return [msgs, existing]
    const block: SubAgentMsg = { id: subId, name, thinking: '', toolCalls: [], activeToolId: null, streaming: true }
    msgs = msgs.map((x) => (x.id === msgId ? { ...x, subagents: [...x.subagents, block] } : x))
    return [msgs, block]
  }
  const patchSub = (msgId: string, patch: Partial<SubAgentMsg>): Message[] =>
    msgs.map((m) =>
      m.id === msgId
        ? { ...m, subagents: m.subagents.map((s) => (s.id === subId ? { ...s, ...patch } : s)) }
        : m
    )

  switch (ev.type) {
    case 'sub_agent_start': {
      const id = ensureAssistant()
      msgs = ensureSubagent(id, ev.text || '子智能体')[0]
      break
    }
    case 'thinking_delta': {
      const id = ensureAssistant()
      const block = ensureSubagent(id, '子智能体')[1]
      msgs = patchSub(id, { thinking: block.thinking + (ev.text ?? '') })
      break
    }
    case 'tool_call_start': {
      const id = ensureAssistant()
      const block = ensureSubagent(id, '子智能体')[1]
      const hasRunning = block.toolCalls.some((t) => t.status === 'running')
      if (!hasRunning) {
        const toolCalls = [
          ...block.toolCalls,
          { id: ev.tool_id || `t${Date.now()}`, name: ev.tool_name ?? '', args: ev.args ?? '', status: 'running' as const }
        ]
        msgs = patchSub(id, { toolCalls, activeToolId: toolCalls[toolCalls.length - 1].id })
      }
      break
    }
    case 'tool_call_delta': {
      const id = ensureAssistant()
      msgs = msgs.map((m) =>
        m.id === id
          ? {
              ...m,
              subagents: m.subagents.map((s) =>
                s.id === subId && s.activeToolId
                  ? {
                      ...s,
                      toolCalls: s.toolCalls.map((t) =>
                        t.id === s.activeToolId && t.status === 'running'
                          ? { ...t, args: t.args + (ev.args ?? '') }
                          : t
                      )
                    }
                  : s
              )
            }
          : m
      )
      break
    }
    case 'tool_call': {
      const id = ensureAssistant()
      msgs = msgs.map((m) =>
        m.id === id
          ? {
              ...m,
              subagents: m.subagents.map((s) =>
                s.id === subId
                  ? {
                      ...s,
                      toolCalls: s.toolCalls.map((t) =>
                        t.id === (ev.tool_id || '')
                          ? { ...t, args: ev.args || t.args, name: ev.tool_name || t.name, status: 'done' as const }
                          : t
                      ),
                      activeToolId: null
                    }
                  : s
              )
            }
          : m
      )
      break
    }
    case 'sub_agent_end': {
      const id = ensureAssistant()
      msgs = msgs.map((m) => {
        if (m.id !== id) return m
        const subagents = m.subagents.map((s) => (s.id === subId ? { ...s, streaming: false } : s))
        // 后台子智能体场景：消息可能仅为装载子智能体块而建（无正文/思考/工具），
        // 全部块结束后同步收起其流式态，避免留下永久光标。
        // 前台场景该消息必有主层 tool_calls/正文，不受影响（由主 turn_end 收尾）。
        const blockOnly = !m.content && !m.thinking && m.toolCalls.length === 0
        const allDone = subagents.every((s) => !s.streaming)
        return { ...m, subagents, streaming: blockOnly && allDone ? false : m.streaming }
      })
      break
    }
  }
  return msgs
}

function mapMsg(msgs: Message[], id: string, patch: Partial<Message>): Message[] {
  return msgs.map((m) => (m.id === id ? { ...m, ...patch } : m))
}

export const useAgentStore = create<AgentState>((set, get) => ({
  connection: 'disconnected',
  python: 'stopped',
  messages: [],
  messagesBySession: {},
  runningSessions: [],
  bgSessions: [],
  completedBg: [],
  pendingFresh: null,
  sessions: [],
  trashSessions: [],
  activeSession: null,
  isSending: false,
  settingsOpen: false,
  settingsTab: 'model',
  llmConfig: null,
  llmSaving: false,
  currentContextStats: null,
  overridesByModel: {},
  sessionModelId: null,
  lastSessionModelId: null,
  lastOverridesByModel: {},
  toast: null,
  toastType: 'info',

  setConnection: (c) =>
    set((s) => {
      if (c === s.connection) return s
      // 断线重连（→ connected）：清空陈旧运行态。后端会在新连接上重放
      // 仍在运行会话的 session_status（running/background），重新点亮真实
      // 运行指示；清空防止断连期间的状态残留（如永远转圈的僵尸会话）。
      if (c === 'connected' && s.connection !== 'connected') {
        return { ...s, connection: c, runningSessions: [], bgSessions: [], isSending: false }
      }
      return { ...s, connection: c }
    }),
  setPython: (p) => set({ python: p }),

  send: (text) => {
    const t = text.trim()
    if (!t || get().isSending) return
    const num = get().activeSession
    const modelId = get().sessionModelId
    const ov = resolveOverridesPayload(get().llmConfig, get().overridesByModel, modelId)
    const userMsg: Message = {
      id: mid(), role: 'user', content: t, thinking: '', toolCalls: [], subagents: [], activeToolId: null, streaming: false, usage: {}
    }
    const assMsg: Message = {
      id: mid(), role: 'assistant', content: '', thinking: '', toolCalls: [], subagents: [], activeToolId: null, streaming: true, usage: {}
    }
    set((s) => {
      // 新建任务（尚无会话号）：首条消息进临时草稿缓冲，等后端 session 信封迁移
      if (num === null) {
        const pendingFresh = [userMsg, assMsg]
        return { ...s, pendingFresh, messages: pendingFresh, isSending: true }
      }
      const buf = s.messagesBySession[num] ?? []
      const messagesBySession = { ...s.messagesBySession, [num]: [...buf, userMsg, assMsg] }
      const messages = messagesBySession[num]
      return { ...s, messagesBySession, messages, isSending: true }
    })
    // 发送实际交给后端：fresh 时后端领号并回发 session 信封，前端据此迁移草稿
    window.agent.send(t, num, ov, modelId).catch(() => set({ isSending: false }))
  },

  stop: () => {
    const num = get().activeSession
    const clearStreaming = (m: Message): Message =>
      m.role === 'assistant' && m.streaming ? { ...m, streaming: false, activeToolId: null } : m
    if (num === null) {
      // 新建任务草稿态：仅本地清流式标记（后端会话尚未建立，无需 stop 命令）
      set((s) => {
        if (!s.pendingFresh) return s
        const pendingFresh = s.pendingFresh.map(clearStreaming)
        return { ...s, pendingFresh, messages: pendingFresh, isSending: false }
      })
      return
    }
    // 真实停止：通知后端只停当前显示会话这一轮（其它后台会话不受影响）
    window.agent.stop(num)
    set((s) => {
      const buf = (s.messagesBySession[num] ?? []).map(clearStreaming)
      return {
        ...s,
        messagesBySession: { ...s.messagesBySession, [num]: buf },
        messages: s.activeSession === num ? buf : s.messages,
        runningSessions: s.runningSessions.filter((n) => n !== num),
        isSending: false
      }
    })
  },

  handleEvent: (ev) => {
    if (ev.kind === 'event') {
      // 按 session_num 路由到对应会话缓冲；后台会话增量各自累积，显示会话投影实时更新
      const aev = ev.payload as AgentEvent
      const num = aev.session_num
      if (typeof num !== 'number') return
      set((s) => {
        const next = applyAgentEventBuffer(s.messagesBySession[num] ?? [], aev)
        const messagesBySession = { ...s.messagesBySession, [num]: next }
        const messages = s.activeSession === num ? next : s.messages
        return { ...s, messagesBySession, messages }
      })
      return
    }
    switch (ev.kind) {
      case 'sessions': {
        const payload = ev.payload as { sessions?: SessionMeta[] } | SessionMeta[] | null
        const raw = Array.isArray(payload) ? payload : payload?.sessions
        if (!Array.isArray(raw)) break
        set({ sessions: raw as SessionMeta[] })
        break
      }
      case 'session': {
        const num = (ev.payload as { num?: number })?.num
        if (typeof num !== 'number') break
        const wasFresh = get().pendingFresh !== null
        set((s) => {
          // 新建任务的草稿缓冲迁移到正式会话缓冲（拿到后端分配的会话号）
          let messagesBySession = s.messagesBySession
          if (s.pendingFresh !== null && s.activeSession !== num) {
            messagesBySession = { ...messagesBySession, [num]: s.pendingFresh }
          }
          return {
            ...s,
            activeSession: num,
            pendingFresh: null,
            messagesBySession,
            messages: messagesBySession[num] ?? [],
            isSending: s.runningSessions.includes(num)
          }
        })
        // 新建会话由首条消息落号：把当前选定的模型与按模型参数覆盖写入该会话元数据
        //（用 UI 形状 map，便于切回/新会话按模型独立恢复；chat 透传的已是换算后的
        // 单轮 resolved overrides，不写元数据）。
        if (wasFresh) {
          window.agent.setSessionModel({
            num,
            model_id: get().sessionModelId,
            overrides: toBackendOverrides(get().overridesByModel)
          }).catch(() => {})
        }
        break
      }
      case 'session_status': {
        const p = ev.payload as { num: number; status: SessionRunStatus }
        set((s) => {
          // running：turn 执行中（脉冲点 + 停止按钮）；background：turn 已结束
          // 但后台任务仍在执行（脉冲点，无停止按钮）；done/stopped：全部复位
          const runningSessions =
            p.status === 'running'
              ? addUnique(s.runningSessions, p.num)
              : s.runningSessions.filter((n) => n !== p.num)
          const bgSessions =
            p.status === 'background'
              ? addUnique(s.bgSessions, p.num)
              : p.status === 'done' || p.status === 'stopped'
                ? s.bgSessions.filter((n) => n !== p.num)
                : s.bgSessions
          let completedBg = s.completedBg
          if (p.status === 'done' || p.status === 'stopped') {
            // 执行完成（后台任务也结束后）且当前显示的不是它 → 绿点未读；切到该会话即清除
            if (p.num !== s.activeSession) completedBg = addUnique(completedBg, p.num)
            else completedBg = completedBg.filter((n) => n !== p.num)
          }
          return {
            ...s,
            runningSessions,
            bgSessions,
            completedBg,
            isSending: runningSessions.includes(s.activeSession ?? -1)
          }
        })
        break
      }
      case 'sessions_trashed': {
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
        const payload = ev.payload as { num?: number; messages?: HistoryMessage[]; model_id?: string | null; overrides?: SessionModelOverridesMap | null } | null
        if (typeof payload?.num !== 'number' || !Array.isArray(payload.messages)) break
        set((s) => {
          // 回调内 payload 的窄化丢失，重断言为已校验形状
          const p = payload as { num: number; messages: HistoryMessage[]; model_id?: string | null; overrides?: SessionModelOverridesMap | null }
          // 运行中（turn 或后台任务）的会话以实时缓冲为准，不回放磁盘快照
          // （避免丢失未落盘/已后台产出的分流增量）
          const buf = s.messagesBySession[p.num] ?? []
          const hasLive =
            (s.runningSessions.includes(p.num) || s.bgSessions.includes(p.num)) &&
            buf.length > 0
          if (hasLive) return s
          const histBuf = historyToMessage(p.num, p.messages)
          const messagesBySession = { ...s.messagesBySession, [p.num]: histBuf }
          const messages = s.activeSession === p.num ? histBuf : s.messages
          // 切到 / 打开该会话时，按元数据恢复其绑定的模型与按模型参数覆盖
          const overridesByModel = fromBackendOverrides(p.overrides) ?? {}
          if (s.activeSession !== p.num) {
            return { ...s, messagesBySession, messages }
          }
          return {
            ...s,
            messagesBySession,
            messages,
            sessionModelId: p.model_id || s.sessionModelId,
            overridesByModel,
            lastSessionModelId: p.model_id || s.lastSessionModelId,
            lastOverridesByModel: overridesByModel,
          }
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
      case 'context_stats': {
        const p = ev.payload as { num: number } & ContextStats
        // 仅当是本会话（当前显示会话）时更新，避免后台会话统计串台
        if (p.num !== get().activeSession) break
        set({
          currentContextStats: {
            used_tokens: p.used_tokens,
            max_tokens: p.max_tokens,
            used_percent: p.used_percent,
            max_label: p.max_label,
          }
        })
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
      if (!Array.isArray(list)) return
      set({ sessions: list })
    } catch {
      /* 忽略 */
    }
  },

  refreshTrash: async () => {
    try {
      const list = (await window.agent.listTrash()) as SessionMeta[]
      if (!Array.isArray(list)) return
      set({ trashSessions: list })
    } catch {
      /* 后端未就绪时静默忽略 */
    }
  },

  /** 新建任务：纯前端行为——清空当前显示与草稿、回到欢迎空态；jsonl 由首条消息发送时惰性创建。
   * 继承上一会话最后选择的模型与按模型参数覆盖（主流智能体行为），随首条消息持久化进新会话元数据。 */
  newSession: () => {
    set((s) => ({
      messages: [],
      activeSession: null,
      pendingFresh: null,
      isSending: false,
      currentContextStats: null,
      sessionModelId: s.lastSessionModelId,
      overridesByModel: s.lastOverridesByModel
    }))
    return Promise.resolve()
  },
  switchSession: async (num) => {
    // 立即高亮 + 切换到该会话缓冲（后台会话继续执行不受影响，仅换投影）。
    // 模型/参数不在此处清空：由后端回发的 session_history 按元数据异步恢复。
    set((s) => ({
      activeSession: num,
      pendingFresh: null,
      completedBg: s.completedBg.filter((n) => n !== num),
      messages: s.messagesBySession[num] ?? [],
      isSending: s.runningSessions.includes(num)
    }))
    // 后端回放该会话历史并刷新列表；运行中的话由实时缓冲覆盖（见 session_history 处理）
    await window.agent.switchSession(num)
  },
  clearSession: async () => {
    const num = get().activeSession
    if (num === null) return
    await window.agent.clearSession()
    set((s) => {
      const messagesBySession = { ...s.messagesBySession }
      delete messagesBySession[num]
      return { ...s, messagesBySession, messages: [], activeSession: num }
    })
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
    await get().refreshSessions()
  },
  trashSession: async (num) => {
    try {
      await window.agent.trashSession(num)
    } catch {
      showToast('删除失败', 'error', 4000)
      return
    }
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
    let deleted: number[] = []
    try {
      const res = (await window.agent.deleteSessions(nums)) as {
        deleted?: number[]
        failed?: number[]
      } | null
      deleted = res?.deleted ?? nums
    } catch {
      showToast('删除失败', 'error', 4000)
      return
    }
    if (deleted.length > 0) {
      // 本地增量移除被删会话（后端已不再全量广播 sessions），
      // 避免删除后重建整张列表（逐个重数 message_count）造成的刷新延迟
      const remove = new Set(deleted)
      set((s) => ({
        sessions: s.sessions.filter((x) => !remove.has(x.num)),
        trashSessions: s.trashSessions.filter((x) => !remove.has(x.num)),
      }))
    }
  },

  openSettings: (tab = 'model') => {
    set({ settingsOpen: true, settingsTab: tab })
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
  fetchModels: async (payload) => {
    try {
      const res = (await window.agent.llmModelsFetch(payload)) as LlmModelsResult | null
      if (!res) {
        showToast('获取模型列表超时', 'error', 4000)
        return []
      }
      if (!res.ok) {
        showToast(res.error || '获取模型列表失败', 'error', 5000)
        return []
      }
      return (res.models ?? []).map((m) => m.id).filter(Boolean)
    } catch {
      showToast('获取模型列表失败', 'error', 4000)
      return []
    }
  },
  setActiveModel: async (id) => {
    const cfg = get().llmConfig
    if (!cfg) return
    await get().saveLlConfig({ active_model_id: id, connections: cfg.connections ?? [] })
  },
  setSessionModel: (id) => {
    set({ sessionModelId: id, lastSessionModelId: id })
    const num = get().activeSession
    if (num !== null) {
      // 选模型的会话级持久化：写会话元数据；无会话（新建预设）由首条 chat 落号后持久化
      window.agent.setSessionModel({
        num,
        model_id: id,
        overrides: toBackendOverrides(get().overridesByModel)
      }).catch(() => {})
    }
  },
  setSessionOverrides: (overrides, modelId) => {
    // 仅更新指定模型的参数覆盖，其余模型不受影响（消除跨模型串改）
    const next = { ...get().overridesByModel }
    const last = { ...get().lastOverridesByModel }
    if (overrides && Object.keys(overrides).length) {
      next[modelId] = overrides
      last[modelId] = overrides
    } else {
      delete next[modelId]
      delete last[modelId]
    }
    set({ overridesByModel: next, lastOverridesByModel: last })
    const num = get().activeSession
    if (num !== null) {
      window.agent.setSessionModel({
        num,
        model_id: get().sessionModelId,
        overrides: toBackendOverrides(next)
      }).catch(() => {})
    }
  },
  clearToast: () => set({ toast: null })
}))