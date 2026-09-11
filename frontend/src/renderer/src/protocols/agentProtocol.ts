/**
 * agentProtocol.ts - 前端事件线协议（与后端 streaming_client.py StreamEvent.to_dict 对齐）。
 * JSON 行协议：后端 WSSink 序列化后的每一行就是一条下面的 AgentEvent。
 */

export type StreamEventType =
  | 'thinking_delta'
  | 'content_delta'
  | 'tool_call_start'
  | 'tool_call_delta'
  | 'tool_call'
  | 'turn_end'
  | 'sub_agent_start'
  | 'sub_agent_end'

export interface ContextStats {
  /** 当前会话已用 token（启发式估算） */
  used_tokens: number
  /** 上下文窗口上限 token */
  max_tokens: number
  /** 已用比例 0-100 */
  used_percent: number
  /** 上限可读化文本（如 "1M" / "128k"） */
  max_label: string
}

export interface AgentEvent {
  type: StreamEventType
  /** 事件所属会话号；多会话并发时据此路由到对应消息缓冲 */
  session_num?: number
  text?: string
  /** 工具调用 id。子智能体生命周期事件（sub_agent_start / sub_agent_end）里
   *  表示**发起该子任务的主智能体 tool_call_id** —— 前端据此执行"唯一锚点
   *  规则"（卡片挂在发起它的那条 assistant 消息下，实时与回放一致）。 */
  tool_id?: string
  tool_name?: string
  args?: string
  finish_reason?: string
  usage?: Record<string, number>
  /** 子智能体来源标识：非空表示该事件由某次子智能体任务发出（前端折叠到子智能体块下） */
  subagent_id?: string
}

/** 会话执行状态（后端 → 前端）：驱动侧边栏运行指示 / 完成绿点 / 停止按钮。
 *  background：turn 已结束但该会话的后台任务（如后台子智能体）仍在执行，
 *  侧边栏同样亮运行脉冲点，但不显示停止按钮（stop 只能停 turn）。 */
export type SessionRunStatus = 'running' | 'done' | 'stopped' | 'background'

/** 面向 UI 的产物事件（非增量），由 bridge 把底层 event 聚合/透传而来 */
export type UiEvent =
  | { kind: 'event'; payload: AgentEvent }
  | { kind: 'pong'; payload: { msg?: string } }
  | { kind: 'error'; payload: { msg?: string } }
  | { kind: 'goal_status'; payload: { text: string } }
  | { kind: 'tasks'; payload: { text: string } }
  | { kind: 'skills'; payload: { text: string } }
  | { kind: 'sessions'; payload: { sessions: SessionMeta[] } }
  | { kind: 'sessions_trashed'; payload: { sessions: SessionMeta[] } }
  | { kind: 'session'; payload: { num: number; message_count: number } }
  | { kind: 'session_status'; payload: { num: number; status: SessionRunStatus } }
  | { kind: 'session_history'; payload: { num: number; messages: HistoryMessage[]; model_id?: string | null; overrides?: SessionModelOverridesMap | null } }
  | { kind: 'session_model'; payload: { num: number; model_id?: string | null; overrides?: SessionModelOverridesMap | null } }
  | { kind: 'session_delete_result'; payload: { deleted: number[]; failed: number[] } }
  | { kind: 'llm_config'; payload: LlmConfigResult }
  | { kind: 'context_stats'; payload: { num: number } & ContextStats }

/** 会话历史回放消息（切换会话时后端下发，已过滤 system/tool/系统注入消息） */
export interface HistoryToolCall {
  name: string
  args: string
  /** 子智能体回放行携带：该次工具调用在子智能体内的 id（实时按同 id 归位） */
  tool_id?: string
  /** 工具执行状态（子智能体回放行携带；缺省按已完成处理） */
  status?: string
}

export interface HistorySubAgent {
  id: string
  name: string
  thinking: string
  toolCalls: HistoryToolCall[]
  /** 终态：running（执行中；进程被强杀时只剩占位行）/ done / error / aborted */
  status?: string
  /** 执行耗时（毫秒） */
  durationMs?: number | null
  /** 失败原因（status=error 时非空） */
  error?: string
}

export interface HistoryMessage {
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  toolCalls?: HistoryToolCall[]
  /** 本 assistant 消息下调用过的子智能体执行块（后端由 role=subagent 行挂载，回放展示用） */
  subagents?: HistorySubAgent[]
}

/** 模型能力声明（输入/输出模态：text / image / video / pdf） */
export interface LlmCapabilities {
  input: string[]
  output: string[]
}

/** 大模型配置（来自后端 llmconfig.json v2，以「连接」为中心；providers 为预置目录） */
export interface LlmProviderModel {
  id: string
  display_name: string
  /** 能力/上下文标签（如 1M、图片），仅作下拉展示 */
  tags?: string[]
  /** 标准上下文窗口大小（如 "128k" / "1M"） */
  max_context?: string
  /** 开启「更大上下文」后的窗口大小（若支持扩展） */
  max_context_extended?: string
  /** 支持的思考强度档位（low=轻 / high=高 / very_high=极高） */
  thinking_strengths?: string[]
  /** 默认思考强度档位 */
  default_thinking?: string
  /** 能力声明（输入/输出模态） */
  capabilities?: LlmCapabilities
}
export interface LlmProvider {
  name: string
  base_url: string
  models: LlmProviderModel[]
  /** 默认 API 格式（如 chat_completions） */
  api_format?: string
  /** 密钥来源环境变量名（如 DEEPSEEK_API_KEY） */
  api_key_env?: string
  /** 获取密钥的文档地址 */
  docs_url?: string
}
/** API 格式选项（后端下发，供下拉渲染） */
export interface LlmApiFormat {
  id: string
  label: string
}
/** 模型高级设置（全部可选项；留空/缺省 = 走程序默认，不写入配置文件） */
export interface LlmAdvanced {
  /** 上下文窗口-输入（如 "1M" / "128k" / "8000"） */
  context_in?: string
  /** 上下文窗口-输出 → max_tokens 默认值 */
  context_out?: string
  /** 工具调用轮数 → agent 循环上限 */
  tool_rounds?: string
  /** 支持图片输入：yes/no，空 = 未设置 */
  image_input?: 'yes' | 'no' | ''
  /** 思考模式：跟随模型默认配置/开启/关闭 */
  thinking?: 'default' | 'enabled' | 'disabled' | ''
  temperature?: string
  top_p?: string
  top_k?: string
}
/** 连接内的模型条目（v2：模型不再自带 base_url/api_key，继承所在连接） */
export interface LlmConnectionModel {
  id: string
  /** 实际提交给 API 的模型 id */
  model: string
  display_name: string
  enabled: boolean
  /** 能力/上下文标签（如 1M、图片） */
  tags?: string[]
  /** 上下文窗口-输入（如 "1M" / "128000"），空 = 继承 */
  context_in?: string
  /** 输出上限（Token），空 = 继承 */
  context_out?: string
  capabilities?: LlmCapabilities
  /** 能力来源：auto 自动识别 / manual 手动覆盖 */
  capability_source?: 'auto' | 'manual'
  max_context?: string
  max_context_extended?: string
  thinking_strengths?: string[]
  default_thinking?: string
  /** 高级设置（可选，未配置时不落盘） */
  advanced?: LlmAdvanced
}

/** 连接（= 一个「模型服务」/供应商账号）：承载端点与密钥，下挂多个模型 */
export interface LlmConnection {
  id: string
  /** 预置 catalog key（如 deepseek），或 "custom:<slug>" */
  provider: string
  /** 展示名（可改） */
  name: string
  base_url: string
  api_format: string
  api_key: string
  models: LlmConnectionModel[]
  /** 兼容设置（通常不用改） */
  compat?: Record<string, unknown>
  /** 是否为自定义供应商（非预置 catalog） */
  custom?: boolean
}

/** 扁平模型视图（v2 兼容层：输入区下拉 / 会话绑定 / resolveModelMeta 读取） */
export interface LlmModel extends LlmConnectionModel {
  provider: string
  base_url: string
  api_key: string
  connection_id: string
  connection_name?: string
}

export interface LlmConfig {
  version?: number
  active_model_id: string | null
  /** 连接列表（新 UI 的主数据） */
  connections: LlmConnection[]
  /** 扁平模型视图（兼容既有链路） */
  models: LlmModel[]
  /** 预置厂商目录（~/.aigent/providers.json） */
  providers?: Record<string, LlmProvider>
  /** API 格式选项 */
  api_formats?: LlmApiFormat[]
}
export interface LlmConfigResult {
  config: LlmConfig
  applied?: boolean
  msg?: string
}
/** 保存入参（后端 save_config 只消费 active_model_id + connections） */
export interface LlmConfigPayload {
  active_model_id: string | null
  connections: LlmConnection[]
}
/** 「刷新模型列表」结果（GET {base_url}/models） */
export interface LlmModelsResult {
  ok: boolean
  models: { id: string }[]
  base_url?: string
  error?: string
}

/** 会话元数据（来自后端 index.jsonl + 会话文件统计） */
export interface SessionMeta {
  num: number
  /** 会话标题；null = 未生成（UI 回退显示 session_N） */
  title: string | null
  /** 标题来源：none 未生成 / auto LLM 生成 / trunc 截断兜底 / user 手动重命名 */
  title_source?: 'auto' | 'user' | 'trunc' | 'none'
  status?: 'active' | 'trashed'
  created_at?: string
  updated_at?: string
  trashed_at?: string | null
  message_count: number
  file?: string
  /** 会话绑定的模型 id（记录进会话元数据）；null = 未绑定（用全局） */
  model_id?: string | null
}

/** 会话元数据里记录的模型参数（UI 级：后端不参与窗口换算，仅保存已选档位） */
export interface SessionModelOverrides {
  thinking_strength?: string
  max_context_option?: 'standard' | 'extended'
}

/** 会话元数据里记录的模型参数（按模型 id 分别保存，互不串改） */
export interface SessionModelOverridesMap {
  [modelId: string]: SessionModelOverrides
}

/** 前端 → 后端命令信封（session_new 已移除：新建任务是纯前端态，会话由首条 chat 惰性创建） */
export type ControlKind =
  | 'chat'
  | 'session_switch'
  | 'session_clear'
  | 'session_model'
  | 'sessions_list'
  | 'session_rename'
  | 'session_trash'
  | 'session_restore'
  | 'session_delete'
  | 'trash_list'
  | 'goal_status'
  | 'tasks'
  | 'skills'
  | 'stop'
  | 'llm_config_get'
  | 'llm_config_save'
  | 'llm_models_fetch'

export interface WsOutbound {
  kind: ControlKind | 'ping'
  payload?: Record<string, unknown>
}

/** 前端 → 后端 chat 命令载荷：num 指明目标会话（新建任务无激活会话时省略，由后端领号） */
export interface ChatPayload {
  text: string
  num?: number
  fresh?: boolean
  /** 当前会话请求级覆盖（来自模型下拉悬浮配置面板） */
  overrides?: {
    thinking_strength?: string
    max_context?: string
  }
  /** 当前会话绑定/选择的模型 id（新建任务随首条消息持久化） */
  model_id?: string | null
}

export function parseWsLine(raw: string): UiEvent {
  const parsed = JSON.parse(raw)
  if (!parsed || typeof parsed.kind !== 'string') {
    throw new Error('不合法的事件行: ' + raw)
  }
  return parsed as UiEvent
}

/** 后端未知的流式事件 type：忽略并告警（前后端版本兼容） */
export function isKnownAgentEvent(ev: AgentEvent): boolean {
  return [
    'thinking_delta',
    'content_delta',
    'tool_call_start',
    'tool_call_delta',
    'tool_call',
    'turn_end',
    'sub_agent_start',
    'sub_agent_end'
  ].includes(ev.type)
}