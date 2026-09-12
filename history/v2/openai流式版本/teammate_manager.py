#!/usr/bin/env python3
"""
teammate_manager.py - 团队成员管理模块

本模块实现基于文件的 JSONL 收件箱的团队成员管理系统，整合自 s17 课程
（autonomous agents：WORK → IDLE → SHUTDOWN 生命周期）。核心类 TeammateManager：

- 每个队友是一个独立线程：WORK 阶段（LLM 循环）→ IDLE 阶段（空闲轮询）→ SHUTDOWN
- 空闲时轮询邮箱（优先处理协议消息）与任务板（自动认领未分配任务）
- 支持 shutdown / plan_approval 协议，通过 request_id 关联请求与响应
- 持久化团队配置到 config.json（team_name / members / status）

消息总线 MessageBus 来自 message_bus.py（每成员一个 JSONL 收件箱）。
LLM 调用使用 OpenAI SDK（llm_manage.LLMClient），工具执行通过注入的 ToolRegistry。
"""
import os
import json
import time
import random
import threading
from dataclasses import dataclass, field
from pathlib import Path

from message_bus import MessageBus
from llm_manage import LLMClient
from paths import INBOX_DIR
from tools import ToolRegistry
from streaming_client import streamed_create

# ── 可调参数（遵循 .env 约定，见 AGENTS.md）──
IDLE_POLL_INTERVAL = int(os.environ.get("IDLE_POLL_INTERVAL", "5"))   # 空闲等待的兜底周期（秒），配合 Event 即时唤醒
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "60"))              # 空闲超时时间（秒），超过则队友关闭
TEAM_MAX_TOOL_ROUNDS = int(os.environ.get("TEAM_MAX_TOOL_ROUNDS", "5"))  # 每轮 WORK 最多 tool_use 轮数
TEAM_MAX_TOKENS = int(os.environ.get("TEAM_MAX_TOKENS", "4096"))      # 队友每轮 LLM 生成的 max_tokens（聚焦小任务，宜小以提速）


# ── Protocol State（智能体间协议状态，来自 s16）──
# 协议机制：Lead 可以向队友发送 shutdown/plan_approval 请求，
# 队友通过 request_id 响应，match_response 将响应关联回原始请求

@dataclass
class ProtocolState:
    """协议状态：记录一次请求的完整生命周期。"""
    request_id: str      # 请求唯一标识
    type: str            # 协议类型：shutdown / plan_approval
    sender: str          # 发送方
    target: str          # 目标方
    status: str          # 当前状态：pending / approved / rejected
    payload: str         # 请求负载（如计划内容）
    created_at: float = field(default_factory=time.time)


class TeammateManager:
    """
    团队成员管理器，负责管理团队成员的生命周期、协议通信与团队配置。

    核心职责：
    - 持久化团队配置（config.json）：team_name + members（含状态）
    - 通过独立线程运行每位队友的自主代理循环（WORK → IDLE → SHUTDOWN）
    - 空闲轮询邮箱与任务板，自动认领未分配任务（scan_unclaimed_tasks / idle_poll）
    - 协议机制：shutdown / plan_approval，request_id 关联请求与响应
    - 为 Lead 提供队友管理工具（spawn_teammate / send_message / check_inbox /
      request_shutdown / request_plan / review_plan）

    成员状态机：
    - idle -> working: 收到新消息或自动认领任务
    - working -> idle: 无新工作（空闲轮询超时）
    - working/idle -> shutdown: 收到 shutdown_request 并批准
    """

    def __init__(self, team_dir: Path, tools: ToolRegistry = None):
        """
        初始化团队成员管理器。

        参数:
            team_dir: 团队目录路径，用于存放 config.json
            tools: ToolRegistry 实例，用于执行工具调用（默认构造实例，非全局单例）
        """
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)                      # 确保目录存在
        self.config_path = self.dir / "config.json"        # 团队配置文件路径
        self.config = self._load_config()                  # 加载团队配置
        self.threads = {}                                  # 存储队友线程 {name: Thread}
        self.bus = MessageBus(INBOX_DIR)                   # 消息总线（JSONL 收件箱）
        self.llm_client = LLMClient().llm                  # OpenAI SDK 客户端
        self.model = os.environ.get("OPENAI_MODEL_ID", "") # 模型 ID
        # 注入工具实例（实例级默认构造，非全局单例）
        self.tools = tools if tools is not None else ToolRegistry()

        # s17：协议请求与活跃队友追踪（实例级）
        self.pending_requests: dict[str, ProtocolState] = {}
        self.active_teammates: dict[str, bool] = {}
        # s17 优化：每队友一个唤醒事件，Lead 发消息即 set()，消除轮询唤醒延迟
        self.wake_events: dict[str, threading.Event] = {}
        # s18：队友 → 绑定的 worktree 路径（Path | None），文件操作以此为工作根
        self._member_worktrees: dict[str, Path | None] = {}

    # ═══════════════════════════════════════════════════════════
    #  团队配置持久化（config.json）
    # ═══════════════════════════════════════════════════════════

    def _load_config(self) -> dict:
        """从文件加载团队配置；不存在时返回默认配置。"""
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self) -> None:
        """将当前团队配置保存到文件（格式化 JSON）。"""
        self.config_path.write_text(
            json.dumps(self.config, indent=2, ensure_ascii=False))

    def _find_member(self, name: str) -> dict | None:
        """根据名称查找团队成员；未找到返回 None。"""
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _update_member_status(self, name: str, status: str) -> None:
        """更新指定团队成员的状态并保存配置。"""
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()

    def list_all(self) -> str:
        """列出所有团队成员及其状态。"""
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        """获取所有团队成员的名称列表。"""
        return [m["name"] for m in self.config["members"]]

    # ═══════════════════════════════════════════════════════════
    #  协议机制：请求 ID 生成与响应关联（来自 s16）
    # ═══════════════════════════════════════════════════════════

    def _send(self, sender: str, to: str, content: str,
              msg_type: str = "message", extra: dict = None) -> str:
        """统一发送封装：写入总线后立即 set 目标队友的唤醒事件。

        目标为 lead（无唤醒事件）时 get 返回 None 安全跳过；目标为队友时
        立刻唤醒，把协作握手延迟从轮询周期降到趋近 0。
        """
        result = self.bus.send(sender, to, content, msg_type, extra)
        event = self.wake_events.get(to)
        if event is not None:
            event.set()
        return result

    def new_request_id(self) -> str:
        """生成新的协议请求 ID。"""
        return f"req_{random.randint(0, 999999):06d}"

    def match_response(self, response_type: str, request_id: str, approve: bool):
        """通过 request_id 将响应与原始请求关联起来。"""
        state = self.pending_requests.get(request_id)
        if not state:
            print(f"  \033[31m[protocol] unknown request_id: {request_id}\033[0m")
            return
        # 校验响应类型与请求类型是否匹配
        if state.type == "shutdown" and response_type != "shutdown_response":
            print(f"  \033[31m[protocol] type mismatch: expected shutdown_response, "
                  f"got {response_type}\033[0m")
            return
        if state.type == "plan_approval" and response_type != "plan_approval_response":
            print(f"  \033[31m[protocol] type mismatch: expected plan_approval_response, "
                  f"got {response_type}\033[0m")
            return
        state.status = "approved" if approve else "rejected"
        icon = "✓" if approve else "✗"
        color = "32" if approve else "31"
        print(f"  \033[{color}m[protocol] {state.type} {icon} "
              f"({request_id}: {state.status})\033[0m")

    # ═══════════════════════════════════════════════════════════
    #  自主队友：任务板扫描 + 空闲轮询（来自 s17）
    # ═══════════════════════════════════════════════════════════

    def scan_unclaimed_tasks(self) -> list:
        """扫描任务板，找到所有待处理、未被认领、且依赖已完成的任务。"""
        tm = self.tools.task_manager
        return [t for t in tm._list_tasks()
                if t.status == "pending" and not t.owner and tm._can_start(t.id)]

    def idle_poll(self, name: str, messages: list) -> str:
        """空闲等待（被唤醒即时处理，否则超时兜底）。返回 'work'、'shutdown' 或 'timeout'。

        s17 优化：用 per-teammate 唤醒事件 event.wait() 替代固定 time.sleep 轮询。
        Lead 发消息到该队友时 _send() 会 set() 事件立刻唤醒，消除 0~IDLE_POLL_INTERVAL 的等待延迟；无消息时 event.wait() 为阻塞休眠，不空转 CPU。IDLE_POLL_INTERVAL
        作为唤醒等待的兜底周期，IDLE_TIMEOUT 作为整体等待上限。
        """
        event = self.wake_events.get(name)
        deadline = time.time() + IDLE_TIMEOUT
        while time.time() < deadline:
            # 有事件则阻塞等待被唤醒（_send set 后立即返回），无事件退化为固定间隔轮询
            if event is not None:
                event.wait(IDLE_POLL_INTERVAL)
                event.clear()
            else:
                time.sleep(IDLE_POLL_INTERVAL)

            # 第一步：检查邮箱——优先处理协议消息
            inbox = self.bus.read_inbox(name)
            if inbox:
                # 检查是否有关闭请求（shutdown_request）
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        req_id = msg.get("request_id", "")
                        self._send(name, "lead", "Shutting down gracefully.",
                                      "shutdown_response",
                                      {"request_id": req_id, "approve": True})
                        print(f"  \033[35m[protocol] {name} approved shutdown "
                              f"in idle ({req_id})\033[0m")
                        return "shutdown"

                # 非协议消息：注入对话上下文，恢复工作
                messages.append({"role": "user",
                    "content": "<inbox>" + json.dumps(inbox) + "</inbox>"})
                print(f"  \033[36m[idle] {name} found inbox messages\033[0m")
                return "work"

            # 第二步：扫描任务板，自动认领未分配的任务
            unclaimed = self.scan_unclaimed_tasks()
            if unclaimed:
                task = unclaimed[0]
                result = self.tools.task_manager._claim_task(task.id, owner=name)
                if "Claimed" in result:
                    messages.append({"role": "user",
                        "content": f"<auto-claimed>Task {task.id}: "
                                   f"{task.subject}</auto-claimed>"})
                    print(f"  \033[32m[idle] {name} auto-claimed: "
                          f"{task.subject}\033[0m")
                    return "work"
                print(f"  \033[33m[idle] {name} claim failed: "
                      f"{result}\033[0m")

        print(f"  \033[31m[idle] {name} timeout ({IDLE_TIMEOUT}s)\033[0m")
        return "timeout"

    # ═══════════════════════════════════════════════════════════
    #  队友线程：WORK → IDLE → SHUTDOWN（来自 s15 + s16 + s17）
    #  每个队友是一个独立线程，拥有自己的消息列表和 LLM 调用循环
    # ═══════════════════════════════════════════════════════════

    def spawn_teammate(self, name: str, role: str, prompt: str, worktree: str | None = None) -> str:
        """生成一个自主队友智能体（独立线程）。

        worktree: 可选，已创建 worktree 的名称。给定时队友的文件操作
                  （bash/read/write/read_pdf）以此 worktree 为工作根。
        """
        # s18：解析并校验 worktree（先于重复检查，避免非法名称走到 spawn）
        wt_path = None
        if worktree:
            wm = self.tools.get_worktree_manager()
            resolved = wm.resolve(worktree)
            if not resolved.exists():
                return (f"Worktree '{worktree}' not found. "
                        "Create it first via create_worktree.")
            wt_path = resolved

        if name in self.active_teammates or (self.threads.get(name)
                                             and self.threads[name].is_alive()):
            return f"Teammate '{name}' already exists"

        # 持久化成员记录：已存在则重新激活，不存在则新建
        member = self._find_member(name)
        if member is None:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        else:
            member["status"] = "working"
            member["role"] = role
        self._save_config()

        self.active_teammates[name] = True
        self.wake_events[name] = threading.Event()   # 为此队友创建唤醒事件
        self._member_worktrees[name] = wt_path       # s18：记录队友工作目录（worktree 或 None）
        thread = threading.Thread(
            target=self._teammate_loop, args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
        return f"Teammate '{name}' spawned as {role} (autonomous)"

    def _handle_inbox_message(self, name: str, msg: dict, messages: list) -> bool:
        """根据消息类型分派传入的协议消息；返回 True 表示需要关闭。"""
        msg_type = msg.get("type", "message")
        req_id = msg.get("request_id", "")

        if msg_type == "shutdown_request":
            # 处理关闭请求：自动批准并回复
            self._send(name, "lead", "Shutting down gracefully.",
                          "shutdown_response",
                          {"request_id": req_id, "approve": True})
            print(f"  \033[35m[protocol] {name} approved shutdown "
                  f"({req_id})\033[0m")
            return True  # 返回 True 表示需要关闭

        if msg_type == "plan_approval_response":
            # 处理计划审批回复：批准或拒绝后注入提示
            approve = msg.get("approve", False)
            if approve:
                messages.append({"role": "user",
                    "content": "[Plan approved] Proceed with the task."})
            else:
                messages.append({"role": "user",
                    "content": f"[Plan rejected] Feedback: {msg['content']}"})
        return False  # 不需要关闭

    def _teammate_loop(self, name: str, role: str, prompt: str):
        """队友的代理循环（独立线程）：WORK 阶段 → IDLE 阶段 → 结束。"""
        system = (f"You are '{name}', a {role}. "
                  f"Use tools to complete tasks. "
                  f"You can list and claim tasks from the board. "
                  f"Check inbox for protocol messages.")
        # s18：若队友被指派到 worktree，告知其工作目录（文件操作根）
        wt_path = self._member_worktrees.get(name)
        if wt_path:
            system += (f"\n<system-reminder>你的工作目录（所有文件操作根）是："
                       f"{wt_path}。在此目录内改代码并运行测试。</system-reminder>")

        # OpenAI 格式：首条为 system，随后为初始任务
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": prompt}]

        # 外层循环：WORK → IDLE 循环
        while True:
            # 身份信息重新注入（s17 新增，防止上下文压缩后丢失身份）
            if len(messages) <= 3:
                messages.insert(0, {"role": "user",
                    "content": f"<identity>You are '{name}', role: {role}. "
                               f"Continue your work.</identity>"})

            # ── WORK 阶段：LLM 调用循环（OpenAI SDK）──
            should_shutdown = False
            for _ in range(TEAM_MAX_TOOL_ROUNDS):
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    stopped = self._handle_inbox_message(name, msg, messages)
                    if stopped:
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox
                                    if m.get("type") == "message"]
                    if non_protocol:
                        messages.append({"role": "user",
                            "content": f"<inbox>{json.dumps(non_protocol)}</inbox>"})

                try:
                    # 统一流式入口：队友运行在后台线程，不上任何 UI（sinks=None），
                    # 仅内部聚合出完整消息（接口兼容 OpenAI message）
                    response_msg, finish_reason, _usage = streamed_create(
                        self.llm_client,
                        model=self.model,
                        messages=messages,
                        tools=self._teammate_tools(),
                        tool_choice="auto",
                        max_tokens=TEAM_MAX_TOKENS,
                    )
                except Exception as e:
                    print(f"  \033[31m[teammate] {name} LLM error: {e}\033[0m")
                    break

                tool_calls = response_msg.tool_calls or []
                # 以 OpenAI 请求格式存 assistant 消息（仅保留 role/content/tool_calls，
                # 去掉 model_dump() 混入的 refusal/audio/index 等响应字段）
                assistant_msg = {"role": "assistant"}
                if response_msg.content is not None:
                    assistant_msg["content"] = response_msg.content
                if tool_calls:
                    assistant_msg["tool_calls"] = [
                        tc.model_dump() for tc in tool_calls]
                messages.append(assistant_msg)
                if finish_reason != "tool_calls" or not tool_calls:
                    break  # 非工具调用 → 停止本轮

                for tc in tool_calls:
                    tool_name = tc.function.name
                    try:
                        tool_args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        tool_args = {}
                    output = self._exec(name, tool_name, tool_args)
                    # OpenAI 要求每个工具结果作为独立 tool 消息返回，
                    # 不能再打包进 content（会导致 missing field `type` 400）
                    messages.append({"role": "tool",
                                     "tool_call_id": tc.id,
                                     "name": tool_name,
                                     "content": str(output)})

            if should_shutdown:
                break

            # ── IDLE 阶段（s17 新增）：空闲轮询 ──
            idle_result = self.idle_poll(name, messages)
            if idle_result in ("shutdown", "timeout"):
                break

        # 总结工作结果，发送给 Lead
        summary = "Done."
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    summary = content
                    break
        self._send(name, "lead", summary, "result")
        self._update_member_status(name, "shutdown" if should_shutdown else "idle")
        self.active_teammates.pop(name, None)
        self.wake_events.pop(name, None)
        print(f"  \033[32m[teammate] {name} finished\033[0m")

    # ═══════════════════════════════════════════════════════════
    #  队友工具：定义（OpenAI function calling 格式）+ 执行分发
    # ═══════════════════════════════════════════════════════════

    def _teammate_tools(self) -> list:
        """队友可用的工具定义（OpenAI 格式）。

        通用基础工具（bash / run_read / run_write）直接复用
        ToolRegistry.base_tools 的既有定义（单一事实来源，避免与 tools.py 漂移）；
        团队协议/任务工具是 teammate 角色独有，与 MessageBus / TaskManager /
        ProtocolState 绑定，保留在本地。
        """
        wanted = {"bash", "run_read", "run_write", "run_read_pdf"}
        base = [t for t in self.tools.base_tools
                if t["function"]["name"] in wanted]
        protocol = [
            {"type": "function", "function": {
                "name": "send_message",
                "description": "Send message to another agent.",
                "parameters": {"type": "object",
                               "properties": {"to": {"type": "string"},
                                              "content": {"type": "string"}},
                               "required": ["to", "content"]}}},
            {"type": "function", "function": {
                "name": "submit_plan",
                "description": "Submit a plan for Lead approval.",
                "parameters": {"type": "object",
                               "properties": {"plan": {"type": "string"}},
                               "required": ["plan"]}}},
            # s17 新增：队友可以列出、认领和完成任务
            {"type": "function", "function": {
                "name": "list_tasks",
                "description": "List all tasks on the board.",
                "parameters": {"type": "object", "properties": {},
                               "required": []}}},
            {"type": "function", "function": {
                "name": "claim_task",
                "description": "Claim a pending task.",
                "parameters": {"type": "object",
                               "properties": {"task_id": {"type": "string"}},
                               "required": ["task_id"]}}},
            {"type": "function", "function": {
                "name": "complete_task",
                "description": "Mark an in-progress task as completed.",
                "parameters": {"type": "object",
                               "properties": {"task_id": {"type": "string"}},
                               "required": ["task_id"]}}},
        ]
        return base + protocol

    def _exec(self, name: str, tool_name: str, args: dict) -> str:
        """执行队友的工具调用。"""
        # s18：若无绑定 worktree，base=None → 主目录；有则文件操作落在 worktree 内
        base = self._member_worktrees.get(name)
        if tool_name == "bash":
            return self.tools.run_bash(args.get("command", ""), base=base)
        if tool_name == "run_read":
            return self.tools.run_read(args.get("path", ""), args.get("limit"), base=base)
        if tool_name == "run_write":
            return self.tools.run_write(args.get("path", ""), args.get("content", ""), base=base)
        if tool_name == "run_edit":
            return self.tools.run_edit(
                args.get("path", ""), args.get("old_text", ""),
                args.get("new_text", ""), base=base)
        if tool_name == "run_glob":
            return self.tools.run_glob(args.get("pattern", ""), base=base)
        if tool_name == "run_read_pdf":
            kwargs = {"path": args.get("path", "")}
            if args.get("max_pages") is not None:
                kwargs["max_pages"] = args["max_pages"]
            if args.get("chars_per_page") is not None:
                kwargs["chars_per_page"] = args["chars_per_page"]
            return self.tools.run_read_pdf(**kwargs, base=base)
        if tool_name == "send_message":
            self._send(name, args.get("to", ""), args.get("content", ""))
            return "Sent"
        if tool_name == "submit_plan":
            return self._teammate_submit_plan(name, args.get("plan", ""))
        if tool_name == "list_tasks":
            tasks = self.tools.task_manager._list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                for t in tasks)
        if tool_name == "claim_task":
            return self.tools.task_manager._claim_task(
                args.get("task_id", ""), owner=name)
        if tool_name == "complete_task":
            return self.tools.task_manager._complete_task(args.get("task_id", ""))
        return f"Unknown tool: {tool_name}"

    def _teammate_submit_plan(self, from_name: str, plan: str) -> str:
        """队友向 Lead 提交计划等待审批。"""
        req_id = self.new_request_id()
        self.pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="plan_approval",
            sender=from_name, target="lead",
            status="pending", payload=plan)
        self._send(from_name, "lead", plan,
                      "plan_approval_request",
                      {"request_id": req_id})
        return f"Plan submitted ({req_id}). Waiting for approval..."

    # ═══════════════════════════════════════════════════════════
    #  Lead 协议工具（来自 s16）：关闭请求 / 计划审批
    # ═══════════════════════════════════════════════════════════

    def request_shutdown(self, teammate: str) -> str:
        """请求指定队友优雅关闭。"""
        req_id = self.new_request_id()
        self.pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="shutdown",
            sender="lead", target=teammate,
            status="pending", payload="")
        self._send("lead", teammate, "Please shut down gracefully.",
                      "shutdown_request",
                      {"request_id": req_id})
        print(f"  \033[35m[protocol] shutdown_request → {teammate} "
              f"({req_id})\033[0m")
        return f"Shutdown request sent to {teammate} (req: {req_id})"

    def request_plan(self, teammate: str, task: str) -> str:
        """Lead 要求队友提交一份计划。"""
        self._send("lead", teammate, f"Please submit a plan for: {task}",
                      "message")
        return f"Asked {teammate} to submit a plan"

    def review_plan(self, request_id: str, approve: bool,
                    feedback: str = "") -> str:
        """审批或拒绝队友提交的计划。"""
        state = self.pending_requests.get(request_id)
        if not state:
            return f"Request {request_id} not found"
        if state.status != "pending":
            return f"Request {request_id} already {state.status}"
        state.status = "approved" if approve else "rejected"
        self._send("lead", state.sender,
                      feedback or ("Approved" if approve else "Rejected"),
                      "plan_approval_response",
                      {"request_id": request_id, "approve": approve})
        icon = "✓" if approve else "✗"
        print(f"  \033[32m[protocol] plan {icon} ({request_id})\033[0m")
        return f"Plan {'approved' if approve else 'rejected'} ({request_id})"

    # ═══════════════════════════════════════════════════════════
    #  Lead 邮箱消费（路由协议响应 + 注入队友消息到对话历史）
    # ═══════════════════════════════════════════════════════════

    def send_message(self, to: str, content: str) -> str:
        """Lead 向队友发送普通消息。"""
        self._send("lead", to, content)
        return f"Sent to {to}"

    def consume_lead_inbox(self, route_protocol: bool = True) -> list[dict]:
        """读取 Lead 的邮箱：路由协议响应，返回所有消息。"""
        msgs = self.bus.read_inbox("lead")
        if route_protocol:
            for msg in msgs:
                req_id = msg.get("request_id", "")
                msg_type = msg.get("type", "")
                if req_id and msg_type.endswith("_response"):
                    self.match_response(msg_type, req_id,
                                        msg.get("approve", False))
        return msgs

    def check_inbox(self) -> str:
        """检查 Lead 的邮箱，路由协议消息，返回格式化后的消息列表。"""
        msgs = self.consume_lead_inbox(route_protocol=True)
        if not msgs:
            return "(inbox empty)"
        lines = []
        for m in msgs:
            req_id = m.get("request_id", "")
            tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
            lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
        return "\n".join(lines)
