#!/usr/bin/env python3
"""
agent_full_v2.py - 主智能体引擎（Agent 类）

从函数式 REPL 重构为类形式：所有依赖与会话状态收敛为实例属性，
不再使用模块级可变单例（tools.py 的全局 TOOL_REGISTRY 已移除）。

- 每个 Agent 实例拥有独立的 ToolRegistry / todo holder / background holder /
  hook_system / subagent_runner / session 状态，支持多实例隔离。
- 交互入口：`python agents/agent_cli.py`（实例化 Agent 驱动 REPL）。

为 s14 定时任务（每任务独立会话）与未来 TUI 多会话预留的接缝：
  agent = Agent()
  agent.init_session(resume=False)   # 新会话
  agent.run_turn("[Scheduled] ...") # 非交互单轮
"""

import json
import os

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from session_manage import SessionManager
from subagent import SubAgent
from background_manager import BackgroundManager
from teammate_manager import TeammateManager
from paths import WORKDIR, CHAT_HISTORY_DIR, SKILLS_DIR, TEAM_DIR, WORKTREE_DIR, MCP_CONFIG, WORKFLOW_DIR
from tools import ToolRegistry
from worktree import WorktreeManager
from mcp_manager import MCPManager
from workflow import WorkflowManager, register_default_workflows
from goal import (
    GoalController,
    PromptGoalEvaluator,
    DEFAULT_STOP_HOOK_BLOCK_CAP,
)
from skills import SkillLoader
from llm_manage import LLMClient
from system_prompt import SystemPromptBuilder
from error_recovery import ErrorRecovery, RecoveryAction
from hooks import HookSystem
from utils import truncate_chars


# 加载环境变量
load_dotenv(override=True)


class Agent:
    """
    主智能体引擎：持有全部依赖与会话状态，支持多实例隔离。

    每个实例拥有独立的：
    - tools（ToolRegistry：基础工具方法 / definitions / handlers / execute）
    - skills / memory / hook_system / background_manager / subagent_runner / recovery
    - session 状态（session_num / session_file / history_messages / todo holder）

    交互入口 agent_cli.py 实例化本类并驱动 REPL；
    未来 cron（每任务独立会话）与 TUI（每会话一实例）直接复用。
    """

    MAX_AGENT_ITERATIONS = 100

    def __init__(
        self,
        *,
        skills: SkillLoader | None = None,
        memory=None,
        tools: ToolRegistry | None = None,
        session_prefix: str = "session_",
        cron_scheduler=None,
        silent: bool = False,
    ):
        # ── 模型参数（从 .env 读取） ──
        self.model = os.environ.get("OPENAI_MODEL_ID", "")
        self.fallback_model = os.environ.get("FALLBACK_MODEL_ID", "")

        # ── 会话文件名前缀：默认 "session_"；cron 调度器传入 "cron_" ──
        self.session_prefix = session_prefix

        # ── silent 模式：抑制所有打印输出（cron 定时任务用） ──
        self.silent = silent

        # ── 依赖（默认惰性构造；允许外部注入，多实例可共享/自定义） ──
        self.skills = skills if skills is not None else SkillLoader(SKILLS_DIR)
        self.tools = tools if tools is not None else ToolRegistry(
            skills=self.skills, cron_scheduler=cron_scheduler,
        )
        self.memory = memory if memory is not None else self.tools.memory

        # 钩子实例：每实例独立，主循环与子智能体共用
        self.hook_system = HookSystem(silent=self.silent)
        self.hook_system.register_default_hooks()

        # 后台任务管理器：挂到本实例 tools 的 holder 上（实例级，非全局）
        self.background_manager = BackgroundManager()
        self.tools.set_background_manager(self.background_manager)

        # 团队成员管理器（s17）：挂到本实例 tools 的 holder 上（注入本实例 tools，实例级）
        self.teammate_manager = TeammateManager(TEAM_DIR, tools=self.tools)
        self.tools.set_teammate_manager(self.teammate_manager)

        # worktree 管理器（s18）：挂到本实例 tools 的 holder 上（实例级）
        self.worktree_manager = WorktreeManager(WORKTREE_DIR)
        self.tools.set_worktree_manager(self.worktree_manager)

        # MCP 管理器（s19）：挂到本实例 tools 的 holder 上（实例级）
        # 真实 MCP：按 WorkSpace/HomeDir/mcp/mcp_servers.json 配置加载，每轮热加载。
        self.mcp_manager = MCPManager(config_file=MCP_CONFIG)
        self.tools.set_mcp_manager(self.mcp_manager)
        # 破坏性 MCP 工具门控：注入查询函数，permission_hook 据此二次确认
        self.hook_system.set_mcp_destructive_lookup(self.mcp_manager.is_destructive)
        # 启动即自动加载已配置的 MCP 服务器，使其工具首轮即可用（无需模型手动 connect）
        self.mcp_manager.connect_all()

        # 团队模式标志（粘性）：默认 False（子智能体分发模式）；
        # 由 CLI 的 /teams 置 True、/subagent 置 False，决定 agent_loop 喂哪套工具集。
        self.team_mode = False

        # 子智能体：复用本实例的工具集/处理器/hooks；注入 tool_registry 供 workdir 场景
        self.subagent_runner = SubAgent(
            self.tools.base_tools, self.tools.handlers, self.hook_system,
            tool_registry=self.tools,
        )

        # 系统 prompt：注入本实例的 skills / memory / tools
        self.system_prompt = SystemPromptBuilder(
            workdir=WORKDIR,
            skills=self.skills,
            memory=self.memory,
            tools=self.tools,
            chat_history_dir=CHAT_HISTORY_DIR,
        )

        # LLM 客户端 + S11 错误恢复控制器
        self.llm_client = LLMClient().llm
        self.recovery = ErrorRecovery(
            primary_model=self.model, fallback_model=self.fallback_model
        )

        # 工作流运行时（s16）：复用本实例的 LLM 客户端与模型跑工作流子智能体，
        # 挂到本实例 tools 的 holder 上（实例级），并注册内置示例工作流
        self.workflow_manager = WorkflowManager(WORKFLOW_DIR, self.llm_client, self.model)
        register_default_workflows(self.workflow_manager)
        self.tools.set_workflow_manager(self.workflow_manager)

        # 目标循环（s17 Goal Loop）：goal 功能在主类的接入点（详见 goal.py 模块注释）
        #   - PromptGoalEvaluator：独立评估器（无工具的 LLM 调用），读完整对话判定
        #     目标是否达成；复用宿主 self.llm_client，不新建连接
        #   - evaluator_model：评估器可单独配模（.env 的 GOAL_EVALUATOR_MODEL_ID，
        #     留空则与 Worker 同模型）；goal_block_cap：连续判"未完成"的次数上限
        #   - GoalController 只负责状态与裁决，"拦截动作"发生在 agent_loop 的
        #     停止边界（无 tool_call 分支），见下方 agent_loop 内的注释
        evaluator_model = os.environ.get("GOAL_EVALUATOR_MODEL_ID") or self.model
        goal_block_cap = int(
            os.environ.get("GOAL_STOP_HOOK_BLOCK_CAP") or DEFAULT_STOP_HOOK_BLOCK_CAP
        )
        self.goal_controller = GoalController(
            PromptGoalEvaluator(self.llm_client, evaluator_model),
            block_cap=goal_block_cap,
        )
        # 累计 token 消耗：agent_loop 每次响应后累加（goal 状态展示"目标期间花费"用）
        self.total_tokens = 0

        # ── 会话状态（由 init_session / new_session / switch_session 填充） ──
        self.session_manager: SessionManager | None = None
        self.session_num: int | None = None
        self.session_file: Path | None = None
        self.history_messages: list = []

    # ── silent 打印辅助 ──────────────────────────────────────────
    def _print(self, *args, **kwargs):
        """silent 模式下抑制所有 print 输出（cron 定时任务用）。"""
        if not self.silent:
            print(*args, **kwargs)

    # ═══════════════════════════════════════════════════════════
    #  会话生命周期（CLI / cron / TUI 共用接缝）
    # ═══════════════════════════════════════════════════════════

    def init_session(self, resume: bool = True) -> int:
        """
        创建/恢复会话：构建 SessionManager → 初始化 → 绑定 todo → 注入 reminder。

        resume=True：加载最近一次会话；resume=False：新建独立会话（cron 用）。
        """
        if self.session_manager is None:
            self.session_manager = SessionManager(
                CHAT_HISTORY_DIR, self.system_prompt.build_system_prompt(),
                session_prefix=self.session_prefix,
            )
        if resume:
            self.session_num, self.session_file, self.history_messages = \
                self.session_manager.init_session()
        else:
            self.session_num, self.session_file, self.history_messages = \
                self.session_manager.create_initialized_session()
        # todo 与 session 绑定：每次切会话都要重新指向对应的 todo 文件
        self.tools.set_todo_manager(self.session_num)
        # task 与 session 绑定：任务板限定在本会话作用域（"session_N"/"cron_N"）
        self.tools.task_manager.set_scope(f"{self.session_prefix}{self.session_num}")
        self._inject_todo_reminder()
        return self.session_num

    def run_turn(self, user_query: str) -> str:
        """
        跑一轮非交互对话（CLI / cron / TUI 共用）。返回最终回复文本。

        agent_loop 内部仍会打印 thinking / 本轮回复（保持现状 UX）；
        本方法额外返回历史最后一条消息的文本，供调用方打印。
        """
        # 目标循环（s17）：每轮查询（用户输入 / cron 触发）都是全新过程，
        # 重置连续 block 计数，避免上一轮的 block 累计误判触发 limit
        self.goal_controller.begin_query()
        self.hook_system.trigger("UserPromptSubmit", user_query)
        self.history_messages.append({"role": "user", "content": user_query})
        self.session_manager.append_message_to_session(
            self.session_file, self.history_messages[-1]
        )
        self.session_manager.maybe_compact_context(
            self.history_messages, self.session_file
        )
        self.agent_loop()
        last = self.history_messages[-1].get("content", "")
        if isinstance(last, list):
            return "".join(b.get("text", "") for b in last if isinstance(b, dict))
        return str(last)

    def new_session(self) -> tuple[int, str]:
        """创建新会话并绑定 todo，返回 (新会话编号, 提示语)。"""
        self.session_num, self.session_file, self.history_messages = \
            self.session_manager.create_initialized_session()
        # 新会话的 todo 文件尚不存在，set_todo_manager 会建出空列表；reminder 不会注入
        self.tools.set_todo_manager(self.session_num)
        self.tools.task_manager.set_scope(f"{self.session_prefix}{self.session_num}")
        return self.session_num, f"已创建新会话: session_{self.session_num}.jsonl"

    def switch_session(self, target_num: int) -> tuple[int, int]:
        """
        切换到指定会话，绑定对应 todo 并注入 reminder。
        返回 (会话编号, 消息数)；会话不存在时抛 FileNotFoundError。
        """
        self.session_num, self.session_file, self.history_messages = \
            self.session_manager.switch_session(target_num)
        self.tools.set_todo_manager(self.session_num)
        self.tools.task_manager.set_scope(f"{self.session_prefix}{self.session_num}")
        self._inject_todo_reminder()
        return self.session_num, len(self.history_messages)

    def clear_session(self) -> int:
        """清空当前会话（todo 同步重置），返回被删除的消息数。"""
        deleted_count = self.session_manager.clear_session(self.session_file)
        # todo 与 chat history 同生共死：清空 chat 的同时把当前 session 的 todo 也重置为空
        self.tools.get_todo_manager().update([], fresh_start=False)
        self.history_messages = self.session_manager.load_session_history(
            self.session_file
        )
        return deleted_count

    def show_tasks(self) -> str:
        """返回当前会话待办看板文本。"""
        return self.tools.get_todo_manager().render()

    # ═══════════════════════════════════════════════════════════
    #  目标循环（s17 goal loop，CLI 斜杠命令共用接缝）
    # ═══════════════════════════════════════════════════════════

    def set_goal(self, condition: str) -> str:
        """设置会话级目标（对应 CLI 的 /goal <condition>），返回确认文本。

        记录此刻的累计 token（tokens_at_start），供 /goal 状态统计
        "目标期间花费"；若已有激活目标会被新目标覆盖（事件里记
        "replaced by a new goal"）。condition 非法（空/超长）抛 GoalError。
        """
        state = self.goal_controller.set_goal(condition, self.total_tokens)
        return f"Goal set: {state.condition}"

    def clear_goal(self) -> str:
        """清除当前目标（对应 CLI 的 /goal clear，含同义别名）。

        清除后 agent_loop 的停止边界恢复"直接放行"；无目标时原样返回
        "No goal set"。
        """
        return self.goal_controller.clear()

    def goal_status(self) -> str:
        """返回当前目标状态文本（对应 CLI 的 /goal 无参数）。

        有激活目标：目标条件 / 存活时长 / 评估次数 / 目标期间花费 / 最近判定；
        无激活目标：回显上次"达成/失败"结论，或 "No goal set"。
        """
        return self.goal_controller.status(self.total_tokens)

    def compact(self) -> None:
        """手动触发上下文压缩（/compact）。"""
        self.session_manager.maybe_compact_context(
            self.history_messages, self.session_file, manual=True
        )

    def show_skills(self) -> str:
        """返回可用技能列表文本。"""
        return self.skills.list_skills()

    def context_label(self) -> str:
        """格式化当前上下文窗口显示信息（用于 REPL 提示符）。"""
        return self.session_manager.format_context_label(self.history_messages)

    # ═══════════════════════════════════════════════════════════
    #  工具执行辅助
    # ═══════════════════════════════════════════════════════════

    def _inject_todo_reminder(self) -> None:
        """
        会话恢复/切换时，若当前 session 有未完成的 todo，注入一条 reminder
        让模型意识到"上次有活没干完"。

        reminder 写在 user query 之前、system / 旧 history 之后，
        模型下一轮必能直接看到。reminder 同时落盘 session_file，
        保证下次启动 reload 仍可见。
        """
        mgr = self.tools.get_todo_manager()
        if not mgr.has_open_items():
            return
        reminder = (
            "<system-reminder>本次会话检测到上次有未完成的待办事项：\n"
            f"{mgr.render()}\n"
            "请在继续之前确认是否继续执行；如果任务已不再相关，请用 todo 工具把对应项标记为 completed，"
            "或开启新计划（fresh_start=true 整体替换）。</system-reminder>"
        )
        self.history_messages.append({"role": "user", "content": reminder})
        self.session_manager.append_message_to_session(
            self.session_file, self.history_messages[-1]
        )

    def _make_executor(self, tool_name: str, tool_args: dict):
        """
        把"执行一个工具调用"包成无参闭包，供 background_manager 在后台线程调用。

        tool_name / tool_args 是 _make_executor 的形参（独立作用域、每次调用绑一次），
        所以 lambda 直接闭包捕获即可，无须 def 嵌套，也不会出现 for 循环闭包共享
        变量导致所有闭包都引用最后一次迭代值的经典坑。
        """
        if tool_name == "sub_agent":
            return lambda: self._run_subagent(tool_args)
        elif self.tools.resolve_handler(tool_name) is not None:
            return lambda: self.tools.execute(tool_name, **tool_args)
        else:
            return lambda: f"Error: Unknown tool {tool_name}"

    def _run_subagent(self, tool_args: dict) -> str:
        """派发 sub_agent。若传了 workdir（worktree 名称），解析为路径并注入。"""
        prompt = tool_args.get("prompt", "")
        workdir_name = tool_args.get("workdir")
        if workdir_name:
            wt = self.worktree_manager.resolve(workdir_name)
            if not wt.exists():
                return (f"Worktree '{workdir_name}' not found. "
                        "Create it first via create_worktree.")
            return self.subagent_runner.spawn_subagent(
                prompt,
                allowed_tools=tool_args.get("allowed_tools"),
                workdir=wt,
            )
        return self.subagent_runner.spawn_subagent(
            prompt,
            allowed_tools=tool_args.get("allowed_tools"),
        )

    def _execute_tool_call(self, tool_call) -> dict:
        """
        执行单个工具调用（sub_agent 或普通工具），同步或后台均可。

        同步路径：直接调用 executor()，返回真实输出。
        后台路径：分发给 background_manager 守护线程，立即返回占位
        "[Background task bg_xxxx started] Command: ..." 字符串作为
        本轮的 tool_result。占位 result 必须立即写进 history，
        否则下一轮 LLM 会因 tool_call_id 缺失而报错。
        """
        tool_name = tool_call.function.name
        # OpenAI SDK 返回的 function.arguments 是 JSON 字符串,需解析为 dict 才能 ** 解包
        raw_args = tool_call.function.arguments
        tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        tool_id = tool_call.id

        # 判定是否走后台：模型显式 run_in_background=True 优先，否则启发式
        if self.background_manager.should_run_background(tool_name, tool_args):
            executor = self._make_executor(tool_name, tool_args)
            bg_id = self.background_manager.start_background_task(
                tool_name, tool_args, tool_id, executor
            )
            cmd_text = (
                tool_args.get("command")
                or (tool_args.get("prompt", "")[:80] if tool_args.get("prompt") else "")
                or tool_name
            )
            tool_output = (
                f"[Background task {bg_id} started] "
                f"Command: {cmd_text}. "
                f"Result will be available when complete."
            )
            self._print(f">> {tool_name} 后台分发: {bg_id}")
        else:
            # 同步路径：直接走原逻辑
            executor = self._make_executor(tool_name, tool_args)
            tool_output = executor()

        return {
            "role": "tool",
            "tool_name": tool_name,
            "tool_args": tool_args,
            "tool_call_id": tool_id,
            "content": str(tool_output),
        }

    # ═══════════════════════════════════════════════════════════
    #  智能体主循环
    # ═══════════════════════════════════════════════════════════

    def agent_loop(self) -> None:
        """一轮对话的核心循环：LLM 调用 → 工具执行 → 结果回放，直到无 tool_call。"""

        # ── 后台任务通知预热（turn 起点，while 之外，只跑一次）────────────
        # 上一 turn 退出时，turn 内最后那一轮迭代才会触发 collect_background_results()；
        # 如果上一 turn 在 LLM 不再返回 tool_call 时自然结束，那一帧可能没机会把
        # "已完成的 bg" 喂进来。这段预热专门处理"新 turn 进来时，把之前已经完成、
        # 还没被消费过的后台任务结果先注入上下文"，避免模型在新 turn 第一轮就误判
        # "任务还在跑"而另起一个新任务重复劳动。
        #
        # 注意：必须放在 while 之外，只在 turn 起点跑一次；while 内部的
        # 迭代间反馈仍由 collect_background_results() 负责。
        pre_notifs = self.background_manager.collect_background_results()
        if pre_notifs:
            pre_msg = {"role": "user", "content": "\n".join(pre_notifs)}
            self.history_messages.append(pre_msg)
            self.session_manager.append_message_to_session(self.session_file, pre_msg)
            self._print(
                f"  \033[32m[inject pre-loop] {len(pre_notifs)} background notification(s)\033[0m"
            )

        iteration = 0  # 循环迭代计数
        rounds_since_todo = 0  # 记录距离上次调用 todo 工具的轮数，用于 nag reminder

        # 这里可以增加s10课程中更新systemprompt的逻辑，同时更新内存message和会话记录的jsonl文件。
        # 这样可以保证记忆、工具、skill的实时更新，但会影响缓存未命中率。

        while True:
            iteration += 1
            if iteration > self.MAX_AGENT_ITERATIONS:
                self._print(
                    f"\033[31m[警告] 智能体循环达到最大迭代次数 ({self.MAX_AGENT_ITERATIONS})，强制结束\033[0m"
                )
                break

            # 在调用 LLM 前检查上下文，达到阈值时阻塞执行压缩并同步会话文件。
            self.session_manager.maybe_compact_context(
                self.history_messages, self.session_file
            )

            # S11 Error Recovery — 错误不是结束，是重试的开始（详见 agents/error_recovery.py）
            try:
                # 调用大模型执行当前轮次的回复
                # lambda 通过闭包把当前轮次的 max_tokens / model 锁住，控制器在循环里
                # 可能会通过 ESCALATE / FALLBACK 改变这些值，但本轮 lambda 已固定
                llm_response = self.recovery.with_retry(
                    lambda mt=self.recovery.current_max_tokens, mdl=self.recovery.current_model:
                    self.llm_client.chat.completions.create(
                        model=mdl,
                        messages=self.history_messages,
                        max_tokens=mt,
                        tools=self.tools.build_agent_tools(team_mode=self.team_mode),
                        tool_choice="auto",  # 工具选择，值域 none、auto、required，默认 auto
                        parallel_tool_calls=True,  # 是否并行执行工具调用，默认 False
                        stream=False,  # 是否流式输出，默认 False
                        temperature=0.5,
                        reasoning_effort="high",  # 思考强度，DeepSeek只有 high、max 两个选项
                        extra_body={"thinking": {"type": "enabled"}},  # 思考模式开关
                    )
                )
                # 从大模型回复中提取消息
                response_msg = llm_response.choices[0].message
                # 提取大模型回复中的工具调用
                response_tool_calls = response_msg.tool_calls or []
                # 累计 token 消耗（OpenAI usage：prompt_tokens 输入 + completion_tokens 输出）。
                # /goal 状态里的"目标期间花费" = 当前累计 − 设置目标时的累计
                # （tokens_at_start，见 Agent.set_goal）
                usage = getattr(llm_response, "usage", None)
                if usage is not None:
                    self.total_tokens += int(getattr(usage, "prompt_tokens", 0) or 0) \
                        + int(getattr(usage, "completion_tokens", 0) or 0)
                # 打印大模型的思考和回复内容
                # ANSI: \033[2m=暗(细体)，\033[90m=灰色，\033[0m=重置
                self._print(
                    f"\033[2;90m[thinking]\n{truncate_chars(response_msg.reasoning_content, 300)}\n[/thinking]\033[0m"
                )
                # self._print(f"[本轮回复]\n{response_msg.content}")

            except Exception as e:
                # 外层异常处理：内层 with_retry 主动 raise 出来的"非临时错误"会到这一层。
                # 控制器根据错误类型决定：继续重试（CONTINUE）或退出（ABORT）。
                if self.recovery.handle_exception(
                    e, self.history_messages, self.session_manager, self.session_file
                ) == RecoveryAction.ABORT:
                    return
                continue

            # Path 1：max_tokens 截断恢复
            # 注意：max_tokens 不是异常，是 API 正常返回的 finish_reason 之一
            # DeepSeek 走 OpenAI 兼容协议，没有 Anthropic 的 stop_reason 字段；
            # 截断的判定在 choices[0].finish_reason == "length"（OpenAI 官方语义）
            if llm_response.choices[0].finish_reason == "length":
                if self.recovery.handle_truncation(
                    response_msg, self.history_messages, self.session_manager, self.session_file
                ) == RecoveryAction.ABORT:
                    return
                continue

            # ── 正常完成：把 assistant 的回复追加到对话历史 ──
            # 注意：这里的 response.content 是模型完整返回的内容
            # （如果是 max_tokens 截断，就已经在上面 append 过了，不会走到这里）
            # 将 Pydantic 模型转换为 dict，保留 role/content/reasoning_content/tool_calls 等字段
            response_msg_dict = response_msg.model_dump()
            # 加入大模型回复到历史消息中,role 为 assistant，包含思考过程和回答内容
            self.history_messages.append(response_msg_dict)
            self.session_manager.append_message_to_session(
                self.session_file, response_msg_dict
            )

            if len(response_tool_calls) == 0:
                # ── 停止边界（goal 的唯一拦截点，教程称"会话级 Stop 钩子"）──
                # 模型不再调工具 = 它想停下来。无目标时 evaluate_after_turn
                # 直接放行（allow），行为与原来完全一致；有目标时先裁决再放行。
                decision = self.goal_controller.evaluate_after_turn(
                    self.history_messages,
                    background_running=self.background_manager.has_running(),
                )
                if decision.action == "block":
                    # 目标未达成：把目标条件与评估器理由回注入消息（同时落盘），
                    # continue 回环让 Worker 看到反馈后继续朝目标干活
                    condition = (
                        self.goal_controller.active.condition
                        if self.goal_controller.active else ""
                    )
                    block_msg = {
                        "role": "user",
                        "content": (
                            "[Goal still active]\n"
                            f"Condition: {condition}\n"
                            f"Evaluator: {decision.reason}\n"
                            "Continue working and surface the missing evidence."
                        ),
                    }
                    self.history_messages.append(block_msg)
                    self.session_manager.append_message_to_session(
                        self.session_file, block_msg
                    )
                    continue
                if decision.action == "defer":
                    # 后台任务仍在跑：此刻判"达成"不可靠（证据未回来），本轮先结束；
                    # 后台结果由下轮 turn 起点的 collect_background_results 预热
                    # 注入上下文，用户下次输入后再续判。目标保持激活。
                    self._print(f"\033[33m[goal] defer: {decision.reason}\033[0m")
                    return
                # 终止态只打印结论供用户感知，目标状态已由控制器内部处理：
                #   achieved（达成，清目标）/ failed（无法完成，清目标）/
                #   limit（连续 block 超 block_cap，强制结束，目标保持激活）/
                #   error（评估器调用出错，目标保持激活）
                if decision.action == "achieved":
                    self._print(f"\033[33m[goal] achieved: {decision.reason}\033[0m")
                elif decision.action == "failed":
                    self._print(f"\033[31m[goal] failed: {decision.reason}\033[0m")
                elif decision.action == "limit":
                    self._print(f"\033[31m[goal] limit: {decision.reason}\033[0m")
                elif decision.action == "error":
                    self._print(f"\033[31m[goal] evaluation error: {decision.reason}\033[0m")
                # allow（无目标）与各终止态 → 走原有 Stop hook 流程
                # （钩子返回非 None 仍可强制续跑，goal 之外的第二道拦截不受影响）
                force = self.hook_system.trigger("Stop", self.history_messages)
                if force:
                    # 往消息中记录强制结束的原因
                    self.history_messages.append({"role": "user", "content": force})
                    continue
                return

            # ANSI: \033[2m=暗(细体)，\033[93m=浅黄，\033[0m=重置（与上方灰色 [thinking] 区分）
            self._print(f"\033[2;93m[本轮大模型调用工具数量] {len(response_tool_calls)}\033[0m")
            for tc in response_tool_calls:
                # 单行打印超 200 字符截断，避免大参数（如大段代码/长路径）刷屏
                self._print(
                    f"\033[2;93m{truncate_chars(f"  - {tc.function.name}({tc.function.arguments})  #id={tc.id}\n ")}\033[0m"
                )
            self._print(f"\033[2;93m[本轮大模型工具调用结束,等待执行结果]\033[0m")
            # 三阶段执行：后台 → 并行 → 串行, 互斥分桶。
            #   后台桶: args.run_in_background=true, 立即分发给 background_manager 守护线程
            #   并行桶: args.parallel=true (且非后台), 线程池并发, 全部完成才走下一步
            #   串行桶: args.parallel=false 或缺省 (且非后台), 按声明顺序逐个执行
            # 结果用 {tool_call_id: result} 收集, 最后按 LLM 原始声明顺序回放到 history,
            # 保证 tool 消息顺序与 tool_calls 顺序一致(OpenAI 协议硬约束)。
            tool_call_results: dict[str, dict] = {}
            used_todo = False
            background_calls, parallel_calls, serial_calls = [], [], []
            for tool_call in response_tool_calls:
                if tool_call.function.name == "todo":
                    used_todo = True
                # 解析一次参数, 后面复用, 避免每阶段都重复 json.loads
                raw_args = tool_call.function.arguments
                tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                tool_call._args_cache = tool_args
                if tool_args.get("run_in_background"):
                    background_calls.append(tool_call)
                elif tool_args.get("parallel"):
                    parallel_calls.append(tool_call)
                else:
                    serial_calls.append(tool_call)

            # 阶段 1: 后台分发——立即拿到 bg_id 占位 result, 不阻塞当前 turn
            for tool_call in background_calls:
                # s04: PreToolUse 钩子, 主线程触发(hook 大多是同步观察者, 不该跨线程)
                blocked = self.hook_system.trigger("PreToolUse", tool_call)
                if blocked:
                    tool_call_results[tool_call.id] = {
                        "role": "tool", "tool_call_id": tool_call.id, "content": str(blocked)
                    }
                    continue
                tool_call_result = self._execute_tool_call(tool_call)
                self._print(
                    f"\033[2;93m [工具执行结果(后台)]\n {truncate_chars(str(tool_call_result.get("content", "")))}\n [/工具执行结果]\033[0m"
                )
                tool_call_results[tool_call.id] = tool_call_result
                self.hook_system.trigger("PostToolUse", tool_call, tool_call_result)

            # 阶段 2: 并行执行——parallel=true 且非后台, 线程池并发
            if parallel_calls:
                with ThreadPoolExecutor(max_workers=len(parallel_calls)) as executor:
                    # PreToolUse 在主线程顺序触发, 避免 hook 跨线程
                    futures: dict = {}
                    for tool_call in parallel_calls:
                        blocked = self.hook_system.trigger("PreToolUse", tool_call)
                        if blocked:
                            tool_call_results[tool_call.id] = {
                                "role": "tool", "tool_call_id": tool_call.id, "content": str(blocked)
                            }
                            continue
                        fut = executor.submit(self._execute_tool_call, tool_call)
                        futures[fut] = tool_call
                    # 收集结果, as_completed 谁先完谁先回填; 异常兜底避免 tool_call_id 缺失
                    for fut in as_completed(futures):
                        tc = futures[fut]
                        try:
                            tool_call_result = fut.result()
                        except Exception as e:
                            tool_call_result = {
                                "role": "tool", "tool_call_id": tc.id,
                                "content": f"Error: {type(e).__name__}: {e}",
                            }
                        self._print(
                            f"\033[2;93m [工具执行结果(并行)]\n {truncate_chars(str(tool_call_result.get("content", "")))}\n [/工具执行结果]\033[0m"
                        )
                        tool_call_results[tc.id] = tool_call_result
                        self.hook_system.trigger("PostToolUse", tc, tool_call_result)

            # 阶段 3: 串行执行——按声明顺序, 一个一个来
            for tool_call in serial_calls:
                blocked = self.hook_system.trigger("PreToolUse", tool_call)
                if blocked:
                    tool_call_results[tool_call.id] = {
                        "role": "tool", "tool_call_id": tool_call.id, "content": str(blocked)
                    }
                    continue
                tool_call_result = self._execute_tool_call(tool_call)
                self._print(
                    f"\033[2;93m [工具执行结果(串行)]\n {truncate_chars(str(tool_call_result.get("content", "")))}\n [/工具执行结果]\033[0m"
                )
                tool_call_results[tool_call.id] = tool_call_result
                self.hook_system.trigger("PostToolUse", tool_call, tool_call_result)

            # 按 LLM 声明顺序回放 tool 消息(三桶结果合并, 严格保序)
            for tc in response_tool_calls:
                result = tool_call_results.get(tc.id)
                if result is None:
                    # 极端兜底: hook 拦截或异常分支下, 万一没填, 写一条占位
                    result = {"role": "tool", "tool_call_id": tc.id,
                              "content": f"Error: no result for {tc.id}"}
                content = result.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                tool_msg = {"role": "tool", "content": content, "tool_call_id": tc.id}
                self.history_messages.append(tool_msg)
                self.session_manager.append_message_to_session(
                    self.session_file, tool_msg
                )

            # 后台任务通知注入：本轮（或更早轮次）已完成的后台任务，
            # 把它们的输出整理成 <task_notification> 文本块作为 user 消息追加。
            # 与 s13 教程的"每轮都收集"语义一致：
            # - daemon 线程可能在任何时刻完成 task，调用方随时可以拿到通知；
            # - 同一结果只通知一次（collect_background_results 内部 pop）。
            # 不要把通知合并进 tool message——tool 消息必须严格对应
            # assistant.tool_calls 里的 tool_call_id，否则 LLM 会报参数错误。
            bg_notifications = self.background_manager.collect_background_results()
            if bg_notifications:
                notification_msg = {
                    "role": "user",
                    "content": "\n".join(bg_notifications),
                }
                self.history_messages.append(notification_msg)
                self.session_manager.append_message_to_session(
                    self.session_file, notification_msg
                )
                self._print(
                    f"  \033[32m[inject] {len(bg_notifications)} background notification(s)\033[0m"
                )

            # todo 更新追踪: 本轮用了 todo 就清零, 否则累加;
            # 连续 3 轮未更新且仍有 open items 时, 注入提醒作为本轮最后一条消息, 并清零避免重复打扰
            rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
            if rounds_since_todo >= 3 and self.tools.get_todo_manager().has_open_items():
                reminder_msg = {"role": "user", "content": "<reminder>Update your tasks.</reminder>"}
                self.history_messages.append(reminder_msg)
                self.session_manager.append_message_to_session(
                    self.session_file, reminder_msg
                )
                rounds_since_todo = 0

        self._print("\033[2;93m[****一个turn循环结束****]\n \033[0m\n")
