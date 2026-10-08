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

/** 任务执行模式（2026-09-25，docs/frontend/22）：与权限档位**正交**的另一条轴。
 *  - `normal` 什么都不显示（默认，**零占位**）
 *  - `plan`   先出计划文书，未经批准不得改动系统（写操作被 PreToolUse 守卫拦下）
 *  - `goal`   朝一个明确条件反复推进直到达成 —— 是对既有 `GoalController` 的
 *             **投影**，唯一真相是 `goal_controller.active`（后端铁律，见 execution_state）
 *  两条轴概念独立、状态独立、UI 入口独立、**判定链不合并**。 */
export type ExecutionMode = 'normal' | 'plan' | 'goal'

/** 计划文书状态（`plan` 模式）：ready = 已产出待批准；approved = 已批准（卡片转只读）。 */
export type PlanStatus = 'ready' | 'approved'

/** 目标 Stop 裁决的结论（后端 `StopDecision.action`）。
 *  与 `goal.py` 的 action 词族逐字对齐：
 *  - block    未达成，回环继续（**唯一会进模型上下文**的一档）
 *  - achieved 已达成（目标已清空）
 *  - failed   判定无法完成（目标已清空）
 *  - limit    连续 block 超上限，强制结束（**目标保持激活**）
 *  - error    评估器调用出错（**目标保持激活**）
 *  - defer    后台任务仍在跑，本轮暂缓判定（**目标保持激活**） */
export type GoalAction = 'block' | 'achieved' | 'failed' | 'limit' | 'error' | 'defer'

/** 目标模式的消息级标记（2026-09-30 目标可见化，docs/frontend/22 §6.6）。
 *
 *  四处同形（jsonl user 行的 `goal` 字段 / `goal_check` 事件载荷 /
 *  后端 `GoalState.snapshot()` / 前端 `Message.goal`），因此只有一个类型。
 *
 *  `kind` 的三种取值对应三条完全不同的渲染路径：
 *  - `instruction` 用户那条**被设为目标**的指令 → 照常渲染气泡 + 挂「已设为执行目标」徽标
 *  - `set`         `[Goal set]` 内部消息 → 渲染成「目标设定」卡片（不显示原英文正文）
 *  - `check`       每轮 Stop 裁决结果 → 渲染成「目标检查」卡片
 */
export interface GoalMarker {
  kind: 'instruction' | 'set' | 'check'
  /** 目标条件（三种 kind 都有；`check` 里是快照当时的条件） */
  condition?: string
  /** 结论（仅 `kind === 'check'`） */
  action?: GoalAction
  /** 第几轮（= 后端 `GoalController.active.iterations`，每完成一次评估 +1）。
   *  检查卡片显示该值；常驻目标条显示 `round + 1`（当前进行/即将进行的一轮）。 */
  round?: number
  /** 评估器给出的判定理由 / 偏差说明（仅 `kind === 'check'`） */
  reason?: string
  /** 目标已运行秒数（仅 `kind === 'check'`） */
  elapsed?: number
  /** 目标期间消耗的 token（仅 `kind === 'check'`） */
  tokens?: number
  /** 记录时间（ISO，秒级本地时间） */
  at?: string
  /** **仅前端乐观态使用**（2026-09-30）：`send()` 在发送那一刻给目标指令消息
   *  打上徽标时置 true，表示"这条还没有被后端确认"。后端若因条件非法静默忽略
   *  整个目标预选（它不回复执），没人会来纠正这枚徽标 —— 故由 `settleGoalBadges`
   *  在后端给出权威模式时收敛：goal → 摘掉 pending（徽标留下）；非 goal → 整枚撤掉。
   *  落盘的 jsonl 与回放**永远不带这个字段**。 */
  pending?: boolean
}

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
  /** 目标检查结果（2026-09-30 目标可见化，docs/frontend/22 §6.2）。
   *  每轮 Stop 裁决后由 Agent 经 `_emit_kind` 推送（线程安全），前端把它
   *  按时间顺序 append 进该会话的消息流，渲染成一张「目标检查」卡片。
   *  与落盘的 jsonl 记录（user 行旁挂 `goal`）**同源同形** —— 实时与回放
   *  共用同一个渲染组件，不存在两套口径。 */
  | { kind: 'goal_check'; payload: {
      session_id: string
      kind: 'check'
      action: GoalAction
      round: number
      reason: string
      condition: string
      elapsed: number
      tokens: number
      at: string
    } }
  | { kind: 'tasks'; payload: { text: string } }
  | { kind: 'skills'; payload: { text: string } }
  | { kind: 'sessions'; payload: { sessions: SessionMeta[] } }
  /** 工作空间列表（连接建立时重放 + 增删改后广播）。前端侧边栏空间树与
   *  输入框 chip 下拉都由它驱动；`active` 是后端持久化的"当前活动空间"。 */
  | { kind: 'projects'; payload: ProjectsPayload }
  | { kind: 'sessions_trashed'; payload: { sessions: SessionMeta[] } }
  | { kind: 'session'; payload: { session_id: string; message_count: number; /** 新会话所属工作空间 id（前端据此对齐活动空间） */ project_id?: string } }
  | { kind: 'session_status'; payload: { session_id: string; status: SessionRunStatus } }
  | { kind: 'session_history'; payload: { session_id: string; messages: HistoryMessage[]; model_id?: string | null; overrides?: SessionModelOverridesMap | null; usage_totals?: UsageStats | null; /** 该会话当前权限档位（2026-09-22）：切会话时恢复盾牌 chip 选中态 */ permission_mode?: PermissionMode; /** 右侧面板状态（2026-09-23）：切会话时恢复"开着的标签 + 当前激活" */ right_panel?: RPanelPersist | null; /** ── 任务执行模式 4 字段（2026-09-25，docs/frontend/22）─────────────
   *  切会话 / 回放时恢复胶囊 tag 与计划卡片外壳。
   *  `plan_path`（2026-09-29 改口径）：**相对工作空间**的 `.aiagent/plan/<name>.md`，
   *  由后端按 meta 的 `plan_name` 拼出；存量会话回退旧的元数据目录绝对路径。
  *  正文仍经 `plan_read` 拉取。
   *  ⚠ 断线重连**不走这条信道**（重放序列不含 session_history）→ 恢复还依赖
   *  `sessions` 列表载荷的同名 4 字段，两处都要带上。
   *  `goal_round` / `goal_started_at`（2026-09-30 目标可见化）：常驻目标条要在
   *  切会话后显示正确的「第 N 轮」与已运行时长（同 `goal_condition` 是可空投影）。 */
    execution_mode?: ExecutionMode; plan_status?: PlanStatus | null; plan_path?: string | null; goal_condition?: string | null; goal_round?: number | null; goal_started_at?: number | null } }
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
  /** MCP 服务器配置（设置页「MCP」页，docs/frontend/23）。**点对点信封**（同上）：
   *  `mcp_config_get` / `mcp_server_upsert` / `mcp_server_remove` 三条共用同形回执，
   *  每次都回读全量 → 前端整份替换。 */
  | { kind: 'mcp_config'; payload: McpConfigResult }
  /** MCP 一次性试连结果（应答 `mcp_server_test`）。**点对点信封**。
   *  成功时 `tools` 是该 server 暴露的工具原名清单（用于「先验证再保存」）。 */
  | { kind: 'mcp_test'; payload: McpTestResult }
  /** 市场搜索结果（应答 `mcp_market_search`）。**点对点信封**。
   *  `error` 非空时 `items` 为空 —— 那是"搜不到"和"搜失败"的区别，前端必须分开渲染。 */
  | { kind: 'mcp_market'; payload: McpMarketResult }
  /** 市场条目 → 配置的翻译结果（应答 `mcp_market_resolve`）。**点对点信封**。
   *  不落盘：只用于安装确认弹窗展示将写入的 command/args/url 原文。 */
  | { kind: 'mcp_market_plan'; payload: McpMarketPlan }
  /** 本地包安装计划（应答 `mcp_pkg_resolve`）。**点对点信封**。
   *  不落盘、不下载：只用于「下载到本地」确认区展示版本/哈希/依赖规模/脚本清单。
   *  ⚠️ 与 `mcp_market_plan` 是**两条**命令 —— 前者把 command 写成 `npx`，
   *  后者写成包内 bin 的绝对路径，混用会装出指向错误路径的条目。 */
  | { kind: 'mcp_pkg_plan'; payload: McpPkgPlan }
  /** 技能配置（设置页「技能」页，docs/frontend/24）。**点对点信封**（同 mcp_config）：
   *  `skill_config_get` / `skill_set_enabled` / `skill_remove` / `skill_install` /
   *  `skill_market_upsert` / `skill_market_remove` 六条共用同形回执，整份替换。 */
  | { kind: 'skill_config'; payload: SkillConfigResult }
  /** 单个技能的 SKILL.md 全文（应答 `skill_read`）。**点对点信封**。 */
  | { kind: 'skill_content'; payload: SkillContentResult }
  /** 技能市场搜索结果（应答 `skill_market_search`）。**点对点信封**。
   *  `error` 非空时 `items` 为空 —— 前端必须把「搜失败」与「搜不到」分开渲染。 */
  | { kind: 'skill_market'; payload: SkillMarketResult }
  /** 技能安装计划（应答 `skill_market_resolve`）。**点对点信封**。
   *  不落盘：只用于安装确认页展示 SKILL.md 全文与文件清单。 */
  | { kind: 'skill_market_plan'; payload: SkillMarketPlan }
  /** 插件配置（设置页「插件」页，docs/frontend/25）。**点对点信封**。 */
  | { kind: 'plugin_config'; payload: PluginConfigResult }
  /** 插件详情（应答 `plugin_read`）：plugin.json 原文 + 文件清单。**点对点信封**。 */
  | { kind: 'plugin_content'; payload: PluginContentResult }
  /** 插件市场搜索结果（应答 `plugin_market_search`）。**点对点信封**。 */
  | { kind: 'plugin_market'; payload: PluginMarketResult }
  /** 插件安装计划（应答 `plugin_market_resolve`）。**点对点信封**。
   *  不落盘：只用于安装确认页列出该插件将贡献的全部组件。 */
  | { kind: 'plugin_market_plan'; payload: PluginMarketPlan }
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
  /** 任务执行模式已切换（2026-09-25，docs/frontend/22）。**三条来源共用同一条
   *  投递路径**（用户点 tag 切模式 / goal 达成自动回落 / plan 批准续跑）—— 后端
   *  `Agent.execution_mode_sink` 统一投递，避免两套口径漂移。
   *  与 `permission_changed` 同款：前端**不做乐观更新**，tag 选中态只认这条广播。 */
  | { kind: 'execution_mode_changed'; payload: ExecutionModeChangedPayload }
  /** 计划文书已产出（`plan_write` 落盘后推）。**不带正文** —— 正文由
   *  `plan_read` 拉取（实时与回放共用同一条链路）。
   *  推送顺序契约：**先** `execution_mode_changed`（已带 `plan_status="ready"`
   *  与 `plan_path`）**再** 本信封，保证前端处理卡片时 tag 状态已就位。
   *
   *  `plan_path`（2026-09-29）：相对工作空间的 `.aiagent/plan/<name>.md`。
   *  前端拿它**自动打开右侧面板**并落一枚文件标签 —— 这正是"文书放进工作区"
   *  换来的能力（旧口径在元数据目录里，右栏读不到）。`plan_name` 是文件名，
   *  供标签标题显示。 */
  | { kind: 'plan_ready'; payload: { session_id: string; plan_status: PlanStatus; plan_path?: string | null; plan_name?: string | null } }
  /** 计划文书正文（应答 `plan_read` 命令）。**点对点信封** —— 同 `file_content`：
   *  不进 isKnownAgentEvent 白名单（当流式事件处理会静默丢消息）。形状与其同族，
   *  前端复用一套降级字段解析。 */
  | { kind: 'plan_content'; payload: PlanContentPayload }

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

/** 计划文书正文（应答 `plan_read`，docs/frontend/22 §4.5）。
 *
 *  **形状刻意与 `FileContentPayload` 同族**（同一套降级字段），前端复用一套解析：
 *  - `reason` 非空 = 读不到（文件被清理 / 空间目录不可用），前端渲染占位块而非白屏；
 *  - `too_large` = 超过 `refs.file_preview_max_bytes()`（512KB）→ **一点内容都不给**
 *    （"宁可不给，不给半个"），`text` 为空串。
 *
 *  为什么仍**不是** `file_read`：卡片只知道 sid，路径要由后端从 meta 的
 *  `plan_name` 解析（口径唯一），而且本回执的形状是 plan 专用的最小子集
 *  （不带 image/pdf/office 那套分派字段）。2026-09-29 起文书虽已在工作空间内，
 *  读取链路仍保持独立 —— 越界校验由后端用 `resolve_within` 兜住。 */
export interface PlanContentPayload {
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
  lines: number
  text: string
  reason: string
}

/** 执行模式变更广播载荷（`execution_mode_changed`）。
 *  与后端 `Agent.execution_state()` **逐字段同形** —— 那个方法同时服务本信封与
 *  `session_history` 的 4 字段，前端契约因此只有一份。 */
export interface ExecutionModeChangedPayload {
  session_id: string
  mode: ExecutionMode
  /** plan 状态（非 plan 模式时为 null）。 */
  plan_status: PlanStatus | null
  /** 计划文书路径（无计划状态时为 null）。**相对工作空间**
   *  （`.aiagent/plan/<name>.md`）；存量会话是旧的元数据目录绝对路径。
   *  相对路径是右栏标签与 `file_read` 的天然口径，故直接用它开面板。 */
  plan_path: string | null
  /** 目标条件（goal 模式）。 */
  goal_condition: string | null
  /** 目标已完成/进行到第几轮（= 后端 `active.iterations`）。非 goal 时为 null。
   *  常驻目标条显示的是 `goal_round + 1`（当前进行/即将进行的一轮）。 */
  goal_round: number | null
  /** 目标设置时刻（Unix 秒）。常驻目标条据此本地 tick 出"已运行多久"。 */
  goal_started_at: number | null
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
  /** `goal_check`（2026-09-30 目标可见化）：目标检查/设定的展示卡，不是对话
   *  的一轮 —— 后端 `_history_to_ui` 把落盘 user 行旁挂的 `goal` 标记
   *  （kind=check/set）转成它，渲染端由 `MessageItem` 单独分支处理。 */
  role: 'user' | 'assistant' | 'goal_check'
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
  /** 目标模式标记（2026-09-30 目标可见化，docs/frontend/22 §6.6）。
   *  - `instruction`：这条 user 消息就是被设为执行目标的那条指令 → 挂徽标；
   *  - `check` / `set`：本轮检查结果 / 目标设定 → 后端已把 role 转成
   *    `goal_check`（走卡片渲染），此字段只用于承载内容。
   *  **无标记的老消息不带这个字段** —— 回放形状与改造前逐字节一致。 */
  goal?: GoalMarker
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
 *  get 回执与 save 回执同构（save 多带 applied/errors），前端整份替换 store。
 *  注意状态行需要 `sandbox_enabled` 与 `backend_available` **两个**字段一起判：
 *  只看后者会出现"开关已关、界面还写生效中"（2026-09-24 修）。 */
export interface SandboxConfigResult {
  /** 后端 sys.platform（darwin / linux / win32 …） */
  platform: string
  /** 探测到的后端名（seatbelt / bwrap）；null = 当前环境无可用后端 */
  backend: string | null
  /** 后端是否可用（false = 启用后也会裸跑，状态行提示） */
  backend_available: boolean
  /** 后端不可用原因：ok / off（SANDBOX_BACKEND=off）/ unsupported / error */
  reason?: string | null
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

/** MCP 传输类型（与后端 mcp_manager 的三条分派一致）。
 *  缺 `type` 时后端按「有 command 走 stdio、否则 streamable-http」推断，
 *  但设置页始终显式给出，避免歧义。 */
export type McpTransport = 'stdio' | 'sse' | 'streamable-http'

/** MCP 条目状态五态（后端 `_mcp_config_payload_sync` 计算，前端零硬编码推导）。
 *  - `disabled`     条目 enable=0，不参与连接
 *  - `connected`    至少一个 MCPManager 已连上（工具清单可用）
 *  - `error`        有确切失败原因 `last_error`（连接失败 → 内联红字）
 *  - `idle`         无任何可观测的 MCPManager（理论上仅当全局 Agent 构造失败）
 *  - `disconnected` 有 manager、没连上、也没留下错因（刚写盘、下一轮才 reconcile） */
export type McpServerStatus = 'disabled' | 'connected' | 'error' | 'idle' | 'disconnected'

/** 单条 MCP 服务器（设置页「MCP」页，docs/frontend/23）。
 *
 *  ⚠️ `env` / `headers` 里的密钥值（`*_KEY`/`*_TOKEN`/`*_SECRET`/`*_AUTH` 类键名）
 *  回传时**已被后端脱敏成 `••••••`**。保存编辑后的表单时**原样回传**该掩码即表示
 *  「不改这一项」—— 后端会把它换回磁盘上的真值（`McpStore._merge_secret`）。
 *  千万不要在前端把它当成真实值展示给用户看。 */
export interface McpServer {
  name: string
  enable: boolean
  transport: McpTransport
  command?: string | null
  args: string[]
  env?: Record<string, string> | null
  cwd?: string | null
  url?: string | null
  headers?: Record<string, string> | null
  /** 来源：`market` = 从市场安装（带 market_id），`local` = 手填/用户自建 */
  source: 'local' | 'market'
  market_id?: string | null
  market_name?: string | null
  /** 发布者信任档：official / community / domain-verified（仅市场来源有） */
  publisher?: string | null
  installed_at?: string | null
  status: McpServerStatus
  /** 已发现的 MCP 工具原名（未加 `mcp__<server>__` 前缀） */
  tools: string[]
  tool_count: number
  last_error?: string | null
}

/** `mcp_config` 回执 —— `mcp_config_get` / `mcp_server_upsert` / `mcp_server_remove`
 *  三条命令**共用同一信封**（每次操作都回读全量，前端整份替换，不做出增量拼接）。
 *  **点对点信封**：只回发起窗口、不广播（同 permission_config / sandbox_config：
 *  广播会冲掉另一个窗口正在编辑的 draft）→ 不进 isKnownAgentEvent 白名单。 */
export interface McpConfigResult {
  /** `~/.aigent/mcp/mcp_servers.json` 绝对路径（页面展示用） */
  path?: string
  /** 旁路元数据文件路径（`mcp_sources.json`） */
  sources_path?: string
  exists?: boolean
  servers: McpServer[]
  /** 本地已安装包（`~/.aigent/mcp/pkgs/` 扫描结果，docs/frontend/23 §本地安装）。
   *  与 `servers` **放在同一份回执里**：设置页打开时两边都要显示，分两次往返
   *  会出现"条目已刷新、本地包还是旧的"这种中间态。 */
  packages?: McpLocalPkg[]
  /** 本地包根目录的绝对路径（页面展示用） */
  pkgs_dir?: string
  /** 可观测的 MCPManager 实例数（全局 Agent + 各会话 runtime）。
   *  =0 时所有启用条目都会是 `idle`，不要谎报「无法连接」。 */
  sessions?: number
  summary?: { total: number; enabled: number; connected: number; packages?: number }
  /** 操作回执：false = 校验被拒或落盘失败（原因在 `errors`） */
  applied?: boolean
  /** 校验/落盘错误（人话，**页面内联展示，不走 toast**） */
  errors?: string[]
  /** 非阻断提醒（如两条目归一化后工具前缀撞车），内联展示为黄色提示 */
  warnings?: string[]
  msg?: string
  /** 本地包动作结果（`mcp_pkg_install` / `mcp_pkg_remove` / `mcp_pkg_verify`
   *  三条命令挂在同一个字段上）。`action` 区分是哪一次动作。 */
  pkg_action?: McpPkgActionResult
}

/** `mcp_server_test` 回执：一次性试连（**不落盘、不登记**）。
 *  会真实起子进程 / 建连接并等握手，最长 MCP_CONNECT_TIMEOUT（默认 15s）。 */
export interface McpTestResult {
  ok: boolean
  /** 失败原因（后端已格式化为一行）；ok=true 时为空串 */
  error?: string
  tools: string[]
  tool_count: number
  resource_count: number
  elapsed_ms: number
}

/** `mcp_server_upsert` 载荷。`original_name ≠ name` 表示重命名（改 JSON key）。 */
export interface McpUpsertPayload {
  name: string
  config: Record<string, unknown>
  original_name?: string
  /** 市场来源元数据（写旁路文件；手填/编辑时不带） */
  meta?: Record<string, unknown>
}

/** 市场条目的信任档。⚠️ 三档都**只经过命名空间所有权校验**（DNS / GitHub），
 *  **都没有代码审计** —— 这只是"谁发布"的分级，不是"是否安全"的评级。 */
export type McpPublisher = 'official' | 'community' | 'domain-verified'

/** 市场条目声明的必填环境变量（安装确认弹窗据此生成表单） */
export interface McpMarketEnvVar {
  name: string
  description: string
  required: boolean
  /** 建议用密码框输入（不影响落盘方式 —— 本期仍明文写 0600 的 mcp_servers.json） */
  secret: boolean
  default: string
}

/** 官方 MCP Registry 的一条搜索结果（后端 mcp_market 归一化后的形状）。
 *
 *  `packages` / `remotes` **原样带回**：`mcp_market_resolve` 靠它们做翻译，
 *  把整条发回去就省掉一次网络往返（官方 registry 单次搜索可达十几秒）。
 *  ⚠️ 别在前端解析这两个字段 —— 翻译规则只应有一处（后端 `mcp_market.resolve`）。
 */
export interface McpMarketItem {
  /** reverse-DNS 全名，如 `io.github.acme/my-server` */
  id: string
  name: string
  /** 最后一段，用作建议条目名 */
  short_name: string
  title: string
  description: string
  version: string
  repository: string
  /** 该条目可用的传输类型 */
  kinds: string[]
  /** 能否一键安装（false 时看 `reason`） */
  installable: boolean
  /** 不可一键安装的原因（人话，如"需要 Docker 运行时"） */
  reason: string
  publisher: McpPublisher
  published_at: string
  /** registry 侧状态（active 之外的值值得提醒用户） */
  status: string
  packages: Record<string, unknown>[]
  remotes: Record<string, unknown>[]
}

/** `mcp_market` 回执（应答 `mcp_market_search`）。**点对点信封**。
 *  `error` 非空 = 搜索失败（超时 / 断网 / HTTP 错），此时 `items` 为空 ——
 *  前端要展示错误文案，**不能**渲染成"没搜到结果"（两者含义完全不同）。 */
export interface McpMarketResult {
  items: McpMarketItem[]
  /** 下一页游标；空串 = 没有更多 */
  next_cursor: string
  query: string
  error: string
  cached: boolean
  elapsed_ms: number
}

/** `mcp_market_plan` 回执（应答 `mcp_market_resolve`）：把市场条目翻译成
 *  `mcpServers` 条目，**纯翻译、不落盘** —— 供安装确认弹窗展示。
 *
 *  核心用途：把 `config`（尤其是 `command` / `args` / `url`）**原样**摆给用户看。
 *  那是"即将在本机执行什么代码"的唯一凭据，必须在点确认之前看到。 */
export interface McpMarketPlan {
  ok: boolean
  /** 建议条目名（后端已避开与现有条目的撞名） */
  name: string
  config: Record<string, unknown>
  env_required: McpMarketEnvVar[]
  /** **只属于服务本身**的启动参数（不含 runner 与包名，如 `["-y", "pkg@1.0.0"]`
   *  里的 `-y` 与包名都已剥离）。
   *
   *  勾选「下载到本地」时 `command` 会换成包内 bin 的绝对路径，此时 runner（npx）
   *  与包名都不再适用，但这部分参数必须原样保留。别去切 `config.args` 自己算 ——
   *  解析规则只应有一处（后端 `mcp_market._config_from_package`）。 */
  package_args: string[]
  /** 该条目选中的包的坐标（远程端点条目为 null）。
   *  「下载到本地」靠它去问 `mcp_pkg_resolve` —— **别在前端解析 `item.packages`**，
   *  翻译规则只应有一处（后端 `mcp_market.resolve`）。 */
  pkg: {
    registry_type: string
    identifier: string
    version: string
    /** 只有 npm 支持本地安装（PyPI 侧要落 uv 工具链，口径未定） */
    local_installable: boolean
  } | null
  warnings: string[]
  /** ok=false 时的原因（如只有 oci 包、暂不支持） */
  unsupported: string
  error: string
}

/** ══ MCP 本地包安装（2026-10-07，docs/frontend/23 §本地安装）════════════════
 *
 *  与市场安装的分歧：市场只把条目翻译成 `npx -y pkg@ver`，包由 npm 在**首次连接时**
 *  隐式拉取（缓存落在 `~/.npm` 里，看得见摸不着）；本地安装是真的把包装进
 *  `~/.aigent/mcp/pkgs/<包@版本>/`，再把条目的 `command` 指向包内 bin 的绝对路径。 */

/** 已安装到本地的一个包（`mcp_config.packages[]`）。 */
export interface McpLocalPkg {
  /** 目录名，也是卸载 / 复核的寻址标识（如 `scope__pkg@1.2.3`） */
  slug: string
  name: string
  version: string
  spec: string
  dir: string
  command: string
  bin: string
  bins: string[]
  registry: string
  integrity: string
  dep_count?: number | null
  /** 本次安装是否允许执行了安装期脚本（默认 false；true 属高风险操作） */
  scripts_allowed: boolean
  installed_at: string
  size_bytes: number
  /** `ok` = 可执行文件在；`incomplete` = 上次安装没跑完（有哨兵）；
   *  `broken` = 元数据在但可执行文件没了 */
  status: 'ok' | 'incomplete' | 'broken'
  /** 正在引用这个包的条目名。卸载前必须提醒（删了条目就起不来了） */
  referenced_by: string[]
}

/** `mcp_pkg_plan` 回执（应答 `mcp_pkg_resolve`）：本地安装计划，**不落盘、不下载**。
 *  与 `mcp_market_plan` 同构 —— 确认区展示的必须是"将要执行什么"的原文。 */
export interface McpPkgPlan {
  ok: boolean
  /** 钉死后的规格 `包名@精确版本`（缺版本时会向 registry 问 latest 再钉死） */
  spec: string
  name: string
  version: string
  slug: string
  dir: string
  /** **实际使用的 registry**，必须在确认区明文展示（镜像能篡改元数据与哈希） */
  registry: string
  /** tarball 的 sha512（装后与 package-lock 对账；不符即安装失败） */
  integrity: string
  shasum: string
  tarball: string
  description: string
  /** 直接依赖数（来自 registry 元数据） */
  direct_dep_count: number
  /** 整棵依赖树规模（来自 `npm install --dry-run`）。null = 没取到，**显示"未知"，
   *  不要编一个数字** —— 传递依赖是供应链攻击的主要载体，报少了会给出虚假的安心感 */
  dep_count?: number | null
  /** 该包声明的全部 scripts */
  scripts: Record<string, string>
  /** 其中会在**安装期**执行的三个钩子（preinstall / install / postinstall） */
  install_hooks: Record<string, string>
  /** install_hooks 非空 → 确认区必须红字列出脚本名 */
  has_scripts: boolean
  bins: string[]
  default_bin: string
  /** true = 该条目没声明版本，本次由 registry 的 latest 钉死 */
  pinned_from_latest: boolean
  /** 本机已装过同样的版本 → 可直接复用（不必再下一次） */
  already_installed: {
    slug: string
    dir: string
    command: string
    installed_at: string
  } | null
  warnings: string[]
  error: string
}

/** `mcp_config.pkg_action`：本地包动作的结果（install / remove / verify 共用）。 */
export interface McpPkgActionResult {
  action: 'install' | 'remove' | 'verify'
  ok: boolean
  /** 失败原因（人话，**内联展示，不走 toast**） */
  error?: string
  slug?: string
  dir?: string
  /** install：可写进配置 `command` 的**绝对路径**（已解析符号链接、已做目录包含检查） */
  command?: string
  bins?: string[]
  bin?: string
  version?: string
  integrity?: string
  registry?: string
  dep_count?: number | null
  /** install：npm 的输出尾部（截断过），失败时是唯一线索 */
  log?: string
  /** install：本次是否直接复用了已装好的目录 */
  reused?: boolean
  scripts?: Record<string, string>
  scripts_allowed?: boolean
  /** install：跳过安装期脚本的提醒，必须显示给用户 */
  warnings?: string[]
  /** remove：释放的字节数 */
  freed_bytes?: number
  /** remove：人话结果 */
  msg?: string
  /** verify：发现的全部问题（空 = 通过） */
  errors?: string[]
  checked?: Record<string, unknown>
}

/** ══ 技能管理（设置弹窗「技能」页，2026-09-30，docs/frontend/24）══════════
 *
 *  ⚠️ 与 MCP 侧同源的两条原则：
 *  1. **UI 数据源 = 磁盘扫描结果（含被禁用项）**，而不是 `SkillLoader` 的注册表 ——
 *     后者只收启用的技能，拿它当列表会让"被禁用的技能永远看不见"。
 *  2. **启停只写旁路元数据**（`~/.aigent/skills_sources.json`），绝不改写 SKILL.md
 *     正文 —— 技能目录要保持"可直接拷给别的 Agent"的原样。 */

/** 单个技能（`skill_config` 回执里的条目）。 */
export interface SkillEntry {
  /** 磁盘目录名（唯一标识；也用于 `skill_read` 寻址） */
  name: string
  /** frontmatter 里的 name（缺省时等于 name） */
  title: string
  description: string
  tags: string[]
  enabled: boolean
  /** SKILL.md 是否存在。false = 目录在但清单缺失 → 该技能不会被加载 */
  has_manifest: boolean
  path: string
  manifest: string
  /** 全部文件相对路径（**含 SKILL.md**）。展示"附属文件"时自行过滤掉它 */
  files: string[]
  file_count: number
  size_bytes: number
  /** `market` = 从市场安装（带 market_id），`local` = 手写 / 用户自建 */
  source: 'local' | 'market'
  market_id?: string | null
  market_name?: string | null
  publisher?: string | null
  market_url?: string | null
  installed_at?: string | null
  /** 非阻断提醒（缺 description / 缺 SKILL.md 等），内联展示 */
  warnings: string[]
}

/** 技能市场源（`skill_config` 回执里的 `markets`）。 */
export interface SkillMarketEntry {
  id: string
  name: string
  /** `git` = 一个仓库；`api` = 第三方公开 API；`index` = 自建 JSON 索引 */
  type: 'git' | 'api' | 'index'
  type_label?: string
  repo?: string
  ref?: string
  provider?: string
  base_url?: string
  url?: string
  /** 信任档：official / community / third-party（**不是安全评级**） */
  publisher?: string
  homepage?: string
  note?: string
  enabled: boolean
  /** 内置源：只能启停，不能删除，身份字段也不可改 */
  builtin?: boolean
  is_default?: boolean
}

/** `skill_config` 回执 —— `skill_config_get` / `skill_set_enabled` / `skill_remove` /
 *  `skill_install` / `skill_market_upsert` / `skill_market_remove` **六条命令共用**，
 *  每次都回读全量 → 前端整份替换（拼接会留下已删技能的残影）。
 *  **点对点信封**：只回发起窗口、不广播 → 不进 isKnownAgentEvent 白名单。 */
export interface SkillConfigResult {
  /** `~/.aigent/skills` 绝对路径（页面展示用） */
  dir?: string
  sources_path?: string
  markets_path?: string
  exists?: boolean
  skills: SkillEntry[]
  markets: SkillMarketEntry[]
  default_market?: string
  /** 已启用插件贡献的技能数 —— 它们**不归本页管理**，但用户需要知道"技能为什么变多了" */
  plugin_skill_count?: number
  summary?: { total: number; enabled: number; from_market: number; invalid: number }
  /** 操作回执：false = 校验被拒或落盘失败（原因在 `errors`） */
  applied?: boolean
  /** 校验 / 落盘错误（人话，**页面内联展示，不走 toast**） */
  errors?: string[]
  warnings?: string[]
  msg?: string
}

/** `skill_read` 回执：单个技能的 SKILL.md 全文（**只读、不落盘**）。 */
export interface SkillContentResult {
  name: string
  text: string
  path?: string
  dir?: string
  description?: string
  files?: string[]
  error: string
}

/** 技能市场的一条搜索结果（后端 `skill_market` 归一化后的形状）。 */
export interface SkillMarketItem {
  /** `<源 id>:<仓库内路径>`，全局唯一 */
  id: string
  market_id: string
  market_name: string
  source_kind: 'git' | 'api' | 'index'
  name: string
  dir_name: string
  /** 仓库内目录（`skills/pdf`）；api 源里是条目 slug */
  path: string
  description: string
  version: string
  tags: string[]
  publisher: string
  installable: boolean
  /** 不可安装的原因（人话） */
  reason: string
  repo: string
  ref: string
  url: string
}

/** `skill_market` 回执（应答 `skill_market_search`）。**点对点信封**。
 *  `error` 非空 = 搜索失败，此时 `items` 必为空 —— 「搜失败」与「搜不到」必须分开渲染。 */
export interface SkillMarketResult {
  items: SkillMarketItem[]
  next_cursor: string
  market_id: string
  market_name?: string
  query: string
  /** 该结果集的**总条数**（分页前），用于显示"共 N 条" */
  total: number
  error: string
  cached?: boolean
  elapsed_ms: number
}

/** `skill_market_plan` 回执（应答 `skill_market_resolve`）：**纯抓取、不落盘**。
 *
 *  `skill_md` 是**整份 SKILL.md 原文**，这是安装确认页的安全闸门 —— 技能正文是
 *  "模型接下来会照着做什么"的唯一凭据，必须原样展示（不折叠、不摘要）。 */
export interface SkillMarketPlan {
  ok: boolean
  /** 建议落盘目录名（后端已避开撞名） */
  name: string
  /** frontmatter 里声明的技能名（可能与 `name` 不同） */
  skill_name: string
  description: string
  /** SKILL.md 全文 */
  skill_md: string
  files: { path: string; size: number }[]
  file_count: number
  total_bytes: number
  tags: string[]
  version: string
  warnings: string[]
  /** ok=false 时的原因（人话） */
  unsupported: string
  error: string
  /** 安装时由后端直接复用（前端只需原样回传 item，不必解析它） */
  meta: Record<string, unknown>
}

/** ══ 插件管理（设置弹窗「插件」页，2026-09-30，docs/frontend/25）══════════
 *
 *  插件采用 **Claude Code 插件规范**：一个目录 + `.claude-plugin/plugin.json`
 *  清单，可贡献 skills / commands / agents / hooks / MCP 服务器 / 语言服务器。
 *
 *  ⚠️ **本期运行时只接「技能」这一路贡献**（`wired` 字段就是它的出处）。
 *  commands / hooks / MCP 只做清单展示 —— 界面上必须如实标注"本期未接入"，
 *  不能让人以为装上就等于那些代码开始跑了。 */

/** 单个插件（`plugin_config` 回执里的条目）。 */
export interface PluginEntry {
  name: string
  display_name: string
  description: string
  version: string
  author: string
  homepage: string
  /** `.claude-plugin/plugin.json` 是否存在。false = 不能算有效插件 */
  has_manifest: boolean
  enabled: boolean
  path: string
  manifest_path: string
  /** 六类组件清单：skills / commands / agents / hooks / mcp_servers / lsp_servers */
  components: Record<string, string[]>
  component_counts: Record<string, number>
  /** 本期**真正接入运行时**的组件类型（目前只有 `skills`） */
  wired: string[]
  source: 'local' | 'market'
  market_id?: string | null
  market_name?: string | null
  market_url?: string | null
  publisher?: string | null
  repo?: string | null
  installed_at?: string | null
  warnings: string[]
}

/** 插件市场源。 */
export interface PluginMarketEntry {
  id: string
  name: string
  repo: string
  ref?: string
  publisher?: string
  homepage?: string
  note?: string
  enabled: boolean
  builtin?: boolean
  is_default?: boolean
}

/** `plugin_config` 回执（六条命令共用，整份替换）。**点对点信封**。 */
export interface PluginConfigResult {
  dir?: string
  sources_path?: string
  markets_path?: string
  exists?: boolean
  plugins: PluginEntry[]
  markets: PluginMarketEntry[]
  default_market?: string
  /** 后端声明的"本期接入运行时的组件类型"（单一出处，前端别硬编码） */
  wired_components?: string[]
  /** 当前实际进入技能表的插件技能数（启用插件 × 有 SKILL.md 的技能） */
  contributed_skill_count?: number
  summary?: { total: number; enabled: number; invalid: number; contributing: number }
  applied?: boolean
  errors?: string[]
  warnings?: string[]
  msg?: string
}

/** `plugin_read` 回执：plugin.json 原文 + 文件清单。 */
export interface PluginContentResult {
  name: string
  plugin_json: string
  path?: string
  manifest_path?: string
  components?: Record<string, string[]>
  files: { path: string; size: number }[]
  error: string
}

/** 插件市场的一条搜索结果。 */
export interface PluginMarketItem {
  id: string
  market_id: string
  market_name: string
  name: string
  display_name: string
  description: string
  version: string
  author: string
  category: string
  tags: string[]
  homepage: string
  publisher: string
  /** source 的形态：relpath / url / git-subdir / github / invalid */
  source_kind: string
  source_label: string
  repo: string
  ref: string
  path: string
  installable: boolean
  reason: string
  /** 清单里**声明**的技能名（实际以安装确认页的组件清单为准） */
  declared_skills: string[]
  /** 原始市场条目 —— **前端不要解析它**，翻译规则只在后端 `plugin_market` */
  raw?: Record<string, unknown>
}

/** `plugin_market` 回执（应答 `plugin_market_search`）。**点对点信封**。 */
export interface PluginMarketResult {
  items: PluginMarketItem[]
  next_cursor: string
  market_id: string
  market_name?: string
  /** `marketplace.json` 里声明的市场名 / 维护者（展示"这是谁的市场"） */
  catalog_name?: string
  catalog_owner?: string
  query: string
  total: number
  error: string
  elapsed_ms: number
}

/** `plugin_market_plan` 回执（应答 `plugin_market_resolve`）：**纯抓取、不落盘**。
 *
 *  安装确认页靠 `components` 列出"这个插件将贡献什么"，靠 `wired` 说明
 *  "其中哪些本期真正生效"，靠 `plugin_json` 给出"它自称是什么"的原文凭据。 */
export interface PluginMarketPlan {
  ok: boolean
  name: string
  plugin_name: string
  display_name: string
  description: string
  version: string
  author: string
  homepage: string
  /** `.claude-plugin/plugin.json` 原文 */
  plugin_json: string
  components: Record<string, string[]>
  component_counts: Record<string, number>
  wired: string[]
  /** 贡献技能的名字 + 首行描述（让用户在确认页看出"这些技能会教模型做什么"） */
  skill_previews: { name: string; description: string }[]
  files: { path: string; size: number }[]
  file_count: number
  total_bytes: number
  source_kind: string
  repo: string
  warnings: string[]
  unsupported: string
  error: string
  meta: Record<string, unknown>
}

/** `skill_install` 载荷。**只回传「哪一条 + 叫什么名」** —— 后端会自己重新解析与
 *  抓取（结果在后端有内存缓存，几乎不额外花网络），既不依赖前端带回来的 meta，
 *  也不可能出现"前端传了别的文件却装成别的东西"。 */
export interface SkillInstallPayload {
  name: string
  item: SkillMarketItem
}

/** `plugin_install` 载荷（同上的最小协议面）。 */
export interface PluginInstallPayload {
  name: string
  item: PluginMarketItem
}

/** `skill_create` 载荷：手动新建一个技能（后端拼成标准 SKILL.md 落盘）。 */
export interface SkillCreatePayload {
  name: string
  description: string
  body: string
  tags?: string[]
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
  /** ── 任务执行模式（2026-09-25，docs/frontend/22）─────────────────────
   *  ⚠ **断线重连 / 整页重载后恢复胶囊 tag 的唯一通道**：连接重放序列不含
   *  `session_history`（它只在收到 `session_switch` 后才发），前端重连只做
   *  `resetTransient()` + `listSessions()` —— 与 `permission_mode`/`unread`
   *  同通道即天然覆盖。老会话/老后端缺省时按 `'normal'` 处理（零占位）。 */
  execution_mode?: ExecutionMode
  /** 计划文书状态：非空才会渲染计划卡片外壳（正文经 `plan_read` 拉取）。 */
  plan_status?: PlanStatus | null
  /** 计划文书落点（2026-09-29 改口径）：**相对工作空间**的
   *  `.aiagent/plan/<name>.md`（由后端按 meta 的 `plan_name` 拼出）。
   *  仅 `plan_status` 非空时有值；存量会话为旧口径的绝对路径。 */
  plan_path?: string | null
  /** 目标条件（goal 模式胶囊 tag 的文案）。仅 `execution_mode === 'goal'` 时非空。 */
  goal_condition?: string | null
  /** 目标已完成的评估轮次（= 后端 `GoalController.active.iterations`）。
   *  常驻目标条显示 `goal_round + 1`。老会话/老后端缺省 → 当作 0。 */
  goal_round?: number | null
  /** 目标设置时刻（Unix 秒）。常驻目标条据此本地 tick 出"已运行多久"；
   *  缺省则不显示时长（零占位，不编一个假起点）。 */
  goal_started_at?: number | null
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
  /** ── MCP 服务管理（2026-09-30，docs/frontend/23）─────────────────────
   *  `mcp_config_get`：读原始条目（**含 enable:0**）+ 各 runtime 实时连接状态；
   *  `mcp_server_upsert`：单条新增/编辑/重命名/启停（**不是整份覆盖** —— MCP 是
   *    多条目集合，整份覆盖在多窗口下误伤面太大）；
   *  `mcp_server_remove`：删单条；
   *  `mcp_server_test`：一次性试连，**不落盘、不登记**（先验证再保存）。
   *  四条都回点对点信封（mcp_config / mcp_test），只回发起窗口、不广播。
   *  ⚠ `mcp_server_test` 会真实起子进程并等握手（最长 15s）→ 前端按钮进行中必须
   *    disabled：主进程 pending 表按 kind FIFO 配对且无 id，同 kind 并发会串台。 */
  | 'mcp_config_get'
  | 'mcp_server_upsert'
  | 'mcp_server_remove'
  | 'mcp_server_test'
  /** 市场：搜索官方 MCP Registry → 回执 `mcp_market`。
   *  ⚠️ 实测单次耗时 0.9s ~ 17s（波动极大）—— 主进程 IPC 超时必须放宽到 30s，
   *  用默认 5s 会稳定误报超时。 */
  | 'mcp_market_search'
  /** 市场：把条目翻译成配置（**不落盘**）→ 回执 `mcp_market_plan`。
   *  安装确认弹窗靠它拿到"将写入的 command/args 原文"。 */
  | 'mcp_market_resolve'
  /** ── 技能管理（2026-09-30，docs/frontend/24）─────────────────────────
   *  与 MCP 侧完全同构：全部**点对点**（只回发起窗口、不广播）→ 不进
   *  isKnownAgentEvent 白名单。
   *  `skill_config_get`：磁盘扫描（**含被禁用项**）+ 源列表；
   *  `skill_set_enabled`：启停，**只写旁路元数据**，绝不改写 SKILL.md；
   *  `skill_remove`：删技能目录（+ 元数据）；
   *  `skill_install`：从市场安装 —— **后端重新解析 + 重新抓取**，前端只回传
   *    「哪一条 + 叫什么名」（见 SkillInstallPayload 的注释）；
   *  `skill_read`：读单个技能的 SKILL.md 全文；
   *  `skill_market_upsert` / `skill_market_remove`：增删技能源（内置源只能停用）；
   *  `skill_market_search`：在指定源里搜索（git 仓库 / 第三方 API / 自建索引）；
   *  `skill_market_resolve`：抓取安装计划（**不落盘**），供确认页展示 SKILL.md 全文。
   *  ⚠ `skill_install` / `skill_market_resolve` / `skill_market_search` 都要打网络
   *    （scan 一个仓库 + 抓若干 SKILL.md），最长可达十几秒 → 前端按钮进行中必须
   *    disabled：主进程 pending 表按 kind FIFO 配对且无 id，同 kind 并发会串台。 */
  | 'skill_config_get'
  | 'skill_set_enabled'
  | 'skill_remove'
  | 'skill_install'
  /** 手动新建技能（`{name, description, tags?, body}` → 后端拼成标准 SKILL.md 落盘）。
   *  这是"自己写技能"的最短路径 —— 不必先建仓库再走市场。 */
  | 'skill_create'
  | 'skill_read'
  | 'skill_market_upsert'
  | 'skill_market_remove'
  | 'skill_market_search'
  | 'skill_market_resolve'
  /** ── 插件管理（2026-09-30，docs/frontend/25）─────────────────────────
   *  命令集与技能侧一一对应（`plugin_*`）。插件额外多一个 `plugin_read`
   *  （读 plugin.json 原文 + 文件清单）—— 技能的正文已经由 market plan 带回，
   *  插件的清单则可能远超一次载荷，单独取更划算。
   *  `plugin_market_search` 读的是市场仓库里的 `marketplace.json`（raw CDN 直读，
   *  不受 GitHub API 限额），实测 314 条目录 1~3s。 */
  | 'plugin_config_get'
  | 'plugin_set_enabled'
  | 'plugin_remove'
  | 'plugin_install'
  | 'plugin_read'
  | 'plugin_market_upsert'
  | 'plugin_market_remove'
  | 'plugin_market_search'
  | 'plugin_market_resolve'
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
  /** ── 任务执行模式（2026-09-25，docs/frontend/22）─────────────────────
   *  与权限档位**正交**：权限回答"能不能做/要不要审批"，执行模式回答"以什么方式做"。
   *  - `session_exec_mode`：切换 normal/plan/goal。fire-and-forget，回执走
   *    `execution_mode_changed` **广播**（多窗口一致）；失败走既有 `error` 信封。
   *  - `plan_approve`：批准计划文书 → `plan_status=approved` + mode 回落 normal +
   *    自动续跑一轮。fire-and-forget（该命令不回内容，等流式事件）。
   *  - `plan_read`：读计划文书正文（**不是 file_read**，文书在元数据目录里）→
   *    点对点回执 `plan_content`。 */
  | 'session_exec_mode'
  | 'plan_approve'
  | 'plan_read'
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
  /** 随本条消息一起落地的**执行模式**（2026-09-27 起）。两种含义：
   *
   *  ① **新建任务**（`session_id` 缺失）：**预选草稿**。后端在**建会话时**把它写进
   *     该会话 meta，Agent 构造时 `_restore_execution_state` 读回 → 对**首轮即生效**。
   *
   *  ② **已有会话 + `'goal'`**（2026-09-30，docs/frontend/22 §2.6）：目标模式的
   *     **武装位** —— 点「目标模式」不再弹框问条件，条件取**本条消息的正文**，
   *     后端在**派发 turn 之前**落地（那一刻才既有条件、又不撞 `rt.busy`）。
   *
   *  两条路径都**不能**改成"等 `session` 信封回来再发 `session_exec_mode`"：
   *  ①会撞 `rt.busy`（评审 P1-5 的 busy 守卫），②则根本发不出去（条件在那时还不存在，
   *  `GoalController` 拒空条件）。plan 仍只走 ①：plan 的真源是后端 gate，必须即时生效。
   *  `'normal'` / 缺省 = 不设置（零字段）。 */
  exec_mode?: ExecutionMode
  /** 目标条件（仅 `exec_mode === 'goal'` 时读取）。**留空即用本条消息的正文**
   *  —— 这正是"首条指令即目标"（正文也为空时用 `[附件] 文件名` / `[引用] 文件名`
   *  兜底）。超 4000 字 / 兜底后仍为空 → 后端忽略整个预选（草稿态**绝不阻断发送**，
   *  胶囊随后的 `sessions` 广播自行纠正）。 */
  exec_condition?: string
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

/** 前端 → 后端：切换会话执行模式（kind='session_exec_mode'，fire-and-forget）。
 *  成功后后端广播 `execution_mode_changed`（前端 tag 只认广播）。`condition` 仅
 *  `mode === 'goal'` 时有意义（空/超 4000 字由后端 `GoalError` 原样回 error 信封）。
 *
 *  ⚠️ 2026-09-30 起 goal 的**常规入口不再走这里**：桌面端点「目标模式」是"武装"
 *  （条件 = 下一条指令的正文），随 `chat.exec_mode/exec_condition` 落地，因为空条件
 *  在本命令里必被拒、且本命令是"即时生效"语义（`rt.busy` 时更是直接拒绝）。
 *  本命令保留给"后端已是 goal 时的关闭 / 跨模式切 plan / 其它客户端"。 */
export interface SessionExecModePayload {
  session_id: string
  mode: ExecutionMode
  condition?: string
}

/** 前端 → 后端：批准计划文书（kind='plan_approve'，fire-and-forget）。
 *  后端在空闲时自动追加"[计划已批准]"续跑指令并起一轮；忙碌/有待答提问时只落状态。 */
export interface PlanApprovePayload {
  session_id: string
}

/** 前端 → 后端：读计划文书正文（kind='plan_read'）→ 点对点回执 `plan_content`。 */
export interface PlanReadPayload {
  session_id: string
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