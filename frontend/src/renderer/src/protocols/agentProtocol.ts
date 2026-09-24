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

/** ── 结构化提问 ask_user（2026-09-21）─────────────────────────────────
 *
 *  模型用 `ask_user` 工具提出 1–4 个选择题并**阻塞等待**作答；前端在输入框
 *  上方弹面板（一题一屏、「下一步」逐步作答），结果作为 tool_result 回填，
 *  模型在**同一个回合内**据此继续。
 *
 *  两条通道的职责划分（重要）：
 *  - **实时**：`ask_request`（下发问题，弹面板）→ `ask_resolved`（清面板 + 落只读小结）；
 *  - **回放**：assistant 行的 `askUsers[]`（问题来自工具参数、答案来自配对上的
 *    tool 行 content）→ 渲染同一个只读小结块。
 *
 *  `result_text` 是**唯一展示载体**：由后端 broker 生成，与回填给模型的
 *  tool_result **逐字节相同**，实时与回放共用同一份文本 —— 所以前端**不做
 *  任何解析**，只原样展示（`white-space: pre-wrap`）。
 */
export interface AskOption {
  label: string
  description?: string
}

export interface AskQuestion {
  /** 批内唯一稳定标识；作答按它对号入座 */
  id: string
  /** ≤12 字短标签，用于步骤条与只读小结块标题 */
  header: string
  question: string
  multi_select: boolean
  /** 是否额外提供「其他」自由文本输入。
   *  **显式字段** —— 不靠 `label === '其他'` 这类隐式约定判断。 */
  allow_custom: boolean
  custom_label: string
  options: AskOption[]
}

/** 一次提问的结局：已作答 / 用户主动取消 / 本轮被停止 */
export type AskStatus = 'answered' | 'cancelled' | 'stopped'

/** 单题作答：`selected` 是命中的 `option.label`（后端会再过滤一次脏值） */
export interface AskAnswer {
  question_id: string
  selected: string[]
  custom_text?: string
}

/** 回放用：按 tool_call_id 配回 assistant 的提问。
 *  `args` 是工具参数 JSON（含 questions），`result` 是配对上的 tool 行 content
 *  （= result_text；空串表示未完成，如进程被杀）。
 *
 *  `status` 由后端 `interaction.status_of_result()` 从 `result` 反推
 *  （jsonl 不存 outcome）—— **前端只搬运、不猜文案**：徽标文案的唯一真相
 *  在后端 interaction 模块，前端 `startswith` 一改常量就会静默错位。 */
export interface HistoryAskUser {
  tool_call_id: string
  args: string
  result: string
  /** completed 结局；`incomplete` = result 为空（未完成） */
  status?: AskStatus | 'incomplete'
}

/** ── 权限管控（2026-09-22，docs/frontend/17）───────────────────────────
 *
 *  两档权限模式（默认 / 完全访问）+ 工具执行前的审批流（范式 C）。
 *  与 ask_user 共用同一套跨线程阻塞基建，但语义不同：ask 问的是**业务问题**
 *  （答案回给模型），审批问的是**裁决问题**（决定不进模型上下文、不进对话历史）。
 */

/** 权限模式：default = 敏感操作逐次审批；full_access = 跳过审批（硬拒绝仍生效） */
export type PermissionMode = 'default' | 'full_access'

/** 审批触发类型（后端 permission.py 的 Decision.trigger） */
export type ApprovalTrigger =
  | 'dangerous_pattern'
  | 'bash_not_allowed'
  | 'outside_workspace'
  | 'mcp_destructive'
  | 'custom_rule'
  | (string & {})

/** 用户决定（approval_answer.decision） */
export type ApprovalDecision = 'allow_once' | 'allow_session' | 'deny'

/** 审批结局（approval_resolved.status；timeout/stopped 由后端自结算） */
export type ApprovalOutcome = 'allowed_once' | 'allowed_session' | 'denied' | 'timeout' | 'stopped'

/** tool 行旁挂的审批结算元数据（jsonl role=tool 行的 approval 字段，§4.4）。
 *  只有拒绝/超时/停止才落盘（allow_* 不写，减少落盘噪音）—— 回放徽标
 *  也只在这三种结局下出现。 */
export interface ApprovalInfo {
  /** 结局（范式 C 词族：denied / timeout / stopped；老数据不含 allow_*） */
  decision: string
  /** 触发类型（§3.6 文案映射） */
  trigger?: string
  /** 触发模式 / 路径 / 工具名（按 trigger 取义） */
  pattern?: string
  /** 当时会话模式 */
  mode?: string
  /** 结算时间 */
  at?: string
}

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
  | { kind: 'session_history'; payload: { session_id: string; messages: HistoryMessage[]; model_id?: string | null; overrides?: SessionModelOverridesMap | null; usage_totals?: UsageStats | null; /** 该会话当前权限档位（2026-09-22）：切会话时恢复盾牌 chip 选中态 */ permission_mode?: PermissionMode; /** 右侧面板状态（2026-09-23）：切会话时恢复"开着的标签 + 当前激活" */ right_panel?: RPanelPersist | null } }
  | { kind: 'session_model'; payload: { session_id: string; model_id?: string | null; overrides?: SessionModelOverridesMap | null } }
  | { kind: 'session_delete_result'; payload: { deleted: string[]; failed: string[] } }
  /** 附件登记结果（应答 `attachment_stage`：items=成功项 / failed=逐条原因） */
  | { kind: 'attachments_staged'; payload: AttachmentsStagedPayload }
  /** 引用候选列表（应答 `refs_list`）。**点对点信封，不进 isKnownAgentEvent 白名单** */
  | { kind: 'refs'; payload: RefsPayload }
  /** 单个文件内容（应答 `file_read`，右栏「文件」预览）。**点对点信封** ——
   *  同 refs：不进 isKnownAgentEvent 白名单（当流式事件处理会静默丢消息）。 */
  | { kind: 'file_content'; payload: FileContentPayload }
  /** git 状态（应答 `git_status`，右栏「变更」面板）。**点对点信封**。 */
  | { kind: 'git_status'; payload: GitStatusPayload }
  /** 单文件 diff（应答 `git_diff`）。**点对点信封**。 */
  | { kind: 'git_diff'; payload: GitDiffPayload }
  | { kind: 'llm_config'; payload: LlmConfigResult }
  /** 权限配置（设置页「权限」页，docs/frontend/18）。**点对点信封**：只回执给发起
   *  窗口、不广播 —— 广播会把另一个窗口正在编辑的未保存 draft 冲掉（对比
   *  permission_changed 必须广播）。因此不进 isKnownAgentEvent 白名单。 */
  | { kind: 'permission_config'; payload: PermissionConfigResult }
  /** 沙盒设置（设置页「沙盒」页，docs/frontend/20）。**点对点信封**（同上）：
   *  get 与 save 回执同构，整份替换 store。 */
  | { kind: 'sandbox_config'; payload: SandboxConfigResult }
  | { kind: 'context_stats'; payload: { session_id: string } & ContextStats }
  /** 任务面板快照（整份替换，不做增量）。
   *  board=null 表示该会话当前没有未完成任务组 → 撤掉面板。
   *  会话切换/回放时后端只发未完成组，故已结束的组切回来不会显示。 */
  | { kind: 'task_board'; payload: { session_id: string; board: TaskBoardSnapshot | null } }
  /** 结构化提问下发（ask_user）：前端弹「交互提问面板」（输入框上方）。
   *  与 task_board 同属**桥层聚合出的 UI 产物事件**，不是流式增量 ——
   *  因此不进 StreamEventType / isKnownAgentEvent 白名单。
   *  断线重连 / 渲染进程刷新时后代会重放在途提问（见 03 文档 §2.10）。 */
  | { kind: 'ask_request'; payload: { session_id: string; request_id: string; tool_call_id: string; questions: AskQuestion[]; created_at: number } }
  /** 提问已解决（作答 / 取消 / 停止）：前端撤面板 + 在该会话的消息下发只读小结。
   *  **必须有这条**：多窗口一致性（A 窗口作答后 B 窗口的面板也要消失）、
   *  提交窗口自身清面板、以及免去 IPC 请求-响应配对（提交是 fire-and-forget）。 */
  | { kind: 'ask_resolved'; payload: { session_id: string; request_id: string; tool_call_id: string; status: AskStatus; answers: AskAnswer[]; result_text: string } }
  /** 权限审批下发（PreToolUse 判定 ask）：前端在消息流里弹审批卡片（锚定
   *  tool_call 折叠条）。与 ask_request 同机制的桥层 UI 产物事件 —— 断线重连 /
   *  渲染进程刷新时后端重放在途审批（`_approval_snapshot_lines()`）。
   *  `args` 是工具参数对象（后端已把超长字符串值截断到 600 字），
   *  `session_scope_hint` 原样展示在「本次会话内允许」按钮副文案（点前知道记什么账）。 */
  | { kind: 'approval_request'; payload: { session_id: string; request_id: string; tool_call_id: string; tool_name: string; args: Record<string, unknown>; trigger: ApprovalTrigger; reason: string; session_scope_hint: string; mode: string; timeout_seconds: number; created_at: number } }
  /** 审批已结算（作答 / 拒绝 / 超时 / 停止）：清卡片 + 在对应工具条上落结算徽标。
   *  同会话多窗口都会收到（多窗口一致）；与 ask_resolved 完全同构。 */
  | { kind: 'approval_resolved'; payload: { session_id: string; request_id: string; tool_call_id: string; status: ApprovalOutcome; decision?: string; at?: number } }
  /** 会话权限模式已切换（session_permission 的回执广播）：前端同步盾牌 chip。
   *  以后端广播为准（不做乐观更新）—— 传输层丢失时乐观 UI 会说谎。 */
  | { kind: 'permission_changed'; payload: { session_id: string; mode: PermissionMode; source?: string } }

/** 附件种类（与后端 attachments.KIND_* 对齐） */
export type AttachmentKind = 'image' | 'document' | 'text'

/** 附件解析统计（2026-09-20）。
 *
 *  草稿、实时消息、回放消息**三条路径共用同一形状** —— UI 由它算出
 *  「2.1MB · 12 页 · 5 图 · 3 表」以及"已降级"状态，避免三处各写一套文案。
 *
 *  字段名保持后端的 snake_case（与 `created_at` / `model_info` 等既有约定一致）。
 *  旧 jsonl 行 / 老后端没有这些键时，前端一律取默认值（0 / '' / []）。 */
export interface AttachmentStats {
  /** 已抽取的文本字符数（文档类才有） */
  text_chars: number
  text_truncated: boolean
  /** PDF 总页数（其它类型为 null） */
  pages: number | null
  /** 随附的图片数量（PDF 页图等） */
  images: number
  /** 识别到的表格数。`find_tables` 对无框线表格命中 0，是**下界** */
  tables: number
  /** 走的哪条转换路径：`pymupdf`（PDF 文本层+页图）/ `office_text`（docx/xlsx/pptx 文本抽取，
   *  2026-09-21 起走统一转换层）/ `fallback_text`（转换层不可用时的兜底）/ `text_layer` / `''` */
  converter: string
  /** 「诚实失败」通道：**非空即表示该附件已降级**（UI 标琥珀 + tooltip 显示原因）。
   *  例：「第 1 页无文本层，已按图像发送」「未提取到文本」「内容已截断」 */
  warnings: string[]
}

/** 已发送附件（回放 / 实时消息上都用这个形状渲染）。
 *  字段名保持后端的 snake_case（与 created_at / model_info 等既有约定一致），
 *  避免每个渲染点都做一次 camel 转换。 */
export interface AttachmentRef extends AttachmentStats {
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
export interface StagedAttachment extends AttachmentStats {
  att_id: string
  kind: AttachmentKind | ''
  name: string
  mime: string
  ext: string
  size: number
  source_path: string
  project_id: string
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

/** `refs` 信封载荷：工作空间内的**扁平**候选列表（无层级）。
 *
 *  `type` 是**列表语义**（`dir` / `file`）；消息里的引用记录用的是 `is_dir`
 *  （见 `RefInput` / `MessageRef`）。两者形状不同是刻意的：前者描述"枚举到的条目"，
 *  后者描述"引用了一条路径"。
 *
 *  `path` 是绝对路径，`name` 是带后缀的名字；前端用 `path` 去掉 `name` 还原所在
 *  目录，故后端**不重复发 dir 字段**。 */
export interface RefListItem {
  path: string
  name: string
  type: 'dir' | 'file'
}

export interface RefsPayload {
  project_id: string
  /** 本次枚举的沙箱根（会话 work_root 快照；与 run_read 同根） */
  workdir: string
  items: RefListItem[]
  /** 命中条目上限 → 列表被截断（前端在底部提示） */
  truncated: boolean
  /** 已扫描到的条目数（含被忽略清单过滤掉的） */
  total_seen: number
  /** 因权限 / 并发删除而跳过的目录数 */
  skipped: number
  /** true = 当前空间不可引用（default 草稿空间 / 目录不可用）→ items 恒为空，
   *  `reason` 给出给用户看的原因。**这是常规状态，不是错误**。 */
  disabled: boolean
  reason?: string
}

/** chat 携带的引用线索。后端**以磁盘为准**重新取 name / is_dir，这里的字段只是线索。 */
export interface RefInput {
  path: string
  name?: string
  is_dir?: boolean
}

/** 消息上的引用（回放与乐观渲染共用同一形状） */
export interface MessageRef {
  path: string
  name: string
  is_dir: boolean
}

/** ── 右侧面板（2026-09-23，docs/frontend/19）─────────────────────────────
 *
 *  右栏是**按会话隔离**的：一个会话一份实例（开着的标签 + 当前激活 + 开合），
 *  状态记在会话元数据 `session_<sid>.meta.json` 的 `right_panel` 字段里，
 *  随 `session_history` 回传恢复。**不是 localStorage** —— 它要与 unread /
 *  permission_mode 同源（跨重启、多窗口一致，删会话即随之消失）。
 */

/** 四个**视图标签**（可用 `+` 菜单按需添加，每类至多一枚）。
 *  「摘要/任务」刻意不在其中：任务面板留在聊天区下方，右栏不重复。 */
export type RPanelView = 'files' | 'changes' | 'terminal' | 'browser'

/** 视图标签的显示名（与后端 `RIGHTPANEL_VIEWS` 同序）。 */
export const RPANEL_VIEW_LABELS: Record<RPanelView, string> = {
  files: '文件',
  changes: '变更',
  terminal: '终端',
  browser: '浏览器'
}

/** 本期可用 / 第二期置灰的视图（终端与浏览器只在菜单与欢迎态出现，置灰标注）。 */
export const RPANEL_AVAILABLE_VIEWS: RPanelView[] = ['files', 'changes']

/** 标签：视图标签 或 文件标签（两类**混在同一条标签栏里**）。
 *
 *  文件标签有两种身份：
 *  - `pinned: false` = **预览位**（全场唯一；2026-09-23 起会话内点文件链接改落
 *    常驻位，预览位仅随「双击固定」的逆操作语义保留，新数据不该再产生）；
 *  - `pinned: true`  = **常驻位**（树里点开 / 会话内点文件链接，独立 tab、互不
 *    顶替，上限 12 枚）。
 *  `name` 是显示用的文件名（basename），与输入区 `@` 胶囊同口径。 */
export type RPanelTab =
  | { kind: 'view'; view: RPanelView }
  | { kind: 'file'; path: string; name: string; pinned: boolean }

/** `session_history` 携带、以及 `session_ui` 上报的右栏状态。
 *  `active` 是 tab key（`view:<view>` / `file:<绝对路径>`），不命中任何标签时
 *  后端会归一化成 null（前端回落"激活末项"，标签全空则显示欢迎态）。 */
export interface RPanelPersist {
  open: boolean
  tabs: RPanelTab[]
  active: string | null
}

/** `session_ui` 上报载荷（fire-and-forget，无点对点回执）。 */
export interface SessionUiPayload {
  session_id: string
  ui: RPanelPersist
}

/** `file_read` 的载荷：只传路径，读写都在后端（沙箱口径唯一）。 */
export interface FileReadPayload {
  session_id?: string
  project_id?: string
  path: string
}

/** `file_content` 回执（应答 `file_read`）。**点对点信封，不进
 *  isKnownAgentEvent 白名单**（同 `refs`）。
 *
 *  降级层次刻意分成四个互斥的标志位，前端据此渲染**不同**的状态 ——
 *  混用会把"文件太大"显示成"读取失败"，是误导：
 *  - `reason` 非空 → 读失败（越界/不存在/是目录/无权限），`text` 为空；
 *  - `binary`   → 二进制，`text` 为空（不是错误）；
 *  - `too_large`→ 超过字节上限，**一点内容都没读**（整屏替换）；
 *  - `truncated`→ 读到了但只保留前 N 行（正文照常渲染 + 顶部横幅）。 */
export interface FileContentPayload {
  project_id?: string
  session_id?: string
  path: string
  name: string
  size: number
  mtime: number
  encoding: string
  binary: boolean
  too_large: boolean
  truncated: boolean
  /** 实际返回的行数（`text.split('\n').length`，供行号槽用） */
  lines: number
  text: string
  reason: string
  /** ── 多格式预览（2026-09-23，docs/frontend/21）────────────────────────
   *  `kind` 是**渲染分支选择器**，由后端按扩展名+魔数分派（前端不再猜）：
   *  - `image` / `pdf` → `aigent-file://` 直读磁盘渲染（`text` 恒空）；
   *  - `office` → 有 `pdf_path` 就渲染转出的 PDF，否则 `text` 是文本抽取降级；
   *  - `text` / `binary` → 代码视图 / 平级空态（原有行为）。
   *  旧后端（无此字段）的回执按 `text` 处理，故给缺省值而不是可选字段。 */
  kind: 'text' | 'image' | 'pdf' | 'office' | 'binary'
  /** 仅 `kind === 'office'` 且 LibreOffice 转换成功时非空：转出 PDF 的绝对路径
   *  （在工作空间 `.aigent/office-preview/` 下，aigent-file 协议读得到）。 */
  pdf_path: string
  /** Office 降级给用户看的原因（"未检测到 LibreOffice…"）；成功时为空串。 */
  office_hint: string
}

/** 变更面板里的一行文件（`path` 是**仓库相对路径**）。 */
export interface GitStatusFile {
  path: string
  /** porcelain 的 X（暂存区状态）单字符 */
  index: string
  /** porcelain 的 Y（工作区状态）单字符 */
  working_dir: string
  /** 两字状态码（`M ` / ` M` / `??` / `UU` …） */
  code: string
  untracked: boolean
  conflicted: boolean
  /** 出现在「已暂存」分组 */
  staged: boolean
  /** 出现在「未暂存」分组（一个文件可以两面都在：暂存后又改） */
  has_working_changes: boolean
  deleted: boolean
  /** 重命名/复制的原路径 */
  orig_path?: string
}

/** `git_status` 回执（应答 `git_status`）。非 git 仓库时 `available:false`
 *  + `reason` 人话 —— 那是**平级空态**，不是错误。 */
export interface GitStatusPayload {
  project_id?: string
  session_id?: string
  available: boolean
  reason: string
  root: string
  branch: string
  ahead: number
  behind: number
  files: GitStatusFile[]
  truncated: boolean
}

/** `git_diff` 的载荷（`path` 为仓库相对路径；`staged` 取暂存区版本）。 */
export interface GitDiffPayloadIn {
  session_id?: string
  project_id?: string
  path: string
  staged?: boolean
}

/** `git_diff` 回执（应答 `git_diff`）。`too_large` 时 `diff` **为空** ——
 *  半截 diff 会让用户以为"改动就这么点"，宁可不显示（见 19 篇 §5.8）。 */
export interface GitDiffPayload {
  project_id?: string
  session_id?: string
  path: string
  staged: boolean
  available: boolean
  reason: string
  diff: string
  chars: number
  too_large: boolean
  binary: boolean
  untracked: boolean
}

/** 会话历史回放消息（切换会话时后端下发，已过滤 system/tool/系统注入消息） */
export interface HistoryToolCall {
  name: string
  args: string
  /** 子智能体回放行携带：该次工具调用在子智能体内的 id（实时按同 id 归位） */
  tool_id?: string
  /** 工具执行状态（子智能体回放行携带；缺省按已完成处理） */
  status?: string
  /** 审批结算元数据（§4.4：拒绝/超时/停止的工具行旁挂，回放渲染「已拒绝」徽标；
   *  允许执行的工具行不写 —— 无字段 = 旧会话/正常流，按普通工具条渲染） */
  approval?: ApprovalInfo
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
  /** user 消息引用的工作空间路径（后端 `_history_to_ui` harvest；**无引用时连字段
   *  都不带** —— 与改造前的回放形状逐字节一致） */
  refs?: MessageRef[]
  /** assistant 消息下发起的结构化提问（ask_user，2026-09-21）。
   *  后端 `_history_to_ui` 按 `tool_call_id` 把 tool 行的 content 配对回来；
   *  这些提问**不会**出现在 `toolCalls` 里（不以普通工具条展示）。
   *  **无提问时连字段都不带** —— 与改造前逐字节一致。 */
  askUsers?: HistoryAskUser[]
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
  /** 预置厂商目录（~/.aigent/config/providers.json） */
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

// ── 权限配置（docs/frontend/17 §5.4 / 18，2026-09-22）────────────────────
// 规则文件是 ~/.aigent/config/permissions.json（与 llmconfig.json 同级的完整结构化
// 配置）。下面这些键与设置页七分区一一对应（18 篇 §3.2）—— 原「⑦ 自定义规则」
// 已于 2026-09-22 下线，`rules` 键保留但恒为空（迁移规则见 agents/permission.py）。

/** **已下线（2026-09-22）**：自定义规则区已从设置页移除，它的三种动作分别由
 *  ⑥ 硬拒绝 / ⑤ 危险命令 / ④ 安全命令白名单 承担（同一语义不再有两个写入口）。
 *  保留类型仅为兼容旧回执；后端 `_normalize` 会把存量 `rules` 迁移进上述三处，
 *  并让该字段恒为空数组。 */
export interface PermissionRule {
  /** 匹配动作：allow=跳过审批直接放行 / deny=硬拒绝 / ask=送审批 */
  action: 'allow' | 'deny' | 'ask'
  /** 匹配模式（与内置清单同语法，见 17 篇 §3.4） */
  pattern: string
  /** 备注（仅展示） */
  note?: string
}

export interface PermissionSafeCommands {
  /** 白名单总开关（false = 命令一律进审批流程；**不是**清空 list） */
  enabled: boolean
  /** 自定义白名单。**空数组会回落到内置全量** —— 想关白名单要用 enabled=false */
  list: string[]
}

/** 归一化后的权限配置（后端 `PermissionStore._normalize` 的权威输出） */
export interface PermissionConfig {
  version: number
  /** 全局兜底档位：**只影响新建会话**（已有会话各有自己的档位） */
  default_mode: PermissionMode
  /** 审批等待超时（秒，60–3600） */
  approval_timeout_seconds: number
  /** MCP 破坏性工具策略：ask=始终询问（完全访问下也问）/ allow=完全访问自动放行 */
  mcp_destructive: 'ask' | 'allow'
  /** 全局额外目录（其内读写视同工作区；敏感路径仍被硬拒） */
  additional_dirs: string[]
  safe_commands: PermissionSafeCommands
  /** 追加的硬拒绝模式（任何模式不可放行，含完全访问） */
  deny_patterns: string[]
  /** 追加的危险模式（默认模式送审批；完全访问放行） */
  dangerous_patterns: string[]
  /** **已下线（2026-09-22）**：后端恒返回空数组，设置页不再渲染该区。
   *  存量内容已按 action 迁入 deny_patterns / dangerous_patterns / safe_commands。 */
  rules: PermissionRule[]
}

/** 内置清单（只读展示）。**由后端下发，前端零硬编码** —— 前端自建一份就会与后端
 *  常量漂移，正是 17 篇 §1.1「两份黑名单不同步」缺陷模式的重演。 */
/** 判定顺序的一档（设置页「判定顺序」区块）。**由后端下发** —— 它与
 *  `evaluate` / `_bash_category_decision` 的实现次序同源，前端自建一份必然漂移。 */
export interface PermissionOrderItem {
  /** 档次标识：deny / dangerous / safe / other（用于样式区分） */
  key: string
  /** 展示名（如「硬拒绝」） */
  label: string
  /** 命中后的处置（如「直接拒绝」） */
  effect: string
  /** 它在链上的位置说明（如「最先判定 · 不可越过」） */
  rank: string
  /** 一句话解释，含典型用法 */
  note: string
}

export interface PermissionBuiltin {
  safe_commands: string[]
  dangerous: string[]
  deny: string[]
  /** 敏感路径黑名单的人类可读描述（展示用） */
  deny_paths: string[]
  timeout: { default: number; min: number; max: number }
  /** 判定顺序（数组顺序即判定先后）。旧后端不下发时调用方需兜底为空数组。 */
  order?: PermissionOrderItem[]
}

export interface PermissionConfigResult {
  config: PermissionConfig
  /** 内置清单（get 回执有；save 回执可省） */
  builtin?: PermissionBuiltin
  /** 配置文件绝对路径（「配置文件位置」展示用） */
  path?: string
  /** 是否已存在配置文件；false → 首屏提示「尚未保存过自定义配置」 */
  exists?: boolean
  /** 保存回执：false 只在写盘失败时出现 */
  applied?: boolean
  /** 归一化修正说明（人话），保存后内联展示 */
  warnings?: string[]
  msg?: string
}
/** 「刷新模型列表」结果（GET {base_url}/models） */
export interface LlmModelsResult {
  ok: boolean
  models: { id: string }[]
  base_url?: string
  error?: string
}

/** 沙盒设置回执（设置页「沙盒」页，docs/frontend/20）。
 *  get 回执与 save 回执同构（save 多带 applied/errors），前端整份替换 store。 */
export interface SandboxConfigResult {
  /** 后端 sys.platform（darwin / linux / win32 …） */
  platform: string
  /** 探测到的后端名（seatbelt / bwrap）；null = 当前环境无可用后端 */
  backend: string | null
  /** 后端是否可用（false = 启用后也会裸跑，状态行提示） */
  backend_available: boolean
  /** 沙盒总开关（config.json 的 SANDBOX_ENABLED，保存后热生效） */
  sandbox_enabled: boolean
  /** macOS Seatbelt profile 模板内容（~/.aigent/sandbox/seatbelt.sb） */
  seatbelt_profile: string
  /** Linux bubblewrap 参数模板内容（~/.aigent/sandbox/bwrap_args.txt） */
  bwrap_args: string
  /** 模板文件绝对路径（编辑器上方「配置文件位置」展示用） */
  seatbelt_path?: string
  bwrap_path?: string
  /** save 回执：false = 有字段保存失败（errors 里带原因，如模板缺占位符） */
  applied?: boolean
  /** save 校验错误（人话，页面内联展示） */
  errors?: string[]
}

/** 沙盒设置保存载荷（**字段部分更新**：只带要改的字段） */
export interface SandboxConfigSavePayload {
  sandbox_enabled?: boolean
  seatbelt_profile?: string
  bwrap_args?: string
  /** 恢复默认模板（不与 *_profile/*_args 同发） */
  reset?: 'seatbelt' | 'bwrap'
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
  /** 该会话当前权限档位（2026-09-22 权限管控）：盾牌 chip 的权威数据源之一。
   *  老会话/老后端缺省时按 "default" 处理。 */
  permission_mode?: PermissionMode
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
  /** 本空间最后更改的权限档位（2026-09-22）：**新建会话 chip 的默认档位来源**
   *  （继承链：会话 meta ← 工作空间最后更改值 ← 全局 default_mode）。
   *  会话内切换模式时后端会同步写回这里（用户需求 #4）。 */
  permission_mode?: PermissionMode
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
  /** 引用候选列表（「引用文件或文件夹」）：输入 @ 时拉一次完整扁平列表 */
  | 'refs_list'
  | 'llm_config_get'
  | 'llm_config_save'
  /** 权限配置读 / 保存（设置页「权限」页；回执为 permission_config 点对点信封） */
  | 'permission_config_get'
  | 'permission_config_save'
  /** 沙盒设置读 / 保存（设置页「沙盒」页，docs/frontend/20；回执为 sandbox_config
   *  点对点信封。save 是**字段部分更新**载荷，见 SandboxConfigSavePayload） */
  | 'sandbox_config_get'
  | 'sandbox_config_save'
  | 'llm_models_fetch'
  /** 结构化提问的作答（ask_user）。**fire-and-forget，无点对点回包** ——
   *  回执走 `ask_resolved` 广播。刻意不走 request()：主进程 pending 表按 kind
   *  FIFO 配对且无 id，同 kind 并发会串台、还会被广播信封误消费。 */
  | 'ask_answer'
  /** 取消本次提问（按"未作答、请自行选默认方案继续"回填）。同样 fire-and-forget。 */
  | 'ask_cancel'
  /** 权限审批作答（2026-09-22 权限管控）。**fire-and-forget，无点对点回包** ——
   *  回执走 `approval_resolved` 广播；迟到/重复作答由后端幂等丢弃（同 ask_answer，
   *  刻意不走 request()：主进程 pending 表按 kind FIFO 配对会串台）。 */
  | 'approval_answer'
  /** 切换会话权限档位（默认 / 完全访问）。fire-and-forget：成功后后端广播
   *  `permission_changed`（多窗口一致）。 */
  | 'session_permission'
  /** 新建任务（无会话）态切换**目标工作空间**的权限档位（2026-09-22，§5.2）。
   *  fire-and-forget：成功后后端广播 `projects` 刷新（无会话号，不带
   *  permission_changed）—— chip 选中态由 projects 广播驱动。 */
  | 'project_permission'
  /** 右侧面板状态上报（2026-09-23，docs/frontend/19）。**fire-and-forget，
   *  无点对点回包** —— 与 ask_answer 同款：主进程 pending 表按 kind FIFO 配对
   *  且无 id，同 kind 并发会串台。前端做 400ms 防抖合并上报，丢一两条只影响
   *  下一次恢复的精确度（状态本身以内存桶为准）。 */
  | 'session_ui'
  /** 右栏「文件」预览：读工作空间内单个文件 → 回执 `file_content` */
  | 'file_read'
  /** 右栏「变更」：git 状态 → 回执 `git_status` */
  | 'git_status'
  /** 右栏「变更」：单文件 diff → 回执 `git_diff` */
  | 'git_diff'

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
  /** 本轮的引用（工作空间内的文件/目录路径）。
   *  **零复制、零存储**：后端只做越界校验与规范化，然后挂一个中性引用块，
   *  把「路径清单 + 内容不在上下文中、需要时用 run_read」注入模型上下文。
   *  与 attachments 是**并列且独立**的两条通道。 */
  refs?: RefInput[]
}

/** 前端 → 后端：提交选择题答案（kind='ask_answer'）。
 *  只传命中的 label 与自定义文本；后端会按问题定义再过滤一次脏值。 */
export interface AskAnswerPayload {
  session_id: string
  request_id: string
  answers: AskAnswer[]
}

/** 前端 → 后端：取消本次提问（kind='ask_cancel'）。 */
export interface AskCancelPayload {
  session_id: string
  request_id: string
}

/** 前端 → 后端：提交审批裁决（kind='approval_answer'，fire-and-forget）。
 *  迟到/重复由后端幂等丢弃。 */
export interface ApprovalAnswerPayload {
  session_id: string
  request_id: string
  decision: ApprovalDecision
}

/** 前端 → 后端：切换会话权限档位（kind='session_permission'，fire-and-forget）。
 *  成功后后端广播 permission_changed（前端以后端广播为准更新 chip）。 */
export interface SessionPermissionPayload {
  session_id: string
  mode: PermissionMode
}

/** 前端 → 后端：新建任务态切换目标工作空间权限档位（kind='project_permission'）。
 *  只写 projects.json 的「最后更改值」；成功后后端广播 projects 刷新。 */
export interface ProjectPermissionPayload {
  project_id: string
  mode: PermissionMode
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