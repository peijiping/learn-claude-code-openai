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
  tool_id?: string
  tool_name?: string
  args?: string
  finish_reason?: string
  usage?: Record<string, number>
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
export interface HistoryMessage {
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  toolCalls?: { name: string; args: string }[]
}

/** 大模型配置（来自后端 llmconfig.json，服务商预置数据由 providers 字段下发） */
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
}
export interface LlmProvider {
  name: string
  base_url: string
  models: LlmProviderModel[]
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
export interface LlmModel {
  id: string
  provider: string
  display_name: string
  /** 实际提交给 API 的模型 id */
  model: string
  base_url: string
  api_key: string
  enabled: boolean
  /** 高级设置（可选，未配置时不落盘） */
  advanced?: LlmAdvanced
}
export interface LlmConfig {
  active_model_id: string | null
  models: LlmModel[]
  providers?: Record<string, LlmProvider>
}
export interface LlmConfigResult {
  config: LlmConfig
  applied?: boolean
  msg?: string
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
    'turn_end'
  ].includes(ev.type)
}