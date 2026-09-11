#!/usr/bin/env python3
# ============================================================================
# s13 课后代码：Background Tasks（后台任务）
# ----------------------------------------------------------------------------
# 本文件是 v2 教程第 13 课的代码，核心主题是"后台任务"：
#
#   为什么需要后台任务？
#     模型在干活时会请求执行耗时命令（pip install、跑测试、构建等）。
#     如果 agent 循环同步傻等命令跑完，整个 agent 就被卡住，且一次只能干一件事。
#     s13 的解法：
#       1) 用 threading.Thread 把耗时工具调用丢到后台线程执行；
#       2) 立即给模型返回"占位" tool_result（任务已启动）；
#       3) 任务真正完成后，把输出整理成 <task_notification> 通知，
#          在下一轮对话注入回给模型继续处理。
#     这样 agent 循环可以继续推进，不被慢命令阻塞——这就是"异步化"。
#
# 相比 s12 的新增内容（英文版见下方 docstring）：
#   - threading.Thread          后台执行线程
#   - background_tasks          后台任务生命周期字典（bg_id → 元信息）
#   - background_results        后台任务输出缓存（bg_id → 输出）
#   - background_lock           保护上述两个字典的线程锁
#   - should_run_background     是否走后台：模型显式要求优先
#   - is_slow_operation         模型未指定时的兜底启发式（关键词猜测）
#   - start_background_task     分发到守护线程，返回后台任务 ID
#   - collect_background_results 收集已完成任务，生成通知文本
#   - agent_loop                慢操作 → 后台 + 占位结果 + 通知注入
#   - 通知使用独立的 <task_notification> 格式，不复用 tool_use_id
#
# 注意：教学代码刻意保留最简 agent 循环以聚焦"后台任务"；
# s11 的完整错误恢复（RecoveryState、退避重试、升级、主动压缩、降级模型）被省略。
# ============================================================================
"""
s13: Background Tasks — thread-based async execution + notification injection.

Run:  python s13_background_tasks/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s12:
  - threading.Thread for background execution
  - background_tasks dict for lifecycle tracking (bg_id, command, status)
  - background_results dict + threading.Lock for thread-safe storage
  - should_run_background: model explicit request via run_in_background param
  - is_slow_operation: fallback heuristic when model doesn't specify
  - start_background_task: dispatch to daemon thread, return bg task id
  - collect_background_results: gather completed, return as notifications
  - agent_loop: slow ops → background + placeholder, inject notifications
  - Notifications use <task_notification> format, not reused tool_use_id

Note: Teaching code keeps a basic agent loop to stay focused on background
tasks. S11's full error recovery (RecoveryState, backoff, escalation,
reactive compact, fallback model) is omitted.
"""

# ── 标准库导入 ──
# os        : 读取 / 修改环境变量（API 地址、鉴权 token 等）
# subprocess: 执行 shell 命令（run_bash 的底层实现）
# json      : 任务对象 ↔ JSON 文件互转；system prompt 缓存键的序列化
# time      : 生成任务 ID 的时间戳部分
# random    : 生成任务 ID 的随机数部分（避免并发冲突）
# threading : 后台任务线程 + 保护共享字典的线程锁
import os, subprocess, json, time, random, threading
# from pathlib    : 跨平台路径对象
# from dataclasses: 数据类（定义 Task）+ asdict（dataclass → dict）
from pathlib import Path
from dataclasses import dataclass, asdict

try:
# 交互式命令行增强：装上 readline 后 input() 支持方向键、行内编辑与历史记录；
# 这里关掉 'set bind-tty-special-chars off'，避免 Ctrl+C 等特殊键行为异常。
# 某些环境（如 Windows）没有 readline，导入失败就静默跳过，不影响主流程。
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

# 第三方库：Anthropic 官方 SDK + python-dotenv（从 .env 文件加载配置）
from anthropic import Anthropic
from dotenv import load_dotenv

# ── 环境与全局配置 ──
# 加载项目根目录的 .env（override=True：覆盖进程中已有的同名环境变量，
# 保证 .env 里的最新配置生效）
load_dotenv(override=True)
# 如果配置了自定义 ANTHROPIC_BASE_URL（例如走代理网关或中转服务），
# 说明不是官方直连，官方 AUTH_TOKEN 不再适用，显式清掉避免冲突
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# WORKDIR      : 工作目录，一切相对路径的基准点
# MEMORY_DIR   : 记忆存储目录
# MEMORY_INDEX : 记忆索引文件（存在则注入 system prompt）
# client       : API 客户端
# MODEL        : 模型 ID，必须在 .env 中配置
WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# ── Task System (from s12, synced) ──

# ── 任务系统（沿用 s12 的代码，保持同步）──
# 任务以 JSON 文件形式持久化在 .tasks 目录下，一个任务一个文件，
# agent 重启后任务状态依然保留（文件系统即"数据库"）。
TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


# Task 数据类：@dataclass 自动生成 __init__ / __repr__ 等样板代码。
# 字段说明：
#   id          唯一任务 ID，如 task_1712345678_0042
#   subject     任务标题（一句话描述要做什么）
#   description 任务详情描述
#   status      三态流转：pending → in_progress → completed
#   owner       认领人（谁在执行，默认 agent）
#   blockedBy   依赖的任务 ID 列表：全部完成后本任务才能开始
@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None
    blockedBy: list[str]


# 根据任务 ID 拼出对应的 JSON 文件路径
def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


# 创建一个新任务并落盘。
# - ID 用「时间戳 + 4 位随机数」生成，保证唯一性；
# - 初始状态为 pending，owner 为空；
# - blockedBy 可为空（无依赖的任务）。
def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject, description=description,
        status="pending", owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


# 把 Task 对象序列化成 JSON 写入磁盘（asdict 把 dataclass 转 dict）
def save_task(task: Task):
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


# 从磁盘读 JSON 并还原成 Task 对象（**kwargs 把字典展开为构造参数）
def load_task(task_id: str) -> Task:
    return Task(**json.loads(_task_path(task_id).read_text()))


# 列出全部任务，按文件名排序（文件名含时间戳，即按创建时间排序）
def list_tasks() -> list[Task]:
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


# 返回单个任务的完整详情（JSON 字符串，方便直接作为工具输出喂给模型）
def get_task(task_id: str) -> str:
    """Return full task details as JSON."""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


# 检查任务的前置依赖（blockedBy）是否全部完成，决定能否开始。
# 规则：任一依赖任务不存在（文件缺失）或状态不是 completed，都视为被阻塞。
def can_start(task_id: str) -> bool:
    """Check if all blockedBy dependencies are completed.
    Missing dependencies are treated as blocked."""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


# 认领任务：pending → in_progress，并记录 owner。
# 两个前置校验：
# - 任务必须处于 pending（in_progress / completed 不能重复认领）；
# - 依赖必须全部完成（can_start），否则返回被谁阻塞。
# 成功时打印彩色日志，并把结果字符串返回给模型。
def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


# 完成任务：in_progress → completed，并顺带汇报"解锁"了哪些下游任务。
# 完成后扫描所有 pending 任务：若某任务依赖已全部完成（can_start 为 True），
# 说明它被解锁了，把标题一并告诉模型。
def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ── Prompt Assembly (from s10, synced) ──

# ── Prompt 组装（沿用 s10 的代码，保持同步）──
# 把 system prompt 拆成若干"段落"，按需拼接：
# 各段落职责清晰、可单独维护，也可按 context 动态增删。
PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


# 把固定段落（身份 / 工具 / 工作目录）拼起来；
# 若 context 里有记忆内容则追加「记忆」段落。
def assemble_system_prompt(context: dict) -> str:
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


# 缓存：记录上一次的 context 键和生成结果，避免同一 context 重复拼接
_last_context_key, _last_prompt = None, None


# 带缓存的 system prompt 获取函数：
# 用 json.dumps 把 context 序列化成可哈希的字符串当键；
# 与上次相同就直接返回缓存，否则重新组装并更新缓存。
# 省掉 agent 循环里每次重复拼接的损耗。
def get_system_prompt(context: dict) -> str:
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ── Tools ──

# 路径安全校验：把相对路径拼到 WORKDIR 下并解析成绝对路径，
# 若解析结果逃出 WORKDIR（如 ../ 或绝对路径指向外部），直接抛异常。
# 这是 agent 场景的关键防线——模型不可信，不能让它随便读写工作目录之外的文件。
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# 执行 shell 命令并返回输出。
# 注意：run_in_background 参数在这里【不处理】——是否后台运行由
# agent_loop 在分发时决定（见 start_background_task），本函数只负责同步执行。
# - timeout=120：命令超 120 秒强制终止，防止挂死；
# - 输出合并 stdout+stderr，截断到 50000 字符，避免撑爆上下文。
def run_bash(command: str, run_in_background: bool = False) -> str:
    # run_in_background is handled by agent_loop dispatch, not here
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


# 读取文件内容。limit 可选：只返回前 limit 行，并在末尾标注省略了多少行，
# 防止一次性读入超大文件。
def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


# 写入文件。父目录不存在时自动创建（parents=True, exist_ok=True）。
def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# Task tools

# 任务类工具的"包装函数"：把核心函数结果加上彩色日志 / 错误兜底，
# 统一返回字符串，作为 tool_result 的内容喂回给模型。
def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


# 列出任务，用 ○ / ● / ✓ 图标直观区分状态
def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●",
                "completed": "✓"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)


# 查询单个任务；文件不存在时返回友好错误而不是抛异常
def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


# ── 工具清单（Tool Schema）──
# 这是喂给 API 的 JSON Schema 描述：告诉模型有哪些工具、参数长什么样。
# 模型只会按这里的描述生成 tool_use 请求。
TOOLS = [
# bash 是唯一带 run_in_background 布尔参数的工具：后台任务的入口标记
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {
                          "command": {"type": "string"},
                          "run_in_background": {"type": "boolean"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "create_task",
     "description": "Create a new task with optional blockedBy dependencies.",
     "input_schema": {"type": "object",
                      "properties": {
                          "subject": {"type": "string"},
                          "description": {"type": "string"},
                          "blockedBy": {"type": "array",
                                        "items": {"type": "string"}}},
                      "required": ["subject"]}},
    {"name": "list_tasks",
     "description": "List all tasks with status, owner, and dependencies.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    {"name": "get_task",
     "description": "Get full details of a specific task by ID.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task",
     "description": "Claim a pending task. Sets owner, changes status to in_progress.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task",
     "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
]

# 工具名 → 处理函数映射表：execute_tool 靠它做统一分发
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task, "claim_task": run_claim_task,
    "complete_task": run_complete_task,
}


# ── Background Tasks (s13 new) ──

# ── 后台任务（s13 新增的核心部分）──
# 三个全局状态 + 一把锁：
#   _bg_counter        后台任务自增计数器，用于生成唯一 bg_id
#   background_tasks   生命周期字典：bg_id → {tool_use_id, command, status}
#   background_results 输出缓存：bg_id → 最终输出字符串
#   background_lock    线程锁：后台线程与主线程都会读写上述两个字典，
#                      必须加锁避免并发读写导致数据损坏 / 脏读
_bg_counter = 0
background_tasks: dict[str, dict] = {}   # bg_id → {tool_use_id, command, status}
background_results: dict[str, str] = {}   # bg_id → output
background_lock = threading.Lock()


# 兜底启发式：从命令文本里猜它是不是"慢操作"（预计超过 30 秒）。
# 规则很简单——只对 bash 生效，命令里出现 install / build / test /
# deploy / compile 等关键词就认为是慢操作。
# 关键词命中是"可能慢"，宁可多后台化也不阻塞主循环。
def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """Fallback heuristic: commands likely to take > 30s."""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)


# 判断这个工具调用要不要进后台。
# 优先级：模型显式传了 run_in_background=True → 听模型的；
# 没传 → 退回启发式判断（is_slow_operation）。
# 这就是"模型显式意图优先、启发式兜底"的双保险设计。
def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """Model explicit request takes priority; fallback to heuristic."""
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)


# 按工具名从 TOOL_HANDLERS 找到处理函数并同步执行，返回输出字符串。
# block 是 API 返回的 tool_use 块：block.name 是工具名，
# block.input 是参数字典（** 展开传给处理函数）。
def execute_tool(block) -> str:
    """Execute a tool call block, return output."""
    handler = TOOL_HANDLERS.get(block.name)
    if handler:
        return handler(**block.input)
    return f"Unknown tool: {block.name}"


# 把工具调用放到守护线程里异步执行，立即返回后台任务 ID。
# 流程：
#   1) 计数器 +1，生成 bg_id（如 bg_0001）；
#   2) 先在 background_tasks 里登记状态为 running（此时拿到锁）；
#   3) 启动 daemon 线程执行 worker：真正的工具调用跑完后，
#      加锁把状态改成 completed 并把输出写进 background_results；
#   4) 主线程不等待，直接返回 bg_id。
# daemon=True 的意义：主程序退出时后台线程自动结束，不会残留线程挂住进程。
def start_background_task(block) -> str:
    """Run tool in a daemon thread. Returns background task ID."""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = block.input.get("command", block.name)

    def worker():
        result = execute_tool(block)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


# 收集所有已完成的后台任务，生成 <task_notification> 通知列表。
# 注意这里用 pop（取出即删除）：同一结果只通知一次，避免下轮重复注入。
# 输出截断到 200 字符作为 summary，防止通知文本过大占用上下文。
# <task_notification> 是独立消息格式（普通 text 块），而非复用 tool_result——
# 因为 tool_result 必须对应具体 tool_use_id，而后台任务的结果与原始
# tool_use 早已"分离"了。
def collect_background_results() -> list[str]:
    """Collect completed background results as task_notification messages."""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        print(f"  \033[32m[background done] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    return notifications


# ── Context ──

# 根据真实状态推导 context（system prompt 的数据来源）。
# 目前主要从 .memory/MEMORY.md 读取记忆内容注入 prompt；
# messages 参数本课未实际使用，保留是为了接口一致性。
def update_context(context: dict, messages: list) -> dict:
    """Derive context from real state."""
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ── Agent Loop (simplified, focused on background tasks) ──

# ── Agent 主循环（简版，聚焦后台任务）──
# 核心模式：
#   while True:
#       response = model.invoke(messages, tools)     # 1. 模型出招
#       处理每个 tool_use（同步执行 或 后台分发）      # 2. 执行工具
#       注入 tool_result + 后台通知 → 回到 1          # 3. 结果回喂
#   直到模型不再请求工具（stop_reason != "tool_use"）才结束本轮。
def agent_loop(messages: list, context: dict):
    # 先根据 context 组装（带缓存的）system prompt
    system = get_system_prompt(context)
    while True:
        try:
            # 1) 调用模型：把历史消息 + 工具清单一起发给 API
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        # 2) 把模型这次的完整回复追加进历史（含 tool_use 块与文本）
        messages.append({"role": "assistant", "content": response.content})
        # 3) 若模型没有请求任何工具，说明已给出最终回答，本轮结束
        if response.stop_reason != "tool_use":
            return

        # 4) 逐个处理模型请求的工具调用
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            # 判断是否走后台：模型显式要求 / 慢操作启发式
            if should_run_background(block.name, block.input):
                # 后台路径：立即返回"占位"结果，真正的执行在线程里进行
                bg_id = start_background_task(block)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"[Background task {bg_id} started] "
                                           f"Command: {block.input.get('command', '')}. "
                                           f"Result will be available when complete."})
            else:
                # 同步路径：直接执行并拿到真实输出
                output = execute_tool(block)
                print(str(output)[:300])
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})

        # 5) 把工具结果 + 后台完成通知合并成一条 user 消息注入，
        #    让模型在同一轮里既能拿到工具输出，也能看到后台任务进展
        # Inject tool results + background notifications in one user message
        user_content = list(results)
        bg_notifications = collect_background_results()
        if bg_notifications:
            for notif in bg_notifications:
                user_content.append({"type": "text", "text": notif})
            print(f"  \033[32m[inject] {len(bg_notifications)} background "
                  f"notification(s)\033[0m")
        messages.append({"role": "user", "content": user_content})
        # 6) 消息变长，context 可能变化（如记忆被更新）：
        #    重新推导 context 并刷新 system prompt，进入下一轮循环
        context = update_context(context, messages)
        system = get_system_prompt(context)


# ── 入口：交互式 REPL ──
if __name__ == "__main__":
    print("s13: background tasks")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    # history：整个会话的消息历史；context：初始 context
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms13 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 让 agent 循环处理这一问（内部会执行工具并回写 history）
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        # 一轮结束后刷新 context，并打印模型最后一段文本回复
        context = update_context(context, history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""))
        print()
