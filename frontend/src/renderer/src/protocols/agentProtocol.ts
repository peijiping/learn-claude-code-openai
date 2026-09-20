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
  /** token 消耗统计（turn 收尾 / 迟到子智能体补发）：usage 为 {turn?, session} 两级汇总 */
  | 'usage_stats'
  /** 子智能体内部工具「开始真正执行」：流聚合完成（tool_call）≠ 执行开始，
   *  执行阶段（往往最耗时）据此把卡片里对应工具行拨回"执行中"，
   *  修复后台子智能体执行期间卡片零更新的断连观感（2026-09-12）。 */
  | 'tool_exec_start'
  | 'tool_exec_end'
  /** 空闲期模型切换：下拉框模型改变时即时上行，携带切换快照 switch
   *  （挂到「切换时最后一条 assistant 消息」上，先于用户下一条指令展示） */
  | 'model_switch'

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

/** token 消耗统计（后端 usage 节点统一结构，四字段；占比由前端计算） */
export interface UsageStats {
  prompt_tokens: number
  completion_tokens: number
  /** 缓存命中 token（OpenAI prompt_tokens_details.cached_tokens / DeepSeek prompt_cache_hit_tokens 归一） */
  cached_tokens: number
  total_tokens: number
  /** 累计轮数（仅会话级 usage_totals 携带） */
  turns?: number
}

/** 轮级模型切换（净变化 = 轮始→轮末）：本轮执行中发生过模型切换时，
 *  随 model_info.switch / usage_stats.model.switch 下发；净切回原模型不携带。 */
export interface ModelSwitch {
  from_id: string
  from_name: string
  to_id: string
  to_name: string
  /** 末次切换时间戳（秒） */
  ts?: number
}

/** 轮级模型快照：本轮实际使用的模型与参数（jsonl 轮末 assistant 行 model_info
 *  节点 / usage_stats 事件 model 字段）。窗口即本轮统计所用口径；老轮次缺省不显示。 */
export interface TurnModelInfo {
  /** llmconfig.json 模型条目 id（如 "m_xxx"；未绑定模型时为空串） */
  model_id: string
  /** 展示名（模型 display_name；未绑定时为 env 模型名） */
  model_name: string
  /** 本轮生效的上下文窗口（token 数；未知为 0） */
  max_context: number
  /** 窗口缩写标签（如 "128K" / "1M"） */
  max_context_label: string
  /** 思考强度档位（low/high/very_high；空 = 未启用/未知） */
  reasoning_effort: string
  /** 本轮净模型切换（仅本轮执行中切换过模型时存在；缺省 = 无切换） */
  switch?: ModelSwitch
}

/** usage_stats 事件载荷：turn（本轮，主 + 子智能体）+ session（会话累计）两级汇总
 *  + model（本轮模型快照）。turn 缺省 = 后台子智能体迟到完成的 session 级补发
 *  （只刷新圆圈 tooltip，不动消息 footer）。 */
export interface UsageStatsEventUsage {
  turn?: UsageStats
  session: UsageStats
  model?: TurnModelInfo
}

export interface AgentEvent {
  type: StreamEventType
  /** 事件所属会话 id（短随机串 / 存量编号字符串）；多会话并发时据此路由到对应消息缓冲 */
  session_id?: string
  text?: string
  /** 工具调用 id。子智能体生命周期事件（sub_agent_start / sub_agent_end）里
   *  表示**发起该子任务的主智能体 tool_call_id** —— 前端据此执行"唯一锚点
   *  规则"（卡片挂在发起它的那条 assistant 消息下，实时与回放一致）。 */
  tool_id?: string
  tool_name?: string
  args?: string
  finish_reason?: string
  /** usage_stats 事件：{turn?, session} 两级汇总（见 UsageStatsEventUsage）；
   *  其余事件（turn_end 遗留）为扁平 usage dict（前端不再消费） */
  usage?: UsageStatsEventUsage | Record<string, number>
  /** 子智能体来源标识：非空表示该事件由某次子智能体任务发出（前端折叠到子智能体块下） */
  subagent_id?: string
  /** model_switch 事件：空闲期模型切换快照（from/to 展示名），挂到切换时最后一条 assistant 消息 */
  switch?: ModelSwitch
}

/** 任务项展示状态。pending / in_progress / completed 是**落盘**状态；
 *  blocked 是**派生**状态 —— 由 blockedBy 实时算出，不写进任务文件
 *  （避免"依赖状态"与"任务状态"双写不一致）。 */
export type TaskItemStatus = 'pending' | 'in_progress' | 'completed' | 'blocked'

/** 任务面板里的一行 */
export interface TaskItem {
  id: string
  subject: string
  /** 落盘状态 */
  status: 'pending' | 'in_progress' | 'completed'
  /** 展示用状态（含派生的 blocked） */
  derived_status: TaskItemStatus
  /** 认领者；主智能体为 "agent"，队友为队友名，null 表示未认领 */
  owner: string | null
  /** 父任务 id（拆子树用，最多 3 层） */
  parentId: string | null
  depth: number
  orderIndex: number
  blockedBy: string[]
  /** 完成摘要（complete_task 的 result） */
  result: string
  started_at: number | null
  updated_at: number
  /** 子项进度（由子项派生，父任务自身 status 不受影响） */
  child_total: number
  child_completed: number
}

/** 任务面板快照：后端每次任务状态变化都推**整份**快照（幂等替换，不做增量）。
 *
 *  - `status`: running = 组内还有未完成任务（面板常驻、不可关闭）；
 *              done    = 本组已全部完成（面板自动收起 + 出现「关闭」）
 *  - `revision`: 组内单调递增，用于丢弃多线程（后台子智能体）乱序到达的旧快照
 *  - `counts.blocked`: 派生口径，与 tasks[].derived_status 一致
 */
export interface TaskBoardSnapshot {
  group_id: string
  revision: number
  status: 'running' | 'done'
  /** 派生字段（后端算，不落盘）：组内**是否真有一条 in_progress**。
   *  与 `status` 是两件事 —— `status` 只回答"活干完没有"，回答不了"现在有人跑吗"：
   *  一条没人认领的 pending/blocked 残留会让 status 长期停在 running。
   *  面板徽标必须以本字段为准，否则会出现"会话已完成但显示执行中"（2026-09-18 修）。
   *  可选：缺字段（旧版后端）时面板回退用 counts.in_progress 近似。 */
  has_in_progress?: boolean
  counts: {
    total: number
    completed: number
    in_progress: number
    pending: number
    blocked: number
  }
  tasks: TaskItem[]
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
  /** 工作空间列表（连接建立时重放 + 增删改后广播）。前端侧边栏空间树与
   *  输入框 chip 下拉都由它驱动；`active` 是后端持久化的"当前活动空间"。 */
  | { kind: 'projects'; payload: ProjectsPayload }
  | { kind: 'sessions_trashed'; payload: { sessions: SessionMeta[] } }
  | { kind: 'session'; payload: { session_id: string; message_count: number; /** 新会话所属工作空间 id（前端据此对齐活动空间） */ project_id?: string } }
  | { kind: 'session_status'; payload: { session_id: string; status: SessionRunStatus } }
  | { kind: 'session_history'; payload: { session_id: string; messages: HistoryMessage[]; model_id?: string | null; overrides?: SessionModelOverridesMap | null; usage_totals?: UsageStats | null } }
  | { kind: 'session_model'; payload: { session_id: string; model_id?: string | null; overrides?: SessionModelOverridesMap | null } }
  | { kind: 'session_delete_result'; payload: { deleted: string[]; failed: string[] } }
  /** 附件登记结果（应答 `attachment_stage`：items=成功项 / failed=逐条原因） */
  | { kind: 'attachments_staged'; payload: AttachmentsStagedPayload }
  | { kind: 'llm_config'; payload: LlmConfigResult }
  | { kind: 'context_stats'; payload: { session_id: string } & ContextStats }
  /** 任务面板快照（整份替换，不做增量）。
   *  board=null 表示该会话当前没有未完成任务组 → 撤掉面板。
   *  会话切换/回放时后端只发未完成组，故已结束的组切回来不会显示。 */
  | { kind: 'task_board'; payload: { session_id: string; board: TaskBoardSnapshot | null } }

/** 附件种类（与后端 attachments.KIND_* 对齐） */
export type AttachmentKind = 'image' | 'document' | 'text'

/** 已发送附件（回放 / 实时消息上都用这个形状渲染）。
 *  字段名保持后端的 snake_case（与 created_at / model_info 等既有约定一致），
 *  避免每个渲染点都做一次 camel 转换。 */
export interface AttachmentRef {
  id: string
  kind: AttachmentKind | ''
  name: string
  mime: string
  ext: string
  size: number
  /** 用户的**原始**文件路径（「在 Finder 中显示」用它） */
  source_path: string
  /** 会话内副本的绝对路径（图片缩略图 / 打开文件用它） */
  stored_path: string
  /** 后端归位时发现文件已不在（手工删过）→ UI 显示「文件已缺失」占位 */
  missing?: boolean
}

/** 后端登记完成（`attachments_staged`）的一条附件 */
export interface StagedAttachment {
  att_id: string
  kind: AttachmentKind | ''
  name: string
  mime: string
  ext: string
  size: number
  source_path: string
  project_id: string
  /** 已抽取的文本字符数（文档类才有；UI 显示"已提取 N 字"） */
  text_chars: number
  text_truncated: boolean
  /** PDF 总页数（其它类型为 null） */
  pages: number | null
}

export interface StagedFailure {
  path: string
  reason: string
}

/** `attachments_staged` 信封载荷：items=成功项，failed=逐条失败原因。
 *  单条失败不影响整批（用户选了 5 个文件不能因为 1 个不支持就全失败）。 */
export interface AttachmentsStagedPayload {
  items: StagedAttachment[]
  failed: StagedFailure[]
  project_id: string
}

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
  /** 消息记录时间（jsonl created_at，秒级 ISO 本地时间如 2026-09-18T10:30:00；
   *  老会话行缺省 → 右下角不显示时间） */
  created_at?: string
  thinking?: string
  toolCalls?: HistoryToolCall[]
  /** 本 assistant 消息下调用过的子智能体执行块（后端由 role=subagent 行挂载，回放展示用） */
  subagents?: HistorySubAgent[]
  /** 本轮 token 消耗（轮末 assistant 行携带；老会话/无消耗轮缺省） */
  usage?: UsageStats
  /** 本轮模型快照（轮末 assistant 行 model_info 节点；老轮次缺省不显示） */
  model_info?: TurnModelInfo
  /** turn 收尾时的会话级累计快照（轮末 assistant 行 usage_session 节点；
   *  回放恢复 footer 第二段「本会话累计」，与实时 usage_stats.session 同构） */
  usage_session?: UsageStats
  /** user 消息携带的附件（后端 `_history_to_ui` 从 content 的引用块 harvest 而来；
   *  无附件的老消息不带这个字段） */
  attachments?: AttachmentRef[]
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

/** 会话元数据（来自后端会话元数据 + 会话文件统计） */
export interface SessionMeta {
  /** 会话 id：短随机串（新会话）/ 存量编号字符串（旧会话）；全链路唯一标识 */
  id: string
  /** 会话标题；null = 未生成（UI 回退显示 session_<id>） */
  title: string | null
  /** 标题来源：none 未生成 / auto LLM 生成 / trunc 截断兜底 / user 手动重命名 */
  title_source?: 'auto' | 'user' | 'trunc' | 'none'
  status?: 'active' | 'trashed'
  created_at?: string
  updated_at?: string
  trashed_at?: string | null
  file?: string
  /** 会话绑定的模型 id（记录进会话元数据）；null = 未绑定（用全局） */
  model_id?: string | null
  /** 所属工作空间 id（多工作空间：前端据此把会话挂到对应空间节点下）。
   *  存量会话缺字段时后端按目录归属兜底，故这里可能缺省（视为 default）。 */
  project?: string
  /** 会话累计 token 消耗（后端 add_usage_totals 写入元数据；无消耗会话缺省） */
  usage_totals?: UsageStats | null
  /** 未读标记：会话完整结束且用户尚未进入查看时为 true（后端元数据持久化，跨重启/多窗口同步） */
  unread?: boolean
}

/**
 * 工作空间元数据（`projects` 信封里的元素）。
 *
 * 一个工作空间 = 一个真实目录 + 一份元数据目录（`~/.aigent/projects/<id>/`）。
 * 它的会话历史 / 任务 / 记忆 / 沙箱根都按 id 隔离（见 docs/frontend/11）。
 */
export interface ProjectMeta {
  /** 工作空间 id：默认空间恒为 "default"，其余为 "ws" + 10 位 base62 短码 */
  id: string
  /** 展示名（新增时默认取文件夹名；同名目录后端自动加 " (2)" 后缀） */
  name: string
  /** 真实目录绝对路径；默认空间为 null（它没有真实目录） */
  path: string | null
  /** 是否为默认工作空间（固定第一位，不可重命名/删除） */
  system: boolean
  /** 真实目录当前是否可达（被删/移动硬盘未挂载 → false，UI 置灰并禁止新建/发送） */
  exists: boolean
  created_at?: string | null
  last_opened_at?: string | null
}

/** `projects` 信封载荷：全部工作空间 + 当前活动空间 */
export interface ProjectsPayload {
  projects: ProjectMeta[]
  /** 当前活动工作空间 id（后端持久化；chip 显示与新建任务归属的默认值） */
  active: string
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
  | 'session_set_unread'
  | 'session_clear'
  | 'session_model'
  | 'sessions_list'
  | 'session_rename'
  | 'session_trash'
  | 'session_restore'
  | 'session_delete'
  | 'trash_list'
  /** 工作空间（多项目）：列表 / 新增（登记目录）/ 切换活动 / 重命名 / 删除 */
  | 'projects_list'
  | 'project_add'
  | 'project_open'
  | 'project_rename'
  | 'project_remove'
  | 'goal_status'
  | 'tasks'
  | 'skills'
  | 'stop'
  | 'status_query'
  /** 附件登记（「添加文件或图片」）：前端把本地绝对路径交给后端复制+解析 */
  | 'attachment_stage'
  | 'llm_config_get'
  | 'llm_config_save'
  | 'llm_models_fetch'

export interface WsOutbound {
  kind: ControlKind | 'ping'
  payload?: Record<string, unknown>
}

/** 前端 → 后端 chat 命令载荷：session_id 指明目标会话（新建任务无激活会话时省略，由后端生成短 id） */
export interface ChatPayload {
  /** 正文。**带附件时可以为空串**（纯附件消息）—— 后端据此判定是否插入文本块。 */
  text: string
  session_id?: string
  fresh?: boolean
  /** 新建任务的归属工作空间 id（点哪个空间的「+」就进哪个空间）。
   *  缺省 = 后端当前活动空间。已有会话无需携带（后端按 session_id 解析归属）。 */
  project_id?: string
  /** 当前会话请求级覆盖（来自模型下拉悬浮配置面板） */
  overrides?: {
    thinking_strength?: string
    max_context?: string
  }
  /** 当前会话绑定/选择的模型 id（新建任务随首条消息持久化） */
  model_id?: string | null
  /** 本轮的附件（`attachment_stage` 登记后拿到的 att_id 列表）。
   *  **只传 id 与少量线索，不传文件内容**：后端按 att_id 从草稿区把文件归位到
   *  会话目录，并在发送边界展开成模型线格式。 */
  attachments?: ChatAttachmentInput[]
}

/** chat 携带的附件线索（真实元数据以磁盘上的 meta.json 为准，前端字段只是线索） */
export interface ChatAttachmentInput {
  att_id: string
  kind: AttachmentKind | ''
  name: string
  mime?: string
  ext: string
  size?: number
  /** 登记时所属工作空间（跨空间场景下后端据此找回草稿） */
  project_id?: string
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
    'sub_agent_end',
    'usage_stats',
    'tool_exec_start',
    'tool_exec_end',
    'model_switch'
  ].includes(ev.type)
}