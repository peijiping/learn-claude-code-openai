import { create } from 'zustand'
import type { AgentEvent, ContextStats, HistoryMessage, ModelSwitch, SessionMeta, SessionModelOverrides, SessionModelOverridesMap, SessionRunStatus, TaskBoardSnapshot, TurnModelInfo, UiEvent, UsageStats, UsageStatsEventUsage, LlmConfig, LlmConfigPayload, LlmConnectionModel, LlmModel, LlmModelsResult } from '@protocols/agentProtocol'

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

/** 会话显示名：无标题（未生成/老会话）回退 session_<id> */
export function sessionDisplayName(s: SessionMeta): string {
  return s.title?.trim() || `session_${s.id}`
}

/** 把会话级覆盖（思考档位 + 标准/扩展上下文）解析成后端 chat payload 的 overrides。
 * maxContextOption 的 standard/extended 需结合模型元数据换算成具体窗口字符串。
 * 上下文窗口只要模型元数据可查就**始终显式携带**（未选择 = 标准窗口）：
 * 历史 bug——未选择时不上送 max_context，后端 set_max_context(None) 回落全局
 * env（如 MAX_CONTEXT_TOKENS=1M），统计/压缩阈值与所选模型真实窗口（如 128k）不符。
 * 思考档位仍只在用户显式选择时携带；覆盖参数按模型 id 保存在 overridesByModel
 * map 里，取绑定模型（modelId 缺省回落全局 active_model_id）对应条目；新建任务
 * （无会话号）同样携带：用户可在空态/新会话预设对话参数，随首条消息下发生效。 */
export function resolveOverridesPayload(
  llmConfig: LlmConfig | null,
  overridesByModel: SessionOverridesMap | null,
  modelId?: string | null
): { thinking_strength?: string; max_context?: string } | undefined {
  const modelOf = modelId || llmConfig?.active_model_id
  const overrides = (modelOf && overridesByModel?.[modelOf]) || undefined
  const payload: { thinking_strength?: string; max_context?: string } = {}
  let hasOverride = false
  if (overrides?.thinkingStrength) {
    hasOverride = true
    payload.thinking_strength = overrides.thinkingStrength
  }
  // 依据该模型元数据（max_context / max_context_extended）换算窗口字符串；
  // 元数据缺失（无窗口声明的模型）才不携带，后端走全局默认
  const activeModel = modelOf ? (llmConfig?.models ?? []).find((m) => m.id === modelOf) : undefined
  const meta = resolveModelMeta(llmConfig, activeModel)
  if (meta) {
    const option = overrides?.maxContextOption ?? 'standard'
    const window = option === 'extended'
      ? meta.max_context_extended
      : meta.max_context
    if (window) {
      hasOverride = true
      payload.max_context = window
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
  /** 思考过程内容（thinking_delta 流式累积） */
  thinking: string
  /** 思考过程是否正在流式输出：thinking_delta 期间 true，转入工具调用/块结束（sub_agent_end）后 false */
  thinkingActive: boolean
  toolCalls: ToolCallMsg[]
  activeToolId: string | null
  streaming: boolean
  /** 终态：running / done / error / aborted（进程被强杀只剩占位记录时为 running） */
  status?: 'running' | 'done' | 'error' | 'aborted'
  /** 执行耗时（毫秒；实时为 null，回放由后端记录补上） */
  durationMs?: number | null
  /** 失败原因（status=error 时非空） */
  error?: string
}

/** 消息 footer 的 token 统计：turn=本轮消耗（主 + 子智能体），
 *  session=turn 收尾时的会话级累计快照（实时事件携带；回放仅恢复 turn，缺省不显示第二段），
 *  model=本轮模型快照（usage_stats 事件 model 字段 / 回放 jsonl model_info 节点） */
export interface MessageUsage {
  turn: UsageStats
  session?: UsageStats
  model?: TurnModelInfo
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  /** 消息记录时间：回放来自 jsonl created_at，实时消息在创建时本地打点
   *  （秒级 ISO 本地时间，与后端 _now_iso 同构；老会话行缺省不显示） */
  created_at?: string
  thinking: string
  /** 思考过程是否正在流式输出：thinking_delta 期间 true，正文/工具调用/turn_end 后 false */
  thinkingActive: boolean
  toolCalls: ToolCallMsg[]
  /** 本消息内调用过的子智能体执行块（按后端 subagent_id 累积） */
  subagents: SubAgentMsg[]
  /** 本消息内发起过的 sub_agent 工具调用 id（实时锚点：子智能体事件据此
   *  挂回"发起它的那条 assistant 消息"，与回放规则一致，切会话不跳位） */
  subAgentToolIds?: string[]
  activeToolId: string | null
  streaming: boolean
  usage: MessageUsage | null
  /** 模型切换提示（空闲期 model_switch 事件 / 本轮 usage_stats.model.switch /
   *  回放 model_info.switch），挂到「切换发生时」那条 assistant 消息上 */
  switch?: ModelSwitch
}

interface AgentState {
  connection: ConnState
  python: PythonState
  /** 当前激活会话的消息投影（= messagesBySession[activeSession] ?? []），组件直接读取 */
  messages: Message[]
  /** 每个会话各自的独立消息缓冲（单一事实源），多会话并发各自累积、互不覆盖；
   *  键为会话 id（短随机串 / 存量编号字符串） */
  messagesBySession: Record<string, Message[]>
  /** 正在执行 turn 的会话 id 集合（脉冲运行指示 + 停止按钮状态） */
  runningSessions: string[]
  /** turn 已结束但后台任务（如后台子智能体）仍在执行的会话 id 集合（脉冲运行指示，无停止按钮） */
  bgSessions: string[]
  /** 新建任务（activeSession==null）首条消息的临时草稿缓冲，后端回发 session id 后迁移 */
  pendingFresh: Message[] | null
  sessions: SessionMeta[]
  trashSessions: SessionMeta[]
  activeSession: string | null
  isSending: boolean
  settingsOpen: boolean
  settingsTab: SettingsTab
  llmConfig: LlmConfig | null
  llmSaving: boolean
  /** 当前激活会话的上下文统计（每轮 turn_end / 切会话时后端下发） */
  currentContextStats: ContextStats | null
  /** 各会话的 token 消耗累计（usage_stats 事件 / session_history.usage_totals 写入；
   *  圆圈 tooltip 数据源，按会话 id 键控，多会话互不覆盖） */
  sessionUsageBySession: Record<string, UsageStats>
  /** 每个会话当前的任务面板快照（task_board 事件**整份替换**，键控会话 id）。
   *  null = 该会话当前没有未完成任务组（面板不显示）。
   *  注意 session_history 到来时会先置 null 再等随后的 task_board 覆盖 ——
   *  否则"切走再切回"会残留上一轮那版 done 快照。 */
  taskBoardBySession: Record<string, TaskBoardSnapshot | null>
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
  switchSession: (sessionId: string) => Promise<void>
  setSessionUnread: (sessionId: string, unread?: boolean) => Promise<void>
  clearSession: () => Promise<void>
  renameSession: (sessionId: string, title: string) => Promise<void>
  trashSession: (sessionId: string) => Promise<void>
  restoreSession: (sessionId: string) => Promise<void>
  deleteSessions: (ids: string[]) => Promise<void>
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

/** 本地时间秒级 ISO（与后端 _now_iso 同构：2026-09-18T10:30:00），
 *  实时消息创建时打点；回放时以 jsonl created_at 为准 */
function nowLocalIso(): string {
  const d = new Date()
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/** 去重追加（不可变数组） */
function addUnique(arr: string[], n: string): string[] {
  return arr.includes(n) ? arr : [...arr, n]
}

/** 就地更新某会话的 unread 标记（值无变化时返回原数组，避免触发重渲染） */
function patchSessionUnread(sessions: SessionMeta[], sessionId: string, unread: boolean): SessionMeta[] {
  if (!sessions.some((x) => x.id === sessionId && x.unread !== unread)) return sessions
  return sessions.map((x) => (x.id === sessionId ? { ...x, unread } : x))
}

function historyToMessage(sid: string, hist: HistoryMessage[]): Message[] {
  return hist.map((m, i) => {
    // 子智能体卡片：优先用后端 role=subagent 挂载的完整记录；若缺失（老会话/
    // 数据未落盘），则从主 toolCalls 里的 sub_agent 调用派生一张基础卡片，
    // 保证回放时 sub_agent 永远以卡片形式展示（与流式执行一致），绝不以普通工具条出现。
    const backendSubs = (m.subagents ?? []).map((s, k) => ({
      id: s.id,
      name: s.name,
      thinking: s.thinking ?? '',
      thinkingActive: false,
      // 工具 id 优先用后端给出的 tool_id（实时/回放同一 id，便于按 id 归位）
      toolCalls: (s.toolCalls ?? []).map((t, l) => ({
        id: t.tool_id || `h${sid}_${i}_s${k}_${l}`,
        name: t.name,
        args: t.args,
        status: t.status === 'running' ? ('running' as const) : ('done' as const)
      })),
      activeToolId: null,
      streaming: false,
      // 终态与耗时来自旁路记录；旧数据缺字段时按"已完成"处理
      status: (s.status as SubAgentMsg['status']) ?? (s.error ? 'error' : 'done'),
      durationMs: s.durationMs ?? null,
      error: s.error ?? ''
    }))
    // 主 toolCalls 里的 sub_agent 调用 → 从中派生兜底卡片（含 prompt 作为名称），并从 toolCalls 剥离
    const subCalls = (m.toolCalls ?? []).filter((t) => t.name === 'sub_agent')
    const derivedSubs = subCalls.map((call, k) => ({
      id: `h${sid}_${i}_submain_${k}`,
      name: subAgentNameFromArgs(call.args),
      thinking: '',
      thinkingActive: false,
      toolCalls: [],
      activeToolId: null,
      streaming: false
    }))
    const subagents = backendSubs.length ? backendSubs : derivedSubs
    const normalCalls = (m.toolCalls ?? []).filter((t) => t.name !== 'sub_agent')
    return {
      id: `h${sid}_${i}`,
      role: m.role,
      content: m.content ?? '',
      // 回放：消息记录时间来自 jsonl created_at（老行缺省 → 右下角不显示）
      created_at: m.created_at ?? undefined,
      thinking: m.thinking ?? '',
      thinkingActive: false,
      toolCalls: normalCalls.map((t, j) => ({
        id: `h${sid}_${i}_${j}`,
        name: t.name,
        args: t.args,
        status: t.status === 'running' ? ('running' as const) : ('done' as const)
      })),
      activeToolId: null,
      streaming: false,
      subagents,
      // 回放：jsonl 轮末 assistant 行携带的 usage / model_info / usage_session →
      // footer 第一段（本轮 + 本轮模型）+ 第二段「本会话累计」（usage_session 快照，
      // 与实时 usage_stats 同构，老会话缺省不显示第二段）
      usage: m.usage && m.usage.total_tokens
        ? {
            turn: m.usage,
            model: m.model_info ?? undefined,
            session: m.usage_session ?? undefined
          }
        : null,
      // 回放：空闲期/本轮切换提示，落到「切换发生时」的 assistant 消息上
      switch: m.model_info?.switch ?? undefined
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
      { id, role: 'assistant', content: '', thinking: '', thinkingActive: false, toolCalls: [], subagents: [], activeToolId: null, streaming: true, usage: null, created_at: nowLocalIso() }
    ]
    return id
  }

  const current = (id: string): Message => msgs.find((m) => m.id === id) as Message

  switch (ev.type) {
    case 'thinking_delta': {
      const id = ensureAssistant()
      const m = current(id)
      msgs = mapMsg(msgs, id, { thinking: m.thinking + (ev.text ?? ''), thinkingActive: true })
      break
    }
    case 'content_delta': {
      const id = ensureAssistant()
      const m = current(id)
      msgs = mapMsg(msgs, id, { content: m.content + (ev.text ?? ''), thinkingActive: false })
      break
    }
    case 'tool_call_start': {
      const id = ensureAssistant()
      // sub_agent 调用不进主工具条：子智能体展示统一由 SubAgentBlock 卡片承载，
      // 避免普通工具条与卡片并存/重复。但要**记录锚点**（本消息发起过该
      // tool_call_id）——子智能体事件到达时据此把卡片挂回这条 assistant
      //（唯一锚点规则），与回放挂载一致 → 切换会话前后卡片位置不跳变。
      if (ev.tool_name === 'sub_agent') {
        const tcid = ev.tool_id || ''
        if (!tcid) return msgs
        msgs = msgs.map((m) =>
          m.id === id ? { ...m, subAgentToolIds: [...(m.subAgentToolIds ?? []), tcid] } : m
        )
        return msgs
      }
      msgs = msgs.map((m) => {
        if (m.id !== id) return m
        const hasRunning = m.toolCalls.some((t) => t.status === 'running')
        if (hasRunning) return { ...m, thinkingActive: false }
        const toolCalls = [
          ...m.toolCalls,
          { id: ev.tool_id || `t${Date.now()}`, name: ev.tool_name ?? '', args: ev.args ?? '', status: 'running' as const }
        ]
        return { ...m, toolCalls, activeToolId: toolCalls[toolCalls.length - 1].id, thinkingActive: false }
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
        activeToolId: null,
        thinkingActive: false
      }))
      break
    case 'turn_end':
      // usage 不在此写：轮级/会话级统计由随后的 usage_stats 事件统一携带
      //（避免显示"最后一次 LLM 调用"的错误数字）
      msgs = msgs.map((m) =>
        m.role === 'assistant' && m.streaming
          ? { ...m, streaming: false, activeToolId: null, thinkingActive: false }
          : m
      )
      break
    case 'model_switch': {
      // 空闲期切换：把切换提示挂到「切换时最后一条 assistant 消息」（即当前
      // 缓冲末尾那条 assistant）上，先于用户下一条指令展示；无 assistant 不挂。
      const sw = ev.switch
      if (!sw || sw.from_id === sw.to_id) break
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role !== 'assistant') continue
        msgs = msgs.map((m, idx) => (idx === i ? { ...m, switch: sw } : m))
        break
      }
      break
    }
  }
  return msgs
}

/** 子智能体事件：路由进「发起它的那条 assistant 消息」的子智能体块（与主消息互不干扰）。
 * 块不存在时先创建（sub_agent_start / 首个 thinking / 首个工具事件都能触发）。 */
function applySubagentEvent(msgs: Message[], ev: AgentEvent): Message[] {
  const subId = ev.subagent_id ?? ''
  if (!subId) return msgs

  // 唯一锚点规则：卡片挂在发起它的那条 assistant 下，实时与回放共用同一规则。
  //   1) 该 subagent_id 的块已存在 → 沿用其所属消息（最高优先级：同一子智能体的
  //      所有事件必须永远落在同一条 assistant 下。子智能体后续的 thinking/tool
  //      事件不带发起方 tool_call_id，只有靠这一步才不会在主智能体进入下一轮后
  //      被挂到别的 assistant 上）；
  //   2) 首次创建：按发起方 tool_call_id（sub_agent_start 携带）定位所属 assistant；
  //   3) 回退：末尾一条 assistant（含已结束的——后台子智能体完成时主 turn 往往
  //      已结束，此时不该新建空气泡）；
  //   4) 兜底：新建气泡（仅在会话缓冲被回放整体替换、锚点丢失时）。
  const ensureAssistant = (): string => {
    const owner = msgs.find((m) => m.subagents.some((s) => s.id === subId))
    if (owner) return owner.id
    const tcid = ev.tool_id || ''
    if (tcid) {
      const byTcid = msgs.find(
        (m) => m.role === 'assistant' && (m.subAgentToolIds ?? []).includes(tcid)
      )
      if (byTcid) return byTcid.id
    }
    const last = msgs[msgs.length - 1]
    if (last && last.role === 'assistant') return last.id
    const id = mid()
    msgs = [
      ...msgs,
      { id, role: 'assistant', content: '', thinking: '', thinkingActive: false, toolCalls: [], subagents: [], activeToolId: null, streaming: true, usage: null, created_at: nowLocalIso() }
    ]
    return id
  }
  const ensureSubagent = (msgId: string, name: string): [Message[], SubAgentMsg] => {
    const m = msgs.find((x) => x.id === msgId) as Message
    const existing = m.subagents.find((s) => s.id === subId)
    if (existing) return [msgs, existing]
    const block: SubAgentMsg = { id: subId, name, thinking: '', thinkingActive: false, toolCalls: [], activeToolId: null, streaming: true, status: 'running' }
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
      msgs = patchSub(id, { thinking: block.thinking + (ev.text ?? ''), thinkingActive: true })
      break
    }
    case 'tool_call_start': {
      const id = ensureAssistant()
      const block = ensureSubagent(id, '子智能体')[1]
      const tid = ev.tool_id || ''
      // 按 tool_id 归位：同一工具只建一行，并行工具调用各自成行
      //（旧实现用"是否已有 running"守卫，并行时会漏建/错位）
      if (tid && block.toolCalls.some((t) => t.id === tid)) {
        msgs = patchSub(id, { thinkingActive: false })
        break
      }
      const toolCalls = [
        ...block.toolCalls,
        { id: tid || `t${Date.now()}`, name: ev.tool_name ?? '', args: ev.args ?? '', status: 'running' as const }
      ]
      msgs = patchSub(id, { toolCalls, activeToolId: toolCalls[toolCalls.length - 1].id, thinkingActive: false })
      break
    }
    case 'tool_call_delta': {
      const id = ensureAssistant()
      const tid = ev.tool_id || ''
      // 按 tool_id 归位（而非"当前 active 的那条"），并行工具调用才不会串台
      msgs = msgs.map((m) =>
        m.id === id
          ? {
              ...m,
              subagents: m.subagents.map((s) =>
                s.id === subId
                  ? {
                      ...s,
                      toolCalls: s.toolCalls.map((t) =>
                        t.id === tid && t.status === 'running'
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
                      activeToolId: null,
                      thinkingActive: false
                    }
                  : s
              )
            }
          : m
      )
      break
    }
    case 'tool_exec_start':
    case 'tool_exec_end': {
      // 工具「真正执行」的生命周期：tool_call（流聚合完成）把工具标 done 后，
      // 实际执行此刻才开始 —— 执行开始拨回 running，执行结束再标 done。
      // 缺了这两个事件，执行阶段（往往最耗时）卡片会完全冻结（2026-09-12 修复）。
      const id = ensureAssistant()
      const tid = ev.tool_id || ''
      if (!tid) break
      const running = ev.type === 'tool_exec_start'
      msgs = msgs.map((m) => {
        if (m.id !== id) return m
        return {
          ...m,
          subagents: m.subagents.map((s) => {
            if (s.id !== subId) return s
            const exists = s.toolCalls.some((t) => t.id === tid)
            const toolCalls = exists
              ? s.toolCalls.map((t) =>
                  t.id === tid
                    ? {
                        ...t,
                        status: (running ? 'running' : 'done') as ToolCallMsg['status'],
                        name: ev.tool_name || t.name
                      }
                    : t
                )
              : [
                  ...s.toolCalls,
                  { id: tid, name: ev.tool_name ?? '', args: ev.args ?? '', status: (running ? 'running' : 'done') as ToolCallMsg['status'] }
                ]
            return { ...s, toolCalls, activeToolId: running ? tid : s.activeToolId }
          })
        }
      })
      break
    }
    case 'sub_agent_end': {
      const id = ensureAssistant()
      msgs = msgs.map((m) => {
        if (m.id !== id) return m
        const subagents = m.subagents.map((s) =>
          s.id === subId
            ? { ...s, streaming: false, thinkingActive: false, status: s.error ? s.status : ('done' as const) }
            : s
        )
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
  sessionUsageBySession: {},
  taskBoardBySession: {},
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
    const sid = get().activeSession
    const modelId = get().sessionModelId
    const ov = resolveOverridesPayload(get().llmConfig, get().overridesByModel, modelId)
    const userMsg: Message = {
      id: mid(), role: 'user', content: t, thinking: '', thinkingActive: false, toolCalls: [], subagents: [], activeToolId: null, streaming: false, usage: null, created_at: nowLocalIso()
    }
    const assMsg: Message = {
      id: mid(), role: 'assistant', content: '', thinking: '', thinkingActive: false, toolCalls: [], subagents: [], activeToolId: null, streaming: true, usage: null, created_at: nowLocalIso()
    }
    set((s) => {
      // 新建任务（尚无会话 id）：首条消息进临时草稿缓冲，等后端 session 信封迁移
      if (sid === null) {
        const pendingFresh = [userMsg, assMsg]
        return { ...s, pendingFresh, messages: pendingFresh, isSending: true }
      }
      const buf = s.messagesBySession[sid] ?? []
      const messagesBySession = { ...s.messagesBySession, [sid]: [...buf, userMsg, assMsg] }
      const messages = messagesBySession[sid]
      return { ...s, messagesBySession, messages, isSending: true }
    })
    // 发送实际交给后端：fresh 时后端生成短 id 并回发 session 信封，前端据此迁移草稿
    window.agent.send(t, sid, ov, modelId).catch(() => set({ isSending: false }))
  },

  stop: () => {
    const sid = get().activeSession
    const clearStreaming = (m: Message): Message =>
      m.role === 'assistant' && m.streaming ? { ...m, streaming: false, activeToolId: null } : m
    if (sid === null) {
      // 新建任务草稿态：仅本地清流式标记（后端会话尚未建立，无需 stop 命令）
      set((s) => {
        if (!s.pendingFresh) return s
        const pendingFresh = s.pendingFresh.map(clearStreaming)
        return { ...s, pendingFresh, messages: pendingFresh, isSending: false }
      })
      return
    }
    // 真实停止：通知后端只停当前显示会话这一轮（其它后台会话不受影响）
    window.agent.stop(sid)
    set((s) => {
      const buf = (s.messagesBySession[sid] ?? []).map(clearStreaming)
      return {
        ...s,
        messagesBySession: { ...s.messagesBySession, [sid]: buf },
        messages: s.activeSession === sid ? buf : s.messages,
        runningSessions: s.runningSessions.filter((n) => n !== sid),
        isSending: false
      }
    })
  },

  handleEvent: (ev) => {
    if (ev.kind === 'event') {
      // 按 session_id 路由到对应会话缓冲；后台会话增量各自累积，显示会话投影实时更新
      const aev = ev.payload as AgentEvent
      const sid = aev.session_id
      if (typeof sid !== 'string' || !sid) return
      set((s) => {
        const next = applyAgentEventBuffer(s.messagesBySession[sid] ?? [], aev)
        let messagesBySession = { ...s.messagesBySession, [sid]: next }
        let messages = s.activeSession === sid ? next : s.messages
        // token 消耗统计：session 级写入圆圈 tooltip 数据源；带 turn 时同步写入
        // 该会话末条 assistant 消息 footer（{turn, session} 快照，回放同构）。
        // turn 缺省 = 后台子智能体迟到完成的补发（只刷 tooltip，不动 footer）。
        let sessionUsageBySession = s.sessionUsageBySession
        if (aev.type === 'usage_stats') {
          const u = aev.usage as UsageStatsEventUsage | undefined
          if (u && u.session && u.session.total_tokens !== undefined) {
            sessionUsageBySession = { ...sessionUsageBySession, [sid]: u.session }
            if (u.turn && u.turn.total_tokens) {
              const buf = messagesBySession[sid] ?? []
              for (let i = buf.length - 1; i >= 0; i--) {
                if (buf[i].role !== 'assistant') continue
                const patched = [...buf]
                patched[i] = {
                  ...patched[i],
                  usage: { turn: u.turn, session: u.session, model: u.model },
                  switch: u.model?.switch ?? patched[i].switch
                }
                messagesBySession = { ...messagesBySession, [sid]: patched }
                messages = s.activeSession === sid ? patched : s.messages
                break
              }
            }
          }
        }
        return { ...s, messagesBySession, messages, sessionUsageBySession }
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
        const sid = (ev.payload as { session_id?: string })?.session_id
        if (typeof sid !== 'string' || !sid) break
        const wasFresh = get().pendingFresh !== null
        set((s) => {
          // 新建任务的草稿缓冲迁移到正式会话缓冲（拿到后端分配的会话 id）
          let messagesBySession = s.messagesBySession
          if (s.pendingFresh !== null && s.activeSession !== sid) {
            messagesBySession = { ...messagesBySession, [sid]: s.pendingFresh }
          }
          return {
            ...s,
            activeSession: sid,
            pendingFresh: null,
            messagesBySession,
            messages: messagesBySession[sid] ?? [],
            isSending: s.runningSessions.includes(sid)
          }
        })
        // 新建会话由首条消息落号：把当前选定的模型与按模型参数覆盖写入该会话元数据
        //（用 UI 形状 map，便于切回/新会话按模型独立恢复；chat 透传的已是换算后的
        // 单轮 resolved overrides，不写元数据）。
        if (wasFresh) {
          window.agent.setSessionModel({
            session_id: sid,
            model_id: get().sessionModelId,
            overrides: toBackendOverrides(get().overridesByModel)
          }).catch(() => {})
        }
        break
      }
      case 'session_status': {
        const p = ev.payload as { session_id: string; status: SessionRunStatus }
        // 会话完整结束（done/stopped）：用户当前不在查看它 → 标记未读并持久化；
        // 正在查看它 → 保持/置为已读（未读语义 = 「有新产出但还没点到它」）。
        if (p.status === 'done' || p.status === 'stopped') {
          const unread = p.session_id !== get().activeSession
          void get().setSessionUnread(p.session_id, unread)
        }
        set((s) => {
          // running：turn 执行中（脉冲点 + 停止按钮）；background：turn 已结束
          // 但后台任务仍在执行（脉冲点，无停止按钮）；done/stopped：全部复位。
          // 未读/已读状态由元数据（sessions[].unread）持久化驱动，不做前端内存态。
          const runningSessions =
            p.status === 'running'
              ? addUnique(s.runningSessions, p.session_id)
              : s.runningSessions.filter((n) => n !== p.session_id)
          const bgSessions =
            p.status === 'background'
              ? addUnique(s.bgSessions, p.session_id)
              : p.status === 'done' || p.status === 'stopped'
                ? s.bgSessions.filter((n) => n !== p.session_id)
                : s.bgSessions
          return {
            ...s,
            runningSessions,
            bgSessions,
            isSending: s.activeSession !== null && runningSessions.includes(s.activeSession)
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
        const payload = ev.payload as { deleted?: string[]; failed?: string[] } | null
        const deleted = payload?.deleted?.length ?? 0
        const failed = payload?.failed?.length ?? 0
        if (deleted > 0) showToast(`已彻底删除 ${deleted} 个会话`, 'info')
        if (failed > 0) showToast(`${failed} 个会话删除失败`, 'error', 4000)
        break
      }
      case 'session_history': {
        const payload = ev.payload as { session_id?: string; messages?: HistoryMessage[]; model_id?: string | null; overrides?: SessionModelOverridesMap | null; usage_totals?: UsageStats | null } | null
        if (typeof payload?.session_id !== 'string' || !payload.session_id || !Array.isArray(payload.messages)) break
        set((s) => {
          // 回调内 payload 的窄化丢失，重断言为已校验形状
          const p = payload as { session_id: string; messages: HistoryMessage[]; model_id?: string | null; overrides?: SessionModelOverridesMap | null; usage_totals?: UsageStats | null }
          // 任务面板：先把本会话 board 清空，等紧随其后的 task_board 事件覆盖。
          // 必须清 —— 后端回放只发"未完成组"，已结束的组不再下发；不清的话
          // "看到完成的组 → 切走 → 切回"会残留上一轮那版 done 快照，
          // 违反"会话切换/复现时仅显示正在执行的组"。
          const taskBoardBySession = { ...s.taskBoardBySession, [p.session_id]: null }
          // 运行中（turn 或后台任务）的会话以实时缓冲为准，不回放磁盘快照
          // （避免丢失未落盘/已后台产出的分流增量）
          const buf = s.messagesBySession[p.session_id] ?? []
          const hasLive =
            (s.runningSessions.includes(p.session_id) || s.bgSessions.includes(p.session_id)) &&
            buf.length > 0
          if (hasLive) return { ...s, taskBoardBySession }
          const histBuf = historyToMessage(p.session_id, p.messages)
          const messagesBySession = { ...s.messagesBySession, [p.session_id]: histBuf }
          const messages = s.activeSession === p.session_id ? histBuf : s.messages
          // 会话级累计从元数据恢复（null=老会话无统计，清除避免残留旧值）
          let sessionUsageBySession = s.sessionUsageBySession
          if (p.usage_totals) {
            sessionUsageBySession = { ...sessionUsageBySession, [p.session_id]: p.usage_totals }
          } else {
            const { [p.session_id]: _drop, ...rest } = sessionUsageBySession
            sessionUsageBySession = rest
          }
          // 切到 / 打开该会话时，按元数据恢复其绑定的模型与按模型参数覆盖
          const overridesByModel = fromBackendOverrides(p.overrides) ?? {}
          if (s.activeSession !== p.session_id) {
            return { ...s, messagesBySession, messages, sessionUsageBySession, taskBoardBySession }
          }
          return {
            ...s,
            messagesBySession,
            messages,
            sessionUsageBySession,
            sessionModelId: p.model_id || s.sessionModelId,
            overridesByModel,
            taskBoardBySession,
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
        const p = ev.payload as { session_id: string } & ContextStats
        // 仅当是本会话（当前显示会话）时更新，避免后台会话统计串台
        if (p.session_id !== get().activeSession) break
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
      case 'task_board': {
        const p = ev.payload as { session_id?: string; board?: TaskBoardSnapshot | null } | null
        const sid = p?.session_id
        if (typeof sid !== 'string' || !sid) break
        set((s) => {
          const prev = s.taskBoardBySession[sid]
          const next = p?.board ?? null
          // 同组内丢弃乱序/过期快照：后台子智能体在 daemon 线程里改任务，
          // 多线程推送可能乱序到达；revision 组内单调递增，更小的直接丢。
          if (prev && next && prev.group_id === next.group_id && next.revision < prev.revision) {
            return s
          }
          return { ...s, taskBoardBySession: { ...s.taskBoardBySession, [sid]: next } }
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
  switchSession: async (sid) => {
    // 进入会话 = 已读。先本地即时置已读（即时反馈），再持久化到后端元数据。
    void get().setSessionUnread(sid, false)
    // 立即高亮 + 切换到该会话缓冲（后台会话继续执行不受影响，仅换投影）。
    // 模型/参数不在此处清空：由后端回发的 session_history 按元数据异步恢复。
    set((s) => ({
      activeSession: sid,
      pendingFresh: null,
      messages: s.messagesBySession[sid] ?? [],
      isSending: s.runningSessions.includes(sid)
    }))
    // 后端回放该会话历史并刷新列表；运行中的话由实时缓冲覆盖（见 session_history 处理）
    await window.agent.switchSession(sid)
  },
  /** 标记某会话未读/已读：本地即时生效 + 后端写入元数据持久化（跨窗口/重启随 sessions 同步）。
   *  进入会话=已读；非当前查看的会话完整结束后置未读。调用各处通过 get().setSessionUnread 触发。 */
  setSessionUnread: async (sessionId: string, unread?: boolean) => {
    set((s) => ({ sessions: patchSessionUnread(s.sessions, sessionId, Boolean(unread)) }))
    try {
      await window.agent.setSessionUnread({ session_id: sessionId, unread: Boolean(unread) })
    } catch {
      /* 后端未就绪时忽略；sessions 重播时会以元数据为准校准 */
    }
  },
  clearSession: async () => {
    const sid = get().activeSession
    if (sid === null) return
    await window.agent.clearSession()
    set((s) => {
      const messagesBySession = { ...s.messagesBySession }
      delete messagesBySession[sid]
      // 清空会话同步清掉 token 统计（后端 meta 的 usage_totals 已一并清除）
      const { [sid]: _drop, ...sessionUsageBySession } = s.sessionUsageBySession
      return { ...s, messagesBySession, messages: [], activeSession: sid, sessionUsageBySession }
    })
    get().refreshSessions()
  },

  renameSession: async (sid, title) => {
    const t = title.trim()
    if (!t) return
    try {
      await window.agent.renameSession(sid, t)
    } catch {
      showToast('重命名失败', 'error', 4000)
    }
    await get().refreshSessions()
  },
  trashSession: async (sid) => {
    try {
      await window.agent.trashSession(sid)
    } catch {
      showToast('删除失败', 'error', 4000)
      return
    }
    if (get().activeSession === sid) await get().newSession()
    await get().refreshSessions()
    await get().refreshTrash()
    showToast('已移入回收站', 'info')
  },
  restoreSession: async (sid) => {
    try {
      await window.agent.restoreSession(sid)
    } catch {
      showToast('还原失败', 'error', 4000)
      return
    }
    await get().refreshSessions()
    await get().refreshTrash()
    showToast('已还原会话', 'info')
  },
  deleteSessions: async (ids) => {
    if (ids.length === 0) return
    let deleted: string[] = []
    try {
      const res = (await window.agent.deleteSessions(ids)) as {
        deleted?: string[]
        failed?: string[]
      } | null
      deleted = res?.deleted ?? ids
    } catch {
      showToast('删除失败', 'error', 4000)
      return
    }
    if (deleted.length > 0) {
      // 本地增量移除被删会话（后端已不再全量广播 sessions），
      // 避免删除后重建整张列表（逐个重数 message_count）造成的刷新延迟
      const remove = new Set(deleted)
      set((s) => ({
        sessions: s.sessions.filter((x) => !remove.has(x.id)),
        trashSessions: s.trashSessions.filter((x) => !remove.has(x.id)),
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
    const sid = get().activeSession
    if (sid !== null) {
      // 选模型的会话级持久化：写会话元数据；无会话（新建预设）由首条 chat 落号后持久化
      window.agent.setSessionModel({
        session_id: sid,
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
    const sid = get().activeSession
    if (sid !== null) {
      window.agent.setSessionModel({
        session_id: sid,
        model_id: get().sessionModelId,
        overrides: toBackendOverrides(next)
      }).catch(() => {})
    }
  },
  clearToast: () => set({ toast: null })
}))