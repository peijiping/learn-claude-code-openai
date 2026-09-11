#!/usr/bin/env python3
"""
s14: Cron Scheduler — independent daemon thread + queue processor.

Run:  python s14_cron_scheduler/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s13:
  - CronJob dataclass (id, cron, prompt, recurring, durable)
  - cron_matches: 5-field cron expression matching with DOM/DOW OR semantics
  - schedule_job / cancel_job: register/remove cron jobs (with validation)
  - cron_scheduler_loop: independent daemon thread, polls every 1s
  - cron_queue: thread-safe queue, scheduler writes, queue processor delivers
  - queue_processor_loop: auto-runs agent_loop when cron_queue has work
  - Durable storage: .scheduled_tasks.json (survives restart)
  - 3 new tools: schedule_cron, list_crons, cancel_cron

Four layers:
  1. Scheduler: daemon thread checks time → fires matching jobs
  2. Queue: cron_queue decouples scheduler from agent loop
  3. Queue processor: wakes the agent when queued work exists and it is idle
  4. Consumer: agent_loop consumes queued jobs and injects them into messages
"""

import os, subprocess, json, time, random, threading
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# ── Task System (from s12, synced) ──

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None
    blockedBy: list[str]


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


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


def save_task(task: Task):
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
    return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """Return full task details as JSON."""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


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

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "schedule_cron, list_crons, cancel_cron.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


_last_context_key, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ── Tools ──

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, run_in_background: bool = False) -> str:
    # run_in_background is handled by agent_loop dispatch, not here
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# Task tools

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


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


def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


# ── Background Tasks (from s13, synced) ──

_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """Fallback heuristic: commands likely to take > 30s."""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """Model explicit request takes priority; fallback to heuristic."""
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)


def execute_tool(block) -> str:
    """Execute a tool call block, return output."""
    handler = {
        "bash": run_bash, "read_file": run_read, "write_file": run_write,
        "create_task": run_create_task, "list_tasks": run_list_tasks,
        "get_task": run_get_task, "claim_task": run_claim_task,
        "complete_task": run_complete_task,
        "schedule_cron": run_schedule_cron, "list_crons": run_list_crons,
        "cancel_cron": run_cancel_cron,
    }.get(block.name)
    if handler:
        return handler(**block.input)
    return f"Unknown tool: {block.name}"


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
    threading.Thread(target=worker, daemon=True).start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


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


# ── Cron Scheduler (s14 new) ──
# ============================================================================
# 本节实现 s14 课程的核心：定时任务（Cron）调度器。
#
# 整体采用「生产者-队列-消费者」三层解耦的架构：
#   1. 调度线程（生产者）：cron_scheduler_loop，独立 daemon 线程
#      - 每 1 秒轮询一次当前时间
#      - 用 cron_matches 判断每个注册任务是否到期
#      - 命中后将任务投入 cron_queue
#   2. 任务队列（缓冲）：cron_queue，线程安全列表
#      - 解耦调度与执行：调度线程不直接驱动 agent
#      - 用 cron_lock 保护，跨线程访问
#   3. 队列处理器（消费者）：queue_processor_loop，独立 daemon 线程
#      - 监控 cron_queue 是否非空
#      - 拿到 agent_lock 后调用 agent_loop 消费任务
#      - 把任务 prompt 注入到 messages 列表
#
# 之所以要拆成调度线程 + 队列 + 队列处理器三块：
#   - 调度线程不能直接执行 agent（agent_loop 是阻塞的，否则调度会卡住）
#   - 不能让调度线程和用户输入「抢」agent，用队列串行化
#   - 队列处理器用 agent_lock（不可重入 mutex）保证 agent_loop 同一时刻只跑一次
# ============================================================================

# 持久化路径：所有 durable=True 的任务会被序列化到该文件，重启后自动恢复
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"


@dataclass
class CronJob:
    # 任务唯一 ID，格式如 cron_123456（6 位随机数）
    id: str
    # 5 段 cron 表达式："分 时 日 月 周"，例如 "0 9 * * *" 表示每天 9:00
    cron: str        # "0 9 * * *"
    # 触发时注入到 agent 上下文的提示词（作为 user message 追加）
    prompt: str      # message to inject when fired
    # True=周期性任务（命中后保留在 scheduled_jobs 中）
    # False=一次性任务（命中后从 scheduled_jobs 中移除）
    recurring: bool  # True = recurring, False = one-shot
    # True=持久化到 .scheduled_tasks.json，重启后仍生效
    # False=仅本进程内有效，进程退出后丢失
    durable: bool    # True = persist to disk


# 已注册任务表：job_id -> CronJob。所有跨线程访问都要拿 cron_lock
scheduled_jobs: dict[str, CronJob] = {}
# 触发队列：调度线程写入，agent_loop 消费。线程安全列表
cron_queue: list[CronJob] = []
# 保护 scheduled_jobs / cron_queue / _last_fired 的互斥锁
cron_lock = threading.Lock()
# 保护 agent_loop 串行化执行：同一时刻只能有一个 agent turn 在跑
# - 用户输入主循环在持有它时调用 run_agent_turn_locked
# - 队列处理器用 blocking=False 尝试获取，抢不到就跳过本次轮询
agent_lock = threading.Lock()
# 防重放记录：job_id -> "YYYY-MM-DD HH:MM"，同一分钟内不会重复触发同一任务
_last_fired: dict[str, str] = {}  # job_id → "YYYY-MM-DD HH:MM"


def _cron_field_matches(field: str, value: int) -> bool:
    """匹配单个 cron 字段（如 "*/5" / "1,3,5" / "9-17" / "30"）与具体数值。"""
    # 通配符：任意值都匹配
    if field == "*":
        return True
    # 步长语法：*/n 表示每隔 n 命中一次（例如 */5 在分钟字段上 = 0,5,10,...,55）
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    # 枚举语法：用逗号分隔多个子表达式（每个子表达式可递归匹配）
    if "," in field:
        return any(_cron_field_matches(f.strip(), value)
                   for f in field.split(","))
    # 区间语法：a-b 表示闭区间 [a, b]
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    # 默认按精确值匹配
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """判断 5 段 cron 表达式是否匹配给定时间。
    采用标准 cron 语义：DOM（日）和 DOW（周）同时被约束时取「或」逻辑。"""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    # Python 的 weekday() 是 Monday=0, cron 的 dow 是 Sunday=0，统一到 cron 习惯
    dow_val = (dt.weekday() + 1) % 7  # Python Monday=0 → cron Sunday=0

    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)

    # 分、时、月必须全部命中（这三者是「与」关系）
    if not (m and h and month_ok):
        return False
    # DOM 和 DOW 特殊处理：两者都是「通配」则直接通过
    dom_unconstrained = dom == "*"
    dow_unconstrained = dow == "*"
    if dom_unconstrained and dow_unconstrained:
        return True
    # 只约束 DOW（如 "0 9 * * 1"）：按星期匹配
    if dom_unconstrained:
        return dow_ok
    # 只约束 DOM（如 "0 9 1 * *"）：按日期匹配
    if dow_unconstrained:
        return dom_ok
    # 两者都约束（如 "0 9 1 * 1"）：DOM 或 DOW 命中其一即可（OR 语义）
    return dom_ok or dow_ok


def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    """校验单个 cron 字段值是否落在 [lo, hi] 范围内，返回错误信息或 None。"""
    if field == "*":
        return None
    # */n：步长必须为正整数
    if field.startswith("*/"):
        step_str = field[2:]
        if not step_str.isdigit():
            return f"Invalid step: {field}"
        step = int(step_str)
        if step <= 0:
            return f"Step must be > 0: {field}"
        return None
    # a,b,c：逐段校验
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err: return err
        return None
    # a-b：闭区间，且要求 lo <= hi、端点都在合法范围内
    if "-" in field:
        parts = field.split("-", 1)
        if not parts[0].isdigit() or not parts[1].isdigit():
            return f"Invalid range: {field}"
        a, b = int(parts[0]), int(parts[1])
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    # 单值：必须为合法范围内的整数
    if not field.isdigit():
        return f"Invalid field: {field}"
    val = int(field)
    if val < lo or val > hi:
        return f"Value {val} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    """校验完整 cron 表达式。各字段合法范围：
        minute: 0-59, hour: 0-23, day-of-month: 1-31, month: 1-12, day-of-week: 0-6（周日=0）"""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for i, (field, (lo, hi), name) in enumerate(zip(fields, bounds, names)):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


def save_durable_jobs():
    """把所有 durable=True 的任务持久化到 .scheduled_tasks.json。
    session-only 的任务（durable=False）不会被保存。"""
    durable = [asdict(j) for j in scheduled_jobs.values() if j.durable]
    DURABLE_PATH.write_text(json.dumps(durable, indent=2))


def load_durable_jobs():
    """进程启动时从 .scheduled_tasks.json 恢复任务。
    恢复时再次用 validate_cron 校验，避免历史脏数据让调度线程崩溃。"""
    if not DURABLE_PATH.exists():
        return
    try:
        jobs = json.loads(DURABLE_PATH.read_text())
        for j in jobs:
            job = CronJob(**j)
            err = validate_cron(job.cron)
            if err:
                print(f"  \033[31m[cron] skipping invalid job {job.id}: {err}\033[0m")
                continue
            scheduled_jobs[job.id] = job
        valid = [j for j in jobs if j["id"] in scheduled_jobs]
        if valid:
            print(f"  \033[35m[cron] loaded {len(valid)} durable job(s)\033[0m")
    except Exception:
        pass


def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
    """注册一条新的 cron 任务。成功返回 CronJob，失败返回错误字符串。"""
    # 先校验表达式，校验失败直接返回错误，不修改任何状态
    err = validate_cron(cron)
    if err:
        return err
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable,
    )
    # 写共享字典必须持锁，避免与调度线程的遍历产生竞争
    with cron_lock:
        scheduled_jobs[job.id] = job
    # 持久化任务才需要立即落盘
    if durable:
        save_durable_jobs()
    print(f"  \033[35m[cron register] {job.id} '{cron}' → {prompt[:40]}\033[0m")
    return job


def cancel_job(job_id: str) -> str:
    """取消一条 cron 任务。若该任务是 durable 的，会同步从磁盘删除。"""
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
    if not job:
        return f"Job {job_id} not found"
    if job.durable:
        save_durable_jobs()
    print(f"  \033[31m[cron cancel] {job_id}\033[0m")
    return f"Cancelled {job_id}"


def cron_scheduler_loop():
    """调度线程主体（独立 daemon 线程）。
    每 1 秒检查一次：遍历所有任务，时间匹配则入队。
    try/except 包住单个任务的处理，保证「一个坏任务不能拖垮整个调度器」。"""
    while True:
        time.sleep(1)
        now = datetime.now()
        # 用「年-月-日 时:分」作为去重 marker：
        # 同一分钟内不会重复触发同一任务（解决「每分钟匹配」任务的重复入队问题）
        minute_marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            for job in list(scheduled_jobs.values()):  # 用 list() 复制一份避免迭代中修改
                try:
                    if cron_matches(job.cron, now):
                        # 用 _last_fired 防止同一分钟内被重复入队
                        if _last_fired.get(job.id) != minute_marker:
                            cron_queue.append(job)
                            _last_fired[job.id] = minute_marker
                            print(f"  \033[35m[cron fire] {job.id} → "
                                  f"{job.prompt[:40]}\033[0m")
                        # 一次性任务触发后立刻从注册表移除
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")


def consume_cron_queue() -> list[CronJob]:
    """消费者调用：一次性把队列里所有待处理任务取出并清空队列。
    在 agent_loop 每轮迭代开头被调用，把 cron 任务作为 user message 注入。"""
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


def has_cron_queue() -> bool:
    """队列处理器调用：判断是否有待处理任务。
    用来决定是否要唤醒 agent（避免无意义地争抢 agent_lock）。"""
    with cron_lock:
        return bool(cron_queue)


# 进程启动序列（顺序很关键）：
#   1) 先从 .scheduled_tasks.json 恢复历史 durable 任务
#   2) 再启动调度线程（daemon=True：主进程退出时自动结束，无需手动 join）
# 注意：必须先恢复任务再启动线程，否则启动瞬间的那 1 秒可能错过本应触发的任务
load_durable_jobs()
threading.Thread(target=cron_scheduler_loop, daemon=True).start()
print("  \033[35m[cron] scheduler thread started\033[0m")


# ── Cron Tools ──
# 以下三个函数是「工具层」的薄包装：
#   - 把 tool schema 里的强类型参数透传给底层 schedule_job / cancel_job
#   - 把 CronJob 对象格式化成模型可读的字符串（错误则带上 "Error:" 前缀便于模型识别）


def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    # schedule_job 在校验失败时返回错误字符串，成功时返回 CronJob
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: '{cron}' → {prompt}"


def run_list_crons() -> str:
    # 列表是只读快照，但访问 scheduled_jobs 仍要持锁（防与调度线程的遍历冲突）
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs. Use schedule_cron to add one."
    lines = []
    for j in jobs:
        tag = "recurring" if j.recurring else "one-shot"
        dur = "durable" if j.durable else "session"
        lines.append(f"  {j.id}: '{j.cron}' → {j.prompt[:40]} "
                     f"[{tag}, {dur}]")
    return "\n".join(lines)


def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)


# ── Tool Definitions ──

TOOLS = [
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
    # 注册一条 cron 任务。cron 为 5 段表达式（分 时 日 月 周），
    # prompt 是触发时要注入到 agent 上下文的提示词
    {"name": "schedule_cron",
     "description": "Schedule a cron job. cron is 5-field: min hour dom month dow.",
     "input_schema": {"type": "object",
                      "properties": {
                          "cron": {"type": "string",
                                   "description": "5-field cron expression"},
                          "prompt": {"type": "string",
                                     "description": "Message to inject when fired"},
                          "recurring": {"type": "boolean",
                                        "description": "True=recurring, False=one-shot"},
                          "durable": {"type": "boolean",
                                      "description": "True=persist to disk"}},
                      "required": ["cron", "prompt"]}},
    # 列出当前所有已注册的 cron 任务（durable + session-only）
    {"name": "list_crons",
     "description": "List all registered cron jobs.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    # 按 job_id 取消一条 cron 任务（durable 的会同步从磁盘清除）
    {"name": "cancel_cron",
     "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
]


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    """Derive context from real state."""
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": [t["name"] for t in TOOLS],
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ── Agent Loop (simplified, focused on cron scheduler) ──
# Teaching code keeps a basic agent loop. S11's full error recovery is omitted.
# cron_scheduler_loop produces work; queue_processor_loop wakes this loop when
# queued work exists and no other agent turn is running.

def agent_loop(messages: list, context: dict) -> dict:
    system = get_system_prompt(context)
    while True:
        # 第 4 层（消费者）：从 cron_queue 拉取本轮触发的任务，作为 user message 追加进对话。
        # 注入位置选择循环开头：保证本次模型调用能「看到」这些任务，从而给出相应反应。
        fired = consume_cron_queue()
        for job in fired:
            # "[Scheduled]" 前缀让模型能区分「用户输入」和「定时任务触发」两种来源
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": f"[Error] {type(e).__name__}: {e}"}]})
            return context

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return context

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"[Background task {bg_id} started] "
                                           f"Result will be available when complete."})
            else:
                output = execute_tool(block)
                print(str(output)[:300])
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})

        # Merge background tool results + notifications into one user message
        user_content = list(results)
        bg_notifications = collect_background_results()
        if bg_notifications:
            for notif in bg_notifications:
                user_content.append({"type": "text", "text": notif})
        messages.append({"role": "user", "content": user_content})
        context = update_context(context, messages)
        system = get_system_prompt(context)


session_history: list = []
session_context = update_context({}, [])


def print_latest_assistant_text(messages: list):
    """Print text blocks from the latest assistant message."""
    if not messages:
        return
    msg = messages[-1]
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return
    content = msg.get("content", "")
    if isinstance(content, str):
        print(content)
        return
    for block in content:
        if getattr(block, "type", None) == "text":
            print(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            print(block.get("text", ""))


def run_agent_turn_locked(user_query: str | None = None):
    """Run one agent turn. Caller must hold agent_lock."""
    global session_context
    if user_query is not None:
        session_history.append({"role": "user", "content": user_query})
    session_context = agent_loop(session_history, session_context)
    session_context = update_context(session_context, session_history)
    print_latest_assistant_text(session_history)
    print()


def queue_processor_loop():
    """队列处理器（独立 daemon 线程）。
    负责「在 agent 空闲时」自动把 cron_queue 里的任务喂给 agent_loop。

    工作循环：
      - 每 0.2 秒检查一次
      - 队列为空就跳过
      - 用 agent_lock.acquire(blocking=False) 非阻塞抢锁：
        · 抢到 → 自己是独占运行者，可以安全跑 agent
        · 没抢到 → 说明用户正在输入，让出本轮
      - 拿到锁后再次检查队列（防止锁竞争期间队列被其他线程消费）"""
    global session_context
    while True:
        time.sleep(0.2)
        if not has_cron_queue():
            continue
        # 非阻塞抢锁：抢不到说明用户正持有 agent_lock（用户正在输入），本轮放弃
        if not agent_lock.acquire(blocking=False):
            continue
        try:
            # 双重检查：拿到锁的瞬间队列可能已被别处处理（极端情况下发生）
            if not has_cron_queue():
                continue
            print("\n  \033[35m[queue processor] delivering scheduled work\033[0m")
            run_agent_turn_locked()
        finally:
            # 任何分支都要释放锁，否则 agent 将永久被卡死
            agent_lock.release()


if __name__ == "__main__":
    print("s14: cron scheduler")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    # 启动队列处理器线程：至此 s14 的「调度线程 + 队列 + 队列处理器」三件套全部就位
    # 注意：调度线程已在模块顶部（load_durable_jobs 之后）启动；此处只需补齐消费者侧
    threading.Thread(target=queue_processor_loop, daemon=True).start()
    print("  \033[35m[queue processor] started\033[0m")
    while True:
        try:
            query = input("\033[36ms14 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 用户输入时持锁，独占 agent；队列处理器会因抢不到锁而自动让出
        with agent_lock:
            run_agent_turn_locked(query)
