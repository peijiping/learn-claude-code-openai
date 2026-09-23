import { create } from 'zustand'
// 右栏（2026-09-23）：本文件是**单向**依赖右栏 store 的调用方 ——
// `rightPanelStore` 绝不反向 import 本文件（那会形成循环依赖，见其文件头警告）。
// 这里只用到三件事：接收 `session_history` 里的 right_panel、切会话时收尾、
// 删会话/断线重连时清缓存。
import { flushRightPanelPending, useRightPanelStore } from './rightPanelStore'
import type { AgentEvent, ApprovalDecision, ApprovalInfo, ApprovalOutcome, AskAnswer, AskQuestion, AskStatus, AttachmentKind, AttachmentRef, AttachmentsStagedPayload, ChatAttachmentInput, ContextStats, HistoryAskUser, HistoryMessage, MessageRef, ModelSwitch, PermissionConfig, PermissionConfigResult, PermissionMode, ProjectMeta, ProjectsPayload, RefInput, RPanelPersist, SandboxConfigResult, SandboxConfigSavePayload, SessionMeta, SessionModelOverrides, SessionModelOverridesMap, SessionRunStatus, StagedAttachment, TaskBoardSnapshot, TurnModelInfo, UiEvent, UsageStats, UsageStatsEventUsage, LlmConfig, LlmConfigPayload, LlmConnectionModel, LlmModel, LlmModelsResult } from '@protocols/agentProtocol'

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
export type SettingsTab = 'general' | 'model' | 'permission' | 'sandbox' | 'trash' | 'about'

/** 会话显示名：无标题（未生成/老会话）回退 session_<id> */
export function sessionDisplayName(s: SessionMeta): string {
  return s.title?.trim() || `session_${s.id}`
}

/** 默认工作空间 id（后端常量同值；前端用它判"是否默认空间"与兜底分组） */
export const DEFAULT_PROJECT_ID = 'default'

/** 工作空间显示名：按 id 查名称；查不到（列表未到/已删除）回退 id 本身 */
export function projectDisplayName(projects: ProjectMeta[], id?: string | null): string {
  if (!id) return '默认'
  return projects.find((p) => p.id === id)?.name ?? (id === DEFAULT_PROJECT_ID ? '默认' : id)
}

/** 会话所属工作空间 id（存量会话缺字段 → default） */
export function sessionProjectId(s: SessionMeta): string {
  return s.project || DEFAULT_PROJECT_ID
}

/** 合并 `permission_config` 回执（get / save 两种回执各只带一半字段）。
 *
 * **不能整份替换**：get 回执带 `builtin`/`path`/`exists`，save 回执带 `applied`/`warnings`/`msg`。
 * 整份覆盖会让保存后 `builtin` 变 `undefined` → 权限页 `if (!builtin)` 兜底分支直接塌回
 * 「读取权限配置…」加载态（用户点一次保存页面就没了）。
 *
 * 反向也要清：get 回执不带 `applied`，若沿用上一次 save 的 `applied/warnings`，
 * 「保存成功但被调整」的提示会挂在新加载的干净配置上（假告警）。 */
function mergePermissionResult(
  prev: PermissionConfigResult | null,
  next: PermissionConfigResult
): PermissionConfigResult {
  const base: PermissionConfigResult = { ...(prev ?? {}), ...next }
  if (next.applied === undefined) {
    return { ...base, applied: undefined, warnings: undefined, msg: undefined }
  }
  return base
}

/** 每个空间默认最多展示的会话条数（超出折叠，末尾给"展开全部"入口） */
export const SESSION_PREVIEW_LIMIT = 15

/** 展开态持久化（按空间 id 记；缺省 = 展开） */
const EXPANDED_KEY = 'aigent.workspace.expanded'
const PREVIEW_KEY = 'aigent.workspace.previewExpanded'

function loadFlagMap(key: string): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, boolean>) : {}
  } catch {
    return {}
  }
}

function saveFlagMap(key: string, map: Record<string, boolean>): void {
  try {
    localStorage.setItem(key, JSON.stringify(map))
  } catch {
    /* 隐私模式等场景忽略：持久化失败不影响本次会话内的展开态 */
  }
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
 * 回落预置目录（~/.aigent/config/providers.json）。都拿不到时返回 null（不渲染悬浮面板）。 */
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
  /** 审批结算元数据（2026-09-22 权限管控）：实时路径由 `approval_resolved` 旁挂、
   *  回放路径由 HistoryToolCall.approval 映射而来。有值 → 工具条渲染审批徽标。
   *  允许结局（allowed_*）只有实时路径可见（后端不落盘允许，回放自然没有）。 */
  approval?: ApprovalInfo
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

/** 结构化提问（ask_user）在 assistant 消息下的**只读小结块**数据。
 *
 *  两条路径产出同一形状：
 *  - **实时**：`tool_call_start` 时先建块（status='pending'，此期间**不渲染**，
 *    由输入框上方的面板承载）→ `tool_call`（终态参数）补 questions →
 *    `ask_resolved` 补 resultText/status（离开 pending → 渲染）。
 *  - **回放**：assistant 行的 `askUsers[]` 一次到位（status 由后端反推；
 *    resultText 为空 = 'incomplete'，如进程被杀）。
 *
 *  `resultText` 是**唯一展示载体**（与回填给模型的 tool_result 逐字节相同）：
 *  前端**不做任何结构化解析**，只按行原样展示（`white-space: pre-wrap`）。 */
export interface AskUserMsg {
  /** 配对键：发起它的 tool_call id（实时/回放同一 id） */
  toolCallId: string
  /** 问题（实时取自 ask_request / 工具参数；回放由 `args` 解析）。解析失败为空数组 */
  questions: AskQuestion[]
  /** 后端 broker 生成的 result_text；空串 = 未完成 */
  resultText: string
  /** 'pending' = 实时在途（不渲染）；'incomplete' = 回放发现未完成 */
  status: AskStatus | 'pending' | 'incomplete'
  /** 发起本次提问时**本条消息正文的长度**（2026-09-22，实时路径在 `tool_call_start`
   *  那一刻记录；回放缺省）。
   *
   *  用途：把正文切回它真正的位置。模型总是"先说一句话（先跟你确认几个关键项），
   *  再提问"—— 这句话必须显示在提问卡片**上方**；而同一个气泡里提问之后续写的
   *  正文（实时路径整轮合并进一条消息）仍留在卡片下方（见 MessageItem 的分段规则）。
   *
   *  缺省（回放路径）= 正文全在提问之前：jsonl 每次 LLM 调用一行，正文只可能出自
   *  发起该 tool_call 的那一次响应，必然在提问之前，不存在"提问后还有正文"的同一行。 */
  contentOffset?: number
}

/** 输入框上方「待确认」面板的实时状态（一个会话至多一条在途提问）。
 *  纯 UI 态、不落盘 —— 由 `ask_request` 建立、`ask_resolved` 清除；
 *  **不从 session_history 恢复**（重连时后端用 `_ask_snapshot_lines()` 重放）。 */
export interface AskInteraction {
  requestId: string
  /** 发起它的 tool_call id（与消息下的只读小结块配对） */
  toolCallId: string
  questions: AskQuestion[]
}

/** 消息流中「在途审批卡片」的数据（2026-09-22 权限管控，范式 C）。
 *  与 AskInteraction 同为纯 UI 态、不落盘 —— 由 `approval_request` 建立、
 *  `approval_resolved` 清除；**不从 session_history 恢复**（断线重连时后端用
 *  `_approval_snapshot_lines()` 重放在途审批，同 ask 机制）。
 *  后端同会话至多一条在途审批，但仍按 request_id 键控存储 —— 迟到/乱序的
 *  approval_resolved 只清自己那一条，且重连重放不会重置已点击的按钮。 */
export interface ApprovalInteraction {
  requestId: string
  /** 发起审批的 tool_call id（卡片按它锚定到消息流的工具条） */
  toolCallId: string
  toolName: string
  /** 工具参数对象（后端已把超长字符串值截断到 600 字，前端原样展示摘要） */
  args: Record<string, unknown>
  /** 触发类型（dangerous_pattern / bash_not_allowed / …） */
  trigger: string
  /** 给用户看的原因说明（后端生成，原样展示） */
  reason: string
  /** 「本次会话内允许」按钮的副文案（点前知道会记住什么账） */
  sessionScopeHint: string
  /** 发起审批时会话的权限档位 */
  mode: string
  /** 后端自结算超时（秒）；卡片据此显示倒计时 */
  timeoutSeconds: number
  /** 审批创建时间戳（秒，后端 created_at） */
  createdAt: number
  /** 用户已点击某个决定（命令已 fire-and-forget 发出，等广播收卡）：
   *  置位后三按钮禁用；重连重放的 approval_request **不重置**它。 */
  submitted: boolean
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
  /** 本消息发起过的结构化提问（ask_user）：消息下方只读小结块的数据源。
   *  与 subagents 同理，ask_user 调用**不进普通工具条**（避免与面板/小结块重复），
   *  但必须记在消息上 —— 实时与回放共用同一挂载规则，切会话不跳位。 */
  askUsers?: AskUserMsg[]
  activeToolId: string | null
  streaming: boolean
  usage: MessageUsage | null
  /** 模型切换提示（空闲期 model_switch 事件 / 本轮 usage_stats.model.switch /
   *  回放 model_info.switch），挂到「切换发生时」那条 assistant 消息上 */
  switch?: ModelSwitch
  /** user 消息携带的附件（实时由草稿项转成，回放由后端 harvest 而来）。
   *  图片经 `attachmentUrl()` 转成自定义协议 URL 显示缩略图。 */
  attachments?: AttachmentRef[]
  /** user 消息引用的工作空间路径（实时由 `serializeDoc` 收集，回放由后端 harvest）。
   *  **只有引用没有正文也是合法发送**。与 attachments 是并列且独立的通道。 */
  refs?: MessageRef[]
}

/** 输入区的附件草稿项。
 *  staging = 已提交给后端、等 `attachments_staged` 回填（用 pendingPath 配对）；
 *  ready = 后端已复制并解析完成，可随消息发送；failed = 该文件被拒（不可发送）。 */
export interface DraftAttachment {
  /** 列表 key：staging 期用本地 id（同一文件选两次不会撞），就绪后换成 att_id */
  key: string
  /** staging = 已提交给后端、等 `attachments_staged` 回填（用 pendingPath 配对）；
   *  ready = 后端已复制并解析完成，可随消息发送；
   *  degraded = 同样可发送，但后端给了 `warnings`（扫描件 / 截断 / 渲染失败…）
   *             → chip 标琥珀并说明原因，避免发出"解析成功"的假信号；
   *  failed = 该文件被拒（不可发送）。 */
  status: 'staging' | 'ready' | 'degraded' | 'failed'
  attId: string
  kind: AttachmentKind | ''
  name: string
  mime: string
  ext: string
  size: number
  /** 用户原始路径（staging 期是本地路径，就绪后仍是原始路径） */
  sourcePath: string
  /** 后端登记时所属工作空间（发送时带上，跨空间也能找回草稿） */
  projectId: string
  /** 会话内副本路径（就绪后才有；图片缩略图 / 打开文件用它） */
  storedPath: string
  textChars: number
  textTruncated: boolean
  /** 解析统计（见 `AttachmentStats`）：chip 文案与降级提示都用它 */
  pages: number | null
  images: number
  tables: number
  converter: string
  /** 非空 = 已降级（status 为 'degraded'） */
  warnings: string[]
  /** failed 原因（UI 直接展示，不吞掉） */
  error?: string
}

/** 该草稿附件是否可随消息发送。
 *
 *  只有 `ready` 与 `degraded` 可发：`degraded` = "解析不完整但能用"（扫描件已按图像
 *  发送、内容被截断…），用户看过琥珀提示后仍应能发出；`staging` 还是半成品、
 *  `failed` 已被后端拒绝，两者都不能进 payload。
 *
 *  **这个判据必须只有一处** —— 曾经 InputBox（按钮可用性）与 ChatPanel（真正构造
 *  payload）各写了一遍，加 `degraded` 之后只改了前者，于是附件被**静默**从 payload
 *  里丢掉（后端日志表现为 `chat 派发 … attachments=0`）。这正是本项目要消灭的
 *  "附件没到模型手里、却没有任何提示"。
 */
export function isSendableAttachment(a: DraftAttachment): boolean {
  return a.status === 'ready' || a.status === 'degraded'
}

/** 本轮有没有**可发送的内容** —— 正文 / 就绪附件 / 引用 三者任一非空。
 *
 *  **这个判据必须只有一处**（与 `isSendableAttachment` 同理，见其上方注释）：
 *  InputBox 的发送按钮、InputBox 的 Enter 守卫、ChatPanel 构造 payload、
 *  store.send 的内部守卫，四处都调它；主进程 `agent:send` 的守卫因为跨进程
 *  无法 import，只能镜像同一条件（多一个 `refs`）—— 那里漏判就等于把消息
 *  静默丢掉，是本项目已经踩过一次的坑。 */
export function hasSendableContent(
  text: string,
  attachments: DraftAttachment[],
  refs?: RefInput[] | null
): boolean {
  return (
    (text ?? '').trim().length > 0 ||
    attachments.some(isSendableAttachment) ||
    (refs?.length ?? 0) > 0
  )
}

let draftSeq = 0

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
  /** 全部工作空间（`projects` 信封驱动；default 恒第一） */
  projects: ProjectMeta[]
  /** 当前活动工作空间 id（后端持久化；chip 显示与新建任务归属的默认值） */
  activeProject: string
  /** 侧边栏空间节点的展开态（localStorage 持久化；缺省 = 展开） */
  expandedProjects: Record<string, boolean>
  /** 会话列表"超过 15 条折叠"的展开态（按空间记；localStorage 持久化） */
  previewExpanded: Record<string, boolean>
  /** 新建任务的目标工作空间（点哪个空间的「+」/ chip 选哪个空间；首条消息随 chat 带上） */
  pendingProjectId: string | null
  trashSessions: SessionMeta[]
  activeSession: string | null
  isSending: boolean
  settingsOpen: boolean
  settingsTab: SettingsTab
  llmConfig: LlmConfig | null
  llmSaving: boolean
  /** 权限配置（设置页「权限」页）：get 回执整份结果（含内置清单 / 路径 / exists）。
   *  只由设置页消费 —— 它是点对点回执，不参与全局广播状态。 */
  permissionConfig: PermissionConfigResult | null
  permissionSaving: boolean
  /** 沙盒设置（设置页「沙盒」页，docs/frontend/20）：get/save 回执整份结果
   *  （平台状态 + 开关 + 两个模板文件内容）。只由设置页消费 —— 点对点回执。 */
  sandboxConfig: SandboxConfigResult | null
  sandboxSaving: boolean
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
  /** 每个会话当前在途的结构化提问（ask_user）：输入框上方「待确认」面板的数据源。
   *  null / 缺省 = 该会话没有待作答提问（面板不显示）。
   *  **不落盘、不从 session_history 恢复** —— 断线重连时由后端重放 ask_request
   *  （`_ask_snapshot_lines()`），所以在 setConnection('connected') 时整体清空，
   *  避免断连期间已被结算的提问留下永不消失的僵尸面板。 */
  interactionBySession: Record<string, AskInteraction | null>
  /** 每个会话当前在途的权限审批（PreToolUse 判定 ask，范式 C）：消息流审批
   *  卡片的数据源。按 request_id 键控（后端同会话至多一条在途，但迟到/乱序的
   *  approval_resolved 只清对应条目）。空对象/缺条目 = 无在途审批。
   *  **不落盘、不从 session_history 恢复** —— 断线重连时后端重放 approval_request
   *  （`_approval_snapshot_lines()`），所以 setConnection('connected') 时整体清空，
   *  避免断连期间已被结算的审批留下永不消失的僵尸卡片（同 interactionBySession）。 */
  approvalBySession: Record<string, Record<string, ApprovalInteraction>>
  /** 每个会话当前的权限档位（盾牌 chip 的选中态）：由 `permission_changed` 广播
   *  与 `session_history.permission_mode` 恢复驱动。缺条目时 chip 落
   *  sessions 列表 → 所属工作空间 → 'default' 的 fallback 链。 */
  permissionModeBySession: Record<string, PermissionMode>
  /** 当前激活会话的按模型参数覆盖（仅本会话生效，不写配置；按模型 id 分别保存） */
  overridesByModel: SessionOverridesMap
  /** 当前激活会话（或新建任务）绑定/选择的模型 id（区别于全局 active_model_id） */
  sessionModelId: string | null
  /** 上一次会话/选择留下的模型 id 与按模型参数，供新建会话继承 */
  lastSessionModelId: string | null
  lastOverridesByModel: SessionOverridesMap
  toast: string | null
  toastType: 'info' | 'error'
  /** 输入区附件草稿（三条入口都汇入这里；随消息发送后清空）。
   *  「仅附件无正文」是合法发送，判定依据就是这里有没有 ready 项。 */
  draftAttachments: DraftAttachment[]

  setConnection: (c: ConnState) => void
  setPython: (p: PythonState) => void
  /** 发送一轮消息。`attachments` = 本轮随消息发出的**已就绪**草稿附件
   *  （调用方从 draftAttachments 里筛 status==='ready'）；缺省 = 无附件，
   *  存量调用不受影响。`refs` = 本轮引用的工作空间路径（零复制，只传路径）。
   *  正文 / 附件 / 引用**三者不能同时为空**（调用方先用 `hasSendableContent` 判）。 */
  send: (text: string, attachments?: DraftAttachment[], refs?: RefInput[]) => void
  /** 把本地绝对路径登记为草稿附件（原生对话框 / 拖拽 / 粘贴三条入口的唯一汇合点）。
   *  结果经 `attachments_staged` 信封回填（本函数只放占位项，不等回包）。 */
  stageAttachments: (paths: string[], projectId?: string | null) => Promise<void>
  /** 移除一个草稿附件（staging 期也可移除；后端草稿由 GC 兜底回收） */
  removeDraftAttachment: (key: string) => void
  /** 清空附件草稿（发送后 / 切会话 / 新建任务时调用） */
  clearDraftAttachments: () => void
  stop: () => void
  /** 提交选择题作答（ask_user）。**fire-and-forget**：不乐观关面板，
   *  面板由随后广播的 `ask_resolved` 关闭 —— 与后端 `resolve()` 的
   *  「迟到/重复提交无副作用」语义一致（否则会出现"面板已消失但后端丢弃了作答"）。 */
  answerAsk: (requestId: string, answers: AskAnswer[]) => void
  /** 取消本次提问（用户点「取消」）：同上 fire-and-forget，回执走 `ask_resolved`。 */
  cancelAsk: (requestId: string) => void
  /** 提交审批裁决（允许一次 / 本次会话内允许 / 拒绝）。**fire-and-forget**：
   *  只置 submitted 禁用三按钮，卡片由随后广播的 `approval_resolved` 收掉 ——
   *  与 answerAsk 同款语义（后端对迟到/重复提交幂等丢弃，乐观收卡会在
   *  "后端丢弃作答"时造成已批准的假象）。 */
  answerApproval: (requestId: string, decision: ApprovalDecision) => void
  /** 切换当前会话的权限档位（默认 / 完全访问）。fire-and-forget：**不乐观更新**，
   *  chip 选中态只认后端广播的 `permission_changed`（传输丢失时乐观 UI 会说谎）。 */
  switchPermission: (mode: PermissionMode) => void
  /** 新建任务（无会话）态切换**目标工作空间**的权限档位（默认 / 完全访问）。
   *  只写 projects.json 的「最后更改值」，作为该空间新会话的默认档位 ——
   *  fire-and-forget：chip 选中态由随后的 `projects` 广播驱动（不乐观更新）。 */
  switchProjectPermission: (mode: PermissionMode) => void
  handleEvent: (ev: UiEvent) => void
  refreshSessions: () => Promise<void>
  /** 主动拉取工作空间列表（后端收到后广播 `projects`，渲染层经同管道更新） */
  refreshProjects: () => Promise<void>
  refreshTrash: () => Promise<void>
  /** 新建任务：清空当前显示回到欢迎空态（可指定归属工作空间）。会话仍由首条消息惰性创建 */
  newSession: (projectId?: string) => Promise<void>
  /** 展开/折叠某工作空间的会话列表 */
  toggleProject: (projectId: string) => void
  /** 折叠全部工作空间的会话列表（任务列表头部「收起」） */
  collapseAllProjects: () => void
  /** 展开/收起某工作空间"超过 15 条折叠"的完整会话列表 */
  toggleSessionPreview: (projectId: string) => void
  /** 切换活动工作空间（后端持久化 + 广播 projects） */
  openProject: (projectId: string) => Promise<void>
  /** 弹目录选择框并登记为新工作空间（成功则打开并回到新任务态） */
  addProjectFromPicker: () => Promise<void>
  renameProject: (projectId: string, name: string) => Promise<void>
  /** 删除工作空间（只删元数据；调用方需先确认） */
  removeProject: (projectId: string) => Promise<void>
  /** 在系统文件管理器中定位该工作空间的真实目录 */
  revealProject: (projectId: string) => Promise<void>
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
  /** 拉取权限配置（进「权限」页时懒加载；后端未就绪静默忽略） */
  loadPermissionConfig: () => Promise<void>
  /** 保存权限配置，返回 applied（false = 写盘失败）。
   *  归一化 warnings / 权威值由 `permissionConfig` 回写，页面自行读取展示。 */
  savePermissionConfig: (config: PermissionConfig) => Promise<boolean>
  /** 拉取沙盒设置（进「沙盒」页时懒加载；后端未就绪静默忽略） */
  loadSandboxConfig: () => Promise<void>
  /** 保存沙盒设置（**字段部分更新**：开关 / 模板内容 / 恢复默认）。
   *  权威值由 `sandboxConfig` 回执回写；返回 applied（false = 有字段校验失败）。 */
  saveSandboxConfig: (payload: SandboxConfigSavePayload) => Promise<boolean>
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

/**
 * 会话内附件副本 → 渲染层可用的 URL。
 *
 * 渲染层拿不到 `file://`（contextIsolation + CSP），所以走主进程注册的
 * `aigent-att://` 自定义协议。**按 (空间, att_id) 寻址而不是按路径**：附件在发送
 * 时会从 `.attachments/_draft/` 迁到 `.attachments/<会话>/`，路径会变；按 id 检索
 * 由主进程负责，缩略图因此跨越迁移稳定。主进程只放行 `.attachments` 目录内的
 * 真实文件（形状白名单 + 符号链接解引后复检），非法一律 404。
 */
export function attachmentUrl(projectId: string, attId: string): string | null {
  if (!projectId || !attId) return null
  return `aigent-att://local/?pid=${encodeURIComponent(projectId)}&id=${encodeURIComponent(attId)}`
}

/** 附件大小显示（与后端 attachments.human_size 口径一致） */
export function humanSize(size: number): string {
  if (!size || size < 0) return ''
  let v = size
  for (const unit of ['B', 'KB', 'MB', 'GB']) {
    if (v < 1024 || unit === 'GB') {
      return unit === 'B' ? `${Math.round(v)}B` : `${v.toFixed(1)}${unit}`
    }
    v /= 1024
  }
  return `${v.toFixed(1)}GB`
}

/** 路径 → 文件名（错误提示用；不判平台，两种分隔符都切） */
function fileBaseName(p: string): string {
  return p.split(/[/\\]/).pop() || p
}

/** 草稿附件 → 发送用的最小线索（后端以磁盘上的 meta.json 为准，这些只是线索） */
function draftToInput(a: DraftAttachment): ChatAttachmentInput {
  return {
    att_id: a.attId,
    kind: a.kind,
    name: a.name,
    mime: a.mime,
    ext: a.ext,
    size: a.size,
    ...(a.projectId ? { project_id: a.projectId } : {})
  }
}

/** 草稿附件 → 消息附件（乐观渲染用：发送时立刻把缩略图显示出来，不等回放） */
function draftToRef(a: DraftAttachment): AttachmentRef {
  return {
    id: a.attId,
    kind: a.kind,
    name: a.name,
    mime: a.mime,
    ext: a.ext,
    size: a.size,
    source_path: a.sourcePath,
    stored_path: a.storedPath,
    text_chars: a.textChars,
    text_truncated: a.textTruncated,
    pages: a.pages,
    images: a.images,
    tables: a.tables,
    converter: a.converter,
    warnings: a.warnings
  }
}

/** `attachments_staged` 的一条成功项 → 草稿项 */
function stagedToDraft(it: StagedAttachment): DraftAttachment {
  // 后端给了 warnings 就是"能发，但解析不完整" —— 标 degraded 而不是 ready，
  // 否则 chip 会显示成"解析成功"，正是本次事故里误导性反馈的来源。
  // 兼容没有该字段的老后端：`?? []`。
  const warnings = it.warnings ?? []
  return {
    key: it.att_id,
    status: warnings.length > 0 ? 'degraded' : 'ready',
    attId: it.att_id,
    kind: it.kind,
    name: it.name,
    mime: it.mime,
    ext: it.ext,
    size: it.size,
    sourcePath: it.source_path,
    projectId: it.project_id,
    storedPath: '',
    textChars: it.text_chars,
    textTruncated: it.text_truncated,
    pages: it.pages ?? null,
    images: it.images ?? 0,
    tables: it.tables ?? 0,
    converter: it.converter ?? '',
    warnings
  }
}

/** 解析 `ask_user` 工具参数里的 `questions`（参数是 JSON 文本）。
 *  **失败返回 null**（不是空数组）—— 调用方据此保留已有值，
 *  绝不能用"解析失败"的空数组覆盖掉从 `ask_request` 拿到的真实问题。 */
function parseAskQuestions(args: string): AskQuestion[] | null {
  try {
    const parsed = JSON.parse(args || '{}')
    const qs = parsed?.questions
    if (!Array.isArray(qs) || qs.length === 0) return null
    return qs as AskQuestion[]
  } catch {
    return null
  }
}

/** 在指定消息上按 toolCallId 建/取「只读小结块」占位（已存在则原样返回）。
 *  `tool_call_start` 到达时调用：此时参数可能还没流完，questions 先留空。
 *  `contentOffset` = 此刻的正文长度（提问卡片要插在正文的这个位置之后）。 */
function upsertAskBlock(
  msgs: Message[],
  msgId: string,
  toolCallId: string,
  contentOffset: number
): Message[] {
  return msgs.map((m) => {
    if (m.id !== msgId) return m
    const list = m.askUsers ?? []
    if (list.some((a) => a.toolCallId === toolCallId)) return m
    return {
      ...m,
      askUsers: [
        ...list,
        { toolCallId, questions: [], resultText: '', status: 'pending' as const, contentOffset }
      ]
    }
  })
}

/** 按 toolCallId 打补丁到已存在的块（跨消息查找）。找不到时返回原数组。 */
function patchAskBlock(msgs: Message[], toolCallId: string, patch: Partial<AskUserMsg>): Message[] {
  if (!toolCallId) return msgs
  return msgs.map((m) => {
    const idx = (m.askUsers ?? []).findIndex((a) => a.toolCallId === toolCallId)
    if (idx < 0) return m
    const askUsers = (m.askUsers ?? []).slice()
    askUsers[idx] = { ...askUsers[idx], ...patch }
    return { ...m, askUsers }
  })
}

/** `ask_resolved` → 把结果落进「发起它的那条 assistant 消息」的只读小结块。
 *
 *  找不到块时回落到**末尾那条 assistant 消息**（例如工具参数流被中断、
 *  `tool_call_start` 丢失）；连 assistant 都没有就原样返回 —— **绝不凭空新建气泡**
 *  （否则会出现一条只有提问、没有上下文的消息）。
 */
function appendAskResult(
  msgs: Message[],
  toolCallId: string,
  data: { questions: AskQuestion[] | null; resultText: string; status: AskStatus }
): Message[] {
  const tcid = toolCallId || ''
  if (tcid && msgs.some((m) => (m.askUsers ?? []).some((a) => a.toolCallId === tcid))) {
    return msgs.map((m) => {
      const idx = (m.askUsers ?? []).findIndex((a) => a.toolCallId === tcid)
      if (idx < 0) return m
      const askUsers = (m.askUsers ?? []).slice()
      const prev = askUsers[idx]
      askUsers[idx] = {
        ...prev,
        // 已有问题（ask_request / 工具参数）优先，只在为空时用兜底值
        questions: prev.questions.length ? prev.questions : (data.questions ?? []),
        resultText: data.resultText,
        status: data.status
      }
      return { ...m, askUsers }
    })
  }
  // 兜底：挂到末尾那条 assistant
  let lastAssistant = -1
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'assistant') {
      lastAssistant = i
      break
    }
  }
  if (lastAssistant < 0) return msgs
  const block: AskUserMsg = {
    toolCallId: tcid,
    questions: data.questions ?? [],
    resultText: data.resultText,
    status: data.status
  }
  return msgs.map((m, idx) =>
    idx === lastAssistant ? { ...m, askUsers: [...(m.askUsers ?? []), block] } : m
  )
}

/** `approval_resolved` → 把结算徽标旁挂到 tool_call_id 匹配的工具行（范式 C：
 *  决定不进模型上下文，只落 UI 元数据）。主 toolCalls 与 subagents[].toolCalls
 *  都要查 —— 审批可能拦在子智能体的工具上。找不到匹配行时原样返回
 *  （工具条可能还在流式中未建出；此时徽标不补，回放时后端落盘字段会补上）。 */
function patchToolApproval(
  msgs: Message[],
  toolCallId: string,
  approval: ApprovalInfo
): Message[] {
  if (!toolCallId) return msgs
  return msgs.map((m) => {
    let mainHit = false
    const toolCalls = m.toolCalls.map((t) => {
      if (t.id !== toolCallId) return t
      mainHit = true
      return { ...t, approval }
    })
    let subHit = false
    const subagents = m.subagents.map((s) => {
      let hit = false
      const subCalls = s.toolCalls.map((t) => {
        if (t.id !== toolCallId) return t
        hit = true
        return { ...t, approval }
      })
      if (hit) subHit = true
      return hit ? { ...s, toolCalls: subCalls } : s
    })
    if (!mainHit && !subHit) return m
    return { ...m, ...(mainHit ? { toolCalls } : {}), ...(subHit ? { subagents } : {}) }
  })
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
        status: t.status === 'running' ? ('running' as const) : ('done' as const),
        // 审批结算元数据（拒绝/超时/停止才落盘；无字段 = 正常流，按普通工具条渲染）
        ...(t.approval ? { approval: t.approval } : {})
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
    // 结构化提问（ask_user）：不进普通工具条，改由消息下的只读小结块承载。
    // 后端的 `_history_to_ui` 已把它从 toolCalls 里摘出放进 askUsers[]（问题=工具参数，
    // 结果=配对上的 tool 行 content），这里只做形状映射 + 兜底过滤（老后端/旁路数据）。
    const askUsers: AskUserMsg[] = (m.askUsers ?? [])
      .filter((a) => !!a && typeof a.tool_call_id === 'string')
      .map((a: HistoryAskUser) => ({
        toolCallId: a.tool_call_id,
        questions: parseAskQuestions(a.args) ?? [],
        resultText: a.result ?? '',
        // status 由后端 `interaction.status_of_result()` 反推；老后端缺字段时按
        // "有结果=已作答 / 无结果=未完成"退化（**不在前端猜具体是哪一种结局**）
        status: a.status ?? (a.result ? ('answered' as const) : ('incomplete' as const))
      }))
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
        status: t.status === 'running' ? ('running' as const) : ('done' as const),
        // 审批结算元数据（拒绝/超时/停止才落盘；无字段 = 正常流，按普通工具条渲染）
        ...(t.approval ? { approval: t.approval } : {})
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
      switch: m.model_info?.switch ?? undefined,
      // 回放：user 消息携带的附件（后端从 content 引用块 harvest；无附件不带该字段）
      ...(m.attachments && m.attachments.length ? { attachments: m.attachments } : {}),
      // 回放：user 消息引用的工作空间路径（同上，无引用时后端连字段都不发）
      ...(m.refs && m.refs.length ? { refs: m.refs } : {}),
      // 回放：结构化提问的只读小结块（无提问时连字段都不多一个）
      ...(askUsers.length ? { askUsers } : {})
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
export function showToast(msg: string, type: 'info' | 'error' = 'info', ms = 3000): void {  if (toastTimer) clearTimeout(toastTimer)
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
      // ask_user 同理不进主工具条：提问由输入框上方的面板承载、结果由消息下的
      // 只读小结块承载。这里先按 tool_call_id 建块占位（status='pending' →
      // 在途期间**不渲染**，避免与面板重复），问题/结果随后由 `tool_call`
      // 与 `ask_resolved` 补齐。**必须记锚点**：ask_resolved 据此落回本条消息。
      if (ev.tool_name === 'ask_user') {
        const tcid = ev.tool_id || ''
        if (!tcid) return msgs
        // 记录"此刻正文有多长"：提问卡片要插在正文的这个位置之后 ——
        // 模型先说的那句话属于卡片上方，提问后（同轮）续写的正文留在卡片下方。
        msgs = upsertAskBlock(msgs, id, tcid, current(id).content.length)
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
      // ask_user 终态：参数已完整，补进只读小结块（流式下 tool_call_start 时
      // 参数可能只到了一半，解析不出问题，所以在这里补一次）。
      // `ask_resolved` 通常**先于**本事件到达（broker 在工具返回前就广播了结果），
      // 因此这里是"事后补问题"而不是"覆盖结果"—— patch 只带 questions。
      if (ev.tool_name === 'ask_user') {
        const qs = parseAskQuestions(ev.args ?? '')
        if (qs) msgs = patchAskBlock(msgs, ev.tool_id || '', { questions: qs })
        break
      }
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
  projects: [],
  activeProject: DEFAULT_PROJECT_ID,
  expandedProjects: loadFlagMap(EXPANDED_KEY),
  previewExpanded: loadFlagMap(PREVIEW_KEY),
  pendingProjectId: null,
  trashSessions: [],
  activeSession: null,
  isSending: false,
  settingsOpen: false,
  settingsTab: 'model',
  llmConfig: null,
  llmSaving: false,
  permissionConfig: null,
  permissionSaving: false,
  sandboxConfig: null,
  sandboxSaving: false,
  currentContextStats: null,
  sessionUsageBySession: {},
  taskBoardBySession: {},
  interactionBySession: {},
  approvalBySession: {},
  permissionModeBySession: {},
  overridesByModel: {},
  sessionModelId: null,
  lastSessionModelId: null,
  lastOverridesByModel: {},
  toast: null,
  toastType: 'info',
  draftAttachments: [],

  setConnection: (c) =>
    set((s) => {
      if (c === s.connection) return s
      // 断线重连（→ connected）：清空陈旧运行态。后端会在新连接上重放
      // 仍在运行会话的 session_status（running/background），重新点亮真实
      // 运行指示；清空防止断连期间的状态残留（如永远转圈的僵尸会话）。
      // 待作答的提问 / 待裁决的审批同理清空：重连时后端会重放 ask_request
      // 与 approval_request 快照（`_ask_snapshot_lines()` / `_approval_snapshot_lines()`）
      // —— 不断连期间若已被结算，前端拿不到 resolved 广播，留下的面板/卡片会永远消不掉。
      if (c === 'connected' && s.connection !== 'connected') {
        // 断线重连：右栏只清**在途标志**（loading / pendingReveal），
        // **不清 layoutBySession** —— 标签栏是用户的意图，不是可恢复的瞬时态。
        useRightPanelStore.getState().resetTransient()
        return {
          ...s,
          connection: c,
          runningSessions: [],
          bgSessions: [],
          isSending: false,
          interactionBySession: {},
          approvalBySession: {}
        }
      }
      return { ...s, connection: c }
    }),
  setPython: (p) => set({ python: p }),

  send: (text, attachments, refInputs) => {
    const t = text.trim()
    const atts = attachments ?? []
    const refs = refInputs ?? []
    // 「仅附件无正文」「仅引用无正文」都是合法发送：三者同时为空才拦下。
    // （历史 bug：后端 main/index.ts 曾用 !payload.text 直接丢弃这类消息。）
    if (!hasSendableContent(t, atts, refs) || get().isSending) return
    const sid = get().activeSession
    const modelId = get().sessionModelId
    // 新建任务的归属工作空间（点「+」/ chip 选定；未指定 = 后端当前活动空间）
    const projectId = sid === null ? (get().pendingProjectId ?? get().activeProject) : null
    const ov = resolveOverridesPayload(get().llmConfig, get().overridesByModel, modelId)
    // 乐观渲染：把本轮附件与引用直接挂到 user 消息上（缩略图 / 引用胶囊立即出现）。
    // 缩略图按 (空间, att_id) 寻址 → 草稿区→会话目录的迁移不会让它失效；
    // 后端回放时的形状一致，切走再切回不跳变。
    const attRefs = atts.map(draftToRef)
    // 引用没有 id 概念，按 path 去重（与后端 normalize_refs 同一口径）
    const msgRefs: MessageRef[] = []
    const seenRefs = new Set<string>()
    for (const r of refs) {
      const path = String(r?.path ?? '')
      if (!path || seenRefs.has(path)) continue
      seenRefs.add(path)
      msgRefs.push({ path, name: String(r?.name ?? ''), is_dir: !!r?.is_dir })
    }
    const userMsg: Message = {
      id: mid(), role: 'user', content: t, thinking: '', thinkingActive: false, toolCalls: [], subagents: [], activeToolId: null, streaming: false, usage: null, created_at: nowLocalIso(),
      ...(attRefs.length ? { attachments: attRefs } : {}),
      ...(msgRefs.length ? { refs: msgRefs } : {})
    }
    const assMsg: Message = {
      id: mid(), role: 'assistant', content: '', thinking: '', thinkingActive: false, toolCalls: [], subagents: [], activeToolId: null, streaming: true, usage: null, created_at: nowLocalIso()
    }
    set((s) => {
      const cleared = { draftAttachments: [] as DraftAttachment[] }
      // 新建任务（尚无会话 id）：首条消息进临时草稿缓冲，等后端 session 信封迁移
      if (sid === null) {
        const pendingFresh = [userMsg, assMsg]
        return { ...s, ...cleared, pendingFresh, messages: pendingFresh, isSending: true }
      }
      const buf = s.messagesBySession[sid] ?? []
      const messagesBySession = { ...s.messagesBySession, [sid]: [...buf, userMsg, assMsg] }
      const messages = messagesBySession[sid]
      return { ...s, ...cleared, messagesBySession, messages, isSending: true }
    })
    // 发送实际交给后端：fresh 时后端生成短 id 并回发 session 信封，前端据此迁移草稿；
    // projectId 只在新建任务时带（已有会话由后端按 session_id 解析归属）；
    // attachments 只带 att_id 与线索，文件由后端按 att_id 从草稿区归位；
    // refs 只带路径，后端做越界校验后挂中性引用块（**不复制、不读内容**）。
    window.agent
      .send(t, sid, ov, modelId, projectId, atts.map(draftToInput), refs)
      .catch(() => set({ isSending: false }))
  },

  stageAttachments: async (paths, projectId) => {
    const list = (paths ?? []).map((p) => String(p || '').trim()).filter(Boolean)
    if (list.length === 0) return
    // 目标工作空间口径与发送一致：已有会话跟随其归属，新建任务用「+」/chip 选定值
    const sid = get().activeSession
    const pid =
      projectId ??
      (sid === null ? (get().pendingProjectId ?? get().activeProject) : get().activeProject)
    // 占位项：让用户立刻看到"正在读取…"，后端回包按 source_path 配对替换
    const placeholders: DraftAttachment[] = list.map((p) => ({
      key: `d${++draftSeq}`,
      status: 'staging',
      attId: '',
      kind: '',
      name: fileBaseName(p),
      mime: '',
      ext: '',
      size: 0,
      sourcePath: p,
      projectId: pid,
      storedPath: '',
      textChars: 0,
      textTruncated: false,
      pages: null,
      images: 0,
      tables: 0,
      converter: '',
      warnings: []
    }))
    set((s) => ({ ...s, draftAttachments: [...s.draftAttachments, ...placeholders] }))
    try {
      await window.agent.stageAttachments({ paths: list, projectId: pid })
    } catch {
      // IPC 层就失败了：把这批占位项就地标失败（否则永远转圈）
      const failedPaths = new Set(list)
      set((s) => ({
        ...s,
        draftAttachments: s.draftAttachments.map((a) =>
          a.status === 'staging' && failedPaths.has(a.sourcePath)
            ? { ...a, status: 'failed' as const, error: '添加失败：后端无响应' }
            : a
        )
      }))
      showToast('添加附件失败：后端无响应', 'error', 4000)
    }
  },

  removeDraftAttachment: (key) =>
    set((s) => ({ ...s, draftAttachments: s.draftAttachments.filter((a) => a.key !== key) })),

  clearDraftAttachments: () => set({ draftAttachments: [] }),

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
    // 真实停止：通知后端只停当前显示会话这一轮与它的后台任务（其它会话不受影响）。
    // background 态（turn 已结束、后台子智能体还在跑）也在停止范围内。
    window.agent.stop(sid)
    set((s) => {
      const buf = (s.messagesBySession[sid] ?? []).map(clearStreaming)
      return {
        ...s,
        messagesBySession: { ...s.messagesBySession, [sid]: buf },
        messages: s.activeSession === sid ? buf : s.messages,
        runningSessions: s.runningSessions.filter((n) => n !== sid),
        bgSessions: s.bgSessions.filter((n) => n !== sid),
        isSending: false
      }
    })
    // 面板**不在此处本地清空**：停止会走后端 `request_stop()` → `cancel_all()`
    // → 广播 `ask_resolved`（status='stopped'）→ 由该事件清面板并落只读小结。
    // 本地清会让随后到达的 ask_resolved 找不到 in-flight 的问题文本
    //（小结块只剩结果行、丢标题），所以坚持"单一出口"。
    // 在途审批卡片同理**不本地清**：停止路径由后端广播 `approval_resolved`
    //（status='stopped'）收卡并落「已停止」徽标 —— 本地清会丢失 trigger/mode
    // 等结算徽标所需的在途元数据。
  },

  /** 提交选择题作答。fire-and-forget —— 见 AgentState 上的注释。 */
  answerAsk: (requestId, answers) => {
    const sid = get().activeSession
    if (!sid || !requestId) return
    window.agent.answerAsk(sid, requestId, answers)
  },

  /** 取消本次提问。fire-and-forget —— 见 AgentState 上的注释。 */
  cancelAsk: (requestId) => {
    const sid = get().activeSession
    if (!sid || !requestId) return
    window.agent.cancelAsk(sid, requestId)
  },

  /** 提交审批裁决。fire-and-forget —— 见 AgentState 上的注释。
   *  submitted 只在本地置位（禁按钮防重复提交），卡片与徽标的结算出口
   *  唯一：`approval_resolved` 广播（含 timeout / stopped 的后端自结算）。 */
  answerApproval: (requestId, decision) => {
    const sid = get().activeSession
    if (!sid || !requestId) return
    set((s) => {
      const cur = s.approvalBySession[sid]
      const entry = cur?.[requestId]
      if (!entry || entry.submitted) return s
      return {
        ...s,
        approvalBySession: {
          ...s.approvalBySession,
          [sid]: { ...cur, [requestId]: { ...entry, submitted: true } }
        }
      }
    })
    window.agent.approvalAnswer(sid, requestId, decision)
  },

  /** 切换权限档位。fire-and-forget —— chip 只认 `permission_changed` 广播。 */
  switchPermission: (mode) => {
    const sid = get().activeSession
    if (!sid) return
    window.agent.sessionPermission(sid, mode)
  },

  /** 新建任务（无会话）态切换目标工作空间的权限档位。目标空间 = 「+」/chip
   *  选定的 pendingProjectId（缺省跟随后端活动空间）。fire-and-forget ——
   *  chip 只认随后的 `projects` 广播。 */
  switchProjectPermission: (mode) => {
    const pid = get().pendingProjectId ?? get().activeProject
    if (!pid) return
    window.agent.projectPermission(pid, mode)
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
        const list = raw as SessionMeta[]
        // 活动空间与"当前正在看的会话"必须一致：列表是权威（每条带真实 project），
        // 若两者不符（比如别处把活动空间改了），以会话归属为准 —— 否则 chip 会
        // 显示成另一个空间，接下来的「+」会把新任务建到那个空间去。
        const cur = get().activeSession
        const curPid = cur ? list.find((x) => x.id === cur)?.project : undefined
        set((s) => ({
          sessions: list,
          ...(curPid && curPid !== s.activeProject ? { activeProject: curPid } : {})
        }))
        break
      }
      case 'projects': {
        // 工作空间列表（连接重放 / 增删改后广播）。**整份替换**（幂等）。
        // 会话列表不在这里动：它是另一条信封（sessions），两者独立刷新。
        const payload = ev.payload as ProjectsPayload | null
        if (!payload || !Array.isArray(payload.projects)) break
        const active = payload.active || DEFAULT_PROJECT_ID
        set((s) => {
          // 活动空间被删/失效时后端已回落到 default（payload.active），跟随即可；
          // 若前端"新建任务"停在了一个已消失的空间，一并复位到 default，
          // 否则首条消息会带着一个不存在的 project_id 发出去。
          const alive = payload.projects.some((p) => p.id === s.pendingProjectId)
          return {
            projects: payload.projects,
            activeProject: active,
            pendingProjectId: alive ? s.pendingProjectId : null
          }
        })
        break
      }
      case 'session': {
        const sp = ev.payload as { session_id?: string; project_id?: string } | null
        const sid = sp?.session_id
        if (typeof sid !== 'string' || !sid) break
        const newPid = typeof sp?.project_id === 'string' && sp.project_id ? sp.project_id : null
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
      isSending: s.runningSessions.includes(sid) || s.bgSessions.includes(sid),
      // 活动空间对齐到新会话的归属（点空间 B 的「+」新建时，活动空间可能还停在 A）
            ...(newPid ? { activeProject: newPid, pendingProjectId: newPid } : {})
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
        // 让后端"活动空间"跟上新会话的归属：否则下一次 projects 广播会把 chip 拉回旧空间
        if (newPid) void window.agent.openProject(newPid)
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
          // running：turn 执行中；background：turn 已结束但后台任务仍在执行。
          // 两者都算"执行中"——侧栏脉冲点与发送按钮的停止态共用同一判据
          // （2026-09-21 起 background 也显示停止按钮，点击会连后台任务一起停）；
          // done/stopped：全部复位。未读/已读状态由元数据（sessions[].unread）
          // 持久化驱动，不做前端内存态。
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
            isSending:
              s.activeSession !== null &&
              (runningSessions.includes(s.activeSession) ||
                bgSessions.includes(s.activeSession))
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
      case 'attachments_staged': {
        // 附件登记结果（应答 attachment_stage）：按 source_path 与本地占位项配对。
        // 单条失败不影响整批 —— 成功项照常可用，失败项就地标红并给出原因，
        // 用户不必猜"为什么这个文件没了"。
        const p = ev.payload as AttachmentsStagedPayload | null
        if (!p || !Array.isArray(p.items)) break
        const okByPath = new Map(p.items.map((it) => [it.source_path, it]))
        const failByPath = new Map((p.failed ?? []).map((f) => [f.path, f.reason]))
        set((s) => ({
          ...s,
          draftAttachments: s.draftAttachments.map((a) => {
            if (a.status !== 'staging') return a
            const it = okByPath.get(a.sourcePath)
            if (it) return stagedToDraft(it)
            const reason = failByPath.get(a.sourcePath)
            if (reason !== undefined) {
              return { ...a, status: 'failed' as const, error: reason }
            }
            // 不属于本批（另一批仍在途中）→ 保持 staging 等自己的回包
            return a
          })
        }))
        for (const f of p.failed ?? []) {
          showToast(`无法添加「${fileBaseName(f.path)}」：${f.reason}`, 'error', 5000)
        }
        break
      }
      case 'session_history': {
        const payload = ev.payload as { session_id?: string; messages?: HistoryMessage[]; model_id?: string | null; overrides?: SessionModelOverridesMap | null; usage_totals?: UsageStats | null; permission_mode?: PermissionMode; right_panel?: RPanelPersist | null } | null
        if (typeof payload?.session_id !== 'string' || !payload.session_id || !Array.isArray(payload.messages)) break
        // 右栏状态：**在 `set(...)` 之外**调用。两个理由：
        // ① 它是另一个 store 的 action，放进更新函数会破坏"更新函数必须纯"的前提
        //    （StrictMode / 并发渲染下可能被调用两次）；
        // ② `applySessionUi` 自带"内存桶已存在则整条忽略"的守卫（19 篇 §2.5 纪律 1）——
        //    落盘是防抖异步的，回读可能滞后于用户刚做的操作，整份替换会把刚开的标签吃掉。
        useRightPanelStore.getState().applySessionUi(payload.session_id, payload.right_panel)
        set((s) => {
          // 回调内 payload 的窄化丢失，重断言为已校验形状
          const p = payload as { session_id: string; messages: HistoryMessage[]; model_id?: string | null; overrides?: SessionModelOverridesMap | null; usage_totals?: UsageStats | null; permission_mode?: PermissionMode; right_panel?: RPanelPersist | null }
          // 任务面板：先把本会话 board 清空，等紧随其后的 task_board 事件覆盖。
          // 必须清 —— 后端回放只发"未完成组"，已结束的组不再下发；不清的话
          // "看到完成的组 → 切走 → 切回"会残留上一轮那版 done 快照，
          // 违反"会话切换/复现时仅显示正在执行的组"。
          const taskBoardBySession = { ...s.taskBoardBySession, [p.session_id]: null }
          // 权限档位：session_history 带上时落进 permissionModeBySession
          //（盾牌 chip 的权威源；缺字段 = 老后端，chip 走 fallback 链）
          const permissionModeBySession = p.permission_mode
            ? { ...s.permissionModeBySession, [p.session_id]: p.permission_mode }
            : s.permissionModeBySession
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
            return { ...s, messagesBySession, messages, sessionUsageBySession, taskBoardBySession, permissionModeBySession }
          }
          return {
            ...s,
            messagesBySession,
            messages,
            sessionUsageBySession,
            sessionModelId: p.model_id || s.sessionModelId,
            overridesByModel,
            taskBoardBySession,
            permissionModeBySession,
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
      case 'permission_config': {
        // 点对点回执（只回发起窗口、**不广播**）：整份存 store，权限页按需渲染。
        // 刻意不 toast —— warnings 与 msg 要由页面内联展示，用户得对着它们改配置，
        // toast 一闪而过等于没提示（见 18 篇 §3.3）。
        const payload = ev.payload as PermissionConfigResult
        if (payload?.config) {
          set({ permissionConfig: mergePermissionResult(get().permissionConfig, payload) })
        }
        break
      }
      case 'sandbox_config': {
        // 点对点回执（同 permission_config）：get/save 回执同构，整份替换。
        // 校验错误由沙盒页内联展示，不 toast（同上理由）。
        const payload = ev.payload as SandboxConfigResult
        if (payload?.platform) set({ sandboxConfig: payload })
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
      case 'ask_request': {
        // 模型发起结构化提问（ask_user）→ 弹「待确认」面板（输入框上方）。
        // 这是桥层聚合出的 UI 产物事件（同 task_board），**不是流式增量**。
        // 纯 UI 态：不落盘、不进 messages —— 消息里只有 tool_call_start 建好的
        // 只读小结块占位（status='pending' 期间不渲染，避免与面板重复）。
        const p = ev.payload as
          | { session_id?: string; request_id?: string; tool_call_id?: string; questions?: AskQuestion[] }
          | null
        const sid = p?.session_id
        if (typeof sid !== 'string' || !sid || !p?.request_id) break
        set((s) => ({
          ...s,
          interactionBySession: {
            ...s.interactionBySession,
            [sid]: {
              requestId: p.request_id as string,
              toolCallId: typeof p.tool_call_id === 'string' ? p.tool_call_id : '',
              questions: Array.isArray(p.questions) ? p.questions : []
            }
          }
        }))
        break
      }
      case 'ask_resolved': {
        // 提问已结算（作答 / 取消 / 被停止）：清面板 + 把结果落进消息下的只读小结块。
        // **result_text 原样展示**（与回填给模型的 tool_result 逐字节相同），
        // 前端不做任何结构化解析。
        const p = ev.payload as
          | {
              session_id?: string
              request_id?: string
              tool_call_id?: string
              status?: AskStatus
              answers?: AskAnswer[]
              result_text?: string
            }
          | null
        const sid = p?.session_id
        if (typeof sid !== 'string' || !sid) break
        set((s) => {
          const cur = s.interactionBySession[sid] ?? null
          const sameRequest = cur !== null && (!p?.request_id || cur.requestId === p.request_id)
          const interactionBySession = { ...s.interactionBySession }
          // 只清「同一条请求」：迟到/重复的 ask_resolved（例如作答后又被停止路径
          // 结算）不能误清新发起的提问面板。
          if (sameRequest || cur === null) interactionBySession[sid] = null
          const buf = appendAskResult(s.messagesBySession[sid] ?? [], p?.tool_call_id ?? '', {
            questions: sameRequest && cur ? cur.questions : null,
            resultText: p?.result_text ?? '',
            status: p?.status ?? 'answered'
          })
          const messagesBySession = { ...s.messagesBySession, [sid]: buf }
          return {
            ...s,
            interactionBySession,
            messagesBySession,
            messages: s.activeSession === sid ? buf : s.messages
          }
        })
        break
      }
      case 'approval_request': {
        // 工具执行前的审批下发（PreToolUse 判定 ask，范式 C）→ 消息流里弹审批
        // 卡片（锚定 tool_call 折叠条，见 docs/frontend/17 §4）。纯 UI 态：
        // 不落盘、不进 messages —— 决定不进模型上下文，只影响工具执行。
        const p = ev.payload as
          | {
              session_id?: string
              request_id?: string
              tool_call_id?: string
              tool_name?: string
              args?: Record<string, unknown>
              trigger?: string
              reason?: string
              session_scope_hint?: string
              mode?: string
              timeout_seconds?: number
              created_at?: number
            }
          | null
        const sid = p?.session_id
        const rid = p?.request_id
        if (typeof sid !== 'string' || !sid || typeof rid !== 'string' || !rid) break
        set((s) => {
          const cur = s.approvalBySession[sid] ?? {}
          // 幂等：同 request_id 已存在（断线重连重放）→ 保留原条目 ——
          // 尤其不能重置 submitted（用户刚点过按钮，重放把禁用态弹回去等于引诱重复提交）
          if (cur[rid]) return s
          const entry: ApprovalInteraction = {
            requestId: rid,
            toolCallId: typeof p?.tool_call_id === 'string' ? p.tool_call_id : '',
            toolName: typeof p?.tool_name === 'string' ? p.tool_name : '',
            args: p?.args && typeof p.args === 'object' && !Array.isArray(p.args) ? p.args : {},
            trigger: typeof p?.trigger === 'string' ? p.trigger : '',
            reason: typeof p?.reason === 'string' ? p.reason : '',
            sessionScopeHint: typeof p?.session_scope_hint === 'string' ? p.session_scope_hint : '',
            mode: typeof p?.mode === 'string' ? p.mode : 'default',
            timeoutSeconds: typeof p?.timeout_seconds === 'number' ? p.timeout_seconds : 0,
            createdAt: typeof p?.created_at === 'number' ? p.created_at : 0,
            submitted: false
          }
          return {
            ...s,
            approvalBySession: { ...s.approvalBySession, [sid]: { ...cur, [rid]: entry } }
          }
        })
        break
      }
      case 'approval_resolved': {
        // 审批已结算（作答 / 超时 / 停止）：清在途卡片 + 在对应工具条上落结算徽标。
        // 与 ask_resolved 完全同构：多窗口一致 + fire-and-forget 提交的唯一出口。
        const p = ev.payload as
          | {
              session_id?: string
              request_id?: string
              tool_call_id?: string
              status?: ApprovalOutcome
              at?: number
            }
          | null
        const sid = p?.session_id
        if (typeof sid !== 'string' || !sid) break
        set((s) => {
          const cur = s.approvalBySession[sid] ?? {}
          const rid = typeof p?.request_id === 'string' ? p.request_id : ''
          const entry = rid ? cur[rid] : undefined
          // 只清「同一条请求」；request_id 缺失（不该发生）时整表清空兜底
          const next = rid ? { ...cur } : {}
          if (rid) delete next[rid]
          // 结算徽标：outcome + 在途条目的 trigger/mode 写进 tool_call_id 匹配的
          // 工具行。允许结局（allowed_*）也写 —— 徽标只进内存 UI 态，回放是否
          // 带它由后端落盘口径决定（只落 denied/timeout/stopped）。
          const tcid = entry?.toolCallId || (typeof p?.tool_call_id === 'string' ? p.tool_call_id : '')
          const buf = tcid
            ? patchToolApproval(s.messagesBySession[sid] ?? [], tcid, {
                decision: p?.status ?? 'denied',
                trigger: entry?.trigger,
                mode: entry?.mode
              })
            : (s.messagesBySession[sid] ?? [])
          const messagesBySession = { ...s.messagesBySession, [sid]: buf }
          return {
            ...s,
            approvalBySession: { ...s.approvalBySession, [sid]: next },
            messagesBySession,
            messages: s.activeSession === sid ? buf : s.messages
          }
        })
        break
      }
      case 'permission_changed': {
        // 会话权限档位已切换（session_permission 的回执广播）→ 同步盾牌 chip
        // 选中态与 sessions 列表条目（chip 的 fallback 链读它，两处必须同时更新）。
        const p = ev.payload as { session_id?: string; mode?: PermissionMode } | null
        const sid = p?.session_id
        const mode = p?.mode
        if (typeof sid !== 'string' || !sid || (mode !== 'default' && mode !== 'full_access')) break
        set((s) => ({
          ...s,
          permissionModeBySession: { ...s.permissionModeBySession, [sid]: mode },
          sessions: s.sessions.map((x) => (x.id === sid ? { ...x, permission_mode: mode } : x))
        }))
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

  /** 工作空间列表：后端收到 projects_list 后会广播 `projects`（主进程同管道转发），
   *  渲染层在 handleEvent 的 'projects' 分支落库；这里的返回值只作兜底。 */
  refreshProjects: async () => {
    try {
      const payload = (await window.agent.listProjects()) as ProjectsPayload | null
      if (!payload?.projects) return
      set({ projects: payload.projects, activeProject: payload.active || DEFAULT_PROJECT_ID })
    } catch {
      /* 后端未就绪时忽略：连接建立后后端会主动重放 projects */
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
   * 继承上一会话最后选择的模型与按模型参数覆盖（主流智能体行为），随首条消息持久化进新会话元数据。
   * `projectId`：目标工作空间（侧边栏「+」/ chip 下拉）；缺省沿用当前活动空间。 */
  newSession: (projectId) => {
    set((s) => ({
      messages: [],
      activeSession: null,
      pendingFresh: null,
      isSending: false,
      currentContextStats: null,
      sessionModelId: s.lastSessionModelId,
      overridesByModel: s.lastOverridesByModel,
      pendingProjectId: projectId ?? s.pendingProjectId ?? s.activeProject,
      // 新建任务 = 换一条消息，附件草稿必须跟着清（否则会把上一个任务的附件带过去）。
      // 未发送的草稿文件由后端 GC 兜底回收。
      draftAttachments: []
    }))
    return Promise.resolve()
  },

  toggleProject: (projectId) => {
    set((s) => {
      // 缺省（未记录）= 展开：首次点击变成"折叠"，符合直觉
      const expanded = s.expandedProjects[projectId] !== false
      const next = { ...s.expandedProjects, [projectId]: !expanded }
      saveFlagMap(EXPANDED_KEY, next)
      return { expandedProjects: next }
    })
  },

  /** 一键收起：把所有工作空间节点都置为折叠态并持久化 */
  collapseAllProjects: () => {
    const ids = get().projects.map((p) => p.id)
    if (ids.length === 0) return
    set((s) => {
      const next = { ...s.expandedProjects }
      for (const id of ids) next[id] = false
      saveFlagMap(EXPANDED_KEY, next)
      return { expandedProjects: next }
    })
  },

  toggleSessionPreview: (projectId) => {
    set((s) => {
      const next = { ...s.previewExpanded, [projectId]: !s.previewExpanded[projectId] }
      saveFlagMap(PREVIEW_KEY, next)
      return { previewExpanded: next }
    })
  },

  openProject: async (projectId) => {
    // 本地即时切换（chip 立刻反映），后端持久化后再以 projects 广播校准
    set({ activeProject: projectId })
    try {
      await window.agent.openProject(projectId)
    } catch {
      showToast('切换工作空间失败', 'error', 4000)
    }
  },

  /** 选择文件夹 → 登记为新工作空间 → 打开它并回到"新建任务"态。
   *  失败（目录不可写 / 已选过 / 取消）由后端 error 信封 toast 提示。 */
  addProjectFromPicker: async () => {
    let path: string | null = null
    try {
      path = await window.agent.pickFolder()
    } catch {
      showToast('无法打开目录选择框', 'error', 4000)
      return
    }
    if (!path) return
    let payload: ProjectsPayload | null = null
    try {
      payload = (await window.agent.addProject(path)) as ProjectsPayload | null
    } catch {
      showToast('新增工作空间失败', 'error', 4000)
      return
    }
    if (!payload?.projects) return  // 失败时后端回 error 信封（已 toast），这里不再重复报错
    set({ projects: payload.projects, activeProject: payload.active || DEFAULT_PROJECT_ID })
    await get().newSession(payload.active || DEFAULT_PROJECT_ID)
  },

  renameProject: async (projectId, name) => {
    const n = name.trim()
    if (!n) return
    try {
      const payload = (await window.agent.renameProject(projectId, n)) as ProjectsPayload | null
      if (payload?.projects) set({ projects: payload.projects })
    } catch {
      showToast('重命名失败', 'error', 4000)
    }
  },

  /** 删除工作空间：**只删元数据目录**（会话/任务/记忆/回收站一并消失，不可恢复），
   *  用户选定的真实目录保留。调用方（右键菜单）必须先弹确认。 */
  removeProject: async (projectId) => {
    try {
      const payload = (await window.agent.removeProject(projectId)) as ProjectsPayload | null
      if (!payload?.projects) return  // 被拒（空间有会话在跑 / 删 default）时后端回 error 信封
      set({ projects: payload.projects, activeProject: payload.active || DEFAULT_PROJECT_ID })
      // 该空间的会话已随目录消失：本地列表里清掉，避免点进去报"会话不存在"
      const gone = get()
        .sessions.filter((x) => sessionProjectId(x) === projectId)
        .map((x) => x.id)
      set((s) => ({ sessions: s.sessions.filter((x) => sessionProjectId(x) !== projectId) }))
      // 右栏：被删会话的内存桶一起清掉。**这是既有分桶的缺口**
      // （`sessions` 过滤了但 messagesBySession 等没有人清），本 store 不照抄这个缺陷。
      useRightPanelStore.getState().dropSessions(gone)
      if (get().pendingProjectId === projectId) await get().newSession(DEFAULT_PROJECT_ID)
      showToast('已删除工作空间（仅元数据，真实目录已保留）', 'info', 4000)
    } catch {
      showToast('删除工作空间失败', 'error', 4000)
    }
  },

  revealProject: async (projectId) => {
    const p = get().projects.find((x) => x.id === projectId)
    if (!p?.path) {
      showToast('默认工作空间没有真实目录', 'info')
      return
    }
    const r = await window.agent.openInFinder(p.path)
    if (r && !r.ok) showToast(`无法打开目录：${r.error ?? ''}`, 'error', 4000)
  },
  switchSession: async (sid) => {
    // ── 右栏：**先落盘，再切**（19 篇 §2.5 纪律 3）──────────────────
    // 把上一个会话还压在防抖窗口里的写盘立刻发出去。放在最前面是刻意的：
    // 只要"切会话"这件事一旦发生，任何"还欠着没发"的状态都有写错会话的风险，
    // 所以不依赖任何后续顺序。payload 里带的是**当时的 sid**，与当前会话无关。
    flushRightPanelPending()
    // 进入会话 = 已读。先本地即时置已读（即时反馈），再持久化到后端元数据。
    void get().setSessionUnread(sid, false)
    // 立即高亮 + 切换到该会话缓冲（后台会话继续执行不受影响，仅换投影）。
    // 模型/参数不在此处清空：由后端回发的 session_history 按元数据异步恢复。
    // 同时也把"活动工作空间"对齐到该会话的归属 —— chip 显示的必须是当前会话所在空间。
    const target = get().sessions.find((x) => x.id === sid)
    const pid = target ? sessionProjectId(target) : null
    const prevPid = get().activeProject
    set((s) => ({
      activeSession: sid,
      pendingFresh: null,
      messages: s.messagesBySession[sid] ?? [],
      isSending: s.runningSessions.includes(sid) || s.bgSessions.includes(sid),
      ...(pid ? { activeProject: pid, pendingProjectId: pid } : {}),
      // 切会话清空附件草稿：草稿属于"正在编辑的这条消息"，不能跨会话漂移
      //（文本草稿沿用既有行为不清，差异见 docs/frontend/12 的取舍一节）
      draftAttachments: []
    }))
    // ── 右栏：丢掉别的会话的大块数据缓存（树 / 预览 / diff）────────
    // 只留新会话那一份（与 `currentContextStats` 同范式：树可能 3000 条、
    // diff 可能几百 KB，N 个会话累积明显吃内存，且天然易过期）。
    // `layoutBySession` **不丢**：它要长期留着，切回来时标签栏才能同步恢复、不闪动。
    useRightPanelStore.getState().keepOnly(sid)
    // 后端回放该会话历史并刷新列表；运行中的话由实时缓冲覆盖（见 session_history 处理）
    // 切到别的空间时同步后端"活动空间"：否则下一次 projects 广播会把 chip 拉回去
    if (pid && pid !== prevPid) void window.agent.openProject(pid)
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
      showToast('归档失败', 'error', 4000)
      return
    }
    if (get().activeSession === sid) await get().newSession()
    await get().refreshSessions()
    await get().refreshTrash()
    showToast('已归档，可在设置 → 归档中还原', 'info')
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
      // 右栏：同步丢内存桶（含把该 sid 还压着的待写盘条目一并撤掉，
      // 否则防抖定时器到点会往一个已删除的会话写 meta）
      useRightPanelStore.getState().dropSessions(deleted)
    }
  },

  openSettings: (tab = 'model') => {
    set({ settingsOpen: true, settingsTab: tab })
    if (tab === 'model' && !get().llmConfig) void get().loadLlConfig()
    if (tab === 'permission' && !get().permissionConfig) void get().loadPermissionConfig()
    if (tab === 'sandbox' && !get().sandboxConfig) void get().loadSandboxConfig()
  },
  // 关闭时清掉权限/沙盒配置缓存：配置可能被另一个窗口改过，重进必须重新拉取
  // （未保存的 draft 在组件内 state，随组件卸载自然丢弃）
  closeSettings: () => set({ settingsOpen: false, permissionConfig: null, sandboxConfig: null }),
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
  loadPermissionConfig: async () => {
    try {
      const res = (await window.agent.permissionConfigGet()) as PermissionConfigResult | null
      if (res?.config) {
        set({ permissionConfig: mergePermissionResult(get().permissionConfig, res) })
      }
    } catch {
      /* 后端未就绪时静默忽略 */
    }
  },
  savePermissionConfig: async (config) => {
    set({ permissionSaving: true })
    try {
      const res = (await window.agent.permissionConfigSave(config)) as
        | PermissionConfigResult
        | null
      // 以后端回执为准回填（含归一化修正后的权威值与 warnings）：
      // 保留本地 draft 会让界面显示的内容与磁盘实际存的不一致。
      // 用 merge 而非整份替换 —— save 回执不带 builtin，替换会让权限页塌回加载态。
      if (res?.config) {
        set({ permissionConfig: mergePermissionResult(get().permissionConfig, res) })
      }
      return res?.applied ?? false
    } catch {
      showToast('保存权限配置失败', 'error', 4000)
      return false
    } finally {
      set({ permissionSaving: false })
    }
  },
  loadSandboxConfig: async () => {
    try {
      const res = (await window.agent.sandboxConfigGet()) as SandboxConfigResult | null
      if (res?.platform) set({ sandboxConfig: res })
    } catch {
      /* 后端未就绪时静默忽略 */
    }
  },
  saveSandboxConfig: async (payload) => {
    set({ sandboxSaving: true })
    try {
      const res = (await window.agent.sandboxConfigSave(payload)) as
        | SandboxConfigResult
        | null
      // 以后端回执为准回填（权威值）：开关状态 / 模板内容以磁盘实际为准，
      // 校验错误由沙盒页读 res.errors 内联展示。
      if (res?.platform) set({ sandboxConfig: res })
      return res?.applied ?? false
    } catch {
      showToast('保存沙盒设置失败', 'error', 4000)
      return false
    } finally {
      set({ sandboxSaving: false })
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