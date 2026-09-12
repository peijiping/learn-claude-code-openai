#!/usr/bin/env python3

"""
tools.py - 工具注册中心（ToolRegistry）

合并自原 tool_base.py + tools.py，全部工具能力收拢为一个类：

- 基础工具方法（run_bash / run_read / run_write ...）→ 实例方法
- 工具定义（base_tools / tools / main_agent_tools）→ 懒加载属性
- 工具处理器（handlers）→ 懒加载属性，方法名到调用方的统一映射
- 统一执行入口 execute(name, **args)
- 依赖注入（skills / memory / task_manager / bus）+ holder 模式（background / todo）

路径常量统一从 paths.py 导入，不再在此模块内声明（见 AGENTS.md 路径规则）。

⚠️ 不再提供全局单例 TOOL_REGISTRY。
由调用方（Agent 等）显式实例化 ToolRegistry()，保证多实例隔离。
每个 Agent 实例拥有独立的 ToolRegistry / todo holder / background holder。
"""

import os
from pathlib import Path
import subprocess
import glob as g

from paths import (
    WORKDIR,
    SKILLS_DIR,
    INBOX_DIR,
    TODO_DIR,
    MEMORY_DIR,
    todo_file_for_session,
)
from skills import SkillLoader
from todo_manager import TodoManager
from task_manager import TaskManager
from message_bus import MessageBus, VALID_MSG_TYPES
from memories import MemoryStore


# ── 团队工具名集合（s17 自主智能体）────────────────────────────────
# 默认（子智能体）模式下这些工具不暴露给 LLM；仅 `/teams` 进入团队模式
# 时才出现在喂给 LLM 的工具集里（见 default_agent_tools / main_agent_tools）。
TEAM_TOOL_NAMES = {
    "spawn_teammate", "send_message", "check_inbox",
    "request_shutdown", "request_plan", "review_plan",
}


class ToolRegistry:
    """
    统一管理所有工具：方法、定义、处理器与执行入口。

    设计要点：
    - 基础工具（bash / 文件读写 / glob）是实例方法，供本注册中心与
      其他模块（如 teammate_manager）直接调用；
    - handlers 暴露「工具名 → 可调用对象」的映射，兼容 agent 循环与
      SubAgent 以 dict 方式按名查找处理器；
    - base_tools / tools / main_agent_tools 是喂给 LLM 的 JSON Schema 定义，
      区分粒度：base=子智能体可用；tools=主智能体全部；main=主智能体+sub_agent；
    - holder 模式：background / todo 管理器由外部在运行期注入，避免反向依赖。
    """

    def __init__(
        self,
        skills: SkillLoader | None = None,
        memory: MemoryStore | None = None,
        task_manager: TaskManager | None = None,
        bus: MessageBus | None = None,
        cron_scheduler=None,
        teammate_manager=None,
    ):
        # ── 依赖注入：默认惰性构造，允许外部传入自定义实例 ──
        self.skills = skills if skills is not None else SkillLoader(SKILLS_DIR)
        self.memory = memory if memory is not None else MemoryStore(MEMORY_DIR)
        self.task_manager = task_manager if task_manager is not None else TaskManager()
        self.bus = bus if bus is not None else MessageBus(INBOX_DIR)

        # ── holder 模式：运行期注入，避免 tools.py 反向依赖 agent_full_v2 ──
        self._background_manager = None
        self._todo_manager = None
        self._cron_scheduler = cron_scheduler  # cron 调度器（holder）
        self._teammate_manager = teammate_manager  # 团队成员管理器（holder，s17）
        self._worktree_manager = None  # worktree 管理器（holder，s18）
        self._mcp_manager = None  # MCP 管理器（holder，s19）
        self._workflow_manager = None  # 工作流运行时管理器（holder，s16）

        # ── 懒加载缓存 ──
        self._handlers_cache = None
        self._base_tools_cache = None
        self._tools_cache = None
        self._main_agent_tools_cache = None
        self._default_agent_tools_cache = None

    # ═══════════════════════════════════════════════════════════
    #  holder 模式：background / todo（运行期注入）
    # ═══════════════════════════════════════════════════════════

    def set_background_manager(self, bm) -> None:
        """由 agent_full_v2.py 在启动时调用一次，挂上 BackgroundManager 实例。"""
        self._background_manager = bm

    def get_background_manager(self):
        """获取 BackgroundManager 实例；未初始化时抛错，提示调用方先 set_background_manager。"""
        mgr = self._background_manager
        if mgr is None:
            raise RuntimeError(
                "BackgroundManager 未初始化。请先调用 set_background_manager(...)。"
            )
        return mgr

    def set_teammate_manager(self, tm) -> None:
        """由 agent_full_v2.py 在启动时调用一次，挂上 TeammateManager 实例（s17）。"""
        self._teammate_manager = tm

    def get_teammate_manager(self):
        """获取 TeammateManager 实例；未初始化时抛错，提示调用方先 set_teammate_manager。"""
        mgr = self._teammate_manager
        if mgr is None:
            raise RuntimeError(
                "TeammateManager 未初始化。请先调用 set_teammate_manager(...)。"
            )
        return mgr

    def set_worktree_manager(self, wm) -> None:
        """由 agent_full_v2.py 在启动时调用一次，挂上 WorktreeManager 实例（s18）。"""
        self._worktree_manager = wm

    def get_worktree_manager(self):
        """获取 WorktreeManager 实例；未初始化时抛错，提示调用方先 set_worktree_manager。"""
        mgr = self._worktree_manager
        if mgr is None:
            raise RuntimeError(
                "WorktreeManager 未初始化。请先调用 set_worktree_manager(...)。"
            )
        return mgr

    def set_mcp_manager(self, mm) -> None:
        """由 agent_full_v2.py 在启动时调用一次，挂上 MCPManager 实例（s19）。"""
        self._mcp_manager = mm

    def get_mcp_manager(self):
        """获取 MCPManager 实例；未初始化时抛错，提示调用方先 set_mcp_manager。"""
        mgr = self._mcp_manager
        if mgr is None:
            raise RuntimeError(
                "MCPManager 未初始化。请先调用 set_mcp_manager(...)。"
            )
        return mgr

    def set_workflow_manager(self, wm) -> None:
        """由 agent_full_v2.py 在启动时调用一次，挂上 WorkflowManager 实例（s16）。"""
        self._workflow_manager = wm

    def get_workflow_manager(self):
        """获取 WorkflowManager 实例；未初始化时抛错，提示调用方先 set_workflow_manager。"""
        mgr = self._workflow_manager
        if mgr is None:
            raise RuntimeError(
                "WorkflowManager 未初始化。请先调用 set_workflow_manager(...)。"
            )
        return mgr

    def set_todo_manager(self, session_num: int) -> "TodoManager":
        """
        切换 TodoManager 到指定 session 编号对应的 todo 文件。

        调用时机：
        - 启动时 init_session 后
        - /newsession、/switchsession N、/clearsession 后

        每次调用都会重新构造 TodoManager（构造时即从磁盘 load），
        这样上一个会话的内存状态与新会话完全隔离。
        """
        TODO_DIR.mkdir(parents=True, exist_ok=True)
        todo_file = todo_file_for_session(session_num)
        self._todo_manager = TodoManager(todo_file)
        return self._todo_manager

    def get_todo_manager(self) -> "TodoManager":
        """
        获取当前会话的 TodoManager。

        未初始化（set_todo_manager 从未被调用）时抛错，提示调用方先去初始化。
        """
        mgr = self._todo_manager
        if mgr is None:
            raise RuntimeError(
                "TodoManager 未初始化。请先调用 set_todo_manager(session_num) "
                "或在启动后使用 init_session。"
            )
        return mgr

    # ═══════════════════════════════════════════════════════════
    #  基础工具实现（原 tool_base.py 的函数 → 实例方法）
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _is_binary_content(text: str) -> bool:
        """检测输出是否包含大量二进制垃圾数据"""
        if not text or len(text) < 100:
            return False
        sample = text[:2000]
        printable = sum(1 for c in sample if c.isprintable() or c in '\n\r\t')
        if (len(sample) - printable) / len(sample) > 0.3:
            return True
        binary_patterns = [
            'endobj', 'endstream', '/FontDescriptor', '/CIDToGIDMap',
            '/Type /Font', '/Subtype /CIDFont', '/BaseFont /',
            '0 obj<<', '/FontFile2', '/ToUnicode',
        ]
        pattern_hits = sum(1 for p in binary_patterns if p in sample)
        if pattern_hits >= 2:
            return True
        return False

    @staticmethod
    def _smart_truncate(text: str, max_chars: int = 10000) -> str:
        """智能截断：保留首尾，中间用省略标记替代"""
        if len(text) <= max_chars:
            return text
        head_size = max_chars // 2
        tail_size = max_chars // 4
        head = text[:head_size]
        tail = text[-tail_size:]
        return f"{head}\n\n... [输出已截断，共 {len(text)} 字符，保留首 {head_size} + 尾 {tail_size} 字符] ...\n\n{tail}"

    @staticmethod
    def safe_path(p: str, base: Path | None = None) -> Path:
        """
        验证路径是否在指定工作根内，防止路径遍历攻击
        安全机制：
        - 将相对路径与工作根（base，默认 WORKDIR）拼接后转换为绝对路径
        - 检查最终路径是否仍然在 base 内
        - 如果路径逃逸到 base 之外，抛出 ValueError
        参数：
            p: 相对路径字符串
            base: 可选，工作根目录（worktree 场景传入 worktree 路径）；None 时用 WORKDIR
        返回：
            验证通过后的绝对路径(Path对象)
        异常：
            ValueError: 当路径试图逃逸到工作根之外时抛出
                         例如：p = "../../etc/passwd" 会被拒绝
        """
        # 拼接工作根和输入路径，并解析为绝对路径
        # .resolve() 会解析符号链接并返回绝对路径
        base = base or WORKDIR
        path = (base / p).resolve()

        # is_relative_to() 检查 path 是否在 base 的子目录中
        # 如果 path 是 "/etc/passwd" 或 "../other_dir" 等外部路径，则拒绝
        if not path.is_relative_to(base):
            raise ValueError(f"Path escapes workspace: {p}")

        return path

    def run_bash(self, command: str, base: Path | None = None) -> str:
        """
        执行shell命令并返回结果
        安全特性：
        - 危险命令黑名单检查：禁止 rm -rf /, sudo, shutdown, reboot 等高危操作
        - 超时保护：命令执行超过120秒会自动终止
        - 输出截断：结果最多返回50000字符，防止内存溢出
        参数：
            command: 要执行的shell命令字符串
            base: 可选，命令的工作目录（worktree 场景传入 worktree 路径）；None 时用进程当前目录
        返回：
            命令成功：返回标准输出+标准错误的合并内容（最多50000字符）
            命令失败：返回格式 "Error: command failed with return code X\\n错误信息"
            超时：返回 "Error: Timeout (120s)"
            危险命令：返回 "Error: Dangerous command blocked"
        """
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            return "Error: Dangerous command blocked"
        try:
            r = subprocess.run(
                command,
                shell=True,
                cwd=base or os.getcwd(),
                capture_output=True,
                text=True,
                timeout=120
            )
            out = (r.stdout + r.stderr).strip()
            if self._is_binary_content(out):
                return "Error: 输出包含大量二进制数据，请使用专用工具（如 pymupdf 读取 PDF）而非 strings/cat/hexdump 等原始命令。"
            if r.returncode != 0:
                return f"Error: 命令执行失败，返回码 {r.returncode}\n{self._smart_truncate(out, 50000)}"
            return self._smart_truncate(out, 50000) if out else "(command executed successfully, no output)"
        except subprocess.TimeoutExpired:
            # 命令执行超时（超过120秒）
            return "Error: Timeout (120s)"

    def run_read(self, path: str, limit: int | None = None, base: Path | None = None) -> str:
        """
        读取文件内容
        功能特性：
        - 使用 safe_path 进行安全路径验证
        - 支持行数限制：只读取前limit行，避免大文件撑爆内存
        - 当文件被截断时，显示剩余行数提示
        - 自动截断超长内容至50000字符
        参数：
            path: 要读取的文件路径（相对路径）
            limit: 可选，限制读取的行数。默认None表示读取全部
            base: 可选，工作根目录（worktree 场景传入 worktree 路径）；None 时用 WORKDIR
        返回：
            成功：文件内容字符串（可能被截断）
            失败：格式 "Error: {异常信息}"
        """
        try:
            lines = self.safe_path(path, base).read_text().splitlines()
            if limit and limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    def run_read_pdf(self, path: str, max_pages: int = 5, chars_per_page: int = 3000, base: Path | None = None) -> str:
        """
        使用 pymupdf 安全读取 PDF 文件，分页提取文本
        功能特性：
        - 使用 safe_path 进行安全路径验证
        - 分页提取，每页限制字符数
        - 限制最大读取页数
        - 总输出截断至 30000 字符
        参数：
            path: PDF 文件路径（相对路径）
            max_pages: 最大读取页数，默认5
            chars_per_page: 每页最大字符数，默认3000
            base: 可选，工作根目录（worktree 场景传入 worktree 路径）；None 时用 WORKDIR
        返回：
            成功：PDF 文本内容
            失败：格式 "Error: {异常信息}"
        """
        try:
            fp = self.safe_path(path, base)
            if not fp.exists():
                return f"Error: File not found: {path}"
            if not str(fp).lower().endswith('.pdf'):
                return f"Error: Not a PDF file: {path}"
            try:
                import fitz
            except ImportError:
                return "Error: pymupdf 未安装。请运行: python3 -m pip install pymupdf"
            doc = fitz.open(str(fp))
            total_pages = len(doc)
            results = [f"PDF: {path}, 总页数: {total_pages}"]
            read_pages = min(max_pages, total_pages)
            for i in range(read_pages):
                text = doc[i].get_text().strip()
                if text:
                    results.append(f"--- 第 {i+1} 页 ---")
                    results.append(text[:chars_per_page])
                else:
                    results.append(f"--- 第 {i+1} 页 --- (无可提取文本，可能为扫描件)")
            if total_pages > read_pages:
                results.append(f"... (还有 {total_pages - read_pages} 页未读取，可增大 max_pages 参数)")
            doc.close()
            return "\n".join(results)[:30000]

        except Exception as e:
            return f"Error: {e}"

    def run_write(self, path: str, content: str, base: Path | None = None) -> str:
        """
        写入内容到文件
        功能特性：
        - 使用 safe_path 进行安全路径验证
        - 自动创建父目录：如果父目录不存在会递归创建
        - 覆盖写入：目标文件已存在会被覆盖
        - 返回写入字节数，便于验证
        参数：
            path: 要写入的文件路径（相对路径）
            content: 要写入的内容字符串
            base: 可选，工作根目录（worktree 场景传入 worktree 路径）；None 时用 WORKDIR
        返回：
            成功：格式 "Wrote {字节数} bytes to {路径}"
            失败：格式 "Error: {异常信息}"
        """
        try:
            fp = self.safe_path(path, base)
            # 自动创建父目录
            # parents=True: 递归创建所有不存在的父目录
            # exist_ok=True: 如果目录已存在不报错
            fp.parent.mkdir(parents=True, exist_ok=True)
            # 写入内容（覆盖模式）
            fp.write_text(content)
            return f"已写入： {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error: {e}"

    def run_edit(self, path: str, old_text: str, new_text: str, base: Path | None = None) -> str:
        """
        替换文件中的指定文本
        功能特性：
        - 使用 safe_path 进行安全路径验证
        - 精确替换：只替换第一处匹配（使用 count=1）
        - 先检查再写入：验证old_text存在后才执行替换
        - 原子性保证：读取和写入之间可能存在竞态条件
        参数：
            path: 要编辑的文件路径（相对路径）
            old_text: 要被替换的原文本（必须是完整的连续字符串）
            new_text: 替换后的新文本
            base: 可选，工作根目录（worktree 场景传入 worktree 路径）；None 时用 WORKDIR
        返回：
            成功：格式 "Edited {路径}"
            失败（文本未找到）：格式 "Error: Text not found in {路径}"
            失败（其他）：格式 "Error: {异常信息}"
        """
        try:
            fp = self.safe_path(path, base)
            # 读取文件全部内容
            content = fp.read_text()
            # 检查要替换的文本是否存在于文件中
            if old_text not in content:
                return f"Error: Text not found in {path}"
            # 执行替换：只替换第一处匹配
            # replace(old_text, new_text, 1) 中的 1 表示只替换一次
            fp.write_text(content.replace(old_text, new_text, 1))
            return f"已编辑： {path}"
        except Exception as e:
            return f"Error: {e}"

    def run_glob(self, pattern: str, base: Path | None = None) -> str:
        """
        使用 glob 模块搜索匹配的文件路径
        功能特性：
        - 使用 safe_path 进行安全路径验证
        - 仅返回相对于工作目录的路径
        参数：
            pattern: 要匹配的文件路径模式（支持 glob 模式）
            base: 可选，工作根目录（worktree 场景传入 worktree 路径）；None 时用 WORKDIR
        返回：
            成功：匹配的文件路径列表（每个路径占一行）
            失败：格式 "Error: {异常信息}"
        """
        base = base or WORKDIR
        try:
            results = []
            for match in g.glob(pattern, root_dir=base):
                if (base / match).resolve().is_relative_to(base):
                    results.append(match)
            return "\n".join(results) if results else "(no matches)"
        except Exception as e:
            return f"Error: {e}"

    # ═══════════════════════════════════════════════════════════
    #  工具处理器映射（工具名 → 可调用对象）
    # ═══════════════════════════════════════════════════════════

    def _build_handlers(self) -> dict:
        """建立工具名称到调用入口的映射（懒构建、可缓存）。

        当大模型返回工具调用请求时，agent 循环 / SubAgent 按工具名
        从这里取出处理器执行。holder 型依赖（todo / background）在
        调用时才 get，保证运行期注入后依然拿到同一实例。
        """
        return {
            "bash":        lambda **kw: self.run_bash(kw["command"]),
            "run_read":    lambda **kw: self.run_read(kw["path"], kw.get("limit")),
            "run_read_pdf": lambda **kw: self.run_read_pdf(
                kw["path"], kw.get("max_pages", 5), kw.get("chars_per_page", 3000)),
            "run_write":   lambda **kw: self.run_write(kw["path"], kw["content"]),
            "run_edit":    lambda **kw: self.run_edit(kw["path"], kw["old_text"], kw["new_text"]),
            "run_glob":    lambda **kw: self.run_glob(kw["pattern"]),
            "todo":        lambda **kw: self.get_todo_manager().update(kw["items"], kw.get("fresh_start", False)),
            "load_skill":  lambda **kw: self.skills.load_skill(kw["name"]),
            "list_skills": lambda **kw: self.skills.list_skills(),
            "write_memory":   lambda **kw: self.memory.write(kw["name"], kw["type"], kw["description"], kw["body"]),
            "forget_memory":  lambda **kw: self.memory.forget(kw["name"]),
            "create_task": lambda **kw: self.task_manager.run_create_task(
                subject=kw["subject"],
                description=kw.get("description", ""),
                blockedBy=kw.get("blockedBy"),
            ),
            "list_tasks": lambda **kw: self.task_manager.run_list_tasks(),
            "get_task": lambda **kw: self.task_manager.run_get_task(kw["task_id"]),
            "claim_task": lambda **kw: self.task_manager.run_claim_task(kw["task_id"]),
            "complete_task": lambda **kw: self.task_manager.run_complete_task(kw["task_id"]),
            # check_background：仅查询语义，不消费结果；可重复调用。
            # agent_full_v2.py 在每个 turn 开头以及 turn 内每轮 tool 执行后，
            # 会自动把已完成任务以 <task_notification> 注入上下文（消费语义），
            # 本工具是"主动查询"补充，用于模型想看还未被消费的任务当前状态。
            "check_background": lambda **kw: self.get_background_manager().check(kw.get("task_id")),
            # cron 工具（s14）：调度器为 None 时返回占位错误
            "schedule_cron": lambda **kw: (
                self._cron_scheduler.run_schedule_cron(
                    kw["cron"], kw["prompt"],
                    kw.get("recurring", True), kw.get("durable", True)
                ) if self._cron_scheduler else "Error: Cron scheduler not available"
            ),
            "list_crons": lambda **kw: (
                self._cron_scheduler.run_list_crons()
                if self._cron_scheduler else "Error: Cron scheduler not available"
            ),
            "cancel_cron": lambda **kw: (
                self._cron_scheduler.run_cancel_cron(kw["job_id"])
                if self._cron_scheduler else "Error: Cron scheduler not available"
            ),
            # ── 团队成员工具（s17 自主智能体）──
            # teammate_manager 为 None 时返回占位错误（与 cron 工具一致的 holder 语义）
            "spawn_teammate": lambda **kw: (
                self.get_teammate_manager().spawn_teammate(
                    kw["name"], kw["role"], kw["prompt"]
                )
            ),
            "send_message": lambda **kw: (
                self.get_teammate_manager().send_message(
                    kw["to"], kw["content"])
            ),
            "check_inbox": lambda **kw: (
                self.get_teammate_manager().check_inbox()
            ),
            "request_shutdown": lambda **kw: (
                self.get_teammate_manager().request_shutdown(kw["teammate"])
            ),
            "request_plan": lambda **kw: (
                self.get_teammate_manager().request_plan(
                    kw["teammate"], kw["task"])
            ),
            "review_plan": lambda **kw: (
                self.get_teammate_manager().review_plan(
                    kw["request_id"], kw["approve"], kw.get("feedback", ""))
            ),
            # ── worktree 工具（s18）──
            # worktree_manager 为 None 时抛 RuntimeError（与 teammate holder 语义一致）
            "create_worktree": lambda **kw: self.get_worktree_manager().create(
                kw["name"]),
            "list_worktrees": lambda **kw: self.get_worktree_manager().list_all(),
            "remove_worktree": lambda **kw: self.get_worktree_manager().remove(
                kw["name"], kw.get("discard_changes", False)),
            "keep_worktree": lambda **kw: self.get_worktree_manager().keep(
                kw["name"]),
            # ── MCP 工具（s19）──
            # mcp_manager 为 None 时返回占位错误（与 cron 工具一致的 holder 语义）
            "connect_mcp": lambda **kw: (
                self._mcp_manager.connect(kw["name"])
                if self._mcp_manager else "Error: MCP not available"
            ),
            "list_mcp": lambda **kw: (
                "Available MCP servers:\n" + self._mcp_manager.catalog_text()
                if self._mcp_manager else "Error: MCP not available"
            ),
            # ── 工作流工具（s16）──
            # workflow_manager 为 None 时返回占位错误（与 cron/mcp holder 语义一致）。
            # run_sync 内部把 WorkflowInputError 转成 "Error: ..." 文本回给模型。
            "run_workflow": lambda **kw: (
                self.get_workflow_manager().run_sync(
                    kw["name"], kw.get("args"), kw.get("resume_from_run_id"))
            ),
        }

    @property
    def handlers(self) -> dict:
        """工具名 → 处理器的映射（懒构建 + 缓存）。"""
        if self._handlers_cache is None:
            self._handlers_cache = self._build_handlers()
        return self._handlers_cache

    def scoped_handlers(self, cwd: Path) -> dict:
        """返回 handler 的浅拷贝，仅文件类工具改用 `base=cwd` 调用。

        供子智能体 / 队友注入工作目录（worktree）使用，让文件操作落在
        指定 cwd（如 WORKTREE_DIR/<name>）内。其余工具（todo/技能/记忆/
        任务/团队等）复用共享 handlers，不改动 lead 的 handlers 本体。
        """
        scoped = self.handlers.copy()
        scoped["bash"] = lambda **kw: self.run_bash(kw["command"], base=cwd)
        scoped["run_read"] = lambda **kw: self.run_read(
            kw["path"], kw.get("limit"), base=cwd)
        scoped["run_read_pdf"] = lambda **kw: self.run_read_pdf(
            kw["path"], kw.get("max_pages", 5), kw.get("chars_per_page", 3000),
            base=cwd)
        scoped["run_write"] = lambda **kw: self.run_write(
            kw["path"], kw["content"], base=cwd)
        scoped["run_edit"] = lambda **kw: self.run_edit(
            kw["path"], kw["old_text"], kw["new_text"], base=cwd)
        scoped["run_glob"] = lambda **kw: self.run_glob(kw["pattern"], base=cwd)
        return scoped

    # ═══════════════════════════════════════════════════════════
    #  工具定义（初始化时传给大模型，告诉它有哪些工具可用）
    # ═══════════════════════════════════════════════════════════

    @property
    def base_tools(self) -> list:
        """基础工具，主要是子智能体可用的工具。

        工具定义遵循 OpenAI SDK 格式：每个工具用 type="function" 包装，
        参数定义在 function.parameters 下。
        """
        if self._base_tools_cache is None:
            self._base_tools_cache = [
                {
                    "type": "function",
                    "function": {
                        "name": "bash", "description": "执行 shell 命令。",
                        "parameters": {"type": "object", "properties": {
                            "command": {"type": "string"},
                            "run_in_background": {"type": "boolean", "default": False,
                                "description": "True 时把命令丢到后台线程异步执行，立即返回任务 ID；"
                                               "不传则按启发式（install/build/test 等关键词）兜底判断。"},
                            "parallel": {"type": "boolean", "default": False,
                                "description": "True 时与同次响应中其他独立工具调用并行执行。"
                                               "只对无副作用、无共享状态的命令声明（如多个独立查询、多个独立 ls/wc/grep）。"
                                               "链式命令（cd && make）、修改状态的命令（rm/mv/install/build/test）不要传 True。"}
                        }, "required": ["command"]}
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_read", "description": "读取文件内容。",
                        "parameters": {"type": "object", "properties": {
                            "path": {"type": "string"},
                            "limit": {"type": "integer"},
                            "parallel": {"type": "boolean", "default": False,
                                "description": "True 时与同次响应中其他独立文件读取并行执行。多个互不依赖的 read 一起发可提速。"}
                        }, "required": ["path"]}
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_read_pdf", "description": "使用 pymupdf 安全读取 PDF 文件，分页提取文本。读取 PDF 时必须使用此工具，不要使用 bash 的 strings/cat 等命令。",
                        "parameters": {"type": "object", "properties": {
                            "path": {"type": "string", "description": "PDF 文件路径"},
                            "max_pages": {"type": "integer", "description": "最大读取页数，默认5"},
                            "chars_per_page": {"type": "integer", "description": "每页最大字符数，默认3000"},
                            "parallel": {"type": "boolean", "default": False,
                                "description": "True 时与同次响应中其他独立 PDF 读取并行执行。批量读 PDF 时一起发可大幅提速。"}
                        }, "required": ["path"]}
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_write", "description": "将内容写入文件。",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_edit", "description": "替换文件中指定的文本内容。",
                        "parameters": {"type": "object", "properties": {
                            "path": {"type": "string"},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"}
                        }, "required": ["path", "old_text", "new_text"]}
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_glob", "description": "使用 glob 模式匹配文件路径。",
                        "parameters": {"type": "object", "properties": {
                            "pattern": {"type": "string", "description": "要匹配的文件路径模式"},
                            "parallel": {"type": "boolean", "default": False,
                                "description": "True 时与同次响应中其他独立 glob 搜索并行执行。多个互不依赖的 pattern 一起发可提速。"}
                        }, "required": ["pattern"]}
                    }
                },
            ]
        return self._base_tools_cache

    @property
    def tools(self) -> list:
        """主智能体全部工具 = 基础工具 + 待办/技能/记忆/任务/后台查询。"""
        if self._tools_cache is None:
            self._tools_cache = [
                *self.base_tools,
                {"type": "function", "function": {
                    "name": "todo",
                    "description": "更新当前会话的待办列表。整体替换语义：传入完整的 items 数组即可。对复杂任务建议在动手前先调用一次（把计划铺开），执行中逐步把对应项标记为 in_progress / completed。fresh_start=True 表示开始新计划——会先丢弃当前列表里所有已完成的任务，适合在同一会话内切换到下一个独立任务时使用。",
                    "parameters": {"type": "object", "properties": {
                        "items": {"type": "array", "description": "完整的待办事项列表。", "items": {"type": "object", "properties": {
                            "id": {"type": "string", "description": "任务标识，可省略，省略时按数组下标生成。"},
                            "text": {"type": "string", "description": "任务内容（必填）。"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"], "description": "任务状态；同一时刻只能有 1 个 in_progress。"},
                        }, "required": ["text", "status"]}},
                        "fresh_start": {"type": "boolean", "default": False, "description": "True 时表示开始新计划——先清掉当前列表里所有已完成的任务，再用 items 替换整个列表。"},
                    }, "required": ["items"]}
                }},
                {"type": "function", "function": {
                    "name": "load_skill", "description": "加载指定名称的专业技能（skill）知识。",
                    "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "要加载的专业技能（skill）名称"}}, "required": ["name"]}
                }},
                {"type": "function", "function": {
                    "name": "list_skills", "description": "获取当前所有可用技能（skill）的名称和简短描述列表，用于了解当前会话支持哪些技能。",
                    "parameters": {"type": "object", "properties": {
                        "parallel": {"type": "boolean", "default": False,
                            "description": "True 时与同次响应中其他独立查询并行执行。"}
                    }}
                }},
                # ── [改动 2] 新增：记忆工具 ──────────────────────────────
                {"type": "function", "function": {
                    "name": "write_memory",
                    "description": "Save a piece of information to persistent memory. "
                                   "Use when the user states a preference, corrects you, "
                                   "approves an approach, reveals a project fact, or asks you to remember something. "
                                   "The memory will be available in future sessions.",
                    "parameters": {"type": "object", "properties": {
                        "name": {"type": "string", "description": "Short kebab-case identifier, e.g. 'user-preference-tabs'"},
                        "type": {"type": "string",
                                  "enum": ["user", "feedback", "project", "reference"],
                                  "description": "user=preference/habit, feedback=guidance/correction, project=fact/decision, reference=external pointer"},
                        "description": {"type": "string", "description": "One-line summary shown in MEMORY.md index"},
                        "body": {"type": "string", "description": "Full detail in markdown. Include context and rationale."}
                    }, "required": ["name", "type", "description", "body"]}
                }},
                {"type": "function", "function": {
                    "name": "forget_memory",
                    "description": "Delete a memory by its name or filename. "
                                   "Use when the user contradicts a saved memory or asks you to forget something.",
                    "parameters": {"type": "object", "properties": {
                        "name": {"type": "string", "description": "Name of the memory to delete (slug or filename)"}
                    }, "required": ["name"]}
                }},
                # ── [改动 3] 新增：任务管理工具 ──────────────────────────────
                {"type": "function", "function": {
                    "name": "create_task",
                    "description": "Create a new task with optional blockedBy dependencies.",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "subject": {"type": "string"},
                                       "description": {"type": "string"},
                                       "blockedBy": {"type": "array",
                                                     "items": {"type": "string"}}},
                                   "required": ["subject"]}
                }},
                {"type": "function", "function": {
                    "name": "list_tasks",
                    "description": "List all tasks with status, owner, and dependencies.",
                    "parameters": {"type": "object", "properties": {
                        "parallel": {"type": "boolean", "default": False,
                            "description": "True 时与同次响应中其他独立查询并行执行。"}
                    },
                    "required": []}
                }},
                {"type": "function", "function": {
                    "name": "get_task",
                    "description": "Get full details of a specific task by ID.",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "task_id": {"type": "string"},
                                       "parallel": {"type": "boolean", "default": False,
                                           "description": "True 时与同次响应中其他独立查询并行执行。多个 get_task 一起发可提速。"}
                                   },
                                   "required": ["task_id"]}
                }},
                {"type": "function", "function": {
                    "name": "claim_task",
                    "description": "Claim a pending task. Sets owner, changes status to in_progress.",
                    "parameters": {"type": "object",
                                   "properties": {"task_id": {"type": "string"}},
                                   "required": ["task_id"]}
                }},
                {"type": "function", "function": {
                    "name": "complete_task",
                    "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
                    "parameters": {"type": "object",
                                   "properties": {"task_id": {"type": "string"}},
                                   "required": ["task_id"]}
                }},
                {"type": "function", "function": {
                    "name": "check_background",
                    "description": (
                        "查询后台任务状态。仅查询语义，不消费结果，可重复调用。\n"
                        "- 不传 task_id：列出所有后台任务的当前状态（running / completed / notified），已完成或已通知的任务会附带结果预览。\n"
                        "- 传 task_id：返回该任务的详细状态；若已完成或已通知则返回完整结果。\n\n"
                        "适用场景：\n"
                        "1. 用户问后台任务执行得怎么样了时，主动查而不是再起一个新任务。\n"
                        "2. 后台 sub_agent 已派发但还没收到 task_notification 时，确认是否还在跑。\n"
                        "3. task_notification 已注入但 <summary> 截断不够用时，再用 task_id 取完整结果。\n"
                        "4. 排查后台任务是否失败（status=error 也会出现在列表里）。\n\n"
                        "注意：本工具只查询、不消费结果。优先用主循环自动注入的 task_notification 里的 <full_output>；"
                        "如需重复取完整结果再调用本工具的 task_id 参数。"
                    ),
                    "parameters": {"type": "object",
                                   "properties": {
                                       "task_id": {"type": "string",
                                                   "description": "可选，指定任务 ID（如 bg_0001）。不传则列出所有后台任务。"},
                                       "parallel": {"type": "boolean", "default": False,
                                           "description": "True 时与同次响应中其他独立查询并行执行。"}
                                   }}
                }},
                # ── cron 定时任务工具（s14）──────────────────────────────
                {"type": "function", "function": {
                    "name": "schedule_cron",
                    "description": "Schedule a cron job. cron is 5-field: min hour dom month dow.",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "cron": {"type": "string",
                                                "description": "5-field cron expression"},
                                       "prompt": {"type": "string",
                                                  "description": "Message to inject when fired"},
                                       "recurring": {"type": "boolean",
                                                     "description": "True=recurring, False=one-shot"},
                                       "durable": {"type": "boolean",
                                                   "description": "True=persist to disk"}},
                                   "required": ["cron", "prompt"]}
                }},
                {"type": "function", "function": {
                    "name": "list_crons",
                    "description": "List all registered cron jobs.",
                    "parameters": {"type": "object", "properties": {
                        "parallel": {"type": "boolean", "default": False,
                            "description": "True 时与同次响应中其他独立查询并行执行。"}
                    },
                    "required": []}
                }},
                {"type": "function", "function": {
                    "name": "cancel_cron",
                    "description": "Cancel a cron job by ID.",
                    "parameters": {"type": "object",
                                   "properties": {"job_id": {"type": "string"}},
                                   "required": ["job_id"]}
                }},
                # 团队成员工具由 _team_tool_defs() 提供，避免与 tools 内联重复
                *self._team_tool_defs(),
                # worktree 管理工具（s18）：仅 Lead 创建/删除，两种模式都可见
                *self._worktree_tool_defs(),
                # MCP 发现工具（s19）：connect_mcp（name 枚举可用服务器）+ list_mcp（查目录），
                # 连上后其 mcp__* 工具由 build_agent_tools() 动态追加（见 _mcp_tool_defs）
                *self._mcp_discovery_tool_defs(),
                # 工作流工具（s16）：按名运行已保存的编排脚本（计划即代码）
                {"type": "function", "function": {
                    "name": "run_workflow",
                    "description": "Run a saved workflow by name. A workflow is a "
                                   "pre-registered deterministic orchestration script "
                                   "(multi-subagent plan as code): it spawns focused "
                                   "single-step subagents, with journal-based resume "
                                   "support. Pass input in args; pass resume_from_run_id "
                                   "to resume a previous run (unchanged steps replay "
                                   "from cache). Available names are limited to the "
                                   "host registry (e.g. 'review-changes': review "
                                   "changed code across dimensions and adversarially "
                                   "verify each finding).",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "name": {"type": "string",
                                           "description": "注册表中工作流的名字，如 'review-changes'"},
                                       "args": {"type": "object",
                                           "description": "工作流入参（可选）。review-changes 的入参："
                                                          "changes（变更代码文本）、budget（token 上限，null 不限）"},
                                       "resume_from_run_id": {"type": "string",
                                           "description": "可选，上次运行的 runId（wf_* 格式）。"
                                                          "传入后续跑：未变化的步骤直接命中缓存重放。"}},
                                   "required": ["name"]}
                }},
            ]
        return self._tools_cache

    # ── 团队工具定义（s17自主智能体）──仅团队模式下喂给 LLM ─────────
    def _team_tool_defs(self) -> list:
        """6 个团队工具定义（spawn/send_message/check_inbox/协议工具）。

        供 `tools` 属性展开使用；默认（非团队）模式下会被 default_agent_tools
        整体剔除，不暴露给 LLM，从而省掉这部分的 schema token 开销。
        """
        return [
            {"type": "function", "function": {
                "name": "spawn_teammate",
                "description": "Spawn an autonomous teammate agent in a "
                               "background thread. Teammate runs a "
                               "WORK→IDLE loop: lists and claims tasks "
                               "from the board, checks inbox, then shuts "
                               "down. Use with send_message / check_inbox "
                               "/ request_shutdown / request_plan / "
                               "review_plan.",
                "parameters": {"type": "object",
                               "properties": {
                                   "name": {"type": "string"},
                                   "role": {"type": "string"},
                                   "prompt": {"type": "string"},
                                   "worktree": {"type": "string",
                                       "description": "可选，已创建 worktree 的名称。给定时队友在 WORKTREE_DIR/<worktree> 内作业（文件操作根指向该目录）。"}},
                               "required": ["name", "role", "prompt"]}
            }},
            {"type": "function", "function": {
                "name": "send_message",
                "description": "Send a message to a teammate.",
                "parameters": {"type": "object",
                               "properties": {
                                   "to": {"type": "string"},
                                   "content": {"type": "string"}},
                               "required": ["to", "content"]}
            }},
            {"type": "function", "function": {
                "name": "check_inbox",
                "description": "Check Lead's inbox for teammate messages "
                               "and protocol responses (plan/shutdown). "
                               "Routes protocol responses to their "
                               "original requests.",
                "parameters": {"type": "object", "properties": {},
                               "required": []}
            }},
            # 协议工具（队友管理）
            {"type": "function", "function": {
                "name": "request_shutdown",
                "description": "Request a teammate to shut down "
                               "gracefully.",
                "parameters": {"type": "object",
                               "properties": {"teammate": {"type": "string"}},
                               "required": ["teammate"]}
            }},
            {"type": "function", "function": {
                "name": "request_plan",
                "description": "Ask a teammate to submit a plan for "
                               "review.",
                "parameters": {"type": "object",
                               "properties": {
                                   "teammate": {"type": "string"},
                                   "task": {"type": "string"}},
                               "required": ["teammate", "task"]}
            }},
            {"type": "function", "function": {
                "name": "review_plan",
                "description": "Approve or reject a submitted plan by "
                               "request_id.",
                "parameters": {"type": "object",
                               "properties": {
                                   "request_id": {"type": "string"},
                                   "approve": {"type": "boolean"},
                                   "feedback": {"type": "string"}},
                               "required": ["request_id", "approve"]}
            }},
        ]

    # ── worktree 管理工具定义（s18）── Lead 侧创建/删除/保留 ────────
    def _worktree_tool_defs(self) -> list:
        """4 个 worktree 管理工具定义（create/list/remove/keep）。

        供 `tools` 属性展开使用（default/main 两种模式都可见）。子智能体/队友
        只作为 cwd 的"消费者"（workdir/worktree 参数），不暴露这些管理工具。
        """
        return [
            {"type": "function", "function": {
                "name": "create_worktree",
                "description": "创建带独立分支的隔离 git worktree（分支 wt/<name>，位于 WORKTREE_DIR/<name>）。"
                               "创建时自动检测仓库技术栈并软链对应运行时（如 .venv、node_modules、target 等），"
                               "供 agent 在其内部运行/测试。"
                               "仅创建工作区，不绑定任务；随后用 sub_agent(workdir=...) 或 spawn_teammate(worktree=...) 把 agent 派进去作业。",
                "parameters": {"type": "object",
                               "properties": {"name": {"type": "string",
                                   "description": "worktree 名称（字母/数字/点/下划线/连字符，1-64 字符）"},
                                   "stack": {"type": "string",
                                       "description": "可选，强制指定技术栈标签（不传时自动检测）。"
                                                       "常见值：python、node/typescript、go、rust、java-maven、"
                                                       "java-gradle、c-cpp、php、ruby、dotnet、dart-flutter、terraform。"}},
                               "required": ["name"]}
            }},
            {"type": "function", "function": {
                "name": "list_worktrees",
                "description": "列出所有已创建的 worktree（名称/分支/路径/供给概览）。",
                "parameters": {"type": "object", "properties": {},
                               "required": []}
            }},
            {"type": "function", "function": {
                "name": "remove_worktree",
                "description": "移除指定 worktree 及其专属分支。默认拒绝移除有未提交改动或未推送提交的 worktree；"
                               "确需丢弃时传 discard_changes=true 强制移除。",
                "parameters": {"type": "object",
                               "properties": {
                                   "name": {"type": "string"},
                                   "discard_changes": {"type": "boolean",
                                       "description": "True 时忽略未提交/未推送检查，强制移除。"}},
                               "required": ["name"]}
            }},
            {"type": "function", "function": {
                "name": "keep_worktree",
                "description": "保留指定 worktree 供人工复核（不删除分支）。",
                "parameters": {"type": "object",
                               "properties": {"name": {"type": "string"}},
                               "required": ["name"]}
            }},
        ]

    # ── MCP 发现工具定义（s19）──────────────────────────────
    def _mcp_discovery_tool_defs(self) -> list:
        """MCP 「发现」工具定义：connect_mcp（name 枚举可用服务器）+ list_mcp（查目录）。

        痛点：仅靠 connect_mcp 时，LLM 不知道存在哪些服务器、能力藏在哪个 MCP 里，
        只能靠“猜名字”瞎试。这里动态读 MCPManager 的可连接目录（available_servers /
        catalog_text），让 LLM 在调用前就知道「有哪些服务器、各自提供哪些工具」，
        从而作出是否 connect、连哪个的决定。

        仍**不预加载**各工具的完整参数 schema——连接成功后才由 build_agent_tools() 动态
        追加 mcp__{server}__{tool}，保持 s19「动态工具池、省 token」的设计。
        manager 未注入时 name 枚举降级为空、目录文本提示未配置。
        """
        mgr = self._mcp_manager
        enum = list(mgr.available_servers()) if mgr else []
        catalog_txt = mgr.catalog_text() if mgr else ""

        connect_def = {"type": "function", "function": {
            "name": "connect_mcp",
            "description": (
                "Reconnect a configured MCP server that is currently disconnected or "
                "failed to connect (configured servers are auto-loaded at startup). "
                "After connecting, its tools become available as mcp__{server}__{tool} "
                "in subsequent rounds (dynamic tool pool). "
                "Choose the server whose exposed capability matches what you need.\n"
                "Available servers:" + ("\n" + catalog_txt if catalog_txt else " none")
            ),
            "parameters": {"type": "object",
                           "properties": {"name": {"type": "string",
                               "enum": enum,
                               "description": "MCP server name to connect."}},
                           "required": ["name"]}
        }}

        list_def = {"type": "function", "function": {
            "name": "list_mcp",
            "description": "List available MCP servers and the tools each exposes. "
                           "Use this to check whether a capability lives in an MCP "
                           "server before calling connect_mcp.",
            "parameters": {"type": "object", "properties": {},
                           "required": []}
        }}

        return [connect_def, list_def]

    def _mcp_tool_defs(self) -> list:
        """动态组装当前已连接 MCP 服务器的工具定义（OpenAI 格式）。

        每轮现场组装（不缓存），保证 connect_mcp 后 LLM 立即可见新工具。
        MCPManager 未注入时返回空列表。
        """
        if self._mcp_manager is None:
            return []
        return self._mcp_manager.assemble_tools()

    def build_agent_tools(self, team_mode: bool = False) -> list:
        """组装喂给 LLM 的工具集 = 基础（团队/默认）+ 已连接 MCP 工具。

        与 default_agent_tools / main_agent_tools 的差异：这里是**动态方法**，
        每次调用都把已连接 MCP 服务器的工具合并进去，实现 s19 的"动态工具池"。
        主循环 agent_loop 每轮调用它，connect_mcp 后下一轮即可看到新工具。
        """
        base = self.main_agent_tools if team_mode else self.default_agent_tools
        mcp_defs = self._mcp_tool_defs()
        if not mcp_defs:
            return base
        return [*base, *mcp_defs]

    @property
    def main_agent_tools(self) -> list:
        """团队模式工具集 = 全部工具（含团队工具）+ sub_agent。

        仅在 `/teams` 进入团队模式时由主循环喂给 LLM。
        """
        if self._main_agent_tools_cache is None:
            self._main_agent_tools_cache = [
                *self.tools,
                self._sub_agent_tool_def(),
            ]
        return self._main_agent_tools_cache

    @property
    def default_agent_tools(self) -> list:
        """默认（子智能体）模式工具集 = 全部工具剔除团队工具 + sub_agent。

        缺少团队工具的 schema，省 token，并避免模型误触发代价高昂的团队协作。
        """
        if self._default_agent_tools_cache is None:
            non_team = [t for t in self.tools
                        if t["function"]["name"] not in TEAM_TOOL_NAMES]
            self._default_agent_tools_cache = [
                *non_team, self._sub_agent_tool_def(),
            ]
        return self._default_agent_tools_cache

    # ── sub_agent 工具定义（默认与团队模式共用）────────────────────
    def _sub_agent_tool_def(self) -> dict:
        """sub_agent 工具定义（分发子任务给通用型子智能体）。"""
        return {"type": "function", "function": {
            "name": "sub_agent",
            "description": "分发子任务给通用型子智能体。子智能体拥有独立上下文（不污染主对话），共享文件系统，只返回最终摘要。子智能体默认拥有执行工具权限，但不包含 task 系列工具；任务看板只由主智能体维护。当任务需要多步骤操作、读取多个文件、收集信息或可能产生大量工具调用时使用。\n\n⚠️ 强制规则（必须遵守，违例会阻塞主循环浪费时间）：\n凡是「批量 / 全量 / 跨多个文件 / 跨整个目录 / 预计耗时 > 30 秒」的任务，**必须传 run_in_background=true** 丢到后台线程异步执行，立即返回任务 ID，结果通过后续轮次的 <task_notification> 收回。绝对不要同步等待这类任务完成。\n判断标准（命中任意一条就必须后台）：\n  - 涉及 ≥ 2 个文件 / 整个目录 / 全部 N 个 X\n  - prompt 含「全部 / 全量 / 批量 / 跑一遍 / 扫描 / 审计 / 构建 / 测试套件」等关键词\n  - 需要多步骤工具调用且总耗时可能 > 30 秒\n允许同步（run_in_background 默认 false）的场景：\n  - 单个文件的快速查询、单步工具调用\n  - 必须等前序结果才能继续的下一步操作\n\n如果多个子任务之间没有依赖关系，设置 parallel=true 让它们并行执行以提升效率；串行时设为 false。注意：run_in_background 与 parallel 互斥——已传 run_in_background=true 时不要再传 parallel。\n\n可通过 allowed_tools 限制子智能体的工具范围，例如只允许只读操作。\n\n示例：\n- sub_agent(prompt=\"读取 DRG_Docs 目录下全部 19 个 PDF 的标题和摘要\", run_in_background=true)  ← 批量全目录，必须后台\n- sub_agent(prompt=\"实现用户注册功能\", parallel=\"false\")\n- sub_agent(prompt=\"分析当前代码架构并设计重构方案\", parallel=\"false\")\n- sub_agent(prompt=\"只读方式搜索代码中的安全问题\", allowed_tools=[\"bash\",\"read_file\",\"read_pdf\"], parallel=\"true\")\n- sub_agent(prompt=\"跑全量测试并报告失败用例\", parallel=\"false\", run_in_background=true)",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "给子智能体的任务描述，应具体说明要做什么"},
                    "description": {"type": "string", "description": "任务的简短描述，用于日志记录"},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "限制子智能体可用的工具名称列表。不设置则默认使用全部工具。例如 [\"bash\",\"read_file\",\"read_pdf\"] 限制为只读工具集"},
                    "parallel": {"type": "boolean", "description": "是否与其他 sub_agent 并行执行。"},
                    "run_in_background": {"type": "boolean", "default": False,
                        "description": "True 时把子任务丢到后台线程异步执行，立即返回后台任务 ID；"
                                       "结果通过 <task_notification> 在后续轮次通知。"
                                       "与 parallel 互斥：传 True 时不再走并行/串行等待桶。"},
                    "workdir": {"type": "string",
                        "description": "可选，已创建 worktree 的名称。给定时子智能体的工作目录（所有文件操作根）为 WORKTREE_DIR/<workdir>，"
                                       "用于在隔离 worktree 内改代码并运行测试。需先 create_worktree 创建。"}
                },
                "required": ["prompt", "parallel"]
            }
        }}

    # ═══════════════════════════════════════════════════════════
    #  统一执行入口
    # ═══════════════════════════════════════════════════════════

    def resolve_handler(self, tool_name: str):
        """按工具名解析处理器：先查静态 handlers，未命中且为 mcp__* 时查 MCP 管理器。

        返回可调用对象或 None（未找到）。MCP 工具是动态发现的，不预先注册进静态
        handlers，故通过本方法在运行时委托给对应 MCP 客户端。
        """
        handler = self.handlers.get(tool_name)
        if handler is not None:
            return handler
        if tool_name.startswith("mcp__") and self._mcp_manager is not None:
            return self._mcp_manager.assemble_handlers().get(tool_name)
        return None

    def execute(self, tool_name: str, **tool_args) -> str:
        """按工具名执行一次工具调用；未知工具返回错误字符串。"""
        handler = self.resolve_handler(tool_name)
        if handler is None:
            return f"Error: Unknown tool {tool_name}"
        return handler(**tool_args)
