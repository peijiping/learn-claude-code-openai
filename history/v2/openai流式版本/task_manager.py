#!/usr/bin/env python3
"""
task_manager.py - 任务管理模块

任务管理模块负责任务的创建、查询、更新、删除等操作。
支持任务依赖关系管理，任务状态包括：pending（待处理）、in_progress（进行中）、completed（已完成）。
"""

import json
import time, random
from pathlib import Path
from dataclasses import dataclass, asdict

from paths import TASKS_DIR


@dataclass
class Task:
    # 任务的唯一标识符,格式: task_{时间戳}_{4位随机数}
    id: str
    # 任务标题(简短描述,用于列表展示)
    subject: str
    # 任务详细描述(可包含具体执行要求、验收标准等)
    description: str
    # 任务状态机: pending(待处理) | in_progress(进行中) | completed(已完成)
    status: str
    # 任务认领者,多 agent 场景下记录是哪个 agent 在负责;None 表示尚未认领
    owner: str | None
    # 依赖任务 ID 列表:所有列出的任务必须 completed 后,本任务才能开始
    # 注意:缺失的依赖(即 ID 不存在)也会被当作阻塞,防止悬空引用
    blockedBy: list[str]


# -- TaskManager: 支持依赖关系图的CRUD操作，数据持久化为JSON文件 --
class TaskManager:
    """
    任务管理器类
    
    提供任务的增删改查功能，支持任务之间的依赖关系管理。
    每个任务以独立的JSON文件形式存储在指定目录中。
    """
    
    def __init__(self, tasks_dir: Path | None = None):
        """
        初始化任务管理器
        
        Args:
            tasks_dir: 任务数据存储目录的路径,不传则默认使用 TASKS_DIR
        """
        self.task_dir = tasks_dir if tasks_dir else TASKS_DIR  # 任务文件存储目录
        self.task_dir.mkdir(exist_ok=True)  # 如果目录不存在则创建
        # 作用域：会话内任务板用它把任务限定在某个会话（如 "session_3" / "cron_1"）。
        # None 表示旧的全局看板（无会话上下文，向后兼容）。
        self.scope: str | None = None

    def set_scope(self, scope: str | None) -> None:
        """
        设置作用域。作用域为 None 时是全局看板；非空时任务只在本会话内可见/可操作。

        由 Agent 在切换会话时调用（与 todo 的 set_todo_manager 平行），
        scope 值取 f"{session_prefix}{session_num}"，如 "session_3"。
        """
        self.scope = scope

    def _task_path(self, task_id: str) -> Path:
        """根据 task_id 返回对应的 JSON 文件路径(私有内部工具函数)。"""
        return self.task_dir / f"{task_id}.json"

    def _scope_prefix(self) -> str:
        """
        返回当前作用域的文件名前缀，用于任务 ID 与列表过滤。
        scope 非空 → "task_{scope}_"（如 "task_session_3_"）；None → 旧全局命名 "task_"。
        """
        return f"task_{self.scope}_" if self.scope else "task_"

    def _create_task(self, subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> Task:
        """
        创建一个新任务。

        - 自动生成全局唯一 ID(时间戳 + 随机数后缀,降低冲突概率)
        - ID 前缀编码所属作用域（如 task_session_3_{ts}_{rand}）在文件名中，
          实现"用文件名标识与 session 的关系"
        - 初始状态为 pending,owner 为 None(尚未被认领)
        - 立即落盘到 .tasks/{id}.json,确保创建即持久
        - 可选 blockedBy 用于声明对其他任务的依赖(实现 DAG 编排)
        """
        task = Task(
            id=f"{self._scope_prefix()}{int(time.time())}_{random.randint(0, 9999):04d}",
            subject=subject,
            description=description,
            status="pending",
            owner=None,
            blockedBy=blockedBy or [],
        )
        self._save_task(task)
        return task


    def _save_task(self, task: Task):
        """将 Task 对象序列化为 JSON 并覆盖写入对应文件(每次状态变更都要调用)。"""
        self._task_path(task.id).write_text(json.dumps(asdict(task), indent=2, ensure_ascii=False), encoding="utf-8")


    def _load_task(self, task_id: str) -> Task:
        """从 JSON 文件加载并反序列化为 Task 对象;文件不存在时会抛 FileNotFoundError。"""
        return Task(**json.loads(self._task_path(task_id).read_text()))


    def _list_tasks(self) -> list[Task]:
        """
        列出当前作用域下所有任务并按文件名字典序返回(等价于按时间排序)。

        作用域过滤：scope 非空时只列出文件名带该会话前缀的任务；
        scope 为 None 时列出全部（旧全局看板，向后兼容）。
        """
        return [Task(**json.loads(p.read_text()))
                for p in sorted(self.task_dir.glob(f"{self._scope_prefix()}*.json"))]


    def _get_task(self, task_id: str) -> str:
        """返回任务的完整 JSON 详情字符串,供模型查看完整上下文。"""
        task = self._load_task(task_id)
        return json.dumps(asdict(task), indent=2, ensure_ascii=False)


    def _can_start(self, task_id: str) -> bool:
        """
        判断指定任务是否可以开始。

        判定规则:
        1) 遍历 task.blockedBy 列表中的每个依赖 ID
        2) 若依赖文件不存在(被删除/拼写错误) → 视为阻塞(返回 False)
            这样可以防止 agent 引用悬空 ID 时误执行
        3) 若依赖存在但 status != "completed" → 阻塞
        4) 所有依赖都 completed 才返回 True
        """
        task = self._load_task(task_id)
        for dep_id in task.blockedBy:
            if not self._task_path(dep_id).exists():
                return False
            if self._load_task(dep_id).status != "completed":
                return False
        return True


    def _claim_task(self, task_id: str, owner: str = "agent") -> str:
        """
        认领一个 pending 任务:把任务从 pending 推进到 in_progress。

        流程:
        1) 重新加载任务,获取最新状态(避免基于陈旧数据决策)
        2) 状态必须是 pending,否则拒绝(已认领或已完成的任务不能再认领)
        3) 调用 can_start 检查依赖;若仍被阻塞,返回具体阻塞原因(哪些依赖未完成)
        4) 通过校验后:设置 owner 字段,状态置为 in_progress,立即落盘
        5) 在终端打印蓝色日志,方便实时观察 agent 行为
        """
        task = self._load_task(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if not self._can_start(task_id):
            # 收集所有未满足的依赖 ID,精确告知调用方卡在哪里
            deps = [d for d in task.blockedBy
                    if not self._task_path(d).exists() or self._load_task(d).status != "completed"]
            return f"Blocked by: {deps}"
        task.owner = owner
        task.status = "in_progress"
        self._save_task(task)
        print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
        return f"Claimed {task.id} ({task.subject})"


    def _complete_task(self, task_id: str) -> str:
        """
        将 in_progress 任务标记为 completed。

        关键副作用(重要!):
        - 完成后会扫描所有 pending 任务,找出"因为本次完成而新解锁"的下游任务
        - 即:该任务的 ID 出现在它们的 blockedBy 列表中、且其他依赖也已完成的任务
        - 打印黄色 [unblocked] 日志,提醒 agent 优先调度这些可执行任务
        - 这种"完成即触发依赖检查"的模式是 DAG 调度器的核心机制
        """
        task = self._load_task(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        task.status = "completed"
        self._save_task(task)
        # 找出所有因为本次完成而新解锁的待办任务
        unblocked = [t.subject for t in self._list_tasks()
                    if t.status == "pending" and t.blockedBy and self._can_start(t.id)]
        print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
        msg = f"Completed {task.id} ({task.subject})"
        if unblocked:
            msg += f"\nUnblocked: {', '.join(unblocked)}"
            print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
        # 会话内任务板：若本会话任务已全部完成，自动清理该会话的任务文件
        self._gc_scoped_tasks()
        return msg

    def _gc_scoped_tasks(self) -> int:
        """
        会话内任务板的自动清理：仅当 scope 非空、且当前作用域下存在任务、
        并且全部任务都已 completed 时，删除本会话的全部任务文件，返回删除数。

        规则：
        - scope 为 None 的全局看板不触发清理，避免误删遗留数据。
        - 只要还有 pending / in_progress 任务，就不清理（仍然有活要干）。
        - "最后一个任务完成"即满足条件，整个过程让本会话任务板快速归零。
        """
        if not self.scope:
            return 0
        files = sorted(self.task_dir.glob(f"{self._scope_prefix()}*.json"))
        if not files:
            return 0
        if any(t.status != "completed" for t in self._list_tasks()):
            return 0
        for p in files:
            p.unlink(missing_ok=True)
        print(f"  \033[33m[gc] session '{self.scope}' 任务已全部完成，清理 {len(files)} 个任务文件\033[0m")
        return len(files)

    # ── Task tools (面向模型工具调用的薄包装层) ──
    # 这些 run_* 函数是核心业务函数(create_task / list_tasks 等)与 LLM 工具调用之间的桥梁。
    # 主要职责:
    #   1. 参数透传到业务函数
    #   2. 用 ANSI 颜色在终端打印执行日志,方便观察 agent 行为
    #   3. 决定返回给模型的字符串格式(简洁、便于模型解析)

    def run_create_task(self, subject: str, description: str = "",
                        blockedBy: list[str] | None = None) -> str:
        """
        工具入口:创建任务。打印蓝色 [create] 日志,返回任务 ID 与依赖信息。
        """
        task = self._create_task(subject, description, blockedBy)
        deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
        print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
        return f"Created {task.id}: {task.subject}{deps}"


    def run_list_tasks(self) -> str:
        """
        工具入口:列出所有任务,带状态图标和依赖概览。
        图标约定: ○ pending / ● in_progress / ✓ completed
        """
        tasks = self._list_tasks()
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


    def run_get_task(self, task_id: str) -> str:
        """工具入口:获取任务完整 JSON 详情;找不到时返回友好错误而非抛异常。"""
        try:
            return self._get_task(task_id)
        except FileNotFoundError:
            return f"Error: Task {task_id} not found"


    def run_claim_task(self, task_id: str) -> str:
        """工具入口:认领任务(默认 owner=agent)。业务逻辑在 claim_task 内。"""
        return self._claim_task(task_id, owner="agent")


    def run_complete_task(self, task_id: str) -> str:
        """工具入口:完成任务;若解锁了下游任务,会附带 unblocked 提示。"""
        return self._complete_task(task_id)
